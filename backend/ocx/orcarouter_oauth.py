"""OrcaRouter browser PKCE grant yielding one durable subscription API key."""

import asyncio
import base64
import hashlib
import json
import math
import secrets
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx

from .config import protect

API_ORIGIN = "https://api.orcarouter.ai"
AUTH_ORIGIN = "https://www.orcarouter.ai"


def normalize_origin(value=API_ORIGIN):
    try:
        url = urlsplit(value.strip())
        host, port = url.hostname, url.port
    except ValueError, AttributeError:
        raise ValueError("OrcaRouter base URL is invalid.") from None
    if (
        not host
        or url.scheme not in {"https", "http"}
        or (url.scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"})
    ):
        raise ValueError("OrcaRouter requires HTTPS except for loopback HTTP.")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError(
            "OrcaRouter base URL must not contain credentials, a query, or a fragment."
        )
    if url.path.rstrip("/") not in {"", "/v1"}:
        raise ValueError("OrcaRouter base URL path must be empty or /v1.")
    display = "[" + host.lower() + "]" if ":" in host else host.lower()
    return (
        url.scheme
        + "://"
        + display
        + (
            ":" + str(port)
            if port is not None and port != (443 if url.scheme == "https" else 80)
            else ""
        )
    )


def auth_origin(base_url=API_ORIGIN, auth_base_url=None):
    if auth_base_url:
        return normalize_origin(auth_base_url)
    origin = normalize_origin(base_url)
    return AUTH_ORIGIN if origin == API_ORIGIN else origin


def credentials(payload):
    if not isinstance(payload, dict):
        raise ValueError("OrcaRouter key exchange returned an invalid response.")  # noqa: TRY004 - malformed upstream JSON
    key = payload.get("key")
    key = key.strip() if isinstance(key, str) else ""
    if not key.startswith("sk-orca-") or len(key) > 4096 or "\r" in key or "\n" in key:
        raise ValueError("OrcaRouter key exchange did not return a valid API key.")
    if "scope" in payload and payload["scope"] != "api":
        raise ValueError("OrcaRouter key exchange did not grant the required api scope.")
    account = payload.get("user_id")
    if isinstance(account, str):
        account = account.strip()
    elif (
        type(account) in {int, float}
        and math.isfinite(account)
        and int(account) == account
        and abs(account) <= 9007199254740991
    ):
        account = str(int(account))
    else:
        account = ""
    if not account or len(account) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in account):
        raise ValueError("OrcaRouter key exchange did not return a valid user id.")
    return {
        "access": key,
        "refresh": key,
        "expires": 9007199254740.991,
        "accountId": account,
        "generation": uuid4().hex,
    }


class OrcaRouterAuth:
    def __init__(self, directory: Path, *, client=None):
        self.path = directory / "orcarouter-account.json"
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
                "The saved OrcaRouter account could not be unlocked; sign in again."
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
                else "OrcaRouter account"
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

    async def token(self, *, rejected_access=None, expected_account_id=None):
        async with self.lock:
            saved = self.load()
            if not saved or saved.get("needsReauth"):
                raise ValueError("Sign in to this gateway's OrcaRouter channel first.")
            if expected_account_id is not None and expected_account_id != saved["accountId"]:
                raise ValueError("The active OrcaRouter account changed; retry the request.")
            if rejected_access is not None and rejected_access == saved["access"]:
                self.save({**saved, "needsReauth": True})
                raise ValueError("OrcaRouter API keys cannot be refreshed; reconnect your account.")
            return saved

    def submit(self, value):
        flow = self.flow
        if not flow or flow.get("status") != "pending" or flow["code"].done():
            raise ValueError("No OrcaRouter login is waiting for a code.")
        value = value.strip()
        parsed = urlsplit(value)
        state, code = None, None
        structured = bool(parsed.scheme)
        if structured:
            query = parse_qs(parsed.query, keep_blank_values=True)
            source = query if "code" in query else parse_qs(parsed.fragment, keep_blank_values=True)
            code, state = source.get("code", [None])[0], source.get("state", [None])[0]
        elif "code=" in value:
            structured = True
            source = parse_qs(value.lstrip("?#"), keep_blank_values=True)
            code, state = source.get("code", [None])[0], source.get("state", [None])[0]
        else:
            parts = value.split("#", 1)
            code = parts[0]
            state = parts[1] if len(parts) == 2 else None
        if not code or ((structured or state is not None) and state != flow["state"]):
            raise ValueError(
                "The OrcaRouter authorization code or state does not match this login."
            )
        flow["code"].set_result(code)

    async def start(
        self,
        *,
        base_url=API_ORIGIN,
        auth_base_url=None,
        open_browser=False,
        reauth_account_id=None,
        code_mode=False,
    ):
        api, origin = normalize_origin(base_url), auth_origin(base_url, auth_base_url)
        if reauth_account_id is not None:
            saved = self.load()
            if not saved or saved["accountId"] != reauth_account_id:
                raise ValueError("The selected OrcaRouter account is no longer active.")
        await self.cancel()
        flow = {
            "status": "pending",
            "ready": asyncio.Event(),
            "state": secrets.token_hex(16),
            "code": asyncio.get_running_loop().create_future(),
            "reauthAccountId": reauth_account_id,
        }
        self.flow = flow
        flow["task"] = asyncio.create_task(self.run(flow, api, origin))
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

    async def run(self, flow, api, origin):
        server = None
        try:

            async def callback(reader, writer):
                status, text = "200 OK", "Authorization received. Return to OpenCodex."
                try:
                    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
                    method, path, _ = raw.split(b"\r\n", 1)[0].decode("ascii").split(" ", 2)
                    url = urlsplit(path)
                    query = parse_qs(url.query, keep_blank_values=True)
                    if method != "GET" or url.path != "/callback":
                        raise ValueError()
                    if query.get("state", [None])[0] != flow["state"]:
                        raise ValueError()
                    if query.get("error"):
                        if not flow["code"].done():
                            flow["code"].set_exception(
                                ValueError("OrcaRouter authorization was denied.")
                            )
                        status, text = (
                            "400 Bad Request",
                            "Authorization was denied. Return to OpenCodex.",
                        )
                    else:
                        self.submit("http://127.0.0.1" + path)
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
                    body = text.encode()
                    writer.write(
                        f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                        + body
                    )
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            try:
                server = await asyncio.start_server(callback, "127.0.0.1", 51733, limit=16384)
            except OSError as exc:
                if exc.errno not in {48, 98, 10048} and getattr(exc, "winerror", None) != 10048:
                    raise
                server = await asyncio.start_server(callback, "127.0.0.1", 0, limit=16384)
            port = server.sockets[0].getsockname()[1]
            verifier = secrets.token_urlsafe(48)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            flow["hint"] = {
                "url": origin
                + "/auth?"
                + urlencode(
                    {
                        "callback_url": f"http://127.0.0.1:{port}/callback",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "state": flow["state"],
                        "app_name": "OpenCodex",
                        "scope": "api",
                    }
                ),
                "instructions": "Approve access in your browser. If the browser cannot reach this machine, choose the displayed-code option and paste the code here.",
            }
            flow["ready"].set()
            code = await asyncio.wait_for(flow["code"], 300)
            async with asyncio.timeout(30):
                async with self.client.stream(
                    "POST",
                    origin + "/api/v1/auth/keys",
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    json={"code": code, "code_verifier": verifier, "code_challenge_method": "S256"},
                    follow_redirects=False,
                ) as response:
                    if not response.is_success:
                        raise ValueError(
                            f"OrcaRouter key exchange failed (HTTP {response.status_code})."
                        )
                    body = bytearray()
                    async for part in response.aiter_bytes(chunk_size=16384):
                        if len(body) + len(part) > 65536:
                            raise ValueError(
                                "OrcaRouter key exchange response exceeded the 65536-byte limit."
                            )
                        body.extend(part)
                    try:
                        payload = json.loads(body.decode("utf-8"))
                    except ValueError, UnicodeError:
                        raise ValueError("OrcaRouter key exchange returned invalid JSON.") from None
            credential = credentials(payload)
            credential["apiBaseUrl"] = api + "/v1"
            async with self.lock:
                if self.flow is not flow or flow["status"] != "pending":
                    return
                try:
                    saved = self.load()
                except ValueError:
                    if flow["reauthAccountId"]:
                        raise
                    saved = None
                if flow["reauthAccountId"] and (
                    not saved
                    or saved["accountId"] != flow["reauthAccountId"]
                    or credential["accountId"] != saved["accountId"]
                ):
                    raise ValueError("OrcaRouter authorization selected a different account.")
                if saved and saved["accountId"] == credential["accountId"]:
                    credential["alias"] = saved.get("alias")
                self.save(credential)
                flow["status"] = "done"
            try:
                await self.changed()
            except ValueError, RuntimeError, OSError, httpx.HTTPError:
                flow["catalogPending"] = True
        except asyncio.CancelledError:
            raise
        except (ValueError, RuntimeError, OSError, httpx.HTTPError, TimeoutError) as exc:
            flow.update(
                status="failed",
                error=str(exc)
                if isinstance(exc, ValueError)
                else "OrcaRouter authorization could not complete; check the connection and retry.",
            )
        finally:
            flow["ready"].set()
            if server:
                server.close()
                await server.wait_closed()
