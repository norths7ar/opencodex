"""Export the management DTO definitions without starting the gateway."""

import argparse
import json
from pathlib import Path

from backend.application.contracts import (
    ApiError,
    ChannelConfigurationEdit,
    ChannelConfigurationSaved,
    ChannelDefaultSaved,
    ChannelDetail,
    ChannelList,
    ChannelSaved,
    DiscoveryState,
    ModelOverrideUpdate,
    SaveChannel,
)
from backend.application.runtime_contracts import Overview, StartupHealth


def management_schemas() -> dict:
    contracts = (
        ApiError,
        ChannelDetail,
        ChannelList,
        ChannelConfigurationEdit,
        ChannelConfigurationSaved,
        ChannelDefaultSaved,
        ChannelSaved,
        DiscoveryState,
        ModelOverrideUpdate,
        SaveChannel,
        Overview,
        StartupHealth,
    )
    return {
        "version": 2,
        "contracts": {model.__name__: model.model_json_schema() for model in contracts},
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
