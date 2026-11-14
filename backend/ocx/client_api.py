"""Original GUI contracts for the migrated Codex integration."""

import inspect
import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse

from . import client_catalog, client_process, clients, subagent_client
from .runtime import instance_lock


class CodexIntegration:
    def __init__(self, store, directory: Path, port: int, model_rows=None):
        self.store = store
        self.directory = directory
        self.base_url = f"http://127.0.0.1:{port}/v1"
        self.model_rows = model_rows
        self.path = directory / "clients" / "codex-state.json"

    def record(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def _save(self, record: dict):
        clients._atomic_write(self.path, json.dumps(record, indent=2), "Codex integration state")

    def home(self, override: str | None = None) -> Path:
        return clients.codex_home(override or self.record().get("home"))

    async def rows(self) -> list[dict]:
        if self.model_rows is not None:
            value = self.model_rows()
            return await value if inspect.isawaitable(value) else value
        config = self.store.load()
        return [
            {"provider": provider.id, "id": model}
            for provider in config.providers
            if not getattr(provider, "disabled", False)
            for model in provider.models
        ]

    def status(self, home: str | None = None) -> dict:
        target = self.home(home)
        details = clients.status(target, self.directory)
        record = self.record()
        configured = details["state"] == "configured"
        desired = record.get("enabled", configured)
        if str(target) != record.get("home", str(target)):
            desired = configured
        error = details.get("reason") or record.get("error")
        if desired and not configured and not error:
            error = "Codex integration is enabled but applying its configuration is incomplete."
        catalog = client_catalog.catalog_path(target, self.directory)
        if configured and not catalog.exists():
            error = "Codex routing is configured but the model catalog needs synchronization."
        return {
            "clientId": "codex",
            "state": "unsafe" if error else "current" if configured else "absent",
            "installed": target.exists(),
            "configPath": details["configPath"],
            "desiredEnabled": desired,
            "disableBlocked": {"reason": "write_failed", "message": error}
            if details["state"] == "conflict"
            else None,
            "catalogPath": str(catalog),
            "home": str(target),
            "message": error or "",
            "model": details.get("model"),
            "clientCompaction": details.get("clientCompaction", False),
            "retainedProviderTable": details.get("retainedProviderTable", False),
            "warning": details.get("warning", ""),
        }

    async def sync(self, home: str | None = None, *, enable: bool | None = None) -> dict:
        target = self.home(home)
        rows = await self.rows()
        config = self.store.load()
        with instance_lock(self.directory, "codex-integration.lock"):
            record = self.record()
            if record.get("home") and record["home"] != str(target) and record.get("enabled"):
                raise clients.ClientConfigError(
                    "Restore the currently managed Codex home before applying another home."
                )
            current = clients.status(target, self.directory)
            enabled = (
                record.get("enabled", current["state"] == "configured")
                if enable is None
                else enable
            )
            if not enabled:
                return {
                    "ok": True,
                    "status": "skipped",
                    "added": 0,
                    "catalogWritten": False,
                    "cacheSynced": False,
                    "message": "Codex integration is off; no client files changed.",
                }
            provider = next((p for p in config.providers if p.id == config.defaultProvider), None)
            default_model = (provider.defaultModel if provider else None) or config.defaultModel
            if not config.defaultProvider or not default_model:
                raise clients.ClientConfigError(
                    "Select a default provider and model before applying Codex."
                )
            if provider is None or provider.disabled:
                raise clients.ClientConfigError("The default provider is disabled or unavailable.")
            model = f"{config.defaultProvider}/{default_model}"
            enabled_rows = client_catalog.catalog_entries(rows)
            if not enabled_rows:
                raise clients.ClientConfigError("Select at least one model before applying Codex.")
            if model not in {row["slug"] for row in enabled_rows}:
                # Selection controls picker visibility, not explicit routing permission.
                # Preserve the configured default even when its picker row was deselected.
                matching = next(
                    (
                        row
                        for row in rows
                        if row.get("provider") == config.defaultProvider
                        and row.get("id") == default_model
                    ),
                    None,
                )
                if matching is None:
                    raise clients.ClientConfigError(
                        "The default model is unavailable for client access."
                    )
                model = matching.get("namespaced") or matching.get("alias") or model
            if current["state"] == "conflict":
                raise clients.ClientConfigError(current["reason"])
            clients.validate_configuration(
                target,
                self.base_url,
                model,
                self.directory,
                catalog_path=client_catalog.catalog_path(target, self.directory),
                client_compaction=getattr(config, "codexClientCompaction", False) is True,
            )
            catalog, routed_entries = client_catalog.prepare_catalog(
                target, self.directory, rows, config=config
            )
            clients.reference_config(
                target,
                self.directory,
                self.base_url,
                client_catalog.catalog_path(target, self.directory),
                client_compaction=getattr(config, "codexClientCompaction", False) is True,
                validate=True,
            )
            # Validate the requested target before recording intent. A refused first
            # application must not silently become an automatic apply on a later edit.
            # Once writes begin, retain intent so startup can recover a partial publish.
            if enable is not None:
                record.update(home=str(target), enabled=enabled)
                record.pop("error", None)
                self._save(record)
            try:
                path, written = client_catalog.write_catalog(
                    target, self.directory, rows, prepared=catalog
                )
                applied = clients.configure(
                    target,
                    self.base_url,
                    model,
                    self.directory,
                    catalog_path=path,
                    client_compaction=getattr(config, "codexClientCompaction", False) is True,
                )
                cache_synced = client_catalog.sync_cache(
                    target, self.directory, rows, prepared=routed_entries
                )
                subagent_client.sync_settings(target, self.directory, config)
                reference_path = clients.reference_config(
                    target,
                    self.directory,
                    self.base_url,
                    path,
                    client_compaction=getattr(config, "codexClientCompaction", False) is True,
                )
            except (ValueError, RuntimeError, OSError) as exc:
                record.update(home=str(target), enabled=True, error=str(exc))
                self._save(record)
                raise
            record.update(home=str(target), enabled=True)
            record.pop("error", None)
            self._save(record)
            return {
                "ok": True,
                "status": "applied",
                "added": len(enabled_rows),
                "catalogPath": str(path),
                "referenceConfigPath": reference_path,
                "catalogExists": True,
                "catalogWritten": written,
                "cacheSynced": cache_synced,
                "configChanged": current.get("model") != applied.get("model")
                or current.get("clientCompaction") != applied.get("clientCompaction")
                or current["state"] != "configured",
                "message": "Codex configuration and model catalog synchronized.",
                "warning": " ".join(
                    filter(
                        None,
                        [
                            "Client-side compaction is enabled for new opencodex threads; ChatGPT login remains required. Existing openai threads still route through this gateway. Some remote clients filter history by default provider and may hide these existing threads; no history was relabeled."
                            if applied.get("clientCompaction")
                            else "",
                            "The selected Codex profile has its own model provider and can override these gateway defaults; its settings were preserved."
                            if applied.get("profileOverride")
                            else "",
                        ],
                    )
                ),
                "staleAppServerHint": "Restart Codex to load the updated model picker."
                if written or cache_synced
                else "",
            }

    def restore(self, home: str | None = None) -> dict:
        return self._restore(home, disable=True)

    def shutdown(self) -> dict:
        """Restore native routing on stop without changing the user's enable intent."""
        if not self.record():
            return {"ok": True, "status": "skipped"}
        return self._restore(disable=False)

    def _restore(self, home: str | None = None, *, disable: bool) -> dict:
        target = self.home(home)
        with instance_lock(self.directory, "codex-integration.lock"):
            record = self.record()
            record.update(home=str(target))
            if disable:
                record["enabled"] = False
            record.pop("error", None)
            self._save(record)
            try:
                subagent_client.sync_settings(target, self.directory, restore=True)
                clients.reference_config(target, self.directory, restore=True)
                result = clients.restore(target, self.directory)
                client_catalog.restore_cache(target, self.directory)
                client_catalog.catalog_path(target, self.directory).unlink(missing_ok=True)
            except (ValueError, RuntimeError, OSError) as exc:
                record["error"] = str(exc)
                self._save(record)
                raise
            record.pop("error", None)
            self._save(record)
            return result

    async def converge(self) -> dict:
        """Resume the last explicit intent after a process exit during a client write."""
        record = self.record()
        if not record:
            return {"ok": True, "status": "skipped"}
        if record.get("enabled"):
            return await self.sync()
        self.restore()
        return {"ok": True, "status": "restored"}


def client_router(integration: CodexIntegration) -> APIRouter:
    router = APIRouter(prefix="/api")
    store, directory = integration.store, integration.directory

    @router.get("/clients/codex")
    def preview(home: str | None = None):
        target = integration.home(home)
        config = store.load()
        provider = next((p for p in config.providers if p.id == config.defaultProvider), None)
        model = (provider.defaultModel if provider else None) or config.defaultModel
        selector = f"{config.defaultProvider}/{model}" if config.defaultProvider and model else ""
        return {
            **clients.preview(target, integration.base_url, selector, directory),
            "catalogPath": str(client_catalog.catalog_path(target, directory)),
            "home": str(target),
            "integration": integration.status(home),
        }

    @router.post("/clients/codex/configure")
    async def configure(body: Annotated[dict | None, Body()] = None):
        home = (body or {}).get("home")
        outcome = await integration.sync(home, enable=True)
        return {**clients.status(integration.home(home), directory), **outcome}

    @router.post("/clients/codex/restore")
    def restore(body: Annotated[dict | None, Body()] = None):
        return integration.restore((body or {}).get("home"))

    @router.get("/native-integrations")
    def native_status():
        return {"clients": [integration.status()]}

    @router.put("/native-integrations/codex")
    async def toggle(body: dict):
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(400, "enabled must be a boolean")
        before = integration.status(body.get("home"))
        try:
            if enabled:
                outcome = await integration.sync(body.get("home"), enable=True)
            else:
                integration.restore(body.get("home"))
                outcome = {"message": "Codex configuration restored; the gateway remains running."}
        except (ValueError, RuntimeError, OSError) as exc:
            return JSONResponse(
                {
                    "error": "Codex integration change failed",
                    "code": "native_integration_failed",
                    "clientId": "codex",
                    "reason": "write_failed",
                    "message": str(exc),
                    "desiredEnabled": enabled,
                },
                status_code=409,
            )
        after = integration.status(body.get("home"))
        return {
            "ok": True,
            "clientId": "codex",
            "changed": before != after,
            "state": after["state"],
            "desiredEnabled": enabled,
            "message": outcome["message"],
        }

    @router.post("/sync")
    async def sync(body: Annotated[dict | None, Body()] = None):
        return await integration.sync((body or {}).get("home"))

    @router.get("/system/codex-app-server")
    def process_status():
        return client_process.freshness(client_catalog.catalog_path(integration.home(), directory))

    @router.post("/system/codex-restart")
    async def restart():
        import anyio

        synced = await integration.sync()
        return await anyio.to_thread.run_sync(
            lambda: client_process.restart(
                client_catalog.catalog_path(integration.home(), directory),
                synced=bool(synced["catalogWritten"] or synced["cacheSynced"]),
            )
        )

    return router
