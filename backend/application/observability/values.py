"""Usage, price and timing values shared by log and summary projections."""

from backend.telemetry.pricing import estimate_cost


def cost_value(row, providers, official=None):
    if attempts := row.get("fallback_attempts"):
        costs = [
            cost_value({"timestamp": row.get("timestamp"), **attempt}, providers, official)
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
        return unavailable(reasons)
    return {"kind": "value", "estimate": estimate, "estimateReasons": reasons}


def unavailable(reason):
    return {"kind": "unavailable", "reason": reason}


def attempt_sends(attempt):
    return attempt.get("send_count", int(attempt.get("sent_upstream") is True))


def attempt_contribution(row, attempts):
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
        "send_count": sum(attempt_sends(attempt) for attempt in attempts),
        "settled_sends": sum(
            attempt.get("settled_sends", int(attempt.get("sent_upstream") is True))
            for attempt in attempts
        ),
        "duration_ms": sum(attempt.get("duration_ms", 0) for attempt in attempts),
    }


def token_rate(row, *, decode=False):
    usage = row.get("usage")
    if not usage:
        return unavailable(
            "usage_unsupported" if row.get("usage_status") == "unsupported" else "usage_missing"
        )
    if not usage["outputTokens"]:
        return unavailable("output_missing")
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
            return unavailable("ttft_missing")
    if duration <= 0:
        return unavailable("invalid_duration")
    if decode and duration < 1000:
        return unavailable("decode_window_too_short")
    return {
        "kind": "value",
        "value": usage["outputTokens"] * 1000 / duration,
        "estimated": decode or row.get("usage_status") == "estimated",
        **timing,
    }


def protocol_trace(row):
    inbound, wire = (row.get("inbound_protocol"), row.get("upstream_wire"))
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
