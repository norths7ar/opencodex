"""ChatGPT token parsing and refresh error classification."""

import base64
import json
import math
import secrets
import time

TERMINAL_CODES = {
    "invalid_grant",
    "refresh_token_reused",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "token_invalidated",
    "account_deactivated",
    "deactivated_workspace",
}


class InvalidTokenResponse(ValueError):
    """The token service replied without a usable response."""


def claims(token):
    if not isinstance(token, str):
        return {}
    try:
        part = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except ValueError, IndexError, TypeError:
        return {}


def credentials(payload, previous=None):
    previous = previous or {}
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise InvalidTokenResponse("ChatGPT token response is missing its access token.")
    account, email = None, None
    for token in (payload.get("id_token", ""), access):
        parsed = claims(token)
        auth = parsed.get("https://api.openai.com/auth") or {}
        profile = parsed.get("https://api.openai.com/profile") or {}
        if isinstance(auth, dict):
            account = account or parsed.get("chatgpt_account_id") or auth.get("chatgpt_account_id")
        if isinstance(profile, dict):
            email = email or parsed.get("email") or profile.get("email")
    account = account or previous.get("accountId")
    if not isinstance(account, str) or not account.strip():
        raise InvalidTokenResponse("ChatGPT token response is missing its account identity.")
    if previous.get("accountId") and previous["accountId"] != account:
        raise ValueError("ChatGPT refresh returned a different account; sign in again.")
    expires = payload.get("expires_in", 3600)
    if (
        not isinstance(expires, (int, float))
        or not math.isfinite(expires)
        or not 0 <= expires < 10**9
    ):
        expires = 3600
    return {
        "access": access,
        "refresh": payload.get("refresh_token") or previous.get("refresh", ""),
        "accountId": account,
        "email": email or previous.get("email"),
        "expires": time.time() + expires,
        "generation": secrets.token_hex(16),
        "alias": previous.get("alias"),
        "installationId": previous.get("installationId") or secrets.token_hex(16),
    }


class TokenError(ValueError):
    def __init__(self, status, code=None, *, description=""):
        self.terminal = status in {400, 401, 403} and (
            code in TERMINAL_CODES
            or (
                status == 400
                and code is None
                and any(word in description for word in ("invalidated", "revoked", "expired"))
            )
        )
        super().__init__(
            f"ChatGPT authorization failed: HTTP {status}"
            + (f" ({code})" if self.terminal and code else "")
        )


def token_error(response):
    """Match the original refresh verdict without exposing upstream descriptions."""
    try:
        if len(response.text) <= 16_384:
            payload = response.json()
            if isinstance(payload, dict):
                code = payload.get("error", "")
                description = payload.get("error_description")
                description = description if isinstance(description, str) else ""
                if isinstance(code, dict):
                    message = code.get("message")
                    if isinstance(message, str):
                        description = f"{message} {description}"
                    code = code.get("code", "")
                if isinstance(code, str):
                    return TokenError(
                        response.status_code, code.strip() or None, description=description
                    )
    except ValueError:
        pass
    return TokenError(response.status_code)
