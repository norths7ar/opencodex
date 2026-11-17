"""Bridge optional vision and hosted search into explicit internal execution."""

import asyncio
import codecs
import copy
import json
from collections import OrderedDict
from collections.abc import AsyncGenerator
from contextlib import aclosing

from backend.policies.search import MODELS, SearchService, search_plan
from backend.policies.vision import Auxiliary, BorrowedWorkflow, modalities
from backend.requests import RequestPurpose

from .compaction_dispatch import child_context


class InternalCallError(RuntimeError):
    def __init__(self, status, content=None, headers=None):
        super().__init__(f"Internal request failed with HTTP {status}.")
        self.status = status
        self.content = content
        self.headers = headers or {}


def borrowed(lease):
    return BorrowedWorkflow(lease.ledger, lease.root_id) if lease else None


async def decoded_events(response):
    """Yield bounded native SSE events or a buffered canonical terminal."""
    if response.status >= 400:
        raise InternalCallError(response.status, response.content, response.headers)
    if response.stream is None:
        if response.content is None:
            raise ValueError("Internal response has no body.")
        payload = json.loads(response.content)
        if not isinstance(payload, dict):
            raise ValueError("Invalid internal response.")
        yield {"type": "final", "response": payload}
        return
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buffer = ""
    async for chunk in response.stream:
        yield {"type": "heartbeat"}
        buffer = (buffer + decoder.decode(chunk)).replace("\r\n", "\n")
        if len(buffer) > 4 * 1024 * 1024:
            raise ValueError("Internal stream frame too large.")
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            data = "\n".join(
                line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")
            )
            if data and data != "[DONE]":
                value = json.loads(data)
                if isinstance(value, dict):
                    yield value
    buffer += decoder.decode(b"", final=True)
    if buffer.strip():
        data = "\n".join(
            line[5:].lstrip() for line in buffer.splitlines() if line.startswith("data:")
        )
        if data and data != "[DONE]":
            value = json.loads(data)
            if isinstance(value, dict):
                yield value


class AuxiliaryDispatch:
    def __init__(self, config_loader, rows, route, execute, *, auth_status=None, route_native):
        self.config_loader, self.rows, self.route, self.execute = (
            config_loader,
            rows,
            route,
            execute,
        )
        self.auth_status = auth_status
        self.route_native = route_native
        self.search_service = SearchService()
        self.vision_cache = OrderedDict()

    async def vision(self, body, protocol, target, context, headers, lease):
        async def complete(payload, wire, parent, borrowed_lease):
            child = child_context(parent, payload, wire, RequestPurpose.VISION)
            async with self.execute(
                child, payload, headers, borrowed_lease=borrowed_lease
            ) as response:
                if response.status >= 400:
                    return {"error": f"HTTP {response.status}"}
                if response.content is None or len(response.content) > 4 * 1024 * 1024:
                    return {"error": "description response unavailable"}
                try:
                    value = json.loads(response.content)["choices"][0]["message"]["content"]
                    if isinstance(value, list):
                        value = "".join(
                            part.get("text", "")
                            for part in value
                            if isinstance(part, dict) and isinstance(part.get("text"), str)
                        )
                    return (
                        {"text": value}
                        if isinstance(value, str)
                        else {"error": "no description text"}
                    )
                except ValueError, TypeError, KeyError, IndexError:
                    return {"error": "invalid description response"}

        vision = Auxiliary(self.config_loader, self.rows, self.route, complete, self.auth_status)
        vision.cache = self.vision_cache
        return await vision.prepare(
            body,
            protocol,
            target.channel,
            target.model_id,
            context,
            lease,
            terminal=context.purpose != RequestPurpose.GENERATION,
        )

    def plan_search(self, body, target, *, native=False, available=(), helper_channel=None):
        config = self.config_loader()
        settings = config.features.request.web_search_sidecar or {}
        plan = search_plan(
            body,
            settings,
            native=native,
            available=available,
        )
        if plan:
            rows = self.rows()
            models = {
                row["id"]
                for row in rows
                if row.get("provider") == (helper_channel or plan["backend"])
                and isinstance(row.get("id"), str)
                and "/" not in row["id"]
                and not row.get("disabled")
            }
            if settings.get("model") and plan["model"] not in {MODELS[plan["backend"]], *models}:
                return None
            modes = modalities(config, target.channel, target.model_id, rows)
            plan["describeImages"] = modes is not None and "image" not in modes
        return plan

    async def search(
        self,
        body,
        plan,
        context,
        headers,
        lease,
        *,
        target,
        scope=None,
        on_round_complete=None,
        helper_channel=None,
    ) -> AsyncGenerator[dict[str, object]]:
        """Yield search orchestration events; caller owns HTTP/event framing and history."""

        async def complete(payload, _plan):
            child = child_context(context, payload, "responses", RequestPurpose.GENERATION)
            async with self.execute(
                child, payload, headers, borrowed_lease=borrowed(lease), resolved_route=target
            ) as response:
                async for event in decoded_events(response):
                    if event.get("type") in {
                        "final",
                        "response.completed",
                        "response.failed",
                        "response.incomplete",
                    }:
                        if on_round_complete is not None:
                            on_round_complete(event["response"])
                        yield {"type": "final", "response": event["response"]}
                    else:
                        yield event

        async def dispatch(protocol, payload, helper_plan):
            connection = helper_channel or helper_plan["backend"]
            helper = self.route_native(connection, payload["model"])
            child = child_context(context, payload, protocol, RequestPurpose.SEARCH)
            async with self.execute(
                child, payload, headers, borrowed_lease=borrowed(lease), resolved_route=helper
            ) as response:
                async for event in decoded_events(response):
                    if event.get("type") != "heartbeat":
                        yield event

        request = copy.deepcopy(body)
        async with aclosing(
            self.search_service.run(request, plan, complete, dispatch, scope=scope)
        ) as stream:
            async for event in stream:
                if context.cancellation.is_set():
                    raise asyncio.CancelledError
                yield event
