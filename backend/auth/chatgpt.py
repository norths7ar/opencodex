"""Gateway-owned ChatGPT grants and one active account, ported from oauth/chatgpt*.ts."""

import asyncio
import base64
import hashlib
import logging
import math
import secrets
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .storage import AccountStorage
from .tokens import InvalidTokenResponse, TokenError, credentials, token_error

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_ROOT = "https://auth.openai.com"
TOKEN_URL = AUTH_ROOT + "/oauth/token"
CALLBACK = "http://localhost:1455/auth/callback"
DEVICE_CALLBACK = AUTH_ROOT + "/deviceauth/callback"
DEVICE_URL = AUTH_ROOT + "/codex/device"
CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"
logger = logging.getLogger(__name__)
LOGIN_STAGES = {
    "authorization": "browser/device authorization",
    "token": "token exchange",
    "identity": "account identity processing",
    "installation": "local installation identity",
    "models": "model directory query",
    "validation": "account inference validation",
    "save": "credential storage",
}


class ChatGPTAuth:
    def __init__(self, directory: Path):
        self.storage = AccountStorage(directory)
        self.client = httpx.AsyncClient(timeout=30, follow_redirects=False)
        self.lock = asyncio.Lock()
        self.flow = None
        self.revision = 0
        self.on_change = None
        self.validate = None

    @property
    def path(self):
        return self.storage.path

    def load(self):
        return self.storage.load()

    def save(self, credential):
        self.storage.save(credential)
        self.revision += 1

    def installation_id(self, account):
        return self.storage.installation_id(account)

    async def changed(self):
        if self.on_change:
            await self.on_change()

    def status(self):
        error = None
        try:
            credential = self.load()
        except ValueError as exc:
            credential, error = None, str(exc)
        account = None
        if credential:
            email = credential.get("email")
            label = "ChatGPT account"
            if isinstance(email, str) and "@" in email:
                local, domain = email.split("@", 1)
                label = f"{local[:1]}***@{domain}"
            account = {
                "id": credential["accountId"],
                "email": credential.get("email"),
                "alias": credential.get("alias"),
                "active": True,
                "expiresAt": int(credential["expires"] * 1000),
                "needsReauth": credential.get("needsReauth", False),
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
            "credentialUnavailable": error is not None,
            "accounts": [account] if account else [],
            "activeAccountId": account["id"] if account else None,
            **({"hint": flow["hint"]} if pending and flow.get("hint") else {}),
            **({"error": error or flow.get("error")} if error or flow.get("error") else {}),
        }

    async def token_request(self, body, *, form=False):
        response = await self.client.post(TOKEN_URL, **({"data": body} if form else {"json": body}))
        if response.is_error:
            raise token_error(response)
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError()
            return payload
        except (ValueError, TypeError) as exc:
            raise InvalidTokenResponse("ChatGPT returned an invalid token response.") from exc

    async def token(self, *, rejected_access=None, expected_account_id=None):
        async with self.lock:
            saved = self.load()
            if not saved or saved.get("needsReauth"):
                raise ValueError("Sign in to this gateway's ChatGPT channel first.")
            if expected_account_id is not None and saved["accountId"] != expected_account_id:
                raise ValueError("The active ChatGPT account changed; retry the request.")
            force = rejected_access is not None and saved["access"] == rejected_access
            if not force and saved["expires"] > time.time() + 60:
                return saved
            if not saved.get("refresh"):
                self.save({**saved, "needsReauth": True})
                raise ValueError("ChatGPT authorization expired; sign in again.")
            try:
                payload = await self.token_request(
                    {
                        "client_id": CLIENT_ID,
                        "grant_type": "refresh_token",
                        "refresh_token": saved["refresh"],
                    }
                )
                renewed = credentials(payload, saved)
            except TokenError as exc:
                if exc.terminal:
                    self.save({**saved, "needsReauth": True})
                raise
            self.save(renewed)
            return renewed

    async def mark_needs_reauth(self, expected_generation):
        async with self.lock:
            saved = self.load()
            if not saved or saved["generation"] != expected_generation:
                return False
            if not saved.get("needsReauth"):
                self.save({**saved, "needsReauth": True})
            return True

    async def cancel(self):
        flow = self.flow
        if flow and flow.get("status") == "pending":
            flow.update(status="cancelled", error="Login cancelled")
            task = flow.get("task")
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

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

    async def start(self, *, device=False, open_browser=False, reauth_account_id=None):
        if reauth_account_id is not None:
            saved = self.load()
            if not saved or saved["accountId"] != reauth_account_id:
                raise ValueError("The ChatGPT account selected for reauthorization is not active.")
        await self.cancel()
        flow = {
            "status": "pending",
            "ready": asyncio.Event(),
            "code": asyncio.get_running_loop().create_future(),
            "reauthAccountId": reauth_account_id,
        }
        self.flow = flow
        flow["task"] = asyncio.create_task(self.run(flow, device))
        await flow["ready"].wait()
        if flow.get("error"):
            raise ValueError(flow["error"])
        hint = flow.get("hint", {})
        launch = "skipped"
        if open_browser and not device and hint.get("url"):
            launch = (
                "started" if await asyncio.to_thread(webbrowser.open, hint["url"]) else "failed"
            )
        return {**hint, "browserLaunch": launch}

    def submit(self, url):
        flow = self.flow
        if not flow or flow.get("status") != "pending" or not flow.get("state"):
            raise ValueError("No browser login is waiting for a callback.")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1"}
            or parsed.port != 1455
            or parsed.path != "/auth/callback"
        ):
            raise ValueError("Paste the complete localhost callback URL from this login.")
        query = parse_qs(parsed.query)
        if not secrets.compare_digest(query.get("state", [""])[0], flow["state"]):
            raise ValueError("The callback belongs to a different login.")
        if query.get("error"):
            if not flow["code"].done():
                flow["code"].set_exception(ValueError("ChatGPT authorization was declined."))
            return
        code = query.get("code", [""])[0]
        if not code:
            raise ValueError("The callback URL is missing its authorization code.")
        if not flow["code"].done():
            flow["code"].set_result(code)

    async def browser_grant(self, flow):
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        flow["state"] = secrets.token_urlsafe(32)

        async def callback(reader, writer):
            try:
                request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
                line = request.split(b"\r\n", 1)[0].decode("ascii")
                method, path, _ = line.split(" ", 2)
                if method != "GET":
                    raise ValueError("Expected a browser callback.")
                self.submit("http://localhost:1455" + path)
                message, status = "Authorization received. Return to OpenCodex Lite.", "200 OK"
            except (
                TimeoutError,
                ValueError,
                UnicodeError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
            ):
                message, status = (
                    "Invalid authorization callback. Return to OpenCodex Lite and retry.",
                    "400 Bad Request",
                )
            content = message.encode()
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {len(content)}\r\nConnection: close\r\n\r\n".encode()
                + content
            )
            try:
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        try:
            listener = await asyncio.start_server(callback, "127.0.0.1", 1455, limit=16384)
        except OSError as exc:
            raise ValueError(
                "The ChatGPT callback port 1455 is in use. Choose device-code login or close the other login attempt."
            ) from exc
        async with listener:
            flow["hint"] = {
                "url": AUTH_ROOT
                + "/oauth/authorize?"
                + urlencode(
                    {
                        "response_type": "code",
                        "client_id": CLIENT_ID,
                        "redirect_uri": CALLBACK,
                        "scope": "openid profile email offline_access api.connectors.read api.connectors.invoke",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "state": flow["state"],
                        "codex_cli_simplified_flow": "true",
                        "originator": "codex_cli_rs",
                        "id_token_add_organizations": "true",
                        "prompt": "login",
                    }
                ),
                "instructions": "Complete ChatGPT login in your browser.",
            }
            flow["ready"].set()
            code = await asyncio.wait_for(flow["code"], 300)
            flow["stage"] = "token"
            return await self.token_request(
                {
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "code": code,
                    "redirect_uri": CALLBACK,
                    "code_verifier": verifier,
                },
                form=True,
            )

    async def device_grant(self, flow):
        response = await self.client.post(
            AUTH_ROOT + "/api/accounts/deviceauth/usercode", json={"client_id": CLIENT_ID}
        )
        if response.is_error:
            raise ValueError(
                f"ChatGPT device authorization request failed: HTTP {response.status_code}"
            )
        data = response.json()
        if not isinstance(data, dict):
            raise TypeError("ChatGPT device authorization returned an invalid response.")
        device_id, user_code = (
            data.get("device_auth_id"),
            data.get("user_code") or data.get("usercode"),
        )
        if (
            not isinstance(device_id, str)
            or not isinstance(user_code, str)
            or not device_id
            or not user_code
        ):
            raise ValueError("ChatGPT device authorization response is missing required fields.")
        try:
            interval = float(data.get("interval", 5))
            interval = max(1, min(900, interval)) if math.isfinite(interval) and interval > 0 else 5
        except ValueError, TypeError:
            interval = 5
        flow["hint"] = {
            "url": DEVICE_URL,
            "instructions": f"Enter code: {user_code}",
            "deviceCode": user_code,
        }
        flow["ready"].set()
        async with asyncio.timeout(900):
            while True:
                response = await self.client.post(
                    AUTH_ROOT + "/api/accounts/deviceauth/token",
                    json={"device_auth_id": device_id, "user_code": user_code},
                )
                if response.status_code in {403, 404}:
                    await asyncio.sleep(interval)
                    continue
                if response.is_error:
                    raise ValueError(
                        f"ChatGPT device authorization failed: HTTP {response.status_code}"
                    )
                grant = response.json()
                if not isinstance(grant, dict):
                    raise TypeError("ChatGPT device authorization returned an invalid grant.")
                code, verifier = grant.get("authorization_code"), grant.get("code_verifier")
                if (
                    not isinstance(code, str)
                    or not isinstance(verifier, str)
                    or not code
                    or not verifier
                ):
                    raise ValueError("ChatGPT device grant is missing required fields.")
                flow["stage"] = "token"
                return await self.token_request(
                    {
                        "grant_type": "authorization_code",
                        "client_id": CLIENT_ID,
                        "code": code,
                        "code_verifier": verifier,
                        "redirect_uri": DEVICE_CALLBACK,
                    },
                    form=True,
                )

    async def run(self, flow, device):
        flow["stage"] = "authorization"
        try:
            payload = await (self.device_grant(flow) if device else self.browser_grant(flow))
            flow["stage"] = "identity"
            credential = credentials(payload)
            target = flow.get("reauthAccountId")
            if target is not None and credential["accountId"] != target:
                raise ValueError("Sign in to the same ChatGPT account to reauthorize it.")
            flow["stage"] = "installation"
            credential["installationId"] = self.installation_id(credential["accountId"])
            if self.validate:
                await self.validate(credential)
            async with self.lock:
                if self.flow is not flow or flow["status"] != "pending":
                    return
                flow["stage"] = "save"
                try:
                    saved = self.load()
                except ValueError:
                    # A generic sign-in can replace a credential that cannot be unlocked.
                    if target is not None:
                        raise
                    saved = None
                if target is not None and (not saved or saved["accountId"] != target):
                    raise ValueError(
                        "The ChatGPT account selected for reauthorization is no longer active."
                    )
                if saved and saved["accountId"] == credential["accountId"]:
                    credential["alias"] = saved.get("alias")
                self.save(credential)
                flow["status"] = "done"
            try:
                await self.changed()
            except ValueError, RuntimeError, OSError:
                flow["catalogPending"] = True
        except asyncio.CancelledError:
            raise
        except (ValueError, TypeError, OSError, RuntimeError, httpx.HTTPError) as exc:
            self.login_failed(flow, exc)
        finally:
            flow["ready"].set()

    def login_failed(self, flow, exc):
        stage = flow.get("stage", "authorization")
        # Exception text/tracebacks may contain token responses or callback URLs.
        # Log only structural diagnostics, never request bodies, headers or local variables.
        chain, current = [], exc
        while current is not None and len(chain) < 8:
            detail = type(current).__name__
            if isinstance(current, OSError) and isinstance(current.errno, int):
                detail += f"(errno={current.errno})"
            chain.append(detail)
            current = current.__cause__
        logger.warning("ChatGPT login failed stage=%s causes=%s", stage, " <- ".join(chain))
        if isinstance(exc, httpx.TimeoutException | TimeoutError):
            reason = "The request timed out."
        elif isinstance(exc, httpx.HTTPError):
            reason = f"Network request failed ({type(exc).__name__}); check the gateway connection."
        elif isinstance(exc, ValueError):
            reason = str(exc)
        elif isinstance(exc, OSError):
            reason = "Local storage or operating system operation failed."
        else:
            reason = f"Processing failed ({type(exc).__name__}); see the gateway log."
        flow.update(status="failed", error=f"ChatGPT {LOGIN_STAGES[stage]} failed: {reason}")
