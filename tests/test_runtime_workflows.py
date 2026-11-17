"""Runtime operations with real orchestration and isolated scheduler/process boundaries."""

import json
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from backend.api import service_routes
from backend.runtime import cli, process, server, service
from backend.runtime.scheduler import Startup

pytestmark = pytest.mark.anyio


@pytest.fixture
def managed_runtime(sandbox, monkeypatch):
    directory = sandbox / "state"
    events = []
    runtime = {"state": "stopped", "port": 10231, "pid": 987654}
    registration = {
        "installed": False,
        "enabled": False,
        "stale": False,
        "conflict": False,
        "port": 10231,
        "xml": "",
    }
    manager = Startup(directory, 10231, sid="S-1-5-21-123-456-789-1001")
    fail = {"run": False}

    def command(arguments):
        verb = arguments[0]
        events.append(verb)
        if verb == "/create":
            registration.update(
                installed=True,
                enabled=True,
                stale=False,
                xml=(directory / "startup-task.xml").read_text(encoding="utf-16"),
            )
        elif verb == "/change":
            registration["enabled"] = arguments[-1] == "/enable"
        elif verb == "/run":
            if fail["run"]:
                fail["run"] = False
                raise RuntimeError("fixture: task failed to launch")
            runtime["state"] = "running"
        elif verb == "/delete":
            registration.update(installed=False, enabled=False, xml="")
        else:
            raise AssertionError(arguments)

    def health():
        enabled = json.loads((directory / "startup-preference.json").read_text())["enabled"]
        return {
            "status": "protected" if runtime["state"] == "running" else "stopped",
            "taskName": manager.task_name,
            "runtimeState": runtime["state"],
            "autostartEnabled": enabled,
        }

    def stop(_):
        events.append("stop")
        runtime["state"] = "stopped"
        return dict(runtime)

    monkeypatch.setattr(manager, "registration", lambda: dict(registration))
    monkeypatch.setattr(manager, "_command", command)
    monkeypatch.setattr(manager, "preflight", lambda: None)
    monkeypatch.setattr(manager, "environment_changed", lambda: False)
    monkeypatch.setattr(manager, "health", health)
    monkeypatch.setattr(manager, "supervisor_running", lambda: runtime["state"] == "running")
    monkeypatch.setattr(manager, "wait_idle", lambda: events.append("idle"))
    monkeypatch.setattr(service, "Startup", lambda *args: manager)
    monkeypatch.setattr(service, "status", lambda _: dict(runtime))
    monkeypatch.setattr(service, "stop", stop)
    monkeypatch.setattr(
        service, "wait_supervisor_stopped", lambda _: events.append("supervisor-exit")
    )
    return SimpleNamespace(
        directory=directory,
        manager=manager,
        runtime=runtime,
        registration=registration,
        events=events,
        fail=fail,
    )


async def test_foreground_start_stop_restart_and_busy_recovery(sandbox, monkeypatch):
    directory = sandbox / "state"
    runtime = {"state": "stopped", "port": 10231, "pid": 987654}
    launches = []
    requests = []

    def serve(target, port):
        assert target == directory
        launches.append(port)
        runtime.update(state="running", port=port)

    def control(state, route, *, method):
        requests.append((route, method))
        runtime["state"] = "stopped"

    monkeypatch.setattr(server, "serve", serve)
    monkeypatch.setattr(process, "status", lambda _: dict(runtime))
    monkeypatch.setattr(process, "read_state", lambda _: dict(runtime))
    monkeypatch.setattr(process, "control_request", control)
    monkeypatch.setattr(process, "process_alive", lambda _: False)
    process.run_foreground(directory, None)
    assert launches == [10231]
    with pytest.raises(RuntimeError, match="already running"):
        process.run_foreground(directory, None)
    assert launches == [10231]
    process.run_foreground(directory, None, restart=True)
    assert launches == [10231, 10231]
    assert requests == [("/api/stop", "POST")]
    assert process.stop(directory)["state"] == "stopped"
    process.run_foreground(directory, 10232)
    assert launches[-1] == 10232

    runtime["state"] = "unresponsive"
    before = len(requests)
    with pytest.raises(RuntimeError, match="no process was forcibly terminated"):
        process.stop(directory)
    assert len(requests) == before
    runtime["state"] = "running"
    assert process.stop(directory)["state"] == "stopped"


async def test_service_install_disable_repair_enable_and_remove(managed_runtime):
    fixture = managed_runtime
    directory = fixture.directory
    result = service.service_command(directory, None, "install")
    assert result["ok"] and fixture.runtime["state"] == "running"
    preference = directory / "startup-preference.json"
    assert json.loads(preference.read_text())["enabled"] is True

    service.service_command(directory, None, "disable")
    disabled = preference.read_bytes()
    fixture.registration["stale"] = True
    fixture.events.clear()
    service.service_command(directory, None, "repair")
    assert preference.read_bytes() == disabled
    assert fixture.registration["enabled"] is False
    assert "/run" not in fixture.events
    with pytest.raises(ValueError, match="disabled"):
        service.service_command(directory, None, "restart")

    service.service_command(directory, None, "enable")
    assert fixture.registration["enabled"] is True
    assert json.loads(preference.read_text())["enabled"] is True
    fixture.events.clear()
    service.service_command(directory, None, "remove")
    assert fixture.events == ["stop", "supervisor-exit", "idle", "/delete"]
    assert fixture.runtime["state"] == "stopped"
    assert fixture.registration["installed"] is False
    assert not (directory / "startup-task.xml").exists()
    assert json.loads(preference.read_text())["enabled"] is False


async def test_failed_service_repair_restores_registration_and_can_retry(managed_runtime):
    fixture = managed_runtime
    directory = fixture.directory
    service.service_command(directory, None, "install")
    original_xml = fixture.registration["xml"]
    protected = {
        name: (directory / name).read_bytes()
        for name in ("startup-task.xml", "startup-preference.json")
    }
    fixture.registration["stale"] = True
    fixture.fail["run"] = True
    with pytest.raises(RuntimeError, match="previous registration and runtime restored"):
        service.service_command(directory, None, "repair")
    assert fixture.runtime["state"] == "running"
    assert fixture.registration["enabled"] is True
    assert fixture.registration["xml"] == original_xml
    assert {name: (directory / name).read_bytes() for name in protected} == protected
    result = service.service_command(directory, None, "repair")
    assert result["ok"]
    assert fixture.runtime["port"] == 10231


async def test_failed_first_install_removes_partial_registration_and_can_retry(managed_runtime):
    fixture = managed_runtime
    fixture.fail["run"] = True
    with pytest.raises(RuntimeError, match="previous registration and runtime restored"):
        service.service_command(fixture.directory, None, "install")
    assert fixture.runtime["state"] == "stopped"
    assert fixture.registration["installed"] is False
    assert not (fixture.directory / "startup-task.xml").exists()
    assert not (fixture.directory / "startup-preference.json").exists()
    assert service.service_command(fixture.directory, None, "install")["ok"]
    assert fixture.runtime["state"] == "running"
    assert fixture.registration["installed"] is True


async def test_web_service_action_has_durable_failure_then_cli_completion(
    managed_runtime, monkeypatch
):
    fixture = managed_runtime
    directory = fixture.directory
    service.service_command(directory, None, "install")
    app = FastAPI()
    app.include_router(service_routes.service_router(directory, 10231, startup=fixture.manager))

    def unavailable(*args, **kwargs):
        raise OSError("fixture: detached launcher unavailable")

    monkeypatch.setattr(service.subprocess, "Popen", unavailable)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/api/startup-action", json={"action": "restart"})
    assert response.status_code == 202
    receipt = response.json()
    operation = json.loads((directory / "service-operation.json").read_text())
    assert operation["id"] == receipt["operationId"]
    assert operation["state"] == "failed"
    assert "launcher unavailable" in operation["error"]
    assert fixture.runtime["state"] == "running"

    # The detached worker resumes through the actual CLI, not the HTTP process.
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ocx",
            "service",
            "restart",
            "--state-dir",
            str(directory),
            "--operation-id",
            receipt["operationId"],
        ],
    )
    cli.main()
    completed = json.loads((directory / "service-operation.json").read_text())
    assert completed["id"] == receipt["operationId"]
    assert completed["state"] == "succeeded"
    assert "error" not in completed
    assert fixture.runtime["state"] == "running"
