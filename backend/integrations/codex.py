"""Codex desired-state lifecycle and reversible configuration."""

import json
from pathlib import Path

from . import agent_settings, client_catalog, clients
from .locks import instance_lock
from .projection import model_rows


class CodexIntegration:
    def __init__(self, store, directory: Path, port: int, home: Path, catalog):
        self.store = store
        self.directory = directory
        self.base_url = f"http://127.0.0.1:{port}/v1"
        self.catalog = catalog
        self.default_home = Path(home).expanduser().resolve()
        self.path = directory / "clients" / "codex-state.json"

    def record(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def _save(self, record: dict):
        clients._atomic_write(self.path, json.dumps(record, indent=2), "Codex integration state")

    def home(self, override: str | None = None) -> Path:
        return (
            Path(override or self.record().get("home") or self.default_home).expanduser().resolve()
        )

    async def rows(self) -> list[dict]:
        return model_rows(self.catalog())

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
        config = self.store.read().configuration
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
            provider = next((p for p in config.channels if p.id == config.default_channel_id), None)
            default_model = (
                provider.models.default if provider else None
            ) or config.default_model_id
            if provider is None or provider.disabled or not default_model:
                raise clients.ClientConfigError(
                    "Select an available default channel and model before applying Codex."
                )
            matching = next(
                (
                    row
                    for row in rows
                    if row["provider"] == provider.id and row["id"] == default_model
                ),
                None,
            )
            if matching is None:
                raise clients.ClientConfigError(
                    "The default model is unavailable for client access."
                )
            model = matching["namespaced"]
            enabled_rows = client_catalog.catalog_entries(rows)
            if not enabled_rows:
                raise clients.ClientConfigError("Select at least one model before applying Codex.")
            if current["state"] == "conflict":
                raise clients.ClientConfigError(current["reason"])
            clients.validate_configuration(
                target,
                self.base_url,
                model,
                self.directory,
                catalog_path=client_catalog.catalog_path(target, self.directory),
                client_compaction=config.features.request.codex_client_compaction,
            )
            catalog, routed_entries = client_catalog.prepare_catalog(
                target, self.directory, rows, config=config
            )
            clients.reference_config(
                target,
                self.directory,
                self.base_url,
                client_catalog.catalog_path(target, self.directory),
                client_compaction=config.features.request.codex_client_compaction,
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
                    client_compaction=config.features.request.codex_client_compaction,
                )
                cache_synced = client_catalog.sync_cache(
                    target, self.directory, rows, prepared=routed_entries
                )
                agent_settings.sync_settings(target, self.directory, config)
                reference_path = clients.reference_config(
                    target,
                    self.directory,
                    self.base_url,
                    path,
                    client_compaction=config.features.request.codex_client_compaction,
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
                agent_settings.sync_settings(target, self.directory, restore=True)
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
