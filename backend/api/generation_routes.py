"""HTTP adapters for local clients; execution and settlement stay in the gateway."""

import asyncio
from contextlib import AsyncExitStack, suppress
from sys import exc_info
from uuid import uuid4

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from backend.api.request_body import RequestBodyError, read_json
from backend.auth.access import SubscriptionAuthorizationError, SubscriptionRefreshUnavailable
from backend.gateway.alpha_search import AlphaSearchRelay
from backend.gateway.delivery import InvalidUpstreamResponse
from backend.gateway.delivery_sse import UpstreamBodyOverflow
from backend.gateway.transport import finish_cleanup
from backend.policies.pacing import PacingDenied
from backend.policies.spend_ledger import SpendDenied
from backend.policies.workflow import LOCAL_REFUSAL_HEADER, WorkflowDenied
from backend.requests import RequestContext, RequestPurpose
from backend.telemetry.capture import classify_client, conversation_id


def local_refusal(exc):
    message = str(exc)
    detail = {}
    if isinstance(exc, SpendDenied):
        message = (
            "This proxy refused the request locally before this send contacted a provider: "
            + exc.code
        )
        if exc.detail:
            detail["spend"] = exc.detail
    return JSONResponse(
        {
            "error": {
                "type": "rate_limit_error",
                "code": "rate_limit_exceeded",
                "message": message,
                **detail,
            }
        },
        429,
        headers={
            LOCAL_REFUSAL_HEADER: exc.code,
            "access-control-expose-headers": LOCAL_REFUSAL_HEADER,
            "cache-control": "no-store",
        },
    )


def body_error(exc):
    return JSONResponse(
        {"error": {"type": "invalid_request_error", "message": str(exc)}}, exc.status
    )


class OwnedStream(StreamingResponse):
    def __init__(self, response, stack, cancellation):
        super().__init__(response.stream, status_code=response.status, headers=response.headers)
        self.stack = stack
        self.cancellation = cancellation

    async def __call__(self, scope, receive, send):
        async def send_to_client(message):
            try:
                await send(message)
            except OSError:
                self.cancellation.set()
                raise asyncio.CancelledError from None

        try:
            await self.stream_response(send_to_client)
        except asyncio.CancelledError:
            await finish_cleanup(self.stack.__aexit__(*exc_info()))
            if not self.cancellation.is_set():
                raise
        except BaseException:
            await finish_cleanup(self.stack.__aexit__(*exc_info()))
            raise
        else:
            await finish_cleanup(self.stack.aclose())


def generation_router(dispatcher, catalog):
    router = APIRouter()
    alpha_search = AlphaSearchRelay(dispatcher)

    @router.get("/v1/models")
    def models():
        return {
            "object": "list",
            "data": [
                {
                    "id": model.routed_selector,
                    "object": "model",
                    "owned_by": model.identity.channel_id,
                }
                for model in catalog()
                if model.visible
            ],
        }

    @router.get("/v1/responses")
    def responses_transport(request: Request):
        if request.headers.get("upgrade", "").lower() == "websocket":
            return JSONResponse(
                {
                    "error": {
                        "code": "upgrade_required",
                        "message": "Responses WebSocket transport is disabled; use HTTP",
                    }
                },
                426,
            )
        return JSONResponse({"error": "Use POST for Responses requests."}, 405)

    async def generate(request: Request, protocol):
        stack = AsyncExitStack()
        cancellation = asyncio.Event()
        try:
            body = await read_json(
                request, dispatcher.config_loader().features.request.max_inbound_body_bytes
            )
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            if body.get("model") is not None and not isinstance(body["model"], str):
                raise ValueError("Model must be a string.")

            async def watch_disconnect():
                while True:
                    if (await request.receive())["type"] == "http.disconnect":
                        cancellation.set()
                        return

            watcher = asyncio.create_task(watch_disconnect())

            async def stop_watcher():
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher

            stack.push_async_callback(stop_watcher)
            context = RequestContext(
                str(uuid4()),
                protocol,
                body.get("model") or "",
                bool(body.get("stream")),
                cancellation,
                purpose=RequestPurpose.COMPACTION
                if protocol.endswith("/compact")
                else RequestPurpose.GENERATION,
                client_id=classify_client(request.headers),
                workflow_id=request.headers.get("x-codex-parent-thread-id"),
                conversation_id=conversation_id(request.headers, body, protocol),
                requested_service_tier=body.get("service_tier"),
            )
            response = await stack.enter_async_context(
                dispatcher.execute(context, body, dict(request.headers))
            )
            if response.stream is not None:
                return OwnedStream(response, stack.pop_all(), cancellation)
            return Response(response.content, status_code=response.status, headers=response.headers)
        except asyncio.CancelledError:
            if cancellation.is_set():
                return Response(status_code=499)
            raise
        except (SubscriptionAuthorizationError, SubscriptionRefreshUnavailable) as exc:
            return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, exc.status)
        except (InvalidUpstreamResponse, UpstreamBodyOverflow) as exc:
            return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, 502)
        except PacingDenied as exc:
            return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, 429)
        except (SpendDenied, WorkflowDenied) as exc:
            return local_refusal(exc)
        except RequestBodyError as exc:
            return body_error(exc)
        except httpx.TimeoutException:
            return JSONResponse({"error": {"message": "Upstream timed out."}}, 504)
        except httpx.HTTPError:
            return JSONResponse({"error": {"message": "Upstream connection failed."}}, 502)
        except (ValueError, TypeError, KeyError) as exc:
            return JSONResponse(
                {"error": {"message": str(exc), "type": "invalid_request_error"}}, 400
            )
        finally:
            await finish_cleanup(stack.aclose())

    @router.post("/v1/alpha/search")
    async def native_search(request: Request):
        cancellation = asyncio.Event()
        watcher = None
        try:
            body = await read_json(
                request, dispatcher.config_loader().features.request.max_inbound_body_bytes
            )

            async def watch_disconnect():
                while True:
                    if (await request.receive())["type"] == "http.disconnect":
                        cancellation.set()
                        return

            watcher = asyncio.create_task(watch_disconnect())
            model = body.get("model") if isinstance(body, dict) else None
            context = RequestContext(
                str(uuid4()),
                "alpha/search",
                model if isinstance(model, str) else "",
                False,
                cancellation,
                purpose=RequestPurpose.SEARCH,
                client_id=classify_client(request.headers),
                workflow_id=request.headers.get("x-codex-parent-thread-id"),
            )
            result = await alpha_search(context, body, dict(request.headers))
            return Response(result.content, status_code=result.status, headers=result.headers)
        except asyncio.CancelledError:
            if cancellation.is_set():
                return Response(status_code=499)
            raise
        except RequestBodyError as exc:
            return body_error(exc)
        except (SpendDenied, WorkflowDenied) as exc:
            return local_refusal(exc)
        except PacingDenied as exc:
            return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, 429)
        except ValueError, TypeError:
            return JSONResponse(
                {"error": {"type": "invalid_request_error", "message": "Invalid search request."}},
                400,
            )
        finally:
            if watcher is not None:
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher

    @router.post("/v1/chat/completions")
    async def chat(request: Request):
        return await generate(request, "chat/completions")

    @router.post("/v1/responses")
    async def responses(request: Request):
        return await generate(request, "responses")

    @router.post("/v1/responses/compact")
    async def compact(request: Request):
        return await generate(request, "responses/compact")

    @router.post("/v1/messages")
    async def messages(request: Request):
        return await generate(request, "messages")

    @router.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request):
        return await generate(request, "messages/count_tokens")

    return router
