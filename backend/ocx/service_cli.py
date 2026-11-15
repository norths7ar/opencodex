"""CLI service verbs over the shared Windows startup manager."""

import json
import subprocess
import sys
import time
from pathlib import Path

from .runtime import DEFAULT_PORT, instance_lock, read_state, status
from .startup import Startup


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
                    "ocx",
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


def wait_stopped_supervisor(directory):
    deadline = time.monotonic() + 10
    while True:
        try:
            with instance_lock(directory, "startup-supervisor.lock"):
                return
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Background supervision did not stop; inspect startup.log."
                ) from None
            time.sleep(0.1)


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


def service_command(directory, port, action):
    from .cli import stop

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
        if registration["conflict"]:
            raise RuntimeError("A different task occupies the Lite service name.")
        action = action or ("repair" if registration["installed"] else "install")
        if action in {"start", "restart", "repair"} and not registration["installed"]:
            raise ValueError("Service is not installed; run ocx service install.")
        if action in {"start", "restart"} and not registration["enabled"]:
            raise ValueError("Service is disabled; run ocx service enable.")
        needs_reload = registration["stale"] or startup.environment_changed()
        if action in {"install", "repair", "restart"}:
            startup.preflight()
        previous_runtime = status(directory)
        previously_supervised = startup.supervisor_running()
        files = {
            name: (directory / name).read_bytes() if (directory / name).exists() else None
            for name in ("startup-task.xml", "startup-environment.bin", "startup-preference.json")
        }
        if action in {"stop", "uninstall", "remove", "restart", "install"} or (
            action == "repair" and needs_reload and registration["enabled"]
        ):
            with instance_lock(directory, "control.lock"):
                stop(directory)
            wait_stopped_supervisor(directory)
        if action in {"uninstall", "remove"}:
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
            startup._command(["/run", "/tn", startup.task_name])
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
                    startup._save_preference(False)
                    startup.wait_idle()
                    with instance_lock(directory, "control.lock"):
                        stop(directory)
                    wait_stopped_supervisor(directory)
                    startup.restore_registration(registration, files)
                    if previous_runtime["state"] == "running":
                        if (
                            registration["installed"]
                            and registration["enabled"]
                            and previously_supervised
                        ):
                            startup._command(["/run", "/tn", startup.task_name])
                            wait_serving(directory, registration["port"])
                        else:
                            from .cli import start

                            start(directory, previous_runtime["port"])
                except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as recovery:
                    raise RuntimeError(
                        f"Service update failed: {exc}. Recovery failed: {recovery}"
                    ) from exc
                raise RuntimeError(
                    f"Service update failed; previous registration and runtime restored: {exc}"
                ) from exc
        return {**result, "action": action, "health": startup.health()}
