"""Reversible Codex feature/default settings for an explicitly enabled integration."""

import copy
import json
import mmap
import shutil
from pathlib import Path

import tomlkit

from . import clients
from .subagents import setting


def supports_mode_hint(command=None):
    """Read installed native binary capability, never launch or change Codex."""
    selected = command or shutil.which("codex")
    if not selected:
        return None
    path = Path(selected).resolve()
    candidates = {path}
    bases = [path.parent / "node_modules" / "@openai", path.parent.parent / "@openai"]
    if path.name == "codex.js" and path.parent.name == "bin":
        bases.extend([path.parent.parent.parent, path.parent.parent / "node_modules" / "@openai"])
    for base in bases:
        for package, triple in (
            ("codex-win32-x64", "x86_64-pc-windows-msvc"),
            ("codex-win32-arm64", "aarch64-pc-windows-msvc"),
        ):
            candidates.add(base / package / "vendor" / triple / "bin" / "codex.exe")
            candidates.add(
                base
                / "codex"
                / "node_modules"
                / "@openai"
                / package
                / "vendor"
                / triple
                / "bin"
                / "codex.exe"
            )
    inspected = False
    for candidate in candidates:
        try:
            with candidate.open("rb") as handle:
                if handle.read(2) != b"MZ":
                    continue
                inspected = True
                with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as binary:
                    if binary.find(b"multi_agent_mode_hint_text") >= 0:
                        return True
        except OSError, ValueError:
            continue
    return False if inspected else None


def _get(document, path):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return {"present": False}
        value = value[part]
    return {"present": True, "value": value}


def _put(document, path, state):
    parts = path.split(".")
    parent = document
    chain = []
    for part in parts[:-1]:
        if part not in parent:
            if not state["present"]:
                return
            parent[part] = tomlkit.table()
        if not hasattr(parent[part], "items"):
            if path.startswith("features.multi_agent_v2.") and isinstance(parent[part], bool):
                enabled = parent[part]
                parent[part] = tomlkit.table()
                parent[part]["enabled"] = enabled
            else:
                raise clients.ClientConfigError(f"Codex {part} is not a settings table.")
        chain.append((parent, part))
        parent = parent[part]
    if state["present"]:
        parent[parts[-1]] = state["value"]
    else:
        parent.pop(parts[-1], None)
        for ancestor, key in reversed(chain):
            if not ancestor[key]:
                del ancestor[key]


def desired_settings(config):
    fields = {}
    if setting(config, "webSearchSidecar", {}).get("enabled") is False:
        fields["web_search"] = "disabled"
    mode = setting(config, "multiAgentMode", "default")
    client = setting(config, "codexAgentSettings", {})
    enabled = client.get("enabled")
    if mode in {"v1", "v2"}:
        enabled = mode == "v2" and not setting(config, "keepNativeChatGptOnV1", False)
    if enabled is not None:
        fields["features.multi_agent_v2.enabled"] = enabled
    for key, path in {
        "maxConcurrentThreadsPerSession": "features.multi_agent_v2.max_concurrent_threads_per_session",
        "subagentDeveloperInstructions": "features.multi_agent_v2.subagent_developer_instructions",
        "multiAgentModeHintText": "features.multi_agent_v2.multi_agent_mode_hint_text",
        "agentsEnabled": "agents.enabled",
        "agentsMaxDepth": "agents.max_depth",
    }.items():
        if key in client:
            fields[path] = client[key]
    if setting(config, "syncCodexSubagentDefaults", False) and setting(config, "injectionModel"):
        fields["agents.default_subagent_model"] = setting(config, "injectionModel")
        if setting(config, "injectionEffort"):
            fields["agents.default_subagent_reasoning_effort"] = setting(config, "injectionEffort")
    return fields


def sync_settings(home, directory, config=None, *, restore=False):
    path = home / "config.toml"
    journal_path = clients._journal_path(home, directory).with_name("codex-agent-settings.json")
    desired = {} if restore else desired_settings(config)
    with clients._write_lock(home):
        source = clients._read(path, 1 << 20, "Codex config.toml")
        raw = clients._read(journal_path, 4 << 20, "Codex agent settings journal")
        record = json.loads(raw) if raw else {"original": {}, "applied": {}, "before": source}
        if record.get("pending") and source == record["pending"]["source"]:
            record = record["pending"]["previous"]
        record.pop("pending", None)
        if not desired and not raw:
            return False
        document = clients._parse(source)
        current = document.unwrap()
        # A boolean feature must be represented as its effective enabled value
        # before taking per-field ownership; retain the complete original text too.
        feature = current.get("features", {}).get("multi_agent_v2")
        if isinstance(feature, bool):
            current["features"]["multi_agent_v2"] = {"enabled": feature}
        if (
            desired.get("features.multi_agent_v2.enabled") is True
            and "features.multi_agent_v2.max_concurrent_threads_per_session" not in desired
            and "max_threads" in current.get("agents", {})
        ):
            desired["features.multi_agent_v2.max_concurrent_threads_per_session"] = current[
                "agents"
            ]["max_threads"]
        previous = copy.deepcopy(record)
        if record.get("after") is not None and source != record["after"]:
            record["externalEdit"] = True
        for key, applied in record["applied"].items():
            if _get(current, key) != applied:
                if key == "web_search":
                    # The search switch owns disabled while off. Retain a newly
                    # edited native mode for re-enable; while on, leave it intact.
                    record["original"][key] = _get(current, key)
                    continue
                raise clients.ClientConfigError(
                    f"Codex {key} changed after Lite applied it; agent settings were left untouched."
                )
        for key in desired:
            if key not in record["applied"]:
                # A released field may have been edited while another feature
                # kept this shared journal alive. Acquire its current value.
                record["original"][key] = _get(current, key)
        for key in record["applied"].keys() - desired.keys():
            _put(document, key, record["original"][key])
        for key, value in desired.items():
            _put(
                document,
                key,
                {"present": True, "value": value} if value is not None else {"present": False},
            )
        candidate = clients._render(document.as_string(), source)
        if not desired and source == record.get("after") and not record.get("externalEdit"):
            candidate = record["before"]
        if candidate != source:
            record["applied"] = {
                key: {"present": True, "value": value} if value is not None else {"present": False}
                for key, value in desired.items()
            }
            record["after"] = candidate
            record["pending"] = {"source": source, "previous": previous}
            clients._atomic_write(journal_path, json.dumps(record), "Codex agent settings journal")
            clients._unchanged(path, source)
            if candidate is None:
                path.unlink(missing_ok=True)
            else:
                clients._atomic_write(path, candidate, "Codex config.toml")
            record.pop("pending", None)
            clients._atomic_write(journal_path, json.dumps(record), "Codex agent settings journal")
        if not desired:
            journal_path.unlink(missing_ok=True)
        return candidate != source
