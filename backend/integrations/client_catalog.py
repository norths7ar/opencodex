"""Codex's routed model catalog and its reversible cache projection.

The wire fields follow codex/catalog/derive-entry.ts and ensureStrictCatalogFields.
Native cache rows remain native; only Lite-authored rows are replaced or removed.
"""

import copy
import json
import re
from pathlib import Path

from . import codex_config
from .files import ClientConfigError, atomic_write, read_text

_TEMPLATE_DATA = json.loads(
    Path(__file__).with_name("codex_catalog_template.json").read_text(encoding="utf-8")
)
_EFFORT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
_NATIVE_ONLY = (
    "model_messages",
    "multi_agent_version",
    "multi_agent_reasoning_effort",
    "use_responses_lite",
    "supports_reasoning_effort_updates",
    "supports_websockets",
    "supports_experimental_context",
    "additional_speed_tiers",
    "service_tier",
    "service_tiers",
    "default_service_tier",
    "available_access_programs",
    "available_in_plans",
    "minimal_client_version",
    "availability_nux",
    "opencodex_native_display_name",
    "opencodex_spawn_priority",
    "opencodex_native_source_priority",
    "context_window",
    "max_context_window",
    "auto_compact_token_limit",
    "comp_hash",
)


def _native_template(home: Path) -> dict | None:
    source = read_text(home / "models_cache.json", 32 << 20, "Codex model cache")
    catalog = _cache(source)
    retained = catalog.get("opencodex_native_inheritance_template")
    rows = ([retained] if isinstance(retained, dict) else []) + catalog["models"]
    return next(
        (
            row
            for row in rows
            if row.get("slug") in _TEMPLATE_DATA["supportedNativeSlugs"]
            and isinstance(row.get("base_instructions"), str)
            and not row.get("opencodex_lite")
            and not str(row.get("description", "")).startswith("Routed via opencodex → ")
        ),
        None,
    )


def _reasoning(entry: dict, row: dict) -> None:
    declared = row.get("reasoningEfforts")
    efforts = (
        [e for e in _EFFORT_ORDER if e in declared]
        if declared is not None
        else [e["effort"] for e in _TEMPLATE_DATA["reasoningLevels"]]
    )
    independent = (
        row.get("supportsThinkingToggle") is not None
        or row.get("supportsReasoningEffort") is False
        or row.get("supportsReasoning") is False
    )
    default = row.get("defaultReasoningEffort")
    if row.get("supportsReasoning") is False:
        efforts = []
    elif row.get("supportsReasoningEffort") is False:
        efforts = ["none", "high"] if row.get("supportsThinkingToggle") is True else []
    elif row.get("supportsThinkingToggle") is True:
        strengths = [e for e in efforts if e != "none"] if declared is not None else []
        efforts = ["none", *(strengths or ["high"])]
    elif row.get("supportsThinkingToggle") is False:
        efforts = [e for e in efforts if e != "none"]
    elif row.get("provider") != "opencode-go" and any(
        e not in {"none", "minimal"} for e in efforts
    ):
        if not row.get("suppressSyntheticMax") and "max" not in efforts:
            efforts.append("max")
        if "ultra" not in efforts:
            efforts.append("ultra")
    efforts = [e for e in _EFFORT_ORDER if e in efforts]
    descriptions = {level["effort"]: level for level in _TEMPLATE_DATA["reasoningLevels"]}
    descriptions.update(
        {level["effort"]: level for level in entry.get("supported_reasoning_levels", [])}
    )
    entry["supported_reasoning_levels"] = [
        copy.deepcopy(descriptions.get(e, {"effort": e, "description": f"{e} reasoning"}))
        for e in efforts
    ]
    if row.get("supportsThinkingToggle") is True and row.get("supportsReasoning") is not False:
        toggle_only = row.get("supportsReasoningEffort") is False or not any(
            e != "none" for e in (declared or [])
        )
        if row.get("defaultThinkingEnabled") is False:
            default = "none"
        elif toggle_only:
            default = "high" if row.get("defaultThinkingEnabled") is True else None
        elif row.get("defaultThinkingEnabled") is True and default == "none":
            default = None
    if not efforts or (
        independent and row.get("supportsThinkingToggle") is True and default is None
    ):
        entry.pop("default_reasoning_level", None)
    else:
        if default not in efforts:
            default = (
                next(
                    (
                        e
                        for e in reversed(efforts)
                        if _EFFORT_ORDER.index(e) <= _EFFORT_ORDER.index("max")
                    ),
                    efforts[0],
                )
                if row.get("suppressSyntheticMax") and default == "max"
                else "medium"
                if "medium" in efforts
                else "high"
                if "high" in efforts
                else next((e for e in efforts if e not in {"none", "minimal"}), efforts[0])
            )
        entry["default_reasoning_level"] = default
    if row.get("supportsThinkingToggle") is True and row.get("supportsReasoningEffort") is False:
        for level in entry["supported_reasoning_levels"]:
            level["description"] = "Thinking off" if level["effort"] == "none" else "Thinking on"


def catalog_path(home: Path, directory: Path) -> Path:
    return codex_config.config_journal_path(home, directory).with_name("codex-models.json")


def catalog_entries(
    rows: list[dict], template: dict | None = None, *, source_entries: list[dict] | None = None
) -> list[dict]:
    entries = []
    seen = {}
    source_models = {entry["slug"]: entry for entry in source_entries or []}
    for row in rows:
        if row.get("disabled") or row.get("selected") is False:
            continue
        slug = row.get("namespaced") or row.get("alias") or f"{row['provider']}/{row['id']}"
        identity = (row["provider"], row["id"])
        if slug in seen and seen[slug] != identity:
            raise ClientConfigError(
                f"Two models map to the same Codex selector: {slug}. Resolve the model ID collision before synchronizing."
            )
        if slug in seen:
            continue
        seen[slug] = identity
        context = row.get("contextWindow") or row.get("contextCap") or 128000
        entry = copy.deepcopy(template or _TEMPLATE_DATA["entry"])
        modalities = [
            m
            for m in (row.get("inputModalities") or entry.get("input_modalities", ["text"]))
            if m in {"text", "image", "audio"}
        ] or ["text"]
        for field in _NATIVE_ONLY:
            entry.pop(field, None)
        instructions = entry.get("base_instructions", "You are a helpful coding assistant.")
        entry["base_instructions"] = re.sub(
            r"You are Codex, (?:a coding agent|an agent) based on GPT-[0-9]+(?:\.[0-9]+)*\.",
            "You are a coding agent. Do not claim to be GPT-5 or to be made by OpenAI.",
            instructions,
        )
        # A user-authored instruction belongs to its actual model, not an alias
        # or whichever native row happened to be first in the cache.
        source_model = source_models.get(f"{row['provider']}/{row['id']}")
        if source_model is None:
            source_model = source_models.get(row["id"])
        if source_model is not None:
            for field in ("base_instructions", "model_messages"):
                if field in source_model:
                    entry[field] = copy.deepcopy(source_model[field])
        compact_limits = [int(context * 0.9), context]
        compact_limits.extend(
            row[field]
            for field in ("maxInputTokens", "autoCompactTokenLimit")
            if isinstance(row.get(field), int)
            and row[field] > 0
            and (
                field != "autoCompactTokenLimit"
                or row.get("contextWindow")
                or row.get("contextCap")
            )
        )
        entry.update(
            {
                "slug": slug,
                "display_name": row.get("displayName") or slug,
                "description": f"Routed via opencodex → {row['provider']}",
                "opencodex_lite": True,
                "shell_type": "unified_exec"
                if entry.get("shell_type") in {None, "default", "local", "shell_command"}
                else entry["shell_type"],
                "visibility": "list",
                "supported_in_api": True,
                "priority": len(entries),
                "supports_reasoning_summaries": row.get("supportsReasoningSummaries", False),
                "default_reasoning_summary": entry.get("default_reasoning_summary", "none"),
                "support_verbosity": row.get(
                    "supportsVerbosity", entry.get("support_verbosity", True)
                ),
                "supports_parallel_tool_calls": row.get("parallelToolCalls", False),
                "supports_search_tool": True,
                "web_search_tool_type": "text_and_image",
                "input_modalities": modalities,
                "context_window": context,
                "max_context_window": context,
                "effective_context_window_percent": entry.get(
                    "effective_context_window_percent", 95
                ),
                "auto_compact_token_limit": min(compact_limits),
                "comp_hash": None,
            }
        )
        if "upgrade" in entry:
            entry["upgrade"] = None
        for field, default in {
            "node_repl_disabled": False,
            "node_repl_auto_review_required": False,
            "include_plugin_usage_instructions": False,
            "include_apps_usage_instructions": True,
            "apply_patch_tool_type": "freeform",
            "truncation_policy": {"mode": "tokens", "limit": 10000},
            "supports_image_detail_original": False,
            "experimental_supported_tools": [],
        }.items():
            entry.setdefault(field, default)
        if entry["support_verbosity"]:
            entry.setdefault("default_verbosity", "low")
        else:
            entry.pop("default_verbosity", None)
        _reasoning(entry, row)
        if row.get("codexToolMode") != "shell":
            entry["tool_mode"] = "code_mode_only"
        else:
            entry.pop("tool_mode", None)
        if row.get("supportsServiceTier") is True:
            entry["default_service_tier"] = None
            entry["service_tiers"] = [
                {
                    "id": "priority",
                    "name": "Fast",
                    "description": row.get("fastTierDescription") or "1.5x speed, increased usage",
                }
            ]
            entry["additional_speed_tiers"] = ["fast"]
            if row.get("supportsFlexTier") is True:
                entry["service_tiers"].append(
                    {
                        "id": "flex",
                        "name": "Flex",
                        "description": "Capacity-dependent processing; availability and latency may vary",
                    }
                )
                entry["additional_speed_tiers"].append("flex")
        entry["opencodex_capability_provenance"] = {
            "provider": row["provider"],
            "model_id": row["id"],
        }
        for source, target in (
            ("contextWindow", "context_window"),
            ("inputModalities", "input_modalities"),
            ("capabilities", "capabilities"),
        ):
            if row.get(source):
                entry["opencodex_capability_provenance"][target] = row[source]
        entries.append(entry)
    return entries


def _cache(source: str | None) -> dict:
    try:
        value = json.loads(source) if source else {"models": []}
        if not isinstance(value, dict) or not isinstance(value.get("models"), list):
            raise TypeError()
        if any(not isinstance(row, dict) for row in value["models"]):
            raise TypeError()
        return value
    except ValueError, TypeError:
        raise ClientConfigError("Codex models_cache.json is not a valid model cache.") from None


def _configured_catalog(home: Path, directory: Path) -> dict | None:
    current = codex_config.inspect_configuration(
        home / "config.toml", codex_config.config_journal_path(home, directory)
    )
    if current.conflict:
        raise ClientConfigError(current.conflict)
    original = (
        codex_config.parse_document(current.journal["original"])
        if current.revision is not None and current.journal is not None
        else current.document
    )
    configured = original.get("model_catalog_json")
    if configured is None:
        return None
    if not isinstance(configured, str) or not configured.strip():
        raise ClientConfigError("Codex model_catalog_json must name a catalog file.")
    source_path = Path(configured).expanduser()
    if not source_path.is_absolute():
        source_path = home / source_path
    if source_path.resolve() == catalog_path(home, directory).resolve():
        raise ClientConfigError(
            "The original Codex catalog points to Lite's output; restore the source catalog first."
        )
    source = read_text(source_path, 32 << 20, "Configured Codex model catalog")
    if source is None:
        raise ClientConfigError(f"Configured Codex model catalog is missing: {source_path}")
    try:
        value = json.loads(source.removeprefix("\ufeff"))
        if not isinstance(value, dict) or not isinstance(value.get("models"), list):
            raise TypeError()
        seen = set()
        for row in value["models"]:
            if not isinstance(row, dict):
                raise TypeError()
            slug = row.get("slug")
            if not isinstance(slug, str) or not slug.strip() or slug in seen:
                raise ValueError()
            seen.add(slug)
            if "base_instructions" in row and not isinstance(row["base_instructions"], str):
                raise ValueError()
            messages = row.get("model_messages")
            if messages is not None and (
                not isinstance(messages, dict)
                or (
                    "instructions_template" in messages
                    and not isinstance(messages["instructions_template"], str)
                )
            ):
                raise ValueError()
        return value
    except ValueError, TypeError, RecursionError:
        raise ClientConfigError(
            f"Configured Codex model catalog is invalid: {source_path}"
        ) from None


def prepare_catalog(
    home: Path, directory: Path, rows: list[dict], *, config=None
) -> tuple[dict, list[dict]]:
    """Project a read-only user catalog plus Lite routes before publishing any client changes."""
    source = _configured_catalog(home, directory)
    native_rows = source["models"] if source is not None else []
    routed = catalog_entries(rows, _native_template(home), source_entries=native_rows)
    retained = copy.deepcopy(native_rows)
    selectors = {entry["slug"] for entry in retained}
    if selectors.intersection(entry["slug"] for entry in routed):
        raise ClientConfigError(
            "A configured Codex catalog model owns a gateway selector. Change the gateway alias before applying."
        )
    projected = copy.deepcopy(source) if source is not None else {}
    projected["models"] = retained + routed
    if config is not None:
        featured = config.features.agents.subagent_models
        picker = config.features.agents.model_picker_order
        for index, entry in enumerate(projected["models"]):
            slug = entry["slug"]
            if featured:
                entry["priority"] = featured.index(slug) if slug in featured else 100 + index
            if picker:
                entry["opencodex_spawn_priority"] = entry.get("priority", index)
                entry["priority"] = 1000 + (
                    picker.index(slug) if slug in picker else len(picker) + index
                )
        routed = [entry for entry in projected["models"] if entry.get("opencodex_lite")]
    return projected, routed


def write_catalog(
    home: Path, directory: Path, rows: list[dict], *, prepared: dict | None = None
) -> tuple[Path, bool]:
    path = catalog_path(home, directory)
    projected = prepared if prepared is not None else prepare_catalog(home, directory, rows)[0]
    candidate = json.dumps(projected, ensure_ascii=False, indent=2)
    source = read_text(path, 16 << 20, "Codex model catalog")
    if source == candidate:
        return path, False
    atomic_write(path, candidate, "Codex model catalog")
    return path, True


def _retained(value: dict, record: dict | None) -> list[dict]:
    known = {}
    for source in (record or {}).get("applied", ""), (record or {}).get("previous", ""):
        if source:
            for row in _cache(source)["models"]:
                if row.get("opencodex_lite") is True:
                    known.setdefault(row.get("slug"), []).append(row)
    retained = []
    for row in value["models"]:
        if row.get("opencodex_lite") is not True:
            retained.append(row)
        elif row not in known.get(row.get("slug"), []):
            raise ClientConfigError(
                "A Lite-managed Codex cache row was edited outside the gateway; restore was not applied."
            )
    return retained


def sync_cache(
    home: Path, directory: Path, rows: list[dict], *, prepared: list[dict] | None = None
) -> bool:
    path = home / "models_cache.json"
    journal_path = catalog_path(home, directory).with_name("codex-cache-recovery.json")
    with codex_config.write_lock(home):
        source = read_text(path, 32 << 20, "Codex model cache")
        value = _cache(source)
        journal_source = read_text(journal_path, 64 << 20, "Codex cache recovery journal")
        record = json.loads(journal_source) if journal_source else {"original": source}
        entries = prepared if prepared is not None else prepare_catalog(home, directory, rows)[1]
        retained = _retained(value, record)
        foreign = {row.get("slug") for row in retained}
        if foreign.intersection(row["slug"] for row in entries):
            raise ClientConfigError("A foreign Codex cache row owns a selected model slug.")
        value["models"] = retained + entries
        candidate = json.dumps(value, ensure_ascii=False, indent=2)
        if source == candidate:
            return False
        record["previous"] = source
        record["applied"] = candidate
        atomic_write(journal_path, json.dumps(record), "Codex cache recovery journal")
        if read_text(path, 32 << 20, "Codex model cache") != source:
            raise ClientConfigError("Codex model cache changed during synchronization; retry.")
        atomic_write(path, candidate, "Codex model cache")
        return True


def restore_cache(home: Path, directory: Path) -> bool:
    path = home / "models_cache.json"
    journal_path = catalog_path(home, directory).with_name("codex-cache-recovery.json")
    with codex_config.write_lock(home):
        journal_source = read_text(journal_path, 64 << 20, "Codex cache recovery journal")
        if journal_source is None:
            return False
        record = json.loads(journal_source)
        source = read_text(path, 32 << 20, "Codex model cache")
        if source == record["applied"]:
            candidate = record["original"]
        elif source is None:
            candidate = None
        else:
            value = _cache(source)
            retained = _retained(value, record)
            if retained == value["models"]:
                candidate = source
            else:
                value["models"] = retained
                candidate = json.dumps(value, ensure_ascii=False, indent=2)
        if read_text(path, 32 << 20, "Codex model cache") != source:
            raise ClientConfigError("Codex model cache changed during restoration; retry.")
        if candidate != source:
            if candidate is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, candidate, "Codex model cache")
        journal_path.unlink()
        return candidate != source
