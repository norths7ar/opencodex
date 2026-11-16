"""Bounded, content-encoding-aware JSON request admission."""

import compression.zstd
import json
import math
import zlib

from fastapi import Request

MAX_REQUEST_BYTES = 256 * 1024 * 1024


class RequestBodyError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _decode(data: bytes, encoding: str, limit: int) -> bytes:
    if encoding in {"", "identity"}:
        return data
    if encoding == "zstd":
        try:
            decoder = compression.zstd.ZstdDecompressor()
            result = decoder.decompress(data, max_length=limit + 1)
            if len(result) > limit:
                raise RequestBodyError(413, "Request body exceeds the size limit.")
            if not decoder.eof:
                raise ValueError("Incomplete compressed body")
            return result
        except (compression.zstd.ZstdError, ValueError) as exc:
            if isinstance(exc, RequestBodyError):
                raise
            raise RequestBodyError(400, "Invalid compressed request body.") from exc
    for mode in (31,) if encoding in {"gzip", "x-gzip"} else (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        try:
            decoder = zlib.decompressobj(mode)
            result = decoder.decompress(data, limit + 1)
            if len(result) > limit:
                raise RequestBodyError(413, "Request body exceeds the size limit.")
            if decoder.eof:
                return result
        except zlib.error:
            continue
    raise RequestBodyError(400, "Invalid compressed request body.")


def resolve_limit(configured):
    if not isinstance(configured, (int, float)) or not math.isfinite(configured) or configured <= 0:
        return MAX_REQUEST_BYTES
    return min(max(math.floor(configured), 1024 * 1024), 512 * 1024 * 1024)


async def read_json(request: Request, configured_limit=None):
    limit = resolve_limit(configured_limit)
    encoding = request.headers.get("content-encoding", "").strip().lower()
    if encoding not in {"", "identity", "gzip", "x-gzip", "deflate", "zstd"}:
        raise RequestBodyError(415, "Unsupported content encoding.")
    length = request.headers.get("content-length", "")
    if length.isascii() and length.isdecimal() and int(length) > limit:
        raise RequestBodyError(413, "Request body exceeds the size limit.")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > limit:
            raise RequestBodyError(413, "Request body exceeds the size limit.")
        content.extend(chunk)
    try:
        return json.loads(_decode(content, encoding, limit))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestBodyError(400, "Invalid JSON request body.") from exc
