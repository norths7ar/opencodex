"""Shared provider registry."""

import json
from pathlib import Path

REGISTRY = json.loads(
    Path(__file__).with_name("provider_registry.json").read_text(encoding="utf-8")
)
