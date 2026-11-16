"""Application operations for retained settings; HTTP only maps the commands."""

from backend.configuration.store import RevisionConflict
from backend.policies.effort import EFFORTS
from backend.policies.search import backend_status
from backend.policies.vision import settings as vision_settings

from .errors import ApplicationError
from .settings_context import SettingsContext


class HelperPolicyManagement(SettingsContext):
    def __init__(self, *, service, catalog, auth_status=None, apply_changes=None):
        super().__init__(service=service, catalog=catalog, apply_changes=apply_changes)
        self.auth_status = auth_status

    def sidecars(self):
        cfg = self.config()
        search = cfg.features.request.web_search_sidecar or {}
        available = (
            {"openai"}
            if any(
                c.service_id == "chatgpt"
                and (not c.disabled)
                and self.auth_status
                and self.auth_status(c.id).get("loggedIn")
                for c in cfg.channels
            )
            else set()
        )
        rows = list(self.catalog())
        channels = {channel.id: channel for channel in cfg.channels}
        options = []
        search_models = {}
        for model in rows:
            channel = channels.get(model.identity.channel_id)
            if channel is None or model.disabled:
                continue
            modes = model.metadata.values.get("inputModalities")
            if model.selected and (modes is None or "image" in modes):
                options.append(
                    {
                        "value": model.routed_selector,
                        "label": model.routed_selector,
                        "backend": "routed",
                    }
                )
                if channel.service_id == "chatgpt" and channel.auth_mode == "oauth":
                    options.append(
                        {
                            "value": model.identity.native_id,
                            "label": model.routed_selector,
                            "backend": "openai",
                        }
                    )
            if (
                channel.service_id == "chatgpt"
                and channel.auth_mode == "oauth"
                and ("openai" in available)
                and ("/" not in model.identity.native_id)
            ):
                identity = model.identity.native_id
                search_models[identity] = {
                    "value": identity,
                    "label": identity,
                    "backend": "openai",
                    "model": identity,
                }
        saved = search.get("model")
        if "openai" in available:
            search_models.setdefault(
                "gpt-5.6-luna",
                {
                    "value": "gpt-5.6-luna",
                    "label": "gpt-5.6-luna",
                    "backend": "openai",
                    "model": "gpt-5.6-luna",
                    "authSlot": True,
                },
            )
        if saved and saved not in search_models:
            search_models[saved] = {
                "value": saved,
                "label": saved,
                "backend": search.get("backend", "openai"),
                "model": saved,
            }
        return {
            "vision": vision_settings(cfg, self.auth_status),
            "visionModels": options,
            "webSearch": {
                "enabled": search.get("enabled") is not False,
                "model": search.get("model", "gpt-5.6-luna"),
                "backend": search.get("backend"),
                "streamRoutedModelOutput": search.get("streamRoutedModelOutput") is True,
            },
            "webSearchModels": sorted(
                search_models.values(), key=lambda row: row["value"].casefold()
            ),
            "webSearchStatus": backend_status(search, available),
        }

    async def put_sidecars(self, body: dict):
        if (
            not body
            or body.keys() - {"vision", "webSearch"}
            or any(not isinstance(v, dict) for v in body.values())
        ):
            raise ApplicationError(400, "Provide vision or webSearch settings objects.")
        patch = {}
        snapshot = self.service.store.read()
        current = snapshot.configuration.features.request
        for name, key, prior, allowed in [
            (
                "vision",
                "vision_sidecar",
                current.vision_sidecar,
                {"enabled", "backend", "model", "reasoning", "maxDescriptionsPerTurn", "timeoutMs"},
            ),
            (
                "webSearch",
                "web_search_sidecar",
                current.web_search_sidecar,
                {"enabled", "backend", "model", "reasoning", "streamRoutedModelOutput"},
            ),
        ]:
            if name not in body:
                continue
            incoming = body[name]
            if incoming.keys() - allowed:
                raise ApplicationError(400, "Unknown sidecar setting.")
            if "enabled" in incoming and type(incoming["enabled"]) is not bool:
                raise ApplicationError(400, "enabled must be boolean.")
            if (
                "streamRoutedModelOutput" in incoming
                and type(incoming["streamRoutedModelOutput"]) is not bool
            ):
                raise ApplicationError(400, "streamRoutedModelOutput must be boolean.")
            if "reasoning" in incoming and incoming["reasoning"] not in EFFORTS:
                raise ApplicationError(400, "Invalid reasoning effort.")
            if "model" in incoming and (
                not isinstance(incoming["model"], str) or not incoming["model"].strip()
            ):
                raise ApplicationError(400, "Invalid model.")
            for field, low, high in [
                ("maxDescriptionsPerTurn", 0, 2**53 - 1),
                ("timeoutMs", 1, 2147483647),
            ]:
                if field in incoming and (
                    type(incoming[field]) is not int or not low <= incoming[field] <= high
                ):
                    raise ApplicationError(400, f"Invalid {field}.")
            merged = {**(prior or {}), **incoming}
            backend = merged.get("backend") or "openai"
            if name == "vision":
                if backend not in {"openai", "routed"}:
                    raise ApplicationError(400, "Select an OpenAI or routed helper.")
                if "model" in incoming or "backend" in incoming:
                    model = merged.get("model") or ("gpt-5.6-luna" if backend == "openai" else "")
                    if (backend == "routed") != ("/" in model):
                        raise ApplicationError(
                            400, "Routed helpers require a channel/model selector."
                        )
                    matches = [
                        row
                        for row in self.catalog()
                        if (
                            row.routed_selector == model
                            if backend == "routed"
                            else row.identity.native_id == model
                            and any(
                                c.id == row.identity.channel_id and c.service_id == "chatgpt"
                                for c in snapshot.configuration.channels
                            )
                        )
                    ]
                    if not matches or all(
                        row.disabled
                        or (
                            row.metadata.values.get("inputModalities") is not None
                            and "image" not in row.metadata.values["inputModalities"]
                        )
                        for row in matches
                    ):
                        raise ApplicationError(
                            400, "Select a configured image-capable helper model."
                        )
            else:
                if backend != "openai":
                    raise ApplicationError(400, "Selected search backend is not supported.")
                if "model" in incoming:
                    identity = merged["model"]
                    candidates = {
                        row.identity.native_id
                        for row in self.catalog()
                        if not row.disabled
                        and any(
                            c.id == row.identity.channel_id and c.service_id == "chatgpt"
                            for c in snapshot.configuration.channels
                        )
                    } | {"gpt-5.6-luna"}
                    if identity not in candidates:
                        raise ApplicationError(
                            400, "Select a model supported by the search backend."
                        )
            patch[key] = {**(prior or {}), **incoming}
        try:
            self.service.update_request(patch, expected_revision=snapshot.revision)
        except RevisionConflict:
            raise ApplicationError(409, "Sidecar validation state changed; retry.") from None
        except ValueError as exc:
            raise ApplicationError(400, str(exc)) from exc
        applied = await self.converge()
        return {
            "ok": True,
            **self.sidecars(),
            "catalogRefresh": applied,
            "catalogRefreshPending": applied.get("status") == "pending",
            "codexWebSearch": {"applied": applied.get("status") == "applied"},
        }
