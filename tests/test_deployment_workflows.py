"""Publish real build artifacts while replacing only host and HTTP boundaries."""

import io
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from backend.runtime import deployment, update

pytestmark = pytest.mark.anyio


def candidate(root, version):
    root.mkdir(parents=True, exist_ok=True)
    (root / "assets").mkdir(exist_ok=True)
    (root / ".vite").mkdir(exist_ok=True)
    entry = f"assets/index-{version * 8}.js"
    lazy = f"assets/models-{version * 8}.js"
    (root / entry).write_text(f"export const version = '{version}';", encoding="utf-8")
    (root / lazy).write_text(f"export const models = '{version}';", encoding="utf-8")
    (root / "index.html").write_text(
        f'<html><script type="module" src="/{entry}"></script></html>', encoding="utf-8"
    )
    (root / ".vite" / "manifest.json").write_text(
        json.dumps(
            {
                "index.html": {"file": entry, "isEntry": True, "dynamicImports": ["models.ts"]},
                "models.ts": {"file": lazy},
            }
        ),
        encoding="utf-8",
    )
    return entry, lazy


@pytest.fixture
def publication(sandbox, monkeypatch):
    state = sandbox / "state"
    web = sandbox / "web"
    build = sandbox / "candidate"
    # All scratch artifacts remain under the repository-root .tmp sandbox.
    scratch = sandbox
    home = sandbox / "codex"
    (home / "config.toml").write_text('model = "keep-native"\n', encoding="utf-8")
    (home / "auth.json").write_text('{"fixture":"not-a-real-account"}', encoding="utf-8")
    (state / "startup-preference.json").write_text('{"enabled":true}', encoding="utf-8")
    protected = {
        path: path.read_bytes()
        for path in (home / "config.toml", home / "auth.json", state / "startup-preference.json")
    }
    runtime = {"state": "stopped", "port": 10231, "pid": 987654, "startedAt": "before"}
    healthy = {"value": True}
    managed = {"value": True}
    restarted = []
    monkeypatch.setattr(deployment, "status", lambda _: dict(runtime))
    monkeypatch.setattr(
        deployment,
        "Startup",
        lambda *args: SimpleNamespace(
            health=lambda: {
                field: managed["value"]
                for field in (
                    "serviceInstalled",
                    "serviceEnabled",
                    "serviceViable",
                    "serviceRunning",
                    "managerRunning",
                )
            }
        ),
    )
    monkeypatch.setattr(deployment, "control_request", lambda *args: {"home": str(home)})

    def restart(directory, port, action):
        assert directory == state and port == 10231 and action == "restart"
        restarted.append(port)
        runtime["startedAt"] = f"restart-{len(restarted)}"

    def open_local(address, **kwargs):
        path = urlsplit(address).path
        if path == "/healthz":
            return io.BytesIO(
                json.dumps(
                    {
                        "status": "ok" if healthy["value"] else "failed",
                        "service": "opencodex-lite",
                        "pid": runtime["pid"],
                        "port": runtime["port"],
                        "consoleReady": True,
                    }
                ).encode()
            )
        return io.BytesIO((web / (path.lstrip("/") or "index.html")).read_bytes())

    monkeypatch.setattr(deployment, "service_command", restart)
    monkeypatch.setattr(deployment, "build_opener", lambda *args: SimpleNamespace(open=open_local))

    def publish():
        return deployment.publish(state, build_root=build, web=web, scratch=scratch)

    return SimpleNamespace(
        state=state,
        web=web,
        build=build,
        scratch=scratch,
        protected=protected,
        runtime=runtime,
        healthy=healthy,
        managed=managed,
        restarted=restarted,
        publish=publish,
    )


async def test_updates_keep_open_tabs_assets_and_prune_only_expired_files(publication, monkeypatch):
    fixture = publication
    now = [1_800_000_000.0]
    monkeypatch.setattr(deployment.time, "time", lambda: now[0])
    old_entry, old_lazy = candidate(fixture.build, "a")
    assert fixture.publish() is False
    assert fixture.restarted == []  # Publishing a stopped instance must not start it.
    opened_index = (fixture.web / "index.html").read_bytes()
    old_page = (fixture.web / old_lazy).read_bytes()
    unrelated = fixture.web / "operator-notes.txt"
    unrelated.write_text("not owned by the publisher", encoding="utf-8")

    # A new Vite output contains only the new generation of hashed resources.
    (fixture.build / old_entry).unlink()
    (fixture.build / old_lazy).unlink()
    candidate(fixture.build, "b")
    fixture.publish()
    assert (fixture.web / "index.html").read_bytes() != opened_index
    assert (fixture.web / old_lazy).read_bytes() == old_page
    assert (fixture.web / old_entry).exists()
    now[0] += 7 * 24 * 60 * 60 + 1
    fixture.publish()
    assert not (fixture.web / old_lazy).exists()
    assert not (fixture.web / old_entry).exists()
    assert unrelated.read_text() == "not owned by the publisher"
    assert {path: path.read_bytes() for path in fixture.protected} == fixture.protected


async def test_incomplete_candidate_does_not_replace_working_console_then_retry_succeeds(
    publication,
):
    fixture = publication
    candidate(fixture.build, "a")
    fixture.publish()
    previous = (fixture.web / "index.html").read_bytes()
    _, lazy = candidate(fixture.build, "b")
    payload = (fixture.build / lazy).read_bytes()
    (fixture.build / lazy).unlink()
    with pytest.raises(ValueError, match="Missing build resource"):
        fixture.publish()
    assert (fixture.web / "index.html").read_bytes() == previous
    assert fixture.restarted == []
    (fixture.build / lazy).write_bytes(payload)
    fixture.publish()
    assert (fixture.web / "index.html").read_bytes() == (fixture.build / "index.html").read_bytes()
    assert {path: path.read_bytes() for path in fixture.protected} == fixture.protected


async def test_service_update_health_failure_restores_old_entry_then_recovers(publication):
    fixture = publication
    _, old_lazy = candidate(fixture.build, "a")
    fixture.publish()
    previous = (fixture.web / "index.html").read_bytes()
    candidate(fixture.build, "b")
    fixture.runtime["state"] = "running"
    fixture.healthy["value"] = False
    with pytest.raises(RuntimeError, match="Previous console index restored") as failure:
        fixture.publish()
    assert "source and dependencies were not rolled back" in str(failure.value)
    assert (fixture.web / "index.html").read_bytes() == previous
    assert (fixture.web / old_lazy).exists()
    assert fixture.restarted == [10231]
    assert {path: path.read_bytes() for path in fixture.protected} == fixture.protected

    fixture.healthy["value"] = True
    assert fixture.publish() is True
    assert fixture.restarted == [10231, 10231]
    assert (fixture.web / "index.html").read_bytes() == (fixture.build / "index.html").read_bytes()
    assert {path: path.read_bytes() for path in fixture.protected} == fixture.protected


async def test_foreground_instance_requires_stop_before_publishing(publication):
    fixture = publication
    candidate(fixture.build, "a")
    fixture.publish()
    previous = (fixture.web / "index.html").read_bytes()
    candidate(fixture.build, "b")
    fixture.runtime["state"] = "running"
    fixture.managed["value"] = False
    with pytest.raises(RuntimeError, match="foreground gateway"):
        fixture.publish()
    assert (fixture.web / "index.html").read_bytes() == previous
    fixture.runtime["state"] = "stopped"
    assert fixture.publish() is False
    assert (fixture.web / "index.html").read_bytes() != previous


@pytest.mark.parametrize("failed_stage", [0, 1, 2], ids=["python-sync", "frontend-sync", "build"])
async def test_failed_update_stage_leaves_console_then_full_retry_publishes(
    publication, monkeypatch, failed_stage
):
    fixture = publication
    candidate(fixture.build, "a")
    fixture.publish()
    previous = (fixture.web / "index.html").read_bytes()
    checkout = fixture.scratch / "checkout"
    checkout.mkdir()
    candidate(checkout / "frontend" / "build", "b")
    commands = []
    failing = [True]
    monkeypatch.setattr(update, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(update, "WEB_ROOT", fixture.web)
    monkeypatch.setattr(update, "_tool", lambda name: name)
    # Keep scratch at the real repository-root .tmp boundary, not a nested fake .tmp.
    lock = update.instance_lock
    publish = deployment.publish

    def build_lock(directory, name):
        assert directory == checkout / ".tmp"
        return lock(fixture.scratch, name)

    def publish_candidate(directory, *, build_root, web, scratch):
        assert scratch == checkout / ".tmp"
        return publish(directory, build_root=build_root, web=web, scratch=fixture.scratch)

    monkeypatch.setattr(update, "instance_lock", build_lock)
    monkeypatch.setattr(deployment, "publish", publish_candidate)

    def run(command, *, cwd, check):
        assert cwd == checkout
        commands.append(command)
        return SimpleNamespace(
            returncode=7 if failing[0] and len(commands) - 1 == failed_stage else 0
        )

    monkeypatch.setattr(update.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="console was not published"):
        update.update(fixture.state)
    assert len(commands) == failed_stage + 1
    assert (fixture.web / "index.html").read_bytes() == previous
    commands.clear()
    failing[0] = False
    update.update(fixture.state)
    assert commands == [
        ["uv", "sync", "--locked"],
        ["pnpm", "--dir", "frontend", "install", "--frozen-lockfile"],
        ["pnpm", "--dir", "frontend", "build:runtime"],
    ]
    assert (fixture.web / "index.html").read_bytes() == (
        checkout / "frontend" / "build" / "index.html"
    ).read_bytes()
    assert fixture.restarted == []
