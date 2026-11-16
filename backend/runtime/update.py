"""Synchronize this checkout, build its console, and publish to the local service."""

import shutil
import subprocess
from pathlib import Path

from backend.platform.files import instance_lock

from .state import PROJECT_ROOT, WEB_ROOT


def _tool(name: str) -> str:
    executable = shutil.which(name)
    if executable:
        return executable
    fallback = Path.home() / ".local" / "bin" / f"{name}.exe"
    if name == "uv" and fallback.is_file():
        return str(fallback)
    raise RuntimeError(f"{name} is required to update this source checkout; install it first.")


def _run(label: str, command: list[str]) -> None:
    print(f"OpenCodex Lite update: {label}", flush=True)
    result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    if result.returncode:
        raise RuntimeError(
            f"{label} failed (exit {result.returncode}); the console was not published. "
            "Source and dependency changes are not rolled back."
        )


def update(directory: Path) -> None:
    uv, pnpm = _tool("uv"), _tool("pnpm")
    scratch = PROJECT_ROOT / ".tmp"
    # The candidate directory is shared by every state directory of this checkout.
    with instance_lock(scratch, "frontend-build-update.lock"):
        _run("synchronizing Python dependencies", [uv, "sync", "--locked"])
        _run(
            "synchronizing frontend dependencies",
            [pnpm, "--dir", "frontend", "install", "--frozen-lockfile"],
        )
        _run("building the console", [pnpm, "--dir", "frontend", "build:runtime"])

        from .deployment import publish

        print("OpenCodex Lite update: publishing and verifying", flush=True)
        restarted = publish(
            directory,
            build_root=PROJECT_ROOT / "frontend" / "build",
            web=WEB_ROOT,
            scratch=scratch,
        )
    print(
        "Published and verified OpenCodex Lite; the background gateway was restarted."
        if restarted
        else "Published the OpenCodex Lite console; the gateway was left stopped."
    )
    print("Codex configuration, authentication, and the startup preference were unchanged.")
