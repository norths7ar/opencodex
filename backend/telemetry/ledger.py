"""Durable gateway metadata; never stores request/response bodies or headers."""

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Lock

LOG = logging.getLogger(__name__)
MIN_USAGE_LEDGER_MAX_BYTES = 1024 * 1024
CLIENTS = {
    "dsh",
    "general",
    "codex_cli",
    "codex_desktop",
    "codex_ide",
    "claude_code",
    "claude_desktop",
    "grok",
    "opencode",
    "aside",
    "other",
    "unknown",
}
SURFACES = {"codex", "claude", "claude-desktop", "grok", "other"}
_META = {
    "request_id",
    "purpose",
    "provider",
    "model",
    "upstream_model",
    "served_model",
    "requested_model",
    "adapter",
    "surface",
    "request_client",
    "api_key_id",
    "first_token_ms",
    "streaming",
    "account_label",
    "conversation_id",
    "inbound_protocol",
    "protocol_mode",
    "price_family",
    "price_timestamp",
    "admission_kind",
    "service_tier",
    "requested_service_tier",
    "response_service_tier",
    "gen_start_ms",
    "last_output_ms",
    "sent_upstream",
    "send_count",
    "settled_sends",
    "auth_mode",
    "cache_provenance",
    "upstream_wire",
    "fallback_attempts",
    "fallback_refusal",
}


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def conversation_digest(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 4096 or re.search(r"[\x00-\x1f\x7f]", value):
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def _label(value: object) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= 512:
        return None
    if re.search(r"[\x00-\x1f\x7f]", value) or re.search(
        r"(?:bearer\s|sk-[A-Za-z0-9_-]{12,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.)",
        value,
        re.IGNORECASE,
    ):
        return None
    return value


def _count(value: object) -> int | None:
    if type(value) is int and 0 <= value <= (1 << 53) - 1:
        return value
    return None


def _metadata(values: dict) -> dict:
    data = {}
    for key, value in values.items():
        if key not in _META or value is None:
            continue
        if key == "fallback_attempts":
            if not isinstance(value, list):
                continue
            attempts = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                status = _count(item.get("status"))
                if status is None or not 100 <= status <= 599:
                    continue
                attempt = _metadata({k: v for k, v in item.items() if k != "fallback_attempts"})
                usage = normalize_usage(item.get("usage"))
                attempt.update(
                    status=status,
                    usage=usage,
                    usage_status="reported" if usage else "unreported",
                    duration_ms=_count(item.get("duration_ms")) or 0,
                )
                if error := _label(item.get("error_code")):
                    attempt["error_code"] = error
                attempts.append(attempt)
            value = attempts
        elif key in {"streaming", "sent_upstream"}:
            value = value if type(value) is bool else None
        elif key in {"first_token_ms", "gen_start_ms", "last_output_ms"}:
            value = (
                _count(round(value)) if type(value) in {int, float} and 0 <= value < 1e12 else None
            )
        elif key in {"send_count", "settled_sends", "price_timestamp"}:
            value = _count(value)
        elif key == "request_client":
            value = value if isinstance(value, str) and value in CLIENTS else "unknown"
        elif key == "surface":
            value = value if isinstance(value, str) and value in SURFACES else "other"
        elif key == "upstream_wire":
            value = "chat" if value == "chat/completions" else _label(value)
        elif key in {"service_tier", "requested_service_tier", "response_service_tier"}:
            tiers = {
                "priority",
                "fast",
                "ultrafast",
                "default",
                "standard",
                "auto",
                "flex",
                "scale",
                "batch",
            }
            value = value.strip().casefold() if isinstance(value, str) else None
            value = value if value in tiers else None
        elif key == "conversation_id":
            value = conversation_digest(value)
        else:
            value = _label(value)
        if value is not None:
            data[key] = value
    return data


def normalize_usage(raw: dict | None) -> dict | None:
    """Normalize protocol counters without treating absent telemetry as zero usage."""
    if not isinstance(raw, dict):
        return None

    def first(*values):
        return next((item for value in values if (item := _count(value)) is not None), None)

    input_tokens = first(raw.get("inputTokens"), raw.get("input_tokens"), raw.get("prompt_tokens"))
    output = first(raw.get("outputTokens"), raw.get("output_tokens"), raw.get("completion_tokens"))
    total = first(raw.get("totalTokens"), raw.get("total_tokens"))
    input_details = raw.get("input_tokens_details") or raw.get("prompt_tokens_details") or {}
    output_details = raw.get("output_tokens_details") or raw.get("completion_tokens_details") or {}
    read = first(
        raw.get("cacheReadInputTokens"),
        raw.get("cachedInputTokens"),
        raw.get("cache_read_input_tokens"),
        raw.get("prompt_cache_hit_tokens"),
        input_details.get("cached_tokens") if isinstance(input_details, dict) else None,
    )
    write = first(
        raw.get("cacheCreationInputTokens"),
        raw.get("cache_creation_input_tokens"),
        input_details.get("cache_write_tokens") if isinstance(input_details, dict) else None,
    )
    if all(value is None for value in (input_tokens, output, read, write)):
        return None
    input_tokens = input_tokens or 0
    output = output or 0
    split = raw.get("cache_creation")
    write_1h = first(
        raw.get("cacheCreation1hInputTokens"),
        split.get("ephemeral_1h_input_tokens") if isinstance(split, dict) else None,
        input_details.get("cache_write_1h_tokens") if isinstance(input_details, dict) else None,
    )
    if "input_tokens" in raw and (
        "cache_read_input_tokens" in raw or "cache_creation_input_tokens" in raw
    ):
        # Anthropic input excludes both cache buckets, unlike OpenAI and OcxUsage.
        input_tokens += (read or 0) + (write or 0)
    reasoning = first(
        raw.get("reasoningOutputTokens"),
        output_details.get("reasoning_tokens") if isinstance(output_details, dict) else None,
    )
    data = {
        "inputTokens": input_tokens,
        "outputTokens": output,
        "totalTokens": max(input_tokens + output, total or 0),
    }
    if read is not None:
        data.update(cacheReadInputTokens=read, cachedInputTokens=read)
    if write is not None:
        data["cacheCreationInputTokens"] = write
        if write_1h is not None:
            data["cacheCreation1hInputTokens"] = min(write, write_1h)
    if reasoning is not None:
        data["reasoningOutputTokens"] = reasoning
    if raw.get("estimated") is True:
        data["estimated"] = True
    return data


class UsageStore:
    """Small indexed ledger. Writes fail open for inference, but reports fail visibly."""

    def __init__(self, directory: Path, *, max_bytes: int | None = None):
        self.path = directory / "usage.sqlite3"
        # JSON's numeric syntax can express an integer using a decimal or
        # exponent; match the original safe-integer validation, excluding bool.
        self.max_bytes = (
            int(max_bytes)
            if type(max_bytes) in {int, float}
            and MIN_USAGE_LEDGER_MAX_BYTES <= max_bytes < (1 << 53)
            and max_bytes == int(max_bytes)
            else None
        )
        self._initialized = False
        self._init_lock = Lock()
        self._retention_lock = Lock()

    @contextmanager
    def _connection(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            with self._init_lock:
                if not self._initialized:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("""CREATE TABLE IF NOT EXISTS requests (
                        id INTEGER PRIMARY KEY, timestamp INTEGER NOT NULL,
                        metadata TEXT NOT NULL, result TEXT
                    )""")
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS requests_timestamp ON requests(timestamp)"
                    )
                    conn.execute("""CREATE TABLE IF NOT EXISTS retention (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        entries_dropped INTEGER NOT NULL,
                        bytes_dropped INTEGER NOT NULL,
                        newest_dropped INTEGER NOT NULL
                    )""")
                    conn.execute("""CREATE TABLE IF NOT EXISTS attempts (
                        id INTEGER PRIMARY KEY, request_row_id INTEGER NOT NULL,
                        request_id TEXT NOT NULL, attempt_id TEXT NOT NULL UNIQUE,
                        purpose TEXT NOT NULL, reason TEXT NOT NULL,
                        timestamp INTEGER NOT NULL, metadata TEXT NOT NULL, result TEXT,
                        FOREIGN KEY(request_row_id) REFERENCES requests(id) ON DELETE CASCADE
                    )""")
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS attempts_request ON attempts(request_row_id)"
                    )
                    conn.execute(
                        "UPDATE attempts SET result = ? WHERE result IS NULL",
                        (
                            json.dumps(
                                {
                                    "status": 503,
                                    "usage_status": "unreported",
                                    "error_code": "gateway_interrupted",
                                    "duration_ms": 0,
                                }
                            ),
                        ),
                    )
                    # Rows left unfinished by an unclean shutdown are not successful sends.
                    conn.execute(
                        "UPDATE requests SET result = ? WHERE result IS NULL",
                        (
                            json.dumps(
                                {
                                    "status": 503,
                                    "usage_status": "unreported",
                                    "error_code": "gateway_interrupted",
                                    "duration_ms": 0,
                                }
                            ),
                        ),
                    )
                    conn.commit()
                    self._initialized = True
            yield conn

    async def _write(self, fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except sqlite3.Error:
            LOG.exception("Could not record gateway request metadata")
            return None

    async def begin(self, *, method: str, path: str, **values) -> int | None:
        # Path is a registered protocol endpoint, never a raw URL or query string.
        inbound = {
            "/v1/responses": "responses",
            "/v1/responses/compact": "responses",
            "/v1/chat/completions": "chat",
            "/v1/messages": "messages",
            "/v1/messages/count_tokens": "messages",
            "/v1/alpha/search": "alpha/search",
        }.get(path)
        if values.get("surface") not in SURFACES:
            client = values.get("request_client")
            if client in {"codex_cli", "codex_desktop", "codex_ide"}:
                values["surface"] = "codex"
            elif client == "claude_desktop":
                values["surface"] = "claude-desktop"
            elif client == "grok":
                values["surface"] = "grok"
            elif client == "claude_code" or inbound == "messages":
                values["surface"] = "claude"
            else:
                values["surface"] = "other"
        metadata = _metadata({"inbound_protocol": inbound, "request_client": "unknown", **values})
        return await self._write(self._insert, now_ms(), metadata)

    def _insert(self, timestamp, metadata):
        with self._connection() as conn:
            return conn.execute(
                "INSERT INTO requests(timestamp, metadata) VALUES (?, ?)",
                (timestamp, json.dumps(metadata)),
            ).lastrowid

    async def update(self, request_id: int | None, **values):
        if request_id is not None and (metadata := _metadata(values)):
            await self._write(self._update, request_id, metadata)

    def _update(self, request_id, metadata):
        with self._connection() as conn:
            conn.execute(
                "UPDATE requests SET metadata = json_patch(metadata, ?) WHERE id = ? AND result IS NULL",
                (json.dumps(metadata), request_id),
            )

    async def finalize(
        self,
        request_id: int | None,
        *,
        status: int,
        usage: dict | None = None,
        usage_status: str | None = None,
        error_code: str | None = None,
        error_type: str | None = None,
        cancelled: bool = False,
        duration_ms: float | None = None,
        first_token_ms: float | None = None,
        terminal_status: str | None = None,
        close_reason: str | None = None,
        **values,
    ):
        if request_id is None:
            return
        counters = normalize_usage(usage)
        if usage_status not in {"unsupported", "estimated", "reported", "unreported"}:
            usage_status = "estimated" if counters and counters.get("estimated") else "reported"
        if counters is None and usage_status != "unsupported":
            usage_status = "unreported"
        result = {
            "status": 499 if cancelled else status,
            "usage_status": usage_status,
            "usage": counters,
            "duration_ms": round(duration_ms)
            if type(duration_ms) in {int, float} and 0 <= duration_ms < 1e12
            else None,
            "error_code": error_code
            if isinstance(error_code, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", error_code)
            else None,
            "terminal_status": terminal_status
            if terminal_status in {"completed", "incomplete", "failed", "cancelled"}
            else None,
            "close_reason": close_reason
            if close_reason
            in {"terminal", "client_cancel", "non_stream", "body_stall", "body_overflow"}
            else None,
        }
        await self._write(
            self._finalize,
            request_id,
            result,
            _metadata({"first_token_ms": first_token_ms, **values}),
        )

    def _finalize(self, request_id, result, metadata):
        with self._connection() as conn:
            existing = conn.execute(
                "SELECT timestamp FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
            if existing is None:
                return
            if result["duration_ms"] is None:
                result["duration_ms"] = max(0, now_ms() - existing["timestamp"])
            updated = conn.execute(
                "UPDATE requests SET metadata = json_patch(metadata, ?), result = ? WHERE id = ? AND result IS NULL",
                (json.dumps(metadata), json.dumps(result), request_id),
            )
            if updated.rowcount and self.max_bytes is not None:
                # Publish the request result even if cleanup is delayed by a reader or fails.
                conn.commit()
                if self._retention_lock.acquire(blocking=False):
                    try:
                        self._enforce_limit(conn, self.max_bytes)
                    finally:
                        self._retention_lock.release()

    @staticmethod
    def _allocated_bytes(conn):
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        return page_size * conn.execute("PRAGMA page_count").fetchone()[0]

    def _enforce_limit(self, conn, max_bytes):
        if self._allocated_bytes(conn) <= max_bytes:
            return
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        target_pages = (max_bytes * 9 // 10) // page_size
        # A finalized row and its deletion cannot race: the writer transaction
        # protects the selected prefix. Preserve every unfinished row and at
        # least the newest finalized row even if the ceiling cannot fit them.
        conn.execute("BEGIN IMMEDIATE")
        newest = conn.execute(
            "SELECT id FROM requests WHERE result IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        dropped = payload_bytes = newest_dropped = 0
        while newest is not None:
            allocated = conn.execute("PRAGMA page_count").fetchone()[0]
            free = conn.execute("PRAGMA freelist_count").fetchone()[0]
            if allocated - free <= target_pages:
                break
            rows = conn.execute(
                """
                SELECT id, timestamp, length(CAST(metadata AS BLOB)) + length(CAST(result AS BLOB)) AS bytes
                FROM requests WHERE result IS NOT NULL AND id < ? ORDER BY id LIMIT 32
            """,
                (newest[0],),
            ).fetchall()
            if not rows:
                break
            conn.executemany("DELETE FROM requests WHERE id = ?", ((row["id"],) for row in rows))
            dropped += len(rows)
            payload_bytes += sum(row["bytes"] for row in rows)
            newest_dropped = max(newest_dropped, *(row["timestamp"] for row in rows))
        if dropped:
            conn.execute(
                """
                INSERT INTO retention VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    entries_dropped = entries_dropped + excluded.entries_dropped,
                    bytes_dropped = bytes_dropped + excluded.bytes_dropped,
                    newest_dropped = max(newest_dropped, excluded.newest_dropped)
            """,
                (dropped, payload_bytes, newest_dropped),
            )
        conn.commit()
        if dropped or conn.execute("PRAGMA freelist_count").fetchone()[0]:
            # SQLite DELETE alone leaves free pages in the file. Reclaim them
            # only when the opt-in ceiling is crossed, not on every request.
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def retention_status(self):
        """Allocated database bytes (not transient WAL bytes) and the optional ceiling."""
        with self._connection() as conn:
            status = {"currentBytes": self._allocated_bytes(conn)}
        if self.max_bytes is not None:
            status["maxBytes"] = self.max_bytes
        return status

    def retention_history(self):
        with self._connection() as conn:
            row = conn.execute(
                "SELECT entries_dropped, bytes_dropped, newest_dropped FROM retention WHERE id = 1"
            ).fetchone()
            return tuple(row) if row else (0, 0, None)

    def rows(self, *, since=None, until=None, logs=False):
        """Yield a consistent snapshot, bounded to the original 2,000-row log window."""
        with self._connection() as conn:
            if logs:
                rows = conn.execute(
                    "SELECT * FROM (SELECT * FROM requests ORDER BY id DESC LIMIT 2000) ORDER BY id"
                )
            else:
                conditions = ["result IS NOT NULL"]
                args = []
                for op, bound in ((">=", since), ("<=", until)):
                    if bound is not None:
                        conditions.append(f"timestamp {op} ?")
                        args.append(bound)
                rows = conn.execute(
                    f"SELECT * FROM requests WHERE {' AND '.join(conditions)} ORDER BY timestamp, id",
                    args,
                )
            for row in rows:
                yield {
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    **json.loads(row["metadata"]),
                    **(json.loads(row["result"]) if row["result"] else {}),
                }

    async def begin_send(
        self, request_row_id, request_id, attempt_id, purpose, reason, timestamp, metadata
    ):
        return await self._write(
            self._insert_send,
            request_row_id,
            request_id,
            attempt_id,
            purpose,
            reason,
            timestamp,
            _metadata(metadata),
        )

    def _insert_send(
        self, request_row_id, request_id, attempt_id, purpose, reason, timestamp, metadata
    ):
        with self._connection() as conn:
            parent = conn.execute(
                "SELECT metadata FROM requests WHERE id = ? AND result IS NULL", (request_row_id,)
            ).fetchone()
            if parent is None or json.loads(parent["metadata"]).get("request_id") != request_id:
                raise ValueError("Attempt does not belong to an active logical request.")
            return conn.execute(
                "INSERT INTO attempts(request_row_id,request_id,attempt_id,purpose,reason,timestamp,metadata) VALUES (?,?,?,?,?,?,?)",
                (
                    request_row_id,
                    request_id,
                    attempt_id,
                    purpose,
                    reason,
                    timestamp or now_ms(),
                    json.dumps(metadata),
                ),
            ).lastrowid

    async def finalize_send(self, send_id, result, metadata):
        if send_id is not None:
            await self._write(self._finalize_send, send_id, result, _metadata(metadata))

    def _finalize_send(self, send_id, result, metadata):
        with self._connection() as conn:
            conn.execute(
                "UPDATE attempts SET metadata=json_patch(metadata, ?), result=? WHERE id=? AND result IS NULL",
                (json.dumps(metadata), json.dumps(result), send_id),
            )

    def attempts(self, request_row_id=None):
        with self._connection() as conn:
            query = "SELECT * FROM attempts WHERE result IS NOT NULL"
            args = ()
            if request_row_id is not None:
                query += " AND request_row_id = ?"
                args = (request_row_id,)
            for row in conn.execute(query + " ORDER BY timestamp,id", args):
                yield {
                    "id": row["id"],
                    "request_row_id": row["request_row_id"],
                    "request_id": row["request_id"],
                    "attempt_id": row["attempt_id"],
                    "purpose": row["purpose"],
                    "reason": row["reason"],
                    "timestamp": row["timestamp"],
                    **json.loads(row["metadata"]),
                    **json.loads(row["result"]),
                }

    def cost_rows(self):
        """Physical sends, plus historical logical rows that predate send records."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT r.* FROM requests r WHERE r.result IS NOT NULL AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.request_row_id=r.id) ORDER BY r.timestamp,r.id"
            ).fetchall()
        for row in rows:
            yield {
                "id": row["id"],
                "timestamp": row["timestamp"],
                **json.loads(row["metadata"]),
                **json.loads(row["result"]),
            }
        yield from self.attempts()

    def snapshot_bounds(self):
        with self._connection() as conn:
            return tuple(
                conn.execute("""
                SELECT
                    (SELECT timestamp FROM requests WHERE result IS NOT NULL ORDER BY timestamp LIMIT 1),
                    (SELECT timestamp FROM requests WHERE result IS NOT NULL ORDER BY timestamp DESC LIMIT 1)
            """).fetchone()
            )

    def timeline_rows(self, since, until, max_bytes, visit):
        """Visit matching rows from the bounded newest management snapshot."""
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT id, timestamp, metadata, result FROM requests
                WHERE result IS NOT NULL ORDER BY id DESC LIMIT 500001
            """)
            for i, row in enumerate(rows):
                # The SQLite representation separates metadata and result. Charge
                # both to the read budget; do not parse out-of-window rows.
                max_bytes -= len(row["metadata"].encode()) + len(row["result"].encode())
                if max_bytes < 0 or i == 500_000:
                    return True
                if since <= row["timestamp"] < until:
                    visit(
                        {
                            "id": row["id"],
                            "timestamp": row["timestamp"],
                            **json.loads(row["metadata"]),
                            **json.loads(row["result"]),
                        }
                    )
        return False
