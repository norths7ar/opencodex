"""Own-data inventory and reversible cleanup of explicitly disposable Lite files."""

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

CATEGORIES = (
    "configuration",
    "credentials",
    "clientRollback",
    "history",
    "usage",
    "cache",
    "logs",
    "runtime",
    "other",
)
DISPOSABLE = {"chatgpt-quota.json"}


def category(relative):
    name = relative.split("/")[0]
    if name in {
        "chatgpt-account.json",
        "anthropic-account.json",
        "kimi-account.json",
        "kimi-device-id",
        "startup-environment.bin",
    }:
        return "credentials"
    if name in {"config.json", "startup-preference.json"}:
        return "configuration"
    if name == "clients":
        return "clientRollback"
    if name.startswith("responses.sqlite3"):
        return "history"
    if name.startswith(("usage.sqlite3", "spend-ledger.sqlite3")):
        return "usage"
    if name in DISPOSABLE:
        return "cache"
    if name.endswith(".log"):
        return "logs"
    if name.startswith(("runtime.", "startup-")) or name.endswith(".lock"):
        return "runtime"
    return "other"


class Storage:
    def __init__(self, directory, usage=None):
        self.directory = Path(directory).resolve()
        self.trash = self.directory / "storage-quarantine"
        self.usage = usage
        self.lock = threading.RLock()

    def _file(self, relative):
        path = self.directory / relative
        if path.is_symlink() or path.is_junction() or path.resolve() != path:
            raise ValueError("Storage file points outside its owned location")
        return path

    def inventory(self):
        groups = {
            key: {"id": key, "bytes": 0, "count": 0, "disposableBytes": 0} for key in CATEGORIES
        }
        errors = []
        for root, dirs, files in self.directory.walk(follow_symlinks=False):
            dirs[:] = [
                name
                for name in dirs
                if not (root / name).is_junction()
                and not (root / name).is_symlink()
                and root / name != self.trash
            ]
            for name in files:
                path = root / name
                relative = path.relative_to(self.directory).as_posix()
                try:
                    if path.is_symlink():
                        continue
                    size = path.stat().st_size
                except OSError:
                    errors.append(relative)
                    continue
                group = groups[category(relative)]
                group["bytes"] += size
                group["count"] += 1
                if relative in DISPOSABLE:
                    group["disposableBytes"] += size
        return {
            "dataDirectory": str(self.directory),
            "categories": list(groups.values()),
            "errors": errors,
            "usageRetention": self.usage.retention_status() if self.usage else None,
        }

    def preview(self):
        candidates = []
        for relative in sorted(DISPOSABLE):
            path = self._file(relative)
            try:
                stat = path.stat()
                if not path.is_file():
                    continue
                candidates.append(
                    {
                        "relPath": relative,
                        "bytes": stat.st_size,
                        "mtimeNs": stat.st_mtime_ns,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
            except FileNotFoundError:
                continue
        digest = hashlib.sha256(json.dumps(candidates, sort_keys=True).encode()).hexdigest()
        return {
            "digest": digest,
            "candidates": candidates,
            "count": len(candidates),
            "bytes": sum(row["bytes"] for row in candidates),
        }

    def _save(self, folder, journal):
        target = folder / "manifest.json"
        temporary = folder / "manifest.json.tmp"
        temporary.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def quarantine(self, digest):
        with self.lock:
            preview = self.preview()
            if preview["digest"] != digest:
                raise ValueError("Files changed since preview; preview again")
            if not preview["count"]:
                return {"ok": True, "count": 0, "errors": []}
            if self.trash.is_symlink() or self.trash.is_junction():
                raise ValueError("Quarantine must be inside the Lite data directory")
            identity = uuid.uuid4().hex
            folder = self.trash / identity
            folder.mkdir(parents=True)
            journal = {
                "id": identity,
                "createdAt": time.time(),
                "entries": [{**row, "state": "pending"} for row in preview["candidates"]],
            }
            self._save(folder, journal)
            errors = []
            for row in journal["entries"]:
                try:
                    source = self._file(row["relPath"])
                    if hashlib.sha256(source.read_bytes()).hexdigest() != row["sha256"]:
                        raise ValueError("File changed since preview")
                    source.rename(folder / row["relPath"])
                    row["state"] = "quarantined"
                except (OSError, ValueError) as exc:
                    row["error"] = str(exc)
                    row["state"] = "failed"
                    errors.append({"relPath": row["relPath"], "error": str(exc)})
                self._save(folder, journal)
            return {
                "ok": not errors,
                "id": identity,
                "count": sum(row["state"] == "quarantined" for row in journal["entries"]),
                "errors": errors,
            }

    def _read(self, identity):
        if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("Invalid quarantine identifier")
        folder = self.trash / identity
        if (
            folder.is_symlink()
            or folder.is_junction()
            or self.trash.is_symlink()
            or self.trash.is_junction()
        ):
            raise ValueError("Invalid quarantine location")
        journal = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(journal, dict):
            raise TypeError("Invalid quarantine manifest")
        entries = journal.get("entries")
        if not isinstance(entries, list) or any(
            not isinstance(row, dict)
            or row.get("relPath") not in DISPOSABLE
            or not isinstance(row.get("sha256"), str)
            or not isinstance(row.get("bytes"), int)
            or row["bytes"] < 0
            for row in entries
        ):
            raise ValueError("Invalid quarantine manifest")
        return folder, journal

    def list_quarantine(self):
        result = []
        if not self.trash.exists():
            return result
        for folder in sorted(self.trash.iterdir(), reverse=True):
            try:
                _, journal = self._read(folder.name)
                pending = [row for row in journal["entries"] if (folder / row["relPath"]).is_file()]
                result.append(
                    {
                        "id": folder.name,
                        "createdAt": journal["createdAt"],
                        "count": len(pending),
                        "bytes": sum(row["bytes"] for row in pending),
                    }
                )
            except OSError, ValueError, KeyError, TypeError:
                result.append({"id": folder.name, "error": "Quarantine journal needs inspection"})
        return result

    def restore(self, identity):
        with self.lock:
            folder, journal = self._read(identity)
            errors, count = [], 0
            for row in journal["entries"]:
                source = folder / row["relPath"]
                try:
                    destination = self._file(row["relPath"])
                    if not source.exists():
                        if row.get("state") in {"restored", "failed"}:
                            continue
                        if (
                            row.get("state") == "restoring"
                            and destination.is_file()
                            and hashlib.sha256(destination.read_bytes()).hexdigest()
                            == row["sha256"]
                        ):
                            row["state"] = "restored"
                            self._save(folder, journal)
                            continue
                        raise ValueError("Quarantined file is missing; inspect its journal")
                    if destination.exists():
                        raise ValueError("A current file already exists; it has been preserved")
                    if (
                        source.is_symlink()
                        or hashlib.sha256(source.read_bytes()).hexdigest() != row["sha256"]
                    ):
                        raise ValueError("Quarantined file changed")
                    row["state"] = "restoring"
                    self._save(folder, journal)
                    # Windows rename refuses replacing an existing destination, including races.
                    source.rename(destination)
                    row["state"] = "restored"
                    count += 1
                except (OSError, ValueError) as exc:
                    row["error"] = str(exc)
                    errors.append({"relPath": row["relPath"], "error": str(exc)})
                self._save(folder, journal)
            return {"ok": not errors, "count": count, "errors": errors}
