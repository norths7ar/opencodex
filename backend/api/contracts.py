"""Management wire models. Secret storage models never form response schemas."""

from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator

from backend.configuration.models import (
    AuthMode,
    Channel,
    ChannelId,
    CredentialRecord,
    Endpoints,
    ModelMetadata,
    Protocol,
    Schema,
)


class ApiError(Schema):
    code: str
    message: str
    field: str | None = None
    retryable: bool = False


class KeepCredential(Schema):
    action: Literal["keep"] = "keep"


class SetApiKey(Schema):
    action: Literal["set_key"]
    value: SecretStr

    @field_validator("value")
    @classmethod
    def nonempty_key(cls, value):
        if not value.get_secret_value().strip():
            raise ValueError("An API key is required.")
        return value


class SetEnvironmentCredential(Schema):
    action: Literal["set_environment"]
    variable: str

    @field_validator("variable")
    @classmethod
    def environment_name(cls, value):
        result = CredentialRecord(env_var=value).env_var
        if result is None:
            raise ValueError("An environment variable is required.")
        return result


class ClearCredential(Schema):
    action: Literal["clear"]


CredentialChange = Annotated[
    KeepCredential | SetApiKey | SetEnvironmentCredential | ClearCredential,
    Field(discriminator="action"),
]


class SaveChannel(Schema):
    channel: Channel
    credential: CredentialChange = Field(default_factory=KeepCredential)
    # None preserves existing headers; an empty mapping explicitly clears them.
    headers: dict[str, SecretStr] | None = None

    @model_validator(mode="after")
    def credential_mode(self):
        if self.channel.auth_mode != "key" and self.credential.action != "keep":
            raise ValueError("API credential changes apply to API-key channels.")
        return self


class CredentialStatus(Schema):
    state: Literal["unknown", "configured", "missing", "reauth_required", "unavailable"]
    label: str | None = None


class ChannelView(Schema):
    """Compact channel summary for lists and overview cards."""

    id: ChannelId
    service_id: ChannelId
    name: str
    protocol: Protocol
    auth_mode: AuthMode
    endpoints: Endpoints
    disabled: bool
    alias: str | None = None
    credential: CredentialStatus
    has_custom_headers: bool

    @classmethod
    def from_channel(cls, channel: Channel, credential: CredentialStatus, *, has_headers=False):
        return cls(
            id=channel.id,
            service_id=channel.service_id,
            name=channel.name,
            protocol=channel.protocol,
            auth_mode=channel.auth_mode,
            endpoints=channel.endpoints,
            disabled=channel.disabled,
            alias=channel.alias,
            credential=credential,
            has_custom_headers=has_headers,
        )


class ChannelDetail(Schema):
    """Editable channel settings; credential values are submitted separately."""

    channel: Channel
    credential: CredentialStatus
    has_custom_headers: bool


class DiscoveryState(Schema):
    state: Literal["not_checked", "ready", "refreshing", "failed", "unsupported"]
    last_success_at: str | None = None
    last_attempt_at: str | None = None
    error: ApiError | None = None


class ModelIdentity(Schema):
    channel_id: ChannelId
    model_id: str


class RefreshCatalog(Schema):
    channel_id: ChannelId


class ModelSelection(Schema):
    model: ModelIdentity
    visible: bool


class ModelOverrideUpdate(Schema):
    """Omitted properties stay unchanged; null clears an override; [] is an explicit value."""

    model: ModelIdentity
    changes: ModelMetadata


class ApplyResult(Schema):
    saved: bool
    application: Literal["applied", "pending", "not_connected"]
    error: ApiError | None = None


class ClientState(Schema):
    client_id: Literal["codex", "dsh"]
    desired_enabled: bool
    application: Literal["applied", "pending", "not_connected", "conflict"]
    config_path: str | None = None
    error: ApiError | None = None


class ServiceState(Schema):
    gateway: Literal["running", "stopped", "starting", "unknown"]
    installed: bool
    startup_enabled: bool
    console_url: str | None = None
    error: ApiError | None = None


class CatalogChanged(Schema):
    type: Literal["catalog_changed"] = "catalog_changed"
    channel_id: ChannelId
    revision: str


class CredentialChanged(Schema):
    type: Literal["credential_changed"] = "credential_changed"
    channel_id: ChannelId
    status: CredentialStatus


ManagementEvent = Annotated[CatalogChanged | CredentialChanged, Field(discriminator="type")]
