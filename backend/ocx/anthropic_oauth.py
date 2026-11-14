"""Gateway-owned single Claude account, original browser/code PKCE and refresh semantics."""

import asyncio
import base64
import hashlib
import json
import math
import os
import secrets
import socket
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx

from .anthropic_transport import USER_AGENT
from .config import protect

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CALLBACK = "http://localhost:54545/callback"
CODE_CALLBACK = "https://platform.claude.com/oauth/code/callback"
SCOPES = "org:create_api_key user:profile user:inference"
TERMINAL = {
    "invalid_grant",
    "refresh_token_reused",
    "revoked",
    "revoked_token",
    "refresh_token_revoked",
}


class AnthropicTokenError(ValueError):
    def __init__(self, status=None, code=None):
        super().__init__(
            "Claude token request was rejected."
            if status is not None
            else "Claude token request outcome is unknown; sign in again before refreshing."
        )
        self.status = status
        self.code = code
        self.terminal = status in {400, 401} and code in TERMINAL


def _identity(value):
    return (
        value
        if isinstance(value, str)
        and 0 < len(value) <= 128
        and value == value.strip()
        and all(ord(c) >= 32 and ord(c) != 127 for c in value)
        else None
    )


def same_account(saved, candidate):
    """Prefer observed UUID evidence; otherwise compare observed email identity."""
    old_uuid = saved.get("anthropicIdentity", {}).get("accountUuid")
    new_uuid = candidate.get("anthropicIdentity", {}).get("accountUuid")
    if old_uuid and new_uuid:
        return old_uuid == new_uuid
    old_email, new_email = saved.get("email"), candidate.get("email")
    return (
        isinstance(old_email, str)
        and isinstance(new_email, str)
        and bool(old_email.strip())
        and old_email.strip().lower() == new_email.strip().lower()
    )


class AnthropicAuth:
    def __init__(self, directory: Path, *, client=None):
        self.path = directory / "anthropic-account.json"
        self.client = client or httpx.AsyncClient(timeout=30, follow_redirects=False)
        self.lock = asyncio.Lock()
        self.flow = None
        self.revision = 0
        self.on_change = None

    def load(self):
        if not self.path.exists():
            return None
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            return json.loads(protect(base64.b64decode(envelope["protected"]), decrypt=True))
        except (ValueError, KeyError, OSError, RuntimeError) as exc:
            raise ValueError(
                "The saved Claude account could not be unlocked; sign in again."
            ) from exc

    def save(self, credential):
        payload = base64.b64encode(protect(json.dumps(credential).encode())).decode()
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"protected": payload}), encoding="utf-8")
        temporary.replace(self.path)
        self.revision += 1

    async def changed(self):
        if self.on_change:
            await self.on_change()

    def status(self):
        error = None
        try:
            saved = self.load()
        except ValueError as exc:
            saved, error = None, str(exc)
        account = None
        if saved:
            email = saved.get("email")
            label = "Claude account"
            if isinstance(email, str) and "@" in email:
                local, domain = email.split("@", 1)
                label = f"{local[:1]}***@{domain}"
            account = {
                "id": saved["accountId"],
                "email": email,
                "alias": saved.get("alias"),
                "active": True,
                "expiresAt": int(saved["expires"] * 1000),
                "needsReauth": bool(saved.get("needsReauth") or saved.get("refreshIntent")),
                "maskedLabel": label,
                "quotaMode": "unsupported",
                "plan": saved.get("plan"),
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

    async def token_request(self, body):
        response = await self.client.post(
            TOKEN_URL,
            json=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Connection": "close",
            },
            follow_redirects=False,
        )
        if response.is_redirect:
            raise AnthropicTokenError()
        if response.is_error:
            code = None
            try:
                payload = response.json()
                if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                    code = payload["error"]
            except ValueError:
                pass
            raise AnthropicTokenError(response.status_code, code)
        try:
            payload = response.json()
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("access_token"), str)
                or not payload["access_token"]
            ):
                raise ValueError()
            return payload
        except ValueError as exc:
            raise AnthropicTokenError() from exc

    async def credentials(self, payload, previous=None, *, refresh=False):
        previous = previous or {}
        access = payload["access_token"]
        account = payload.get("account")
        account = account if isinstance(account, dict) else {}
        identity = _identity(account.get("uuid"))
        email = account.get("email_address")
        plan = previous.get("plan")
        try:
            timeout = 2 if refresh else 10
            async with (
                asyncio.timeout(timeout),
                self.client.stream(
                    "GET",
                    PROFILE_URL,
                    headers={
                        "Authorization": f"Bearer {access}",
                        "Accept": "application/json",
                        "anthropic-beta": "oauth-2025-04-20",
                        "Cache-Control": "no-cache",
                        "User-Agent": USER_AGENT,
                    },
                    timeout=timeout,
                    follow_redirects=False,
                ) as response,
            ):
                content = bytearray()
                if response.is_success:
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 65536:
                            raise ValueError("Claude profile is too large.")
                profile = json.loads(content) if content else None
                if isinstance(profile, dict):
                    reported = profile.get("account")
                    profile_id = (
                        _identity(reported.get("uuid")) if isinstance(reported, dict) else None
                    )
                    if not profile_id or (identity and profile_id != identity):
                        raise ValueError("Claude profile identity does not match the grant.")
                    identity = identity or profile_id
                    org = profile.get("organization")
                    if isinstance(org, dict):
                        plan = org.get("rate_limit_tier") or org.get("organization_type") or plan
        except httpx.HTTPError, ValueError, TimeoutError:
            pass
        duration = payload.get("expires_in", 3600)
        if not isinstance(duration, int | float) or not math.isfinite(duration) or duration < 0:
            duration = 3600
        expires = time.time() + duration - 300
        if not math.isfinite(expires):
            expires = time.time() + 3300
        old_identity = previous.get("anthropicIdentity", {}).get("accountUuid")
        if refresh and identity and old_identity and identity != old_identity:
            raise ValueError("Claude refresh returned a different account; sign in again.")
        result = {
            "access": access,
            "refresh": payload.get("refresh_token") or previous.get("refresh", ""),
            "expires": expires,
            "generation": secrets.token_hex(16),
            "accountId": previous.get("accountId") or identity or str(uuid4()),
            "email": email if isinstance(email, str) else previous.get("email"),
            "plan": plan if isinstance(plan, str) else None,
            "needsReauth": False,
        }
        if previous.get("alias"):
            result["alias"] = previous["alias"]
        if identity:
            result["anthropicIdentity"] = {
                "v": 1,
                "accountUuid": identity,
                "bearerSha256": hashlib.sha256(access.encode()).hexdigest(),
            }
        return result

    async def token(self, *, rejected_access=None, expected_account_id=None):
        async with self.lock:
            saved = self.load()
            if not saved or saved.get("needsReauth"):
                raise ValueError("Sign in to this gateway's Claude channel first.")
            if expected_account_id is not None and saved["accountId"] != expected_account_id:
                raise ValueError("The active Claude account changed; retry the request.")
            # The intent and token share one atomic encrypted record: successful rotation
            # removes the guard in the same commit. Crashes and ambiguous sends retain it.
            if saved.get("refreshIntent"):
                raise ValueError(
                    "The previous Claude token refresh outcome is unknown; sign in again."
                )
            force = rejected_access is not None and saved["access"] == rejected_access
            if not force and saved["expires"] > time.time():
                return saved
            if not saved.get("refresh"):
                self.save({**saved, "needsReauth": True})
                raise ValueError("Claude authorization expired; sign in again.")
            self.save(
                {**saved, "refreshIntent": {"generation": saved["generation"], "at": time.time()}}
            )
            try:
                payload = await self.token_request(
                    {
                        "grant_type": "refresh_token",
                        "client_id": CLIENT_ID,
                        "refresh_token": saved["refresh"],
                    }
                )
                renewed = await self.credentials(payload, saved, refresh=True)
                self.save(renewed)
                return renewed
            except AnthropicTokenError as exc:
                if exc.status is not None:
                    self.save({**saved, "needsReauth": exc.terminal})
                raise
            except httpx.ConnectError as exc:
                # Only direct token-host DNS failure proves the POST never reached
                # Anthropic. A proxy or generic connection reset is inconclusive.
                cause = exc
                dns_failure = False
                while cause is not None:
                    if isinstance(cause, socket.gaierror):
                        dns_failure = True
                    cause = cause.__cause__
                proxied = any(
                    value
                    for key, value in os.environ.items()
                    if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}
                )
                if dns_failure and not proxied:
                    self.save(saved)
                raise

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

    async def start(self, *, code_mode=False, open_browser=False, reauth_account_id=None):
        if reauth_account_id is not None:
            saved = self.load()
            if not saved or saved["accountId"] != reauth_account_id:
                raise ValueError("The selected Claude account is no longer active.")
        await self.cancel()
        flow = {
            "status": "pending",
            "ready": asyncio.Event(),
            "state": secrets.token_urlsafe(32),
            "code": asyncio.get_running_loop().create_future(),
            "reauthAccountId": reauth_account_id,
        }
        self.flow = flow
        flow["task"] = asyncio.create_task(self.run(flow, code_mode))
        await flow["ready"].wait()
        if flow.get("error"):
            raise ValueError(flow["error"])
        launch = "skipped"
        if open_browser:
            launch = (
                "started"
                if await asyncio.to_thread(webbrowser.open, flow["hint"]["url"])
                else "failed"
            )
        return {**flow["hint"], "browserLaunch": launch}

    def submit(self, value):
        flow = self.flow
        if not flow or flow.get("status") != "pending":
            raise ValueError("No Claude login is waiting for a code.")
        value = value.strip()
        if value.startswith(("http://", "https://")):
            parsed = urlsplit(value)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"localhost", "127.0.0.1"}
                or parsed.port != 54545
                or parsed.path != "/callback"
            ):
                raise ValueError("Paste this Claude login's callback URL or code#state.")
            query = parse_qs(parsed.query)
            code, state = query.get("code", [""])[0], query.get("state", [""])[0]
            if query.get("error"):
                if not secrets.compare_digest(state, flow["state"]):
                    raise ValueError("The callback belongs to a different Claude login.")
                if not flow["code"].done():
                    flow["code"].set_exception(ValueError("Claude authorization was declined."))
                return
        else:
            code, separator, state = value.partition("#")
            if not separator:
                state = flow["state"]
        if not code or not secrets.compare_digest(state, flow["state"]):
            raise ValueError("The code is empty or belongs to a different Claude login.")
        if not flow["code"].done():
            flow["code"].set_result(code)

    async def run(self, flow, code_mode):
        server = None
        try:
            redirect = CODE_CALLBACK if code_mode else CALLBACK
            verifier = secrets.token_urlsafe(48)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )

            async def callback(reader, writer):
                status = "200 OK"
                text = "Claude authorization received. Return to OpenCodex."
                try:
                    request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
                    method, path, _ = request.split(b"\r\n", 1)[0].decode("ascii").split(" ", 2)
                    if method != "GET":
                        raise ValueError()
                    self.submit("http://localhost:54545" + path)
                except (
                    ValueError,
                    OSError,
                    asyncio.IncompleteReadError,
                    asyncio.LimitOverrunError,
                    TimeoutError,
                ):
                    status, text = (
                        "400 Bad Request",
                        "This callback could not be accepted. Return to OpenCodex.",
                    )
                try:
                    content = text.encode()
                    writer.write(
                        f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {len(content)}\r\nConnection: close\r\n\r\n".encode()
                        + content
                    )
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            if not code_mode:
                server = await asyncio.start_server(callback, "127.0.0.1", 54545, limit=16384)
            flow["hint"] = {
                "url": "https://claude.ai/oauth/authorize?"
                + urlencode(
                    {
                        "code": "true",
                        "client_id": CLIENT_ID,
                        "response_type": "code",
                        "redirect_uri": redirect,
                        "scope": SCOPES,
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "state": flow["state"],
                    }
                ),
                "instructions": "Approve access in your browser, then paste the displayed code#state or callback URL if needed.",
            }
            flow["ready"].set()
            code = await asyncio.wait_for(flow["code"], 600 if code_mode else 300)
            payload = await self.token_request(
                {
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "code": code,
                    "state": flow["state"],
                    "redirect_uri": redirect,
                    "code_verifier": verifier,
                }
            )
            credential = await self.credentials(payload)
            async with self.lock:
                if self.flow is not flow or flow["status"] != "pending":
                    return
                target = flow["reauthAccountId"]
                try:
                    saved = self.load()
                except ValueError:
                    if target:
                        raise
                    saved = None
                if target and (
                    not saved or saved["accountId"] != target or not same_account(saved, credential)
                ):
                    raise ValueError("Claude authorization selected a different account.")
                if saved and same_account(saved, credential):
                    credential["accountId"] = saved["accountId"]
                    credential["alias"] = saved.get("alias")
                    if not credential.get("plan"):
                        credential["plan"] = saved.get("plan")
                self.save(credential)
                flow["status"] = "done"
            try:
                await self.changed()
            except ValueError, RuntimeError, OSError, httpx.HTTPError:
                flow["catalogPending"] = True
        except asyncio.CancelledError:
            raise
        except ValueError, TypeError, OSError, RuntimeError, httpx.HTTPError, TimeoutError:
            flow.update(
                status="failed",
                error="Claude authorization could not complete. Retry the login or use code login.",
            )
        finally:
            flow["ready"].set()
            if server:
                server.close()
                await server.wait_closed()
