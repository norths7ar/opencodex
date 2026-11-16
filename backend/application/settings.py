"""Transactional management of model and request policy settings.

Methods return configuration snapshots, never credential records. Validation runs
before commit and optional revision checks reject stale edits.
"""

import math
from typing import Any

from backend.catalog.identity import ModelIdentity, legacy_disabled_candidates
from backend.configuration.models import AgentSettings, RequestSettings
from backend.configuration.store import ConfigurationStore
from backend.policies.compaction import CompactionRouting
from backend.policies.fallback import FallbackSettings, split_selector
from backend.policies.spend_policy import SpendPolicy
from backend.policies.workflow import validate_settings

_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
_CAP = 350_000


def _strings(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f"{name} must be an array of non-empty strings.")
    return [v.strip() for v in value]


class SettingsService:
    def __init__(self, store: ConfigurationStore):
        self.store = store

    def read(self) -> dict:
        state = self.store.read()
        config = state.configuration
        return {
            "revision": state.revision,
            "models": config.models.model_dump(),
            "request": config.features.request.model_dump(),
            "agents": config.features.agents.model_dump(),
        }

    def _update(self, change, expected_revision=None) -> dict:
        def apply(state):
            config = state.configuration
            change(config)
            config.models = config.models
            config.features = config.features
            for channel in config.channels:
                channel.models.metadata = channel.models.metadata
                channel.models = channel.models
            config.channels = config.channels

        state, _ = self.store.update(apply, expected_revision=expected_revision)
        config = state.configuration
        return {
            "revision": state.revision,
            "models": config.models.model_dump(),
            "request": config.features.request.model_dump(),
            "agents": config.features.agents.model_dump(),
        }

    def update_request(self, patch: dict, *, expected_revision=None) -> dict:
        allowed = set(RequestSettings.model_fields)
        if not isinstance(patch, dict) or not patch or patch.keys() - allowed:
            raise ValueError("Unknown or empty request settings patch.")
        patch = dict(patch)
        if patch.get("compaction_routing") is not None and "compaction_routing" in patch:
            patch["compaction_routing"] = CompactionRouting.model_validate(
                patch["compaction_routing"]
            ).model_dump(exclude_none=True)
        if "simple_fallback" in patch and patch["simple_fallback"] is not None:
            patch["simple_fallback"] = FallbackSettings.model_validate(
                patch["simple_fallback"]
            ).model_dump()
        if "spend" in patch and patch["spend"] is not None:
            patch["spend"] = SpendPolicy.model_validate(patch["spend"]).model_dump()
        if "workflow_budget" in patch and patch["workflow_budget"] is not None:
            validate_settings(patch["workflow_budget"])

        def change(config):
            updated = dict(patch)
            if updated.get("workflow_budget") is not None:
                updated["workflow_budget"] = {
                    **(config.features.request.workflow_budget or {}),
                    **updated["workflow_budget"],
                }
            if updated.get("simple_fallback"):
                ids = {channel.id for channel in config.channels}
                for source, targets in updated["simple_fallback"]["routes"].items():
                    for selector in (source, *targets):
                        if split_selector(selector)[0] not in ids:
                            raise ValueError("Fallback channel does not exist.")
            data = config.features.request.model_dump() | updated
            config.features.request = RequestSettings.model_validate(data)

        return self._update(change, expected_revision)

    def update_agents(
        self, patch: dict, *, visible_models: set[str] | None = None, expected_revision=None
    ) -> dict:
        allowed = set(AgentSettings.model_fields)
        if not isinstance(patch, dict) or not patch or patch.keys() - allowed:
            raise ValueError("Unknown or empty agent settings patch.")
        patch = dict(patch)
        for key in ("effort_cap", "subagent_effort_cap", "injection_effort"):
            if key in patch and patch[key] is not None and patch[key] not in _EFFORTS:
                raise ValueError(f"Invalid {key}.")
        for key in ("subagent_models", "model_picker_order", "subagent_model_fallback"):
            if key in patch:
                patch[key] = _strings(patch[key], key)
        if "subagent_models" in patch:
            patch["subagent_models"] = patch["subagent_models"][:5]
        if "model_picker_order" in patch:
            order = patch["model_picker_order"]
            if len(order) != len(set(order)) or (
                visible_models is not None and not set(order) <= visible_models
            ):
                raise ValueError("Picker order must contain unique visible model IDs.")
            if not order:
                patch["model_picker_order_mode"] = None
        elif "model_picker_order_mode" in patch:
            raise ValueError("Picker order mode requires an order.")
        if patch.get("model_picker_order_mode") not in {
            None,
            "alphabetical",
            "provider",
            "most-used",
        }:
            raise ValueError("Invalid picker order mode.")
        if "subagent_model_fallback_poll_ms" in patch and (
            type(patch["subagent_model_fallback_poll_ms"]) is not int
            or not 5000 <= patch["subagent_model_fallback_poll_ms"] <= 600000
        ):
            raise ValueError("Fallback polling must be between 5000 and 600000 ms.")
        if "codex_agent_settings" in patch:
            data = patch["codex_agent_settings"]
            if not isinstance(data, dict) or data.keys() - {
                "agentsEnabled",
                "agentsMaxDepth",
                "maxConcurrentThreadsPerSession",
            }:
                raise ValueError("Unknown Codex agent setting.")
            for key, value in data.items():
                if key == "agentsEnabled":
                    if value is not None and type(value) is not bool:
                        raise ValueError("agentsEnabled must be boolean.")
                elif value is not None and (
                    type(value) is not int
                    or not -(2**31) <= value < 2**31
                    or key == "maxConcurrentThreadsPerSession"
                    and value < 1
                ):
                    raise ValueError(f"Invalid {key}.")

        def change(config):
            data = config.features.agents.model_dump() | patch
            if "codex_agent_settings" in patch:
                existing = config.features.agents.codex_agent_settings
                keys = {"agentsEnabled", "agentsMaxDepth", "maxConcurrentThreadsPerSession"}
                data["codex_agent_settings"] = {
                    **{k: v for k, v in existing.items() if k in keys},
                    **patch["codex_agent_settings"],
                }
            if not data["injection_model"]:
                if patch.get("sync_codex_subagent_defaults") is True:
                    raise ValueError("Native defaults require a model.")
                data.update(injection_effort=None, sync_codex_subagent_defaults=False)
            config.features.agents = AgentSettings.model_validate(data)

        return self._update(change, expected_revision)

    def select_models(self, channel_id: str, models: list[str], *, expected_revision=None) -> dict:
        models = list(dict.fromkeys(_strings(models, "models")))

        def change(config):
            channel = next((c for c in config.channels if c.id == channel_id), None)
            if channel is None:
                raise ValueError("Channel does not exist.")
            channel.models.selected = models

        return self._update(change, expected_revision)

    def set_visibility(
        self,
        channel_id: str,
        models: list[str],
        enabled: bool,
        *,
        all_channel=False,
        expected_revision=None,
    ) -> dict:
        models = _strings(models, "models")
        if type(enabled) is not bool or not models:
            raise ValueError("Models and enabled are required.")

        def change(config):
            channel = next((c for c in config.channels if c.id == channel_id), None)
            if channel is None:
                raise ValueError("Channel does not exist.")
            disabled = set(config.models.disabled)
            legacy = set(config.models.legacy_disabled)
            if enabled and all_channel:
                channel.models.selected = []
                disabled = {s for s in disabled if not s.startswith(channel_id + "/")}
                legacy = {s for s in legacy if not s.startswith(channel_id + "/")}
            elif enabled:
                if channel.models.selected:
                    channel.models.selected = list(
                        dict.fromkeys([*channel.models.selected, *models])
                    )
                disabled.difference_update(
                    ModelIdentity(channel_id, model).namespaced_id for model in models
                )
                legacy.difference_update(
                    spelling
                    for spelling, candidates in legacy_disabled_candidates(
                        config.models, channel, models
                    ).items()
                    if len(candidates) == 1 and candidates <= set(models)
                )
            else:
                disabled.update(ModelIdentity(channel_id, model).namespaced_id for model in models)
            config.models.disabled = sorted(disabled)
            config.models.legacy_disabled = sorted(legacy)

        return self._update(change, expected_revision)

    def set_context_caps(
        self, *, channel_id=None, enabled=None, value=None, set_all=None, expected_revision=None
    ) -> dict:
        if value is not None and (
            type(value) not in {int, float} or not math.isfinite(value) or value < 1
        ):
            raise ValueError("Cap must be a positive finite number.")
        value = int(value) if value is not None else None

        def change(config):
            active = dict(config.models.context_caps or {})
            remembered = {**(config.models.context_cap_values or {}), **active}
            default = config.models.context_cap_value or _CAP
            if channel_id is not None or enabled is not None:
                if (
                    not isinstance(channel_id, str)
                    or type(enabled) is not bool
                    or set_all is not None
                    or not any(c.id == channel_id for c in config.channels)
                ):
                    raise ValueError("A valid channel and enabled flag are required.")
                if enabled:
                    active[channel_id] = value or remembered.get(channel_id, default)
                    remembered[channel_id] = active[channel_id]
                else:
                    active.pop(channel_id, None)
            elif value is not None:
                if set_all is not None and type(set_all) is not bool:
                    raise ValueError("set_all must be boolean.")
                config.models.context_cap_value = value
                if set_all:
                    active = dict.fromkeys(active, value)
                    remembered.update(active)
            elif type(set_all) is bool:
                active = {c.id: default for c in config.channels} if set_all else {}
                remembered.update(active)
            else:
                raise ValueError("Provide a cap operation.")
            config.models.context_caps, config.models.context_cap_values = active, remembered

        return self._update(change, expected_revision)

    def set_disabled_models(self, models: list[str], *, expected_revision=None) -> dict:
        models = list(dict.fromkeys(_strings(models, "models")))

        def change(config):
            channels = {channel.id for channel in config.channels}
            for value in models:
                channel, separator, native = value.partition("/")
                if not separator or channel not in channels or not native.strip():
                    raise ValueError("Use channel/model identities for disabled models.")
            config.models.disabled = models
            config.models.legacy_disabled = []

        return self._update(change, expected_revision)

    def set_discovery_policy(
        self, policy: str, *, channel_id: str | None = None, expected_revision=None
    ) -> dict:
        if policy not in ({"on", "off", "inherit"} if channel_id else {"on", "off"}):
            raise ValueError("Invalid discovery policy.")

        def change(config):
            if channel_id:
                channel = next((c for c in config.channels if c.id == channel_id), None)
                if channel is None:
                    raise ValueError("Channel does not exist.")
                channel.models.metadata.new_model_policy = policy
            else:
                config.models.discovery = {
                    **(config.models.discovery or {}),
                    "newModelPolicy": policy,
                }

        return self._update(change, expected_revision)

    def acknowledge_arrivals(
        self, channel_id: str, ids: list[str], *, expected_revision=None
    ) -> dict:
        ids = _strings(ids, "ids")

        def change(config):
            if not any(c.id == channel_id for c in config.channels):
                raise ValueError("Channel does not exist.")
            discovery = dict(config.models.discovery or {})
            recent = dict(discovery.get("recentArrivals", {}))
            recent[channel_id] = [
                item for item in recent.get(channel_id, []) if item.get("id") not in ids
            ]
            discovery["recentArrivals"] = recent
            config.models.discovery = discovery

        return self._update(change, expected_revision)

    def set_model_preset(self, channel_id: str, mode: str, *, expected_revision=None) -> dict:
        """The selected Lite services have no bundled roster preset."""
        if mode not in {"all", "custom"}:
            raise ValueError("No model preset is shipped for this channel.")

        def change(config):
            channel = next((item for item in config.channels if item.id == channel_id), None)
            if channel is None:
                raise ValueError("Channel does not exist.")
            metadata = channel.models.metadata
            if (metadata.initial_selection or {}).get("status") == "pending":
                raise ValueError("Initial model discovery is pending. Refresh and retry.")
            if mode == "all":
                channel.models.selected = []
                metadata.preset = None
            else:
                metadata.preset = {**(metadata.preset or {}), "mode": "custom"}

        return self._update(change, expected_revision)
