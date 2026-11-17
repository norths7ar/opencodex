"""A caller can finish a request, recover from failure, and cancel work safely."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from backend.api.generation_routes import generation_router
from backend.configuration.models import (
    Channel,
    ChannelModels,
    Endpoints,
    Features,
    RequestSettings,
)
from backend.services import Services

pytestmark = pytest.mark.anyio


def completed():
    return httpx.Response(
        200,
        json={
            "id": "response-completed",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Ready to continue."}],
                }
            ],
            "usage": {"input_tokens": 7, "output_tokens": 3},
        },
    )


@asynccontextmanager
async def gateway(sandbox, upstream):
    services = Services(
        sandbox / "state",
        10333,
        codex_home=sandbox / "codex",
        dsh_home=sandbox / "dsh",
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        background_refresh=False,
    )

    def configure(state):
        state.configuration.channels = [
            Channel(
                id=name,
                name=name,
                service_id="custom",
                protocol="openai-responses",
                auth_mode="local",
                endpoints=Endpoints(base_url=f"https://{name}.invalid/v1"),
                models=ChannelModels(live=False, static=["model"]),
            )
            for name in ("primary", "backup")
        ]
        state.configuration.default_channel_id = "primary"
        state.configuration.features = Features(
            request=RequestSettings(
                simple_fallback={
                    "enabled": True,
                    "routes": {"primary/model": ["backup/model"]},
                }
            )
        )

    services.store.update(configure)
    app = FastAPI()
    app.include_router(generation_router(services.dispatcher, services.models))
    try:
        yield services, app
    finally:
        await services.auth.close()
        await services.upstreams.close()
        services.spend.close()
        services.debug.close()


async def call(app, *, streaming=False, disconnect=False, output=None):
    """Drive HTTP directly so response chunks and a disconnected write remain observable."""
    messages = output if output is not None else []
    incoming = asyncio.Queue()
    incoming.put_nowait(
        {
            "type": "http.request",
            "body": json.dumps(
                {
                    "model": "primary/model",
                    "input": "Help me finish this task.",
                    "stream": streaming,
                    "max_output_tokens": 32,
                }
            ).encode(),
            "more_body": False,
        }
    )

    async def send(message):
        if disconnect and message.get("body"):
            raise BrokenPipeError("The caller closed its connection")
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "scheme": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/v1/responses",
        "raw_path": b"/v1/responses",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"x-codex-parent-thread-id", b"recovery-workflow"),
        ],
        "server": ("127.0.0.1", 10333),
        "client": ("127.0.0.1", 12345),
    }
    async with asyncio.timeout(5):
        await app(scope, incoming.get, send)
    return messages


def result(messages):
    return json.loads(b"".join(message.get("body", b"") for message in messages))


def last_request(services):
    return list(services.telemetry.store.rows())[-1]


def attempts(services, row):
    return list(services.telemetry.store.attempts(row["id"]))


def assert_released(services):
    assert services.workflow.view("recovery-workflow")["root"]["active"] == 0


async def test_normal_generation_records_one_send_and_success(sandbox):
    sent = []

    def upstream(request):
        sent.append(request.url.host)
        return completed()

    async with gateway(sandbox, upstream) as (services, app):
        response = await call(app)
        assert response[0]["status"] == 200, services.dispatcher.fallback.state()
        assert result(response)["output"][0]["content"][0]["text"] == "Ready to continue."
        assert sent == ["primary.invalid"]
        row = last_request(services)
        assert row["status"] == 200
        assert row["provider"] == "primary" and row["model"] == "model"
        assert row["requested_model"] == "primary/model"
        assert row["send_count"] == row["settled_sends"] == 1
        physical = attempts(services, row)
        assert len(physical) == 1 and physical[0]["status"] == 200
        assert row["usage_status"] == "reported"
        assert row["usage"]["inputTokens"] == 7
        assert row["usage"]["outputTokens"] == 3
        assert services.milestones.first_success_at() is not None
        assert_released(services)


async def test_failure_before_output_uses_backup_and_records_both_sends(sandbox):
    sent = []

    def upstream(request):
        sent.append(request.url.host)
        if request.url.host == "primary.invalid":
            return httpx.Response(503, json={"error": {"code": "service_unavailable"}})
        return completed()

    async with gateway(sandbox, upstream) as (services, app):
        response = await call(app)
        assert response[0]["status"] == 200, services.dispatcher.fallback.state()
        assert result(response)["status"] == "completed"
        assert sent == ["primary.invalid", "backup.invalid"]
        row = last_request(services)
        assert row["status"] == 200 and row["send_count"] == 2
        assert row["provider"] == "backup" and row["model"] == "model"
        assert row["requested_model"] == "primary/model"
        physical = attempts(services, row)
        assert [item["status"] for item in physical] == [503, 200]
        assert physical[0]["usage_status"] == "unreported"
        assert physical[0]["usage"] is None
        assert physical[1]["usage_status"] == "reported"
        assert services.milestones.first_success_at() is not None
        assert_released(services)


async def test_all_targets_fail_then_repaired_upstream_can_serve_next_request(sandbox):
    repaired = False
    sent = []

    def upstream(request):
        sent.append(request.url.host)
        if repaired:
            return completed()
        return httpx.Response(503, json={"error": {"code": "service_unavailable"}})

    async with gateway(sandbox, upstream) as (services, app):
        failed = await call(app)
        assert failed[0]["status"] == 503
        assert result(failed)["error"]["code"] == "service_unavailable"
        row = last_request(services)
        assert row["status"] == 503 and row["send_count"] == 2
        assert [item["status"] for item in attempts(services, row)] == [503, 503]
        assert row["usage_status"] == "unreported"
        assert row["usage"] is None
        assert services.milestones.first_success_at() is None
        assert_released(services)

        repaired = True
        recovered = await call(app)
        assert recovered[0]["status"] == 200
        assert result(recovered)["status"] == "completed"
        assert sent == ["primary.invalid", "backup.invalid", "primary.invalid"]
        assert last_request(services)["send_count"] == 1
        assert services.milestones.first_success_at() is not None
        assert_released(services)


class InterruptedOutput(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield b'data: {"type":"response.output_text.delta","delta":"Partial answer"}\n\n'
        raise httpx.ReadError("Upstream connection broke after producing output")

    async def aclose(self):
        self.closed = True


async def test_interrupted_output_is_not_replayed_on_backup(sandbox):
    stream = InterruptedOutput()
    sent = []

    def upstream(request):
        sent.append(request.url.host)
        return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

    async with gateway(sandbox, upstream) as (services, app):
        delivered = []
        with pytest.raises(httpx.ReadError):
            await call(app, streaming=True, output=delivered)
        assert delivered[0]["status"] == 200
        assert b"Partial answer" in b"".join(item.get("body", b"") for item in delivered)
        assert sent == ["primary.invalid"]
        assert stream.closed
        row = last_request(services)
        assert row["status"] == 502 and row["error_code"] == "upstream_interrupted"
        assert row["send_count"] == 1 and row["settled_sends"] == 0
        assert row["usage_status"] == "unreported"
        assert row["usage"] is None
        assert len(attempts(services, row)) == 1
        assert services.milestones.first_success_at() is None
        assert_released(services)


async def test_disconnected_caller_releases_work_and_can_start_another_request(sandbox):
    stream = InterruptedOutput()
    sent = []

    def upstream(request):
        sent.append(request.url.host)
        if len(sent) == 1:
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})
        return completed()

    async with gateway(sandbox, upstream) as (services, app):
        await call(app, streaming=True, disconnect=True)
        assert stream.closed
        row = last_request(services)
        assert row["status"] == 499 and row["close_reason"] == "client_cancel"
        assert row["send_count"] == 1 and row["settled_sends"] == 0
        assert row["usage_status"] == "unreported"
        assert row["usage"] is None
        assert len(attempts(services, row)) == 1
        assert services.milestones.first_success_at() is None
        assert_released(services)

        response = await call(app)
        assert response[0]["status"] == 200
        assert result(response)["status"] == "completed"
        assert sent == ["primary.invalid", "primary.invalid"]
        assert last_request(services)["send_count"] == 1
        assert services.milestones.first_success_at() is not None
        assert_released(services)
