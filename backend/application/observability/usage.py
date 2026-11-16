"""Usage windows, source filters and aggregation over the ledger."""

import os
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from backend.configuration.models import Configuration
from backend.telemetry.ledger import now_ms

from ..errors import ApplicationError
from .values import attempt_contribution, cost_value

APPS = (
    "all",
    "codex",
    "dsh",
    "claude_code",
    "claude_desktop",
    "grok",
    "opencode",
    "aside",
    "other",
    "unknown",
)
MAX_EPOCH = 8640000000000000


def _timezone():
    if (tz := os.getenv("TZ")) and ("/" in tz or tz == "UTC"):
        return tz
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\CurrentControlSet\\Control\\TimeZoneInformation"
        ) as key:
            windows_name = winreg.QueryValueEx(key, "TimeZoneKeyName")[0].rstrip("\x00")
        known = {
            "China Standard Time": "Asia/Shanghai",
            "Tokyo Standard Time": "Asia/Tokyo",
            "Korea Standard Time": "Asia/Seoul",
            "Eastern Standard Time": "America/New_York",
            "Central Standard Time": "America/Chicago",
            "Mountain Standard Time": "America/Denver",
            "Pacific Standard Time": "America/Los_Angeles",
            "GMT Standard Time": "Europe/London",
            "W. Europe Standard Time": "Europe/Berlin",
            "India Standard Time": "Asia/Kolkata",
        }
        if windows_name in known:
            return known[windows_name]
    except ImportError, OSError:
        pass
    offset = datetime.now().astimezone().utcoffset()
    seconds = int(offset.total_seconds()) if offset else 0
    return f"Etc/GMT{-seconds // 3600:+}" if seconds and seconds % 3600 == 0 else "UTC"


def _app(row):
    client = row.get("request_client")
    if client in {"codex_cli", "codex_desktop", "codex_ide"}:
        return "codex"
    return client or "unknown"


def parse_source(params):
    if "client" not in params and "device" not in params:
        return None
    client, device, key = (params.get(name) for name in ("client", "device", "apiKeyId"))
    if not (
        len(params.getlist("client")) == len(params.getlist("device")) == 1
        and all(len(params.getlist(name)) <= 1 for name in ("surface", "apiKeyId"))
        and (params.get("surface", "all") == "all")
        and ("failures" not in params)
        and (client in APPS)
        and (
            device == "key"
            and key
            and (key == key.strip())
            or (device in {"all", "no-key", "unconfigured"} and key is None)
        )
    ):
        raise ApplicationError(400, "invalid usage source filter")
    return {
        "client": client,
        "device": {"kind": device, **({"id": key} if device == "key" else {})},
    }


def _device(row, selected, keys):
    kind = selected["kind"]
    key = row.get("api_key_id")
    if kind == "all":
        return True
    if kind == "no-key":
        return key is None
    if kind == "key":
        return key == selected["id"]
    return bool(key and row.get("admission_kind") == "configured" and (key not in keys))


def _matches_source(row, source, keys):
    return source is None or (
        source["client"] in {"all", _app(row)} and _device(row, source["device"], keys)
    )


def _midnight(day):
    return int(datetime.combine(day, time.min).timestamp() * 1000)


def _calendar_date(timestamp):
    if timestamp > 253402214400000:
        return date.max
    try:
        return datetime.fromtimestamp(timestamp / 1000).date()
    except ValueError, OverflowError, OSError:
        return date(1970, 1, 1)


def _bound(raw, name):
    try:
        if re.fullmatch("[0-9]{1,16}", raw):
            value = int(raw)
        elif re.fullmatch(
            "\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d{1,3})?(?:Z|[+-]\\d{2}:\\d{2})", raw
        ):
            value = round(datetime.fromisoformat(raw).timestamp() * 1000)
        else:
            raise ValueError
        if not 0 <= value <= MAX_EPOCH:
            raise ValueError
        return value
    except ValueError, OverflowError:
        raise ApplicationError(
            400,
            f"{name} must be nonnegative integer epoch milliseconds or a valid full ISO datetime with timezone",
        ) from None


def _window(params, now):
    range_name = params.get("range", "30d")
    if range_name == "1d":
        range_name = "today"
    if range_name not in {"today", "7d", "30d", "all"}:
        range_name = "30d"
    surface = params.get("surface", "all")
    if surface not in {"codex", "claude", "grok"}:
        surface = "all"
    raw_since, raw_until = (params.get("since"), params.get("until"))
    custom = raw_since is not None or raw_until is not None
    if custom:
        if raw_since is None or raw_until is None:
            raise ApplicationError(400, "since and until must be supplied together")
        since, until = (_bound(raw_since, "since"), _bound(raw_until, "until"))
        if since > until:
            raise ApplicationError(400, "since must be less than or equal to until")
    else:
        until = now
        length = {"today": 1, "7d": 7, "30d": 30}.get(range_name)
        since = _midnight(_calendar_date(now) - timedelta(days=length - 1)) if length else None
    return (range_name, surface, since, until, custom)


def source_facets(store, source, keys):
    until = now_ms()
    since = _midnight(_calendar_date(until) - timedelta(days=29))
    selected = source["device"]
    if selected["kind"] == "key" and selected["id"] not in keys:
        raise ApplicationError(400, "unknown key")
    apps = dict.fromkeys(APPS, 0)
    selectable = sorted(key for key in keys if key == key.strip())
    devices = [
        {"kind": "all"},
        {"kind": "no-key"},
        *({"kind": "key", "id": key} for key in selectable),
        {"kind": "unconfigured"},
    ]
    counts = dict.fromkeys(((item["kind"], item.get("id")) for item in devices), 0)
    for row in store.rows(since=since, until=until):
        client = _app(row)
        if _device(row, selected, keys):
            apps["all"] += 1
            apps[client] += 1
        if source["client"] not in {"all", client}:
            continue
        counts["all", None] += 1
        key = row.get("api_key_id")
        if key is None:
            counts["no-key", None] += 1
        elif ("key", key) in counts:
            counts["key", key] += 1
        elif _device(row, {"kind": "unconfigured"}, keys):
            counts["unconfigured", None] += 1
    _, _, newest_dropped = store.retention_history()
    return {
        "sourceFilter": source,
        "window": {"since": since, "until": until, "timeZone": _timezone()},
        "apps": apps,
        "devices": [
            {"device": item, "requests": counts[item["kind"], item.get("id")]} for item in devices
        ],
        "legacyUnclassifiedRequests": 0,
        "unavailableConfiguredKeys": len(keys) - len(selectable),
        "usageIncomplete": newest_dropped is not None and newest_dropped >= since,
    }


def _blank():
    return dict.fromkeys(
        (
            "requests",
            "measuredRequests",
            "reportedRequests",
            "unreportedRequests",
            "unsupportedRequests",
            "estimatedRequests",
            "inputTokens",
            "outputTokens",
            "totalTokens",
            "cachedInputTokens",
            "cacheReadInputTokens",
            "cacheCreationInputTokens",
            "reasoningOutputTokens",
            "coverageRatio",
            "pricedRequests",
            "unpricedRequests",
            "unmeteredRequests",
            "attemptCount",
            "sends",
            "settledSends",
            "unresolvedSends",
            "spendRequests",
            "cacheObservedInputTokens",
            "cacheObservedRequests",
            "cacheSynthesizedRequests",
            "cacheUnknownRequests",
            "throughputSamples",
            "throughputOutputTokens",
            "throughputDurationMs",
        ),
        0,
    )


def _spend(row):
    sends = row.get("send_count")
    if sends is None:
        return None
    settled = row.get("settled_sends")
    if settled is None:
        settled = int(
            sends > 0
            and row.get("terminal_status") != "cancelled"
            and (row.get("close_reason") not in {"body_stall", "body_overflow", "body_interrupted"})
        )
    settled = min(settled, sends)
    return {"sends": sends, "settled": settled, "unresolved": sends - settled}


def _bump(totals, row, amount):
    totals["requests"] += 1
    totals["attemptCount"] += len(row.get("fallback_attempts") or [row])
    if spend := _spend(row):
        totals["sends"] += spend["sends"]
        totals["settledSends"] += spend["settled"]
        totals["unresolvedSends"] += spend["unresolved"]
        totals["spendRequests"] += 1
    status = row["usage_status"]
    totals[f"{status}Requests"] += 1
    usage = row.get("usage")
    if row.get("cache_provenance") == "synthesized":
        totals["cacheSynthesizedRequests"] += 1
    elif (
        usage
        and row.get("cache_provenance") != "unknown"
        and ("cacheReadInputTokens" in usage or "cacheCreationInputTokens" in usage)
    ):
        totals["cacheObservedInputTokens"] += usage["inputTokens"]
        totals["cacheObservedRequests"] += 1
    else:
        totals["cacheUnknownRequests"] += 1
    if status not in {"reported", "estimated"} or not usage:
        totals["unmeteredRequests"] += 1
        return
    totals["measuredRequests"] += 1
    for token_key in (
        "inputTokens",
        "outputTokens",
        "totalTokens",
        "cachedInputTokens",
        "cacheReadInputTokens",
        "cacheCreationInputTokens",
        "reasoningOutputTokens",
    ):
        totals[token_key] += usage.get(token_key, 0)
    if usage["outputTokens"] and row["duration_ms"] > 0:
        totals["throughputSamples"] += 1
        totals["throughputOutputTokens"] += usage["outputTokens"]
        totals["throughputDurationMs"] += row["duration_ms"]
    if amount is not None:
        totals["pricedRequests"] += 1
        totals["estimatedCostUsd"] = totals.get("estimatedCostUsd", 0) + amount
    else:
        totals["unpricedRequests"] += 1


def _finish(totals, total_tokens):
    totals["shareRatio"] = totals["totalTokens"] / total_tokens if total_tokens else 0
    totals["coverageRatio"] = (
        totals["measuredRequests"] / totals["requests"] if totals["requests"] else 0
    )
    totals["priceCoverageRatio"] = (
        totals["pricedRequests"] / totals["requests"] if totals["requests"] else 0
    )
    observed = totals["cacheObservedInputTokens"]
    totals["cacheHitRate"] = min(1, totals["cacheReadInputTokens"] / observed) if observed else None
    if duration := totals["throughputDurationMs"]:
        totals["throughputTokensPerSec"] = 1000 * totals["throughputOutputTokens"] / duration
    return totals


def usage_summary(store, providers, params, source, keys, official=None):
    now = now_ms()
    range_name, surface, since, until, custom = _window(params, now)
    filter_provider, filter_model, filter_key = (
        params.get(name, "").strip() or None for name in ("provider", "model", "apiKeyId")
    )
    totals = _blank()
    models = defaultdict(_blank)
    provider_totals = defaultdict(_blank)
    days = defaultdict(_blank)
    day_models = defaultdict(lambda: defaultdict(_blank))
    apps = defaultdict(_blank)
    accounts = defaultdict(_blank)
    oldest = None
    snapshot_start, snapshot_end = store.snapshot_bounds() if source is None else (None, None)
    resolved = {}
    final_day = _calendar_date(until)
    chart_floor = final_day - timedelta(days=365)
    for row in store.rows(
        since=since if source is None or custom else None,
        until=until if source is None or custom else None,
    ):
        if row.get("fallback_attempts") and (filter_provider or filter_model):
            matching = [
                attempt
                for attempt in row["fallback_attempts"]
                if (
                    not filter_provider
                    or attempt.get("provider", "").casefold() == filter_provider.casefold()
                )
                and (
                    not filter_model
                    or filter_model.casefold()
                    in {
                        attempt.get("model", "").casefold(),
                        f"{attempt.get('provider')}/{attempt.get('model')}".casefold(),
                    }
                )
            ]
            if not matching:
                continue
            row = attempt_contribution(row, matching)
        provider, model = (row.get("provider", "unknown"), row.get("model", "unknown"))
        row_surface = row.get("surface", "other")
        if not _matches_source(row, source, keys) or (
            surface != "all"
            and (
                not (
                    surface == row_surface
                    or (surface == "claude" and row_surface == "claude-desktop")
                )
            )
        ):
            continue
        if (
            filter_provider
            and provider.casefold() != filter_provider.casefold()
            or (
                filter_model
                and filter_model.casefold()
                not in {model.casefold(), f"{provider}/{model}".casefold()}
            )
            or (filter_key and row.get("api_key_id") != filter_key)
        ):
            continue
        stamp = row["timestamp"]
        if source is not None:
            snapshot_start = stamp if snapshot_start is None else snapshot_start
            snapshot_end = stamp
        if stamp > until or (since is not None and stamp < since):
            continue
        oldest = stamp if oldest is None else oldest
        day = _calendar_date(stamp)
        identity = (provider, model)
        if (
            upstream := (row.get("served_model") or row.get("upstream_model"))
        ) and upstream != model:
            resolved[identity] = upstream
        cost = cost_value(row, providers, official)
        amount = cost["estimate"]["cost"]["total"] if cost["kind"] == "value" else None
        groups = [totals, apps[_app(row)]]
        if day >= chart_floor:
            groups.append(days[day])
        for group in groups:
            _bump(group, row, amount)
        attempts = row.get("fallback_attempts")
        if attempts:
            by_model = defaultdict(list)
            by_provider = defaultdict(list)
            for attempt in attempts:
                by_model[
                    attempt.get("provider", "unknown"), attempt.get("model", "unknown")
                ].append(attempt)
                by_provider[attempt.get("provider", "unknown")].append(attempt)
            for target, subset in by_model.items():
                contribution = attempt_contribution(row, subset)
                cost = cost_value(contribution, providers, official)
                amount = cost["estimate"]["cost"]["total"] if cost["kind"] == "value" else None
                _bump(models[target], contribution, amount)
                if day >= chart_floor:
                    _bump(day_models[day][target], contribution, amount)
            for target, subset in by_provider.items():
                contribution = attempt_contribution(row, subset)
                cost = cost_value(contribution, providers, official)
                amount = cost["estimate"]["cost"]["total"] if cost["kind"] == "value" else None
                _bump(provider_totals[target], contribution, amount)
                if label := contribution.get("account_label"):
                    _bump(accounts[target, label], contribution, amount)
        else:
            groups = [models[identity], provider_totals[provider]]
            if day >= chart_floor:
                groups.append(day_models[day][identity])
            if label := row.get("account_label"):
                groups.append(accounts[provider, label])
            for group in groups:
                _bump(group, row, amount)
    token_total = totals["totalTokens"]

    def model_rows(groups):
        return [
            {
                "provider": provider,
                "model": model,
                **_finish(data, token_total),
                **(
                    {"resolvedModel": resolved[provider, model]}
                    if (provider, model) in resolved
                    else {}
                ),
            }
            for (provider, model), data in sorted(
                groups.items(), key=lambda item: -item[1]["requests"]
            )
        ]

    if since is not None:
        first_day = max(_calendar_date(since), chart_floor)
    elif oldest is not None:
        first_day = max(
            final_day - timedelta(days=min(365, (until - oldest + 86399999) // 86400000)),
            chart_floor,
        )
    else:
        first_day = final_day
    entries_dropped, bytes_dropped, _ = store.retention_history()
    result = {
        "range": range_name,
        "surface": surface,
        "since": since,
        "generatedAt": now,
        "historyTruncated": entries_dropped > 0,
        "truncatedPrefixBytes": bytes_dropped,
        "entriesTruncated": entries_dropped > 0,
        "entriesDropped": entries_dropped,
        "legacyUnclassifiedRequests": 0,
        "snapshotWindowStart": snapshot_start,
        "snapshotWindowEnd": snapshot_end,
        "summary": _finish(totals, token_total),
        "days": [
            {
                "date": (day := (first_day + timedelta(days=i))).isoformat(),
                **_finish(days[day], token_total),
                "models": model_rows(day_models[day]),
            }
            for i in range((final_day - first_day).days + 1)
        ],
        "models": model_rows(models),
        "providers": [
            {"provider": provider, **_finish(data, token_total)}
            for provider, data in sorted(
                provider_totals.items(), key=lambda item: -item[1]["requests"]
            )
        ],
        "apps": [
            {
                "client": app,
                **_finish(data, token_total),
                "unpricedRequests": data["unpricedRequests"] + data["unmeteredRequests"],
            }
            for app, data in sorted(apps.items(), key=lambda item: -item[1]["requests"])
        ],
        "accounts": [
            {"provider": provider, "accountLogLabel": account, **_finish(data, token_total)}
            for (provider, account), data in accounts.items()
        ],
    }
    if custom:
        result.update(customWindow=True, until=until)
    if source is not None:
        result["sourceFilter"] = source
    if filter_provider or filter_model or filter_key:
        result["filter"] = {
            "provider": filter_provider,
            "model": filter_model,
            "apiKeyId": filter_key,
            "matched": bool(totals["requests"]),
            "attemptOverlap": False,
        }
    return result


def configured_keys(config: Configuration):
    keys = config.features.usage.key_ids
    if (
        len(keys) > 1024
        or any(not key or len(key) > 256 for key in keys)
        or sum(map(len, keys)) > 65536
    ):
        raise ApplicationError(503, "usage_inventory_unavailable")
    return set(keys)
