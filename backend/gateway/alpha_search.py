"""Bounded native ChatGPT alpha/search relay, sharing physical-send ownership."""

import asyncio
import copy
import json
import time
from dataclasses import dataclass
from uuid import uuid4

import httpx

from backend.auth.access import SubscriptionAuthorizationError, SubscriptionRefreshUnavailable
from backend.policies.pacing import PacingDenied
from backend.policies.workflow import WorkflowDenied
from backend.requests import AttemptContext, AttemptReason, Outcome, TerminalStatus
from backend.telemetry.pricing import pricing_family

from .attempts import PhysicalAttempt
from .delivery_sse import UpstreamBodyOverflow
from .transport import PreparedSend, finish_cleanup, until_cancelled

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
TIMEOUT_SECONDS = 200


@dataclass(slots=True)
class SearchResponse:
    status: int
    headers: dict[str, str]
    content: bytes


def _error(status, message):
    return SearchResponse(
        status,
        {"content-type": "application/json"},
        json.dumps(
            {
                "error": {
                    "type": "authentication_error" if status == 401 else "upstream_error",
                    "message": message,
                }
            }
        ).encode(),
    )


def isolate_metadata(body, installation_id):
    payload = copy.deepcopy(body)
    if isinstance(payload, dict) and isinstance(payload.get("client_metadata"), dict):
        metadata = payload["client_metadata"]
        metadata.pop("x-codex-turn-state", None)
        metadata.pop("x-oai-attestation", None)
        if "x-codex-installation-id" in metadata:
            metadata["x-codex-installation-id"] = installation_id
        turn_metadata = metadata.get("x-codex-turn-metadata")
        if isinstance(turn_metadata, str) and turn_metadata.strip():
            if len(turn_metadata.encode("utf-8")) > 96 * 1024:
                raise ValueError("Turn metadata exceeds the size limit.")
            try:
                parsed = json.loads(turn_metadata)
            except ValueError:
                parsed = None
            if (
                isinstance(parsed, dict)
                and isinstance(parsed.get("installation_id"), str)
                and parsed["installation_id"].strip()
            ):
                parsed["installation_id"] = installation_id
                canonical = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
                if len(canonical.encode("utf-8")) > 32 * 1024:
                    raise ValueError("Turn metadata exceeds the size limit.")
                metadata["x-codex-turn-metadata"] = json.dumps(
                    parsed, ensure_ascii=True, separators=(",", ":")
                )
            elif len(turn_metadata.encode("utf-8")) > 32 * 1024:
                raise ValueError("Turn metadata exceeds the size limit.")
    return payload


class AlphaSearchRelay:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher

    async def __call__(self, request, body, incoming):
        dispatcher = self.dispatcher
        executor = dispatcher.executor
        telemetry = executor.telemetry
        started = time.monotonic()
        row = None
        lease = None
        outcome = Outcome(status_code=500, terminal_status=TerminalStatus.FAILED)
        usage = None
        attempt_id = None
        try:

            async def begin():
                nonlocal row
                row = await telemetry.begin_request(request)

            await finish_cleanup(begin())
            async with asyncio.timeout(TIMEOUT_SECONDS):
                channel = next(
                    (
                        c
                        for c in dispatcher.config_loader().channels
                        if c.auth_mode == "oauth" and c.service_id == "chatgpt" and not c.disabled
                    ),
                    None,
                )
                if channel is None:
                    outcome = Outcome(
                        status_code=401,
                        terminal_status=TerminalStatus.FAILED,
                        error_code="authentication_required",
                    )
                    return _error(401, "Web search relay needs a signed-in ChatGPT channel.")
                try:
                    credential = await until_cancelled(
                        executor.authentication.token(channel), request.cancellation
                    )
                except SubscriptionRefreshUnavailable:
                    raise
                except SubscriptionAuthorizationError, ValueError:
                    outcome = Outcome(
                        status_code=401,
                        terminal_status=TerminalStatus.FAILED,
                        error_code="authentication_required",
                    )
                    return _error(401, "Web search relay needs a signed-in ChatGPT channel.")
                payload = isolate_metadata(body, credential["installationId"])
                model = payload.get("model") if isinstance(payload, dict) else None
                if isinstance(model, str) and "/" in model:
                    prefix, _ = model.split("/", 1)
                    if prefix in {channel.id, channel.alias}:
                        try:
                            model = dispatcher.route(model).model_id
                        except ValueError:
                            outcome = Outcome(
                                status_code=400,
                                terminal_status=TerminalStatus.FAILED,
                                error_code="invalid_model",
                            )
                            return _error(400, "Invalid model selector.")
                        payload["model"] = model
                model = model if isinstance(model, str) else ""
                lease = dispatcher.workflow.admit_http(incoming)
                for index in range(2):
                    if index:
                        try:
                            renewed = await until_cancelled(
                                executor.authentication.token(channel, rejected=credential),
                                request.cancellation,
                            )
                            if renewed["accountId"] != credential["accountId"]:
                                raise SubscriptionAuthorizationError("Account changed.")
                            credential = renewed
                        except SubscriptionAuthorizationError, httpx.HTTPError:
                            outcome = Outcome(
                                status_code=401,
                                terminal_status=TerminalStatus.FAILED,
                                error_code="upstream_error",
                            )
                            return _error(401, "ChatGPT search authorization failed.")
                    headers = executor.authentication.headers(
                        channel, "openai-responses", incoming=incoming, credential=credential
                    )
                    headers.pop("x-codex-turn-state", None)
                    headers.pop("x-oai-attestation", None)
                    headers["accept"] = "application/json"
                    attempt = AttemptContext(
                        request.request_id,
                        str(uuid4()),
                        channel.id,
                        model,
                        "alpha/search",
                        request.cancellation,
                        reason=AttemptReason.AUTH_REFRESH if index else AttemptReason.INITIAL,
                        account_identity=credential["accountId"],
                    )
                    attempt_id = attempt.attempt_id
                    physical = PhysicalAttempt(executor.transport, telemetry)
                    send = PreparedSend(
                        channel.endpoints.base_url.rstrip("/") + "/alpha/search",
                        "alpha/search",
                        headers,
                        payload,
                        channel.id,
                        False,
                    )
                    async with executor.pacer.acquire(
                        channel.id, model, channel.request_pacing, request.cancellation
                    ):
                        if lease:
                            lease.reserve_send()
                        # Native search counts as a send but does not report model-token usage.
                        async with physical.open(
                            send,
                            request,
                            attempt,
                            row,
                            auth_mode="oauth",
                            price_family=pricing_family(channel),
                        ):
                            upstream = physical.response
                            if upstream is None:
                                raise RuntimeError("Search upstream was not opened.")
                            content = bytearray()
                            async for chunk in physical.chunks():
                                if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                                    raise UpstreamBodyOverflow()
                                content.extend(chunk)
                            if upstream.status_code == 401 and index == 0:
                                continue
                            status = upstream.status_code
                            response_headers = {
                                key: upstream.headers[key]
                                for key in ("content-type", "retry-after")
                                if key in upstream.headers
                            }
                            outcome = Outcome(
                                status_code=status,
                                terminal_status=TerminalStatus.COMPLETED
                                if 200 <= status < 400
                                else TerminalStatus.FAILED,
                            )
                            result = SearchResponse(status, response_headers, bytes(content))
                    usage = physical.context.usage
                    return result
        except UpstreamBodyOverflow as exc:
            outcome = Outcome(
                status_code=exc.status, terminal_status=TerminalStatus.FAILED, error_code=exc.code
            )
            return _error(exc.status, "Search response exceeded the size limit.")
        except SubscriptionRefreshUnavailable as exc:
            attempt_id = None
            outcome = Outcome(
                status_code=exc.status, terminal_status=TerminalStatus.FAILED, error_code=exc.code
            )
            return _error(exc.status, str(exc))
        except (PacingDenied, WorkflowDenied) as exc:
            outcome = Outcome(
                status_code=429, terminal_status=TerminalStatus.FAILED, error_code=exc.code
            )
            raise
        except asyncio.CancelledError:
            outcome = Outcome(
                status_code=499, terminal_status=TerminalStatus.CANCELLED, cancelled=True
            )
            raise
        except TimeoutError, httpx.TimeoutException:
            outcome = Outcome(
                status_code=504,
                terminal_status=TerminalStatus.FAILED,
                error_code="upstream_timeout",
            )
            return _error(504, "Search upstream timed out.")
        except httpx.HTTPError:
            outcome = Outcome(
                status_code=502,
                terminal_status=TerminalStatus.FAILED,
                error_code="upstream_failure",
            )
            return _error(502, "Search upstream connection failed.")
        finally:

            async def finalize():
                try:
                    await telemetry.end_request(
                        request,
                        row,
                        outcome,
                        attempt_id=attempt_id,
                        usage=usage,
                        duration_ms=(time.monotonic() - started) * 1000,
                    )
                finally:
                    if lease:
                        lease.release()

            await finish_cleanup(finalize())
