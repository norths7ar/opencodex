"""Logical routing, ordered fallback and request-level resource ownership."""

import asyncio
import copy
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from hashlib import sha256
from typing import TYPE_CHECKING, cast

import httpx

from backend.policies.fallback import (
    FallbackTarget,
    SimpleFallback,
    TargetReplayUnavailable,
    failure_trigger,
    prepare_retry,
    retry_headers,
    upstream_error_code,
)
from backend.policies.pacing import PacingDenied
from backend.policies.spend_calls import RequestSpend
from backend.policies.spend_ledger import SpendDenied
from backend.policies.subagent_fallback import SubagentFallback
from backend.policies.workflow import WorkflowDenied
from backend.requests import AttemptReason, Outcome, RequestPurpose, TerminalStatus

from .compaction_dispatch import CompactionResponse
from .delivery_sse import UpstreamBodyOverflow
from .execution import GenerationResponse
from .history import subscription_issuer
from .routing import native_route, resolve_route
from .search_delivery import SearchResponse
from .transport import finish_cleanup, until_cancelled

if TYPE_CHECKING:
    from .policy_dispatch import ExecutionPolicies


class Dispatcher:
    def __init__(
        self,
        config_loader,
        discovered,
        executor,
        workflow,
        *,
        spend_ledger=None,
        client_selector=None,
    ):
        self.config_loader = config_loader
        self.discovered = discovered
        self.executor = executor
        self.workflow = workflow
        self.spend_ledger = spend_ledger
        self.client_selector = client_selector
        self.fallback = SimpleFallback(config_loader, self._route_pair)
        self.subagents: SubagentFallback | None = None
        self.policies: ExecutionPolicies | None = None
        self.metadata: Callable[[str, str], dict[str, object] | None] = lambda _channel, _model: (
            None
        )

    def route(self, selector):
        return resolve_route(self.config_loader(), selector, self.discovered())

    def _route_pair(self, selector):
        route = self.route(selector)
        return route.channel, route.model_id

    def route_native(self, channel_id, model_id):
        return native_route(self.config_loader(), channel_id, model_id)

    def route_history(self, namespaced_id):
        channel, _, model = namespaced_id.partition("/")
        return self.route_native(channel, model)

    @staticmethod
    def _reservation(tracker, lease, prepared, channel, model, subscription):
        scopes = {"poolId": channel.id}
        if lease:
            scopes["rootId"] = lease.root_id
        if subscription:
            scopes["identityId"] = channel.id + "/" + subscription["accountId"]
        else:
            credential = next(
                (
                    value.removeprefix("Bearer ")
                    for key, value in prepared.headers.items()
                    if key.lower() in {"authorization", "x-api-key", "api-key"}
                ),
                None,
            )
            if credential:
                scopes["identityId"] = (
                    channel.id + "/key/" + sha256(credential.encode()).hexdigest()
                )
        if tracker:
            return tracker.prepare(
                prepared.body,
                prepared.wire,
                scopes,
                reserve_count=lease.reserve_send if lease else None,
                model=model,
            )
        if lease:
            lease.reserve_send()
        return None

    async def _replay(self, original, selected, target, cancellation):
        expanded = None
        if original.get("previous_response_id"):
            route = self.route_native(selected.provider, selected.model)
            credential = (
                await until_cancelled(
                    self.executor.authentication.token(route.channel), cancellation
                )
                if route.channel.auth_mode == "oauth"
                else None
            )
            issuer = subscription_issuer(credential["accountId"]) if credential else None
            expanded, _ = self.executor.continuations.history.expand_history(
                original, selected.provider, selected.model, issuer
            )
        return prepare_retry(original, selected, target, expanded_body=expanded)

    @asynccontextmanager
    async def _target(self, route, request, payload, headers, row_id, lease, **options):
        search_principal = options.pop("search_principal", None)
        if (
            self.policies is not None
            and request.parent_request_id is None
            and request.purpose not in {RequestPurpose.PROBE, RequestPurpose.SIZING}
        ):
            async with self.policies.search(
                request,
                payload,
                route,
                headers,
                lease,
                fallback_origin=options.get("fallback_origin"),
                principal=search_principal,
            ) as response:
                if response is not None:
                    yield response
                    return
        async with self.executor.execute(
            self.config_loader(), route, request, payload, headers, row_id, **options
        ) as response:
            yield response

    @asynccontextmanager
    async def execute(
        self,
        request,
        body,
        incoming,
        *,
        borrowed_lease=None,
        prepare_body=None,
        resolved_route=None,
    ) -> AsyncIterator[GenerationResponse | CompactionResponse | SearchResponse]:
        """Keep the context open until the returned stream has been consumed."""
        started = time.monotonic()
        original = copy.deepcopy(body)

        def normalize_client_selector(selector):
            if self.client_selector is None or request.parent_request_id is not None:
                return selector
            return self.client_selector(
                self.config_loader(), selector, request.client_id, request.protocol
            )

        if self.client_selector is not None and request.parent_request_id is None:
            selector = normalize_client_selector(original.get("model"))
            if selector is not None:
                original["model"] = selector
        logical_body = copy.deepcopy(original)
        incoming = {key.lower(): value for key, value in incoming.items()}
        first = resolved_route or self.route(original.get("model"))
        logical_route = first
        logical_source = FallbackTarget(first.channel.id, first.model_id)
        binding = (
            self.executor.continuations.history.fallback_response_route(
                original.get("previous_response_id")
            )
            if request.protocol in {"responses", "responses/compact"}
            else None
        )
        if binding and binding[0] == logical_source.selector:
            first = self.route_history(binding[1])
            original["model"] = binding[1]
        elif (
            resolved_route is None
            and self.subagents
            and request.protocol == "responses"
            and request.purpose not in {RequestPurpose.PROBE, RequestPurpose.SIZING}
        ):
            await self.subagents.select(
                original,
                incoming,
                lookup_model=body.get("model"),
                normalize=normalize_client_selector,
            )
            first = self.route(original.get("model"))
        selected = FallbackTarget(first.channel.id, first.model_id)
        plan = self.fallback.plan(
            logical_source.provider, logical_source.model, requested=request.requested_model
        )
        targets = [selected]
        if request.protocol in {
            "responses",
            "messages",
            "chat/completions",
        } and request.purpose not in {RequestPurpose.PROBE, RequestPurpose.SIZING}:
            targets.extend(target for target in plan.candidates if target != selected)
        lease = borrowed_lease or self.workflow.admit_http(incoming)
        tracker = RequestSpend(self.spend_ledger) if self.spend_ledger else None
        row_id = None
        response = None
        retry_error = None
        outcome = Outcome(status_code=500, terminal_status=TerminalStatus.FAILED)
        trigger = None
        previous = selected
        delivered_to_caller = False
        telemetry = self.executor.telemetry
        try:

            async def begin():
                nonlocal row_id
                row_id = await telemetry.begin_request(request)

            await finish_cleanup(begin())
            if (
                self.policies is not None
                and request.parent_request_id is None
                and request.purpose not in {RequestPurpose.PROBE, RequestPurpose.SIZING}
            ):
                async with self.policies.intercept(
                    request,
                    logical_body,
                    incoming,
                    lease,
                    logical_source=logical_route,
                ) as intercepted:
                    if intercepted is not None:
                        response = intercepted
                        delivered_to_caller = True
                        yield intercepted
                        state = intercepted.state
                        outcome = Outcome(
                            status_code=state.logical_status or intercepted.status,
                            terminal_status=TerminalStatus(state.terminal)
                            if state.terminal
                            else None,
                            error_code=state.error_code,
                        )
                        return
            if (
                prepare_body is None
                and self.policies is not None
                and request.purpose not in {RequestPurpose.PROBE, RequestPurpose.SIZING}
            ):
                policies = self.policies

                async def prepare_policy_body(body, route, target, context):
                    await policies.prepare(body, route, target, context, incoming, lease)

                prepare_body = prepare_policy_body

            for index, target in enumerate(targets):
                if request.cancellation.is_set():
                    raise asyncio.CancelledError
                if index and not trigger:
                    break
                try:
                    payload = (
                        await self._replay(original, selected, target, request.cancellation)
                        if index
                        else original
                    )
                except TargetReplayUnavailable as exc:
                    self.fallback.record(
                        plan.source,
                        previous,
                        target,
                        cast(str, trigger),
                        outcome="skipped",
                        reason=str(exc),
                    )
                    continue
                except (ValueError, RuntimeError) as exc:
                    self.fallback.record(
                        plan.source,
                        previous,
                        target,
                        cast(str, trigger),
                        outcome="refused",
                        reason=str(exc),
                    )
                    break
                route = self.route_native(target.provider, target.model)
                changed = target != logical_source
                headers = retry_headers(incoming) if changed else incoming
                sent_before = telemetry.send_count(request)
                try:
                    if index:
                        self.executor.telemetry.fallback(
                            previous.selector, target.selector, trigger
                        )
                    async with self._target(
                        route,
                        request,
                        payload,
                        headers,
                        row_id,
                        lease,
                        search_principal=incoming.get("authorization"),
                        reason=AttemptReason.FALLBACK
                        if index
                        else (AttemptReason.CONTINUATION if changed else AttemptReason.INITIAL),
                        fallback_origin=logical_source.selector if changed else None,
                        metadata=self.metadata(route.channel.id, route.model_id),
                        reserve=lambda prepared, channel, model, subscription: self._reservation(
                            tracker, lease, prepared, channel, model, subscription
                        ),
                        prepare_body=prepare_body,
                    ) as current:
                        response = current
                        retry_error = None
                        if self.subagents:
                            self.subagents.note_failure(
                                headers, route.channel, route.model_id, current.status
                            )
                        trigger = failure_trigger(
                            current.status, error_code=upstream_error_code(current.content or b"")
                        )
                        if index:
                            self.fallback.record(
                                plan.source, previous, target, trigger or "recovered"
                            )
                        if current.stream is not None or not trigger or index == len(targets) - 1:
                            delivered_to_caller = True
                            yield current
                            state = current.state
                            outcome = Outcome(
                                status_code=state.logical_status or current.status,
                                terminal_status=TerminalStatus(state.terminal)
                                if state.terminal
                                else TerminalStatus.INCOMPLETE
                                if current.stream is not None
                                else None,
                                error_code=state.error_code,
                                interrupted=current.stream is not None
                                and (
                                    state.terminal is None
                                    or state.error_code
                                    in {
                                        "adapter_eof",
                                        "upstream_interrupted",
                                        "upstream_timeout",
                                        "body_overflow",
                                    }
                                ),
                            )
                            return
                except SpendDenied, WorkflowDenied, PacingDenied:
                    raise
                except (httpx.HTTPError, UpstreamBodyOverflow) as exc:
                    if delivered_to_caller:
                        raise
                    retry_error = exc
                    response = None
                    trigger = (
                        failure_trigger(exc.upstream_error_status)
                        if isinstance(exc, UpstreamBodyOverflow)
                        else failure_trigger(exception=exc)
                    )
                    if not trigger or index == len(targets) - 1:
                        raise
                except ValueError, RuntimeError:
                    if delivered_to_caller:
                        raise
                    sent_after = telemetry.send_count(request)
                    if not index or sent_after != sent_before:
                        raise
                    self.fallback.record(
                        plan.source,
                        previous,
                        target,
                        cast(str, trigger),
                        outcome="skipped",
                        reason="The backup cannot prepare this request.",
                    )
                    continue
                previous = target
            if response is None:
                if retry_error is not None:
                    raise retry_error
                raise RuntimeError("No upstream response was produced.")
            yield response
            outcome = Outcome(
                status_code=response.state.logical_status or response.status,
                terminal_status=TerminalStatus.FAILED,
                error_code=response.state.error_code,
            )
        except asyncio.CancelledError:
            disconnected = request.cancellation.is_set()
            outcome = Outcome(
                status_code=499 if disconnected else 503,
                terminal_status=TerminalStatus.CANCELLED,
                error_code=None if disconnected else "request_cancelled",
                cancelled=disconnected,
            )
            raise
        except httpx.TimeoutException as exc:
            outcome = Outcome(
                status_code=504,
                terminal_status=TerminalStatus.FAILED,
                error_code="upstream_connect_timeout"
                if isinstance(exc, httpx.ConnectTimeout)
                else "upstream_timeout",
            )
            raise
        except httpx.HTTPError as exc:
            outcome = Outcome(
                status_code=502,
                terminal_status=TerminalStatus.FAILED,
                error_code="upstream_connect_error"
                if isinstance(exc, httpx.ConnectError)
                else "upstream_interrupted"
                if delivered_to_caller
                else "upstream_transport_error",
            )
            raise
        except (ValueError, RuntimeError) as exc:
            outcome = Outcome(
                status_code=getattr(exc, "status", 400),
                terminal_status=TerminalStatus.FAILED,
                error_code=getattr(
                    exc, "code", "invalid_request" if isinstance(exc, ValueError) else None
                ),
            )
            raise
        finally:
            if (
                request.cancellation.is_set()
                and outcome.terminal_status == TerminalStatus.INCOMPLETE
            ):
                outcome = Outcome(
                    status_code=499, terminal_status=TerminalStatus.CANCELLED, cancelled=True
                )
            attempt = getattr(response, "attempt", None)
            attempt_context = attempt.context if attempt is not None else None

            async def finalize():
                try:
                    await telemetry.end_request(
                        request,
                        row_id,
                        outcome,
                        usage=attempt_context.usage if attempt_context else None,
                        attempt_id=attempt_context.attempt_id if attempt_context else None,
                        duration_ms=(time.monotonic() - started) * 1000,
                    )
                finally:
                    if tracker:
                        tracker.close()
                    if lease and borrowed_lease is None:
                        lease.release()

            await finish_cleanup(finalize())
