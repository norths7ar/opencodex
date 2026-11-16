"""Windows Codex process freshness and the GUI's explicit restart operation.

Matches the old app-server subcommand rules, not arbitrary '*codex*' processes.
Only processes owned by the current Windows SID are candidates.
"""

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from backend.runtime.state import process_alive

_FLAGS = {
    "--enable",
    "--disable",
    "--config",
    "-c",
    "--profile",
    "-p",
    "--model",
    "-m",
    "--sandbox",
    "-s",
    "--ask-for-approval",
    "-a",
    "--local-provider",
    "--add-dir",
    "--cd",
    "-C",
    "--color",
    "--image",
    "-i",
    "--output-schema",
    "--output-last-message",
    "-o",
}
_BINARY = re.compile(
    r"codex(?:-[a-z0-9_]+-[a-z0-9_]+-[a-z0-9_]+(?:-[a-z0-9_]+)?)?"
    r"(?:\.opencodex-real)?(?:\.exe|\.cmd|\.ps1)?$",
    re.IGNORECASE,
)


def _tokens(command: str) -> list[str]:
    return [
        part[1:-1] if part.startswith(('"', "'")) else part
        for part in re.findall(r""""[^"]*"|'[^']*'|[^\s]+""", command)
    ]


def is_app_server(command: str) -> bool:
    tokens = _tokens(command)
    if not tokens:
        return False
    name = Path(tokens[0]).name.lower()
    if name in {"node", "node.exe", "bun", "bun.exe", "deno", "deno.exe"}:
        tokens = tokens[1:]
    if not tokens:
        return False
    name = Path(tokens[0]).name.lower()
    if name in {"codex-code-mode-host", "codex-code-mode-host.exe"}:
        return True
    if not _BINARY.fullmatch(name):
        return False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return False
        if not token.startswith("-"):
            return token.lower() == "app-server"
        index += 1
        if "=" not in token and token in _FLAGS and index < len(tokens):
            index += 1
    return False


def snapshots() -> list[dict] | None:
    executable = shutil.which("pwsh.exe")
    if executable is None:
        return None
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$rows = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^(codex.*|node|bun|deno)\.exe$'
} | ForEach-Object {
    $p = $_
    if ($p.CommandLine -match '(?i)codex') {
        $owner = Invoke-CimMethod -InputObject $p -MethodName GetOwnerSid
        if ($owner.Sid -eq $sid) {
            @{ pid = [int]$p.ProcessId; command = $p.CommandLine;
               executable = $p.ExecutablePath; parent = [int]$p.ParentProcessId;
               started = ([DateTimeOffset]$p.CreationDate).ToUnixTimeMilliseconds() / 1000.0 }
        }
    }
})
ConvertTo-Json -InputObject $rows -Compress
"""
    try:
        result = subprocess.run(
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=12,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=True,
        )
        return json.loads(result.stdout.decode("utf-8-sig"))
    except OSError, ValueError, subprocess.SubprocessError:
        return None


def _desktop(row: dict) -> bool:
    executable = str(row.get("executable") or "").lower().replace("/", "\\")
    return (
        Path(executable).name == "codex.exe"
        and Path(executable).parent.name == "app"
        and not is_app_server(row["command"])
        and not any(t.startswith("--type=") for t in _tokens(row["command"]))
    )


def freshness(path: Path, rows: list[dict] | None = None) -> dict:
    if rows is None:
        rows = snapshots()
    if rows is None:
        return {"state": "unknown", "runningCount": 0}
    selected = [row for row in rows if is_app_server(row["command"]) or _desktop(row)]
    if not selected:
        return {"state": "not_running", "runningCount": 0}
    try:
        modified = path.stat().st_mtime
    except OSError:
        return {"state": "unknown", "runningCount": len(selected)}
    state = "stale" if any(row["started"] < modified for row in selected) else "fresh"
    return {"state": state, "runningCount": len(selected)}


def restart(path: Path, *, synced: bool = False) -> dict:
    before = snapshots()
    state = freshness(path, before)["state"] if before is not None else "unknown"
    result = {
        "success": True,
        "stateBefore": state,
        "synced": synced,
        "requested": [],
        "stopped": [],
        "surviving": [],
        "failed": [],
        "code": "enumeration_unavailable" if before is None else "nothing_running",
    }
    if before is None:
        return result
    app_servers = [row for row in before if is_app_server(row["command"])]
    desktop = [row for row in before if _desktop(row)]
    stale_desktop = desktop if state in {"stale", "unknown"} else []
    candidates = app_servers + stale_desktop
    if not candidates:
        return result
    # Re-enumerate immediately before signalling; PID reuse is not process identity.
    current = snapshots()
    if current is None:
        result["code"] = "enumeration_unavailable"
        return result
    identities = {(row["pid"], row["started"], row["command"]) for row in current}
    candidates = [
        row for row in candidates if (row["pid"], row["started"], row["command"]) in identities
    ]
    for row in candidates:
        pid = row["pid"]
        result["requested"].append(pid)
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=True,
            )
        except OSError, subprocess.SubprocessError:
            if process_alive(pid):
                result["failed"].append(pid)
    deadline = time.monotonic() + 3
    while any(process_alive(row["pid"]) for row in candidates) and time.monotonic() < deadline:
        time.sleep(0.1)
    result["stopped"] = [row["pid"] for row in candidates if not process_alive(row["pid"])]
    result["surviving"] = [row["pid"] for row in candidates if process_alive(row["pid"])]
    result["success"] = not result["surviving"] and not result["failed"]
    result["code"] = "stopped" if result["success"] else "partially_stopped"
    if desktop:
        desktop_pids = {row["pid"] for row in stale_desktop}
        summary = {
            "attempted": bool(stale_desktop),
            "stopped": [pid for pid in result["stopped"] if pid in desktop_pids],
            "surviving": [pid for pid in result["surviving"] if pid in desktop_pids],
            "relaunch": "skipped",
            "pickerState": "stale" if stale_desktop else "current",
            "reason": "picker_current" if not stale_desktop else "restart_requested",
        }
        if stale_desktop and result["success"]:
            try:
                subprocess.Popen(
                    [stale_desktop[0]["executable"]], creationflags=subprocess.CREATE_NO_WINDOW
                )
                summary["relaunch"] = "started"
            except OSError:
                summary["reason"] = "relaunch_failed"
        result["desktopApp"] = summary
    return result
