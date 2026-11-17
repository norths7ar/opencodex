"""Client ownership workflows against real configuration files and journals."""

import json
import tomllib
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from backend.catalog.projection import model_rows
from backend.catalog.view import effective_models
from backend.configuration.models import Channel, Configuration
from backend.configuration.store import ConfigurationStore
from backend.integrations import client_catalog
from backend.integrations.codex import CodexIntegration
from backend.integrations.coordinator import IntegrationCoordinator
from backend.integrations.dsh import DshError, DshIntegration
from backend.integrations.files import ClientConfigError

pytestmark = pytest.mark.anyio


@pytest.fixture
def clients(sandbox: Path):
    """All paths are explicit; this fixture never discovers installed clients."""
    state = sandbox / "state"
    codex_home = sandbox / "codex"
    dsh_home = sandbox / "dsh"
    codex_home.mkdir(exist_ok=True)
    dsh_home.mkdir(exist_ok=True)
    (codex_home / "config.toml").write_text(
        '# Personal settings\nmodel = "native"\n[features]\nmulti_agent_v2 = false\n',
        encoding="utf-8",
    )
    (codex_home / "auth.json").write_bytes(b'{"fixture_login":"leave unchanged"}\n')
    store = ConfigurationStore(state)
    store.install(
        Configuration(
            channels=[
                Channel(
                    id="fixture",
                    name="Fixture",
                    service_id="openai-api",
                    protocol="openai-responses",
                    auth_mode="local",
                    endpoints={"base_url": "https://upstream.invalid/v1"},
                    models={"live": False, "static": ["first"], "default": "first"},
                )
            ],
            default_channel_id="fixture",
        ),
        {},
    )

    def catalog():
        return effective_models(store.read().configuration, lambda _: None)

    return (
        store,
        CodexIntegration(store, state, 10120, codex_home, catalog),
        DshIntegration(state, 10120, lambda: model_rows(catalog()), dsh_home),
    )


def add_model(store):
    def change(state):
        state.configuration.channels[0].models.static.append("second")

    store.update(change)


def read_codex(codex):
    return tomllib.loads((codex.home() / "config.toml").read_text(encoding="utf-8"))


def read_dsh(path, profile=False):
    data = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    if profile:
        return next(row["config"] for row in data if row["id"] == "llm-pi-ai")
    return data["llm-pi-ai"]


async def apply_dsh(dsh, operation="apply", **kwargs):
    block = await dsh.block()
    plan = dsh.preview(operation, block, **kwargs)
    assert plan["canApply"], plan
    result = dsh.mutate(operation, block, plan["fingerprint"], **kwargs)
    assert result["ok"], result
    return result


async def test_codex_apply_catalog_sync_restore_and_stay_disabled(clients):
    store, codex, _ = clients
    config_path = codex.home() / "config.toml"
    auth_path = codex.home() / "auth.json"
    original_auth = auth_path.read_bytes()
    original_config = config_path.read_bytes()
    assert (await codex.sync())["status"] == "skipped"
    assert config_path.read_bytes() == original_config

    receipt = await codex.sync(enable=True)
    assert receipt["ok"]
    applied = read_codex(codex)
    assert applied["model"] == "fixture/first"
    assert applied["openai_base_url"] == "http://127.0.0.1:10120/v1"
    assert applied["features"]["multi_agent_v2"] is False
    assert auth_path.read_bytes() == original_auth

    # An unrelated edit made while connected must survive both synchronization and restore.
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + '\n[personal]\nnote = "keep me"\n',
        encoding="utf-8",
    )
    add_model(store)
    assert (await codex.sync())["ok"]
    catalog_path = Path(receipt["catalogPath"])
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert {"fixture/first", "fixture/second"} <= {model["slug"] for model in catalog["models"]}

    codex.restore()
    restored = read_codex(codex)
    assert restored["model"] == "native"
    assert "openai_base_url" not in restored
    assert restored["personal"] == {"note": "keep me"}
    assert restored["features"]["multi_agent_v2"] is False
    assert not catalog_path.exists()
    assert auth_path.read_bytes() == original_auth
    disabled_config = config_path.read_bytes()
    assert (await codex.sync())["status"] == "skipped"
    await codex.converge()
    assert config_path.read_bytes() == disabled_config
    assert codex.status()["desiredEnabled"] is False


async def test_codex_partial_publish_recovers_and_owned_drift_is_not_overwritten(
    clients, monkeypatch
):
    _, codex, _ = clients
    auth_path = codex.home() / "auth.json"
    original_auth = auth_path.read_bytes()

    def unavailable_cache(*args, **kwargs):
        raise OSError("fixture: client cache temporarily unavailable")

    # Fail at the real filesystem boundary, leaving integration intent/journal handling intact.
    with monkeypatch.context() as patch:
        patch.setattr(client_catalog, "sync_cache", unavailable_cache)
        with pytest.raises(OSError, match="temporarily unavailable"):
            await codex.sync(enable=True)
    assert codex.status()["desiredEnabled"] is True
    assert codex.status()["state"] == "unsafe"
    assert read_codex(codex)["model"] == "fixture/first"

    assert (await codex.converge())["ok"]
    assert codex.status()["state"] == "current"
    config_path = codex.home() / "config.toml"
    managed = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        managed.replace("http://127.0.0.1:10120/v1", "http://127.0.0.1:19999/v1"),
        encoding="utf-8",
    )
    external_edit = config_path.read_bytes()
    with pytest.raises(ClientConfigError):
        await codex.sync()
    with pytest.raises(ClientConfigError):
        codex.restore()
    assert config_path.read_bytes() == external_edit
    assert auth_path.read_bytes() == original_auth

    # Once the owner resolves the conflict, explicit restore finishes without re-enabling.
    config_path.write_text(managed, encoding="utf-8")
    codex.restore()
    assert read_codex(codex)["model"] == "native"
    assert (await codex.sync())["status"] == "skipped"
    assert auth_path.read_bytes() == original_auth


@pytest.mark.parametrize("profile", [False, True], ids=["settings", "desktop-profile"])
async def test_dsh_preview_sync_disable_and_explicit_snapshot_restore(clients, profile):
    store, _, dsh = clients
    legacy = dsh.home / "settings.yaml"
    legacy.write_text(
        "# User comment\nllm-pi-ai:\n  defaultModel: other/model\n"
        "  providers:\n    other:\n      baseURL: https://other.invalid\n",
        encoding="utf-8",
    )
    legacy_before = legacy.read_bytes()
    target = legacy
    if profile:
        target = dsh.home / "profiles" / "desktop" / "cordis.patch.yml"
        target.parent.mkdir(parents=True)
        target.write_text(
            "# User comment\n- id: llm-pi-ai\n  config:\n    defaultModel: other/model\n"
            "    providers:\n      other:\n        baseURL: https://other.invalid\n"
            "- id: unrelated-loader\n  config:\n    enabled: true\n",
            encoding="utf-8",
        )
    original = target.read_bytes()
    block = await dsh.block()
    assert (await dsh.sync())["status"] == "skipped"
    plan = dsh.preview("apply", block)
    assert plan["canApply"]
    assert target.read_bytes() == original  # Preview is read-only.
    applied = dsh.mutate("apply", block, plan["fingerprint"])
    assert applied["ok"]
    assert read_dsh(target, profile)["providers"]["opencodex"]["baseURL"] == (
        "http://127.0.0.1:10120/v1"
    )

    add_model(store)
    assert (await dsh.sync())["ok"]
    config = read_dsh(target, profile)
    assert {model["id"] for model in config["providers"]["opencodex"]["models"]} == {
        "fixture/first",
        "fixture/second",
    }
    assert config["defaultModel"] == "other/model"
    assert config["providers"]["other"] == {"baseURL": "https://other.invalid"}
    assert "# User comment" in target.read_text(encoding="utf-8")
    await apply_dsh(dsh, "disable")
    assert "opencodex" not in read_dsh(target, profile)["providers"]
    disabled = target.read_bytes()
    assert (await dsh.sync())["status"] == "skipped"
    assert target.read_bytes() == disabled

    # Snapshot restore has whole-document semantics, unlike ordinary disable.
    preview = dsh.preview("restore", await dsh.block(), applied["opId"])
    assert preview["refusalReason"] == "drift_requires_confirm"
    await apply_dsh(dsh, "restore", op_id=applied["opId"], confirm_drift=True)
    assert target.read_bytes() == original
    assert (await dsh.sync())["status"] == "skipped"
    if profile:
        assert legacy.read_bytes() == legacy_before


async def test_clients_isolate_conflicts_and_dsh_requires_fresh_review(clients):
    store, codex, dsh = clients
    target = dsh.home / "settings.yaml"
    target.write_text("personal: original\n", encoding="utf-8")
    block = await dsh.block()
    plan = dsh.preview("apply", block)
    target.write_text("personal: edited after preview\n", encoding="utf-8")
    changed = target.read_bytes()
    stale = dsh.mutate("apply", block, plan["fingerprint"])
    assert stale["ok"] is False
    assert stale["code"] == "integration_preview_stale"
    assert target.read_bytes() == changed
    await apply_dsh(dsh)
    await codex.sync(enable=True)

    # Codex ownership conflict must not prevent a healthy DSH from receiving new models.
    codex_path = codex.home() / "config.toml"
    managed_codex = codex_path.read_text(encoding="utf-8")
    codex_path.write_text(
        managed_codex.replace("http://127.0.0.1:10120/v1", "http://127.0.0.1:19999/v1"),
        encoding="utf-8",
    )
    codex_drift = codex_path.read_bytes()
    add_model(store)
    report = await IntegrationCoordinator(codex, dsh).sync()
    assert report["ok"] is False
    assert report["clients"]["codex"]["ok"] is False
    assert report["clients"]["dsh"]["ok"] is True
    assert codex_path.read_bytes() == codex_drift
    assert "fixture/second" in {
        model["id"] for model in read_dsh(target)["providers"]["opencodex"]["models"]
    }

    managed_dsh = target.read_text(encoding="utf-8")
    target.write_text(managed_dsh.replace("OpenCodex Lite", "User edit"), encoding="utf-8")
    dsh_drift = target.read_bytes()
    assert (await dsh.sync())["ok"] is False
    refused = dsh.preview("disable", await dsh.block())
    assert refused["canApply"] is False
    assert refused["refusalReason"] == "conflict"
    with pytest.raises(DshError):
        dsh.mutate("disable", await dsh.block(), refused["fingerprint"])
    assert target.read_bytes() == dsh_drift

    # Explicit overwrite is a new reviewed operation; automatic refresh never does it.
    await apply_dsh(dsh, "overwrite")
    assert read_dsh(target)["providers"]["opencodex"]["displayName"] == "OpenCodex Lite"
    assert YAML(typ="safe").load(target.read_text(encoding="utf-8"))["personal"] == (
        "edited after preview"
    )
    codex_path.write_text(managed_codex, encoding="utf-8")
    assert (await IntegrationCoordinator(codex, dsh).sync())["ok"]
    codex.restore()
    await apply_dsh(dsh, "disable")
