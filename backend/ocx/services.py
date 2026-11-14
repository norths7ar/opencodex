"""Composition and lifecycle for the loopback application services."""

import asyncio
import logging
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path

from .auxiliary import Auxiliary
from .auxiliary_calls import vision_completion
from .client_api import CodexIntegration
from .config import ConfigStore
from .debug_state import DebugState
from .gateway import Gateway
from .internal_calls import sizing_completion
from .models import ModelInventory
from .spend_ledger import SpendLedger
from .subagent_api import SubagentService


class AppServices:
    def __init__(self, directory: Path, port: int):
        self.directory = directory
        self.stopping = asyncio.Event()
        self.store = ConfigStore(directory)
        self.gateway = Gateway(self.store)
        self.diagnostics = DebugState(directory)
        channels = self.gateway.channels

        def auth_revision(provider):
            return channels.for_provider(provider).revision

        self.inventory = ModelInventory(self.store, self.gateway.models, auth_revision)
        self.quota = channels.quota("openai")
        self.kimi_quota = channels.quota("kimi")
        channels.on_change = self.subscription_changed
        self.integration = CodexIntegration(self.store, directory, port, self.inventory.rows)
        self.subagents = SubagentService(self.store, self.inventory, self.integration)
        self.subagents.complete_sizing = lambda call: sizing_completion(port, call)
        self.subagents.fallback.quota_reader = lambda: self.quota.read(force=True)
        self.auxiliary = Auxiliary(self.store, self.inventory, channels.status)
        self.auxiliary.complete = lambda call: vision_completion(port, call)
        self.gateway.model_rows = self.inventory.rows
        self.gateway.subagents = self.subagents
        self.gateway.debug = self.diagnostics
        self.gateway.auxiliary = self.auxiliary

    async def refresh_catalog(self):
        try:
            result = await self.integration.sync()
            return {
                "status": "committed",
                "changed": bool(result.get("catalogWritten")),
                "degraded": False,
            }
        except (ValueError, RuntimeError, OSError) as exc:
            return {"status": "pending", "changed": False, "degraded": True, "message": str(exc)}

    async def subscription_changed(self, identities):
        if "openai" in identities:
            self.quota.invalidate()
        for identity in identities:
            self.inventory.invalidate(identity, reset=True)
        await self.refresh_catalog()

    async def imported_providers(self, identities):
        for identity in identities:
            self.inventory.invalidate(identity, reset=True)
        result = await self.refresh_catalog()
        if result["status"] != "committed":
            raise RuntimeError(
                "The imported channels are saved; catalog synchronization is pending."
            )

    def spend_diagnostics(self):
        return self.gateway.spend_ledger.diagnostics()

    @asynccontextmanager
    async def lifespan(self, app):
        with ExitStack() as owned:
            ledger = SpendLedger(self.directory, self.store.load)
            self.gateway.spend_ledger = ledger
            owned.callback(ledger.close)
            async with self.gateway.lifespan(app):
                self.diagnostics.install()
                try:
                    try:
                        await self.integration.converge()
                    except (ValueError, RuntimeError, OSError) as exc:
                        # Let the console repair an interrupted integration.
                        app.state.integration_error = str(exc)
                    yield
                finally:
                    self.diagnostics.close()
                    try:
                        await asyncio.to_thread(self.integration.shutdown)
                    except ValueError, RuntimeError, OSError:
                        logging.getLogger(__name__).exception("Could not restore Codex on shutdown")
