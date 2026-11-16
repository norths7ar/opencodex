"""Application-owned resources and lifecycle; domain modules remain independent."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

import httpx

from backend.auth.access import ChannelAuthentication
from backend.auth.chatgpt import ChatGPTAuth
from backend.auth.quota import ChatGPTQuota
from backend.auth.validation import SubscriptionValidator
from backend.catalog.projection import model_rows
from backend.catalog.refresh import CatalogRefresh
from backend.catalog.selection import reconcile_discovery
from backend.catalog.view import effective_models
from backend.configuration.activation import activate
from backend.configuration.milestones import Milestones
from backend.configuration.store import ConfigurationStore
from backend.gateway.client_pool import UpstreamClients
from backend.gateway.continuation import Continuations
from backend.gateway.dispatch import Dispatcher
from backend.gateway.execution import GenerationExecutor
from backend.gateway.history import HistoryStore, TurnStateStore
from backend.gateway.policy_dispatch import ExecutionPolicies
from backend.gateway.preparation import GenerationPreparation
from backend.gateway.transport import HTTPTransport
from backend.integrations.codex import CodexIntegration
from backend.integrations.coordinator import IntegrationCoordinator
from backend.integrations.dsh import DshIntegration
from backend.integrations.request_routing import resolve_client_selector
from backend.policies.spend_ledger import SpendLedger
from backend.policies.subagent_fallback import SubagentFallback
from backend.policies.workflow import WorkflowBudget
from backend.providers.channels import ChannelService
from backend.providers.discovery import ModelDiscovery
from backend.providers.metadata import preset_metadata
from backend.providers.official_metadata import OfficialModelMetadata
from backend.telemetry.debug import DebugState
from backend.telemetry.ledger import UsageStore
from backend.telemetry.service import Telemetry


class Services:
    def __init__(
        self,
        directory: Path,
        port: int,
        *,
        codex_home: Path,
        dsh_home: Path,
        client: httpx.AsyncClient | None = None,
        background_refresh: bool = True,
    ):
        self.directory = directory
        self.stopping = asyncio.Event()
        self.background_refresh = background_refresh
        self.store = ConfigurationStore(directory)
        activate(self.store)
        self.channels = ChannelService(self.store)
        self.upstreams = UpstreamClients(override=client)
        self.client = self.upstreams.get("https://api.openai.com")
        self.auth = ChatGPTAuth(directory)
        self.authentication = ChannelAuthentication(self.store, self.auth)
        self.quota = ChatGPTQuota(self.auth)
        self.official = OfficialModelMetadata(directory)
        self.discovery = ModelDiscovery(
            self.client, self.authentication, client_for_channel=self.upstreams.for_channel
        )
        self.catalog = CatalogRefresh(
            directory,
            self.configuration,
            self.discover,
            self.authentication.revision,
            legacy_fingerprint=self.authentication.legacy_fingerprint,
            reconcile=self.reconcile,
            projection=self.catalog_projection,
            on_changed=self.catalog_changed,
        )
        self.codex = CodexIntegration(self.store, directory, port, codex_home, self.models)
        self.dsh = DshIntegration(directory, port, lambda: model_rows(self.models()), dsh_home)
        self.integrations = IntegrationCoordinator(self.codex, self.dsh)
        self.debug = DebugState(directory)
        self.milestones = Milestones(directory)
        usage = UsageStore(directory)
        if self.milestones.first_success_at() is None:
            first_success = usage.first_success_at()
            if first_success is not None:
                self.milestones.record_success(
                    datetime.fromtimestamp(first_success / 1000, UTC).isoformat()
                )
        self.telemetry = Telemetry(
            usage, debug=self.debug, on_success=self.milestones.record_success
        )
        self.workflow = WorkflowBudget(self.configuration)
        self.spend = SpendLedger(directory, self.configuration)
        self.executor = GenerationExecutor(
            self.authentication,
            GenerationPreparation(self.authentication),
            HTTPTransport(client_for_send=self.upstreams.for_send),
            self.telemetry,
            Continuations(HistoryStore(directory), TurnStateStore()),
        )
        self.executor.observe_response = self.observe_response
        self.dispatcher = Dispatcher(
            self.configuration,
            self.discovered,
            self.executor,
            self.workflow,
            spend_ledger=self.spend,
            client_selector=resolve_client_selector,
        )
        self.dispatcher.metadata = self.model_metadata
        self.dispatcher.subagents = SubagentFallback(
            self.configuration, self.dispatcher._route_pair, self.codex.fallback_models
        )
        self.dispatcher.subagents.quota_reader = lambda: self.quota.read(force=True)
        self.dispatcher.policies = ExecutionPolicies(
            self.dispatcher, self.models, lambda _: self.auth.status()
        )
        self.auth.on_change = self.auth_changed
        self.auth.validate = SubscriptionValidator(
            self.subscription_channel,
            self.authentication,
            lambda channel: self.upstreams.for_channel(channel, channel.endpoints.base_url),
            set_stage=self.login_stage,
        )

    def subscription_channel(self):
        return next(
            channel
            for channel in self.configuration().channels
            if channel.auth_mode == "oauth" and channel.service_id == "chatgpt"
        )

    def login_stage(self, stage):
        if self.auth.flow is not None:
            self.auth.flow["stage"] = stage

    def configuration(self):
        return self.store.read().configuration

    def observe_response(self, channel, credential, headers, model):
        if channel.auth_mode == "oauth" and credential is not None:
            self.quota.observe_headers(credential, headers, model=model)

    def models(self):
        return effective_models(
            self.configuration(),
            self.catalog.snapshot,
            official_lookup=self.official.cached,
            preset_lookup=preset_metadata,
        )

    def model_metadata(self, channel_id, model):
        return next(
            (
                row.metadata.values
                for row in self.models()
                if row.identity.channel_id == channel_id and row.identity.native_id == model
            ),
            None,
        )

    def discovered(self):
        return {
            channel.id: [row["id"] for row in snapshot.rows]
            for channel in self.configuration().channels
            if (snapshot := self.catalog.snapshot(channel)) is not None
        }

    async def discover(self, channel):
        rows = await self.discovery(channel)
        await self.official.read(channel)
        return rows

    def reconcile(self, channel, ids):
        config = self.configuration()
        if reconcile_discovery(config, channel.id, ids) == config:
            return

        def update(state):
            state.configuration = reconcile_discovery(state.configuration, channel.id, ids)

        self.store.update(update)

    def catalog_projection(self, channel_id):
        return model_rows(
            tuple(row for row in self.models() if row.identity.channel_id == channel_id)
        )

    async def catalog_changed(self):
        return await self.integrations.sync()

    async def changed(self, *_):
        self.catalog.request_refresh()
        return await self.integrations.sync()

    async def auth_changed(self):
        self.quota.invalidate()
        await self.changed()

    @asynccontextmanager
    async def lifespan(self, app):
        self.debug.install()
        task = asyncio.create_task(self.catalog.run()) if self.background_refresh else None
        roles = (
            asyncio.create_task(self.integrations.watch_roles())
            if self.background_refresh
            else None
        )
        try:
            try:
                await self.integrations.converge()
            except (ValueError, RuntimeError, OSError) as exc:
                app.state.integration_error = str(exc)
            yield
        finally:
            self.stopping.set()
            for pending in (task, roles):
                if pending:
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending
            try:
                await asyncio.to_thread(self.codex.shutdown)
            except ValueError, RuntimeError, OSError:
                logging.getLogger(__name__).exception("Could not restore Codex on shutdown")
            await self.auth.close()
            await self.upstreams.close()
            self.spend.close()
            self.debug.close()
