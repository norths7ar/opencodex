"""Transactional configuration and private credential records.

A configuration revision and its credential changes commit together. Readers get
an independent snapshot; account grants and runtime state have separate owners.
"""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from .model_identity import remember_selector_collisions, selector_collisions
from .models import Configuration, CredentialRecord

T = TypeVar("T")


class RevisionConflict(ValueError):
    """The edited configuration changed after it was read."""


@dataclass
class ConfigurationState:
    configuration: Configuration
    credentials: dict[str, CredentialRecord] = field(default_factory=dict, repr=False)
    revision: int = 0


class ConfigurationStore:
    def __init__(self, directory: Path):
        self.path = directory / "configuration.sqlite3"

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS configuration "
            "(singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
            "revision INTEGER NOT NULL, document TEXT NOT NULL, credentials TEXT NOT NULL)"
        )
        return connection

    @staticmethod
    def _read(connection) -> ConfigurationState:
        row = connection.execute(
            "SELECT revision, document, credentials FROM configuration WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return ConfigurationState(Configuration())
        revision, document, credentials = row
        config = json.loads(document)
        models = config.get("models") if isinstance(config, dict) else None
        # Older v2 documents mixed exact IDs and lossy Codex selectors.
        if isinstance(models, dict) and "disabled" in models and "legacy_disabled" not in models:
            models["legacy_disabled"] = models.pop("disabled")
        return ConfigurationState(
            Configuration.model_validate(config),
            {
                key: CredentialRecord.model_validate(value)
                for key, value in json.loads(credentials).items()
            },
            revision,
        )

    def read(self) -> ConfigurationState:
        with closing(self._connect()) as connection:
            return self._read(connection)

    def update(
        self,
        change: Callable[[ConfigurationState], T],
        *,
        expected_revision: int | None = None,
    ) -> tuple[ConfigurationState, T]:
        """Apply one local mutation under a transaction; callbacks perform no I/O."""
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._read(connection)
            if expected_revision is not None and state.revision != expected_revision:
                raise RevisionConflict("Configuration changed; reload before saving.")
            previous = selector_collisions(state.configuration)
            result = change(state)
            remember_selector_collisions(previous, state.configuration)
            # Assignment may occur inside a callback. Revalidate the resulting document.
            document = state.configuration.model_dump(mode="json", exclude_unset=True)
            document["schema_version"] = 2
            if "disabled" in document.get("models", {}):
                document["models"].setdefault("legacy_disabled", [])
            state.configuration = Configuration.model_validate(document)
            identities = {channel.id for channel in state.configuration.channels}
            if state.credentials.keys() - identities:
                raise ValueError("Credential records must belong to a configured channel.")
            state.credentials = {
                key: CredentialRecord.model_validate(record.model_dump(exclude_unset=True))
                for key, record in state.credentials.items()
            }
            secrets = {
                key: record.model_dump(mode="json", exclude_unset=True)
                for key, record in state.credentials.items()
            }
            state.revision += 1
            connection.execute(
                "INSERT INTO configuration VALUES (1, ?, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision, "
                "document=excluded.document, credentials=excluded.credentials",
                (state.revision, json.dumps(document), json.dumps(secrets)),
            )
            return state, result

    def install(self, configuration: Configuration, credentials: dict[str, CredentialRecord]):
        """Install a reviewed candidate into an empty store."""

        def replace(state):
            state.configuration = configuration.model_copy(deep=True)
            state.credentials = {
                key: value.model_copy(deep=True) for key, value in credentials.items()
            }

        state, _ = self.update(replace, expected_revision=0)
        return state
