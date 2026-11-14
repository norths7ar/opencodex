"""Project-owned lifecycle commands and per-user Windows automatic startup."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .runtime import (
    DEFAULT_PORT,
    control_request,
    instance_lock,
    process_alive,
    read_state,
    state_directory,
    status,
)


def start(directory: Path, port: int | None) -> dict:
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
                "ocx",
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="ocx", description="OpenCodex Lite local Python runtime")
    parser.add_argument(
        "command", choices=["start", "stop", "restart", "status", "serve", "startup", "service-run"]
    )
    parser.add_argument(
        "startup_action",
        nargs="?",
        choices=["status", "install", "repair", "enable", "disable", "remove"],
    )
    parser.add_argument("--port", type=int, help=f"Listening port (first start: {DEFAULT_PORT})")
    parser.add_argument(
        "--state-dir", help="Independent runtime directory; defaults to LOCALAPPDATA/OpenCodexLite"
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable status")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This Lite runtime supports Windows only.")
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    directory = state_directory(args.state_dir)
    try:
        if args.command == "service-run":
            from .startup import supervise

            supervise(directory, args.port or DEFAULT_PORT)
            return
        if args.command == "startup":
            from .startup import Startup

            startup = Startup(
                directory, args.port or read_state(directory).get("port", DEFAULT_PORT)
            )
            result = (
                startup.health()
                if args.startup_action in {None, "status"}
                else startup.action(args.startup_action)
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                health = result.get("health", result)
                print(f"OpenCodex Lite automatic startup: {health['status']}")
                print(f"Task: {health['taskName']}")
                print(
                    f"Gateway: {health['runtimeState']}; user login startup enabled: {health['autostartEnabled']}"
                )
                if health.get("error"):
                    print(health["error"])
            if result.get("health", result).get("diagnosticStale"):
                raise SystemExit(1)
            return
        if args.startup_action is not None:
            parser.error("The extra action is only supported by ocx startup.")
        if args.command == "serve":
            from .server import serve

            serve(directory, args.port or DEFAULT_PORT)
            return
        if args.command == "status":
            result = status(directory)
        else:
            with instance_lock(directory, "control.lock"):
                if args.command == "restart":
                    stop(directory)
                result = stop(directory) if args.command == "stop" else start(directory, args.port)
            if args.command in {"start", "restart"}:
                from .startup import Startup

                try:
                    Startup(directory, result["port"]).resume_supervision()
                except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    result["startupWarning"] = str(exc)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"OpenCodex Lite: {result['state']}")
            if result["state"] == "running":
                print(f"Console: http://127.0.0.1:{result['port']}/ (PID {result['pid']})")
            print(f"Log: {result['logPath']}")
            if result.get("error"):
                print(result["error"])
            if result.get("startupWarning"):
                print(f"Automatic startup: {result['startupWarning']}")
        if result["state"] in {"failed", "unresponsive"}:
            raise SystemExit(1)
    except (RuntimeError, OSError, ValueError) as exc:
        if args.json:
            print(json.dumps({"state": "error", "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"ocx: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
