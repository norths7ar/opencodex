"""Publish a validated Lite console build and verify an optional service restart."""

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from backend.platform.files import instance_lock

from .scheduler import Startup
from .service import service_command
from .state import control_request, read_state, status

_RETENTION_SECONDS = 7 * 24 * 60 * 60
_HASHED_ASSET = re.compile(r"-[A-Za-z0-9_-]{8,}\.")
_PREFERENCE = "startup-preference.json"


def _relative(value) -> str:
    if not isinstance(value, str) or "\\" in value:
        raise ValueError(f"Invalid build or deployment path: {value!r}")
    if any(
        not part
        or part in {".", ".."}
        or part.endswith((".", " "))
        or any(character in part for character in ':*?"<>|')
        or any(ord(character) < 32 for character in part)
        or os.path.isreserved(part)
        for part in value.split("/")
    ):
        raise ValueError(f"Invalid build or deployment path: {value!r}")
    return value


def _path(root: Path, relative: str) -> Path:
    """Refuse reparse points, including junctions within an otherwise safe path."""
    current = root
    for part in ("", *_relative(relative).split("/")):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError(f"Deployment paths cannot use links or junctions: {current}")
    return current


def _files(root: Path):
    def report(error):
        raise error

    for directory, subdirs, files in root.walk(on_error=report):
        for name in (*subdirs, *files):
            relative = (directory / name).relative_to(root).as_posix()
            path = _path(root, relative)
            if name in files:
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError(f"Deployment input is not a regular file: {path}")
                yield relative, path


class _Index(HTMLParser):
    def __init__(self, source: bytes):
        super().__init__()
        self.files = set()
        self.modules = set()
        self.feed(source.decode("utf-8"))
        self.close()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script":
            address = values.get("src")
        elif tag == "link" and {"stylesheet", "modulepreload", "preload", "icon"} & set(
            (values.get("rel") or "").lower().split()
        ):
            address = values.get("href")
        else:
            return
        if not address:
            return
        url = urlsplit(address)
        if url.scheme or url.netloc or not url.path:
            raise ValueError(f"The built index references a non-local resource: {address!r}")
        path = _relative(unquote(url.path).removeprefix("/").removeprefix("./"))
        self.files.add(path)
        if tag == "script" and values.get("type") == "module":
            self.modules.add(path)


@dataclass(frozen=True)
class Build:
    index: bytes
    entry_files: frozenset[str]
    files: dict[str, Path]


def _validate_build(root: Path) -> Build:
    index = _path(root, "index.html").read_bytes()
    metadata = _path(root, ".vite/manifest.json")
    manifest = json.loads(metadata.read_bytes())
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError("The Vite manifest must contain a build graph.")
    files = {name: path for name, path in _files(root) if _managed_resource(name)}

    def require(name):
        if _relative(name) not in files:
            raise ValueError(f"Missing build resource: {name}")

    entries = set()
    for key, entry in manifest.items():
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid Vite manifest entry: {key!r}")
        require(entry.get("file"))
        if entry.get("isEntry") is True:
            entries.add(entry["file"])
        for field in ("css", "assets", "imports", "dynamicImports"):
            references = entry.get(field, [])
            if not isinstance(references, list):
                raise ValueError(f"Invalid Vite manifest reference list: {key!r} ({field})")
            for reference in references:
                if field in {"css", "assets"}:
                    require(reference)
                elif not isinstance(reference, str) or reference not in manifest:
                    raise ValueError(f"Missing Vite dependency: {key!r} ({reference!r})")

    page = _Index(index)
    for name in page.files:
        require(name)
    if not page.modules or not page.modules <= entries:
        raise ValueError("The built index must load a module entry from the Vite manifest.")
    return Build(index, frozenset(page.files), files)


def _digest(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except FileNotFoundError:
        return None


def _atomic_file(path: Path, scratch: Path, write) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=scratch, prefix=f"{date.today():%Y%m%d}-publish-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(10):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.05)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _hashed(name: str) -> bool:
    return name.startswith("assets/") and bool(_HASHED_ASSET.search(Path(name).name))


def _managed_resource(name: str) -> bool:
    name = name.casefold()
    return name != "index.html" and not name.startswith(".vite/")


def _read_inventory(path: Path, web: Path) -> tuple[set[str], dict[str, float]]:
    try:
        data = json.loads(path.read_bytes())
    except FileNotFoundError:
        # First managed update: old browser tabs may use any existing Vite hash.
        if not web.exists():
            return set(), {}
        return {name for name, _ in _files(web) if _hashed(name)}, {}
    if not (
        isinstance(data, dict)
        and data.get("version") == 1
        and isinstance(data.get("current"), list)
        and isinstance(data.get("retired"), dict)
    ):
        raise ValueError("Invalid frontend deployment inventory; no files were changed.")
    current = {_relative(name) for name in data["current"]}
    retired = {_relative(name): when for name, when in data["retired"].items()}
    if any(not _managed_resource(name) for name in current | retired.keys()) or any(
        type(when) not in {int, float} or not 0 < when < 1e12 for when in retired.values()
    ):
        raise ValueError("Invalid frontend deployment inventory; no files were changed.")
    return current, retired


def _write_inventory(path: Path, scratch: Path, current: set[str], retired: dict) -> None:
    active = {name.casefold() for name in current}
    content = {
        "version": 1,
        "current": sorted(current),
        "retired": {
            name: retired[name] for name in sorted(retired) if name.casefold() not in active
        },
    }
    _atomic_file(path, scratch, lambda stream: stream.write(json.dumps(content).encode("utf-8")))


def _protected_files(directory: Path, running: bool) -> dict[Path, str | None]:
    homes = {Path.home() / ".codex"}
    if home := os.environ.get("CODEX_HOME"):
        homes.add(Path(home).expanduser())
    if running:
        # Use the app's resolved home, which can differ from the shell's CODEX_HOME.
        active_home = control_request(read_state(directory), "/api/clients/codex").get("home")
        if not isinstance(active_home, str) or not Path(active_home).is_absolute():
            raise RuntimeError("Could not verify the running gateway's Codex configuration path.")
        homes.add(Path(active_home))
    paths = {directory / _PREFERENCE}
    paths.update(home / name for home in homes for name in ("config.toml", "auth.json"))
    return {path: _digest(path) for path in paths}


def _verify_protected(before: dict[Path, str | None]) -> None:
    changed = [str(path) for path, digest in before.items() if _digest(path) != digest]
    if changed:
        raise RuntimeError("Protected files changed: " + ", ".join(sorted(changed)))


def _running_service(directory: Path):
    before = status(directory)
    if before["state"] == "stopped":
        return None
    if before["state"] != "running":
        raise RuntimeError("Gateway state is not healthy; inspect ocx status before updating.")
    health = Startup(directory, before["port"]).health()
    if not all(
        health.get(field)
        for field in (
            "serviceInstalled",
            "serviceEnabled",
            "serviceViable",
            "serviceRunning",
            "managerRunning",
        )
    ):
        raise RuntimeError(
            "An unmanaged or foreground gateway is running. Stop it with ocx stop "
            "before updating, or enable a healthy background service."
        )
    return before


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _verify_running(directory: Path, before: dict, build: Build) -> None:
    live = status(directory)
    port = before["port"]
    if (
        live["state"] != "running"
        or live["port"] != port
        or live.get("startedAt") == before.get("startedAt")
    ):
        raise RuntimeError("The gateway did not restart on its original port.")
    open_local = build_opener(ProxyHandler({}), _NoRedirect()).open
    base = f"http://127.0.0.1:{port}"
    with open_local(base + "/healthz", timeout=5) as response:
        health = json.loads(response.read(65_536))
    if (
        health.get("status") != "ok"
        or health.get("service") != "opencodex-lite"
        or health.get("pid") != live.get("pid")
        or health.get("port") != port
        or health.get("consoleReady") is not True
    ):
        raise RuntimeError("The restarted gateway failed its health check.")
    with open_local(base + "/", timeout=5) as response:
        if response.read(len(build.index) + 1) != build.index:
            raise RuntimeError("The gateway did not serve the new console index.")
    for name in sorted(build.entry_files):
        with open_local(base + "/" + quote(name), timeout=5) as response:
            if hashlib.file_digest(response, "sha256").hexdigest() != _digest(build.files[name]):
                raise RuntimeError(f"The gateway served an unexpected entry resource: {name}")


def publish(directory: Path, *, build_root: Path, web: Path, scratch: Path) -> bool:
    """Return whether a supervised process was restarted."""
    scratch = _path(scratch.parent, scratch.name)
    with instance_lock(scratch, "frontend-deployment.lock"):
        build = _validate_build(_path(build_root.parent, build_root.name))
        inventory = _path(web.parent, ".deployment-state.json")
        prior_service = _running_service(directory)
        protected = _protected_files(directory, prior_service is not None)
        index = _path(web, "index.html")
        previous_index = index.read_bytes() if index.exists() else None
        current, retired = _read_inventory(inventory, web)
        next_files = set(build.files)
        destination = {name: _path(web, name) for name in next_files}
        for name, target in destination.items():
            if target.exists() and (
                not target.is_file()
                or (_hashed(name) and _digest(target) != _digest(build.files[name]))
            ):
                raise ValueError(f"Refusing to overwrite an incompatible console resource: {name}")

        # Keep both sets reachable if the process stops between the two commits.
        # A later successful publication retires the inactive files for seven days.
        _write_inventory(inventory, scratch, current | next_files, retired)
        switched = False
        try:
            for name, source in sorted(build.files.items()):
                target = destination[name]
                if target.exists() and _hashed(name):
                    continue
                with source.open("rb") as stream:
                    _atomic_file(target, scratch, lambda output: shutil.copyfileobj(stream, output))
            _atomic_file(index, scratch, lambda stream: stream.write(build.index))
            switched = True
            if prior_service:
                service_command(directory, prior_service["port"], "restart")
                _verify_running(directory, prior_service, build)
            _verify_protected(protected)
            now = time.time()
            retired.update({name: now for name in current - next_files})
            _write_inventory(inventory, scratch, next_files, retired)
        except (Exception, KeyboardInterrupt) as cause:
            if not switched:
                raise
            try:
                if previous_index is None:
                    index.unlink()
                else:
                    _atomic_file(index, scratch, lambda stream: stream.write(previous_index))
            except OSError as restore:
                raise RuntimeError(
                    f"Update failed: {cause}. Could not restore the previous console index: "
                    f"{restore}. Python source and dependencies were not rolled back."
                ) from cause
            raise RuntimeError(
                f"Update failed: {cause}. Previous console index restored. "
                "Python source and dependencies were not rolled back; check ocx status."
            ) from cause

        active = {name.casefold() for name in next_files}
        expired = [
            name
            for name, when in retired.items()
            if name.casefold() not in active and when <= now - _RETENTION_SECONDS
        ]
        for name in expired:
            try:
                _path(web, name).unlink(missing_ok=True)
                retired.pop(name)
            except (OSError, ValueError) as exc:
                print(f"Could not prune an expired console file: {exc}", file=sys.stderr)
        if expired:
            try:
                _write_inventory(inventory, scratch, next_files, retired)
            except OSError as exc:
                print(f"Could not record completed console pruning: {exc}", file=sys.stderr)
        return prior_service is not None
