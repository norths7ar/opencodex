"""Bundled capability hints for selected destinations, never a discovery roster."""

import json
from pathlib import Path

from backend.configuration.models import ModelMetadata

_METADATA = json.loads(Path(__file__).with_name("model_metadata.json").read_text(encoding="utf-8"))


def preset_metadata(channel, model):
    declaration = _METADATA.get(channel.endpoints.base_url.rstrip("/"))
    if declaration is None:
        return None
    return ModelMetadata.model_validate(
        {
            **declaration["defaults"],
            **declaration["models"].get(model, {}),
        }
    )
