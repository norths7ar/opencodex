"""Disk storage for complete discovered directories, separate from configuration."""

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path


def validate_rows(rows: object) -> tuple[dict, ...]:
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip()
        for row in rows
    ):
        raise ValueError("Expected a complete directory of models with nonempty IDs.")
    return tuple(deepcopy(rows))


@dataclass(frozen=True, slots=True)
class DirectorySnapshot:
    fingerprint: str
    checked: float
    rows: tuple[dict, ...]
    status: str | None
    checked_at: str | None
    last_success_at: str | None
    reason: str | None = None
    legacy: bool = False
    failures: int = 0


class DirectoryStore:
    def __init__(self, directory: Path):
        self.path = directory / "catalog-directory.json"
        self._snapshots = self._load()

    def _load(self) -> dict[str, DirectorySnapshot]:
        try:
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                legacy = False
            except FileNotFoundError:
                path = self.path.with_name("model-catalog.json")
                saved = json.loads(path.read_text(encoding="utf-8"))
                legacy = True
            if saved["version"] != (1 if legacy else 2):
                return {}
            entries = saved["providers" if legacy else "channels"]
            if not isinstance(entries, dict):
                return {}
        except OSError, ValueError, TypeError, KeyError:
            return {}
        snapshots = {}
        for channel_id, entry in entries.items():
            try:
                fingerprint = entry["fingerprint"]
                if not isinstance(fingerprint, str) or not fingerprint:
                    continue
                discovery = entry.get("discovery", {})
                snapshots[channel_id] = DirectorySnapshot(
                    fingerprint,
                    float(entry["checked"]),
                    validate_rows(entry["models"]),
                    discovery.get("status"),
                    discovery.get("checkedAt"),
                    discovery.get("lastSuccessAt"),
                    discovery.get("reason"),
                    legacy or entry.get("legacy", False),
                    max(0, min(7, int(discovery.get("failures", 0)))),
                )
            except ValueError, TypeError, KeyError, AttributeError:
                continue
        return snapshots

    def read(self, channel_id: str) -> DirectorySnapshot | None:
        return deepcopy(self._snapshots.get(channel_id))

    def put(self, channel_id: str, snapshot: DirectorySnapshot, active_ids: set[str]) -> None:
        proposed = {key: value for key, value in self._snapshots.items() if key in active_ids} | {
            channel_id: deepcopy(snapshot)
        }
        saved = {
            "version": 2,
            "channels": {
                key: {
                    "fingerprint": value.fingerprint,
                    "checked": value.checked,
                    "models": value.rows,
                    "legacy": value.legacy,
                    "discovery": {
                        "status": value.status,
                        "checkedAt": value.checked_at,
                        "lastSuccessAt": value.last_success_at,
                        "reason": value.reason,
                        "failures": value.failures,
                    },
                }
                for key, value in proposed.items()
            },
        }
        payload = json.dumps(saved, ensure_ascii=False, allow_nan=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.path)
        self._snapshots = proposed
