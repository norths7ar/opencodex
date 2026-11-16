"""Export phase-one management schemas without starting the gateway."""

import argparse
import json
from pathlib import Path

from pydantic import TypeAdapter

from .contracts import (
    ApiError,
    ApplyResult,
    ChannelDetail,
    ChannelView,
    ClientState,
    DiscoveryState,
    ManagementEvent,
    ModelOverrideUpdate,
    ModelSelection,
    RefreshCatalog,
    SaveChannel,
    ServiceState,
)


def management_schemas() -> dict:
    contracts = (
        ApiError,
        ApplyResult,
        ChannelDetail,
        ChannelView,
        ClientState,
        DiscoveryState,
        ModelOverrideUpdate,
        ModelSelection,
        RefreshCatalog,
        SaveChannel,
        ServiceState,
    )
    return {
        "version": 2,
        "contracts": {model.__name__: model.model_json_schema() for model in contracts},
        "events": TypeAdapter(ManagementEvent).json_schema(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(
        json.dumps(management_schemas(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
