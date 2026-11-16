"""Native model identities and the external routing selector projection."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from backend.configuration.model_identity import colliding_slugs, legacy_slug, models_by_legacy_slug
from backend.configuration.models import Channel, GlobalModels

_ALIAS_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ENCODED_PREFIX = "~ocx-id~"
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
    def legacy_slug(self) -> str:
        return legacy_slug(self.native_id)

    @property
    def encoded_selector(self) -> str:
        return f"{self.channel_id}/{_ENCODED_PREFIX}{self.native_id.encode('utf-8').hex()}"


@dataclass(frozen=True, slots=True)
class ModelAlias:
    value: str
    source: Literal["user", "builtin"]


def legacy_disabled_candidates(
    models: GlobalModels, channel: Channel, known_ids: Iterable[str] = ()
) -> dict[str, set[str]]:
    """Keep ambiguous historical selectors without assigning them to either model."""
    if not models.legacy_disabled:
        return {}
    prefix = channel.id + "/"
    pairs = models_by_legacy_slug(models, channel, known_ids)
    historical_collisions = models.legacy_selector_collisions.get(channel.id, ())
    result = {}
    for item in models.legacy_disabled:
        if item.startswith(prefix):
            native = item[len(prefix) :]
            candidates = pairs.get(legacy_slug(native), set())
            if "/" in native:
                candidates = candidates & {native}
            elif native in historical_collisions and len(candidates) < 2:
                # An old ambiguous spelling never becomes a claim on a survivor.
                result[item] = set()
                continue
            if candidates:
                result[item] = candidates
    return result


def disabled_model_ids(
    models: GlobalModels, channel: Channel, known_ids: Iterable[str] = ()
) -> set[str]:
    prefix = channel.id + "/"
    disabled = {item[len(prefix) :] for item in models.disabled if item.startswith(prefix)}
    for candidates in legacy_disabled_candidates(models, channel, known_ids).values():
        if len(candidates) == 1:
            disabled.update(candidates)
    return disabled


def projected_selector(identity: ModelIdentity, collisions: set[str]) -> str:
    if identity.legacy_slug in collisions or identity.legacy_slug.startswith(_ENCODED_PREFIX):
        return identity.encoded_selector
    return f"{identity.channel_id}/{identity.legacy_slug}"


def decode_model_slug(slug: str) -> str | None:
    if not slug.startswith(_ENCODED_PREFIX):
        return None
    payload = slug[len(_ENCODED_PREFIX) :]
    if not payload or len(payload) % 2 or not re.fullmatch(r"[a-f0-9]+", payload):
        raise ValueError("Invalid encoded model ID.")
    try:
        return bytes.fromhex(payload).decode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid encoded model ID.") from None


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
    if (decoded := decode_model_slug(selector)) is not None:
        return ModelIdentity(channel.id, decoded)
    metadata = channel.models.metadata
    known = set(known_ids) | channel.models.aliases.keys() | metadata.capabilities.keys()
    known.update(
        model
        for model, values in metadata.overrides.items()
        if values.model_fields_set & {"displayName", "contextWindow"}
    )
    if channel.models.default:
        known.add(channel.models.default)
    if "/" in selector and selector in known:
        return ModelIdentity(channel.id, selector)
    if selector in colliding_slugs(models, channel, known):
        raise ValueError("Ambiguous model ID; use a selector from the current model catalog.")
    if selector in known:
        return ModelIdentity(channel.id, selector)
    matches = {model for model in known if legacy_slug(model) == selector}
    matches.update(
        model
        for model, alias in effective_aliases(models, channel, known).items()
        if alias.value.lower() == selector.lower()
    )
    if len(matches) > 1:
        raise ValueError("Ambiguous model alias.")
    # A discovery snapshot is not a routing allowlist.
    return ModelIdentity(channel.id, next(iter(matches), selector))
