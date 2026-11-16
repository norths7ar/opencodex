"""Application operations for retained settings; HTTP only maps the commands."""

from backend.catalog.identity import disabled_model_ids, legacy_disabled_candidates

from .errors import ApplicationError
from .settings_context import SettingsContext


class ModelSelectionManagement(SettingsContext):
    async def select_models(self, body: dict):
        self.checked(self.service.select_models, body.get("provider"), body.get("models"))
        return {
            "ok": True,
            "provider": body["provider"],
            "selected": body["models"],
            "catalogRefresh": await self.converge(),
        }

    async def visibility(self, body: dict):
        targets = body.get("targets")
        scope = body.get("scope")
        if (
            scope not in {"models", "provider"}
            or not isinstance(targets, list)
            or (not targets)
            or any(not isinstance(x, dict) or not isinstance(x.get("id"), str) for x in targets)
        ):
            raise ApplicationError(400, "Invalid visibility operation.")
        result = self.checked(
            self.service.set_visibility,
            body.get("provider"),
            [x["id"] for x in targets],
            body.get("enabled"),
            all_channel=scope == "provider",
        )
        channel = next(c for c in self.config().channels if c.id == body["provider"])
        return {
            "ok": True,
            "disabled": result["models"]["disabled"],
            "selected": channel.models.selected,
            "catalogRefresh": await self.converge(),
        }

    async def disabled_models(self, body: dict):
        result = self.checked(self.service.set_disabled_models, body.get("models"))
        return {
            "ok": True,
            "disabled": result["models"]["disabled"],
            "catalogRefresh": await self.converge(),
        }

    def caps(self):
        m = self.config().models
        active = m.context_caps or {}
        return {
            "cap": 350000,
            "value": m.context_cap_value or 350000,
            "caps": active,
            "values": {**(m.context_cap_values or {}), **active},
        }

    async def set_caps(self, body: dict):
        self.checked(
            self.service.set_context_caps,
            channel_id=body.get("provider"),
            enabled=body.get("enabled"),
            value=body.get("value"),
            set_all=body.get("setAll"),
        )
        return {"ok": True, **self.caps(), "catalogRefresh": await self.converge()}

    def discovery(self):
        cfg = self.config()
        state = cfg.models.discovery or {}
        disabled = {channel.id: disabled_model_ids(cfg.models, channel) for channel in cfg.channels}
        return {
            "policy": state.get("newModelPolicy", "on"),
            "providers": {
                c.id: c.models.metadata.new_model_policy or "inherit" for c in cfg.channels
            },
            "recentArrivals": {
                name: [
                    {
                        **row,
                        "state": "auto-disabled"
                        if row["id"] in disabled.get(name, ())
                        else "enabled",
                    }
                    for row in rows
                ]
                for name, rows in state.get("recentArrivals", {}).items()
            },
            "baselineCounts": {
                name: len(row["ids"]) for name, row in state.get("knownModels", {}).items()
            },
            "ambiguousDisabledSelectors": sorted(
                spelling
                for channel in cfg.channels
                for spelling, candidates in legacy_disabled_candidates(cfg.models, channel).items()
                if len(candidates) != 1
            ),
        }

    async def set_discovery(self, body: dict):
        self.checked(
            self.service.set_discovery_policy, body.get("policy"), channel_id=body.get("provider")
        )
        return {
            "ok": True,
            "policy": body.get("policy"),
            "provider": body.get("provider"),
            "catalogRefresh": await self.converge(),
        }

    async def acknowledge(self, body: dict):
        self.checked(self.service.acknowledge_arrivals, body.get("provider"), body.get("ids"))
        return {
            "ok": True,
            "provider": body["provider"],
            "acknowledged": list(dict.fromkeys(body["ids"])),
            "catalogRefresh": await self.converge(),
        }

    def model_presets(self):
        return {"providers": {}}

    async def set_model_preset(self, body: dict):
        mode, name = (body.get("mode"), body.get("provider"))
        self.checked(self.service.set_model_preset, name, mode)
        channel = next(item for item in self.config().channels if item.id == name)
        return {
            "ok": True,
            "provider": name,
            "mode": mode,
            "selected": channel.models.selected,
            "catalogRefresh": await self.converge(),
        }
