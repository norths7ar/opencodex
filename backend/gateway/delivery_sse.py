"""Incremental SSE inspection that leaves native response bytes untouched."""

import codecs
import json
import re
from collections.abc import AsyncIterable, AsyncIterator, Iterator

_NEWLINE = re.compile(r"\r\n|\r|\n")
MAX_SSE_FRAME_CHARS = 4 * 1024 * 1024


class UpstreamBodyOverflow(ValueError):
    status = 502
    code = "body_overflow"

    def __init__(self, *, upstream_error_status: int | None = None):
        super().__init__("Upstream response exceeded the size limit.")
        self.upstream_error_status = upstream_error_status


class SSELines:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")("replace")
        self._pending: list[str] = []
        self._cr = False
        self._frame_chars = 0

    def _add(self, length: int) -> None:
        self._frame_chars += length
        if self._frame_chars > MAX_SSE_FRAME_CHARS:
            raise UpstreamBodyOverflow()

    def feed(self, data: bytes, *, final: bool = False) -> Iterator[str]:
        chunk = ("\r" if self._cr else "") + self._decoder.decode(data, final=final)
        self._cr = not final and chunk.endswith("\r")
        if self._cr:
            chunk = chunk[:-1]
        start = 0
        for sep in _NEWLINE.finditer(chunk):
            self._add(sep.start() - start)
            line = "".join([*self._pending, chunk[start : sep.start()]])
            self._pending.clear()
            start = sep.end()
            if line:
                self._add(1)
            else:
                self._frame_chars = 0
            yield line
        if start < len(chunk):
            self._add(len(chunk) - start)
            self._pending.append(chunk[start:])
        if final and self._pending:
            yield "".join(self._pending)
            self._pending.clear()


class ByteLines:
    """Adapt caller-owned, cancellation-aware chunks to the protocol decoder."""

    def __init__(self, source: AsyncIterable[bytes]) -> None:
        self._source = source

    async def aiter_lines(self) -> AsyncIterator[str]:
        decoder = SSELines()
        async for chunk in self._source:
            for line in decoder.feed(chunk):
                yield line
        for line in decoder.feed(b"", final=True):
            yield line


class SSEObserver:
    """Decode data fields without changing native bytes or requiring valid JSON."""

    def __init__(self) -> None:
        self._decoder = SSELines()
        self._data: list[str] = []

    def feed(self, chunk: bytes, *, final: bool = False) -> Iterator[dict]:
        for line in self._decoder.feed(chunk, final=final):
            if not line:
                yield from self._frame()
            elif line.startswith("data:"):
                self._data.append(line[5:].lstrip(" "))
        if final:
            yield from self._frame()

    def _frame(self) -> Iterator[dict]:
        if not self._data:
            return
        value = "\n".join(self._data)
        self._data.clear()
        if value == "[DONE]":
            yield {"type": "ocx.done"}
            return
        try:
            parsed = json.loads(value)
        except ValueError, RecursionError:
            return
        if isinstance(parsed, dict):
            yield parsed
