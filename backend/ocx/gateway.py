"""Native OpenAI-compatible forwarding, including unchanged SSE and tool-call payloads."""

import json
from contextlib import asynccontextmanager

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import ConfigStore, Provider


class Gateway:
    def __init__(self, store: ConfigStore):
        self.store = store
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=10),
            follow_redirects=False,
        )

    @asynccontextmanager
    async def lifespan(self, app):
        try:
            yield
        finally:
            await self.client.aclose()

    def headers(self, provider: Provider) -> dict:
        return {
            "Authorization": f"Bearer {self.store.credential(provider)}",
            "Content-Type": "application/json",
        }

    async def models(self, provider: Provider) -> list[dict]:
        response = await self.client.get(
            f"{provider.baseUrl}/models", headers=self.headers(provider)
        )
        if response.status_code != 200:
            raise ValueError(f"Model discovery failed with upstream HTTP {response.status_code}.")
        data = response.json().get("data")
        if not isinstance(data, list) or any(
            not isinstance(m, dict) or not isinstance(m.get("id"), str) for m in data
        ):
            raise ValueError("Upstream did not return an OpenAI-compatible model list.")
        return data

    async def check(self, provider: Provider, model: str) -> dict:
        if provider.models and model not in provider.models:
            raise ValueError("Select an enabled model.")
        response = await self.client.post(
            f"{provider.baseUrl}/chat/completions",
            headers=self.headers(provider),
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK only."}],
                "max_tokens": 32,
                "stream": False,
                **(
                    {"thinking": {"type": "disabled"}}
                    if provider.baseUrl
                    in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}
                    else {}
                ),
            },
        )
        if response.status_code != 200:
            raise ValueError(f"Connection check failed with upstream HTTP {response.status_code}.")
        try:
            reply = response.json()["choices"][0]["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ValueError("Upstream returned an invalid chat response.") from exc
        return {"ok": True, "model": model, "reply": reply}

    async def forward(self, request: Request, protocol: str):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("Expected a JSON object.")
            if body.get("model") is not None and not isinstance(body["model"], str):
                raise ValueError("Model must be a string.")
            provider, model = self.store.route(body.get("model"))
            if protocol == "responses" and not provider.responses:
                raise ValueError(
                    "This provider does not have native Responses enabled. Use Chat Completions."
                )
            body["model"] = model
            credential = self.store.credential(provider)
            upstream_request = self.client.build_request(
                "POST",
                f"{provider.baseUrl}/{protocol}",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            upstream = await self.client.send(upstream_request, stream=True)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return JSONResponse(
                {"error": {"message": str(exc), "type": "invalid_request_error"}}, 400
            )
        except httpx.TimeoutException:
            return JSONResponse({"error": {"message": "Upstream timed out."}}, 504)
        except httpx.HTTPError:
            return JSONResponse(
                {"error": {"message": "Could not connect to the configured upstream."}}, 502
            )

        headers = {
            key: upstream.headers[key]
            for key in ("content-type", "retry-after", "x-request-id")
            if key in upstream.headers
        }
        if upstream.is_error or upstream.is_redirect or not body.get("stream"):
            try:
                content = await upstream.aread()
                if upstream.is_error:
                    content = content.replace(credential.encode(), b"[redacted]")
                return Response(content, status_code=upstream.status_code, headers=headers)
            except httpx.HTTPError:
                return JSONResponse(
                    {"error": {"message": "Upstream response was interrupted."}}, 502
                )
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()

        async def chunks():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()

        return StreamingResponse(chunks(), status_code=upstream.status_code, headers=headers)

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/v1/models")
        async def models():
            config = self.store.load()
            return {
                "object": "list",
                "data": [
                    {"id": f"{p.id}/{model}", "object": "model", "owned_by": p.id}
                    for p in config.providers
                    for model in p.models
                ],
            }

        @router.post("/v1/chat/completions")
        async def chat(request: Request):
            return await self.forward(request, "chat/completions")

        @router.post("/v1/responses")
        async def responses(request: Request):
            return await self.forward(request, "responses")

        return router
