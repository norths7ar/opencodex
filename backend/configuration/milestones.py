"""Durable product milestones, independent of request-log retention and configuration edits."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


class Milestones:
    def __init__(self, directory: Path):
        self.path = directory / "configuration.sqlite3"

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS milestones (name TEXT PRIMARY KEY, completed_at TEXT NOT NULL)"
        )
        return connection

    def first_success_at(self) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT completed_at FROM milestones WHERE name = 'first_success'"
            ).fetchone()
            return row[0] if row else None

    def record_success(self, timestamp: str | None = None) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO milestones VALUES ('first_success', ?)",
                (timestamp or datetime.now(UTC).isoformat(),),
            )
