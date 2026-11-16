"""Fetch complete directories on demand and in a background task, never on read."""

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import httpx

from backend.configuration.models import Channel, Configuration

from .directory import DirectorySnapshot, DirectoryStore, validate_rows

_DAILY = 24 * 60 * 60
_LOG = logging.getLogger(__name__)


class CatalogRefresh:
    def __init__(
        self,
        directory: Path,
        read_config: Callable[[], Configuration],
        fetch_models: Callable[[Channel], Awaitable[list[dict]]],
        auth_revision: Callable[[Channel], str | None],
        *,
        fingerprint: Callable[[Channel], str] | None = None,
        legacy_fingerprint: Callable[[Channel], str | None] | None = None,
        reconcile: Callable[[Channel, tuple[str, ...]], None] | None = None,
        projection: Callable[[str], object] | None = None,
        on_changed: Callable[[], Awaitable[object]] | None = None,
    ):
        self.store = DirectoryStore(directory)
        self._read_config = read_config
        self._fetch_models = fetch_models
        self._auth_revision = auth_revision
        self._fingerprint = fingerprint or self._default_fingerprint
        self._legacy_fingerprint = legacy_fingerprint or (lambda _channel: None)
        self._reconcile = reconcile
        self._projection = projection
        self._on_changed = on_changed
        self._locks: dict[str, asyncio.Lock] = {}
        self._wake = asyncio.Event()

    def _default_fingerprint(self, channel: Channel) -> str:
        # The revision represents credential/environment changes and account replacement,
        # not a short-lived token's refresh on the same account.
        connection = (
            channel.service_id,
            channel.protocol,
            channel.auth_mode,
            channel.oauth_binding,
            channel.endpoints.base_url,
            channel.endpoints.discovery,
            channel.api_key_transport,
            channel.request_policy.modelDiscoveryClientVersion,
            self._auth_revision(channel),
        )
        return hashlib.sha256(repr(connection).encode()).hexdigest()

    def snapshot(self, channel: Channel) -> DirectorySnapshot | None:
        if channel.disabled or not channel.models.live:
            return None
        snapshot = self.store.read(channel.id)
        if snapshot is None:
            return None
        expected = (
            self._legacy_fingerprint(channel) if snapshot.legacy else self._fingerprint(channel)
        )
        return snapshot if expected == snapshot.fingerprint else None

    def request_refresh(self) -> None:
        """Wake the scheduler after config/auth changes; unchanged snapshots stay fresh."""
        self._wake.set()

    async def refresh(self, channel: Channel, *, force: bool = False) -> DirectorySnapshot | None:
        started = time.time()
        async with self._locks.setdefault(channel.id, asyncio.Lock()):
            current = next((c for c in self._read_config().channels if c.id == channel.id), None)
            if current is None or current.disabled or not current.models.live:
                return None
            fingerprint = self._fingerprint(current)
            cached = self.snapshot(current)
            if (
                cached
                and (not force or cached.checked >= started)
                and time.time() - cached.checked < _DAILY
            ):
                return cached
            checked_at = datetime.now(UTC).isoformat()
            before = object()
            if self._on_changed is not None and self._projection is not None:
                try:
                    before = self._projection(current.id)
                except Exception:
                    _LOG.exception("Could not inspect the previous model catalog projection")
            try:
                rows = validate_rows(await self._fetch_models(current))
            except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
                status = "failed"
                reason = (
                    "network" if isinstance(exc, (httpx.HTTPError, OSError)) else "invalid_response"
                )
                rows = cached.rows if cached else ()
                last_success_at = cached.last_success_at if cached else None
            else:
                status, reason, last_success_at = "ok", None, checked_at

            channels = {c.id: c for c in self._read_config().channels}
            latest = channels.get(current.id)
            if (
                latest is None
                or latest.disabled
                or not latest.models.live
                or self._fingerprint(latest) != fingerprint
            ):
                self.request_refresh()
                return None

            snapshot = DirectorySnapshot(
                fingerprint, time.time(), rows, status, checked_at, last_success_at, reason
            )
            self.store.put(current.id, snapshot, set(channels))
            if status == "ok" and self._reconcile is not None:
                self._reconcile(latest, tuple(row["id"] for row in rows))
            if status == "ok" and self._on_changed is not None:
                try:
                    changed = (
                        self._projection(current.id) != before
                        if self._projection
                        else cached is None or cached.rows != rows
                    )
                    if changed:
                        await self._on_changed()
                except Exception:
                    _LOG.exception("Could not project the refreshed model catalog")
            return snapshot

    async def refresh_due(self) -> None:
        await asyncio.gather(*(self.refresh(channel) for channel in self._read_config().channels))

    async def run(self) -> None:
        """Run until cancelled by the application; each connection has a daily TTL."""
        while True:
            self._wake.clear()
            try:
                await self.refresh_due()
            except OSError, ValueError, RuntimeError:
                _LOG.exception("Model directory refresh failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=60)
