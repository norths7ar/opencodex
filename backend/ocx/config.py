"""Independent API connections and Windows-protected credentials for the Python gateway."""

import base64
import ctypes
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .runtime import PROJECT_ROOT, instance_lock


class Provider(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    name: str = Field(min_length=1, max_length=100)
    baseUrl: str
    apiKeyEnv: str | None = None
    responses: bool = False
    models: list[str] = Field(default_factory=list)
    apiKey: str | None = Field(default=None, exclude=True, repr=False)
    protectedKey: str | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def credential_source(self):
        if self.apiKey and self.apiKeyEnv:
            raise ValueError("Choose either an API key or an environment variable reference.")
        return self

    @field_validator("baseUrl")
    @classmethod
    def base_url(cls, value):
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Use an HTTP or HTTPS API base URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Keep credentials and query parameters out of the base URL.")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote API connections require HTTPS.")
        return value

    @field_validator("apiKeyEnv")
    @classmethod
    def environment_name(cls, value):
        if not value:
            return None
        if not value.isascii() or not value.replace("_", "a").isalnum() or value[0].isdigit():
            raise ValueError("Use an environment variable name, not a credential value.")
        return value

    @field_validator("models")
    @classmethod
    def model_names(cls, values):
        if any(not value.strip() for value in values):
            raise ValueError("Model names cannot be empty.")
        return list(dict.fromkeys(value.strip() for value in values))


class Configuration(BaseModel):
    providers: list[Provider] = Field(default_factory=list)
    defaultProvider: str | None = None
    defaultModel: str | None = None

    @model_validator(mode="after")
    def selection(self):
        ids = [provider.id for provider in self.providers]
        if len(ids) != len(set(ids)):
            raise ValueError("Provider IDs must be unique.")
        if self.defaultProvider and self.defaultProvider not in ids:
            raise ValueError("The default provider must exist.")
        if self.defaultModel and not self.defaultProvider:
            raise ValueError("Select a default provider before a default model.")
        if self.defaultModel:
            selected = next(p for p in self.providers if p.id == self.defaultProvider)
            if selected.models and self.defaultModel not in selected.models:
                raise ValueError("The default model must belong to the selected provider.")
        return self


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


class ConfigStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "config.json"
        directory.mkdir(parents=True, exist_ok=True)
        with instance_lock(directory, "config.lock"):
            key_path = directory / "gateway-key.txt"
            if not key_path.exists():
                key_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
            self.gateway_key = key_path.read_text(encoding="utf-8").strip()

    def load(self) -> Configuration:
        if not self.path.exists():
            return Configuration()
        try:
            return Configuration.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise RuntimeError(
                "Cannot read the saved gateway configuration; no file was overwritten."
            ) from exc

    def credential(self, provider: Provider) -> str:
        if provider.apiKeyEnv:
            environment = dotenv_values(PROJECT_ROOT / ".env", encoding="utf-8-sig")
            value = os.environ.get(provider.apiKeyEnv) or environment.get(provider.apiKeyEnv)
            if not value:
                raise ValueError(f"Credential variable {provider.apiKeyEnv} is not configured.")
            return value
        if provider.protectedKey:
            return protect(base64.b64decode(provider.protectedKey), decrypt=True).decode("utf-8")
        raise ValueError("This provider has no API credential.")

    def public(self, config: Configuration | None = None) -> dict:
        config = config or self.load()
        data = config.model_dump()
        for item, provider in zip(data["providers"], config.providers, strict=True):
            try:
                item["hasKey"] = bool(self.credential(provider))
            except ValueError, RuntimeError:
                item["hasKey"] = False
        return data

    def save(self, config: Configuration) -> dict:
        with instance_lock(self.directory, "config.lock"):
            old = {provider.id: provider for provider in self.load().providers}
            data = config.model_dump()
            for item, provider in zip(data["providers"], config.providers, strict=True):
                saved = old.get(provider.id)
                # Encrypted blobs are server-owned; never accept one from an HTTP payload.
                encrypted = saved.protectedKey if saved else None
                if provider.apiKeyEnv:
                    encrypted = None
                if provider.apiKey:
                    encrypted = base64.b64encode(protect(provider.apiKey.encode("utf-8"))).decode(
                        "ascii"
                    )
                item["protectedKey"] = encrypted
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.path)
        return self.public()

    def provider(self, provider_id: str) -> Provider:
        for provider in self.load().providers:
            if provider.id == provider_id:
                return provider
        raise ValueError("Provider not found.")

    def route(self, model: str | None) -> tuple[Provider, str]:
        config = self.load()
        model = model or config.defaultModel
        if not model:
            raise ValueError("Select a model first.")
        if "/" in model and model.split("/", 1)[0] in {p.id for p in config.providers}:
            provider_id, model = model.split("/", 1)
        else:
            provider_id = config.defaultProvider
        if not provider_id:
            raise ValueError("Use provider/model or select a default provider.")
        provider = next((p for p in config.providers if p.id == provider_id), None)
        if provider is None:
            raise ValueError("Provider not found.")
        if provider.models and model not in provider.models:
            raise ValueError("The model is not enabled for this provider.")
        return provider, model
