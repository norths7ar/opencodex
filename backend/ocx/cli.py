"""Project-owned lifecycle commands and per-user Windows automatic startup."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
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
    parser = argparse.ArgumentParser(prog="ocx", description="OpenCodex Lite local gateway")
    parser.add_argument(
        "--version", "-v", action="version", version=f"OpenCodex Lite {__version__}"
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["start", "stop", "restart", "status", "serve", "service", "service-run"],
    )
    parser.add_argument(
        "service_action",
        nargs="?",
        choices=[
            "status",
            "install",
            "repair",
            "restart",
            "start",
            "stop",
            "enable",
            "disable",
            "uninstall",
            "remove",
        ],
    )
    parser.add_argument("--port", type=int, help=f"Listening port (first start: {DEFAULT_PORT})")
    parser.add_argument(
        "--state-dir", help="Independent runtime directory; defaults to LOCALAPPDATA/OpenCodexLite"
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable status")
    parser.add_argument("--operation-id", help=argparse.SUPPRESS)
    backend = parser.add_mutually_exclusive_group()
    backend.add_argument(
        "--scheduler", action="store_true", help="Use the Windows Task Scheduler service"
    )
    backend.add_argument(
        "--native", action="store_true", help="WinSW service (not supported by Lite)"
    )
    argv = sys.argv[1:]
    if argv == ["version"]:
        argv = ["--version"]
    elif argv and argv[0] == "help":
        argv = [*argv[1:], "--help"]
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return
    if args.scheduler or args.native:
        if args.command != "service":
            parser.error("Service backend options apply to ocx service only.")
        if args.service_action not in {None, "install"}:
            parser.error("Service backend options apply to ocx service install only.")
        if args.native:
            parser.error("Lite uses Windows Task Scheduler; the WinSW backend is not migrated.")
        if args.service_action is None:
            args.service_action = "install"
    if os.name != "nt":
        parser.error("This Lite runtime supports Windows only.")
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.json and args.command in {"start", "serve"}:
        parser.error(
            "Foreground startup does not support --json; use ocx status --json separately."
        )
    directory = state_directory(args.state_dir)
    try:
        if args.command == "service-run":
            from .startup import supervise

            supervise(directory, args.port or DEFAULT_PORT)
            return
        if args.command == "service":
            from .service_cli import record_operation, service_command

            if args.json and args.service_action != "status":
                parser.error("--json applies to ocx service status only.")
            result = service_command(directory, args.port, args.service_action)
            if args.operation_id:
                record_operation(directory, args.operation_id, args.service_action, "succeeded")
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                health = result.get("health", result)
                print(f"OpenCodex Lite service: {health['status']}")
                print(f"Task: {health['taskName']}")
                print(
                    f"Gateway: {health['runtimeState']}; user login startup enabled: {health['autostartEnabled']}"
                )
                if health.get("error"):
                    print(health["error"])
            if result.get("health", result).get("diagnosticStale"):
                raise SystemExit(1)
            return
        if args.service_action is not None:
            parser.error("The extra action is only supported by ocx service.")
        if args.command in {"start", "serve"}:
            from .server import serve

            current = status(directory)
            if current["state"] in {"running", "unresponsive"}:
                raise RuntimeError(
                    "A gateway is already running in this state directory; use ocx stop first."
                )
            port = args.port or current["port"]
            print(f"OpenCodex Lite: http://127.0.0.1:{port}/ (foreground)", flush=True)
            serve(directory, port)
            return
        if args.command == "status":
            result = status(directory)
        else:
            with instance_lock(directory, "control.lock"):
                if args.command == "restart":
                    stop(directory)
                result = stop(directory) if args.command == "stop" else start(directory, args.port)
            if args.command == "restart":
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
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        if args.operation_id:
            from .service_cli import record_operation

            record_operation(directory, args.operation_id, args.service_action, "failed", str(exc))
        if args.json:
            print(json.dumps({"state": "error", "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"ocx: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
