"""Explicit completion probes through the actual selected provider wire."""

import asyncio
import json
import time
from uuid import uuid4

import httpx

from backend.configuration.models import Channel, Protocol
from backend.protocols import response_to_chat
from backend.providers import match_destination
from backend.requests import RequestContext, RequestPurpose

from .delivery import InvalidUpstreamResponse
from .preparation import WIRES

_CAP = 32
_PROMPT = "Reply with OK only."
_MAX_RESPONSE = 4 * 1024 * 1024


def _body(channel: Channel, model: str, protocol: Protocol) -> dict:
    body = {
        "model": f"{channel.id}/{model}",
        "stream": False,
        **(
            {"input": _PROMPT, "max_output_tokens": _CAP, "store": False}
            if protocol == "openai-responses"
            else {"messages": [{"role": "user", "content": _PROMPT}], "max_tokens": _CAP}
        ),
    }
    match = match_destination(channel)
    if match is not None and match.service.id == "deepseek":
        body["thinking"] = {"type": "disabled"}
    return body


def _status_error(status: int) -> dict:
    code = (
        "authentication_required"
        if status in {401, 403}
        else "rate_limit_exceeded"
        if status == 429
        else "model_not_found"
        if status == 404
        else "upstream_timeout"
        if status in {408, 504}
        else "upstream_failure"
    )
    return {
        "code": code,
        "status": status,
        "message": f"Connection check failed with upstream HTTP {status}.",
    }


class CompletionProbe:
    """Adapter for a confirmed management action; never called by model discovery."""

    def __init__(self, dispatcher, *, timeout: float = 30):
        self.dispatcher = dispatcher
        self.timeout = timeout

    async def __call__(self, channel: Channel, model: str) -> dict:
        if channel.disabled:
            return {"applicable": False, "reason": "disabled"}
        started = time.monotonic()
        cancel = asyncio.Event()
        selector = f"{channel.id}/{model}"
        wire_model, protocol = model, channel.protocol_for_model(model)
        served = None
        ttft = None
        error = None
        termination = "completed"
        try:
            target = self.dispatcher.route_native(channel.id, model)
            if target.channel.id != channel.id:
                raise ValueError("Probe target no longer matches the selected channel.")
            wire_model = target.model_id
            protocol = target.channel.protocol_for_model(wire_model)
            wire = WIRES[protocol]
            body = _body(target.channel, wire_model, protocol)
            request = RequestContext(
                request_id=str(uuid4()),
                protocol=wire,
                requested_model=selector,
                streaming=False,
                cancellation=cancel,
                purpose=RequestPurpose.PROBE,
            )
            async with asyncio.timeout(self.timeout):
                async with self.dispatcher.execute(
                    request, body, {}, resolved_route=target
                ) as response:
                    if not 200 <= response.status < 300:
                        error = _status_error(response.status)
                        termination = "timeout" if response.status in {408, 504} else "failed"
                    elif response.content is None:
                        raise InvalidUpstreamResponse("The completion probe did not return a body.")
                    elif len(response.content) > _MAX_RESPONSE:
                        termination = "too_large"
                        error = {
                            "code": "response_too_large",
                            "message": "Completion probe response was too large.",
                        }
                    elif not (
                        response.state.terminal == "completed"
                        or target.channel.auth_mode != "oauth"
                        and response.state.terminal == "incomplete"
                        and response.state.logical_status == 200
                    ):
                        logical = response.state.logical_status
                        error = _status_error(logical if logical and logical >= 400 else 502)
                        termination = "failed"
                    else:
                        payload = json.loads(response.content)
                        try:
                            chat = response_to_chat(payload, wire)
                        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                            raise InvalidUpstreamResponse(
                                "Upstream returned an invalid completion."
                            ) from exc
                        if not isinstance(chat["choices"][0]["message"], dict):
                            raise InvalidUpstreamResponse(
                                "The completion probe returned no message."
                            )
                        reported = payload.get("model")
                        if isinstance(reported, str) and reported.strip():
                            served = reported
                attempt = getattr(response, "attempt", None)
                if attempt is not None:
                    served = attempt.capture.served_model or served
                    if attempt.capture.streaming:
                        ttft = attempt.capture.first_output_ms
        except asyncio.CancelledError:
            cancel.set()
            raise
        except TimeoutError, httpx.TimeoutException:
            cancel.set()
            termination = "timeout"
            error = {"code": "upstream_timeout", "message": "Completion probe timed out."}
        except httpx.HTTPError:
            termination = "failed"
            error = {
                "code": "upstream_transport_error",
                "message": "Could not connect to the selected channel.",
            }
        except InvalidUpstreamResponse, json.JSONDecodeError, KeyError, IndexError, TypeError:
            termination = "failed"
            error = {
                "code": "invalid_upstream_response",
                "message": "Upstream returned an invalid completion.",
            }
        except ValueError, RuntimeError, OSError:
            termination = "failed"
            error = {
                "code": "upstream_failure",
                "message": "The selected channel could not complete the probe.",
            }
        return {
            "applicable": True,
            "ok": error is None,
            "provider": channel.id,
            "requestedModel": model,
            "wireModel": wire_model,
            "wire": protocol,
            "upstreamModel": served,
            "modelComparison": "match"
            if served == wire_model
            else "mismatch"
            if served
            else "unknown",
            **({"comparisonNote": "missing_metadata"} if not served else {}),
            "identityVerified": False,
            "ttftMs": ttft,
            "totalLatencyMs": round((time.monotonic() - started) * 1000),
            "outputTokenCap": _CAP,
            "outputTokenCapApplied": channel.auth_mode != "oauth",
            "termination": termination,
            "error": error,
        }
