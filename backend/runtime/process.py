"""Gateway process lifecycle shared by foreground and service controllers."""

import subprocess
import sys
import time
from pathlib import Path

from backend.platform.files import instance_lock

from .state import control_request, process_alive, read_state, status


def wait_supervisor_stopped(directory: Path) -> None:
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


def run_foreground(directory: Path, port: int | None, *, restart=False, announce=None) -> None:
    from .server import serve

    if restart:
        with instance_lock(directory, "control.lock"):
            stop(directory)
        wait_supervisor_stopped(directory)
    current = status(directory)
    if current["state"] in {"running", "unresponsive"}:
        raise RuntimeError(
            "A gateway is already running in this state directory; use ocx stop first."
        )
    selected = port or current["port"]
    if announce:
        announce(selected)
    serve(directory, selected)


def start_background(directory: Path, port: int | None) -> dict:
    current = status(directory)
    if current["state"] == "running":
        if port is not None and port != current["port"]:
            raise RuntimeError(
                f"Already running on port {current['port']}; stop it before changing port."
            )
        return current
    if current["state"] == "unresponsive":
        raise RuntimeError(
            "An existing instance is unresponsive; inspect its log before starting another."
        )
    selected_port = port or current["port"]
    # The browser can request shutdown too: wait for its final lock release before spawning.
    deadline = time.monotonic() + 3
    while True:
        try:
            with instance_lock(directory, "instance.lock"):
                break
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)
    with (directory / "runtime.log").open("ab") as log:
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "backend.runtime",
                "serve",
                "--state-dir",
                str(directory),
                "--port",
                str(selected_port),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = status(directory)
        # On Windows the virtualenv launcher can have a different PID from its Python child.
        if result["state"] == "running":
            return result
        if child.poll() is not None:
            raise RuntimeError(f"Startup failed. See {directory / 'runtime.log'}")
        time.sleep(0.1)
    raise RuntimeError(
        "Startup did not become ready within 30 seconds; the process may still be starting. "
        f"Check ocx status and {directory / 'runtime.log'}. Runtime control state was preserved."
    )


def stop(directory: Path) -> dict:
    current = status(directory)
    if current["state"] in {"stopped", "failed"}:
        return current
    if current["state"] != "running":
        raise RuntimeError("Instance is unresponsive; no process was forcibly terminated.")
    control_request(read_state(directory), "/api/stop", method="POST")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = read_state(directory)
        if state.get("state") == "stopped" and not process_alive(current["pid"]):
            return status(directory)
        time.sleep(0.1)
    raise RuntimeError(
        f"Shutdown did not finish within 15 seconds. See {directory / 'runtime.log'}"
    )
