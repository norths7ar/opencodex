"""Prepare a versioned migration candidate in a separate directory."""

import hashlib
import json
from pathlib import Path

from .migration import MigrationResult, migrate_legacy


def migration_summary(result: MigrationResult, source_digest: str) -> dict:
    return {
        "source_sha256": source_digest,
        "schema_version": result.configuration.schema_version,
        "channels": [channel.id for channel in result.configuration.channels],
        "credential_records": len(result.credentials),
        "mapping_complete": result.complete,
        "unmapped_fields": sorted(result.remainder),
        "retired_fields": list(result.retired_fields),
        "activated": False,
    }


def prepare_snapshot(source: Path, destination: Path | None = None) -> dict:
    """Read explicitly named input; never activate it or update the input file."""
    original = source.read_bytes()
    try:
        raw = json.loads(original.decode("utf-8-sig"))
    except ValueError, UnicodeError:
        raise ValueError("The source is not a UTF-8 JSON configuration.") from None
    result = migrate_legacy(raw)
    summary = migration_summary(result, hashlib.sha256(original).hexdigest())
    if destination is None:
        return summary

    # A fresh directory makes a candidate distinct from an active state directory.
    destination.mkdir(parents=True, exist_ok=False)
    documents = {
        "config.json": result.configuration.model_dump(mode="json", exclude_unset=True),
        "credentials.json": {
            "schema_version": 2,
            "channels": {
                identity: record.model_dump(mode="json", exclude_unset=True)
                for identity, record in result.credentials.items()
            },
        },
        "unmapped.json": result.remainder,
        "migration-report.json": summary,
    }
    # Explicitly include the discriminator even when Configuration supplied its default.
    documents["config.json"]["schema_version"] = 2
    for filename, data in documents.items():
        with (destination / filename).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return summary
