"""Runtime files, instance locks and local control requests (not gateway configuration)."""

import ctypes
import json
import msvcrt
import os
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

DEFAULT_PORT = 10101
PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = PROJECT_ROOT / "frontend" / "dist-runtime"


def state_directory(value: str | None = None) -> Path:
    configured = value or os.environ.get("OPENCODEX_LITE_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(os.environ["LOCALAPPDATA"]) / "OpenCodexLite"


@contextmanager
def instance_lock(directory: Path, name: str):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError(
                "Another ocx command or instance is using this state directory."
            ) from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def read_state(directory: Path) -> dict:
    try:
        return json.loads((directory / "runtime.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        raise RuntimeError(f"Cannot read runtime state in {directory}: {exc}") from exc


def write_state(directory: Path, state: dict) -> None:
    temporary = directory / "runtime.json.tmp"
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    for attempt in range(10):
        try:
            temporary.replace(directory / "runtime.json")
            return
        except PermissionError:
            # Windows can briefly hold the destination open during CLI status polling.
            if attempt == 9:
                raise
            time.sleep(0.05)


def process_alive(pid: int) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE, never terminate access.
    if not handle:
        return ctypes.get_last_error() == 5  # Access denied is not proof of process exit.
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        kernel.CloseHandle(handle)


def control_request(state: dict, route: str, *, method: str = "GET") -> dict:
    request = Request(
        f"http://127.0.0.1:{state['port']}{route}",
        method=method,
        headers={"Authorization": f"Bearer {state['token']}"},
        data=b"" if method == "POST" else None,
    )
    with build_opener(ProxyHandler({})).open(request, timeout=1) as response:
        return json.load(response)


def status(directory: Path) -> dict:
    state = read_state(directory)
    base = {
        "state": "stopped",
        "stateDirectory": str(directory),
        "logPath": str(directory / "runtime.log"),
        "port": state.get("port", DEFAULT_PORT),
    }
    if not state or state.get("state") == "stopped":
        return base
    if state.get("state") == "failed":
        return {**base, "state": "failed", "error": state.get("error", "Startup failed")}
    try:
        live = control_request(state, "/api/status")
        if live.get("service") == "opencodex-lite" and live.get("pid") == state.get("pid"):
            return {**base, **live, "state": "running"}
    except URLError, HTTPError, TimeoutError, OSError, ValueError, KeyError:
        pass
    alive = process_alive(state.get("pid", 0))
    # Shutdown may finish between the initial state read and the failed HTTP probe.
    if not alive and read_state(directory).get("state") == "stopped":
        return base
    return {
        **base,
        "state": "unresponsive" if alive else "failed",
        "pid": state.get("pid"),
        "error": "Instance is not responding."
        if alive
        else "Process exited unexpectedly; see log.",
    }
