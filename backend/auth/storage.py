"""Windows DPAPI primitives and API credential resolution."""

import base64
import ctypes
import hashlib
import hmac
import json
import math
import os
import secrets
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from dotenv import dotenv_values

from backend.configuration.models import CredentialRecord


class Blob(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(value: bytes, *, decrypt: bool = False) -> bytes:
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise RuntimeError("Windows could not unlock the saved API credential for this user.")
    try:
        return ctypes.string_at(result.data, result.length)
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def resolve_credential(
    record: CredentialRecord, *, environment: Mapping[str, str] | None = None, dotenv_path=None
) -> tuple[str, dict[str, str]]:
    """Resolve a credential without mutating its configuration record."""
    if record.env_var:
        values = os.environ if environment is None else environment
        fallback = dotenv_values(dotenv_path, encoding="utf-8-sig") if dotenv_path else {}
        value = values.get(record.env_var) or fallback.get(record.env_var)
        if not value:
            raise ValueError(f"Credential variable {record.env_var} is not configured.")
        return value, dict(record.headers)
    if record.protected_key:
        return protect(base64.b64decode(record.protected_key), decrypt=True).decode("utf-8"), dict(
            record.headers
        )
    raise ValueError("This provider has no API credential.")


class AccountStorage:
    def __init__(self, directory: Path):
        self.path = directory / "chatgpt-account.json"

    def load(self):
        try:
            try:
                raw = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            blob = json.loads(raw)["protected"]
            credential = json.loads(protect(base64.b64decode(blob), decrypt=True))
            if (
                not isinstance(credential, dict)
                or any(
                    not isinstance(credential.get(field), str) or not credential[field].strip()
                    for field in ("accountId", "access", "generation", "installationId")
                )
                or not isinstance(credential.get("refresh", ""), str)
                or type(credential.get("needsReauth", False)) is not bool
                or type(credential.get("expires")) not in {int, float}
                or not math.isfinite(credential["expires"])
                or not math.isfinite(credential["expires"] * 1000)
            ):
                raise ValueError("Invalid saved account fields.")
            for field in ("email", "alias"):
                if field in credential and not isinstance(credential[field], str):
                    credential[field] = None
            return credential
        except (ValueError, KeyError, TypeError, OverflowError, OSError, RuntimeError) as exc:
            raise ValueError(
                "The saved ChatGPT account could not be unlocked; sign in again."
            ) from exc

    def save(self, credential):
        encoded = base64.b64encode(protect(json.dumps(credential).encode())).decode()
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"protected": encoded}), encoding="utf-8")
        temporary.replace(self.path)

    def installation_id(self, account):
        path = self.path.with_name("chatgpt-installation.key")
        if path.exists():
            secret = protect(path.read_bytes(), decrypt=True)
        else:
            secret = secrets.token_bytes(32)
            path.write_bytes(protect(secret))
        digest = hmac.new(
            secret, ("opencodex-codex-installation-id-v1\0" + account).encode(), hashlib.sha256
        ).digest()
        return str(UUID(bytes=digest[:16], version=4))
