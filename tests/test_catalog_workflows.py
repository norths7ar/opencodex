"""Catalog changes remain consistent with operator settings, clients, and routing."""

import asyncio
import json

import httpx
import pytest

from backend.catalog import refresh as refresh_module

pytestmark = pytest.mark.anyio


def completion(model):
    return httpx.Response(
        200,
        json={
            "id": "catalog-request",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": model},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    )


async def generate(client, selector):
    return await client.post(
        "/v1/chat/completions",
        json={"model": selector, "messages": [{"role": "user", "content": "hello"}]},
    )


async def test_refresh_preserves_overrides_syncs_clients_and_accepts_empty_directory(
    open_lite, register_channel, sandbox
):
    roster = ["model-a"]
    reads = 0

    def upstream(request):
        nonlocal reads
        if request.method == "GET":
            reads += 1
            return httpx.Response(200, json={"data": [{"id": name} for name in roster]})
        model = json.loads(request.content)["model"]
        assert model == "model-a"
        return completion(model)

    async with open_lite(upstream) as app:
        await register_channel(
            app.client,
            models={
                "live": True,
                "static": [],
                "default": "model-a",
                "metadata": {"defaults": {"inputModalities": ["text"]}},
            },
        )
        assert (await app.client.get("/api/models")).json() == []
        assert reads == 0
        assert (await app.client.post("/api/channels/demo/models/refresh")).json()["ok"]
        changed = await app.client.put(
            "/api/model-settings",
            json={
                "model": {"channel_id": "demo", "model_id": "model-a"},
                "changes": {"displayName": "My model", "inputModalities": []},
            },
        )
        assert changed.status_code == 200, changed.text
        assert (
            await app.client.put(
                "/api/channels/demo/model-aliases", json={"set": {"model-a": "chosen"}}
            )
        ).status_code == 200
        target = sandbox / "dsh" / "settings.yaml"
        target.write_text("personal: keep\n", encoding="utf-8")
        block = await app.services.dsh.block()
        preview = app.services.dsh.preview("apply", block)
        assert app.services.dsh.mutate("apply", block, preview["fingerprint"])["ok"]
        roster.append("model-b")
        refreshed = (await app.client.post("/api/channels/demo/models/refresh")).json()
        assert refreshed["catalogRefresh"]["clients"]["dsh"]["ok"]
        assert "model-b" in target.read_text(encoding="utf-8")
        selected = next(row for row in refreshed["models"] if row["id"] == "model-a")
        assert selected["displayName"] == "My model"
        assert selected["inputModalities"] == []
        assert (await generate(app.client, "demo/chosen")).json()["choices"][0]["message"][
            "content"
        ] == "model-a"
        roster.clear()
        empty = (await app.client.post("/api/channels/demo/models/refresh")).json()
        assert empty["ok"] is True and empty["models"] == []
        assert empty["discovery"]["stale_default_model"] == "model-a"
        assert "model-b" not in target.read_text(encoding="utf-8")
        roster.append("model-a")
        restored = (await app.client.post("/api/channels/demo/models/refresh")).json()
        assert restored["models"][0]["displayName"] == "My model"
        assert restored["models"][0]["inputModalities"] == []
        reset = await app.client.put(
            "/api/model-settings",
            json={
                "model": {"channel_id": "demo", "model_id": "model-a"},
                "changes": {"displayName": None, "inputModalities": None},
            },
        )
        assert reset.status_code == 200
        inherited = (await app.client.get("/api/models")).json()[0]
        assert inherited["displayName"] != "My model"
        assert inherited["inputModalities"] == ["text"]
        before = reads
    async with open_lite(upstream) as app:
        assert (await app.client.get("/api/models")).json()[0]["id"] == "model-a"
        assert reads == before
        assert (await generate(app.client, "demo/chosen")).status_code == 200


async def test_daily_refresh_failure_backoff_and_manual_recovery(
    open_lite, register_channel, monkeypatch
):
    now = refresh_module.time.time()
    monkeypatch.setattr(refresh_module.time, "time", lambda: now)
    offline = False
    calls = 0

    def upstream(request):
        nonlocal calls
        calls += 1
        if offline:
            raise httpx.ConnectError("fixture upstream unavailable")
        return httpx.Response(200, json={"data": [{"id": "model-a"}]})

    async with open_lite(upstream) as app:
        await register_channel(app.client, models={"live": True, "static": []})
        await app.services.catalog.refresh_due()
        assert calls == 1
        await app.services.catalog.refresh_due()
        assert calls == 1
        now += 86401
        offline = True
        await app.services.catalog.refresh_due()
        assert calls == 2
        assert (await app.client.get("/api/channels/demo/models/status")).json()[
            "state"
        ] == "failed"
        assert (await app.client.get("/api/models")).json()[0]["id"] == "model-a"
        await app.services.catalog.refresh_due()
        assert calls == 2
        now += 61
        await app.services.catalog.refresh_due()
        assert calls == 3
        now += 61  # A second failure doubles the retry interval.
        await app.services.catalog.refresh_due()
        assert calls == 3
        offline = False
        refreshed = await app.client.post("/api/channels/demo/models/refresh")
        assert refreshed.json()["discovery"]["state"] == "ready"
        assert calls == 4
        await app.services.catalog.refresh_due()
        assert calls == 4


@pytest.mark.parametrize("change", ["replace-connection", "remove-channel"])
async def test_late_directory_cannot_revive_replaced_or_removed_channel(
    open_lite, register_channel, change
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def upstream(request):
        if request.url.host == "demo.invalid":
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"data": [{"id": "stale-model"}]})
        assert request.url.host == "new.invalid"
        return httpx.Response(200, json={"data": [{"id": "current-model"}]})

    async with open_lite(upstream) as app:
        created = await register_channel(app.client, models={"live": True, "static": []})
        async with asyncio.timeout(5):
            task = asyncio.create_task(app.client.post("/api/channels/demo/models/refresh"))
            await entered.wait()
            if change == "replace-connection":
                channel = {
                    **created["channel"],
                    "endpoints": {"base_url": "https://new.invalid/v1"},
                }
                assert (
                    await app.client.put("/api/channels/demo", json={"channel": channel})
                ).status_code == 200
            else:
                await register_channel(
                    app.client, identity="other", models={"live": False, "static": []}
                )
                assert (
                    await app.client.put("/api/channels/default", json={"channel_id": "other"})
                ).status_code == 200
                assert (await app.client.delete("/api/channels/demo")).status_code == 200
            release.set()
            await task
        assert (await app.client.get("/api/models")).json() == []
        if change == "replace-connection":
            current = (await app.client.post("/api/channels/demo/models/refresh")).json()
            assert [row["id"] for row in current["models"]] == ["current-model"]
        else:
            assert (await app.client.get("/api/channels/demo")).status_code == 404


async def test_concurrent_refresh_shares_client_failure_and_retry_recovers(
    open_lite, register_channel, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    roster = ["model-a"]

    async def upstream(request):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"data": [{"id": name} for name in roster]})

    async with open_lite(upstream) as app:
        await register_channel(app.client, models={"live": True, "static": []})
        original = app.services.codex.sync
        original_refresh = app.services.catalog.refresh
        second_entered = asyncio.Event()
        entered_count = 0

        async def coordinated_refresh(*args, **kwargs):
            nonlocal entered_count
            entered_count += 1
            if entered_count == 2:
                second_entered.set()
            return await original_refresh(*args, **kwargs)

        monkeypatch.setattr(app.services.catalog, "refresh", coordinated_refresh)

        async def locked():
            raise OSError("fixture client file unavailable")

        monkeypatch.setattr(app.services.codex, "sync", locked)
        async with asyncio.timeout(5):
            first = asyncio.create_task(app.client.post("/api/channels/demo/models/refresh"))
            await entered.wait()
            second = asyncio.create_task(app.client.post("/api/channels/demo/models/refresh"))
            await second_entered.wait()
            release.set()
            results = [result.json() for result in await asyncio.gather(first, second)]
        assert calls == 1
        assert results[0]["catalogRefresh"] == results[1]["catalogRefresh"]
        assert results[0]["catalogRefresh"]["status"] == "failed"
        assert [row["id"] for row in results[0]["models"]] == ["model-a"]
        monkeypatch.setattr(app.services.codex, "sync", original)
        roster.append("model-b")
        recovered = (await app.client.post("/api/channels/demo/models/refresh")).json()
        assert recovered["ok"]
        assert all(value["ok"] for value in recovered["catalogRefresh"]["clients"].values())


async def test_colliding_model_ids_remain_distinct_after_removal_and_reopen(
    open_lite, register_channel
):
    sent = []

    def upstream(request):
        model = json.loads(request.content)["model"]
        sent.append(model)
        return completion(model)

    async with open_lite(upstream) as app:
        await register_channel(
            app.client, models={"live": False, "static": ["vendor/model", "vendor-model"]}
        )
        rows = (await app.client.get("/api/models")).json()
        selectors = {row["id"]: row["namespaced"] for row in rows}
        assert len(set(selectors.values())) == 2
        for identity, selector in selectors.items():
            assert (await generate(app.client, selector)).status_code == 200
            assert sent[-1] == identity
        assert (await generate(app.client, "demo/vendor-model")).status_code == 400
        detail = (await app.client.get("/api/channels/demo")).json()
        detail["channel"]["models"]["static"] = ["vendor-model"]
        assert (
            await app.client.put("/api/channels/demo", json={"channel": detail["channel"]})
        ).status_code == 200
    async with open_lite(upstream) as app:
        assert (await generate(app.client, "demo/vendor-model")).status_code == 400
        assert (await generate(app.client, selectors["vendor-model"])).status_code == 200
        assert sent[-1] == "vendor-model"
