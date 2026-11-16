"""Project-owned lifecycle commands and per-user Windows automatic startup."""

import argparse
import json
import os
import subprocess
import sys
from importlib.metadata import version

__version__ = version("opencodex-lite")
from backend.platform.files import instance_lock

from .process import run_foreground, stop
from .state import DEFAULT_PORT, state_directory, status


def main() -> None:
    parser = argparse.ArgumentParser(prog="ocx", description="OpenCodex Lite local gateway")
    parser.add_argument(
        "--version", "-v", action="version", version=f"OpenCodex Lite {__version__}"
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["start", "stop", "restart", "status", "serve", "service", "service-run"],
        metavar="{start,stop,restart,status,service}",
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
    argv = sys.argv[1:]
    if argv == ["version"]:
        argv = ["--version"]
    elif argv and argv[0] == "help":
        argv = [*argv[1:], "--help"]
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return
    if os.name != "nt":
        parser.error("This Lite runtime supports Windows only.")
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.json and args.command in {"start", "restart", "serve"}:
        parser.error(
            "Foreground startup does not support --json; use ocx status --json separately."
        )
    directory = state_directory(args.state_dir)
    try:
        if args.command == "service-run":
            from .supervisor import supervise

            supervise(directory, args.port or DEFAULT_PORT)
            return
        if args.command == "service":
            from .service import record_operation, service_command

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
        if args.command in {"start", "restart", "serve"}:
            run_foreground(
                directory,
                args.port,
                restart=args.command == "restart",
                announce=lambda port: print(
                    f"OpenCodex Lite: http://127.0.0.1:{port}/ (foreground)", flush=True
                ),
            )
            return
        if args.command == "status":
            result = status(directory)
        else:
            with instance_lock(directory, "control.lock"):
                result = stop(directory)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"OpenCodex Lite: {result['state']}")
            if result["state"] == "running":
                print(f"Console: http://127.0.0.1:{result['port']}/ (PID {result['pid']})")
            print(f"Log: {result['logPath']}")
            if result.get("error"):
                print(result["error"])
        if result["state"] in {"failed", "unresponsive"}:
            raise SystemExit(1)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        if args.operation_id:
            from .service import record_operation

            record_operation(directory, args.operation_id, args.service_action, "failed", str(exc))
        if args.json:
            print(json.dumps({"state": "error", "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"ocx: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
