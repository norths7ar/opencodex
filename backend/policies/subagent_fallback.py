"""Quota-aware child-request fallback, without the retired account pool."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

from backend.auth.quota import Observation
from backend.catalog.identity import disabled_model_ids


def thread_spawn(headers):
    if headers.get("x-openai-subagent") == "collab_spawn":
        return True
    try:
        metadata = json.loads(headers.get("x-codex-turn-metadata", "{}"))
        return isinstance(metadata, dict) and metadata.get("subagent_kind") == "thread_spawn"
    except ValueError:
        return False


class SubagentFallback:
    def __init__(self, config_loader, route, fallback_models):
        self.config_loader = config_loader
        self.route = route
        self.fallback_models = fallback_models
        self.quota_reader: Callable[[], Awaitable[Observation | None]] | None = None
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
        provider, model = self.route(selector)
        return (
            provider.id,
            model.casefold(),
            self.account if provider.auth_mode == "oauth" else None,
        )

    def note_failure(self, headers, provider, model, status):
        if not thread_spawn(headers) or status != 429:
            return
        now = time.monotonic()
        interval = self.config_loader().features.agents.subagent_model_fallback_poll_ms / 1000
        key = (
            provider.id,
            model.casefold(),
            self.account if provider.auth_mode == "oauth" else None,
        )
        self.health = {key: until for key, until in self.health.items() if until > now}
        self.health[key] = now + interval

    def _unavailable(self, selector, config):
        try:
            provider, model = self.route(selector)
            key = self._key(selector)
        except ValueError:
            return True
        if model in disabled_model_ids(config.models, provider, (model,)):
            return True
        if self.health.get(key, 0) > time.monotonic():
            return True
        if provider.auth_mode == "oauth" and provider.service_id == "chatgpt" and self.quota:
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

    async def select(self, body, headers, *, lookup_model=None, normalize=None):
        if not thread_spawn(headers):
            return None
        primary = body.get("model")
        if not isinstance(primary, str) or not primary:
            return None
        config = self.config_loader()
        lookup = lookup_model if isinstance(lookup_model, str) and lookup_model else primary
        specific = config.features.agents.subagent_model_fallback_by_model.get(
            lookup, config.features.agents.subagent_model_fallback_by_model.get(primary, [])
        )
        global_chain = config.features.agents.subagent_model_fallback
        client_chain = self.fallback_models(lookup)
        if not specific and not global_chain and not client_chain:
            return None
        chain = []
        seen = set()
        for model in [primary, *specific, *global_chain, *client_chain]:
            if normalize is not None:
                try:
                    model = normalize(model)
                except ValueError:
                    continue
            if not isinstance(model, str) or not model.strip() or model.strip().casefold() in seen:
                continue
            seen.add(model.strip().casefold())
            chain.append(model.strip())
        for candidate in chain:
            try:
                if self.route(candidate)[0].auth_mode == "oauth":
                    await self._refresh(
                        config.features.agents.subagent_model_fallback_poll_ms / 1000
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
