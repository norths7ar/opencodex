"""Preview a Lite configuration upgrade, optionally writing an inactive candidate."""

import argparse
import json
from pathlib import Path

from .snapshot import prepare_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Saved Lite config.json to inspect")
    parser.add_argument(
        "--output", type=Path, help="New directory for the redacted migration report"
    )
    parser.add_argument(
        "--include-sensitive",
        action="store_true",
        help="Also export a complete candidate, including credentials and unmapped secret values",
    )
    options = parser.parse_args()
    if options.include_sensitive and options.output is None:
        parser.error("--include-sensitive requires --output")
    try:
        summary = prepare_snapshot(
            options.source, options.output, include_sensitive=options.include_sensitive
        )
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Configuration conversion failed: {exc}\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["mapping_complete"]:
        parser.exit(1)


if __name__ == "__main__":
    main()
