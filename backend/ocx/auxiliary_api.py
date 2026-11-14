"""Original auxiliary model settings over retained helper execution."""

from fastapi import APIRouter, HTTPException

from .auxiliary import Auxiliary, helper_selector, modalities, settings, usable_subscription
from .search_auxiliary import backend_status
from .search_settings import apply_patch as search_patch
from .search_settings import candidate_rows, model_options
from .subagents import EFFORTS


def auxiliary_router(service: Auxiliary, integration):
    router = APIRouter(prefix="/api")
    store, inventory, auth_status = service.store, service.inventory, service.auth_status

    def search_candidates(config, rows):
        available = {
            provider.id
            for provider in config.providers
            if provider.id in {"openai", "anthropic"}
            and provider.authMode == "oauth"
            and not provider.disabled
            and auth_status
            and usable_subscription(auth_status(provider.id))
        }
        candidates = candidate_rows(
            [{**row, "native": not row.get("custom")} for row in rows], available
        )
        return candidates, available

    async def state():
        config = store.load()
        rows = await inventory.rows()
        options = []
        for row in rows:
            if row.get("disabled") or not row.get("selected", True):
                continue
            provider = next((p for p in config.providers if p.id == row["provider"]), None)
            if provider is None:
                continue
            modes = modalities(config, provider, row["id"], rows)
            if modes is not None and "image" not in modes:
                continue
            slug = row.get("namespaced") or f"{row['provider']}/{row['id']}"
            if provider.id in {"openai", "anthropic"} and provider.authMode == "oauth":
                options.append({"value": row["id"], "label": slug, "backend": provider.id})
            options.append({"value": slug, "label": slug, "backend": "routed"})
        stored_search = getattr(config, "webSearchSidecar", {})
        candidates, available = search_candidates(config, rows)
        return {
            "vision": settings(config, auth_status),
            "visionModels": options,
            "webSearch": {
                "enabled": stored_search.get("enabled") is not False,
                "model": stored_search.get("model", "gpt-5.6-luna"),
                "backend": stored_search.get("backend"),
                "streamRoutedModelOutput": stored_search.get("streamRoutedModelOutput") is True,
            },
            "webSearchModels": model_options(stored_search, candidates),
            "webSearchStatus": backend_status(stored_search, available),
        }

    @router.get("/sidecar-settings")
    async def get_settings():
        return await state()

    async def validate_vision(patch):
        if set(patch) - {
            "enabled",
            "backend",
            "model",
            "reasoning",
            "maxDescriptionsPerTurn",
            "timeoutMs",
        }:
            raise HTTPException(400, "Unknown vision setting")
        if "enabled" in patch and type(patch["enabled"]) is not bool:
            raise HTTPException(400, "enabled must be boolean")
        if "backend" in patch and patch["backend"] not in {"routed", "openai", "anthropic", None}:
            raise HTTPException(400, "Select an OpenAI, Anthropic or routed helper")
        if "model" in patch:
            if not isinstance(patch["model"], str) or not patch["model"].strip():
                raise HTTPException(400, "model must be a nonempty model selector")
            try:
                config = store.load()
                prospective = {**getattr(config, "visionSidecar", {}), **patch}
                backend = prospective.get("backend")
                if (backend == "routed") != ("/" in patch["model"]):
                    raise ValueError(
                        "Routed helpers use provider/model; subscription helpers use a bare model."
                    )
                merged = config.model_copy(update={"visionSidecar": prospective})
                selector = helper_selector(settings(merged, auth_status))
                if not any(p.id == selector.split("/", 1)[0] for p in config.providers):
                    raise ValueError("Select a configured provider/model helper")
                target, model = store.route(selector)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            modes = modalities(store.load(), target, model, await inventory.rows())
            if modes is not None and "image" not in modes:
                raise HTTPException(400, "The helper model cannot accept images")
        if "reasoning" in patch and patch["reasoning"] not in EFFORTS:
            raise HTTPException(400, "Invalid reasoning effort")
        for key, low, high in (
            ("maxDescriptionsPerTurn", 0, (1 << 53) - 1),
            ("timeoutMs", 1, 2147483647),
        ):
            if key in patch and (type(patch[key]) is not int or not low <= patch[key] <= high):
                raise HTTPException(400, f"{key} must be an integer between {low} and {high}")

    @router.put("/sidecar-settings")
    async def put_settings(body: dict):
        if (
            not body
            or set(body) - {"vision", "webSearch"}
            or any(not isinstance(patch, dict) for patch in body.values())
        ):
            raise HTTPException(400, "Provide vision or webSearch settings objects.")
        before = store.load().model_dump(mode="json")
        if "vision" in body:
            await validate_vision(body["vision"])
        rows = await inventory.rows() if "webSearch" in body else []
        with store.mutation_lock:
            config = store.load()
            if config.model_dump(mode="json") != before:
                raise HTTPException(409, "Sidecar validation state changed during discovery.")
            if "webSearch" in body:
                candidates, _ = search_candidates(config, rows)
                try:
                    config.webSearchSidecar = search_patch(
                        getattr(config, "webSearchSidecar", {}), body["webSearch"], candidates
                    )
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
            if "vision" in body:
                config.visionSidecar = {
                    **getattr(config, "visionSidecar", {}),
                    **body["vision"],
                }
            store.save(config)
        search_changed = (before.get("webSearchSidecar", {}).get("enabled") is not False) != (
            getattr(config, "webSearchSidecar", {}).get("enabled") is not False
        )
        try:
            applied = await integration.sync()
        except (ValueError, RuntimeError, OSError) as exc:
            applied = {"status": "pending", "message": str(exc)}
        report = {"applied": False, "reason": "not_requested", "retryable": False}
        if search_changed:
            if applied and applied.get("status") == "applied":
                report = {"applied": True}
            elif not applied or applied.get("status") == "skipped":
                report = {
                    "applied": False,
                    "reason": "integration_disabled",
                    "retryable": False,
                }
            else:
                report = {
                    "applied": False,
                    "reason": "injection_refused",
                    "retryable": True,
                    "detail": applied.get("message", "Review the Codex connection in Route."),
                }
        return {
            "ok": True,
            **await state(),
            "catalogRefresh": applied,
            "catalogRefreshPending": bool(applied and applied.get("status") == "pending"),
            "codexWebSearch": report,
        }

    return router
