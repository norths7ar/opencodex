"""Shared safe UTF-8 file operations for client adapters."""

import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path


class ClientConfigError(RuntimeError):
    """A client file operation could not safely complete."""


def assert_regular_file(path: Path, label: str) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ClientConfigError(f"{label} is not a regular file; no changes were made.")


def read_text(path: Path, limit: int, label: str) -> str | None:
    try:
        assert_regular_file(path, label)
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ClientConfigError(f"{label} is too large to edit safely.")
        return data.decode("utf-8")
    except FileNotFoundError:
        return None
    except OSError, UnicodeError:
        raise ClientConfigError(f"Could not read {label} as UTF-8.") from None


def atomic_write(path: Path, text: str, label: str) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as f:
            temporary = Path(f.name)
            f.write(text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        temporary.replace(path)
    except OSError:
        raise ClientConfigError(f"Could not write {label}; check file access and retry.") from None
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
