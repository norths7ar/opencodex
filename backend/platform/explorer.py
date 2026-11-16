"""Reveal trusted local paths in Windows File Explorer."""

import os
from pathlib import Path


def open_location(directory: Path, *, filename: str | None = None) -> None:
    if os.name != "nt":
        raise NotImplementedError("Opening a location requires Windows File Explorer.")
    directory = directory.absolute()
    if not directory.is_dir():
        raise FileNotFoundError("The state directory does not exist.")
    if filename and (item := directory / filename).is_file():
        windows = os.environ.get("SYSTEMROOT")
        if not windows:
            raise FileNotFoundError("Could not locate Windows File Explorer.")
        explorer = Path(windows) / "explorer.exe"
        os.startfile(str(explorer), arguments=f'/select,"{item}"')
    else:
        os.startfile(str(directory), "explore")
