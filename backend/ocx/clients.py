"""Manage only Lite's Codex routing fields; never touch Codex login or history."""

import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import Comment, InlineTable, Table
from tomlkit.toml_document import TOMLDocument

from .runtime import instance_lock

_PROVIDER = "opencodex-lite"
_LOCK_FILE = ".opencodex-lite.lock"
_ROOT_KEYS = ("model", "model_provider")
_MAX_CONFIG = 1 << 20
_MAX_JOURNAL = 6 * _MAX_CONFIG + 2048


class ClientConfigError(RuntimeError):
    """An unsafe or unsuccessful config operation (safe to display without a traceback)."""


def codex_home(value: str | Path | None = None) -> Path:
    configured = value or os.environ.get("CODEX_HOME")
    return (Path(configured).expanduser() if configured else Path.home() / ".codex").resolve()


def _journal_path(home: Path, state_directory: Path) -> Path:
    home_id = _digest(os.path.normcase(str(home)))
    return state_directory / "clients" / home_id / "codex-config.json"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _assert_regular_file(path: Path, label: str) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ClientConfigError(f"{label} is not a regular file; no changes were made.")


def _read(path: Path, limit: int, label: str) -> str | None:
    try:
        _assert_regular_file(path, label)
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ClientConfigError(f"{label} is too large to edit safely.")
        return data.decode("utf-8")
    except FileNotFoundError:
        return None
    except OSError, UnicodeError:
        raise ClientConfigError(f"Could not read {label} as UTF-8.") from None


def _parse(source: str | None) -> TOMLDocument:
    try:
        return tomlkit.parse((source or "").removeprefix("\ufeff"))
    except TOMLKitError, ValueError, RecursionError:
        raise ClientConfigError("Codex config.toml is not valid TOML.") from None


def _render(rendered: str, source: str | None) -> str:
    if source:
        crlf = source.count("\r\n")
        if crlf and crlf == source.count("\n"):
            rendered = re.sub(r"(?<!\r)\n", "\r\n", rendered)
        if source.startswith("\ufeff"):
            rendered = "\ufeff" + rendered
    return rendered


def _atomic_write(path: Path, text: str, label: str) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as f:
            temporary = Path(f.name)
            f.write(text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        temporary.replace(path)
    except OSError:
        raise ClientConfigError(f"Could not write {label}; check file access and retry.") from None
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


@contextmanager
def _write_lock(home: Path):
    try:
        with suppress(FileNotFoundError):
            _assert_regular_file(home / _LOCK_FILE, "Codex configuration lock")
        with instance_lock(home, _LOCK_FILE):
            yield
    except OSError:
        raise ClientConfigError(
            "Could not update Codex configuration files; check file access and retry."
        ) from None


def _read_journal(path: Path) -> dict | None:
    raw = _read(path, _MAX_JOURNAL, "Codex recovery journal")
    if raw is None:
        return None
    try:
        record = json.loads(raw)
        original = record["original"]
        revisions = record["revisions"]
        if (
            record["version"] == 1
            and (original is None or isinstance(original, str))
            and isinstance(revisions, list)
            and 1 <= len(revisions) <= 2
            and all(
                isinstance(rev["pristine"], bool)
                and all(re.fullmatch(r"[0-9a-f]{64}", rev[key]) for key in ("owned", "config"))
                for rev in revisions
            )
        ):
            return record
    except KeyError, TypeError, ValueError, RecursionError:
        pass
    raise ClientConfigError("Codex recovery journal is invalid; it was left untouched.")


def _write_journal(path: Path, original: str | None, revisions: list[dict]) -> None:
    record = {"version": 1, "original": original, "revisions": revisions}
    _atomic_write(path, json.dumps(record, ensure_ascii=False), "Codex recovery journal")


def _owned(values: dict) -> str:
    provider_map = values.get("model_providers")
    provider = provider_map.get(_PROVIDER) if isinstance(provider_map, dict) else None
    fields = [values.get(key) for key in _ROOT_KEYS] + [provider]
    return _digest(_canonical(fields))


def _canonical(value: dict | list) -> str:
    return json.dumps(value, sort_keys=True, default=lambda entry: entry.isoformat())


def _verified(candidate: str, expected: dict) -> TOMLDocument:
    parsed = _parse(candidate)
    if _canonical(parsed.unwrap()) != _canonical(expected):
        raise ClientConfigError(
            "Could not edit this Codex TOML layout without changing other settings."
        )
    return parsed


@dataclass
class _Inspection:
    path: Path
    source: str | None
    document: TOMLDocument
    journal: dict | None
    revision: dict | None = None
    conflict: str | None = None

    def metadata(self) -> dict:
        result = {"configPath": str(self.path), "configExists": self.source is not None}
        if self.conflict:
            return {**result, "state": "conflict", "reason": self.conflict}
        if self.revision is None:
            return {**result, "state": "notConfigured"}
        values = self.document.unwrap()
        return {
            **result,
            "state": "configured",
            "baseUrl": values["model_providers"][_PROVIDER]["base_url"],
            "model": values["model"],
        }


def _inspect(path: Path, journal_path: Path) -> _Inspection:
    source = _read(path, _MAX_CONFIG, "Codex config.toml")
    document = _parse(source)
    journal = _read_journal(journal_path)
    result = _Inspection(path, source, document, journal)
    values = document.unwrap()
    provider_map = values.get("model_providers", {})
    if not isinstance(provider_map, dict) or any(
        key in values and not isinstance(values[key], str) for key in _ROOT_KEYS
    ):
        result.conflict = "Codex model or provider settings are not in a supported format."
    elif journal is not None:
        current = _owned(values)
        result.revision = next(
            (rev for rev in reversed(journal["revisions"]) if current == rev["owned"]), None
        )
        if result.revision is None and current != _owned(_parse(journal["original"]).unwrap()):
            result.conflict = "Codex routing settings changed after Lite configured them."
    elif _PROVIDER in provider_map or values.get("model_provider") == _PROVIDER:
        result.conflict = "The Lite provider already exists without a matching recovery journal."
    else:
        active = values.get("model_provider", "openai")
        table = provider_map.get(active)
        # An authless Desktop placeholder has no URL; a real foreign provider owns its routing.
        if active != "openai" and (not isinstance(table, dict) or table.get("base_url")):
            result.conflict = "An external provider already controls Codex routing."
    return result


def _target(gateway_url: str, model: str | None) -> None:
    port = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/v1", gateway_url)
    if port is None or int(port[1]) > 65535:
        raise ClientConfigError("Codex must use Lite's local HTTP gateway URL.")
    if model is not None and not re.fullmatch(r"[^\s/\x00-\x1f\x7f]+/[^\s\x00-\x1f\x7f]+", model):
        raise ClientConfigError("Select a gateway model in provider/model format.")


def status(home: Path, state_directory: Path) -> dict:
    home = codex_home(home)
    path = home / "config.toml"
    try:
        return _inspect(path, _journal_path(home, state_directory)).metadata()
    except ClientConfigError as exc:
        return {
            "configPath": str(path),
            "configExists": path.exists(),
            "state": "conflict",
            "reason": str(exc),
        }


def preview(home: Path, gateway_url: str, model: str | None, state_directory: Path) -> dict:
    _target(gateway_url, model or None)
    return {**status(home, state_directory), "baseUrl": gateway_url, "model": model or ""}


def _update_document(
    current: _Inspection, gateway_url: str, model: str, gateway_key: str
) -> tuple[str, TOMLDocument]:
    document = current.document
    expected = document.unwrap()
    fields = {
        "name": "OpenCodex Lite",
        "base_url": gateway_url,
        "wire_api": "responses",
        "requires_openai_auth": False,
        # This is only the generated loopback gateway key, never an upstream credential.
        "experimental_bearer_token": gateway_key,
    }
    expected.update(model=model, model_provider=_PROVIDER)
    expected.setdefault("model_providers", {})[_PROVIDER] = fields
    document["model"] = model
    document["model_provider"] = _PROVIDER
    providers = document.get("model_providers", {})
    if isinstance(providers, InlineTable) or _PROVIDER in providers:
        if _PROVIDER not in providers:
            providers[_PROVIDER] = tomlkit.inline_table()
        for key, value in fields.items():
            providers[_PROVIDER][key] = value
        rendered = document.as_string()
    else:
        # A sibling header also works if the existing providers were declared with dotted
        # keys, where extending their TOMLKit parent can move root keys into the new table.
        table = tomlkit.table()
        table.update(fields)
        rendered = document.as_string()
        if rendered and not rendered.endswith("\n"):
            rendered += "\n"
        rendered += f"\n[model_providers.{_PROVIDER}]\n{table.as_string()}"
    candidate = _render(rendered, current.source)
    return candidate, _verified(candidate, expected)


def _unchanged(path: Path, before: str | None) -> None:
    if _read(path, _MAX_CONFIG, "Codex config.toml") != before:
        raise ClientConfigError("Codex config.toml changed during this operation; retry.")


def configure(
    home: Path, gateway_url: str, model: str, gateway_key: str, state_directory: Path
) -> dict:
    _target(gateway_url, model)
    if not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", gateway_key):
        raise ClientConfigError("The local gateway credential is invalid.")
    home = codex_home(home)
    path = home / "config.toml"
    journal_path = _journal_path(home, state_directory)
    with _write_lock(home):
        current = _inspect(path, journal_path)
        if current.conflict:
            raise ClientConfigError(current.conflict)
        candidate, current.document = _update_document(current, gateway_url, model, gateway_key)
        if len(candidate.encode()) > _MAX_CONFIG:
            raise ClientConfigError(
                "The configured Codex file would be too large to restore safely."
            )
        if candidate != current.source:
            previous = current.revision
            original = current.journal["original"] if previous is not None else current.source
            pristine = previous is None or (
                previous["pristine"] and previous["config"] == _digest(current.source)
            )
            revision = {
                "pristine": pristine,
                "config": _digest(candidate),
                "owned": _owned(current.document.unwrap()),
            }
            # Record both sides of an update before writing config. An interrupted write can
            # still prove ownership of whichever version reached config.toml.
            _unchanged(path, current.source)
            _write_journal(journal_path, original, [previous, revision] if previous else [revision])
            _unchanged(path, current.source)
            _atomic_write(path, candidate, "Codex config.toml")
            if previous is not None:
                _write_journal(journal_path, original, [revision])
            current.source = candidate
            current.revision = revision
        return current.metadata()


def _keep_comment(document: TOMLDocument, comment: str) -> None:
    if comment:
        document.add(tomlkit.comment(comment.removeprefix("#").lstrip()))


def _keep_table_comments(document: TOMLDocument, table: Table) -> None:
    _keep_comment(document, table.trivia.comment)
    for key, item in table.value.body:
        if isinstance(item, Comment):
            document.add(item)
        elif key is not None:
            _keep_comment(document, item.trivia.comment)


def _restore_document(current: _Inspection, original: TOMLDocument) -> str:
    document = current.document
    expected = document.unwrap()
    baseline_values = original.unwrap()
    providers = document["model_providers"]
    provider = providers[_PROVIDER]
    if isinstance(provider, Table):
        _keep_table_comments(document, provider)
    for key in _ROOT_KEYS:
        trivia = document.item(key).trivia
        if key in original:
            expected[key] = baseline_values[key]
            baseline = original.item(key)
            baseline.trivia.comment = trivia.comment
            baseline.trivia.comment_ws = trivia.comment_ws
            document[key] = baseline
        else:
            del expected[key]
            _keep_comment(document, trivia.comment)
            del document[key]
    del providers[_PROVIDER]
    del expected["model_providers"][_PROVIDER]
    if not providers and "model_providers" not in original:
        if isinstance(providers, Table):
            _keep_table_comments(document, providers)
        del document["model_providers"]
        del expected["model_providers"]
    candidate = _render(document.as_string(), current.source)
    _verified(candidate, expected)
    return candidate


def restore(home: Path, state_directory: Path) -> dict:
    home = codex_home(home)
    path = home / "config.toml"
    journal_path = _journal_path(home, state_directory)
    if _read_journal(journal_path) is None:
        return status(home, state_directory)
    with _write_lock(home):
        current = _inspect(path, journal_path)
        if current.conflict:
            raise ClientConfigError(current.conflict)
        if current.journal is None:
            return current.metadata()
        if current.revision is not None:
            original = current.journal["original"]
            baseline = _parse(original)
            if current.revision["pristine"] and current.revision["config"] == _digest(
                current.source
            ):
                candidate = original
            else:
                candidate = _restore_document(current, baseline)
                if original is None and not candidate.strip():
                    candidate = None
            _unchanged(path, current.source)
            if candidate is None:
                path.unlink()
            else:
                _atomic_write(path, candidate, "Codex config.toml")
            current.source = candidate
        journal_path.unlink()
        return {
            "configPath": str(path),
            "configExists": current.source is not None,
            "state": "notConfigured",
        }
