"""API routing, native wire forwarding and explicit cross-protocol bridges."""

import asyncio
import codecs
import copy
import json
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from urllib.parse import urlsplit

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .agent_policy import apply_effort_policy
from .anthropic_history import sanitize_native_ids
from .anthropic_images import AnthropicImages
from .anthropic_transport import (
    prepare_anthropic_body,
    restore_anthropic_payload,
)
from .chatgpt_transport import (
    discovery_version,
    subscription_body,
    subscription_headers,
)
from .compaction import (
    COMPACT_PROMPT,
    remote_checkpoint,
    replay_projection,
    routing_override,
    summary_item,
    summary_text,
    user_item,
    v1_output,
)
from .config import ConfigStore, Provider
from .fallback import (
    FallbackTarget,
    SimpleFallback,
    failure_trigger,
    prepare_retry,
    upstream_error_code,
)
from .native_tools import NativeToolAdapter, canonical_openai_forward
from .protocol_streams import StreamEncoder, chat_chunks, events, sse
from .protocols import response_from_chat, response_to_chat, response_tools
from .provider_registry import REGISTRY
from .request_attempt import RequestAttempt, retry_request
from .search_auxiliary import SearchService
from .search_gateway import maybe_search
from .spend_ledger import SpendDenied
from .subscription_channels import SubscriptionChannels
from .subscription_history import (
    ensure_history_issuer,
    scrub_subscription_replay,
    subscription_issuer,
)
from .subscription_turn_state import TURN_STATE_HEADER, TurnStateStore
from .upstream_request import UpstreamPreparation
from .usage_store import UsageStore
from .workflow_budget import WorkflowBudget

WIRES = {
    "openai-chat": "chat/completions",
    "openai-responses": "responses",
    "anthropic": "messages",
}


class Gateway:
    def __init__(self, store: ConfigStore):
        self.store = store
        self.spend_ledger = None
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=10), follow_redirects=False
        )
        self.model_rows = None
        self.workflow_budget = WorkflowBudget(store.load)
        self.subagents = None
        self.debug = None
        self.auxiliary = None
        self.search_service = SearchService()
        self.fallback = SimpleFallback(store)
        self.channels = SubscriptionChannels(store.directory, validate=self.validate_subscription)
        self.anthropic_images = AnthropicImages()
        self.history_path = store.directory / "responses.sqlite3"
        self.reasoning = {}
        self.transient_responses = {}
        self.transient_fallback_routes = {}
        self.turn_states = TurnStateStore()
        self.usage_store = UsageStore(
            store.directory, max_bytes=getattr(store.load(), "usageLedgerMaxBytes", None)
        )
        self.registry = REGISTRY

    @asynccontextmanager
    async def lifespan(self, app):
        try:
            yield
        finally:
            await self.channels.close()
            await self.client.aclose()

    @staticmethod
    def is_chatgpt(provider):
        return provider.authMode == "oauth" and provider.id == "openai"

    def auth_for(self, provider):
        return self.channels.for_provider(provider)

    async def credential_for(self, provider):
        return await self.channels.credential(provider)

    async def mark_oauth_rejected(self, provider, credential):
        await self.channels.reject(provider, credential)

    issuer_for = staticmethod(SubscriptionChannels.issuer)

    def oauth_headers(self, provider, credential, incoming=None):
        return self.preparation().oauth_headers(provider, credential, incoming)

    def preparation(self):
        return UpstreamPreparation(self.store, self.registry, self.reasoning)

    def headers(self, provider, incoming=None):
        return self.preparation().headers(provider, incoming)

    def url(self, provider, protocol):
        return self.preparation().url(provider, protocol)

    def wire(self, provider, model, inbound="chat/completions"):
        return self.preparation().wire(provider, model, inbound)

    async def models(self, provider: Provider) -> list[dict]:
        params = {}
        credential = None
        if provider.authMode == "oauth":
            credential = await self.credential_for(provider)
            if self.is_chatgpt(provider):
                params["client_version"] = discovery_version(provider)
        rows = []
        cursors = set()
        while True:
            response = await self.client.get(
                self.url(provider, "models"),
                params=params,
                headers=self.oauth_headers(provider, credential)
                if credential
                else self.headers(provider),
            )
            if credential and response.status_code == 401:
                credential = await self.auth_for(provider).token(
                    rejected_access=credential["access"],
                    expected_account_id=credential["accountId"],
                )
                response = await self.client.get(
                    self.url(provider, "models"),
                    params=params,
                    headers=self.oauth_headers(provider, credential),
                )
            if response.status_code != 200:
                if response.status_code == 401:
                    await self.mark_oauth_rejected(provider, credential)
                raise ValueError(
                    f"Model discovery failed with upstream HTTP {response.status_code}."
                )
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("error"):
                raise ValueError("Upstream did not return a model list.")
            data = payload.get("models" if self.is_chatgpt(provider) else "data")
            if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
                raise ValueError("Upstream did not return a model list.")
            if self.is_chatgpt(provider):
                data = [
                    {**row, "id": row.get("slug"), "name": row.get("display_name", row.get("slug"))}
                    for row in data
                    if row.get("supported_in_api") is not False and row.get("visibility") != "hide"
                ]
            if any(not isinstance(row.get("id"), str) or not row["id"].strip() for row in data):
                raise ValueError("Upstream did not return a model list.")
            rows.extend(data)
            if not payload.get("has_more") and not payload.get("nextPageToken"):
                return list({row["id"]: row for row in rows}.values())
            # Anthropic's complete directory uses last_id -> after_id. Other
            # pagination dialects must be understood before retiring old models.
            cursor = payload.get("last_id")
            if (
                provider.adapter != "anthropic"
                or payload.get("nextPageToken")
                or not isinstance(cursor, str)
                or not cursor
                or cursor in cursors
                or not data
            ):
                raise ValueError("Upstream model pagination is unsupported or did not advance.")
            cursors.add(cursor)
            params["after_id"] = cursor

    async def check(
        self, provider: Provider, model: str, *, prompt="Reply with OK only.", max_tokens=32
    ) -> dict:
        protocol = self.wire(provider, model)
        body = self.preparation().convert(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "stream": False,
            },
            ("chat/completions", protocol),
            provider,
            cache_retention=getattr(self.store.load(), "cacheRetention", None),
        )
        if urlsplit(provider.baseUrl).hostname == "api.deepseek.com":
            body["thinking"] = {"type": "disabled"}
        if self.is_chatgpt(provider):
            credential = await self.channels["openai"].token()
            body = subscription_body(body, credential=credential)
            for attempt in range(2):
                prepared = self.preparation().prepare(
                    provider,
                    protocol,
                    body,
                    subscription_headers(provider, credential),
                    response_mode="event_stream",
                )
                async with self.client.stream(
                    "POST",
                    prepared.address,
                    headers=prepared.headers,
                    json=prepared.body,
                ) as response:
                    if response.status_code == 401 and attempt == 0:
                        await response.aclose()
                        credential = await self.channels["openai"].token(
                            rejected_access=credential["access"],
                            expected_account_id=credential["accountId"],
                        )
                        continue
                    if response.status_code != 200:
                        raise ValueError(
                            f"Connection check failed with upstream HTTP {response.status_code}."
                        )
                    final = None
                    async for event in events(response):
                        if event.get("type") == "response.completed":
                            final = event.get("response")
                    if final is None:
                        raise RuntimeError("ChatGPT stream ended without a completed response.")
                    break
            response = httpx.Response(200, json=final)
        elif provider.authMode == "oauth":
            credential = await self.credential_for(provider)
            body, name_map = prepare_anthropic_body(body)
            for attempt in range(2):
                prepared = self.preparation().prepare(
                    provider,
                    protocol,
                    body,
                    self.oauth_headers(provider, credential),
                    response_mode="auto",
                )
                response = await self.client.post(
                    prepared.address,
                    headers=prepared.headers,
                    json=prepared.body,
                )
                if response.status_code != 401 or attempt:
                    break
                credential = await self.auth_for(provider).token(
                    rejected_access=credential["access"],
                    expected_account_id=credential["accountId"],
                )
            if response.status_code == 200:
                response = httpx.Response(
                    200, json=restore_anthropic_payload(response.json(), name_map)
                )
        else:
            prepared = self.preparation().prepare(
                provider, protocol, body, self.headers(provider), response_mode="auto"
            )
            response = await self.client.post(
                prepared.address, headers=prepared.headers, json=prepared.body
            )
        if response.status_code != 200:
            if response.status_code == 401 and provider.authMode == "oauth":
                await self.mark_oauth_rejected(provider, credential)
            raise ValueError(f"Connection check failed with upstream HTTP {response.status_code}.")
        try:
            result = response_to_chat(response.json(), protocol)
            reply = result["choices"][0]["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ValueError("Upstream returned an invalid completion response.") from exc
        return {
            "ok": True,
            "model": model,
            "upstreamModel": result.get("model", model),
            "reply": reply,
            "usage": result.get("usage", {}),
        }

    async def validate_subscription(self, credential):
        """Verify a new grant before replacing the working account, as the original login does."""
        provider = self.store.provider("openai")
        flow = self.channels["openai"].flow
        if flow:
            flow["stage"] = "models"
        async with asyncio.timeout(60):
            roster = await self.client.get(
                self.url(provider, "models"),
                params={"client_version": discovery_version(provider)},
                headers=subscription_headers(provider, credential),
            )
            if roster.status_code != 200:
                raise ValueError(
                    f"ChatGPT account model access could not be verified (HTTP {roster.status_code}). The previous account was kept."
                )
            payload = roster.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
                raise TypeError("ChatGPT account returned an invalid model directory.")
            rows = payload["models"]
            candidates = [
                row["slug"]
                for row in rows
                if isinstance(row, dict)
                and isinstance(row.get("slug"), str)
                and row.get("supported_in_api") is not False
                and row.get("visibility") != "hide"
            ]
            if not candidates:
                raise ValueError(
                    "ChatGPT login succeeded, but the account returned no usable models. Check its subscription access."
                )
            preferred = [m for m in candidates if "luna" in m or "mini" in m]
            candidates = list(dict.fromkeys(preferred + candidates))[:3]
            for model in candidates:
                if flow:
                    flow["stage"] = "validation"
                body = subscription_body(
                    {
                        "model": model,
                        "input": "Reply with OK only.",
                        "instructions": "Reply briefly.",
                    },
                    credential=credential,
                )
                async with self.client.stream(
                    "POST",
                    self.url(provider, "responses"),
                    headers=subscription_headers(provider, credential),
                    json=body,
                ) as response:
                    if response.status_code in {400, 404}:
                        continue
                    if response.status_code == 429:
                        raise ValueError(
                            "ChatGPT login succeeded, but the account usage limit prevented validation. Retry after its limit resets."
                        )
                    if response.status_code != 200:
                        raise ValueError(
                            f"ChatGPT account validation failed (HTTP {response.status_code}). The previous account was kept."
                        )
                    async for event in events(response):
                        if event.get("type") == "response.completed":
                            credential["validatedAt"] = time.time()
                            return
                        if event.get("type") in {"error", "response.failed", "response.incomplete"}:
                            raise ValueError(
                                "ChatGPT account validation did not complete. The previous account was kept."
                            )
                    raise ValueError("ChatGPT account validation stream ended before completion.")
            raise ValueError(
                "ChatGPT login succeeded, but available models refused the validation request. Check account provisioning."
            )

    @contextmanager
    def history(self):
        db = sqlite3.connect(self.history_path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS responses (id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL, input TEXT NOT NULL)"
        )
        ensure_history_issuer(db)
        db.execute(
            "CREATE TABLE IF NOT EXISTS fallback_responses (id TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL)"
        )
        try:
            with db:
                yield db
        finally:
            db.close()

    def expand_history(self, body, provider, model, issuer=None):
        previous = body.get("previous_response_id")
        if not previous:
            return body, False
        self.prune_responses()
        cached = self.transient_responses.get(previous)
        row = cached[1] if cached else None
        if row is None:
            with self.history() as db:
                row = db.execute(
                    "SELECT provider, model, input, issuer FROM responses WHERE id = ?", (previous,)
                ).fetchone()
        if row is None:
            raise ValueError(
                "The previous response is not available in this gateway's translated response history."
            )
        if row[:2] != (provider.id, model):
            raise ValueError("The previous response belongs to a different provider or model.")
        if issuer is None and len(row) > 3 and row[3] is not None:
            raise ValueError(
                "The previous response belongs to a subscription account; start a new conversation for this API channel."
            )
        items = body.get("input", [])
        if isinstance(items, str):
            items = [{"role": "user", "content": items}]
        expanded = {**body, "input": json.loads(row[2]) + items}
        changed = issuer is not None and (len(row) < 4 or row[3] != issuer)
        if changed and provider.adapter == "anthropic":
            # Messages thinking signatures are not ChatGPT account ciphertext. Keep
            # readable canonical turns and native signatures, but never guess file ownership.
            pending = list(expanded["input"])
            while pending:
                item = pending.pop()
                if isinstance(item, dict):
                    if (
                        item.get("file_id")
                        or item.get("file_ids")
                        or item.get("type") == "item_reference"
                    ):
                        raise ValueError(
                            "This continuation contains files or references from a different Claude account."
                        )
                    pending.extend(item.values())
                elif isinstance(item, list):
                    pending.extend(item)
            return expanded, True
        return (scrub_subscription_replay(expanded) if changed else expanded), changed

    def remember(
        self, provider, model, body, result, chat, issuer=None, scope=None, fallback_origin=None
    ):
        message = chat["choices"][0]["message"]
        reasoning = message.get("reasoning_content")
        if reasoning:
            for call in message.get("tool_calls", []):
                self.reasoning[(provider.id, model, call["id"])] = reasoning
        self.save_response(provider, model, body, result, issuer, scope, fallback_origin)
        if result.get("object") != "response":
            self.observe_subscription_turn(provider, issuer, scope)

    def observe_subscription_turn(self, provider, issuer, scope):
        if self.is_chatgpt(provider) and issuer and scope is not None:
            try:
                active = self.channels["openai"].load()
            except ValueError, RuntimeError, OSError:
                return
            if active and subscription_issuer(active["accountId"]) == issuer:
                self.turn_states.observe_issuer(issuer, provider.id, scope)

    def save_response(
        self, provider, model, body, result, issuer=None, scope=None, fallback_origin=None
    ):
        if result.get("object") != "response" or result.get("status") == "failed":
            return
        if (
            result.get("status") == "incomplete"
            and (result.get("incomplete_details") or {}).get("reason") != "max_output_tokens"
        ):
            return
        self.bind_fallback_response(provider, model, body, result, fallback_origin)
        items = body.get("input", [])
        if isinstance(items, str):
            items = [{"role": "user", "content": items}]
        if body.get("store") is False:
            self.prune_responses()
            self.transient_responses[result["id"]] = (
                time.monotonic(),
                (
                    provider.id,
                    model,
                    json.dumps(items + result["output"], ensure_ascii=False),
                    issuer,
                ),
            )
            self.observe_subscription_turn(provider, issuer, scope)
            return
        with self.history() as db:
            db.execute(
                "INSERT OR REPLACE INTO responses (id, provider, model, input, issuer) VALUES (?, ?, ?, ?, ?)",
                (
                    result["id"],
                    provider.id,
                    model,
                    json.dumps(items + result["output"], ensure_ascii=False),
                    issuer,
                ),
            )
        self.observe_subscription_turn(provider, issuer, scope)

    def prune_responses(self):
        cutoff = time.monotonic() - 24 * 60 * 60
        for key, (created, _) in list(self.transient_responses.items()):
            if created < cutoff:
                del self.transient_responses[key]
        for key, (created, _) in list(self.transient_fallback_routes.items()):
            if created < cutoff:
                del self.transient_fallback_routes[key]

    def bind_fallback_response(self, provider, model, body, result, origin):
        if not origin or result.get("object") != "response" or result.get("status") == "failed":
            return
        identity = result.get("id")
        if not isinstance(identity, str) or not identity:
            return
        route = (origin.selector, f"{provider.id}/{model}")
        if body.get("store") is False:
            self.transient_fallback_routes[identity] = (time.monotonic(), route)
        else:
            with self.history() as db:
                db.execute(
                    "INSERT OR REPLACE INTO fallback_responses VALUES (?, ?, ?)", (identity, *route)
                )

    def fallback_response_route(self, identity):
        if not isinstance(identity, str) or not identity:
            return None
        self.prune_responses()
        if identity in self.transient_fallback_routes:
            return self.transient_fallback_routes[identity][1]
        with self.history() as db:
            return db.execute(
                "SELECT source, target FROM fallback_responses WHERE id = ?", (identity,)
            ).fetchone()

    def accounting(self, request):
        return RequestAttempt(
            request,
            usage_store=self.usage_store,
            spend_ledger=self.spend_ledger,
            workflow_budget=self.workflow_budget,
            auxiliary=self.auxiliary,
            debug=self.debug,
        )

    async def dispatch_search_iteration(self, request, payload, *, continuing):
        if continuing:
            self.accounting(request).next_attempt(200, debug=True)
        payload["stream"] = True
        attempt = retry_request(request, payload)
        attempt.scope["ocx_search_bypass"] = True
        return await self._forward(attempt, "responses")

    async def dispatch_search_helper(self, request, protocol, payload):
        return await self.forward(self.accounting(request).search_helper(payload), protocol)

    def record_search_error(self, request, code, *, terminal=None):
        self.accounting(request).error(code, terminal=terminal)

    async def dispatch_compaction(self, request, protocol, original):
        compact = protocol == "responses/compact"
        remote = remote_checkpoint(original)
        body = copy.deepcopy(original)
        source_selector = routing_override(
            body, request.headers, self.store.load(), compact=compact
        )
        if not compact and not remote and source_selector is None:
            return None
        source, source_model = self.store.route(original.get("model"))
        logical_source = FallbackTarget(source.id, source_model)
        binding = self.fallback_response_route(original.get("previous_response_id"))
        if binding and binding[0] == logical_source.selector:
            source, source_model = self.store.route(binding[1])
            if source_selector is None:
                body["model"] = binding[1]
        target, target_model = self.store.route(body.get("model"))
        cross_identity = source.id != target.id
        if body.get("previous_response_id") and source_model != target_model:
            credential = await self.credential_for(source)
            issuer = self.issuer_for(source, credential)
            body, _ = self.expand_history(body, source, source_model, issuer)
            body.pop("previous_response_id", None)
        native_wire = self.wire(target, target_model, "responses") == "responses"
        canonical = self.is_chatgpt(target) or canonical_openai_forward(target)
        native = (
            native_wire
            and not cross_identity
            and (
                canonical
                or (
                    getattr(target, "supportsNativeCompactEndpoint", False)
                    or (
                        target.id == "openai-apikey"
                        and target.baseUrl.rstrip("/") == "https://api.openai.com/v1"
                    )
                    if compact
                    else getattr(target, "supportsNativeCompactionTrigger", False)
                )
            )
        )
        if not compact and not remote:
            # Local checkpoints use the normal assistant-summary contract.
            if cross_identity or source_model != target_model:
                credential = await self.credential_for(source)
                issuer = self.issuer_for(source, credential)
                expanded, _ = self.expand_history(body, source, source_model, issuer)
                body = prepare_retry(
                    body,
                    FallbackTarget(source.id, source_model),
                    FallbackTarget(target.id, target_model),
                    expanded_body=expanded,
                )
            return await self._forward(
                retry_request(request, body, change_target=cross_identity), "responses"
            )
        if native:
            response = await self._forward(retry_request(request, body), protocol)
            if not compact or response.status_code != 404:
                return response
            # Native v1 404 is a capability miss; preserve its real send/usage then summarize.
            self.accounting(request).next_attempt(response.status_code, debug=True)
        try:
            credential = await self.credential_for(source)
            source_issuer = self.issuer_for(source, credential)
            expanded, _ = self.expand_history(body, source, source_model, source_issuer)
            original_items = expanded.get("input", [])
            if isinstance(original_items, str):
                original_items = [user_item(original_items)]
            summary_body = replay_projection(expanded, opaque_note=True, frame=False)
            if cross_identity:
                summary_body = prepare_retry(
                    summary_body,
                    FallbackTarget(source.id, source_model),
                    FallbackTarget(target.id, target_model),
                    expanded_body=summary_body,
                )
            summary_body.pop("previous_response_id", None)
            items = summary_body.get("input", [])
            if isinstance(items, str):
                items = [user_item(items)]
            summary_body["input"] = [
                item
                for item in items
                if not isinstance(item, dict) or item.get("type") != "compaction_trigger"
            ] + [user_item(COMPACT_PROMPT)]
            for key in (
                "tools",
                "tool_choice",
                "parallel_tool_calls",
                "text",
                "response_format",
                "conversation",
                "context_management",
            ):
                summary_body.pop(key, None)
            summary_body["stream"] = False
            summary_body["store"] = False
            response = await self._forward(
                retry_request(request, summary_body, change_target=cross_identity), "responses"
            )
            if response.status_code >= 400:
                return response
            result = json.loads(response.body)
            summary = summary_text(result)
            await self.accounting(request).update_usage(streaming=bool(original.get("stream")))
            output = v1_output(original_items, summary) if compact else [summary_item(summary)]
            result = {
                "id": ("cmp_" if compact else "resp_ocxcmp_") + uuid.uuid4().hex,
                "object": "response.compaction" if compact else "response",
                "created_at": int(time.time()),
                "status": "completed",
                "model": target_model,
                "output": output,
                "usage": result.get("usage"),
            }
            if not compact:
                self.save_response(
                    source,
                    source_model,
                    {"input": [], "store": original.get("store", True)},
                    result,
                    source_issuer,
                    request.headers,
                    logical_source
                    if source.id != logical_source.provider or source_model != logical_source.model
                    else None,
                )
            if not compact and original.get("stream"):

                async def compact_events():
                    initial = {**result, "status": "in_progress", "output": []}
                    for sequence, event in enumerate(
                        [
                            {"type": "response.created", "response": initial},
                            {
                                "type": "response.output_item.added",
                                "output_index": 0,
                                "item": output[0],
                            },
                            {
                                "type": "response.output_item.done",
                                "output_index": 0,
                                "item": output[0],
                            },
                            {"type": "response.completed", "response": result},
                        ]
                    ):
                        event["sequence_number"] = sequence
                        yield sse(event, event["type"])

                return StreamingResponse(compact_events(), media_type="text/event-stream")
            return JSONResponse(result)
        except SpendDenied:
            raise
        except (ValueError, TypeError, RuntimeError) as exc:
            self.accounting(request).error("compaction_failed")
            return JSONResponse({"error": {"code": "compaction_failed", "message": str(exc)}}, 502)

    async def dispatch_with_fallback(self, request, protocol):
        try:
            original = copy.deepcopy(await request.json())
            if not isinstance(original, dict):
                return await self._forward(request, protocol)
            request.state.requested_model = original.get("model")
            if protocol in {"responses", "responses/compact"}:
                try:
                    compact_response = await self.dispatch_compaction(request, protocol, original)
                except SpendDenied:
                    raise
                except (ValueError, TypeError, RuntimeError) as exc:
                    return JSONResponse(
                        {"error": {"code": "compaction_route_error", "message": str(exc)}}, 400
                    )
                if compact_response is not None:
                    return compact_response
            if protocol == "responses" and self.subagents:
                await self.subagents.prepare_request(original)
            provider, model = self.store.route(original.get("model"))
            logical_source = FallbackTarget(provider.id, model)
            binding = (
                self.fallback_response_route(original.get("previous_response_id"))
                if protocol == "responses"
                else None
            )
            if binding and binding[0] == logical_source.selector:
                # A continuation belongs to the provider that actually returned its ID.
                self.store.provider(binding[1].split("/", 1)[0])
                original["model"] = binding[1]
            elif self.subagents and protocol == "responses":
                await self.subagents.fallback.select(original, request.headers)
            provider, model = self.store.route(original.get("model"))
            selected = FallbackTarget(provider.id, model)
            request.state.fallback_origin = (
                logical_source
                if selected != logical_source or (binding and logical_source.selector in binding)
                else None
            )
            plan = self.fallback.plan(
                logical_source.provider,
                logical_source.model,
                requested=request.state.requested_model,
            )
        except SpendDenied:
            raise
        except ValueError, TypeError, KeyError:
            return await self._forward(request, protocol)
        attempt = retry_request(request, original, change_target=selected != logical_source)
        response = await self._forward(attempt, protocol)
        if protocol not in {"responses", "chat/completions", "messages"}:
            return response
        previous = selected
        for target in plan.candidates:
            if target == selected:
                continue
            trigger = getattr(request.state, "fallback_trigger", None)
            if not trigger or isinstance(response, StreamingResponse):
                break
            if await request.is_disconnected():
                break
            try:
                expanded = None
                if original.get("previous_response_id"):
                    # Resolve against the original serving route, including its issuer.
                    credential = await self.credential_for(provider)
                    issuer = self.issuer_for(provider, credential)
                    expanded, _ = self.expand_history(original, provider, model, issuer)
                retry = prepare_retry(original, selected, target, expanded_body=expanded)
            except (ValueError, RuntimeError) as exc:
                request.state.fallback_refusal = str(exc)
                self.fallback.record(
                    plan.source, previous, target, trigger, outcome="refused", reason=str(exc)
                )
                break
            accounting = self.accounting(request)
            retained_state = accounting.snapshot()
            retained_response = response
            self.accounting(request).next_attempt(response.status_code)
            request.state.fallback_origin = logical_source
            if self.debug:
                self.debug.event(
                    "provider",
                    "fallback",
                    source=previous.selector,
                    target=target.selector,
                    trigger=trigger,
                )
            attempt = retry_request(request, retry, change_target=True)
            response = await self._forward(attempt, protocol)
            if not accounting.sent_since(retained_state) and response.status_code >= 400:
                # A target-local preparation failure is not a new upstream verdict.
                # Keep the last real failure and continue through the ordered backups.
                accounting.restore(retained_state)
                response = retained_response
                self.fallback.record(
                    plan.source,
                    previous,
                    target,
                    trigger,
                    outcome="skipped",
                    reason="The backup cannot prepare this request.",
                )
                await accounting.restore_usage_metadata()
                continue
            accounting.debug_usage(retained_response.status_code, state=retained_state)
            self.fallback.record(plan.source, previous, target, trigger)
            previous = target
        return response

    def chat_policy(self, body, provider, model, *, translated):
        return self.preparation().chat_policy(body, provider, model, translated=translated)

    async def forward(self, request: Request, protocol: str):
        async def dispatch():
            if request.scope.get("ocx_search_helper"):
                return await self._forward(request, protocol)
            return await self.dispatch_with_fallback(request, protocol)

        return await self.accounting(request).run(dispatch)

    async def _forward(self, request: Request, protocol: str):
        accounting = self.accounting(request)
        request.state.fallback_trigger = None
        request.state.upstream_status = None
        request.state.upstream_content_type = None
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("Expected a JSON object.")
            if body.get("model") is not None and not isinstance(body["model"], str):
                raise ValueError("Model must be a string.")
            provider, model = self.store.route(body.get("model"))
            if (
                self.model_rows
                and provider.liveModels
                and provider.id not in self.store.discovered_models
            ):
                await self.model_rows()
                provider, model = self.store.route(body.get("model"))
            native = self.wire(provider, model, protocol.split("/compact")[0])
            config = self.store.load()
            if any(
                getattr(config, key, None)
                for key in ("effortCap", "subagentEffortCap", "modelPinnedEfforts")
            ) or any(
                getattr(provider, key, None)
                for key in ("pinnedReasoningEffort", "modelPinnedReasoningEfforts")
            ):
                metadata = None
                if self.model_rows:
                    metadata = next(
                        (
                            row
                            for row in await self.model_rows()
                            if row["provider"] == provider.id and row["id"] == model
                        ),
                        None,
                    )
                request.state.effort_policy = apply_effort_policy(
                    body, request.headers, config, provider, model, protocol, metadata
                )
            await accounting.route(
                provider,
                model,
                protocol,
                native,
                body,
                event=lambda: (
                    self.debug.event(
                        "provider", "route", provider=provider.id, model=model, protocol=protocol
                    )
                    if self.debug
                    else None
                ),
            )
            body["model"] = model
            special = protocol in {"responses/compact", "messages/count_tokens"}
            if special and not protocol.startswith(native + "/"):
                raise ValueError(f"{protocol} requires a provider using its native protocol.")
            translated = not special and protocol != native
            stateless = protocol == "responses" and (
                provider.authMode == "oauth"
                or getattr(provider, "statelessResponses", False)
                or request.state.fallback_origin is not None
                or str(body.get("previous_response_id", "")).startswith(
                    ("resp_ocxcmp_", "resp_ocxsearch_")
                )
            )
            try:
                subscription = await self.credential_for(provider)
            except ValueError, RuntimeError:
                return JSONResponse(
                    {
                        "error": {
                            "message": "Sign in to this gateway's subscription channel first.",
                            "code": "authentication_required",
                        }
                    },
                    401,
                )
            chatgpt = self.is_chatgpt(provider)
            claude = bool(subscription) and provider.adapter == "anthropic"
            if claude and native != "messages":
                raise ValueError("Claude subscription models use the Messages protocol.")
            issuer = self.issuer_for(provider, subscription)
            account_changed = False
            if protocol == "responses" and (translated or stateless):
                body, account_changed = self.expand_history(body, provider, model, issuer)
            if (
                chatgpt
                and issuer
                and self.turn_states.issuer_changed(issuer, provider.id, request.headers)
            ):
                if isinstance(body.get("input"), str):
                    body["input"] = [{"role": "user", "content": body["input"]}]
                body.setdefault("input", [])
                body = scrub_subscription_replay(body)
                account_changed = True
            if self.auxiliary:
                await self.auxiliary.prepare(
                    body,
                    protocol,
                    provider,
                    model,
                    request.headers,
                    workflow_lease=request.state.workflow_lease,
                )
            if protocol == "responses":
                body, search_response = await maybe_search(
                    self,
                    request,
                    body,
                    provider,
                    model,
                    issuer,
                    native=chatgpt or canonical_openai_forward(provider),
                    expanded=translated or stateless,
                )
                if search_response is not None:
                    return search_response
            tool_names = response_tools(body)[1] if protocol == "responses" and translated else {}
            custom_tools = {name for name, identity in tool_names.items() if identity["custom"]}
            generation_body = (
                replay_projection(
                    body,
                    opaque_note=translated,
                    frame=not (chatgpt or canonical_openai_forward(provider)),
                )
                if protocol == "responses"
                else body
            )
            outbound = (
                self.preparation().convert(
                    generation_body,
                    (protocol, native),
                    provider,
                    cache_retention=getattr(config, "cacheRetention", None),
                    raw_body=generation_body,
                )
                if translated
                else copy.deepcopy(generation_body)
            )
            if native == "messages" and not translated:
                sanitize_native_ids(outbound.get("messages", []))
            progress_updates = (
                native == "messages" and outbound.get("thinking", {}).get("display") == "updates"
            )
            reasoning = body.get("reasoning") or {}
            summary = reasoning.get("summary", reasoning.get("generate_summary"))
            hide_thinking = (
                protocol == "responses"
                and not progress_updates
                and (
                    summary == "none"
                    or (
                        summary is None
                        and reasoning.get("effort")
                        not in {"minimal", "low", "medium", "high", "xhigh", "max"}
                    )
                )
            )
            native_tools = None
            if (
                protocol == "responses"
                and not translated
                and provider.authMode != "oauth"
                and not canonical_openai_forward(provider)
            ):
                native_tools = NativeToolAdapter(
                    body, getattr(provider, "supportsResponsesCustomTools", None)
                )
                outbound = native_tools.request(outbound)
            if stateless:
                outbound.pop("previous_response_id", None)
            await self.preparation().normalize_images(
                outbound, self.anthropic_images, native=native, translated=translated
            )
            if native == "chat/completions":
                outbound = self.chat_policy(outbound, provider, model, translated=translated)
                if outbound.get("stream"):
                    options = outbound.get("stream_options")
                    outbound["stream_options"] = {
                        **(options if isinstance(options, dict) else {}),
                        "include_usage": True,
                    }
            effective_provider = provider.model_copy(
                update={"adapter": next(k for k, v in WIRES.items() if v == native)}
            )
            credential = (
                subscription["access"]
                if subscription
                else (self.store.credential(provider) if provider.authMode != "local" else "")
            )
            outbound, name_map = self.preparation().channel_body(
                outbound, chatgpt=chatgpt, claude=claude, compact=special, credential=subscription
            )
            incoming_headers = dict(request.headers)
            if account_changed:
                incoming_headers.pop("x-codex-turn-state", None)
            if chatgpt and issuer:
                raw_state = incoming_headers.pop(TURN_STATE_HEADER, None)
                resolved = self.turn_states.resolve(
                    raw_state, issuer, provider.id, model, incoming_headers
                )
                if resolved:
                    incoming_headers[TURN_STATE_HEADER] = resolved
                metadata = outbound.get("client_metadata")
                if isinstance(metadata, dict) and TURN_STATE_HEADER in metadata:
                    raw_state = metadata.pop(TURN_STATE_HEADER)
                    resolved = (
                        None
                        if account_changed
                        else self.turn_states.resolve(
                            raw_state, issuer, provider.id, model, incoming_headers
                        )
                    )
                    if resolved:
                        metadata[TURN_STATE_HEADER] = resolved
            prepared = self.preparation().prepare(
                effective_provider,
                protocol if special else native,
                outbound,
                self.oauth_headers(effective_provider, subscription, incoming_headers)
                if subscription
                else self.headers(effective_provider, request.headers),
                response_mode="event_stream" if chatgpt and not special else "auto",
            )
            upstream_request = self.client.build_request(
                "POST", prepared.address, headers=prepared.headers, json=prepared.body
            )
            if native == "messages" and outbound.get("thinking", {}).get("display") == "updates":
                betas = upstream_request.headers.get("anthropic-beta", "").split(",")
                beta = "thinking-display-updates-2026-08-18"
                upstream_request.headers["anthropic-beta"] = ",".join(
                    dict.fromkeys([*(value for value in betas if value), beta])
                )
            spend = accounting.prepare_spend(
                outbound, native, provider, model, subscription, credential
            )
            accounting.start_send(spend)
            upstream = await self.client.send(upstream_request, stream=True)
            accounting.receive_response()
            if subscription and upstream.status_code == 401:
                # Capture this physical response before replacing it with an OAuth retry.
                accounting.count_oauth_rejection(upstream, native)
                await upstream.aread()
                accounting.finish_oauth_rejection(spend)
                await upstream.aclose()
                try:
                    subscription = await self.auth_for(provider).token(
                        rejected_access=credential,
                        expected_account_id=subscription["accountId"],
                    )
                except RuntimeError, ValueError:
                    accounting.discard_response_receipt()
                    request.state.upstream_status = 401
                    request.state.fallback_trigger = failure_trigger(401)
                    return JSONResponse(
                        {
                            "error": {
                                "message": "Subscription authorization needs renewal.",
                                "code": "invalid_api_key",
                            }
                        },
                        401,
                    )
                credential = subscription["access"]
                previous_beta = upstream_request.headers.get("anthropic-beta")
                upstream_request.headers.update(
                    self.oauth_headers(effective_provider, subscription, incoming_headers)
                )
                if claude and previous_beta:
                    upstream_request.headers["anthropic-beta"] = ",".join(
                        dict.fromkeys(
                            [
                                *upstream_request.headers.get("anthropic-beta", "").split(","),
                                *previous_beta.split(","),
                            ]
                        )
                    )
                accounting.reset_physical_response()
                spend = accounting.prepare_spend(
                    outbound, native, provider, model, subscription, credential
                )
                accounting.start_send(spend)
                upstream = await self.client.send(upstream_request, stream=True)
                accounting.receive_response()
                if claude and upstream.status_code == 401:
                    await self.mark_oauth_rejected(provider, subscription)
            request.state.upstream_status = upstream.status_code
            request.state.upstream_content_type = upstream.headers.get("content-type")
            debug_sample = self.debug and self.debug.enabled("usage")
            request.state.usage_capture.observe(
                upstream,
                protocol=native,
                # The ChatGPT generation endpoint emits SSE regardless of its media type.
                event_stream=prepared.response_mode == "event_stream" and upstream.is_success,
                sample_redactor=(lambda text: self.debug.sample(text, secrets=(credential,)))
                if debug_sample
                else None,
                sample_limit=2048 + len(credential or ""),
            )
            if self.debug:
                self.debug.event(
                    "provider",
                    "upstream-status",
                    provider=provider.id,
                    model=model,
                    status=upstream.status_code,
                )
            if self.subagents:
                self.subagents.fallback.note_failure(
                    request.headers, f"{provider.id}/{model}", upstream.status_code
                )
            if chatgpt:
                self.channels.quota("openai").observe_headers(
                    subscription, upstream.headers, model=model
                )
        except SpendDenied:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            return JSONResponse(
                {"error": {"message": str(exc), "type": "invalid_request_error"}}, 400
            )
        except httpx.TimeoutException as exc:
            if self.debug:
                self.debug.event("provider", "transport-error", kind=type(exc).__name__)
            request.state.fallback_trigger = failure_trigger(exception=exc)
            return JSONResponse({"error": {"message": "Upstream timed out."}}, 504)
        except httpx.HTTPError as exc:
            if self.debug:
                self.debug.event("provider", "transport-error", kind=type(exc).__name__)
            request.state.fallback_trigger = failure_trigger(exception=exc)
            return JSONResponse(
                {"error": {"message": "Could not connect to the configured upstream."}}, 502
            )

        headers = {
            key: upstream.headers[key]
            for key in ("content-type", "retry-after", "x-request-id", "request-id")
            if key in upstream.headers
        }
        if chatgpt and issuer and not upstream.is_error and not upstream.is_redirect:
            handle = self.turn_states.issue(
                upstream.headers.get(TURN_STATE_HEADER),
                issuer,
                provider.id,
                model,
                incoming_headers,
            )
            if handle:
                headers[TURN_STATE_HEADER] = handle
        if upstream.is_error or upstream.is_redirect or not body.get("stream"):
            try:
                if chatgpt and not special and not upstream.is_error and not upstream.is_redirect:
                    result = None
                    async for event in events(upstream):
                        if event.get("type") in {
                            "response.completed",
                            "response.incomplete",
                            "response.failed",
                        }:
                            result = event.get("response")
                    if not isinstance(result, dict):
                        raise ValueError("ChatGPT stream ended without a final response.")
                    if result.get("status") == "failed":
                        return JSONResponse(
                            {
                                "error": result.get("error")
                                or {"message": "ChatGPT response failed."}
                            },
                            502,
                        )
                    content = json.dumps(result).encode()
                    headers["content-type"] = "application/json"
                else:
                    content = await upstream.aread()
                if chatgpt and special and not upstream.is_error and not upstream.is_redirect:
                    compact_result = json.loads(content)
                    if isinstance(compact_result, dict) and not compact_result.get("error"):
                        self.observe_subscription_turn(provider, issuer, incoming_headers)
                if upstream.is_error:
                    request.state.fallback_trigger = failure_trigger(
                        upstream.status_code, error_code=upstream_error_code(content)
                    )
                    if credential:
                        content = content.replace(credential.encode(), b"[redacted]")
                elif claude:
                    content = json.dumps(
                        restore_anthropic_payload(json.loads(content), name_map)
                    ).encode()
                if not upstream.is_error and translated:
                    chat = response_to_chat(
                        json.loads(content),
                        native,
                        progress_updates=progress_updates,
                        hide_thinking=hide_thinking,
                    )
                    result = response_from_chat(
                        chat, protocol, custom_tools=custom_tools, tool_names=tool_names
                    )
                    if protocol == "responses" and result.get("status") == "failed":
                        request.state.usage_capture.terminal_status = "failed"
                        request.state.usage_capture.logical_status = (
                            400
                            if (result.get("error") or {}).get("code") == "invalid_prompt"
                            else 502
                        )
                        request.state.usage_capture.error_code = (result.get("error") or {}).get(
                            "code"
                        ) or "upstream_error"
                    self.remember(
                        provider,
                        model,
                        body,
                        result,
                        chat,
                        issuer,
                        incoming_headers,
                        request.state.fallback_origin,
                    )
                    return JSONResponse(
                        result,
                        status_code=upstream.status_code,
                        headers={
                            key: value for key, value in headers.items() if key != "content-type"
                        },
                    )
                elif not upstream.is_error and native_tools:
                    result = native_tools.response(json.loads(content))
                    if stateless:
                        self.save_response(
                            provider,
                            model,
                            body,
                            result,
                            issuer,
                            incoming_headers,
                            request.state.fallback_origin,
                        )
                    return JSONResponse(
                        result,
                        status_code=upstream.status_code,
                        headers={
                            key: value for key, value in headers.items() if key != "content-type"
                        },
                    )
                elif not upstream.is_error and stateless:
                    self.save_response(
                        provider,
                        model,
                        body,
                        json.loads(content),
                        issuer,
                        incoming_headers,
                        request.state.fallback_origin,
                    )
                return Response(content, status_code=upstream.status_code, headers=headers)
            except httpx.HTTPError, ValueError, KeyError, IndexError, TypeError:
                return JSONResponse(
                    {"error": {"message": "Upstream returned an interrupted or invalid response."}},
                    502,
                )
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()

        async def chunks():
            encoder = (
                StreamEncoder(protocol, model, custom_tools, tool_names) if translated else None
            )
            try:
                if native_tools:
                    async for event in events(upstream):
                        for restored in native_tools.events(event):
                            if stateless and restored.get("type") in {
                                "response.completed",
                                "response.incomplete",
                            }:
                                self.save_response(
                                    provider,
                                    model,
                                    body,
                                    restored["response"],
                                    issuer,
                                    incoming_headers,
                                    request.state.fallback_origin,
                                )
                            yield sse(restored, restored.get("type"))
                elif encoder is None and claude:
                    async for event in events(upstream):
                        restored = restore_anthropic_payload(event, name_map)
                        yield sse(restored, restored.get("type"))
                elif encoder is None:
                    decoder = codecs.getincrementaldecoder("utf-8")() if stateless else None
                    pending = ""
                    async for value in upstream.aiter_bytes():
                        if decoder is not None:
                            pending += decoder.decode(value)
                            pending = pending.replace("\r\n", "\n")
                            while "\n\n" in pending:
                                frame, pending = pending.split("\n\n", 1)
                                payload = "\n".join(
                                    line[5:].lstrip(" ")
                                    for line in frame.split("\n")
                                    if line.startswith("data:")
                                )
                                if payload and payload != "[DONE]":
                                    event = json.loads(payload)
                                    if event.get("type") in {
                                        "response.completed",
                                        "response.incomplete",
                                    }:
                                        self.save_response(
                                            provider,
                                            model,
                                            body,
                                            event["response"],
                                            issuer,
                                            incoming_headers,
                                            request.state.fallback_origin,
                                        )
                        yield value
                else:
                    for value in encoder.start():
                        yield value
                    async for value in chat_chunks(
                        upstream,
                        native,
                        progress_updates=progress_updates,
                        hide_thinking=hide_thinking,
                        transform=(lambda event: restore_anthropic_payload(event, name_map))
                        if claude
                        else None,
                    ):
                        for event in encoder.feed(value):
                            yield event
                    result, terminal = encoder.finish()
                    if protocol == "responses" and result.get("status") == "failed":
                        request.state.usage_capture.terminal_status = "failed"
                        request.state.usage_capture.logical_status = (
                            400
                            if (result.get("error") or {}).get("code") == "invalid_prompt"
                            else 502
                        )
                        request.state.usage_capture.error_code = (result.get("error") or {}).get(
                            "code"
                        ) or "upstream_error"
                    self.remember(
                        provider,
                        model,
                        body,
                        result,
                        encoder.data,
                        issuer,
                        incoming_headers,
                        request.state.fallback_origin,
                    )
                    for event in terminal:
                        yield event
            except httpx.HTTPError, ValueError, KeyError, IndexError, TypeError:
                if encoder:
                    yield encoder.error(
                        "Upstream response was interrupted or could not be translated."
                    )
                else:
                    raise
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()

        if translated:
            headers.update({"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        return StreamingResponse(chunks(), status_code=upstream.status_code, headers=headers)

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/v1/models")
        async def models():
            if self.model_rows:
                rows = await self.model_rows()
                return {
                    "object": "list",
                    "data": [
                        {"id": row["namespaced"], "object": "model", "owned_by": row["provider"]}
                        for row in rows
                        if not row["disabled"] and row.get("selected", True)
                    ],
                }
            config = self.store.load()
            return {
                "object": "list",
                "data": [
                    {"id": f"{p.id}/{model}", "object": "model", "owned_by": p.id}
                    for p in config.providers
                    if not p.disabled
                    for model in p.models
                ],
            }

        @router.post("/v1/chat/completions")
        async def chat(request: Request):
            return await self.forward(request, "chat/completions")

        @router.post("/v1/responses")
        async def responses(request: Request):
            return await self.forward(request, "responses")

        @router.post("/v1/responses/compact")
        async def compact(request: Request):
            return await self.forward(request, "responses/compact")

        @router.post("/v1/messages")
        async def messages(request: Request):
            return await self.forward(request, "messages")

        @router.post("/v1/messages/count_tokens")
        async def count_tokens(request: Request):
            return await self.forward(request, "messages/count_tokens")

        return router
