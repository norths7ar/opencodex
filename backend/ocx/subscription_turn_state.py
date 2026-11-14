"""Local handles for ChatGPT turn-state tokens returned over the HTTP Responses wire."""

import re
import secrets
import time
from collections.abc import Callable, Mapping
from typing import NamedTuple

TURN_STATE_HEADER = "x-codex-turn-state"
_HANDLE_PREFIX = "ocx-ts-"
_MAX_HANDLES = 4096
_TTL_SECONDS = 6 * 60 * 60
_OPAQUE_TOKEN = re.compile(r"[A-Za-z0-9._~+/=:-]{1,8192}")
_SCOPE_HEADERS = {"session_id", "session-id", "thread-id"}

type _Binding = tuple[str, str, str, str]


class _Entry(NamedTuple):
    raw: str
    binding: _Binding
    expires: float


def _session_scope(incoming_headers: Mapping[str, str]) -> tuple[str, str]:
    scope = {
        name.lower(): value.strip()
        for name, value in incoming_headers.items()
        if name.lower() in _SCOPE_HEADERS
    }
    session = scope.get("session_id") or scope.get("session-id", "")
    return session, scope.get("thread-id", "")


class TurnStateStore:
    """Process-local indices for Lite's single loopback data-plane principal.

    Client session/thread headers narrow that scope but are not authentication. When
    neither is sent, handles still bind to provider/account; there is no reliable
    conversation key to track account changes across full-history requests.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._handles: dict[tuple[_Binding, str], str] = {}
        self._owners: dict[tuple[str, str, str], tuple[str, float]] = {}

    def _remove(self, handle: str) -> None:
        entry = self._entries.pop(handle)
        del self._handles[(entry.binding, entry.raw)]

    def issue(
        self,
        raw: str,
        issuer: str,
        provider: str,
        model: str,
        incoming_headers: Mapping[str, str],
    ) -> str | None:
        if not isinstance(raw, str) or not _OPAQUE_TOKEN.fullmatch(raw):
            return None
        now = self._clock()
        while self._entries:
            oldest = next(iter(self._entries))
            if self._entries[oldest].expires > now:
                break
            self._remove(oldest)
        # As in the original, changing the model does not change turn-state ownership.
        binding = (issuer, provider, *_session_scope(incoming_headers))
        existing = self._handles.get((binding, raw))
        if existing is not None:
            return existing
        if len(self._entries) >= _MAX_HANDLES:
            self._remove(next(iter(self._entries)))
        handle = _HANDLE_PREFIX + secrets.token_hex(16)
        while handle in self._entries or handle == raw:
            handle = _HANDLE_PREFIX + secrets.token_hex(16)
        self._entries[handle] = _Entry(raw, binding, now + _TTL_SECONDS)
        self._handles[(binding, raw)] = handle
        return handle

    def resolve(
        self,
        handle: str,
        issuer: str,
        provider: str,
        model: str,
        incoming_headers: Mapping[str, str],
    ) -> str | None:
        if not isinstance(handle, str) or not handle:
            return None
        if not handle.startswith(_HANDLE_PREFIX):
            return handle
        entry = self._entries.get(handle)
        if entry is None:
            return None
        if entry.expires <= self._clock():
            self._remove(handle)
            return None
        if entry.binding != (issuer, provider, *_session_scope(incoming_headers)):
            return None
        return entry.raw

    def issuer_changed(
        self, issuer: str, provider: str, incoming_headers: Mapping[str, str]
    ) -> bool:
        """Check ownership before dispatch without committing an unserved turn."""
        session, thread = _session_scope(incoming_headers)
        if not (session or thread):
            return False
        previous = self._owners.get((provider, session, thread))
        return previous is not None and previous[1] > self._clock() and previous[0] != issuer

    def observe_issuer(
        self, issuer: str, provider: str, incoming_headers: Mapping[str, str]
    ) -> bool:
        """Commit the account that successfully served this conversation."""
        session, thread = _session_scope(incoming_headers)
        if not (session or thread):
            return False
        now = self._clock()
        key = (provider, session, thread)
        previous = self._owners.pop(key, None)
        if len(self._owners) >= _MAX_HANDLES:
            del self._owners[next(iter(self._owners))]
        self._owners[key] = issuer, now + _TTL_SECONDS
        return previous is not None and previous[1] > now and previous[0] != issuer
