"""Reversible Codex feature/default settings for an explicitly enabled integration."""

import copy
import json

import tomlkit

from . import codex_config
from .files import ClientConfigError, atomic_write, read_text


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
                raise ClientConfigError(f"Codex {part} is not a settings table.")
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
    if (
        config.features.request.web_search_sidecar
        and config.features.request.web_search_sidecar.get("enabled") is False
    ):
        fields["web_search"] = "disabled"
    agents = config.features.agents
    for key, path in {
        "agentsEnabled": "agents.enabled",
        "agentsMaxDepth": "agents.max_depth",
        "maxConcurrentThreadsPerSession": "features.multi_agent_v2.max_concurrent_threads_per_session",
    }.items():
        if key in agents.codex_agent_settings:
            fields[path] = agents.codex_agent_settings[key]
    if agents.sync_codex_subagent_defaults and agents.injection_model:
        fields["agents.default_subagent_model"] = agents.injection_model
        if agents.injection_effort:
            fields["agents.default_subagent_reasoning_effort"] = agents.injection_effort
    return fields


def sync_settings(home, directory, config=None, *, restore=False):
    path = home / "config.toml"
    journal_path = codex_config.config_journal_path(home, directory).with_name(
        "codex-agent-settings.json"
    )
    desired = {} if restore else desired_settings(config)
    with codex_config.write_lock(home):
        source = read_text(path, 1 << 20, "Codex config.toml")
        raw = read_text(journal_path, 4 << 20, "Codex agent settings journal")
        record = json.loads(raw) if raw else {"original": {}, "applied": {}, "before": source}
        if record.get("pending") and source == record["pending"]["source"]:
            record = record["pending"]["previous"]
        record.pop("pending", None)
        if not desired and not raw:
            return False
        document = codex_config.parse_document(source)
        current = document.unwrap()
        # A boolean feature must be represented as its effective enabled value
        # before taking per-field ownership; retain the complete original text too.
        feature = current.get("features", {}).get("multi_agent_v2")
        if isinstance(feature, bool):
            current["features"]["multi_agent_v2"] = {"enabled": feature}
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
                raise ClientConfigError(
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
        candidate = codex_config.render_document(document.as_string(), source)
        if not desired and source == record.get("after") and not record.get("externalEdit"):
            candidate = record["before"]
        if candidate != source:
            record["applied"] = {
                key: {"present": True, "value": value} if value is not None else {"present": False}
                for key, value in desired.items()
            }
            record["after"] = candidate
            record["pending"] = {"source": source, "previous": previous}
            atomic_write(journal_path, json.dumps(record), "Codex agent settings journal")
            codex_config.ensure_unchanged(path, source)
            if candidate is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, candidate, "Codex config.toml")
            record.pop("pending", None)
            atomic_write(journal_path, json.dumps(record), "Codex agent settings journal")
        if not desired:
            journal_path.unlink(missing_ok=True)
        return candidate != source
