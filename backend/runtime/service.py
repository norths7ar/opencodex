"""CLI service verbs over the shared Windows startup manager."""

import json
import subprocess
import sys
import time
from pathlib import Path

from backend.platform.files import instance_lock

from .process import start_background, stop, wait_supervisor_stopped
from .scheduler import Startup
from .state import DEFAULT_PORT, read_state, status


def record_operation(directory, identity, action, state, error=None):
    path = directory / "service-operation.json"
    value = {"id": identity, "action": action, "state": state, "updatedAt": time.time()}
    if error:
        value["error"] = error
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def launch_service_command(directory, port, action, identity):
    """Continue a web-requested shutdown outside the process being stopped."""
    try:
        with (directory / "startup.log").open("ab") as log:
            subprocess.Popen(
                [
                    str(Path(sys.executable).with_name("pythonw.exe")),
                    "-m",
                    "backend.runtime",
                    "service",
                    action,
                    "--state-dir",
                    str(directory),
                    "--port",
                    str(port),
                    "--operation-id",
                    identity,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
    except OSError as exc:
        record_operation(directory, identity, action, "failed", str(exc))


def wait_serving(directory, port):
    deadline = time.monotonic() + 30
    while True:
        current = status(directory)
        if current["state"] == "running":
            if current["port"] != port:
                raise RuntimeError(
                    f"Gateway is running on port {current['port']}, but the service uses port {port}."
                )
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "The service is registered but the gateway is not responding; inspect startup.log."
            )
        time.sleep(0.1)


def _operation(startup, registration, action):
    if registration["conflict"]:
        raise RuntimeError("A different task occupies the Lite service name.")
    action = action or ("repair" if registration["installed"] else "install")
    if action == "uninstall":
        action = "remove"
    if action not in {
        "install",
        "repair",
        "start",
        "stop",
        "restart",
        "enable",
        "disable",
        "remove",
    }:
        raise ValueError("Unknown service action.")
    if action in {"start", "restart", "repair"} and not registration["installed"]:
        raise ValueError("Service is not installed; run ocx service install.")
    if action in {"start", "restart"} and not registration["enabled"]:
        raise ValueError("Service is disabled; run ocx service enable.")
    reload = registration["stale"] or startup.environment_changed()
    return {
        "action": action,
        "reload": reload,
        "detached": action in {"stop", "remove", "restart", "install"}
        or (action == "repair" and reload and registration["enabled"]),
    }


def plan_operation(directory, port, action, *, startup=None):
    """Resolve whether a service operation must outlive the serving HTTP process."""
    manager = startup or Startup(directory, port or read_state(directory).get("port", DEFAULT_PORT))
    registration = manager.registration()
    if registration.get("port") and port is None and startup is None:
        manager = Startup(directory, registration["port"])
    return _operation(manager, registration, action)


def service_command(directory, port, action):
    startup = Startup(directory, port or read_state(directory).get("port", DEFAULT_PORT))
    try:
        registered = startup.registration()
    except RuntimeError, OSError, subprocess.TimeoutExpired:
        if action != "status":
            raise
        registered = {}
    if registered.get("port") and (port is None or action == "status"):
        startup = Startup(directory, registered["port"])
    if action == "status":
        health = startup.health()
        installed = health["serviceInstalled"]
        return {
            **health,
            "schemaVersion": 1,
            "supported": True,
            "backend": "schtasks" if installed else "none",
            "label": health["taskName"] if installed else None,
            "installed": installed,
            "enabled": health["serviceEnabled"],
            "running": health["managerRunning"],
            "viable": health["serviceViable"],
            "stale": health["serviceStale"],
            "conflict": health["serviceConflict"],
            "serving": {
                "probed": installed,
                "ok": installed and health["serviceRunning"],
                "port": health["port"] if installed else None,
            },
            "summary": health["error"]
            or (
                "Service not installed"
                if not installed
                else f"Service {'enabled' if health['serviceEnabled'] else 'disabled'}; gateway {health['runtimeState']}"
            ),
            "runtimeLogPath": health["logPath"],
            "logPath": health["supervisorLogPath"],
            "ownershipLease": None,
        }
    with instance_lock(directory, "service-command.lock"):
        registration = startup.registration()
        operation = _operation(startup, registration, action)
        action, needs_reload = operation["action"], operation["reload"]
        if action in {"install", "repair", "restart"}:
            startup.preflight()
        previous_runtime = status(directory)
        previously_supervised = startup.supervisor_running()
        files = {
            name: (directory / name).read_bytes() if (directory / name).exists() else None
            for name in ("startup-task.xml", "startup-environment.bin", "startup-preference.json")
        }
        if operation["detached"]:
            with instance_lock(directory, "control.lock"):
                stop(directory)
            wait_supervisor_stopped(directory)
            # The supervisor releases its file lock before Task Scheduler marks
            # its process idle. /run during that gap is ignored (IgnoreNew).
            startup.wait_idle()
        if action == "remove":
            return startup.action("remove")
        if action == "stop":
            return {"ok": True, "action": action, "health": startup.health()}
        if action == "start":
            if registration["stale"]:
                raise ValueError("Service definition is stale; run ocx service repair.")
            if previous_runtime["state"] == "running" and previous_runtime["port"] != startup.port:
                raise RuntimeError(
                    "A gateway is already running on another port in this state directory."
                )
            startup.run_task()
            result = {"ok": True, "action": action}
            wait_serving(directory, startup.port)
        else:
            try:
                result = startup.action("repair" if action == "restart" else action)
                if action in {"install", "restart", "enable"} or (
                    action == "repair" and registration["enabled"]
                ):
                    wait_serving(directory, startup.port)
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                if action not in {"install", "repair", "restart"} or (
                    action == "repair" and not needs_reload
                ):
                    raise
                try:
                    startup.save_preference(False)
                    startup.wait_idle()
                    with instance_lock(directory, "control.lock"):
                        stop(directory)
                    wait_supervisor_stopped(directory)
                    startup.restore_registration(registration, files)
                    if previous_runtime["state"] == "running":
                        if (
                            registration["installed"]
                            and registration["enabled"]
                            and previously_supervised
                        ):
                            startup.run_task()
                            wait_serving(directory, registration["port"])
                        else:
                            start_background(directory, previous_runtime["port"])
                except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as recovery:
                    raise RuntimeError(
                        f"Service update failed: {exc}. Recovery failed: {recovery}"
                    ) from exc
                raise RuntimeError(
                    f"Service update failed; previous registration and runtime restored: {exc}"
                ) from exc
        return {**result, "action": action, "health": startup.health()}
