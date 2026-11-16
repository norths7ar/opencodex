"""Preview a Lite configuration upgrade, optionally writing an inactive candidate."""

import argparse
import json
from pathlib import Path

from .snapshot import prepare_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Saved Lite config.json to inspect")
    parser.add_argument("--output", type=Path, help="New directory for the candidate files")
    options = parser.parse_args()
    try:
        summary = prepare_snapshot(options.source, options.output)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Configuration conversion failed: {exc}\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["mapping_complete"]:
        parser.exit(1)


if __name__ == "__main__":
    main()
