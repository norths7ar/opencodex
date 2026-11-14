"""Quota-aware child-request fallback, without the retired account pool."""

import asyncio
import json
import time

from . import clients
from .subagents import setting


def thread_spawn(headers):
    if headers.get("x-openai-subagent") == "collab_spawn":
        return True
    try:
        metadata = json.loads(headers.get("x-codex-turn-metadata", "{}"))
        return isinstance(metadata, dict) and metadata.get("subagent_kind") == "thread_spawn"
    except ValueError:
        return False


def legacy_fallback(home, primary):
    """Read old role model_fallback fields; new settings are kept in gateway config."""
    found = []
    for path in (home / "agents").glob("*.toml"):
        try:
            document = clients._parse(clients._read(path, 1 << 20, "Codex agent role"))
        except clients.ClientConfigError:
            continue
        if document.get("model") != primary:
            continue
        chain = document.get("model_fallback", [])
        if isinstance(chain, list) and all(isinstance(value, str) for value in chain):
            found.extend(chain)
    return found


class SubagentFallback:
    def __init__(self, store, integration):
        self.store = store
        self.integration = integration
        self.quota_reader = None
        self.health = {}
        self.checked_at = 0
        self.quota = None
        self.account = None
        self.lock = asyncio.Lock()

    async def _refresh(self, interval):
        if self.quota_reader is None or time.monotonic() - self.checked_at < interval:
            return
        async with self.lock:
            if time.monotonic() - self.checked_at < interval:
                return
            try:
                state = await self.quota_reader()
            except OSError, ValueError, TimeoutError:
                return
            if state is None:
                self.quota, self.account = None, None
                return
            self.account = getattr(state, "account", None)
            self.quota = getattr(state, "quota", None)
            if not getattr(state, "failure", None):
                self.checked_at = time.monotonic()

    def _key(self, selector):
        provider, model = self.store.route(selector)
        return provider.id, model.casefold(), self.account if provider.authMode == "oauth" else None

    def note_failure(self, headers, model, status):
        if not thread_spawn(headers) or status != 429:
            return
        now = time.monotonic()
        interval = setting(self.store.load(), "subagentModelFallbackPollMs", 60000) / 1000
        try:
            key = self._key(model)
        except ValueError:
            return
        self.health = {key: until for key, until in self.health.items() if until > now}
        self.health[key] = now + interval

    def _unavailable(self, selector, config):
        try:
            provider, model = self.store.route(selector)
            key = self._key(selector)
        except ValueError:
            return True
        disabled = setting(config, "disabledModels", [])
        if selector in disabled or f"{provider.id}/{model}" in disabled:
            return True
        if self.health.get(key, 0) > time.monotonic():
            return True
        if provider.authMode == "oauth" and provider.id == "openai" and self.quota:
            now = time.time()
            updated = self.quota.get("updatedAt")
            if updated is not None and now * 1000 - updated > 30 * 60_000:
                return False
            for window in ("short", "weekly", "monthly"):
                reset = self.quota.get(f"{window}ResetAt")
                if reset is not None and reset > 10_000_000_000:
                    reset /= 1000
                if self.quota.get(f"{window}Percent", 0) >= 100 and (reset is None or reset > now):
                    return True
        return False

    async def select(self, body, headers):
        if not thread_spawn(headers):
            return None
        primary = body.get("model")
        if not isinstance(primary, str) or not primary:
            return None
        config = self.store.load()
        specific = setting(config, "subagentModelFallbackByModel", {}).get(primary, [])
        global_chain = setting(config, "subagentModelFallback", [])
        legacy = legacy_fallback(self.integration.home(), primary)
        if not specific and not global_chain and not legacy:
            return None
        chain = []
        seen = set()
        for model in [primary, *specific, *global_chain, *legacy]:
            if not isinstance(model, str) or not model.strip() or model.strip().casefold() in seen:
                continue
            seen.add(model.strip().casefold())
            chain.append(model.strip())
        for candidate in chain:
            try:
                if self.store.route(candidate)[0].authMode == "oauth":
                    await self._refresh(
                        setting(config, "subagentModelFallbackPollMs", 60000) / 1000
                    )
                    break
            except ValueError:
                continue
        skipped = []
        for candidate in chain:
            if self._unavailable(candidate, config):
                skipped.append(candidate)
                continue
            if candidate != primary:
                body["model"] = candidate
                return {"from": primary, "to": candidate, "skipped": skipped}
            return None
        # Match original: when every candidate is unavailable, let the primary
        # produce its authoritative failure instead of inventing a successful route.
        return None
