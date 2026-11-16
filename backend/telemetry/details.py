"""Bounded timeline and content-free failure projections of the usage ledger."""

import hashlib
import json
import math
import re

from .ledger import now_ms

_CLOSE_REASONS = {"terminal", "client_cancel", "non_stream", "body_stall", "body_overflow"}
_TERMINALS = {"completed", "failed", "incomplete"}
_POOL_MAIN = {"openai-main", "chatgpt-main", "openai-multi-main"}
_POOL_SUFFIX = re.compile(r"-(p[a-f0-9]{6})$")


class TimelineQueryError(ValueError):
    pass


def _provider_label(provider):
    if provider in _POOL_MAIN:
        return "openai"
    provider = _POOL_SUFFIX.sub("", provider)
    return "openai" if provider in {"chatgpt", "openai-multi"} else provider


def _failed(row):
    status, terminal, close = row["status"], row.get("terminal_status"), row.get("close_reason")
    if status == 499 or close == "client_cancel":
        return False
    if terminal == "failed":
        return True
    if terminal in {"completed", "incomplete"} or close in {"body_stall", "body_overflow"}:
        return False
    return status != 101 and not 200 <= status < 400


def _failure_evidence(row):
    status = row["status"]
    code = row.get("error_code")
    sends, settled = row.get("send_count"), row.get("settled_sends")
    close = row.get("close_reason")
    if sends == 0 and code == "invalid_request":
        return "local-refusal", None, "synthetic"
    if code in {"upstream_failure", "upstream_timeout", "upstream_interrupted"} and (
        sends is not None and settled is not None and sends > settled
    ):
        return "transport-ambiguous", "pre_headers", "synthetic"
    if code == "upstream_interrupted" and settled and close in {"body_stall", "body_overflow"}:
        return "transport-ambiguous", "mid_stream", "synthetic"
    if not settled or close not in {"non_stream", "terminal"}:
        return None
    if close == "terminal" and code != "upstream_error":
        # A locally synthesized error at EOF is not an upstream failure response.
        return None
    if code == "upstream_error" and status == 502 and close == "terminal":
        # Lite maps arbitrary upstream error events to logical 502. The status
        # alone cannot distinguish a provider fault from a rejected payload.
        return None
    cause = {
        401: "credential-rejected",
        402: "quota-exhausted",
        403: "credential-rejected",
        413: "payload-too-large",
        429: "rate-limit",
        451: "policy-refusal",
        503: "upstream-declined",
    }.get(status)
    if not cause and status >= 500 and close == "non_stream":
        cause = "upstream-fault"
    if not cause and 400 <= status < 500 and code == "upstream_error":
        cause = "payload-rejected"
    if cause:
        return cause, "terminal_sse" if close == "terminal" else None, "upstream"
    return None


def failure_projection(store):
    groups = {}
    unattributed = invalid_timestamp = 0
    for row in store.rows():
        if not _failed(row):
            continue
        evidence = _failure_evidence(row)
        if evidence is None:
            unattributed += 1
            continue
        stamp = row["timestamp"]
        if type(stamp) is not int:
            invalid_timestamp += 1
            continue
        cause, phase, source = evidence
        status = row["status"]
        provider = _provider_label(row.get("provider", ""))
        protocol, terminal, close = (
            row.get(key) for key in ("inbound_protocol", "terminal_status", "close_reason")
        )
        facts = {
            "cause": cause,
            "statusClass": f"{status // 100}xx" if 100 <= status <= 599 else "unknown",
            "providerClass": provider or None,
            "inboundProtocol": protocol if protocol in {"responses", "chat", "messages"} else None,
            "terminalStatus": terminal if terminal in _TERMINALS else None,
            "closeReason": close if close in _CLOSE_REASONS else None,
            "transportPhase": phase,
            "terminalSource": source,
        }
        raw = json.dumps((1, *facts.values()), separators=(",", ":"), ensure_ascii=False)
        fingerprint = "v1:" + hashlib.sha256(raw.encode()).hexdigest()
        if fingerprint not in groups:
            groups[fingerprint] = {
                **facts,
                "fingerprint": fingerprint,
                "firstSeen": stamp,
                "lastSeen": stamp,
                "count": 1,
            }
        else:
            group = groups[fingerprint]
            group["firstSeen"] = min(stamp, group["firstSeen"])
            group["lastSeen"] = max(stamp, group["lastSeen"])
            group["count"] += 1
    return {
        "fingerprintVersion": 1,
        "historyIncomplete": bool(store.retention_history()[0]),
        "unattributedFailures": unattributed,
        "invalidTimestampFailures": invalid_timestamp,
        "failures": sorted(
            groups.values(), key=lambda group: (-group["lastSeen"], group["fingerprint"])
        ),
    }


def _query_int(raw):
    try:
        raw = raw.strip()
        if re.fullmatch(r"0[xX][\da-fA-F]+|0[bB][01]+|0[oO][0-7]+", raw):
            return int(raw, 0)
        if not re.fullmatch(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", raw):
            return None
        value = float(raw)
        return int(value) if math.isfinite(value) and value.is_integer() else None
    except OverflowError, ValueError:
        return None


def _timeline_query(params):
    def first(key, default=""):
        values = params.getlist(key)
        return values[0] if values else default

    hours = _query_int(first("hours", "24"))
    if hours not in (6, 24, 72, 168):
        raise TimelineQueryError("hours must be one of 6, 24, 72, 168")
    minutes = _query_int(first("bucketMinutes", "60"))
    if minutes is None or not 1 <= minutes <= 1440:
        raise TimelineQueryError("bucketMinutes must be an integer from 1 through 1440")
    buckets = (hours * 60 + minutes - 1) // minutes
    if buckets > 2000:
        raise TimelineQueryError("timeline bucket count must not exceed 2000")
    values = []
    for name, allowed in (
        ("metric", ("total", "input", "output", "cached")),
        ("aggregation", ("sum", "average", "max")),
        ("grouping", ("model", "modelAccount")),
    ):
        value = first(name) or allowed[0]
        if value not in allowed:
            raise TimelineQueryError(f"invalid value for parameter: {value}")
        values.append(value)
    models = first("models").strip()
    if models:
        models = [model.strip() for model in models.split(",")]
        if len(models) > 100:
            raise TimelineQueryError("models must contain at most 100 identifiers")
        if any(not re.fullmatch(r"[^/\s]+/\S+", model) for model in models):
            raise TimelineQueryError("models must contain provider/model identifiers")
    hidden = params.getlist("hiddenProvider")
    if len(hidden) > 100 or any(not provider or re.search(r"\s", provider) for provider in hidden):
        raise TimelineQueryError("hiddenProvider must contain at most 100 nonblank provider names")
    return (
        minutes * 60,
        buckets,
        *values,
        sorted(set(models)) if models else None,
        sorted(set(hidden)),
    )


def _combine(left, right):
    return left[0] + right[0], max(left[1], right[1]), left[2] + right[2]


def timeline(store, params, max_bytes):
    seconds, buckets, metric, aggregation, grouping, models, hidden = _timeline_query(params)
    end = (now_ms() // (1000 * seconds) + 1) * seconds
    start = end - buckets * seconds
    series = {}
    available = set()
    selected = (
        None
        if models is None
        else {
            f"{_provider_label(model.split('/')[0])}/{model.split('/', 1)[1]}" for model in models
        }
    )
    missing = 0
    token_key = {
        "total": "totalTokens",
        "input": "inputTokens",
        "output": "outputTokens",
        "cached": "cacheReadInputTokens",
    }[metric]

    def add(row):
        nonlocal missing
        raw_provider = row.get("provider", "unknown")
        provider = _provider_label(raw_provider)
        if raw_provider in hidden or provider in hidden:
            return
        model = row.get("model", "unknown")
        model_id = f"{provider}/{model}"
        available.add(model_id)
        if selected is not None and model_id not in selected:
            return
        account = row.get("account_label")
        if not account and raw_provider in _POOL_MAIN:
            account = "main"
        if not account and (suffix := _POOL_SUFFIX.search(raw_provider)):
            account = suffix[1]
        account = account or "unknown"
        identity = model_id if grouping == "model" else f"{model_id} · {account}"
        if identity not in series:
            series[identity] = (
                {
                    "id": identity,
                    "provider": provider,
                    "model": model,
                    **({"accountLogLabel": account} if grouping == "modelAccount" else {}),
                },
                {},
            )
        usage = row.get("usage") or {}
        value = usage.get(token_key)
        if value is None:
            missing += 1
            return
        bucket = (row["timestamp"] - start * 1000) // (seconds * 1000)
        points = series[identity][1]
        points[bucket] = _combine(points.get(bucket, (0, 0, 0)), (value, value, 1))

    def add_request(row):
        if attempts := row.get("fallback_attempts"):
            for attempt in attempts:
                add({**row, **attempt})
        else:
            add(row)

    read_truncated = store.timeline_rows(start * 1000, end * 1000, max_bytes, add_request)

    def value(point):
        total, maximum, count = point
        return (
            maximum
            if aggregation == "max"
            else total / count
            if aggregation == "average"
            else total
        )

    ranked = sorted(
        series.values(),
        key=lambda item: (
            -sum(map(value, item[1].values())),
            item[0]["id"].casefold(),
            item[0]["id"],
        ),
    )
    if len(ranked) > 24:
        other = {}
        for _, points in ranked[23:]:
            for bucket, point in points.items():
                other[bucket] = _combine(other.get(bucket, (0, 0, 0)), point)
        ranked = [*ranked[:23], ({"id": "other", "provider": "", "model": "other"}, other)]
    result = []
    for row, points in ranked:
        dense = [0] * buckets
        for bucket, point in points.items():
            dense[bucket] = value(point)
        result.append({**row, "total": sum(dense), "points": dense})
    return {
        "appliedFilters": {"models": models, "hiddenProviders": hidden},
        "start": start,
        "end": end,
        "bucketSeconds": seconds,
        "buckets": buckets,
        "metric": metric,
        "aggregation": aggregation,
        "grouping": grouping,
        "series": result,
        "availableModels": sorted(available),
        "missingMeasurements": missing,
        "truncated": read_truncated or bool(store.retention_history()[0]),
    }
