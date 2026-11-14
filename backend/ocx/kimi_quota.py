"""Kimi coding quota windows and identity-scoped, bounded-age observations."""

import asyncio
import copy
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from .quota import MAX_REPORT_AGE_MS, ProbeFailure, now_ms, number, read_json, record

BASE_URL = "https://api.kimi.com/coding/v1"
PROVIDER_TTL_MS = 5 * 60_000
ACCOUNT_TTL_MS = 10 * 60_000


def reset_at(value):
    numeric = number(value)
    if numeric is not None:
        milliseconds = numeric if numeric > 10_000_000_000 else numeric * 1000
        return milliseconds if 0 < milliseconds <= 8.64e15 else None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
            milliseconds = parsed.replace(tzinfo=parsed.tzinfo or UTC).timestamp() * 1000
            return milliseconds if milliseconds > 0 else None
        except ValueError, OverflowError, OSError:
            pass
    return None


def first(row, *keys):
    return next((row[key] for key in keys if row.get(key) is not None), None)


def parse_row(value, fallback=None):
    if not isinstance(value, dict):
        return None
    limit, used, remaining = (number(value.get(key)) for key in ("limit", "used", "remaining"))
    percent = None
    if limit is not None and limit > 0:
        if (used is not None and used >= limit) or (remaining is not None and remaining <= 0):
            percent = 100
        elif not ((used is not None and used < 0) or (remaining is not None and remaining > limit)):
            if used is not None:
                percent = used / limit * 100
            elif remaining is not None:
                percent = min((limit - remaining) / limit * 100, 99.999)
    if percent is None:
        ratio = number(first(value, "used_ratio", "usedRatio"))
        if ratio is not None and 0 <= ratio <= 1:
            percent = ratio * 100
    if percent is None:
        percent = number(first(value, "utilization", "percent", "usedPercent", "used_percent"))
    if percent is None:
        return None
    reset = reset_at(first(value, "resetTime", "resetAt", "reset_time", "reset_at"))
    if reset is None and fallback:
        reset = reset_at(first(fallback, "resetTime", "resetAt", "reset_time", "reset_at"))
    return {
        "percent": max(0, min(100, percent)),
        **({"resetAt": reset} if reset is not None else {}),
    }


def parse_usage(payload, *, now=None):
    if not isinstance(payload, dict):
        return None
    nested = record(payload.get("data"))

    def legacy(body):
        return any(body.get(key) is not None for key in ("usage", "limits", "totalQuota"))

    body = nested if not legacy(payload) and legacy(nested) else payload
    weekly, total = parse_row(body.get("usage")), parse_row(body.get("totalQuota"))
    monthly = None
    for container in (payload, nested, record(payload.get("quota")), record(nested.get("quota"))):
        usages = record(container.get("usages"))
        monthly = parse_row(first(usages, "limit_month_total", "monthTotal")) or parse_row(
            first(usages, "limit_month_code", "monthCode")
        )
        if monthly:
            break
    five_hour = None
    for raw in body.get("limits", []) if isinstance(body.get("limits"), list) else []:
        if not isinstance(raw, dict):
            continue
        detail = record(raw.get("detail")) if isinstance(raw.get("detail"), dict) else raw
        window = record(raw.get("window"))
        duration = number(
            next(
                (
                    row["duration"]
                    for row in (window, raw, detail)
                    if row.get("duration") is not None
                ),
                None,
            )
        )
        unit = str(
            next(
                (
                    row["timeUnit"]
                    for row in (window, raw, detail)
                    if row.get("timeUnit") is not None
                ),
                "",
            )
        ).upper()
        label = " ".join(
            value
            for value in (
                raw.get("name"),
                raw.get("title"),
                raw.get("scope"),
                detail.get("name"),
                detail.get("title"),
            )
            if isinstance(value, str)
        ).lower()
        if not five_hour and (
            ("MINUTE" in unit and duration == 300)
            or ("HOUR" in unit and duration == 5)
            or re.search(r"(^|\b)5\s*(?:h|hour)", label)
        ):
            five_hour = parse_row(detail, window)
        if not weekly and (
            ("DAY" in unit and duration == 7)
            or ("HOUR" in unit and duration == 168)
            or re.search(r"weekly|7\s*(?:d|day)", label)
        ):
            weekly = parse_row(detail, window)
    quota = {}
    for name, row in (("fiveHour", five_hour), ("weekly", weekly), ("monthly", monthly)):
        if row:
            quota[name + "Percent"] = row["percent"]
            if "resetAt" in row:
                quota[name + "ResetAt"] = row["resetAt"]
    if total:
        quota["customWindows"] = [{"label": "Total subscription credits", **total}]
    return {**quota, "updatedAt": now_ms() if now is None else now} if quota else None


@dataclass
class KimiObservation:
    account: str
    generation: str
    quota: dict | None = None
    checked_at: int = 0
    failure: str | None = None


class KimiQuota:
    def __init__(self, auth):
        self.auth = auth
        self.state = None
        self.inflight = None

    def _active(self):
        try:
            credential = self.auth.load()
        except ValueError, OSError:
            return None
        if not credential or credential.get("needsReauth"):
            self.state = None
            return None
        if not self.state or (self.state.account, self.state.generation) != (
            credential["accountId"],
            credential["generation"],
        ):
            self.state = KimiObservation(credential["accountId"], credential["generation"])
        return credential

    async def read(self, *, force=False, account=False, base_url=BASE_URL):
        if base_url.rstrip("/").lower() != BASE_URL:
            return None
        saved = self._active()
        if not saved:
            return None
        state = self.state
        ttl = ACCOUNT_TTL_MS if account else PROVIDER_TTL_MS
        if not force and state.checked_at and now_ms() - state.checked_at < ttl:
            return self._projection(state)
        if self.inflight and self.inflight[0] is state:
            return await asyncio.shield(self.inflight[1])
        task = asyncio.create_task(self._probe(state))
        self.inflight = (state, task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self.inflight == (state, task):
                self.inflight = None

    def _projection(self, state):
        value = copy.deepcopy(state)
        if value.quota and now_ms() - value.quota["updatedAt"] >= MAX_REPORT_AGE_MS:
            value.quota = None
        return value

    async def _probe(self, state):
        quota, failure = None, None
        stage = "auth"
        try:
            before = self.auth.load()
            if (
                not before
                or before.get("generation") != state.generation
                or self.state is not state
            ):
                return None
            credential = await self.auth.token(expected_account_id=state.account)
            current = self.auth.load()
            if (
                not current
                or current.get("generation") != credential["generation"]
                or current.get("accountId") != state.account
                or self.state is not state
            ):
                return None
            # A successful token renewal changes credential generation, not the
            # account observed by this probe. Adopt only this exact returned token.
            state.generation = credential["generation"]
            stage = "response"
            async with asyncio.timeout(8):
                async with self.auth.client.stream(
                    "GET",
                    BASE_URL + "/usages",
                    headers={
                        "Accept": "application/json",
                        "Authorization": "Bearer " + credential["access"],
                    },
                    follow_redirects=False,
                ) as response:
                    if response.status_code in {401, 403}:
                        failure = "access_denied"
                    elif not response.is_success:
                        failure = "upstream_error"
                    else:
                        quota = parse_usage(await read_json(response))
                        if quota is None:
                            failure = "response_unusable"
        except ValueError, RuntimeError:
            failure = "access_denied" if stage == "auth" else "response_unusable"
        except TimeoutError:
            failure = "timeout"
        except httpx.HTTPError, OSError:
            failure = "transport_error"
        except ProbeFailure:
            failure = "response_unusable"
        current = self._active()
        if not current or self.state is not state:
            return None
        state.checked_at, state.failure = now_ms(), failure
        if quota:
            state.quota = quota
        return self._projection(state)

    def account_fields(self, account, *, enriched=False):
        fields = {"quotaMode": "probe"}
        saved = self._active()
        if not saved or saved["accountId"] != account or not enriched:
            return fields
        state = self._projection(self.state)
        fields.update(quota=state.quota, quotaUnavailable=bool(state.failure or not state.quota))
        if state.failure:
            fields["quotaFailure"] = state.failure
        return fields

    async def close(self):
        if self.inflight:
            self.inflight[1].cancel()
            await asyncio.gather(self.inflight[1], return_exceptions=True)
            self.inflight = None
