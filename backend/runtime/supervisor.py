"""Supervise a per-user gateway and honor intentional shutdown."""

import json
import os
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from backend.platform.files import instance_lock

from .process import start_background
from .state import status


def supervise(directory: Path, port: int):
    """Scheduler child: adopt an existing instance, recover crashes, honor deliberate stop."""
    from backend.auth.storage import protect

    directory.mkdir(parents=True, exist_ok=True)
    with (
        instance_lock(directory, "startup-supervisor.lock"),
        (directory / "startup.log").open("a", encoding="utf-8", buffering=1) as log,
        redirect_stdout(log),
        redirect_stderr(log),
    ):
        print("Starting OpenCodex Lite background supervision.", flush=True)
        environment_path = directory / "startup-environment.bin"
        if environment_path.exists():
            environment = json.loads(protect(environment_path.read_bytes(), decrypt=True))
            os.environ.update(environment)
        started = False
        while True:
            try:
                preference = directory / "startup-preference.json"
                if preference.exists() and not json.loads(
                    preference.read_text(encoding="utf-8")
                ).get("enabled", False):
                    print("Automatic startup disabled; leaving the gateway running.", flush=True)
                    return
                # Serialize control operations with readiness and crash recovery.
                with instance_lock(directory, "control.lock"):
                    current = status(directory)
                    if started and current["state"] == "stopped":
                        print("Gateway stopped explicitly; ending supervision.", flush=True)
                        return
                    if current["state"] not in {"running", "unresponsive"}:
                        start_background(directory, port)
                        started = True
                    elif current["state"] == "running":
                        started = True
            except (RuntimeError, OSError, ValueError) as exc:
                print(f"Background startup: {exc}", flush=True)
            time.sleep(5)
