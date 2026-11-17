"""Configure a connection, use it, correct failures, and reopen persisted state."""

import json

import httpx
import pytest

pytestmark = pytest.mark.anyio


def answer():
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "model-a",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ready"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
        },
    )


async def generate(client):
    return await client.post(
        "/v1/chat/completions",
        json={"model": "demo/model-a", "messages": [{"role": "user", "content": "hello"}]},
    )


async def test_credentials_discovery_generation_and_reopen(
    open_lite, register_channel, monkeypatch
):
    requests = []
    discovery_fails = False
    expected_key = "fixture-key-one"
    monkeypatch.setenv("LITE_WORKFLOW_KEY", expected_key)

    def upstream(request):
        requests.append(request)
        assert request.url.host == "demo.invalid"
        if request.headers.get("authorization") != f"Bearer {expected_key}":
            return httpx.Response(401, json={"error": {"message": "invalid test key"}})
        if request.url.path == "/v1/models":
            if discovery_fails:
                return httpx.Response(503, json={"error": {"message": "directory offline"}})
            return httpx.Response(200, json={"data": [{"id": "model-a"}]})
        assert request.url.path == "/v1/chat/completions"
        assert json.loads(request.content)["model"] == "model-a"
        return answer()

    async with open_lite(upstream) as app:
        created = await register_channel(app.client, auth_mode="key")
        assert created["credential"]["state"] == "missing"
        channel = created["channel"]
        saved = await app.client.put(
            "/api/channels/demo",
            json={
                "channel": channel,
                "credential": {"action": "set_environment", "variable": "LITE_WORKFLOW_KEY"},
            },
        )
        assert saved.status_code == 200, saved.text
        channel = saved.json()["channel"]
        channel["models"]["live"] = True
        channel["name"] = "Edited without replacing credential"
        kept = await app.client.put("/api/channels/demo", json={"channel": channel})
        assert kept.json()["credential"]["state"] == "configured"

        refreshed = await app.client.post("/api/channels/demo/models/refresh")
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["discovery"]["state"] == "ready"
        models = (await app.client.get("/v1/models")).json()["data"]
        assert "demo/model-a" in {model["id"] for model in models}
        response = await generate(app.client)
        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["content"] == "ready"

        discovery_fails = True
        failed = await app.client.post("/api/channels/demo/models/refresh")
        assert failed.json()["discovery"]["state"] == "failed", failed.text
        assert (await generate(app.client)).status_code == 200
        discovery_fails = False
        assert (await app.client.post("/api/channels/demo/models/refresh")).json()["discovery"][
            "state"
        ] == "ready"

        # Replace the upstream key: old credentials fail, an explicit edit recovers.
        expected_key = "fixture-key-two"
        assert (await generate(app.client)).status_code == 401
        detail = (await app.client.get("/api/channels/demo")).json()
        replaced = await app.client.put(
            "/api/channels/demo",
            json={
                "channel": detail["channel"],
                "credential": {"action": "set_key", "value": expected_key},
            },
        )
        assert replaced.status_code == 200, replaced.text
        assert expected_key not in replaced.text
        assert (await generate(app.client)).status_code == 200
        public = await app.client.get("/api/channels/configuration")
        assert expected_key not in public.text

        cleared = await app.client.put(
            "/api/channels/demo",
            json={"channel": detail["channel"], "credential": {"action": "clear"}},
        )
        assert cleared.json()["credential"]["state"] == "missing"
        count = len(requests)
        assert (await generate(app.client)).status_code == 400
        assert len(requests) == count  # Missing credentials never leave the gateway.
        monkeypatch.setenv("LITE_WORKFLOW_KEY", expected_key)
        restored = await app.client.put(
            "/api/channels/demo",
            json={
                "channel": detail["channel"],
                "credential": {"action": "set_environment", "variable": "LITE_WORKFLOW_KEY"},
            },
        )
        assert restored.json()["saved"] is True
        assert (await generate(app.client)).status_code == 200

    async with open_lite(upstream) as reopened:
        detail = (await reopened.client.get("/api/channels/demo")).json()
        assert detail["channel"]["name"] == "Edited without replacing credential"
        assert detail["credential"]["state"] == "configured"
        assert (await generate(reopened.client)).status_code == 200
        logs = (await reopened.client.get("/api/logs")).json()
        rows = logs["logs"]
        assert len(rows) == 7
        assert sum(row["status"] == 200 for row in rows) == 5
        assert sum(row["status"] == 401 for row in rows) == 1
        assert sum(row["status"] == 400 for row in rows) == 1
        assert all(
            row["provider"] == "demo" and row["model"] == "model-a"
            for row in rows
            if row["status"] != 400
        )
        refused = next(row for row in rows if row["status"] == 400)
        assert refused["requestedModel"] == "demo/model-a"
        assert all(row["totalTokens"] == 10 for row in rows if row["status"] == 200)
        assert reopened.services.milestones.first_success_at() is not None


async def test_invalid_and_stale_edits_preserve_saved_connection(open_lite, register_channel):
    async with open_lite(lambda request: answer()) as app:
        created = await register_channel(app.client)
        original_revision = created["revision"]
        channel = created["channel"]
        invalid = {**channel, "endpoints": {"base_url": "not-a-url"}}
        rejected = await app.client.put("/api/channels/demo", json={"channel": invalid})
        assert rejected.status_code == 422
        assert (await app.client.get("/api/channels/demo")).json()["revision"] == original_revision
        assert (await generate(app.client)).status_code == 200

        edited = {**channel, "name": "Saved by the other editor"}
        accepted = await app.client.put(
            "/api/channels/demo", params={"revision": original_revision}, json={"channel": edited}
        )
        assert accepted.status_code == 200, accepted.text
        stale = await app.client.put(
            "/api/channels/demo", params={"revision": original_revision}, json={"channel": channel}
        )
        assert stale.status_code == 409
        current = (await app.client.get("/api/channels/demo")).json()
        assert current["channel"]["name"] == edited["name"]
        current["channel"]["name"] = "Resolved after rereading"
        recovered = await app.client.put(
            "/api/channels/demo",
            params={"revision": current["revision"]},
            json={"channel": current["channel"]},
        )
        assert recovered.status_code == 200
        assert (await generate(app.client)).status_code == 200


async def test_saved_configuration_survives_client_apply_failure(
    open_lite, register_channel, monkeypatch
):
    async with open_lite(lambda request: answer()) as app:
        created = await register_channel(app.client)
        original_sync = app.services.codex.sync

        async def unavailable():
            raise OSError("test client file temporarily unavailable")

        monkeypatch.setattr(app.services.codex, "sync", unavailable)
        channel = {**created["channel"], "name": "Saved despite client failure"}
        saved = await app.client.put("/api/channels/demo", json={"channel": channel})
        result = saved.json()
        assert saved.status_code == 200, saved.text
        assert result["saved"] is True
        assert result["application"]["status"] == "failed"
        assert result["application"]["clients"]["codex"]["ok"] is False
        assert result["application"]["clients"]["dsh"]["ok"] is True
        assert (await app.client.get("/api/channels/demo")).json()["channel"]["name"] == channel[
            "name"
        ]
        assert (await generate(app.client)).status_code == 200

        monkeypatch.setattr(app.services.codex, "sync", original_sync)
        recovered = await app.client.put("/api/channels/demo", json={"channel": channel})
        assert recovered.json()["application"]["status"] == "skipped"
        assert all(item["ok"] for item in recovered.json()["application"]["clients"].values())
