"""First startup, reviewed conversion, and explicit original-provider import."""

import json

import httpx
import pytest

from backend.configuration.activation import activate
from backend.configuration.snapshot import prepare_snapshot
from backend.configuration.store import ConfigurationStore

pytestmark = pytest.mark.anyio


def legacy():
    return {
        "providers": [
            {
                "id": "demo",
                "name": "Old API",
                "authMode": "key",
                "apiKeyEnv": "LITE_MIGRATION_KEY",
                "baseUrl": "https://demo.invalid/v1",
                "adapter": "openai-chat",
                "models": ["model-a"],
                "liveModels": False,
                "defaultModel": "model-a",
            }
        ],
        "defaultProvider": "demo",
    }


async def test_first_start_then_migrate_once_and_continue_calling(sandbox, open_lite, monkeypatch):
    state_dir = sandbox / "migration"
    state_dir.mkdir()
    source = state_dir / "config.json"
    source.write_text(json.dumps(legacy()), encoding="utf-8")
    original = source.read_bytes()
    monkeypatch.setenv("LITE_MIGRATION_KEY", "migration-fixture-only")
    sent = []

    def upstream(request):
        sent.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer migration-fixture-only"
        assert sent[-1]["model"] == "model-a"
        return httpx.Response(
            200,
            json={
                "id": "converted",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ready"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    async with open_lite(upstream, directory=state_dir) as app:
        listing = (await app.client.get("/api/channels")).json()
        assert listing["default_channel_id"] == "demo"
        assert listing["channels"][0]["credential"]["state"] == "configured"
        channel = listing["channels"][0]["channel"]
        channel["name"] = "Edited in Lite"
        assert (
            await app.client.put("/api/channels/demo", json={"channel": channel})
        ).status_code == 200
        response = await app.client.post(
            "/v1/chat/completions",
            json={"model": "demo/model-a", "messages": [{"role": "user", "content": "hello"}]},
        )
        assert response.status_code == 200
    assert source.read_bytes() == original
    # The old file is no longer authoritative, even if it becomes invalid.
    source.write_text("broken old file", encoding="utf-8")
    async with open_lite(upstream, directory=state_dir) as app:
        assert (await app.client.get("/api/channels/demo")).json()["channel"][
            "name"
        ] == "Edited in Lite"
        assert (
            await app.client.post(
                "/v1/chat/completions",
                json={"model": "demo/model-a", "messages": [{"role": "user", "content": "again"}]},
            )
        ).status_code == 200
    assert len(sent) == 2


@pytest.mark.parametrize("failure", ["unknown", "invalid-json", "plaintext-key"])
async def test_failed_activation_preserves_source_and_can_be_corrected(sandbox, failure):
    directory = sandbox / "activation"
    directory.mkdir()
    source = directory / "config.json"
    raw = legacy()
    if failure == "unknown":
        raw["unmappedSetting"] = {"secret": "must-not-be-logged"}
    elif failure == "plaintext-key":
        raw["providers"][0]["apiKey"] = "plaintext-fixture-only"
    source.write_text("{broken" if failure == "invalid-json" else json.dumps(raw), encoding="utf-8")
    before = source.read_bytes()
    store = ConfigurationStore(directory)
    with pytest.raises(ValueError):
        activate(store)
    assert source.read_bytes() == before
    assert store.read().revision == 0
    assert store.read().credentials == {}
    source.write_text(json.dumps(legacy()), encoding="utf-8")
    activated = activate(store)
    assert activated.revision == 1
    assert activated.configuration.channels[0].id == "demo"
    assert activated.credentials["demo"].env_var == "LITE_MIGRATION_KEY"
    assert activate(store).revision == 1


async def test_review_export_is_redacted_and_retired_features_are_not_activated(sandbox):
    raw = legacy()
    raw["providers"].append({"id": "anthropic", "authMode": "oauth"})
    raw["multiAgentGuidanceEnabled"] = True
    raw["providers"][0]["headers"] = {"x-fixture-secret": "private-value"}
    raw["unknown"] = "unmapped-private-value"
    source = sandbox / "review-source.json"
    source.write_text(json.dumps(raw), encoding="utf-8")
    before = source.read_bytes()
    report_dir = sandbox / "redacted"
    summary = prepare_snapshot(source, report_dir)
    assert summary["activated"] is False
    assert summary["mapping_complete"] is False
    assert summary["channels"] == ["demo"]
    assert "/providers/1" in summary["retired_fields"]
    assert "/multiAgentGuidanceEnabled" in summary["retired_fields"]
    assert {path.name for path in report_dir.iterdir()} == {"migration-report.json"}
    assert "private-value" not in (report_dir / "migration-report.json").read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        prepare_snapshot(source, report_dir)
    candidate = sandbox / "explicit-sensitive"
    prepare_snapshot(source, candidate, include_sensitive=True)
    assert "private-value" in (candidate / "credentials.json").read_text(encoding="utf-8")
    assert source.read_bytes() == before
    assert not (candidate / "configuration.sqlite3").exists()
    raw.pop("unknown")
    source.write_text(json.dumps(raw), encoding="utf-8")
    directory = sandbox / "retired-activation"
    directory.mkdir()
    (directory / "config.json").write_bytes(source.read_bytes())
    assert [
        channel.id for channel in activate(ConfigurationStore(directory)).configuration.channels
    ] == ["demo"]


async def test_explicit_import_requires_fresh_review_and_preserves_existing_channels(
    sandbox, open_lite, register_channel
):
    source = sandbox / "original.json"
    raw = {
        "providers": {
            "deepseek": {
                "baseUrl": "https://api.deepseek.com/v1",
                "adapter": "openai-chat",
                "apiKey": "import-fixture-secret",
                "models": ["model-a"],
                "liveModels": False,
            },
            "chatgpt": {"authMode": "oauth"},
        }
    }
    source.write_text(json.dumps(raw), encoding="utf-8")
    async with open_lite(lambda request: pytest.fail("Import must not contact an upstream")) as app:
        await register_channel(app.client)
        request = {"source": str(source)}
        preview = (await app.client.post("/api/provider-import/preview", json=request)).json()
        assert "import-fixture-secret" not in json.dumps(preview)
        assert {row["id"]: row["status"] for row in preview["providers"]} == {
            "deepseek": "ready",
            "chatgpt": "pending",
        }
        assert len((await app.client.get("/api/channels")).json()["channels"]) == 1
        raw["providers"]["deepseek"]["name"] = "Reviewed later"
        source.write_text(json.dumps(raw), encoding="utf-8")
        stale = await app.client.post(
            "/api/provider-import/apply",
            json={**request, "digest": preview["digest"], "selected": ["deepseek"]},
        )
        assert stale.status_code == 409
        fresh = (await app.client.post("/api/provider-import/preview", json=request)).json()
        denied = await app.client.post(
            "/api/provider-import/apply",
            json={**request, "digest": fresh["digest"], "selected": ["deepseek", "chatgpt"]},
        )
        assert denied.status_code == 409
        assert len((await app.client.get("/api/channels")).json()["channels"]) == 1
        applied = await app.client.post(
            "/api/provider-import/apply",
            json={**request, "digest": fresh["digest"], "selected": ["deepseek"]},
        )
        assert applied.status_code == 200, applied.text
        listing = (await app.client.get("/api/channels")).json()
        assert {item["channel"]["id"] for item in listing["channels"]} == {"demo", "deepseek"}
        assert listing["default_channel_id"] == "demo"
        again = (await app.client.post("/api/provider-import/preview", json=request)).json()
        assert (
            next(row for row in again["providers"] if row["id"] == "deepseek")["status"] == "exists"
        )
        assert json.loads(source.read_text(encoding="utf-8")) == raw
