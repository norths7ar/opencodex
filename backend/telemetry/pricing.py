"""Display-only estimates from official DeepSeek prices and the bundled provider catalog.

usage_pricing_catalog.json is a packaged projection of the original generated
model metadata and expected-prices exports, not a live price feed. Only exact
provider/model matches are eligible; an arbitrary compatible endpoint does not
inherit another vendor's prices because it reused the model's name.
"""

import json
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import urlsplit

RATE_KEYS = ("input", "output", "cacheRead", "cacheWrite")
type Rates = tuple[int | float, ...]


class _PriceProvenance(TypedDict):
    source: str


def _index(rows, *, verified_first=False):
    index = {}
    for row in rows:
        identity = row["provider"], row["modelId"]
        if identity not in index or (verified_first and row["status"] == "verified"):
            index[identity] = row
    return index


_CATALOG = json.loads(
    Path(__file__).with_name("usage_pricing_catalog.json").read_text(encoding="utf-8")
)
_EXPECTED = _index(_CATALOG["expected"], verified_first=True)
_CORRECTIONS = _index(_CATALOG["corrections"])
_CONTEXT = _index(_CATALOG["context"])
_PRIORITY = _index(_CATALOG["priority"])
# These canonical direct API vendors have bundled data but are not aliases in
# resolveMetadataProvider; the original looks them up after expected prices.
_VENDOR_FALLBACK = {"openai-apikey", "cerebras", "mistral", "zai"}


def _destination(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.port not in (None, 443):
        return None
    return parsed.hostname, parsed.path.rstrip("/")


_DESTINATIONS = {
    family: {_destination(url) for url in urls} for family, urls in _CATALOG["destinations"].items()
}
_DESTINATIONS["deepseek"].update({("api.deepseek.com", "/v1"), ("api.deepseek.com", "/anthropic")})
_DESTINATIONS["anthropic-apikey"].add(("api.anthropic.com", "/v1"))


def pricing_family(provider):
    if not provider or provider.auth_mode != "key":
        return None
    try:
        destination = _destination(provider.endpoints.base_url)
    except ValueError:
        return None
    if destination in _DESTINATIONS.get(provider.service_id, ()):
        return provider.service_id
    families = [family for family, endpoints in _DESTINATIONS.items() if destination in endpoints]
    return families[0] if len(families) == 1 else None


def _rates(value) -> Rates | None:
    if not isinstance(value, dict):
        return None
    values = tuple(value.get(key) for key in RATE_KEYS)
    if all(
        (type(item) is int or type(item) is float) and 0 <= item <= 1_000_000 for item in values
    ):
        # Each tuple element was checked above; all() does not narrow the tuple type.
        return cast(Rates, values)
    return None


def _user_rates(overlay):
    rates = _rates(overlay)
    if rates is None or "promptLengthPricing" not in overlay:
        return rates, None
    policy = overlay["promptLengthPricing"]
    if not isinstance(policy, dict):
        return None, None
    if policy.get("mode") in ("automatic", "flat") and set(policy) == {"mode"}:
        return rates, policy
    if (
        policy.get("mode") == "custom"
        and set(policy) == {"mode", "thresholdTokens", "comparison", "rates"}
        and type(policy["thresholdTokens"]) is int
        and 0 < policy["thresholdTokens"] < (1 << 53)
        and policy["comparison"] in ("gt", "gte")
        and _rates(policy["rates"]) is not None
        and set(policy["rates"]) == set(RATE_KEYS)
    ):
        return rates, policy
    return None, None


def _expected_price(row):
    if row and (rates := _rates(row["cost4"])) and any(rates):
        return rates, {
            "source": "expected",
            "sourceRef": row["source"],
            "status": row["status"],
            "verifiedAt": row["verifiedAt"],
        }
    return None


@lru_cache(maxsize=2048)
def _bundled_price(family, model):
    identity = family, model
    if correction := _expected_price(_CORRECTIONS.get(identity)):
        return correction
    expected = _expected_price(_EXPECTED.get(identity))
    if family in _VENDOR_FALLBACK and expected:
        return expected
    vendor = _CATALOG["vendors"].get(family)
    if vendor:
        bundled = _CATALOG["bundled"].get(vendor, {})
        if (rates := _rates(bundled.get(model))) and any(rates):
            return rates, {"source": "jawcode", "jawcodeProvider": vendor, "status": "verified"}
    return expected


def _tier_multiplier(rates, row, family, model, policy):
    def priority(value):
        return isinstance(value, str) and value.strip().casefold() in {"priority", "fast"}

    tier_response = row.get("response_service_tier")
    tier_request = row.get("requested_service_tier") or row.get("service_tier")
    confirmed = priority(tier_response)
    applied = priority(tier_response if tier_response is not None else tier_request)
    catalog = _CONTEXT.get((family, model))
    relation = catalog.get("confirmedPriorityRelation") if catalog else None
    tokens = row["usage"]["inputTokens"]
    mode = policy.get("mode", "automatic") if policy else "automatic"
    extras = {}
    if mode == "custom" and not (confirmed and relation == "exclusive"):
        threshold = policy["thresholdTokens"]
        if tokens > threshold or (tokens == threshold and policy["comparison"] == "gte"):
            # Custom policies reach this helper only after _user_rates validates their rates.
            rates = cast(Rates, _rates(policy["rates"]))
            extras["customThresholdApplied"] = True
    elif mode == "automatic" and catalog and not (confirmed and relation == "exclusive"):
        threshold = catalog["thresholdInputTokens"]
        if tokens > threshold or (tokens == threshold and catalog["inclusive"]):
            rates = tuple(
                rate * catalog["multiplier"][key]
                for key, rate in zip(RATE_KEYS, rates, strict=True)
            )
            extras["contextTier"] = "long"
    lower_bound = confirmed and relation == "lower-bound" and bool(extras)
    if family == "openrouter" and applied:
        lower_bound = True
    rule = _PRIORITY.get((family, model))
    if (
        applied
        and rule
        and not lower_bound
        and (confirmed or not rule.get("requiresResponseConfirmation"))
        and ("contextTier" not in extras or relation == "stack")
    ):
        multiplier = rule["multiplier"]
        rates = tuple(rate * multiplier for rate in rates)
        extras["priorityMultiplier"] = multiplier
    if lower_bound:
        extras["priorityLowerBound"] = True
    return rates, extras


# State Council notice, republished by Beijing government. Weekend make-up work
# stays off-peak because DeepSeek explicitly prices every weekend as off-peak.
_HOLIDAY_SOURCE = "https://www.beijing.gov.cn/fuwu/bmfw/sy/jrts/202511/t20251104_4258838.html"
_HOLIDAYS = {
    2026: (
        ("01-01", "01-03"),
        ("02-15", "02-23"),
        ("04-04", "04-06"),
        ("05-01", "05-05"),
        ("06-19", "06-21"),
        ("09-25", "09-27"),
        ("10-01", "10-07"),
    )
}
_DEEPSEEK_ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}


def _official_price(row, models, official):
    for model in models:
        identity = _DEEPSEEK_ALIASES.get(model, model)
        metadata = official.get(identity, {})
        pricing = metadata.get("pricing", {})
        bands = pricing.get("bands")
        if not bands:
            continue
        timestamp = row.get("price_timestamp", row.get("timestamp"))
        try:
            at = datetime.fromtimestamp(timestamp / 1000, UTC)
        except TypeError, ValueError, OverflowError, OSError:
            return None, "price_time_unknown"
        windows = pricing.get("peakMinutesUTC")
        if not windows:
            return None, "price_time_unknown"
        peak = at.weekday() < 5 and any(
            start <= at.hour * 60 + at.minute < end for start, end in windows
        )
        if peak:
            holidays = _HOLIDAYS.get(at.year)
            if holidays is None:
                return None, "price_time_unknown"
            day = at.strftime("%m-%d")
            peak = not any(start <= day <= end for start, end in holidays)
        band = "peak" if peak else "offPeak"
        rates = bands.get(band, {})
        # DeepSeek reports cache hits, not a separate cache-write tariff.
        if row["usage"].get("cacheCreationInputTokens", 0):
            return None, "price_unmatched"
        values = _rates({**rates, "cacheWrite": 0})
        if values is None:
            return None, "price_unmatched"
        source = metadata.get("provenance", {})
        reasons = ["current_official_price"]
        if source.get("stale"):
            reasons.append("stale_official_price")
        if "price_timestamp" not in row:
            reasons.append("request_time_estimated")
        return (
            values,
            {
                "source": "official",
                "sourceRef": pricing.get("sourceRef"),
                "verifiedAt": pricing.get("verifiedAt"),
                "status": "verified",
                "timeBand": band,
                "timestamp": timestamp,
                "holidaySource": _HOLIDAY_SOURCE,
                "stale": bool(source.get("stale")),
            },
            identity,
            reasons,
        ), None
    return None, "price_unmatched"


def estimate_cost(row, provider, official=None):
    """Return (estimate, reasons), or (None, the metric-unavailable reason)."""
    usage = row.get("usage")
    status = row.get("usage_status")
    if status == "unsupported":
        return None, "usage_unsupported"
    if not usage or status not in {"reported", "estimated"}:
        return None, "usage_missing"
    read = usage.get("cacheReadInputTokens", 0)
    write = usage.get("cacheCreationInputTokens", 0)
    if read + write > usage["inputTokens"]:
        return None, "invalid_cache_breakdown"
    auth_mode = row.get("auth_mode") or getattr(provider, "auth_mode", None)
    if auth_mode == "oauth":
        return None, "price_unmatched"

    family = row.get("price_family") or pricing_family(provider)
    # The configured selector can have its own price (for example, a virtual
    # service-tier variant). If it has none, try the observed upstream identity.
    models = tuple(
        dict.fromkeys(
            filter(None, (row.get("model"), row.get("served_model"), row.get("upstream_model")))
        )
    )
    overlays = provider.models.metadata.costs if provider else None
    rates = policy = provenance = model = None
    if isinstance(overlays, dict):
        for model in models:
            rates, policy = _user_rates(overlays.get(model))
            if rates is not None:
                provenance = {
                    "source": "user",
                    "sourceRef": "provider modelCosts",
                    "status": "verified",
                    "verifiedAt": "user-configured",
                }
                break
    official_reasons = []
    official_models = set(official or {}) | {"deepseek-flash", "deepseek-v4-pro"}
    if (
        rates is None
        and family == "deepseek"
        and any(
            _DEEPSEEK_ALIASES.get(candidate, candidate) in official_models for candidate in models
        )
    ):
        matched, unavailable = _official_price(row, models, official or {})
        if not matched:
            return None, unavailable
        rates, provenance, model, official_reasons = matched
    if rates is None:
        for model in models:
            if matched := _bundled_price(family, model):
                rates, provenance = matched
                break
    if rates is None:
        return None, "price_unmatched"

    # Every successful user, official or bundled match assigns rates and provenance together.
    provenance = cast(_PriceProvenance, provenance)
    rates, extras = _tier_multiplier(rates, row, family, model, policy)
    reason_by_source = {"user": "provider_cost_overlay", "expected": "expected_price_overlay"}
    reasons = list(official_reasons)
    if reason := reason_by_source.get(provenance["source"]):
        reasons.append(reason)
    if status == "estimated" or usage.get("estimated"):
        reasons.append("usage_estimated")
    if row.get("cache_provenance") in {"synthesized", "unknown"} or not (
        "cacheReadInputTokens" in usage or "cacheCreationInputTokens" in usage
    ):
        reasons.append("cache_detail_missing")
    if extras.get("priorityLowerBound"):
        reasons.append("priority_lower_bound")
    quantities = (usage["inputTokens"] - read - write, usage["outputTokens"], read, write)
    amount = {
        key: rate * count / 1_000_000
        for key, rate, count in zip(RATE_KEYS, rates, quantities, strict=True)
    }
    # Cost4 carries the 5-minute Anthropic write rate. The observed 1-hour
    # subset is billed at 2x rather than 1.25x input, after tier multipliers.
    write_1h = min(write, usage.get("cacheCreation1hInputTokens", 0))
    amount["cacheWrite"] += rates[3] * (2 / 1.25 - 1) * write_1h / 1_000_000
    amount["total"] = sum(amount.values())
    return {
        "cost": amount,
        "estimated": bool(reasons),
        "price": {"provider": row.get("provider", ""), "modelId": model, **provenance},
        **extras,
    }, reasons
