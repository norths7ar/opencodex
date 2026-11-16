"""Install a complete legacy conversion once, preserving its source file."""

import json

from backend.catalog.selection import reconcile_discovery

from .migration import migrate_legacy
from .models import Configuration


def activate(store):
    current = store.read()
    if current.revision:
        return current
    source = store.path.parent / "config.json"
    if not source.is_file():
        return store.install(Configuration(), {})
    result = migrate_legacy(json.loads(source.read_text(encoding="utf-8")))
    if not result.complete:
        fields = ", ".join(sorted(result.remainder))
        raise ValueError(f"Configuration migration has unmapped fields: {fields}")
    configuration = result.configuration
    for channel in configuration.channels:
        if not channel.models.live:
            configuration = reconcile_discovery(configuration, channel.id, channel.models.static)
    return store.install(configuration, result.credentials)
