"""Explicit API credential edits; encryption and header validation have one owner."""

import base64
import re

import httpx

from backend.configuration.models import CredentialRecord

from .storage import protect


def apply_credential_change(edit, previous: CredentialRecord) -> CredentialRecord:
    action = edit.credential
    changes = {}
    if action.action == "set_key":
        value = action.value.get_secret_value().strip().encode()
        changes.update(protected_key=base64.b64encode(protect(value)).decode(), env_var=None)
    elif action.action == "set_environment":
        changes.update(protected_key=None, env_var=action.variable)
    elif action.action == "clear":
        changes.update(protected_key=None, env_var=None)
    if edit.headers is not None:
        headers = {key: value.get_secret_value() for key, value in edit.headers.items()}
        for name, value in headers.items():
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or any(
                ord(char) < 32 or ord(char) == 127 for char in value
            ):
                raise ValueError("Invalid custom header name or value.")
        httpx.Headers(headers)
        changes["headers"] = headers
    return previous.model_copy(update=changes)
