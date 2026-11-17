"""Optional helpers participate in the caller's task rather than a separate request lifecycle."""

import asyncio
import json
import time
from uuid import uuid4

import httpx
import pytest

from backend.configuration.models import AgentSettings, Features, RequestSettings
from backend.requests import RequestContext

pytestmark = pytest.mark.anyio

IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlS8AAAAASUVORK5CYII="
)


def chat_answer(text):
    return httpx.Response(
        200,
        json={
            "id": "chat-result",
            "object": "chat.completion",
            "model": "model-a",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        },
    )


def response_value(text="Task completed.", *, output=None):
    return {
        "id": "response-" + uuid4().hex,
        "object": "response",
        "status": "completed",
        "output": output
        if output is not None
        else [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }


def configure(services, **settings):
    def change(state):
        state.configuration.features = Features(request=RequestSettings(**settings))

    services.store.update(change)


def image_request():
    return {
        "model": "main/model-a",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in the picture?"},
                    {"type": "image_url", "image_url": {"url": IMAGE}},
                ],
            }
        ],
    }


def request_rows(services):
    return list(services.telemetry.store.rows())


async def setup_vision(app, register_channel, **settings):
    await register_channel(
        app.client, identity="main", request_policy={"noVisionModels": ["model-a"]}
    )
    await register_channel(app.client, identity="vision")
    configure(
        app.services, vision_sidecar={"backend": "routed", "model": "vision/model-a"}, **settings
    )


async def test_vision_failure_is_explained_then_recovered_description_reaches_main_model(
    open_lite, register_channel
):
    unavailable = True
    sent = []

    def upstream(request):
        body = json.loads(request.content)
        sent.append((request.url.host, body))
        if request.url.host == "vision.invalid":
            if unavailable:
                return httpx.Response(503, json={"error": {"message": "unavailable"}})
            return chat_answer("A blue square.")
        return chat_answer("The image was considered.")

    async with open_lite(upstream) as app:
        await setup_vision(app, register_channel)
        first = await app.client.post("/v1/chat/completions", json=image_request())
        assert first.status_code == 200, first.text
        assert "vision helper unavailable" in json.dumps(sent[-1][1])
        assert IMAGE not in json.dumps(sent[-1][1])
        unavailable = False
        second = await app.client.post("/v1/chat/completions", json=image_request())
        assert second.status_code == 200, second.text
        assert "A blue square." in json.dumps(sent[-1][1])
        assert [host for host, _ in sent] == ["vision.invalid", "main.invalid"] * 2
        rows = request_rows(app.services)
        assert len(rows) == 2 and all(row["send_count"] == 2 for row in rows)
        for row in rows:
            attempts = list(app.services.telemetry.store.attempts(row["id"]))
            assert [item["purpose"] for item in attempts] == ["vision", "generation"]
            assert attempts[0]["request_id"] == row["request_id"]
            assert attempts[0]["request_row_id"] == row["id"]
            assert row["provider"] == "main"


async def test_vision_and_main_share_send_budget_and_next_task_can_proceed(
    open_lite, register_channel
):
    sent = []

    def upstream(request):
        sent.append(request.url.host)
        return chat_answer("A blue square.")

    async with open_lite(upstream) as app:
        await setup_vision(app, register_channel, workflow_budget={"maxPhysicalSends": 1})
        blocked = await app.client.post(
            "/v1/chat/completions",
            json=image_request(),
            headers={"x-codex-parent-thread-id": "limited-task"},
        )
        assert blocked.status_code == 429, blocked.text
        assert sent == ["vision.invalid"]
        assert app.services.workflow.view("limited-task")["root"]["active"] == 0
        assert app.services.workflow.view("limited-task")["root"]["sends"] == 1
        assert app.services.milestones.first_success_at() is None
        next_task = await app.client.post(
            "/v1/chat/completions",
            json={
                "model": "main/model-a",
                "messages": [{"role": "user", "content": "Continue without an image."}],
            },
            headers={"x-codex-parent-thread-id": "new-task"},
        )
        assert next_task.status_code == 200
        assert sent == ["vision.invalid", "main.invalid"]


async def test_parent_cancellation_stops_pending_vision_and_releases_work(
    open_lite, register_channel
):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    sent = []

    async def upstream(request):
        sent.append(request.url.host)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with open_lite(upstream) as app:
        await setup_vision(app, register_channel)
        cancellation = asyncio.Event()
        context = RequestContext(
            str(uuid4()), "chat/completions", "main/model-a", False, cancellation
        )

        async def run():
            async with app.services.dispatcher.execute(
                context, image_request(), {"x-codex-parent-thread-id": "cancelled-task"}
            ):
                pytest.fail("A cancelled helper must not produce the parent answer")

        task = asyncio.create_task(run())
        async with asyncio.timeout(3):
            await entered.wait()
            cancellation.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert cancelled.is_set()
        assert sent == ["vision.invalid"]
        assert app.services.workflow.view("cancelled-task")["root"]["active"] == 0
        row = request_rows(app.services)[-1]
        assert row["status"] == 499 and row["send_count"] == 1
        assert app.services.milestones.first_success_at() is None


async def test_portable_compaction_then_continuation_keeps_task_context(
    open_lite, register_channel
):
    sent = []
    failed = True

    def upstream(request):
        body = json.loads(request.content)
        sent.append(body)
        if failed:
            return httpx.Response(503, json={"error": {"message": "temporary outage"}})
        return chat_answer("Checkpoint: blue was selected; next draw the square.")

    async with open_lite(upstream) as app:
        await register_channel(app.client, identity="main")
        body = {"model": "main/model-a", "input": "Select blue, then draw a square."}
        first = await app.client.post("/v1/responses/compact", json=body)
        assert first.status_code == 503, first.text
        failed = False
        compact = await app.client.post("/v1/responses/compact", json=body)
        assert compact.status_code == 200, compact.text
        checkpoint = compact.json()
        assert checkpoint["object"] == "response.compaction"
        assert app.services.milestones.first_success_at() is None
        continued = await app.client.post(
            "/v1/responses",
            json={
                "model": "main/model-a",
                "input": [*checkpoint["output"], {"role": "user", "content": "Continue."}],
            },
        )
        assert continued.status_code == 200, continued.text
        assert "blue was selected" in json.dumps(sent[-1])
        assert "Continue." in json.dumps(sent[-1])
        rows = request_rows(app.services)
        assert len(rows) == 3
        assert [row["send_count"] for row in rows] == [1, 1, 1]
        assert [item["purpose"] for item in app.services.telemetry.store.attempts()] == [
            "compaction",
            "compaction",
            "generation",
        ]
        assert app.services.milestones.first_success_at() is not None


async def test_subagent_recovers_with_next_candidate_without_redirecting_normal_turns(
    open_lite, register_channel
):
    sent = []
    primary_unavailable = True

    def upstream(request):
        sent.append(request.url.host)
        if request.url.host == "main.invalid" and primary_unavailable:
            return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}})
        return httpx.Response(200, json=response_value())

    async with open_lite(upstream) as app:
        for identity in ("main", "backup"):
            await register_channel(app.client, identity=identity, protocol="openai-responses")

        def configure_agents(state):
            state.configuration.features = Features(
                agents=AgentSettings(subagent_model_fallback=["backup/model-a"])
            )

        app.services.store.update(configure_agents)
        body = {"model": "main/model-a", "input": "Complete the delegated task."}
        headers = {
            "x-openai-subagent": "collab_spawn",
            "x-codex-parent-thread-id": "root",
            "thread-id": "child",
        }
        first = await app.client.post("/v1/responses", json=body, headers=headers)
        assert first.status_code == 429
        next_turn = await app.client.post("/v1/responses", json=body, headers=headers)
        assert next_turn.status_code == 200
        primary_unavailable = False
        normal = await app.client.post("/v1/responses", json=body)
        assert normal.status_code == 200
        assert sent == ["main.invalid", "backup.invalid", "main.invalid"]
        assert app.services.workflow.view("root")["root"]["active"] == 0


async def test_search_helper_failure_then_recovery_returns_sources_and_owned_sends(
    open_lite, register_channel
):
    helper_failed = True
    sent = []

    def upstream(request):
        body = json.loads(request.content)
        sent.append((request.url.host, body))
        if request.url.host == "chatgpt.com":
            if helper_failed:
                return httpx.Response(503, json={"error": {"message": "search unavailable"}})
            value = response_value("The sky is blue.\nSources:\n- Sky: https://example.com/sky")
            return httpx.Response(
                200,
                text="data: "
                + json.dumps({"type": "response.completed", "response": value})
                + "\n\n",
                headers={"content-type": "text/event-stream"},
            )
        items = body.get("input", [])
        if any(
            item.get("type") == "function_call_output" for item in items if isinstance(item, dict)
        ):
            return httpx.Response(
                200, json=response_value("Answer based on the available search evidence.")
            )
        return httpx.Response(
            200,
            json=response_value(
                output=[
                    {
                        "type": "function_call",
                        "name": "web_search",
                        "call_id": "search-call",
                        "arguments": json.dumps({"query": "What color is the sky?"}),
                    }
                ]
            ),
        )

    async with open_lite(upstream) as app:
        await register_channel(app.client, identity="main", protocol="openai-responses")
        await register_channel(
            app.client,
            identity="subscription",
            protocol="openai-responses",
            service_id="chatgpt",
            auth_mode="oauth",
            endpoints={"base_url": "https://chatgpt.com/backend-api/codex"},
            oauth_binding={"service_id": "chatgpt", "account_ref": "chatgpt"},
        )
        app.services.auth.save(
            {
                "access": "fake-access",
                "refresh": "",
                "accountId": "search-account",
                "expires": time.time() + 3600,
                "generation": "fake-generation",
                "installationId": str(uuid4()),
            }
        )
        configure(app.services, web_search_sidecar={"enabled": True})
        body = {
            "model": "main/model-a",
            "input": "Look up the sky.",
            "tools": [{"type": "web_search"}],
        }
        for failed in (True, False):
            helper_failed = failed
            response = await app.client.post(
                "/v1/responses", json=body, headers={"x-codex-parent-thread-id": "search-task"}
            )
            assert response.status_code == 200, response.text
            output = response.json()["output"]
            cell = next(item for item in output if item["type"] == "web_search_call")
            assert cell["status"] == ("failed" if failed else "completed")
            if not failed:
                assert cell["action"]["sources"][0]["url"] == "https://example.com/sky"
                assert "https://example.com/sky" in json.dumps(sent[-1][1])
            row = request_rows(app.services)[-1]
            assert row["send_count"] == 3
            attempts = list(app.services.telemetry.store.attempts(row["id"]))
            assert [item["purpose"] for item in attempts] == ["generation", "search", "generation"]
            assert all(item["request_id"] == row["request_id"] for item in attempts)
            assert all(item["request_row_id"] == row["id"] for item in attempts)
            assert app.services.workflow.view("search-task")["root"]["active"] == 0
        assert [host for host, _ in sent] == ["main.invalid", "chatgpt.com", "main.invalid"] * 2
