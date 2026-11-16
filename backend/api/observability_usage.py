"""Console projections over the gateway's request ledger."""

import base64
import copy
import hashlib
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from backend.configuration.models import Configuration
from backend.configuration.store import ConfigurationStore
from backend.telemetry.details import TimelineQueryError, failure_projection, timeline
from backend.telemetry.ledger import UsageStore, conversation_digest, now_ms
from backend.telemetry.pricing import estimate_cost

APPS = (
    "all",
    "codex",
    "claude_code",
    "claude_desktop",
    "grok",
    "opencode",
    "aside",
    "other",
    "unknown",
)
MAX_EPOCH = 8_640_000_000_000_000


def _cost(row, providers, official=None):
    if attempts := row.get("fallback_attempts"):
        costs = [
            _cost({"timestamp": row.get("timestamp"), **attempt}, providers, official)
            for attempt in attempts
        ]
        if missing := next((cost for cost in costs if cost["kind"] != "value"), None):
            return missing
        estimates = [cost["estimate"] for cost in costs]
        amount = {
            key: sum(estimate["cost"].get(key, 0) for estimate in estimates)
            for key in ("input", "output", "cacheRead", "cacheWrite", "total")
        }
        reasons = list(
            dict.fromkeys(reason for cost in costs for reason in cost.get("estimateReasons", []))
        )
        return {
            "kind": "value",
            "estimate": {
                "cost": amount,
                "estimated": any(estimate.get("estimated") for estimate in estimates),
                "attempts": [
                    {"ordinal": index + 1, "price": estimate["price"]}
                    for index, estimate in enumerate(estimates)
                ],
            },
            "estimateReasons": reasons,
        }
    estimate, reasons = estimate_cost(row, providers.get(row.get("provider")), official)
    if estimate is None:
        return _unavailable(reasons)
    return {"kind": "value", "estimate": estimate, "estimateReasons": reasons}


def _unavailable(reason):
    return {"kind": "unavailable", "reason": reason}


def _attempt_sends(attempt):
    # New attempt rows describe one physical send; historical grouped attempts may contain many.
    return attempt.get("send_count", int(attempt.get("sent_upstream") is True))


def _attempt_contribution(row, attempts):
    """One logical request's measured contribution to a provider/model grouping."""
    measured = [attempt["usage"] for attempt in attempts if attempt.get("usage")]
    usage = (
        {
            key: sum(item.get(key, 0) for item in measured)
            for key in {key for item in measured for key in item}
            if key != "estimated"
        }
        if measured
        else None
    )
    return {
        **row,
        **attempts[-1],
        "fallback_attempts": attempts,
        "usage": usage,
        "usage_status": "reported" if usage else "unreported",
        "send_count": sum(_attempt_sends(attempt) for attempt in attempts),
        "settled_sends": sum(
            attempt.get("settled_sends", int(attempt.get("sent_upstream") is True))
            for attempt in attempts
        ),
        "duration_ms": sum(attempt.get("duration_ms", 0) for attempt in attempts),
    }


def _rate(row, *, decode=False):
    usage = row.get("usage")
    if not usage:
        return _unavailable(
            "usage_unsupported" if row.get("usage_status") == "unsupported" else "usage_missing"
        )
    if not usage["outputTokens"]:
        return _unavailable("output_missing")
    duration = row.get("duration_ms") or 0
    timing = {}
    if decode:
        if "gen_start_ms" in row and "last_output_ms" in row:
            duration = row["last_output_ms"] - row["gen_start_ms"]
            timing["timingBasis"] = "generation-window"
        elif "first_token_ms" in row:
            duration -= row["first_token_ms"]
            timing["timingBasis"] = "legacy-post-visible-output"
        else:
            return _unavailable("ttft_missing")
    if duration <= 0:
        return _unavailable("invalid_duration")
    if decode and duration < 1000:
        return _unavailable("decode_window_too_short")
    return {
        "kind": "value",
        "value": usage["outputTokens"] * 1000 / duration,
        "estimated": decode or row.get("usage_status") == "estimated",
        **timing,
    }


def _protocol_trace(row):
    inbound, wire = row.get("inbound_protocol"), row.get("upstream_wire")
    if not wire and (attempts := row.get("fallback_attempts")):
        wire = attempts[-1].get("upstream_wire")
    wires = {"chat", "messages", "responses"}
    if not row.get("send_count") or inbound not in wires or wire not in wires:
        return None
    native = inbound == wire
    return {
        "v": 1,
        "inbound": inbound,
        "upstream": wire,
        "mode": "native" if native else "translated",
        "requestPath": [inbound, wire],
        "responsePath": [wire, inbound],
        "reasonCodes": ["same-wire-native" if native else "cross-wire-codec"],
        "contractVersion": "2026-09-25.2",
    }


def _log_dto(row, providers, official=None):
    fields = {
        "timestamp": "timestamp",
        "request_client": "client",
        "surface": "surface",
        "inbound_protocol": "inboundProtocol",
        "api_key_id": "apiKeyId",
        "admission_kind": "admissionKind",
        "account_label": "accountLogLabel",
        "conversation_id": "conversationId",
        "upstream_model": "resolvedModel",
        "served_model": "servedModel",
        "requested_model": "requestedModel",
        "first_token_ms": "firstOutputMs",
        "requested_service_tier": "requestedServiceTier",
        "response_service_tier": "responseServiceTier",
        "gen_start_ms": "genStartMs",
        "last_output_ms": "lastOutputMs",
        "terminal_status": "terminalStatus",
        "close_reason": "closeReason",
        "error_code": "errorCode",
        "fallback_refusal": "fallbackRefusal",
    }
    result = {wire: row[key] for key, wire in fields.items() if row.get(key) is not None}
    result.update(
        requestId=str(row["id"]),
        provider=row.get("provider", "unknown"),
        model=row.get("model", "unknown"),
        status=row.get("status", 102),
        durationMs=row.get("duration_ms", 0),
        usageStatus=row.get("usage_status", "unreported"),
        displayMetrics={
            "tokPerSecond": _rate(row),
            "decodeTokPerSecond": _rate(row, decode=True),
            "cost": _cost(row, providers, official),
        },
    )
    if usage := row.get("usage"):
        result.update(usage=usage, totalTokens=usage["totalTokens"])
    if spend := _spend(row):
        result["spend"] = spend
    if trace := _protocol_trace(row):
        result["protocolTrace"] = trace
    if attempts := row.get("fallback_attempts"):
        result["attempts"] = [
            {
                "ordinal": index + 1,
                "provider": attempt.get("provider", "unknown"),
                "model": attempt.get("model", "unknown"),
                "adapter": attempt.get("adapter") or attempt.get("upstream_wire") or "unknown",
                "status": attempt["status"],
                "durationMs": attempt["duration_ms"],
                "sendCount": _attempt_sends(attempt),
                "recoveryKinds": [],
                "usageStatus": attempt["usage_status"],
                **(
                    {"usage": attempt["usage"], "totalTokens": attempt["usage"]["totalTokens"]}
                    if attempt.get("usage")
                    else {}
                ),
                "displayMetrics": {
                    "cost": _cost(
                        {"timestamp": row.get("timestamp"), **attempt}, providers, official
                    ),
                    "tokPerSecond": _rate(attempt),
                    "decodeTokPerSecond": _rate(attempt, decode=True),
                },
            }
            for index, attempt in enumerate(attempts)
        ]
    return result


def _cursor_encode(data):
    return (
        base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def _cursor_decode(raw):
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", raw):
            raise ValueError
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != raw:
            raise ValueError
        value = json.loads(decoded)
        if not isinstance(value, dict) or type(value.get("v")) is not int:
            raise ValueError
        if (
            value["v"] == 1
            and set(value) == {"v", "t", "id"}
            and (
                type(value["t"]) in {int, float}
                and 0 <= value["t"] <= MAX_EPOCH
                and isinstance(value["id"], str)
                and 1 <= len(value["id"]) <= 256
            )
        ):
            return value
        if (
            value["v"] != 2
            or set(value) != {"v", "e", "q", "h", "n"}
            or not (
                type(value["n"]) is int
                and 0 <= value["n"] <= 2000
                and all(
                    isinstance(value[key], str) and re.fullmatch(pattern, value[key])
                    for key, pattern in (
                        ("e", r"[0-9a-f]{32}"),
                        ("q", r"[0-9a-f]{64}"),
                        ("h", r"[0-9a-f]{64}"),
                    )
                )
            )
        ):
            raise ValueError
        return value
    except ValueError, RecursionError:
        raise HTTPException(
            400, {"error": {"code": "invalid_cursor", "message": "invalid cursor"}}
        ) from None


def _log_matches(row, params):
    def get(key):
        return params.get(key, "").strip()

    provider, model, account, mode, status = (
        get(key) for key in ("provider", "model", "account", "protocolMode", "status")
    )
    conversation = get("conversationId") or get("conversation")
    if conversation:
        candidates = [conversation, conversation_digest(conversation)]
        if match := re.fullmatch(
            r"codex://threads/([^/\s?#]+)/?(?:[?#].*)?", conversation, re.IGNORECASE
        ):
            candidates.extend([match[1], conversation_digest(match[1])])
        if not row.get("conversation_id") or row["conversation_id"] not in candidates:
            return False
    if status:
        status = status.lower()
        code = row.get("status", 102)
        if not (
            code // 100 == int(status[0])
            if re.fullmatch(r"[1-5]xx", status)
            else str(code) == status
        ):
            return False
    return (
        any(
            (not provider or provider == target.get("provider"))
            and (
                not model
                or model in {target.get("model"), f"{target.get('provider')}/{target.get('model')}"}
            )
            for target in row.get("fallback_attempts") or [row]
        )
        and (not account or account == row.get("account_label"))
        and (not mode or mode.lower() == ((_protocol_trace(row) or {}).get("mode", "none")))
    )


def _positive_int(raw):
    match = re.match(r"[+-]?\d{1,12}", raw.strip())
    return max(0, int(match[0])) if match else 0


def _logs(store, providers, params, epoch, official=None):
    cursor = _cursor_decode(params["cursor"]) if "cursor" in params else None
    rows = [row for row in store.rows(logs=True) if _log_matches(row, params)]
    if tail := _positive_int(params.get("tail", "")):
        rows = rows[-tail:]
    total = len(rows)
    if limit := _positive_int(params.get("limit", "")):
        end = max(0, total - _positive_int(params.get("offset", "")))
        rows = rows[max(0, end - min(limit, 2000)) : end]
    dtos = [_log_dto(row, providers, official) for row in rows]
    query = urlencode(sorted((k, v) for k, v in params.multi_items() if k != "cursor"))
    query_hash = hashlib.sha256(query.encode()).hexdigest()
    candidate = cursor and cursor["v"] == 2 and cursor["e"] == epoch and cursor["q"] == query_hash
    serialized = [(json.dumps(dto, separators=(",", ":")) + "\n").encode() for dto in dtos]
    unchanged = bool(
        candidate
        and cursor["n"] <= len(rows)
        and hashlib.sha256(b"".join(serialized[: cursor["n"]])).hexdigest() == cursor["h"]
    )
    token = _cursor_encode(
        {
            "v": 2,
            "e": epoch,
            "q": query_hash,
            "n": len(rows),
            "h": hashlib.sha256(b"".join(serialized)).hexdigest(),
        }
    )
    return {
        "timeZone": _timezone(),
        "generatedAt": now_ms(),
        "total": total,
        "logs": dtos[cursor["n"] :] if unchanged else dtos,
        "cursor": token,
        "reset": cursor is not None and not unchanged,
    }


def _timezone():
    # Browser consumers expect an Intl-compatible identifier, not the Windows
    # localized display string. Day buckets below use host civil dates directly.
    if (tz := os.getenv("TZ")) and ("/" in tz or tz == "UTC"):
        return tz
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation"
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


def _source(params):
    if "client" not in params and "device" not in params:
        return None
    client, device, key = (params.get(name) for name in ("client", "device", "apiKeyId"))
    if not (
        len(params.getlist("client")) == len(params.getlist("device")) == 1
        and all(len(params.getlist(name)) <= 1 for name in ("surface", "apiKeyId"))
        and params.get("surface", "all") == "all"
        and "failures" not in params
        and client in APPS
        and (
            (device == "key" and key and key == key.strip())
            or (device in {"all", "no-key", "unconfigured"} and key is None)
        )
    ):
        raise HTTPException(400, "invalid usage source filter")
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
    return bool(key and row.get("admission_kind") == "configured" and key not in keys)


def _matches_source(row, source, keys):
    return source is None or (
        source["client"] in {"all", _app(row)} and _device(row, source["device"], keys)
    )


def _midnight(day):
    return int(datetime.combine(day, time.min).timestamp() * 1000)


def _calendar_date(timestamp):
    if timestamp > 253_402_214_400_000:
        return date.max
    try:
        return datetime.fromtimestamp(timestamp / 1000).date()
    except ValueError, OverflowError, OSError:
        # Windows CRT rejects early positive UTC times when local offset is negative.
        return date(1970, 1, 1)


def _bound(raw, name):
    try:
        if re.fullmatch(r"[0-9]{1,16}", raw):
            value = int(raw)
        elif re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})", raw
        ):
            value = round(datetime.fromisoformat(raw).timestamp() * 1000)
        else:
            raise ValueError
        if not 0 <= value <= MAX_EPOCH:
            raise ValueError
        return value
    except ValueError, OverflowError:
        raise HTTPException(
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
    raw_since, raw_until = params.get("since"), params.get("until")
    custom = raw_since is not None or raw_until is not None
    if custom:
        if raw_since is None or raw_until is None:
            raise HTTPException(400, "since and until must be supplied together")
        since, until = _bound(raw_since, "since"), _bound(raw_until, "until")
        if since > until:
            raise HTTPException(400, "since must be less than or equal to until")
    else:
        until = now
        length = {"today": 1, "7d": 7, "30d": 30}.get(range_name)
        since = _midnight(_calendar_date(now) - timedelta(days=length - 1)) if length else None
    return range_name, surface, since, until, custom


def _facets(store, source, keys):
    until = now_ms()
    since = _midnight(_calendar_date(until) - timedelta(days=29))
    selected = source["device"]
    if selected["kind"] == "key" and selected["id"] not in keys:
        raise HTTPException(400, "unknown key")
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
        counts[("all", None)] += 1
        key = row.get("api_key_id")
        if key is None:
            counts[("no-key", None)] += 1
        elif ("key", key) in counts:
            counts[("key", key)] += 1
        elif _device(row, {"kind": "unconfigured"}, keys):
            counts[("unconfigured", None)] += 1
    _, _, newest_dropped = store.retention_history()
    return {
        "sourceFilter": source,
        "window": {"since": since, "until": until, "timeZone": _timezone()},
        "apps": apps,
        "devices": [
            {"device": item, "requests": counts[(item["kind"], item.get("id"))]} for item in devices
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
            and row.get("close_reason") not in {"body_stall", "body_overflow"}
        )
    settled = min(settled, sends)
    return {"sends": sends, "settled": settled, "unresolved": sends - settled}


def _bump(totals, row, amount):
    totals["requests"] += 1
    # A refresh/retry is another upstream send on the same logical attempt.
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


def _summary(store, providers, params, source, keys, official=None):
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
            row = _attempt_contribution(row, matching)
        provider, model = row.get("provider", "unknown"), row.get("model", "unknown")
        row_surface = row.get("surface", "other")
        if not _matches_source(row, source, keys) or (
            surface != "all"
            and not (
                surface == row_surface or surface == "claude" and row_surface == "claude-desktop"
            )
        ):
            continue
        if (
            filter_provider
            and provider.casefold() != filter_provider.casefold()
            or filter_model
            and filter_model.casefold() not in {model.casefold(), f"{provider}/{model}".casefold()}
            or filter_key
            and row.get("api_key_id") != filter_key
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
        identity = provider, model
        if (upstream := row.get("served_model") or row.get("upstream_model")) and upstream != model:
            resolved[identity] = upstream
        cost = _cost(row, providers, official)
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
                contribution = _attempt_contribution(row, subset)
                cost = _cost(contribution, providers, official)
                amount = cost["estimate"]["cost"]["total"] if cost["kind"] == "value" else None
                _bump(models[target], contribution, amount)
                if day >= chart_floor:
                    _bump(day_models[day][target], contribution, amount)
            for target, subset in by_provider.items():
                contribution = _attempt_contribution(row, subset)
                cost = _cost(contribution, providers, official)
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

    # All-time totals may extend beyond the activity chart's 366-day display window.
    if since is not None:
        first_day = max(_calendar_date(since), chart_floor)
    elif oldest is not None:
        first_day = max(
            final_day - timedelta(days=min(365, (until - oldest + 86_399_999) // 86_400_000)),
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
                "date": (day := first_day + timedelta(days=i)).isoformat(),
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


def _configured_keys(config: Configuration):
    # Lite does not infer public device IDs from credentials.
    keys = config.features.usage.key_ids
    if (
        len(keys) > 1024
        or any(not key or len(key) > 256 for key in keys)
        or sum(map(len, keys)) > 65536
    ):
        raise HTTPException(503, "usage_inventory_unavailable")
    return set(keys)


def usage_router(store: UsageStore, config_store: ConfigurationStore, official_prices=None):
    router = APIRouter(prefix="/api")
    epoch = uuid4().hex

    def prices():
        # Only cached metadata belongs on the read path.
        return copy.deepcopy(official_prices()) if official_prices else None

    @router.get("/logs")
    async def logs(request: Request):
        providers = {
            provider.id: provider for provider in config_store.read().configuration.channels
        }
        try:
            return await run_in_threadpool(
                _logs,
                store,
                providers,
                request.query_params,
                epoch,
                prices(),
            )
        except sqlite3.Error:
            raise HTTPException(500, "read_failed") from None

    @router.get("/usage/source-facets")
    def source_facets(request: Request):
        params = request.query_params
        source = _source(params)
        if source is None or set(params) - {"client", "device", "apiKeyId"}:
            raise HTTPException(400, "invalid usage source filter")
        try:
            return JSONResponse(
                _facets(store, source, _configured_keys(config_store.read().configuration)),
                headers={"Cache-Control": "no-store"},
            )
        except sqlite3.Error:
            return JSONResponse(
                {"error": "usage_facets_unavailable"},
                503,
                headers={"Cache-Control": "no-store", "Retry-After": "1"},
            )

    @router.get("/usage")
    async def usage(request: Request):
        params = request.query_params
        source = _source(params)
        try:
            if params.get("failures") == "1":
                return JSONResponse(
                    await run_in_threadpool(failure_projection, store),
                    headers={"Cache-Control": "no-store"},
                )
            config = config_store.read().configuration
            result = await run_in_threadpool(
                _summary,
                store,
                {provider.id: provider for provider in config.channels},
                params,
                source,
                _configured_keys(config)
                if source and source["device"]["kind"] == "unconfigured"
                else set(),
                prices(),
            )
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except sqlite3.Error:
            raise HTTPException(500, "read_failed") from None

    @router.get("/usage/timeline")
    def usage_timeline(request: Request):
        maximum = config_store.read().configuration.features.usage.management_max_read_bytes
        if (
            type(maximum) not in {int, float}
            or not 0 < maximum < (1 << 53)
            or maximum != int(maximum)
        ):
            maximum = 64 * 1024 * 1024
        try:
            return JSONResponse(
                timeline(store, request.query_params, int(maximum)),
                headers={"Cache-Control": "no-store"},
            )
        except TimelineQueryError as error:
            raise HTTPException(400, str(error)) from None
        except sqlite3.Error, json.JSONDecodeError:
            raise HTTPException(500, "read_failed") from None

    return router
