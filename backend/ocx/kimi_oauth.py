"""Single Kimi account shared by the Chat and Responses provider presets."""

import asyncio
import base64
import json
import math
import os
import platform
import time
from pathlib import Path
from uuid import uuid4

import httpx

from .config import protect

CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
VERSION = "0.14.0"
TERMINAL = {"invalid_grant", "invalid_token", "refresh_token_reused", "revoked", "access_denied"}


def identity(access, refresh=None):
    def payload(token):
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return {}
            value = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            return value if isinstance(value, dict) else {}
        except AttributeError, ValueError, UnicodeError:
            return {}

    a, r = payload(access), payload(refresh)
    account = next(
        (
            value
            for value in (a.get("user_id"), r.get("user_id"), a.get("sub"), r.get("sub"))
            if isinstance(value, str) and value
        ),
        None,
    )
    email = next(
        (
            value.lower()
            for value in (a.get("email"), r.get("email"))
            if isinstance(value, str) and value
        ),
        None,
    )
    return {**({"accountId": account} if account else {}), **({"email": email} if email else {})}


def observed_account(credential):
    return credential.get("upstreamAccountId") or (
        credential.get("accountId") if credential.get("identityObserved") else None
    )


def same_account(saved, candidate):
    old, new = observed_account(saved), observed_account(candidate)
    if old and new:
        return old == new
    old_email, new_email = saved.get("email"), candidate.get("email")
    return (
        isinstance(old_email, str)
        and isinstance(new_email, str)
        and bool(old_email.strip())
        and old_email.strip().lower() == new_email.strip().lower()
    )


class KimiAuth:
    def __init__(self, directory: Path, *, client=None):
        self.path = directory / "kimi-account.json"
        self.device_path = directory / "kimi-device-id"
        self.client = client or httpx.AsyncClient(timeout=30, follow_redirects=False)
        self.lock = asyncio.Lock()
        self.flow = None
        self.revision = 0
        self.on_change = None

    def load(self):
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return json.loads(protect(base64.b64decode(data["protected"]), decrypt=True))
        except (ValueError, KeyError, OSError, RuntimeError) as exc:
            raise ValueError(
                "The saved Kimi account could not be unlocked; sign in again."
            ) from exc

    def save(self, credential):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = base64.b64encode(protect(json.dumps(credential).encode())).decode()
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"protected": encoded}), encoding="utf-8")
        temporary.replace(self.path)
        self.revision += 1

    async def changed(self):
        if self.on_change:
            await self.on_change()

    def headers(self):
        try:
            device = self.device_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            device = ""
        if not device:
            device = uuid4().hex
            self.device_path.parent.mkdir(parents=True, exist_ok=True)
            self.device_path.write_text(device + "\n", encoding="utf-8")
        architecture = {"AMD64": "x64", "ARM64": "arm64"}.get(
            platform.machine(), platform.machine()
        )
        return {
            "User-Agent": f"KimiCLI/{VERSION}",
            "X-Msh-Platform": "kimi_code_cli",
            "X-Msh-Version": VERSION,
            "X-Msh-Device-Name": platform.node(),
            "X-Msh-Device-Model": f"Windows {platform.release()} {architecture}",
            "X-Msh-Os-Version": platform.version(),
            "X-Msh-Device-Id": device,
        }

    def status(self):
        error = None
        try:
            saved = self.load()
        except ValueError as exc:
            saved, error = None, str(exc)
        account = None
        if saved:
            email = saved.get("email")
            label = (
                f"{email[:1]}***@{email.split('@', 1)[1]}"
                if email and "@" in email
                else "Kimi account"
            )
            account = {
                "id": saved["accountId"],
                "email": email,
                "alias": saved.get("alias"),
                "active": True,
                "expiresAt": int(saved["expires"] * 1000),
                "needsReauth": bool(saved.get("needsReauth")),
                "maskedLabel": label,
                "quotaMode": "unsupported",
            }
        flow = self.flow or {}
        pending = flow.get("status") == "pending"
        return {
            "loggedIn": bool(account and not account["needsReauth"] and not pending),
            "pending": pending,
            "done": flow.get("status") == "done",
            "email": account.get("email") if account else None,
            "needsReauth": bool(account and account["needsReauth"]),
            "accounts": [account] if account else [],
            "activeAccountId": account["id"] if account else None,
            **({"hint": flow["hint"]} if pending and flow.get("hint") else {}),
            **({"error": error or flow.get("error")} if error or flow.get("error") else {}),
        }

    async def request(self, path, body):
        host = (
            os.environ.get("KIMI_CODE_OAUTH_HOST")
            or os.environ.get("KIMI_OAUTH_HOST")
            or "https://auth.kimi.com"
        )
        response = await self.client.post(
            host.rstrip("/") + path, data=body, headers=self.headers(), follow_redirects=False
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return response, payload if isinstance(payload, dict) else {}

    def credentials(self, payload, previous=None):
        previous = previous or {}
        access, duration = payload.get("access_token"), payload.get("expires_in")
        refresh = payload.get("refresh_token", previous.get("refresh"))
        if (
            not isinstance(access, str)
            or not access
            or not isinstance(refresh, str)
            or not refresh
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration < 0
        ):
            raise ValueError("Kimi token response is missing required fields.")
        expires = time.time() + duration - 300
        if not math.isfinite(expires) or not math.isfinite(expires * 1000):
            raise ValueError("Kimi token response has an invalid expiry.")
        observed = identity(access, refresh)
        if (
            observed_account(previous)
            and observed.get("accountId")
            and observed["accountId"] != observed_account(previous)
        ):
            raise ValueError("Kimi refresh selected a different account; sign in again.")
        return {
            "access": access,
            "refresh": refresh,
            "expires": expires,
            "accountId": previous.get("accountId")
            or observed.get("accountId")
            or "kimi-" + uuid4().hex,
            "upstreamAccountId": observed.get("accountId") or observed_account(previous),
            "identityObserved": bool(observed.get("accountId") or previous.get("identityObserved")),
            "email": observed.get("email", previous.get("email")),
            "alias": previous.get("alias"),
            "generation": uuid4().hex,
        }

    async def token(self, *, rejected_access=None, expected_account_id=None):
        async with self.lock:
            saved = self.load()
            if not saved or saved.get("needsReauth"):
                raise ValueError("Sign in to this gateway's Kimi channel first.")
            if expected_account_id is not None and saved["accountId"] != expected_account_id:
                raise ValueError("The active Kimi account changed; retry the request.")
            force = rejected_access is not None and saved["access"] == rejected_access
            if not force and saved["expires"] > time.time() + 60:
                return saved
            response, payload = await self.request(
                "/api/oauth/token",
                {
                    "grant_type": "refresh_token",
                    "refresh_token": saved["refresh"],
                    "client_id": CLIENT_ID,
                },
            )
            if not response.is_success:
                if response.status_code in {400, 401, 403} and (
                    payload.get("error") in TERMINAL or response.status_code in {401, 403}
                ):
                    self.save({**saved, "needsReauth": True})
                raise ValueError(f"Kimi token refresh failed (HTTP {response.status_code}).")
            renewed = self.credentials(payload, saved)
            self.save(renewed)
            return renewed

    async def mark_needs_reauth(self, expected_generation):
        async with self.lock:
            saved = self.load()
            if not saved or saved["generation"] != expected_generation:
                return False
            self.save({**saved, "needsReauth": True})
            return True

    async def cancel(self):
        flow = self.flow
        if flow and flow.get("status") == "pending":
            flow.update(status="cancelled", error="Login cancelled")
            flow["ready"].set()
            flow["task"].cancel()
            await asyncio.gather(flow["task"], return_exceptions=True)

    async def logout(self):
        await self.cancel()
        async with self.lock:
            self.path.unlink(missing_ok=True)
            self.flow = None
            self.revision += 1
        await self.changed()

    async def close(self):
        await self.cancel()
        await self.client.aclose()

    def submit(self, value):
        raise ValueError("Enter the Kimi device code on the verification page.")

    async def start(self, *, device=True, open_browser=False, reauth_account_id=None):
        if not device:
            raise ValueError("Kimi uses device authorization.")
        if reauth_account_id is not None:
            saved = self.load()
            if not saved or saved["accountId"] != reauth_account_id:
                raise ValueError("The selected Kimi account is no longer active.")
        await self.cancel()
        flow = {"status": "pending", "ready": asyncio.Event(), "reauthAccountId": reauth_account_id}
        self.flow = flow
        flow["task"] = asyncio.create_task(self.run(flow))
        await flow["ready"].wait()
        if flow.get("error"):
            raise ValueError(flow["error"])
        return {**flow["hint"], "browserLaunch": "skipped"}

    async def run(self, flow):
        try:
            response, payload = await self.request(
                "/api/oauth/device_authorization", {"client_id": CLIENT_ID}
            )
            if not response.is_success or any(
                not isinstance(payload.get(k), str) or not payload[k]
                for k in ("user_code", "device_code", "verification_uri")
            ):
                raise ValueError("Kimi device authorization could not start.")
            flow["hint"] = {
                "url": payload.get("verification_uri_complete") or payload["verification_uri"],
                "deviceCode": payload["user_code"],
                "instructions": f"Enter code: {payload['user_code']}",
            }
            flow["ready"].set()
            ttl, interval = payload.get("expires_in", 900), payload.get("interval", 5)
            ttl = (
                ttl
                if isinstance(ttl, (int, float))
                and not isinstance(ttl, bool)
                and math.isfinite(ttl)
                else 900
            )
            interval = (
                interval
                if isinstance(interval, (int, float))
                and not isinstance(interval, bool)
                and math.isfinite(interval)
                and interval > 0
                else 5
            )
            deadline, wait = time.monotonic() + ttl, max(1, interval)
            code = payload["device_code"]
            while time.monotonic() < deadline:
                response, payload = await self.request(
                    "/api/oauth/token",
                    {
                        "client_id": CLIENT_ID,
                        "device_code": code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                )
                if response.is_success and payload.get("access_token"):
                    credential = self.credentials(payload)
                    async with self.lock:
                        if self.flow is not flow or flow["status"] != "pending":
                            return
                        try:
                            saved = self.load()
                        except ValueError:
                            if flow["reauthAccountId"]:
                                raise
                            saved = None
                        same = bool(saved and same_account(saved, credential))
                        if flow["reauthAccountId"] and (
                            not same or saved["accountId"] != flow["reauthAccountId"]
                        ):
                            raise ValueError("Kimi authorization selected a different account.")
                        if same:
                            credential["accountId"] = saved["accountId"]
                            credential["alias"] = saved.get("alias")
                        self.save(credential)
                        flow["status"] = "done"
                    try:
                        await self.changed()
                    except ValueError, RuntimeError, OSError, httpx.HTTPError:
                        flow["catalogPending"] = True
                    return
                error = payload.get("error")
                if error == "slow_down":
                    wait += 5
                    supplied = payload.get("interval")
                    if (
                        isinstance(supplied, (int, float))
                        and not isinstance(supplied, bool)
                        and math.isfinite(supplied)
                    ):
                        wait = max(wait, supplied)
                elif error != "authorization_pending":
                    raise ValueError(
                        {
                            "expired_token": "Kimi device authorization expired.",
                            "access_denied": "Kimi device authorization denied.",
                        }.get(error, "Kimi device authorization failed.")
                    )
                await asyncio.sleep(min(wait, max(0, deadline - time.monotonic())))
            raise ValueError("Kimi device authorization expired.")
        except asyncio.CancelledError:
            raise
        except (ValueError, RuntimeError, OSError, httpx.HTTPError) as exc:
            flow.update(
                status="failed",
                error=str(exc)
                if isinstance(exc, ValueError)
                else "Kimi authorization could not complete; check the connection and retry.",
            )
        finally:
            flow["ready"].set()
