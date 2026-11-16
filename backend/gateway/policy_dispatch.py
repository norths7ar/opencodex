"""Apply optional request policies through the ordinary accounted executor."""

from contextlib import asynccontextmanager

from backend.integrations.projection import model_rows
from backend.policies.vision import usable_subscription
from backend.providers.registry import match_destination

from .auxiliary import AuxiliaryDispatch, borrowed
from .compaction_dispatch import dispatch_compaction
from .history import subscription_issuer
from .search_delivery import dispatch_search, restore_search_replay


class ExecutionPolicies:
    def __init__(self, dispatcher, models, auth_status):
        self.dispatcher = dispatcher
        self.auth_status = auth_status
        self.auxiliary = AuxiliaryDispatch(
            dispatcher.config_loader,
            lambda: model_rows(models()),
            dispatcher._route_pair,
            dispatcher.execute,
            auth_status=auth_status,
        )

    async def expand(self, body, source):
        credential = (
            await self.dispatcher.executor.authentication.token(source.channel)
            if source.channel.auth_mode == "oauth"
            else None
        )
        issuer = subscription_issuer(credential["accountId"]) if credential else None
        return self.dispatcher.executor.continuations.history.expand_history(
            body, source.channel.id, source.model_id, issuer
        )[0]

    @staticmethod
    def native_compaction(source, target, compact):
        channel = target.channel
        if (
            source.channel.id != channel.id
            or channel.protocol_for_model(target.model_id) != "openai-responses"
        ):
            return False
        match = match_destination(channel)
        canonical = match is not None and match.service.id in {"openai", "chatgpt"}
        policy = channel.request_policy
        return canonical or bool(
            policy.supportsNativeCompactEndpoint
            if compact
            else policy.supportsNativeCompactionTrigger
        )

    async def save_compaction(self, source, body, result, *, fallback_origin=None):
        credential = (
            await self.dispatcher.executor.authentication.token(source.channel)
            if source.channel.auth_mode == "oauth"
            else None
        )
        issuer = subscription_issuer(credential["accountId"]) if credential else None
        self.dispatcher.executor.continuations.history.save_response(
            source.channel.id,
            source.model_id,
            body,
            result,
            issuer,
            fallback_origin=fallback_origin,
        )

    @asynccontextmanager
    async def intercept(self, request, body, headers, lease, *, logical_source=None):
        if request.protocol not in {"responses", "responses/compact"}:
            yield None
            return
        async with dispatch_compaction(
            request,
            body,
            headers,
            self.dispatcher.config_loader(),
            self.dispatcher.route,
            self.expand,
            self.native_compaction,
            self.dispatcher.execute,
            borrowed_lease=borrowed(lease),
            save_response=self.save_compaction,
            logical_source=logical_source,
            binding_lookup=self.dispatcher.executor.continuations.history.fallback_response_route,
        ) as response:
            yield response

    def native_search(self, target, body):
        match = match_destination(target.channel)
        route = self.dispatcher.executor.preparation.route(
            target.channel, target.model_id, "responses", body
        )
        return (
            route.wire == "responses"
            and match is not None
            and match.service.id in {"openai", "chatgpt"}
        )

    @asynccontextmanager
    async def search(
        self, request, body, target, headers, lease, *, fallback_origin=None, principal=None
    ):
        if request.protocol != "responses":
            yield None
            return
        native = self.native_search(target, body)
        status = self.auth_status("openai")
        helper = next(
            (
                channel
                for channel in self.dispatcher.config_loader().channels
                if channel.auth_mode == "oauth"
                and channel.service_id == "chatgpt"
                and not channel.disabled
            ),
            None,
        )
        available = ("openai",) if helper and usable_subscription(status) else ()

        async def save(source, checkpoint, result):
            await self.save_compaction(source, checkpoint, result, fallback_origin=fallback_origin)

        async with dispatch_search(
            self.auxiliary,
            request,
            body,
            target,
            headers,
            lease,
            available=available,
            native=native,
            expand=self.expand,
            save_response=save,
            account_scope=status.get("activeAccountId"),
            principal=principal,
            helper_channel=helper.id if helper else None,
        ) as response:
            yield response

    async def prepare(self, body, route, target, context, headers, lease):
        await self.auxiliary.vision(body, route.inbound, target, context, headers, lease)
        if context.parent_request_id is None and route.inbound == "responses":
            native = self.native_search(target, body)
            restored = restore_search_replay(self.auxiliary, body, context, headers, native=native)
            if restored is not body:
                body.clear()
                body.update(restored)
