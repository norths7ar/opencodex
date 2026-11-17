"""Gateway-owned account grants survive cancellation, refresh, and account changes."""

import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager, suppress
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from backend.auth.chatgpt import ChatGPTAuth
from backend.auth.tokens import TokenError, credentials

pytestmark = pytest.mark.anyio


def token_payload(account="account-a", *, expiry=3600):
    claims = (
        base64.urlsafe_b64encode(
            json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": account}}).encode()
        )
        .decode()
        .rstrip("=")
    )
    return {
        "access_token": f"test.{claims}.signature",
        "refresh_token": f"refresh-{account}",
        "expires_in": expiry,
    }


@asynccontextmanager
async def account(sandbox, handler):
    auth = ChatGPTAuth(sandbox)
    await auth.client.aclose()
    auth.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    try:
        yield auth
    finally:
        await auth.close()


class CallbackListener:
    """Replace only the OS listener; callback URL/state processing remains production code."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


@pytest.fixture
def browser_listener(monkeypatch):
    async def start_listener(*_, **__):
        return CallbackListener()

    monkeypatch.setattr(asyncio, "start_server", start_listener)


async def finish_browser(auth, *, code="accepted"):
    hint = await auth.start(open_browser=False)
    state = parse_qs(urlsplit(hint["url"]).query)["state"][0]
    auth.submit(f"http://localhost:1455/auth/callback?state={state}&code={code}")
    async with asyncio.timeout(3):
        await auth.flow["task"]
    return hint


async def test_browser_cancel_then_login_keeps_callback_and_account_boundaries(
    sandbox, browser_listener
):
    exchanges = []

    def upstream(request):
        assert request.url.path == "/oauth/token"
        exchanges.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json=token_payload())

    async with account(sandbox, upstream) as auth:
        hint = await auth.start(open_browser=False)
        state = parse_qs(urlsplit(hint["url"]).query)["state"][0]
        with pytest.raises(ValueError, match="different login"):
            auth.submit("http://localhost:1455/auth/callback?state=foreign&code=wrong")
        await auth.cancel()
        assert auth.load() is None and not exchanges
        with pytest.raises(ValueError, match="No browser login"):
            auth.submit(f"http://localhost:1455/auth/callback?state={state}&code=late")

        completed = await finish_browser(auth)
        assert completed["browserLaunch"] == "skipped"
        assert auth.status()["activeAccountId"] == "account-a"
        assert auth.status()["loggedIn"]
        assert len(exchanges) == 1
        assert exchanges[0]["grant_type"] == ["authorization_code"]
        assert exchanges[0]["code"] == ["accepted"]
        assert exchanges[0]["code_verifier"]
        assert auth.load()["refresh"] == "refresh-account-a"
        assert "refresh-account-a" not in auth.path.read_text()
        await auth.logout()
        assert not auth.status()["loggedIn"] and auth.load() is None


async def test_device_login_cancellation_then_retry_completes(sandbox):
    waiting = asyncio.Event()
    attempts = 0

    async def upstream(request):
        nonlocal attempts
        if request.url.path.endswith("/usercode"):
            attempts += 1
            return httpx.Response(
                200, json={"device_auth_id": f"device-{attempts}", "user_code": "TEST-CODE"}
            )
        if request.url.path.endswith("/deviceauth/token"):
            if attempts == 1:
                waiting.set()
                await asyncio.Event().wait()
            return httpx.Response(
                200, json={"authorization_code": "device-grant", "code_verifier": "verifier"}
            )
        assert request.url.path == "/oauth/token"
        return httpx.Response(200, json=token_payload("device-account"))

    async with account(sandbox, upstream) as auth:
        hint = await auth.start(device=True)
        assert hint["deviceCode"] == "TEST-CODE"
        async with asyncio.timeout(3):
            await waiting.wait()
            await auth.cancel()
        assert auth.load() is None
        await auth.start(device=True)
        async with asyncio.timeout(3):
            await auth.flow["task"]
        assert auth.status()["activeAccountId"] == "device-account"
        assert auth.status()["done"] and not auth.status()["pending"]


async def test_temporary_refresh_failure_recovers_and_concurrent_requests_share_refresh(sandbox):
    unavailable = True
    calls = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def upstream(request):
        calls.append(json.loads(request.content))
        if unavailable:
            return httpx.Response(503, json={"error": "temporarily_unavailable"})
        entered.set()
        await release.wait()
        return httpx.Response(200, json=token_payload())

    async with account(sandbox, upstream) as auth:
        auth.save(credentials(token_payload(expiry=0)))
        with pytest.raises(TokenError) as failure:
            await auth.token()
        assert not failure.value.terminal
        assert not auth.status()["needsReauth"]
        assert auth.load()["refresh"] == "refresh-account-a"

        unavailable = False
        first = asyncio.create_task(auth.token())
        async with asyncio.timeout(3):
            await entered.wait()
            followers = [asyncio.create_task(auth.token()) for _ in range(4)]
            release.set()
            renewed = await asyncio.gather(first, *followers)
        assert len(calls) == 2  # One failed refresh, one shared successful refresh.
        assert len({item["generation"] for item in renewed}) == 1
        assert all(item["expires"] > time.time() for item in renewed)
        assert auth.status()["loggedIn"]


async def test_cancelled_old_login_cannot_replace_new_account(sandbox, browser_listener):
    entered = asyncio.Event()
    release = asyncio.Event()
    delayed = None

    async def upstream(request):
        nonlocal delayed
        code = parse_qs(request.content.decode())["code"][0]
        if code == "old":
            delayed = asyncio.current_task()
            entered.set()
            # Model an external completion that wins a cancellation race.
            with suppress(asyncio.CancelledError):
                await release.wait()
            return httpx.Response(200, json=token_payload("old-account"))
        return httpx.Response(200, json=token_payload("new-account"))

    async with account(sandbox, upstream) as auth:
        hint = await auth.start(open_browser=False)
        state = parse_qs(urlsplit(hint["url"]).query)["state"][0]
        auth.submit(f"http://localhost:1455/auth/callback?state={state}&code=old")
        async with asyncio.timeout(3):
            await entered.wait()
            await auth.logout()
            await finish_browser(auth, code="new")
        release.set()
        assert delayed.done()
        assert auth.status()["activeAccountId"] == "new-account"
        assert len(auth.status()["accounts"]) == 1
        with pytest.raises(ValueError, match="account changed"):
            await auth.token(expected_account_id="old-account")


async def test_account_switch_continues_text_without_replaying_old_private_state(
    open_lite, register_channel
):
    sent = []

    def upstream(request):
        body = json.loads(request.content)
        sent.append((dict(request.headers), body))
        value = {
            "id": f"response-{len(sent)}",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "id": "private-reasoning",
                    "type": "reasoning",
                    "encrypted_content": "secret-a",
                    "summary": [],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Remember blue."}],
                },
            ],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }
        return httpx.Response(
            200,
            text="data: " + json.dumps({"type": "response.completed", "response": value}) + "\n\n",
            headers={"content-type": "text/event-stream", "x-codex-turn-state": "raw-state-a"},
        )

    async with open_lite(upstream) as app:
        await register_channel(
            app.client,
            identity="subscription",
            protocol="openai-responses",
            service_id="chatgpt",
            auth_mode="oauth",
            endpoints={"base_url": "https://chatgpt.com/backend-api/codex"},
            oauth_binding={"service_id": "chatgpt", "account_ref": "chatgpt"},
        )
        auth = app.services.auth
        auth.save(credentials(token_payload("account-a")))
        first = await app.client.post(
            "/v1/responses",
            json={"model": "subscription/model-a", "input": "Remember blue."},
            headers={"thread-id": "conversation"},
        )
        assert first.status_code == 200, first.text
        handle = first.headers["x-codex-turn-state"]
        assert handle != "raw-state-a"
        await auth.logout()
        auth.save(credentials(token_payload("account-b")))
        continued = await app.client.post(
            "/v1/responses",
            json={
                "model": "subscription/model-a",
                "previous_response_id": first.json()["id"],
                "input": "What color?",
            },
            headers={"thread-id": "conversation", "x-codex-turn-state": handle},
        )
        assert continued.status_code == 200, continued.text
        headers, body = sent[-1]
        assert headers["chatgpt-account-id"] == "account-b"
        assert "x-codex-turn-state" not in headers
        assert "secret-a" not in json.dumps(body)
        assert "private-reasoning" not in json.dumps(body)
        assert "previous_response_id" not in body
        assert "Remember blue." in json.dumps(body) and "What color?" in json.dumps(body)
        assert auth.status()["activeAccountId"] == "account-b"
        # A configured API-style price must not turn subscription tokens into API charges.
        overlay = await app.client.put(
            "/api/model-settings",
            json={
                "model": {"channel_id": "subscription", "model_id": "model-a"},
                "changes": {},
                "cost": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0},
            },
        )
        assert overlay.status_code == 200, overlay.text
        logs = (await app.client.get("/api/logs?provider=subscription")).json()["logs"]
        assert len(logs) == 2
        assert all(row["usageStatus"] == "reported" and row["totalTokens"] == 6 for row in logs)
        assert all(
            row["displayMetrics"]["cost"] == {"kind": "unavailable", "reason": "price_unmatched"}
            for row in logs
        )
        summary = (await app.client.get("/api/usage?range=all&provider=subscription")).json()[
            "summary"
        ]
        assert summary["requests"] == 2 and summary["totalTokens"] == 12
        assert summary["pricedRequests"] == 0 and summary["unpricedRequests"] == 2


async def test_login_validation_failure_keeps_previous_account_until_retry_succeeds(
    open_lite, register_channel, browser_listener
):
    validation_unavailable = True
    validated_accounts = []

    def upstream(request):
        if request.method == "GET":
            assert request.url.path.endswith("/models")
            if validation_unavailable:
                return httpx.Response(503)
            return httpx.Response(200, json={"models": [{"slug": "model-a"}]})
        validated_accounts.append(request.headers["chatgpt-account-id"])
        return httpx.Response(
            200,
            text='data: {"type":"response.completed","response":{"status":"completed"}}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    async with open_lite(upstream) as app:
        await register_channel(
            app.client,
            identity="subscription",
            protocol="openai-responses",
            service_id="chatgpt",
            auth_mode="oauth",
            endpoints={"base_url": "https://chatgpt.com/backend-api/codex"},
            oauth_binding={"service_id": "chatgpt", "account_ref": "chatgpt"},
        )
        auth = app.services.auth
        auth.save(credentials(token_payload("previous-account")))
        await auth.client.aclose()
        auth.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=token_payload("candidate-account"))
            ),
            trust_env=False,
        )
        await finish_browser(auth)
        assert auth.flow["status"] == "failed"
        assert auth.status()["activeAccountId"] == "previous-account"
        assert not validated_accounts
        validation_unavailable = False
        await finish_browser(auth)
        assert auth.status()["activeAccountId"] == "candidate-account"
        assert auth.status()["done"]
        assert validated_accounts == ["candidate-account"]
        assert auth.load()["validatedAt"] > 0
