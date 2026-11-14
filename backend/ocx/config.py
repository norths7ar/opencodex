"""Independent API connections and Windows-protected credentials for the Python gateway."""

import base64
import ctypes
import json
import os
from pathlib import Path
from threading import RLock
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .model_policy import resolve_model_alias
from .runtime import PROJECT_ROOT, instance_lock
from .spend_policy import SpendPolicy
from .subscription_channels import CHANNELS


class Provider(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    baseUrl: str
    apiKeyEnv: str | None = None
    responses: bool = False
    adapter: Literal["openai-chat", "openai-responses", "anthropic"] = "openai-chat"
    authMode: Literal["key", "local", "oauth"] = "key"
    apiKeyTransport: Literal["bearer", "x-api-key"] | None = None
    responsesPath: str | None = None
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    disabled: bool = False
    defaultModel: str | None = None
    selectedModels: list[str] = Field(default_factory=list)
    alias: str | None = None
    modelAliases: dict[str, str] = Field(default_factory=dict)
    liveModels: bool = True
    models: list[str] = Field(default_factory=list)
    apiKey: str | None = Field(default=None, exclude=True, repr=False)
    protectedKey: str | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def credential_source(self):
        channel = CHANNELS.get(self.id)
        if self.authMode == "oauth" and (
            not channel or self.adapter != channel["adapter"] or self.baseUrl != channel["baseUrl"]
        ):
            raise ValueError("Subscription authorization uses its canonical channel and endpoint.")
        if self.apiKey and self.apiKeyEnv:
            raise ValueError("Choose either an API key or an environment variable reference.")
        return self

    @model_validator(mode="before")
    @classmethod
    def legacy_protocol(cls, value):
        if isinstance(value, dict) and "adapter" not in value and value.get("responses"):
            value = {**value, "adapter": "openai-responses"}
        return value

    @field_validator("baseUrl")
    @classmethod
    def base_url(cls, value):
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Use an HTTP or HTTPS API base URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Keep credentials and query parameters out of the base URL.")
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
    model_config = ConfigDict(extra="allow")
    providers: list[Provider] = Field(default_factory=list)
    defaultProvider: str | None = None
    defaultModel: str | None = None
    disabledModels: list[str] = Field(default_factory=list)
    customModels: list[dict] = Field(default_factory=list)
    defaultModelAliases: bool = False
    spend: SpendPolicy | None = None

    @model_validator(mode="after")
    def selection(self):
        ids = [provider.id for provider in self.providers]
        if len(ids) != len(set(ids)):
            raise ValueError("Provider IDs must be unique.")
        if self.defaultProvider and self.defaultProvider not in ids:
            raise ValueError("The default provider must exist.")
        if self.defaultModel and not self.defaultProvider:
            raise ValueError("Select a default provider before a default model.")
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
        self.mutation_lock = RLock()
        self.discovered_models: dict[str, list[str]] = {}
        self.directory = directory
        self.path = directory / "config.json"
        directory.mkdir(parents=True, exist_ok=True)

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
        if provider.authMode == "oauth":
            raise ValueError("Subscription credentials are managed by the gateway login flow.")
        if provider.authMode == "local":
            return ""
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
            item.pop("headers", None)
            try:
                item["hasKey"] = bool(self.credential(provider))
            except ValueError, RuntimeError:
                item["hasKey"] = False
        return data

    def save(self, config: Configuration, *, clear_keys: set[str] | None = None) -> dict:
        with instance_lock(self.directory, "config.lock"):
            old = {provider.id: provider for provider in self.load().providers}
            data = config.model_dump()
            for item, provider in zip(data["providers"], config.providers, strict=True):
                saved = old.get(provider.id)
                # Encrypted blobs are server-owned; never accept one from an HTTP payload.
                encrypted = saved.protectedKey if saved else None
                if provider.id in (clear_keys or set()):
                    encrypted = None
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
        default = next((p for p in config.providers if p.id == config.defaultProvider), None)
        model = model or (default.defaultModel if default else None) or config.defaultModel
        if not model:
            raise ValueError("Select a model first.")
        by_name = {name: p.id for p in config.providers for name in (p.id, p.alias) if name}
        if "/" in model and model.split("/", 1)[0] in by_name:
            provider_name, model = model.split("/", 1)
            provider_id = by_name[provider_name]
        elif "/" not in model and (
            model.startswith(("gpt-", "o1-", "o3-", "o4-")) or model == "codex-auto-review"
        ):
            # Native Codex selectors keep the original canonical-channel identity.
            provider_id = "openai"
            if not any(p.id == provider_id and not p.disabled for p in config.providers):
                raise ValueError(f"Model {model} requires the enabled openai channel.")
        else:
            provider_id = config.defaultProvider
        if not provider_id:
            raise ValueError("Use provider/model or select a default provider.")
        provider = next((p for p in config.providers if p.id == provider_id), None)
        if provider is None:
            raise ValueError("Provider not found.")
        if provider.disabled:
            raise ValueError("This provider is disabled.")
        known = set(
            self.discovered_models.get(provider.id, [] if provider.liveModels else provider.models)
        ) | {m["modelId"] for m in config.customModels if m["provider"] == provider.id}
        # Explicit routing declarations remain decodable before discovery, including
        # when the upstream is temporarily unavailable. They do not populate the catalog.
        known.update(provider.modelAliases)
        if provider.defaultModel:
            known.add(provider.defaultModel)
        for field in ("modelDisplayNames", "modelContextWindows", "modelCapabilities"):
            known.update(getattr(provider, field, {}))
        encoded_matches = {
            native for native in known if native.replace("/", "-") == model.replace("/", "-")
        }
        if len(encoded_matches) > 1:
            raise ValueError(
                "Ambiguous model ID: multiple native models share this Codex selector."
            )
        if model not in known:
            aliases = [native for native in known if native.replace("/", "-") == model]
            alias_target = resolve_model_alias(config, provider, known, model)
            if alias_target:
                aliases.append(alias_target)
            if len(set(aliases)) == 1:
                model = aliases[0]
            elif len(set(aliases)) > 1:
                raise ValueError("Ambiguous model alias.")
        # selectedModels controls catalog visibility, not permission to explicitly route a model.
        return provider, model
