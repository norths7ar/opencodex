"""Native identities and the legacy selector projection used by Codex."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from backend.configuration.models import Channel, GlobalModels

_ALIAS_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ALIAS_RULES = (
    (r"^claude-opus-5", "opus"),
    (r"^claude-sonnet-5", "sonnet"),
    (r"^claude-haiku", "haiku"),
    (r"^gemini-3(?:\.\d+)?-pro", "g3p"),
    (r"^gemini-3(?:\.\d+)?-flash", "g3f"),
    (r"^deepseek-v4\.1", "ds41"),
    (r"^deepseek-v4", "ds4"),
    (r"^deepseek-flash", "dsf"),
    (r"^grok-4", "grok"),
)


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    channel_id: str
    native_id: str

    @property
    def namespaced_id(self) -> str:
        return f"{self.channel_id}/{self.native_id}"

    @property
    def routed_selector(self) -> str:
        return f"{self.channel_id}/{self.native_id.replace('/', '-')}"


@dataclass(frozen=True, slots=True)
class ModelAlias:
    value: str
    source: Literal["user", "builtin"]


def effective_aliases(
    models: GlobalModels, channel: Channel, known_ids: Iterable[str]
) -> dict[str, ModelAlias]:
    known = set(known_ids)
    aliases = {
        model: ModelAlias(alias, "user")
        for model, alias in channel.models.aliases.items()
        if _ALIAS_PATTERN.fullmatch(alias)
    }
    default_aliases = channel.models.default_aliases
    if not (models.default_aliases if default_aliases is None else default_aliases):
        return aliases
    claims: dict[str, list[str]] = {}
    for model in known.difference(aliases):
        basename = model.rsplit("/", 1)[-1]
        for pattern, alias in _ALIAS_RULES:
            if re.search(pattern, model) or re.search(pattern, basename):
                claims.setdefault(alias, []).append(model)
                break
    occupied = {alias.value.lower() for alias in aliases.values()} | {m.lower() for m in known}
    for alias, owners in claims.items():
        if len(owners) == 1 and alias not in occupied:
            aliases[owners[0]] = ModelAlias(alias, "builtin")
    return aliases


def resolve_selector(
    models: GlobalModels, channel: Channel, known_ids: Iterable[str], selector: str
) -> ModelIdentity:
    """Decode a selector's model component, after the caller has chosen its channel."""
    metadata = channel.models.metadata
    known = set(known_ids) | channel.models.aliases.keys() | metadata.capabilities.keys()
    known.update(
        model
        for model, values in metadata.overrides.items()
        if values.model_fields_set & {"displayName", "contextWindow"}
    )
    if channel.models.default:
        known.add(channel.models.default)
    encoded_matches = {
        model for model in known if model.replace("/", "-") == selector.replace("/", "-")
    }
    if len(encoded_matches) > 1:
        raise ValueError("Ambiguous model ID: multiple native models share this Codex selector.")
    if selector in known:
        return ModelIdentity(channel.id, selector)
    matches = {model for model in encoded_matches if model.replace("/", "-") == selector}
    matches.update(
        model
        for model, alias in effective_aliases(models, channel, known).items()
        if alias.value.lower() == selector.lower()
    )
    if len(matches) > 1:
        raise ValueError("Ambiguous model alias.")
    # A discovery snapshot is not a routing allowlist.
    return ModelIdentity(channel.id, next(iter(matches), selector))
