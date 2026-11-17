"""Stable request-log pagination and row projection."""

import base64
import hashlib
import json
import re
from urllib.parse import urlencode

from backend.telemetry.details import failure_log_fields
from backend.telemetry.ledger import conversation_digest, now_ms

from ..errors import ApplicationError
from .usage import _spend, _timezone
from .values import attempt_sends, cost_value, protocol_trace, token_rate

MAX_EPOCH = 8640000000000000


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
    result.update(failure_log_fields(row))
    result.update(
        requestId=str(row["id"]),
        provider=row.get("provider", "unknown"),
        model=row.get("model", "unknown"),
        status=row.get("status", 102),
        durationMs=row.get("duration_ms", 0),
        usageStatus=row.get("usage_status", "unreported"),
        displayMetrics={
            "tokPerSecond": token_rate(row),
            "decodeTokPerSecond": token_rate(row, decode=True),
            "cost": cost_value(row, providers, official),
        },
    )
    if usage := row.get("usage"):
        result.update(usage=usage, totalTokens=usage["totalTokens"])
    if spend := _spend(row):
        result["spend"] = spend
    if trace := protocol_trace(row):
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
                "sendCount": attempt_sends(attempt),
                "recoveryKinds": [],
                "usageStatus": attempt["usage_status"],
                **(
                    {"usage": attempt["usage"], "totalTokens": attempt["usage"]["totalTokens"]}
                    if attempt.get("usage")
                    else {}
                ),
                "displayMetrics": {
                    "cost": cost_value(
                        {"timestamp": row.get("timestamp"), **attempt}, providers, official
                    ),
                    "tokPerSecond": token_rate(attempt),
                    "decodeTokPerSecond": token_rate(attempt, decode=True),
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
        if not re.fullmatch("[A-Za-z0-9_-]{1,512}", raw):
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
                and (1 <= len(value["id"]) <= 256)
            )
        ):
            return value
        if (
            value["v"] != 2
            or set(value) != {"v", "e", "q", "h", "n"}
            or (
                not (
                    type(value["n"]) is int
                    and 0 <= value["n"] <= 2000
                    and all(
                        (
                            isinstance(value[key], str) and re.fullmatch(pattern, value[key])
                            for key, pattern in (
                                ("e", "[0-9a-f]{32}"),
                                ("q", "[0-9a-f]{64}"),
                                ("h", "[0-9a-f]{64}"),
                            )
                        )
                    )
                )
            )
        ):
            raise ValueError
        return value
    except ValueError, RecursionError:
        raise ApplicationError(400, "Invalid cursor.", code="invalid_cursor") from None


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
            "codex://threads/([^/\\s?#]+)/?(?:[?#].*)?", conversation, re.IGNORECASE
        ):
            candidates.extend([match[1], conversation_digest(match[1])])
        if not row.get("conversation_id") or row["conversation_id"] not in candidates:
            return False
    if status:
        status = status.lower()
        code = row.get("status", 102)
        if not (
            code // 100 == int(status[0])
            if re.fullmatch("[1-5]xx", status)
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
        and (not mode or mode.lower() == (protocol_trace(row) or {}).get("mode", "none"))
    )


def _positive_int(raw):
    match = re.match("[+-]?\\d{1,12}", raw.strip())
    return max(0, int(match[0])) if match else 0


def log_page(store, providers, params, epoch, official=None):
    cursor = _cursor_decode(params["cursor"]) if "cursor" in params else None
    rows = [row for row in store.rows(logs=True) if _log_matches(row, params)]
    if tail := _positive_int(params.get("tail", "")):
        rows = rows[-tail:]
    total = len(rows)
    if limit := _positive_int(params.get("limit", "")):
        end = max(0, total - _positive_int(params.get("offset", "")))
        rows = rows[max(0, end - min(limit, 2000)) : end]
    dtos = [_log_dto(row, providers, official) for row in rows]
    query = urlencode(sorted(((k, v) for k, v in params.multi_items() if k != "cursor")))
    query_hash = hashlib.sha256(query.encode()).hexdigest()
    candidate = (
        cursor and cursor["v"] == 2 and (cursor["e"] == epoch) and (cursor["q"] == query_hash)
    )
    serialized = [(json.dumps(dto, separators=(",", ":")) + "\n").encode() for dto in dtos]
    unchanged = bool(
        cursor is not None
        and candidate
        and cursor["n"] <= len(rows)
        and (hashlib.sha256(b"".join(serialized[: cursor["n"]])).hexdigest() == cursor["h"])
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
        "logs": dtos[cursor["n"] :] if cursor is not None and unchanged else dtos,
        "cursor": token,
        "reset": cursor is not None and (not unchanged),
    }
