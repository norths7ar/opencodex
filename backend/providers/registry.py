"""Small catalog of supported services and their known destinations."""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from backend.configuration.models import AuthMode, Channel, Protocol

ServiceId = Literal["openai", "chatgpt", "deepseek", "mimo", "opencode-zen", "opencode-go"]


@dataclass(frozen=True, slots=True)
class Destination:
    """One service connection; endpoint defaults may differ across variants."""

    id: str
    base_url: str
    default_protocol: Protocol
    discovery: str | None = None
    chat: str | None = None
    responses: str | None = None
    messages: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    id: ServiceId
    name: str
    auth_mode: AuthMode
    destinations: tuple[Destination, ...]


@dataclass(frozen=True, slots=True)
class MatchedService:
    service: ServiceDefinition
    destination: Destination


_OPENAI = "https://api.openai.com"
_CHATGPT = "https://chatgpt.com/backend-api/codex"
_DEEPSEEK = "https://api.deepseek.com"
_MIMO = "https://api.xiaomimimo.com"
_MIMO_PLAN = "https://token-plan-cn.xiaomimimo.com"
_ZEN = "https://opencode.ai/zen/v1"
_GO = "https://opencode.ai/zen/go/v1"
_GO_ALIAS = "https://opencode.ai/inference/go/v1"

SERVICES: tuple[ServiceDefinition, ...] = (
    ServiceDefinition(
        "openai",
        "OpenAI API",
        "key",
        (
            Destination("api", f"{_OPENAI}/v1", "openai-responses"),
            Destination(
                "api-root",
                _OPENAI,
                "openai-responses",
                discovery=f"{_OPENAI}/v1/models",
                chat=f"{_OPENAI}/v1/chat/completions",
            ),
        ),
    ),
    ServiceDefinition(
        "chatgpt",
        "ChatGPT",
        "oauth",
        (
            Destination(
                "codex",
                _CHATGPT,
                "openai-responses",
                discovery=f"{_CHATGPT}/models",
                chat=f"{_CHATGPT}/chat/completions",
                responses=f"{_CHATGPT}/responses",
                messages=f"{_CHATGPT}/messages",
            ),
        ),
    ),
    ServiceDefinition(
        "deepseek",
        "DeepSeek API",
        "key",
        (
            # DeepSeek's catalog covers both OpenAI and Anthropic at the origin's /models.
            Destination(
                "api",
                _DEEPSEEK,
                "openai-chat",
                discovery=f"{_DEEPSEEK}/models",
                responses=f"{_DEEPSEEK}/responses",
            ),
            Destination(
                "api-v1",
                f"{_DEEPSEEK}/v1",
                "openai-chat",
                discovery=f"{_DEEPSEEK}/models",
                responses=f"{_DEEPSEEK}/responses",
            ),
            Destination(
                "anthropic",
                f"{_DEEPSEEK}/anthropic",
                "anthropic",
                discovery=f"{_DEEPSEEK}/models",
                messages=f"{_DEEPSEEK}/anthropic/v1/messages",
            ),
        ),
    ),
    ServiceDefinition(
        "mimo",
        "MiMo API",
        "key",
        (
            Destination("api", f"{_MIMO}/v1", "openai-chat", discovery=f"{_MIMO}/v1/models"),
            Destination(
                "anthropic",
                f"{_MIMO}/anthropic",
                "anthropic",
                discovery=f"{_MIMO}/v1/models",
                messages=f"{_MIMO}/anthropic/v1/messages",
            ),
            # The upstream preset defaults to Chat; explicit Responses remains selectable.
            Destination(
                "token-plan",
                f"{_MIMO_PLAN}/v1",
                "openai-chat",
                discovery=f"{_MIMO_PLAN}/v1/models",
            ),
            Destination(
                "token-plan-anthropic",
                f"{_MIMO_PLAN}/anthropic",
                "anthropic",
                discovery=f"{_MIMO_PLAN}/v1/models",
                messages=f"{_MIMO_PLAN}/anthropic/v1/messages",
            ),
        ),
    ),
    ServiceDefinition(
        "opencode-zen",
        "OpenCode Zen",
        "key",
        (Destination("zen", _ZEN, "openai-chat", discovery=f"{_ZEN}/models"),),
    ),
    ServiceDefinition(
        "opencode-go",
        "OpenCode Go",
        "key",
        (
            Destination("go", _GO, "openai-chat"),
            Destination("inference", _GO_ALIAS, "openai-chat"),
        ),
    ),
)


def _destination_key(base_url: str) -> tuple[str, str, int, str] | None:
    try:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return None
        port = parsed.port
        return (
            parsed.scheme,
            parsed.hostname,
            443 if port is None else port,
            parsed.path.rstrip("/"),
        )
    except ValueError:
        return None


def match_destination(channel: Channel) -> MatchedService | None:
    """Recognize a known base and auth mode, regardless of user-defined channel IDs."""

    key = _destination_key(channel.endpoints.base_url)
    if key is None:
        return None
    for service in SERVICES:
        if service.auth_mode != channel.auth_mode:
            continue
        for destination in service.destinations:
            if _destination_key(destination.base_url) == key:
                return MatchedService(service, destination)
    return None
