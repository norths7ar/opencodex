"""Windows cross-process file locks."""

import msvcrt
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def instance_lock(directory: Path, name: str):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError(
                "Another ocx command or instance is using this state directory."
            ) from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
