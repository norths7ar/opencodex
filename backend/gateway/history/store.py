"""Local storage of stateless Responses history and continuation route bindings."""

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from .replay import ensure_history_issuer, scrub_subscription_replay

_TRANSIENT_TTL = 24 * 60 * 60


class _HistoryRow(NamedTuple):
    channel_id: str
    model: str
    input_json: str
    issuer: str | None


def _expanded_input(body: dict) -> list:
    items = body.get("input", [])
    if isinstance(items, str):
        return [{"role": "user", "content": items}]
    if not isinstance(items, list):
        raise ValueError("Responses history expects text or a list as input.")
    return items


def _reject_foreign_messages_files(items: list) -> None:
    # Native thinking signatures are not ChatGPT ciphertext and remain readable.
    pending = list(items)
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if item.get("file_id") or item.get("file_ids") or item.get("type") == "item_reference":
                raise ValueError(
                    "This continuation contains files or references from a different Claude account."
                )
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _response_id(result: dict) -> str | None:
    if result.get("object") != "response" or result.get("status") == "failed":
        return None
    identity = result.get("id")
    return identity if isinstance(identity, str) and identity else None


class HistoryStore:
    def __init__(self, directory: Path, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._path = Path(directory) / "responses.sqlite3"
        self._clock = clock
        self._responses: dict[str, tuple[float, _HistoryRow]] = {}
        self._routes: dict[str, tuple[float, tuple[str, str]]] = {}

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path)
        try:
            with db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS responses "
                    "(id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL, "
                    "input TEXT NOT NULL, issuer TEXT)"
                )
                ensure_history_issuer(db)
                db.execute(
                    "CREATE TABLE IF NOT EXISTS fallback_responses "
                    "(id TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL)"
                )
                yield db
        finally:
            db.close()

    def prune_responses(self) -> None:
        cutoff = self._clock() - _TRANSIENT_TTL
        for identity, (created, _) in list(self._responses.items()):
            if created < cutoff:
                del self._responses[identity]
        for identity, (created, _) in list(self._routes.items()):
            if created < cutoff:
                del self._routes[identity]

    def expand_history(
        self,
        body: dict,
        channel_id: str,
        model: str,
        issuer: str | None = None,
        *,
        anthropic: bool = False,
    ) -> tuple[dict, bool]:
        previous = body.get("previous_response_id")
        if not previous:
            return body, False
        self.prune_responses()
        cached = self._responses.get(previous)
        if cached:
            row = cached[1]
        else:
            with self._connect() as db:
                stored = db.execute(
                    "SELECT provider, model, input, issuer FROM responses WHERE id = ?", (previous,)
                ).fetchone()
            if stored is None:
                raise ValueError(
                    "The previous response is not available in this gateway's translated response history."
                )
            row = _HistoryRow(*stored)
        if (row.channel_id, row.model) != (channel_id, model):
            raise ValueError("The previous response belongs to a different provider or model.")
        if issuer is None and row.issuer is not None:
            raise ValueError(
                "The previous response belongs to a subscription account; start a new conversation for this API channel."
            )
        expanded = {**body, "input": json.loads(row.input_json) + _expanded_input(body)}
        changed = issuer is not None and row.issuer != issuer
        if changed and anthropic:
            _reject_foreign_messages_files(expanded["input"])
            return expanded, True
        return (scrub_subscription_replay(expanded) if changed else expanded), changed

    def save_response(
        self,
        channel_id: str,
        model: str,
        body: dict,
        result: dict,
        issuer: str | None = None,
        *,
        fallback_origin: str | None = None,
    ) -> bool:
        """Save a replayable terminal; commit its route only with valid history."""
        identity = _response_id(result)
        output = result.get("output")
        if identity is None or not isinstance(output, list):
            return False
        if result.get("status") == "incomplete":
            details = result.get("incomplete_details")
            if not isinstance(details, dict) or details.get("reason") != "max_output_tokens":
                return False
        items = json.dumps(_expanded_input(body) + output, ensure_ascii=False)
        row = _HistoryRow(channel_id, model, items, issuer)
        route = (fallback_origin, f"{channel_id}/{model}") if fallback_origin else None
        if body.get("store") is False:
            self.prune_responses()
            now = self._clock()
            self._responses[identity] = (now, row)
            if route:
                self._routes[identity] = (now, route)
            return True
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO responses (id, provider, model, input, issuer) "
                "VALUES (?, ?, ?, ?, ?)",
                (identity, *row),
            )
            if route:
                self._write_route(db, identity, route)
        return True

    @staticmethod
    def _write_route(db: sqlite3.Connection, identity: str, route: tuple[str, str]) -> None:
        db.execute(
            "INSERT OR REPLACE INTO fallback_responses (id, source, target) VALUES (?, ?, ?)",
            (identity, *route),
        )

    def bind_fallback_response(
        self, channel_id: str, model: str, body: dict, result: dict, origin: str | None
    ) -> None:
        """Bind an upstream-owned response's route without storing its history."""
        identity = _response_id(result)
        if not origin or identity is None:
            return
        route = (origin, f"{channel_id}/{model}")
        if body.get("store") is False:
            self.prune_responses()
            self._routes[identity] = (self._clock(), route)
        else:
            with self._connect() as db:
                self._write_route(db, identity, route)

    def fallback_response_route(self, identity: str | None) -> tuple[str, str] | None:
        if not isinstance(identity, str) or not identity:
            return None
        self.prune_responses()
        if identity in self._routes:
            return self._routes[identity][1]
        with self._connect() as db:
            return db.execute(
                "SELECT source, target FROM fallback_responses WHERE id = ?", (identity,)
            ).fetchone()
