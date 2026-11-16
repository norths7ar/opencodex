"""Original spend section; pool is the independent provider channel in Lite."""

from pydantic import BaseModel, ConfigDict, Field


class SpendLimit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    maxTokens: int | None = Field(default=None, gt=0, le=(1 << 53) - 1)


class SpendPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    root: SpendLimit = Field(default_factory=SpendLimit)
    identity: SpendLimit = Field(default_factory=SpendLimit)
    pool: SpendLimit = Field(default_factory=SpendLimit)
    retentionDays: int = Field(default=7, ge=1, le=365)


def current_policy(loader):
    config = loader()
    value = config.features.request.spend if config is not None else None
    return SpendPolicy.model_validate(value or {})
