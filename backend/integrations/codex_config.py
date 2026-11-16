"""Manage only Lite's Codex routing fields; never touch Codex login or history."""

import hashlib
import json
import os
import re
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import Comment, Table
from tomlkit.toml_document import TOMLDocument

from backend.platform.files import instance_lock

from .files import ClientConfigError, assert_regular_file, atomic_write, read_text

_PROVIDER = "opencodex-lite"
_COMPACTION_PROVIDER = "opencodex"
_LOCK_FILE = ".opencodex-lite.lock"
_ROOT_KEYS = ("model", "model_provider", "model_catalog_json")
_NATIVE_ROOT_KEYS = (*_ROOT_KEYS, "openai_base_url")
_MAX_CONFIG = 1 << 20
_MAX_JOURNAL = 6 * _MAX_CONFIG + 2048


def codex_home(value: str | Path | None = None) -> Path:
    configured = value or os.environ.get("CODEX_HOME")
    return (Path(configured).expanduser() if configured else Path.home() / ".codex").resolve()


def config_journal_path(home: Path, state_directory: Path) -> Path:
    home_id = _digest(os.path.normcase(str(home)))
    return state_directory / "clients" / home_id / "codex-config.json"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def parse_document(source: str | None) -> TOMLDocument:
    try:
        return tomlkit.parse((source or "").removeprefix("\ufeff"))
    except TOMLKitError, ValueError, RecursionError:
        raise ClientConfigError("Codex config.toml is not valid TOML.") from None


def render_document(rendered: str, source: str | None) -> str:
    if source:
        crlf = source.count("\r\n")
        if crlf and crlf == source.count("\n"):
            rendered = re.sub(r"(?<!\r)\n", "\r\n", rendered)
        if source.startswith("\ufeff"):
            rendered = "\ufeff" + rendered
    return rendered


@contextmanager
def write_lock(home: Path):
    try:
        with suppress(FileNotFoundError):
            assert_regular_file(home / _LOCK_FILE, "Codex configuration lock")
        with instance_lock(home, _LOCK_FILE):
            yield
    except OSError:
        raise ClientConfigError(
            "Could not update Codex configuration files; check file access and retry."
        ) from None


def _read_journal(path: Path) -> dict | None:
    raw = read_text(path, _MAX_JOURNAL, "Codex recovery journal")
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
    atomic_write(path, json.dumps(record, ensure_ascii=False), "Codex recovery journal")


def _owned(
    values: dict, *, legacy: bool = False, native: bool = False, compaction: bool = False
) -> str:
    if native:
        keys = _NATIVE_ROOT_KEYS if not legacy else ("model", "model_provider", "openai_base_url")
        fields = [values.get(key) for key in keys]
        if compaction:
            fields.append(values.get("model_providers", {}).get(_COMPACTION_PROVIDER))
        return _digest(_canonical(fields))
    provider_map = values.get("model_providers")
    provider = provider_map.get(_PROVIDER) if isinstance(provider_map, dict) else None
    fields = [values.get(key) for key in (_ROOT_KEYS[:2] if legacy else _ROOT_KEYS)] + [provider]
    return _digest(_canonical(fields))


def _canonical(value: dict | list) -> str:
    return json.dumps(value, sort_keys=True, default=lambda entry: entry.isoformat())


def _verified(candidate: str, expected: dict) -> TOMLDocument:
    parsed = parse_document(candidate)
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
        if self.revision.get("inactive"):
            return {
                **result,
                "state": "notConfigured",
                "retainedProviderTable": True,
                "warning": "Native defaults restored. The opencodex provider definition remains so existing opencodex conversations can open; those conversations still need the gateway to send requests.",
            }
        values = self.document.unwrap()
        return {
            **result,
            "state": "configured",
            "baseUrl": values["openai_base_url"]
            if self.revision.get("nativeRouting")
            else values["model_providers"][_PROVIDER]["base_url"],
            "model": values["model"],
            "clientCompaction": self.revision.get("clientCompaction", False),
            "profileOverride": values.get("profile")
            if isinstance(values.get("profiles", {}).get(values.get("profile")), dict)
            and values["profiles"][values["profile"]].get("model_provider")
            else None,
        }


def inspect_configuration(path: Path, journal_path: Path) -> _Inspection:
    source = read_text(path, _MAX_CONFIG, "Codex config.toml")
    document = parse_document(source)
    journal = _read_journal(journal_path)
    result = _Inspection(path, source, document, journal)
    values = document.unwrap()
    provider_map = values.get("model_providers", {})
    if not isinstance(provider_map, dict) or any(
        key in values and not isinstance(values[key], str) for key in _NATIVE_ROOT_KEYS
    ):
        result.conflict = "Codex model or provider settings are not in a supported format."
    elif journal is not None:
        result.revision = next(
            (
                rev
                for rev in reversed(journal["revisions"])
                if rev["owned"]
                == _owned(
                    values,
                    legacy=not rev.get("catalogOwned", False),
                    native=rev.get("nativeRouting", False),
                    compaction=rev.get("compactionTableOwned", rev.get("clientCompaction", False)),
                )
            ),
            None,
        )
        baseline = parse_document(journal["original"]).unwrap()
        if result.revision is None and not any(
            _owned(
                values,
                legacy=not rev.get("catalogOwned", False),
                native=rev.get("nativeRouting", False),
                compaction=rev.get("compactionTableOwned", rev.get("clientCompaction", False)),
            )
            == _owned(
                baseline,
                legacy=not rev.get("catalogOwned", False),
                native=rev.get("nativeRouting", False),
                compaction=rev.get("compactionTableOwned", rev.get("clientCompaction", False)),
            )
            for rev in journal["revisions"]
        ):
            result.conflict = "Codex routing settings changed after Lite configured them."
    elif _PROVIDER in provider_map or values.get("model_provider") == _PROVIDER:
        result.conflict = "The Lite provider already exists without a matching recovery journal."
    else:
        active = values.get("model_provider", "openai")
        table = provider_map.get(active)
        # An authless Desktop placeholder has no URL; a real foreign provider owns its routing.
        if active != "openai" and (not isinstance(table, dict) or table.get("base_url")):
            result.conflict = "An external provider already controls Codex routing."
        elif values.get("openai_base_url"):
            result.conflict = "A user-owned openai_base_url already controls Codex routing."
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
        return inspect_configuration(path, config_journal_path(home, state_directory)).metadata()
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


def _reference_owned(document):
    return _canonical(
        [document.get(key) for key in ("model_provider", "model_catalog_json", "openai_base_url")]
        + [document.get("model_providers", {}).get(_COMPACTION_PROVIDER)]
    )


def reference_config(
    home,
    directory,
    gateway_url=None,
    catalog_path=None,
    *,
    client_compaction=False,
    restore=False,
    validate=False,
):
    """Journal the original fallback/reference artifact without touching named user profiles."""
    path = home / "opencodex.config.toml"
    journal_path = config_journal_path(home, directory).with_name("codex-reference.json")
    source = read_text(path, _MAX_CONFIG, "Codex reference config")
    raw = read_text(journal_path, _MAX_JOURNAL, "Codex reference journal")
    record = json.loads(raw) if raw else None
    document = parse_document(source)
    if (
        record
        and source != record.get("original")
        and _reference_owned(document) not in record.get("owned", [])
    ):
        raise ClientConfigError(
            "Codex reference routing was edited; its recovery journal was preserved."
        )
    original = record["original"] if record else source
    if restore:
        if record is None:
            return
        if source in record.get("applied", []):
            candidate = original
        else:
            baseline = parse_document(original)
            for key in ("model_provider", "model_catalog_json", "openai_base_url"):
                if key in baseline:
                    document[key] = baseline[key]
                else:
                    document.pop(key, None)
            providers = document.get("model_providers", {})
            baseline_provider = baseline.get("model_providers", {}).get(_COMPACTION_PROVIDER)
            if baseline_provider is not None:
                if "model_providers" not in document:
                    document["model_providers"] = tomlkit.table()
                    providers = document["model_providers"]
                providers[_COMPACTION_PROVIDER] = baseline_provider
            elif _COMPACTION_PROVIDER in providers:
                del providers[_COMPACTION_PROVIDER]
            if not providers and "model_providers" not in baseline:
                document.pop("model_providers", None)
            candidate = render_document(document.as_string(), source)
        if candidate is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, candidate, "Codex reference config")
        journal_path.unlink()
        return
    if source is None:
        document.add(
            tomlkit.comment(
                "OpenCodex routing reference: merge these settings into config.toml if automatic injection is unavailable."
            )
        )
    for key in ("model_provider", "model_catalog_json", "openai_base_url"):
        document.pop(key, None)
    document["model_catalog_json"] = str(catalog_path)
    if client_compaction:
        document["model_provider"] = _COMPACTION_PROVIDER
        if "model_providers" not in document:
            document["model_providers"] = tomlkit.table()
        document["model_providers"][_COMPACTION_PROVIDER] = {
            "name": "OpenCodex Lite",
            "base_url": gateway_url,
            "wire_api": "responses",
            "requires_openai_auth": True,
        }
    else:
        document["openai_base_url"] = gateway_url
        providers = document.get("model_providers", {})
        if _COMPACTION_PROVIDER in providers:
            del providers[_COMPACTION_PROVIDER]
        if not providers:
            document.pop("model_providers", None)
    candidate = render_document(document.as_string(), source)
    if validate or candidate == source:
        return str(path)
    ensure_unchanged(path, source)
    next_record = {
        "original": original,
        "applied": [source, candidate],
        "owned": [_reference_owned(parse_document(source)), _reference_owned(document)],
    }
    atomic_write(journal_path, json.dumps(next_record), "Codex reference journal")
    atomic_write(path, candidate, "Codex reference config")
    return str(path)


def _update_document(
    current: _Inspection,
    gateway_url: str,
    model: str,
    catalog_path: Path | None = None,
    client_compaction: bool = False,
) -> tuple[str, TOMLDocument]:
    document = current.document
    expected = document.unwrap()
    # Original OpenCodex Design B: retain Codex's built-in provider identity so
    # existing openai threads keep their routing/history without relabeling.
    if current.revision and not current.revision.get("nativeRouting"):
        original = parse_document(current.journal["original"]).unwrap()
        if original.get("openai_base_url"):
            raise ClientConfigError(
                "A user-owned openai_base_url must be resolved before migrating legacy routing."
            )
        providers = document.get("model_providers", {})
        if _PROVIDER in providers:
            del providers[_PROVIDER]
            del expected["model_providers"][_PROVIDER]
            if not providers and "model_providers" not in original:
                del document["model_providers"]
                del expected["model_providers"]
    expected.update(model=model, openai_base_url=gateway_url)
    expected.pop("model_provider", None)
    document["model"] = model
    document.pop("model_provider", None)
    document["openai_base_url"] = gateway_url
    previous_table = (current.revision or {}).get(
        "compactionTableOwned", (current.revision or {}).get("clientCompaction", False)
    )
    if client_compaction or previous_table:
        providers = document.get("model_providers", {})
        if _COMPACTION_PROVIDER in providers and not previous_table:
            raise ClientConfigError(
                "The opencodex provider table is user-owned; client-side compaction was not applied."
            )
        if "model_providers" not in document:
            document["model_providers"] = tomlkit.table()
        provider = {
            "name": "OpenCodex Lite",
            "base_url": gateway_url,
            "wire_api": "responses",
            "requires_openai_auth": True,
        }
        document["model_providers"][_COMPACTION_PROVIDER] = provider
        expected.setdefault("model_providers", {})[_COMPACTION_PROVIDER] = provider
        if client_compaction:
            document["model_provider"] = _COMPACTION_PROVIDER
            expected["model_provider"] = _COMPACTION_PROVIDER
    if catalog_path is not None:
        expected["model_catalog_json"] = str(catalog_path)
        document["model_catalog_json"] = str(catalog_path)
    rendered = document.as_string()
    candidate = render_document(rendered, current.source)
    return candidate, _verified(candidate, expected)


def ensure_unchanged(path: Path, before: str | None) -> None:
    if read_text(path, _MAX_CONFIG, "Codex config.toml") != before:
        raise ClientConfigError("Codex config.toml changed during this operation; retry.")


def validate_configuration(
    home: Path,
    gateway_url: str,
    model: str,
    state_directory: Path,
    *,
    catalog_path: Path | None = None,
    client_compaction: bool = False,
) -> None:
    """Preflight the same transformation before publishing the model catalog."""
    _target(gateway_url, model)
    home = codex_home(home)
    current = inspect_configuration(
        home / "config.toml", config_journal_path(home, state_directory)
    )
    if current.conflict:
        raise ClientConfigError(current.conflict)
    _update_document(current, gateway_url, model, catalog_path, client_compaction)


def configure(
    home: Path,
    gateway_url: str,
    model: str,
    state_directory: Path,
    *,
    catalog_path: Path | None = None,
    client_compaction: bool = False,
) -> dict:
    _target(gateway_url, model)
    home = codex_home(home)
    path = home / "config.toml"
    journal_path = config_journal_path(home, state_directory)
    with write_lock(home):
        current = inspect_configuration(path, journal_path)
        if current.conflict:
            raise ClientConfigError(current.conflict)
        candidate, current.document = _update_document(
            current, gateway_url, model, catalog_path, client_compaction
        )
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
                "owned": _owned(
                    current.document.unwrap(),
                    legacy=catalog_path is None and not (previous or {}).get("catalogOwned", False),
                    native=True,
                    compaction=client_compaction
                    or (previous or {}).get("compactionTableOwned", False),
                ),
                "nativeRouting": True,
                "clientCompaction": client_compaction,
                "compactionTableOwned": client_compaction
                or (previous or {}).get("compactionTableOwned", False),
                "catalogOwned": catalog_path is not None
                or (previous or {}).get("catalogOwned", False),
            }
            # Record both sides of an update before writing config. An interrupted write can
            # still prove ownership of whichever version reached config.toml.
            ensure_unchanged(path, current.source)
            _write_journal(journal_path, original, [previous, revision] if previous else [revision])
            ensure_unchanged(path, current.source)
            atomic_write(path, candidate, "Codex config.toml")
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
    native = current.revision.get("nativeRouting", False)
    root_keys = _NATIVE_ROOT_KEYS if native else _ROOT_KEYS
    if not current.revision.get("catalogOwned"):
        root_keys = tuple(key for key in root_keys if key != "model_catalog_json")
    for key in root_keys:
        trivia = document.item(key).trivia if key in document else None
        if key in original:
            expected[key] = baseline_values[key]
            baseline = original.item(key)
            if trivia is not None:
                baseline.trivia.comment = trivia.comment
                baseline.trivia.comment_ws = trivia.comment_ws
            document[key] = baseline
        elif key in document:
            del expected[key]
            _keep_comment(document, trivia.comment)
            del document[key]
    if not native:
        providers = document["model_providers"]
        provider = providers[_PROVIDER]
        if isinstance(provider, Table):
            _keep_table_comments(document, provider)
        del providers[_PROVIDER]
        del expected["model_providers"][_PROVIDER]
        if not providers and "model_providers" not in original:
            if isinstance(providers, Table):
                _keep_table_comments(document, providers)
            del document["model_providers"]
            del expected["model_providers"]
    if current.revision.get(
        "compactionTableOwned", current.revision.get("clientCompaction", False)
    ):
        del document["model_providers"][_COMPACTION_PROVIDER]
        del expected["model_providers"][_COMPACTION_PROVIDER]
        if not document["model_providers"] and "model_providers" not in original:
            del document["model_providers"]
            del expected["model_providers"]
    candidate = render_document(document.as_string(), current.source)
    _verified(candidate, expected)
    return candidate


def restore(home: Path, state_directory: Path) -> dict:
    home = codex_home(home)
    path = home / "config.toml"
    journal_path = config_journal_path(home, state_directory)
    if _read_journal(journal_path) is None:
        return status(home, state_directory)
    with write_lock(home):
        current = inspect_configuration(path, journal_path)
        if current.conflict:
            raise ClientConfigError(current.conflict)
        if current.journal is None:
            return current.metadata()
        if current.revision and current.revision.get("inactive"):
            return current.metadata()
        retain_table = bool(
            current.revision
            and current.revision.get(
                "compactionTableOwned", current.revision.get("clientCompaction", False)
            )
        )
        retained_definition = (
            current.document.unwrap().get("model_providers", {}).get(_COMPACTION_PROVIDER)
            if retain_table
            else None
        )
        if current.revision is not None:
            original = current.journal["original"]
            baseline = parse_document(original)
            if current.revision["pristine"] and current.revision["config"] == _digest(
                current.source
            ):
                candidate = original
            else:
                candidate = _restore_document(current, baseline)
                if original is None and not candidate.strip():
                    candidate = None
            if retain_table:
                restored = parse_document(candidate)
                if "model_providers" not in restored:
                    restored["model_providers"] = tomlkit.table()
                restored["model_providers"][_COMPACTION_PROVIDER] = retained_definition
                candidate = render_document(restored.as_string(), original or current.source)
            ensure_unchanged(path, current.source)
            if retain_table:
                inactive = {
                    **current.revision,
                    "inactive": True,
                    "clientCompaction": False,
                    "pristine": False,
                    "config": _digest(candidate),
                    "owned": _owned(
                        parse_document(candidate).unwrap(),
                        native=True,
                        compaction=True,
                        legacy=not current.revision.get("catalogOwned", False),
                    ),
                }
                _write_journal(journal_path, original, [current.revision, inactive])
                ensure_unchanged(path, current.source)
            if candidate is None:
                path.unlink()
            else:
                atomic_write(path, candidate, "Codex config.toml")
            current.source = candidate
            if retain_table:
                _write_journal(journal_path, original, [inactive])
                current.revision = inactive
                return current.metadata()
        journal_path.unlink()
        return {
            "configPath": str(path),
            "configExists": current.source is not None,
            "state": "notConfigured",
        }


def read_document(path: Path) -> TOMLDocument:
    """Read a Codex TOML document without acquiring write ownership."""
    return parse_document(read_text(path, _MAX_CONFIG, "Codex configuration"))
