"""Quota observations for the gateway's one ChatGPT subscription."""

import asyncio
import contextlib
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import cast

import httpx

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DAY = 86_400
TTL_MS = 5 * 60_000
MAX_REPORT_AGE_MS = 30 * 60_000
MAX_DISK_AGE_MS = 6 * 60 * 60_000
MAX_BODY_BYTES = 512 * 1024
TERMINAL_AUTH_CODES = ("invalid_workspace_selected", "invalid_refresh_token", "token_invalidated")


def now_ms():
    return time.time_ns() // 1_000_000


def record(value):
    return value if isinstance(value, dict) else {}


def number(value, *, strings=True):
    if type(value) not in ((int, float, str) if strings else (int, float)):
        return None
    try:
        value = float(value)
    except ValueError, OverflowError:
        return None
    return value if math.isfinite(value) else None


def reset_ms(value):
    return value * 1000 if value < 10_000_000_000 else value


def reset_at(value):
    value = number(value)
    return value if value is not None and 0 <= reset_ms(value) <= 8.64e15 else None


def window_tuple(raw):
    window = record(raw)
    percent = number(window.get("used_percent"))
    return (
        max(0, min(100, percent)) if percent is not None else None,
        reset_at(window.get("reset_at")),
        number(window.get("limit_window_seconds"), strings=False) or 0,
    )


def add_window(quota, name, window):
    percent, reset, _ = window
    if percent is not None:
        quota[f"{name}Percent"] = percent
        if reset is not None:
            quota[f"{name}ResetAt"] = reset


def windows(primary, secondary, tertiary, *, monthly_only=False):
    quota = {}
    primary_short = 0 < primary[2] < DAY
    primary_monthly = primary[2] >= 28 * DAY
    if primary_short:
        percent, reset, seconds = primary
        if percent is not None:
            quota["shortPercent"] = percent
        if reset is not None:
            quota["shortResetAt"] = reset
        quota["shortWindowSeconds"] = seconds
    if not monthly_only:
        use_primary = not primary_short and not primary_monthly and primary[0] is not None
        add_window(quota, "weekly", primary if use_primary else secondary)
    use_primary = primary_monthly and primary[0] is not None
    add_window(quota, "monthly", primary if use_primary else tertiary)
    if use_primary:
        quota["monthlyIsPrimaryWindow"] = True
    return quota


def parse_credits(payload, now):
    raw = record(payload.get("credits"))
    control = payload.get("spend_control")
    allowed = None if control is None else record(control).get("reached") is False
    if not (
        "credits" in payload
        or allowed is False
        or record(payload.get("rate_limit")).get("allowed") is False
    ):
        return {}
    credits = {}
    for field, wire in (
        ("hasCredits", "has_credits"),
        ("unlimited", "unlimited"),
        ("overageLimitReached", "overage_limit_reached"),
    ):
        if isinstance(raw.get(wire), bool):
            credits[field] = raw[wire]
    balance = raw.get("balance")
    if not isinstance(balance, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", balance):
        numeric = number(balance)
        if numeric is not None and numeric >= 0:
            credits["balance"] = numeric
    # WHAM can send approximate message ranges without reporting any credit balance.
    has_range = any(
        isinstance(values := raw.get(name), list)
        and len(values) == 2
        and all(
            (numeric := number(item, strings=False)) is not None and numeric >= 0 for item in values
        )
        for name in ("approx_local_messages", "approx_cloud_messages")
    )
    if credits or has_range:
        if allowed is not None:
            credits["allowed"] = allowed
        return {"credits": {**credits, "observedAt": now}}
    return {
        "credits": None,
        **({"creditObservationUnavailable": True} if allowed is not False else {}),
    }


def parse_usage(payload, now):
    rate = record(payload.get("rate_limit"))
    plan = payload.get("plan_type")
    quota = windows(
        *(
            window_tuple(rate.get(f"{slot}_window"))
            for slot in ("primary", "secondary", "tertiary")
        ),
        monthly_only=isinstance(plan, str) and plan.strip().lower() in {"free", "go"},
    )
    quota.update(parse_credits(payload, now))
    count = number(
        record(payload.get("rate_limit_reset_credits")).get("available_count"), strings=False
    )
    if count is not None:
        quota["resetCredits"] = count
    return quota or None


def parse_headers(headers, model):
    if isinstance(model, str) and model.strip().lower().split("/")[-1] == "gpt-5.3-codex-spark":
        return None
    parts = []
    for slot in ("primary", "secondary", "tertiary"):
        prefix = f"x-codex-{slot}-"
        percent = headers.get(prefix + "used-percent")
        minutes = number(headers.get(prefix + "window-minutes")) or 0
        parts.append(
            window_tuple(
                {
                    "used_percent": percent,
                    "reset_at": headers.get(prefix + "reset-at"),
                    "limit_window_seconds": math.floor(minutes * 60 + 0.5)
                    if 0 < minutes < 1440
                    else minutes * 60
                    if percent is not None
                    else 0,
                }
            )
        )
    return windows(*parts) or None


def merge(previous, fresh, now, rate=None):
    result = {**previous, **fresh, "updatedAt": now}
    if "credits" in fresh and "creditObservationUnavailable" not in fresh:
        result.pop("creditObservationUnavailable", None)

    def drop(*keys):
        for key in keys:
            result.pop(key, None)

    for name in ("short", "weekly", "monthly"):
        if f"{name}Percent" in fresh and f"{name}ResetAt" not in fresh:
            drop(f"{name}ResetAt")
    if "monthlyPercent" in fresh:
        if "monthlyIsPrimaryWindow" not in fresh:
            drop("monthlyIsPrimaryWindow")
        if "weeklyPercent" not in fresh:
            drop("weeklyPercent", "weeklyResetAt")

    # An explicit, measured WHAM topology can retire windows; absent metadata and
    # response headers are partial observations and cannot prove absence.
    if rate and rate.get("allowed") is not False:

        def measured_long(raw):
            usage = number(record(raw).get("used_percent"))
            return usage is not None and 0 <= usage <= 100 and window_tuple(raw)[2] >= DAY

        primary, secondary, tertiary = (
            rate.get(f"{slot}_window") for slot in ("primary", "secondary", "tertiary")
        )
        complete = (
            (
                measured_long(primary)
                or ("primary_window" in rate and primary is None and measured_long(secondary))
            )
            and (measured_long(secondary) or ("secondary_window" in rate and secondary is None))
            and (
                measured_long(tertiary)
                or (tertiary is None and ("tertiary_window" in rate or rate.get("allowed") is True))
            )
        )
        if complete:
            drop("shortPercent", "shortResetAt", "shortWindowSeconds", "shortObservedAt")
            if (
                tertiary is None
                and max(window_tuple(primary)[2], window_tuple(secondary)[2]) < 28 * DAY
            ):
                drop("monthlyPercent", "monthlyResetAt", "monthlyIsPrimaryWindow")
    if "shortWindowSeconds" in fresh:
        for key in ("shortPercent", "shortResetAt", "shortObservedAt"):
            if key not in fresh:
                result.pop(key, None)
        if "shortPercent" in fresh:
            result["shortObservedAt"] = now
    elif previous.get("shortResetAt", 0) > 0 and reset_ms(previous["shortResetAt"]) <= now:
        drop("shortPercent", "shortResetAt", "shortWindowSeconds", "shortObservedAt")
    return result


def provider_quota(quota):
    if not quota:
        return None
    fields = {
        "shortPercent": "fiveHourPercent",
        "shortResetAt": "fiveHourResetAt",
        "shortObservedAt": "shortObservedAt",
        "shortWindowSeconds": "shortWindowSeconds",
        "weeklyPercent": "weeklyPercent",
        "weeklyResetAt": "weeklyResetAt",
        "monthlyPercent": "monthlyPercent",
        "monthlyResetAt": "monthlyResetAt",
    }
    projected = {target: quota[key] for key, target in fields.items() if key in quota}
    if not any(key in projected for key in ("fiveHourPercent", "weeklyPercent", "monthlyPercent")):
        return None
    return {**projected, "updatedAt": quota["updatedAt"]}


def plan_name(value):
    if isinstance(value, str) and value.strip() and len(value) <= 100 and value.isprintable():
        return value
    return None


def disk_quota(value):
    raw = record(value)
    quota = {}
    for name in ("short", "weekly", "monthly"):
        percent = number(raw.get(f"{name}Percent"), strings=False)
        if percent is not None and 0 <= percent <= 100:
            quota[f"{name}Percent"] = percent
        reset = reset_at(raw.get(f"{name}ResetAt"))
        if reset is not None:
            quota[f"{name}ResetAt"] = reset
    for key in ("updatedAt", "shortObservedAt", "shortWindowSeconds", "resetCredits"):
        numeric = number(raw.get(key), strings=False)
        if numeric is not None and numeric >= 0:
            quota[key] = numeric
    if raw.get("monthlyIsPrimaryWindow") is True:
        quota["monthlyIsPrimaryWindow"] = True
    # Spendable credits have a five-minute, in-process observation clock. They
    # are fetched again after restart rather than rehydrating spending evidence.
    return quota


def retry_after_ms(value):
    delay = number(value)
    if delay is not None:
        return min(DAY * 1000, max(0, delay * 1000))
    if value:
        try:
            date = parsedate_to_datetime(value)
            delay = (
                date if date.tzinfo else date.replace(tzinfo=UTC)
            ).timestamp() * 1000 - now_ms()
            return min(DAY * 1000, max(0, delay))
        except TypeError, ValueError, OverflowError:
            pass
    return 0


class ProbeFailure(Exception):
    def __init__(self, code, *, status=None, retry_after: float = 0, terminal=False):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after
        self.terminal = terminal


async def read_json(response, limit=MAX_BODY_BYTES):
    length = number(response.headers.get("content-length"))
    if length is not None and length > limit:
        raise ProbeFailure("response_unusable")
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
        if len(body) + len(chunk) > limit:
            raise ProbeFailure("response_unusable")
        body.extend(chunk)
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ProbeFailure("response_unusable")
    return payload


async def terminal_auth_response(response):
    try:
        async with asyncio.timeout(1):
            payload = await read_json(response, limit=16 * 1024)
    except TimeoutError, httpx.HTTPError, ProbeFailure, ValueError, RecursionError:
        return False
    for key in ("detail", "error"):
        if isinstance(payload.get(key), dict):
            return payload[key].get("code") in TERMINAL_AUTH_CODES
    return payload.get("code") in TERMINAL_AUTH_CODES


@dataclass
class Observation:
    account: str
    installation: str
    quota: dict | None = None
    plan: str | None = None
    check_count: int = 0
    checked_at: int = 0
    failure: str | None = None
    retry_at: float = 0
    delay_ms: int = 0
    rejected_generation: str | None = None


class ChatGPTQuota:
    def __init__(self, auth):
        self.auth = auth
        self.path = auth.path.with_name("chatgpt-quota.json")
        self.state = None
        self.lock = asyncio.Lock()
        self.invalidation = 0

    def invalidate(self):
        self.invalidation += 1
        self.state = None
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)

    def _active(self):
        try:
            return self.auth.load()
        except ValueError:
            return None

    def _state_for(self, credential):
        account = credential["accountId"]
        installation = credential["installationId"]
        if (
            self.state is not None
            and self.state.account == account
            and self.state.installation == installation
        ):
            return self.state
        state = self.state = Observation(account, installation)
        try:
            with self.path.open("rb") as file:
                body = file.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                return state
            saved = record(json.loads(body))
            quota = disk_quota(saved["quota"])
            age = now_ms() - quota["updatedAt"]
            if saved["installation"] == installation and 0 <= age < MAX_DISK_AGE_MS:
                state.quota = quota
                state.plan = plan_name(saved.get("plan"))
        except OSError, ValueError, KeyError, RecursionError:
            pass
        return state

    def _publish(self, state, fresh, *, rate=None):
        state.quota = merge(state.quota or {}, fresh, now_ms(), rate)
        # Only non-secret quota display state is persisted. OAuth retains ownership of grants.
        temporary = self.path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "installation": state.installation,
                        "plan": state.plan,
                        "quota": disk_quota(state.quota),
                    }
                ),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError, ValueError:
            pass

    def observe_headers(self, credential, headers, *, model=None):
        fresh = parse_headers(headers, model)
        if fresh is None:
            return
        active = self._active()
        if (
            active
            and not active.get("needsReauth")
            and active["generation"] == credential["generation"]
        ):
            self._publish(self._state_for(active), fresh)

    async def _usage(self, credential):
        try:
            async with (
                asyncio.timeout(8),
                self.auth.client.stream(
                    "GET",
                    USAGE_URL,
                    follow_redirects=False,
                    headers={
                        "authorization": f"Bearer {credential['access']}",
                        "chatgpt-account-id": credential["accountId"],
                        "accept": "application/json",
                    },
                ) as response,
            ):
                status = response.status_code
                if not response.is_success:
                    code = {401: "access_denied", 403: "access_denied", 429: "rate_limited"}.get(
                        status
                    )
                    raise ProbeFailure(
                        code or ("redirect_blocked" if response.is_redirect else "upstream_error"),
                        status=status,
                        retry_after=retry_after_ms(response.headers.get("retry-after")),
                        terminal=status in {401, 403} and await terminal_auth_response(response),
                    )
                return await read_json(response)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ProbeFailure("timeout") from exc
        except httpx.HTTPError as exc:
            raise ProbeFailure("transport_error") from exc
        except (ValueError, RecursionError) as exc:
            raise ProbeFailure("response_unusable") from exc

    async def read(self, *, force=False):
        requested = self._active()
        if requested is None:
            return None
        epoch = self.invalidation
        previous_check = self._state_for(requested).check_count
        async with self.lock:
            current = self._active()
            if (
                not current
                or current["accountId"] != requested["accountId"]
                or epoch != self.invalidation
            ):
                return None
            state = self._state_for(current)
            if current.get("needsReauth"):
                state.failure = "access_denied"
                return state
            now = now_ms()
            if (
                state.retry_at > now
                or state.check_count != previous_check
                or (not force and state.checked_at and now - state.checked_at < TTL_MS)
            ):
                return state

            dispatched = current
            failure = None
            successful_probe: tuple[dict[str, object], dict[str, object]] | None = None
            try:
                dispatched = await self.auth.token(expected_account_id=state.account)
                if epoch != self.invalidation:
                    return None
                try:
                    payload = await self._usage(dispatched)
                except ProbeFailure as exc:
                    if epoch != self.invalidation:
                        return None
                    if (
                        exc.status != 401
                        or exc.terminal
                        or state.rejected_generation == dispatched["generation"]
                    ):
                        raise
                    dispatched = await self.auth.token(
                        expected_account_id=state.account, rejected_access=dispatched["access"]
                    )
                    if epoch != self.invalidation:
                        return None
                    state.rejected_generation = dispatched["generation"]
                    payload = await self._usage(dispatched)
                payload_account = payload.get("account_id")
                if (
                    isinstance(payload_account, str)
                    and payload_account
                    and payload_account != state.account
                ):
                    raise ProbeFailure("account_unavailable")
                fresh = parse_usage(payload, now_ms())
                if fresh is None:
                    raise ProbeFailure("response_unusable")
                successful_probe = (payload, fresh)
            except ProbeFailure as exc:
                failure = exc
                if exc.terminal and epoch == self.invalidation:
                    with contextlib.suppress(OSError, ValueError):
                        await self.auth.mark_needs_reauth(dispatched["generation"])
            except TimeoutError, httpx.TimeoutException:
                failure = ProbeFailure("timeout")
            except httpx.HTTPError:
                failure = ProbeFailure("transport_error")
            except OSError, ValueError:
                active = self._active()
                failure = ProbeFailure(
                    "access_denied" if active and active.get("needsReauth") else "upstream_error"
                )

            active = self._active()
            if (
                not active
                or active["generation"] != dispatched["generation"]
                or epoch != self.invalidation
            ):
                return None
            state.check_count += 1
            state.checked_at = now_ms()
            state.failure = failure.code if failure else None
            if failure:
                state.delay_ms = min((state.delay_ms or TTL_MS // 2) * 2, 60 * 60_000)
                state.retry_at = state.checked_at + max(state.delay_ms, failure.retry_after)
            else:
                # No failure means both the payload and parsed quota were accepted.
                payload, fresh = cast(tuple[dict[str, object], dict[str, object]], successful_probe)
                state.delay_ms = state.retry_at = 0
                state.plan = plan_name(payload.get("plan_type")) or state.plan
                self._publish(state, fresh, rate=record(payload.get("rate_limit")))
            return state

    def account_fields(self, account, *, enriched=False):
        fields: dict[str, object] = {"quotaMode": "probe"}
        credential = self._active()
        if not credential or credential["accountId"] != account:
            return fields
        state = self._state_for(credential)
        if state.plan:
            fields["plan"] = state.plan
        if enriched:
            failure = "access_denied" if credential.get("needsReauth") else state.failure
            observed = state.quota
            quota = provider_quota(observed)
            if (
                quota is None
                and observed
                and any(key in observed for key in ("resetCredits", "credits"))
            ):
                quota = {"updatedAt": observed["updatedAt"]}
            if quota and now_ms() - quota["updatedAt"] < MAX_REPORT_AGE_MS:
                quota = {
                    **quota,
                    **{
                        key: observed[key]
                        for key in ("resetCredits", "credits")
                        if observed is not None and key in observed
                    },
                }
            else:
                quota = None
            fields.update(quota=quota, quotaUnavailable=bool(failure or quota is None))
            if failure:
                fields["quotaFailure"] = failure
        return fields
