"""Read-only management queries over configuration and the request ledger."""

import copy
import json
import sqlite3
from uuid import uuid4

from backend.telemetry.details import TimelineQueryError, failure_projection, timeline

from .errors import ApplicationError
from .observability.logs import log_page
from .observability.usage import configured_keys, parse_source, source_facets, usage_summary


class ObservabilityQueries:
    def __init__(self, store, configuration, *, official_prices=None):
        self.store, self.configuration = store, configuration
        self.official_prices = official_prices
        self.epoch = uuid4().hex

    def prices(self):
        return copy.deepcopy(self.official_prices()) if self.official_prices else None

    def logs(self, params):
        channels = {c.id: c for c in self.configuration.read().configuration.channels}
        try:
            return log_page(self.store, channels, params, self.epoch, self.prices())
        except sqlite3.Error:
            raise ApplicationError(
                500, "Could not read the request log.", code="read_failed"
            ) from None

    def facets(self, params):
        source = parse_source(params)
        if source is None or set(params) - {"client", "device", "apiKeyId"}:
            raise ApplicationError(400, "Invalid usage source filter.")
        try:
            return source_facets(
                self.store, source, configured_keys(self.configuration.read().configuration)
            )
        except sqlite3.Error:
            raise ApplicationError(
                503, "Usage source filters are unavailable.", code="usage_facets_unavailable"
            ) from None

    def usage(self, params):
        source = parse_source(params)
        try:
            if params.get("failures") == "1":
                return failure_projection(self.store)
            config = self.configuration.read().configuration
            return usage_summary(
                self.store,
                {c.id: c for c in config.channels},
                params,
                source,
                configured_keys(config)
                if source and source["device"]["kind"] == "unconfigured"
                else set(),
                self.prices(),
            )
        except sqlite3.Error:
            raise ApplicationError(500, "Could not read usage.", code="read_failed") from None

    def timeline(self, params):
        maximum = self.configuration.read().configuration.features.usage.management_max_read_bytes
        if (
            type(maximum) not in {int, float}
            or not 0 < maximum < (1 << 53)
            or maximum != int(maximum)
        ):
            maximum = 64 * 1024 * 1024
        try:
            return timeline(self.store, params, int(maximum))
        except TimelineQueryError as exc:
            raise ApplicationError(400, str(exc)) from None
        except sqlite3.Error, json.JSONDecodeError:
            raise ApplicationError(
                500, "Could not read the request timeline.", code="read_failed"
            ) from None
