"""Per-user Windows Task Scheduler startup, retaining the gateway's lifecycle ownership."""

import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from backend.platform.files import instance_lock

from .state import PROJECT_ROOT, status

TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
ET.register_namespace("", TASK_NS)


def run_windows(program: str, arguments: list[str]) -> subprocess.CompletedProcess:
    # Keep the Windows spelling when inspecting the environment on other platforms.
    executable = Path(os.environ["SystemRoot"]) / "System32" / program  # noqa: SIM112
    result = subprocess.run(
        [str(executable), *arguments],
        capture_output=True,
        check=False,
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    def decode(value: bytes) -> str:
        if value.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        elif value.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8-sig"
        elif b"\x00" in value[:100]:
            encoding = "utf-16-le"
        else:
            # Native scheduler diagnostics use the Windows ANSI codepage, not
            # Python's optional UTF-8 mode. Keep localized failures readable.
            encoding = "mbcs"
        return value.decode(encoding, errors="replace")

    return subprocess.CompletedProcess(
        result.args, result.returncode, decode(result.stdout), decode(result.stderr)
    )


def current_identity(run=run_windows) -> tuple[str, str]:
    result = run("whoami.exe", ["/user", "/fo", "csv", "/nh"])
    rows = list(csv.reader(io.StringIO(result.stdout)))
    if result.returncode or not rows or len(rows[0]) != 2 or not rows[0][1].startswith("S-1-"):
        raise RuntimeError("Cannot determine the current Windows account for automatic startup.")
    return rows[0][1], rows[0][0]


class Startup:
    def __init__(self, directory: Path, port: int, *, run=run_windows, sid: str | None = None):
        self.directory = directory.resolve()
        self.port = port
        self.run = run
        self._sid = sid
        self._user = None
        self._health_lock = threading.Lock()
        self._health_registration: tuple[float, object, dict, str | None] | None = None
        digest = hashlib.sha256(str(self.directory).casefold().encode()).hexdigest()[:12]
        self.task_name = f"OpenCodex Lite {digest}"
        self.description = f"OpenCodex Lite background gateway: {self.directory}"

    @property
    def sid(self) -> str:
        if self._sid is None:
            self._sid, self._user = current_identity(self.run)
        return self._sid

    @property
    def executable(self) -> Path:
        # pythonw keeps login startup invisible; the supervisor redirects its own log.
        return Path(sys.executable).with_name("pythonw.exe")

    @property
    def arguments(self) -> str:
        return subprocess.list2cmdline(
            [
                "-m",
                "backend.runtime",
                "service-run",
                "--state-dir",
                str(self.directory),
                "--port",
                str(self.port),
            ]
        )

    def xml(self) -> str:
        root = ET.Element("Task", {"version": "1.4", "xmlns": TASK_NS})

        def add(parent, tag, value=None, **attributes):
            node = ET.SubElement(parent, tag, attributes)
            node.text = value
            return node

        registration = add(root, "RegistrationInfo")
        add(registration, "Description", self.description)
        triggers = add(root, "Triggers")
        logon = add(triggers, "LogonTrigger")
        add(logon, "Enabled", "true")
        add(logon, "UserId", self.sid)
        for change in ("ConsoleConnect", "RemoteConnect", "SessionUnlock"):
            trigger = add(triggers, "SessionStateChangeTrigger")
            add(trigger, "Enabled", "true")
            add(trigger, "UserId", self.sid)
            add(trigger, "StateChange", change)
        principal = add(add(root, "Principals"), "Principal", id="Author")
        add(principal, "UserId", self.sid)
        add(principal, "LogonType", "InteractiveToken")
        add(principal, "RunLevel", "LeastPrivilege")
        settings = add(root, "Settings")
        for key, value in {
            "MultipleInstancesPolicy": "IgnoreNew",
            "DisallowStartIfOnBatteries": "false",
            "StopIfGoingOnBatteries": "false",
            "AllowHardTerminate": "false",
            "StartWhenAvailable": "true",
            "RunOnlyIfNetworkAvailable": "false",
            "AllowStartOnDemand": "true",
            "Enabled": "true",
            "ExecutionTimeLimit": "PT0S",
        }.items():
            add(settings, key, value)
        restart = add(settings, "RestartOnFailure")
        add(restart, "Interval", "PT1M")
        add(restart, "Count", "3")
        action = add(add(root, "Actions", Context="Author"), "Exec")
        add(action, "Command", str(self.executable))
        add(action, "Arguments", self.arguments)
        add(action, "WorkingDirectory", str(PROJECT_ROOT))
        return '<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(root, encoding="unicode")

    def _command(self, arguments: list[str]):
        result = self.run("schtasks.exe", arguments)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip() or "Task Scheduler failed.")
        return result

    def registration(self) -> dict:
        result = self.run("schtasks.exe", ["/query", "/tn", self.task_name, "/xml"])
        if result.returncode:
            # A localized failure is not proof of absence. A successful inventory is.
            inventory = self._command(["/query", "/fo", "csv", "/nh"])
            names = {
                row[0].lstrip("\\").casefold()
                for row in csv.reader(io.StringIO(inventory.stdout))
                if row
            }
            if self.task_name.casefold() in names:
                raise RuntimeError(
                    "The startup task exists but its registration could not be read."
                )
            return {"installed": False, "enabled": False, "stale": False, "conflict": False}
        try:
            root = ET.fromstring(result.stdout.strip().lstrip("\ufeff"))
        except ET.ParseError as exc:
            raise RuntimeError(
                "Task Scheduler returned an unreadable startup registration."
            ) from exc

        def value(path):
            return root.findtext("/".join(f"{{{TASK_NS}}}{part}" for part in path.split("/")))

        conflict = value("RegistrationInfo/Description") != self.description
        expected = ET.fromstring(self.xml())
        # Compare behavior, not serialized XML formatting or scheduler-added defaults.
        paths = [
            "Principals/Principal/UserId",
            "Principals/Principal/LogonType",
            "Actions/Exec/Command",
            "Actions/Exec/Arguments",
            "Actions/Exec/WorkingDirectory",
            "Settings/MultipleInstancesPolicy",
            "Settings/ExecutionTimeLimit",
            "Settings/DisallowStartIfOnBatteries",
            "Settings/StopIfGoingOnBatteries",
            "Settings/StartWhenAvailable",
            "Settings/AllowStartOnDemand",
            "Settings/RunOnlyIfNetworkAvailable",
            "Settings/RestartOnFailure/Interval",
            "Settings/RestartOnFailure/Count",
        ]
        defaults = {
            "Settings/AllowStartOnDemand": "true",
            "Settings/RunOnlyIfNetworkAvailable": "false",
        }
        stale = any(
            (value(path) if value(path) is not None else defaults.get(path))
            != expected.findtext("/".join(f"{{{TASK_NS}}}{p}" for p in path.split("/")))
            for path in paths
        )
        triggers = root.find(f"{{{TASK_NS}}}Triggers")
        actual_triggers = (
            {
                (
                    node.tag.rsplit("}", 1)[-1],
                    node.findtext(f"{{{TASK_NS}}}StateChange"),
                    self.sid
                    if node.findtext(f"{{{TASK_NS}}}UserId", "").casefold()
                    in {self.sid.casefold(), (self._user or self.sid).casefold()}
                    else node.findtext(f"{{{TASK_NS}}}UserId"),
                    node.findtext(f"{{{TASK_NS}}}Enabled", "true"),
                )
                for node in triggers
            }
            if triggers is not None
            else set()
        )
        required = {
            ("LogonTrigger", None, self.sid, "true"),
            *(
                ("SessionStateChangeTrigger", change, self.sid, "true")
                for change in ("ConsoleConnect", "RemoteConnect", "SessionUnlock")
            ),
        }
        stale = (
            stale
            or not required.issubset(actual_triggers)
            or not self.executable.is_file()
            or len(root.findall(f"{{{TASK_NS}}}Actions/{{{TASK_NS}}}Exec")) != 1
            or value("Principals/Principal/RunLevel")
            not in {None, "LeastPrivilege", "HighestAvailable"}
        )
        return {
            "installed": True,
            "enabled": value("Settings/Enabled") != "false",
            "stale": stale,
            "conflict": conflict,
            "port": int(match[1])
            if (
                match := re.search(
                    r"(?:^|\s)--port\s+(\d+)(?:\s|$)", value("Actions/Exec/Arguments") or ""
                )
            )
            else None,
            "xml": result.stdout,
        }

    def health(self, *, cached: bool = False) -> dict:
        try:
            operation = json.loads(
                (self.directory / "service-operation.json").read_text(encoding="utf-8")
            )
        except OSError, ValueError:
            operation = None
        versions = []
        for name in ("startup-preference.json", "startup-task.xml"):
            try:
                metadata = (self.directory / name).stat()
                versions.append((metadata.st_mtime_ns, metadata.st_size))
            except OSError:
                versions.append(None)
        cache_key = (operation, tuple(versions))
        # Overview polling needs live process state, not a new scheduler process
        # each time. Explicit diagnostics remain fresh; operations invalidate the
        # overview cache through receipts and preference/registration file changes.
        with self._health_lock:
            previous = self._health_registration
            if cached and previous and previous[1] == cache_key and time.monotonic() < previous[0]:
                registration, error = previous[2], previous[3]
            else:
                error = None
                try:
                    registration = self.registration()
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                    registration = {
                        "installed": False,
                        "enabled": False,
                        "stale": False,
                        "conflict": False,
                    }
                    error = str(exc)
                self._health_registration = (
                    time.monotonic() + (60 if error else 300),
                    cache_key,
                    registration,
                    error,
                )
        live = status(self.directory)
        service_port = registration.get("port") or self.port
        viable = (
            registration["installed"]
            and registration["enabled"]
            and not registration["stale"]
            and not registration["conflict"]
            and not error
        )
        return {
            "status": "protected" if viable else "at-risk",
            "platform": "win32",
            "mode": "user-logon",
            "taskName": self.task_name,
            "stateDirectory": str(self.directory),
            "port": service_port,
            "runtimePort": live["port"],
            "runtimeState": live["state"],
            "logPath": live["logPath"],
            "supervisorLogPath": str(self.directory / "startup.log"),
            "autostartEnabled": registration["enabled"],
            "rebootSafe": viable,
            "protection": "service" if viable else "none",
            "serviceInstalled": registration["installed"],
            "serviceEnabled": registration["enabled"],
            "serviceViable": viable,
            "serviceStale": registration["stale"],
            "serviceConflict": registration["conflict"],
            "serviceSupported": True,
            "serviceRunning": live["state"] == "running" and live["port"] == service_port,
            "managerRunning": self.supervisor_running(),
            "diagnosticStale": error is not None,
            "error": error,
            "serviceOperation": operation,
            "commands": {
                action: f'ocx service {action} --state-dir "{self.directory}" --port {service_port}'
                for action in ("install", "repair", "enable", "disable", "remove", "status")
            },
        }

    def supervisor_running(self):
        if not (self.directory / "startup-supervisor.lock").exists():
            return False
        try:
            with instance_lock(self.directory, "startup-supervisor.lock"):
                return False
        except RuntimeError:
            return True

    def action(self, action: str, *, repair: bool = False) -> dict:
        with instance_lock(self.directory, "startup-control.lock"):
            current = self.registration()
            if current["conflict"]:
                raise RuntimeError(
                    "A different task occupies the Lite startup name; it was not changed."
                )
            if action in {"install-service", "install", "repair"}:
                self.preflight()
                if (repair or action == "repair") and not current["installed"]:
                    raise ValueError("Automatic startup is not installed; choose Install first.")
                if not current["installed"] or current["stale"] or action != "repair":
                    xml_path = self.directory / "startup-task.xml"
                    xml_path.write_text(self.xml(), encoding="utf-16")
                    self._command(["/create", "/tn", self.task_name, "/xml", str(xml_path), "/f"])
                if self.environment_changed():
                    self._save_environment()
                enabled = not ((repair or action == "repair") and not current["enabled"])
                if not enabled:
                    self._command(["/change", "/tn", self.task_name, "/disable"])
                self.save_preference(enabled)
                if enabled:
                    self._command(["/run", "/tn", self.task_name])
            elif action in {"enable", "disable"}:
                if not current["installed"]:
                    raise ValueError("Automatic startup is not installed.")
                self._command(["/change", "/tn", self.task_name, f"/{action}"])
                self.save_preference(action == "enable")
                if action == "enable":
                    self._command(["/run", "/tn", self.task_name])
            elif action == "remove":
                if current["installed"]:
                    self._command(["/delete", "/tn", self.task_name, "/f"])
                self.save_preference(False)
                (self.directory / "startup-task.xml").unlink(missing_ok=True)
                (self.directory / "startup-environment.bin").unlink(missing_ok=True)
            else:
                raise ValueError("Unknown startup action.")
            return {"ok": True, "action": action, "health": self.health()}

    def preflight(self):
        if not self.executable.is_file():
            raise RuntimeError(
                "This Python environment has no pythonw.exe; repair the environment first."
            )
        self.environment()

    def restore_registration(self, registration, files):
        current = self.registration()
        if current["conflict"]:
            raise RuntimeError("Service registration changed owner during recovery.")
        if registration["installed"]:
            if not current["installed"] or current["xml"].strip() != registration["xml"].strip():
                path = self.directory / "startup-task.xml"
                path.write_text(registration["xml"], encoding="utf-16")
                self._command(["/create", "/tn", self.task_name, "/xml", str(path), "/f"])
                if not registration["enabled"]:
                    self._command(["/change", "/tn", self.task_name, "/disable"])
        elif current["installed"]:
            self._command(["/delete", "/tn", self.task_name, "/f"])
        for name, content in files.items():
            path = self.directory / name
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)

    def wait_idle(self):
        if not self.registration()["installed"]:
            return
        # A queued scheduler launch has not acquired the supervisor lock yet. Wait for
        # the task itself before restoring the old definition and enabling supervision.
        shell = shutil.which("pwsh.exe") or str(
            Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"  # noqa: SIM112
        )
        deadline = time.monotonic() + 45
        while (remaining := deadline - time.monotonic()) > 0:
            window = min(12, remaining)
            script = (
                "$ErrorActionPreference='Stop';"
                "$scheduler=New-Object -ComObject 'Schedule.Service';$scheduler.Connect();"
                "$folder=$scheduler.GetFolder('\\');"
                "$clock=[Diagnostics.Stopwatch]::StartNew();"
                f"while ($folder.GetTask('{self.task_name}').State -in 2,4) {{"
                f"if ($clock.Elapsed.TotalSeconds -ge {window:.2f}) {{Write-Output 'busy';exit 0}};"
                "Start-Sleep -Milliseconds 100};Write-Output 'idle'"
            )
            result = self.run(
                shell, ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script]
            )
            if result.returncode:
                raise RuntimeError(
                    result.stderr.strip() or "Could not confirm the service task stopped."
                )
            if result.stdout.strip() == "idle":
                return
        raise RuntimeError("Service task did not stop within 45 seconds.")

    def save_preference(self, enabled: bool):
        path = self.directory / "startup-preference.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"enabled": enabled}), encoding="utf-8")
        temporary.replace(path)

    def run_task(self):
        self._command(["/run", "/tn", self.task_name])

    def saved_environment(self):
        from backend.auth.storage import protect

        try:
            return json.loads(
                protect((self.directory / "startup-environment.bin").read_bytes(), decrypt=True)
            )
        except OSError, ValueError, RuntimeError:
            return None

    def environment(self):
        # The scheduler does not inherit the installing shell's proxy/credential environment.
        # Persist only names this gateway uses, protected for the same Windows account.
        from backend.configuration.store import ConfigurationStore

        names = {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "DSH_HOME",
            "CODEX_HOME",
        }
        names.update(
            record.env_var
            for record in ConfigurationStore(self.directory).read().credentials.values()
            if record.env_var
        )
        saved = self.saved_environment() or {}
        return {
            name: os.environ[name] if name in os.environ else saved[name]
            for name in names
            if name in os.environ or name in saved
        }

    def environment_changed(self):
        return self.saved_environment() != self.environment()

    def _save_environment(self):
        from backend.auth.storage import protect

        path = self.directory / "startup-environment.bin"
        temporary = path.with_suffix(".bin.tmp")
        temporary.write_bytes(protect(json.dumps(self.environment()).encode()))
        temporary.replace(path)
