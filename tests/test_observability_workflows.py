"""Users can interpret request history and clean only reviewed disposable data."""

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.anyio


def reply(usage=None):
    body = {
        "id": "usage-fixture",
        "object": "chat.completion",
        "model": "model-a",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


async def call(client, model="demo/model-a", origin="dsh"):
    return await client.post(
        "/v1/chat/completions",
        headers={"originator": origin},
        json={"model": model, "messages": [{"role": "user", "content": "hello"}]},
    )


async def test_usage_distinguishes_zero_missing_partial_and_unpriced_calls(
    open_lite, register_channel
):
    usage = {"prompt_tokens": 10, "completion_tokens": 5}
    async with open_lite(lambda request: reply(usage)) as app:
        await register_channel(app.client)
        await register_channel(app.client, identity="other")
        assert (await call(app.client)).status_code == 200
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        assert (await call(app.client)).status_code == 200
        usage = None
        assert (await call(app.client, "other/model-a", "codex_cli_rs")).status_code == 200
        usage = {"prompt_tokens": 10}  # Partial is not a fabricated complete zero.
        assert (await call(app.client)).status_code == 200
        logs = (await app.client.get("/api/logs")).json()["logs"]
        assert len(logs) == 4
        assert [row["usageStatus"] for row in logs] == [
            "reported",
            "reported",
            "unreported",
            "unreported",
        ]
        assert logs[0]["totalTokens"] == 15
        assert logs[1]["totalTokens"] == 0
        assert "totalTokens" not in logs[2] and "totalTokens" not in logs[3]
        assert all(row["displayMetrics"]["cost"]["kind"] != "value" for row in logs)
        usage_view = (await app.client.get("/api/usage?range=all")).json()
        summary = usage_view["summary"]
        assert summary["requests"] == 4
        assert summary["reportedRequests"] == 2
        assert summary["unreportedRequests"] == 2
        assert summary["totalTokens"] == 15
        assert summary["pricedRequests"] == 0
        assert summary["sends"] == 4
        filtered = (await app.client.get("/api/usage?range=all&provider=other")).json()
        assert filtered["summary"]["requests"] == 1
        client_only = (await app.client.get("/api/usage?client=dsh&device=all")).json()
        assert client_only["summary"]["requests"] == 3
        provider_logs = (await app.client.get("/api/logs?provider=other&model=model-a")).json()[
            "logs"
        ]
        assert len(provider_logs) == 1 and provider_logs[0]["client"] == "codex_cli"
        empty = (await app.client.get("/api/usage?since=0&until=1")).json()
        assert empty["summary"]["requests"] == 0
        assert (await app.client.get("/api/usage?since=invalid&until=1")).status_code == 400
        assert (await app.client.get("/api/usage?range=all")).json()["summary"]["requests"] == 4
        # A price overlay makes existing measured requests explainable without inventing usage.
        cost = await app.client.put(
            "/api/model-settings",
            json={
                "model": {"channel_id": "demo", "model_id": "model-a"},
                "changes": {},
                "cost": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0},
            },
        )
        assert cost.status_code == 200, cost.text
        priced = (await app.client.get("/api/logs?provider=demo")).json()["logs"]
        assert priced[0]["displayMetrics"]["cost"]["kind"] == "value"
        assert priced[0]["displayMetrics"]["cost"]["estimate"]["cost"]["total"] == pytest.approx(
            20 / 1_000_000
        )
        assert priced[1]["displayMetrics"]["cost"]["estimate"]["cost"]["total"] == 0
        assert priced[2]["displayMetrics"]["cost"]["kind"] != "value"


async def test_query_failure_is_visible_and_retry_recovers_without_losing_history(
    open_lite, register_channel, monkeypatch
):
    async with open_lite(
        lambda request: reply({"prompt_tokens": 1, "completion_tokens": 2})
    ) as app:
        await register_channel(app.client)
        assert (await call(app.client)).status_code == 200
        original = app.services.telemetry.store.rows

        def busy(**kwargs):
            raise sqlite3.OperationalError("fixture database busy")

        monkeypatch.setattr(app.services.telemetry.store, "rows", busy)
        failed = await app.client.get("/api/logs")
        assert failed.status_code == 500
        assert failed.json()["error"]["code"] == "read_failed"
        monkeypatch.setattr(app.services.telemetry.store, "rows", original)
        restored = (await app.client.get("/api/logs")).json()["logs"]
        assert len(restored) == 1 and restored[0]["totalTokens"] == 3
        assert (await call(app.client)).status_code == 200
        assert len((await app.client.get("/api/logs")).json()["logs"]) == 2


async def test_storage_review_cleanup_conflict_and_restore_preserve_working_install(
    open_lite, register_channel, sandbox, monkeypatch
):
    directory = sandbox / "state"
    async with open_lite(
        lambda request: reply({"prompt_tokens": 1, "completion_tokens": 2})
    ) as app:
        await register_channel(app.client)
        assert (await call(app.client)).status_code == 200
        milestone = app.services.milestones.first_success_at()
        protected = {
            "chatgpt-account.json": b'{"fixture":"not a real account"}',
            "startup-preference.json": b'{"enabled":false}',
            "runtime.log": b"keep log",
            "unrelated.txt": b"keep unrelated",
        }
        for name, value in protected.items():
            (directory / name).write_bytes(value)
        # Avoid treating this deliberately non-account fixture as an active grant.
        # Storage never parses it, and generation uses an independent local channel.
        cache = directory / "chatgpt-quota.json"
        cache.write_text('{"quota":"old"}', encoding="utf-8")
        preview = (await app.client.get("/api/lite-storage/preview")).json()
        assert [row["relPath"] for row in preview["candidates"]] == [cache.name]
        cache.write_text('{"quota":"new"}', encoding="utf-8")
        stale = await app.client.post(
            "/api/lite-storage/quarantine", json={"digest": preview["digest"]}
        )
        assert stale.status_code == 409 and cache.exists()
        fresh = (await app.client.get("/api/lite-storage/preview")).json()
        original_rename = Path.rename

        def locked(path, target):
            if path == cache:
                raise PermissionError("fixture cache locked")
            return original_rename(path, target)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "rename", locked)
            failed = (
                await app.client.post(
                    "/api/lite-storage/quarantine", json={"digest": fresh["digest"]}
                )
            ).json()
        assert failed["ok"] is False and failed["count"] == 0
        assert cache.exists()
        cleaned = (
            await app.client.post("/api/lite-storage/quarantine", json={"digest": fresh["digest"]})
        ).json()
        assert cleaned["ok"] and cleaned["count"] == 1
        assert not cache.exists()
        cache.write_text('{"quota":"newer current data"}', encoding="utf-8")
        refused = (await app.client.post(f"/api/lite-storage/restore/{cleaned['id']}")).json()
        assert refused["ok"] is False
        assert json.loads(cache.read_text(encoding="utf-8"))["quota"] == "newer current data"
        cache.unlink()  # The user explicitly removes the conflicting disposable cache.
        restored = (await app.client.post(f"/api/lite-storage/restore/{cleaned['id']}")).json()
        assert restored["ok"] and restored["count"] == 1
        assert json.loads(cache.read_text(encoding="utf-8"))["quota"] == "new"
        for name, value in protected.items():
            assert (directory / name).read_bytes() == value
        assert app.services.milestones.first_success_at() == milestone
        assert (await app.client.get("/api/channels/demo")).status_code == 200
        assert (await call(app.client)).status_code == 200
        assert len((await app.client.get("/api/logs")).json()["logs"]) == 2
