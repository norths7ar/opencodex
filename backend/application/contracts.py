"""Management wire models. Secret storage models never form response schemas."""

from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator

from backend.configuration.models import (
    Channel,
    ChannelId,
    CredentialRecord,
    ModelMetadata,
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


class ChannelDetail(Schema):
    """Editable channel settings; credential values are submitted separately."""

    channel: Channel
    credential: CredentialStatus
    has_custom_headers: bool
    revision: int
    discovery: DiscoveryState


class DiscoveryState(Schema):
    state: Literal["not_checked", "ready", "refreshing", "failed", "unsupported"]
    last_success_at: str | None = None
    last_attempt_at: str | None = None
    error: ApiError | None = None
    stale_default_model: str | None = None
    stale_aliases: dict[str, str] = Field(default_factory=dict)


class ModelIdentity(Schema):
    channel_id: ChannelId
    model_id: str


class ModelOverrideUpdate(Schema):
    """Omitted properties stay unchanged; null clears an override; [] is an explicit value."""

    model: ModelIdentity
    changes: ModelMetadata
    cost: dict | None = None
    new_model_id: str | None = None


class ChannelConfigurationEdit(Schema):
    revision: int
    channels: list[Channel]
    default_channel_id: ChannelId | None = None
    default_model_id: str | None = None


class ChannelList(Schema):
    revision: int
    default_channel_id: ChannelId | None
    default_model_id: str | None
    channels: list[ChannelDetail]


class ClientApplicationOutcome(Schema):
    ok: bool
    state: str
    message: str | None = None
    reason: str | None = None


class ApplicationStatus(Schema):
    status: Literal["applied", "pending", "skipped", "failed"]
    clients: dict[str, ClientApplicationOutcome] | None = None
    message: str | None = None


class ChannelSaved(ChannelDetail):
    saved: Literal[True] = True
    application: ApplicationStatus


class ChannelDefaultSaved(ChannelList):
    saved: Literal[True] = True
    application: ApplicationStatus


class ChannelConfigurationSaved(ChannelConfigurationEdit):
    saved: Literal[True] = True
    application: ApplicationStatus
