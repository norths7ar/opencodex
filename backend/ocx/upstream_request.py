"""Preparation of provider-specific upstream requests."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .anthropic_policy import apply_anthropic_policy
from .anthropic_transport import anthropic_headers, prepare_anthropic_body
from .chatgpt_transport import subscription_body, subscription_headers, subscription_url
from .config import Provider
from .protocols import from_chat, to_chat

WIRES = {
    "openai-chat": "chat/completions",
    "openai-responses": "responses",
    "anthropic": "messages",
}


@dataclass
class PreparedRequest:
    address: str
    headers: dict
    body: dict
    protocol: str
    response_mode: str


class UpstreamPreparation:
    def __init__(self, store, registry, reasoning):
        self.store = store
        self.registry = registry
        self.reasoning = reasoning

    @staticmethod
    def is_chatgpt(provider):
        return provider.authMode == "oauth" and provider.id == "openai"

    def oauth_headers(self, provider, credential, incoming=None):
        if provider.id in {"kimi", "kimi-responses"}:
            headers = {
                key: value
                for key, value in provider.headers.items()
                if key.lower() not in {"authorization", "x-api-key"}
            }
            return {
                **headers,
                "Content-Type": "application/json",
                "Authorization": f"Bearer {credential['access']}",
            }
        if provider.adapter != "anthropic":
            return subscription_headers(provider, credential, incoming)
        # Internal workflow/admission handles are local, not upstream request headers.
        native = {
            key: value
            for key, value in (incoming or {}).items()
            if key.lower().startswith("anthropic-")
        }
        native.update(provider.headers)
        session = next(
            (
                incoming.get(key)
                for key in ("x-claude-code-session-id", "session_id", "session-id", "thread-id")
                if incoming and incoming.get(key)
            ),
            None,
        )
        return anthropic_headers(credential["access"], native, session)

    def headers(self, provider: Provider, incoming=None) -> dict:
        headers = {"Content-Type": "application/json"}
        credential = self.store.credential(provider) if provider.authMode != "local" else ""
        if provider.adapter == "anthropic":
            headers["anthropic-version"] = (incoming or {}).get("anthropic-version", "2023-06-01")
            if incoming and incoming.get("anthropic-beta"):
                headers["anthropic-beta"] = incoming["anthropic-beta"]
        if credential:
            if provider.apiKeyTransport == "x-api-key" or (
                provider.adapter == "anthropic" and provider.apiKeyTransport != "bearer"
            ):
                headers["x-api-key"] = credential
            else:
                headers["Authorization"] = f"Bearer {credential}"
        headers.update(provider.headers)
        return headers

    def url(self, provider, protocol):
        if self.is_chatgpt(provider):
            return subscription_url(protocol)
        base = provider.baseUrl.rstrip("/")
        if protocol.startswith("responses"):
            if provider.responsesPath:
                url = base + provider.responsesPath
            else:
                root = re.sub(r"/responses$", "", base)
                root = re.sub(r"/v1$", "", root)
                url = root + "/v1/responses"
            return url + ("/compact" if protocol.endswith("/compact") else "")
        if protocol == "chat/completions":
            path = getattr(provider, "chatCompletionsPath", None)
            return (
                base + path
                if path
                else re.sub(r"/chat/completions$", "", base) + "/chat/completions"
            )
        if protocol.startswith("messages"):
            root = re.sub(r"/v1/messages$", "", base, flags=re.IGNORECASE)
            root = re.sub(r"/v1$", "", root, flags=re.IGNORECASE)
            return root + "/v1/" + protocol
        if provider.adapter == "anthropic" and not urlsplit(base).path.rstrip("/"):
            base += "/v1"
        return f"{base}/{protocol}"

    def wire(self, provider, model, inbound="chat/completions"):
        configured = getattr(provider, "modelAdapters", {}).get(model)
        adapter = configured or provider.adapter
        defaults = self.registry.get("wireDefaults", {}).get(provider.id, {})
        seed = self.registry.get("seeds", {}).get(provider.id, {})
        applies = True
        if defaults.get("preserveCustomDestination"):
            applies = (
                not defaults.get("allowBaseUrlOverride")
                and provider.adapter == seed.get("adapter")
                and provider.baseUrl.rstrip("/") == seed.get("baseUrl", "").rstrip("/")
            )
        # API channels use the operator's selected protocol. Registry wire preferences
        # are subscription transport defaults, not an override of an editable API adapter.
        if not configured and applies and provider.authMode == "oauth":
            rule = defaults.get("modelWireDefaults", {}).get(model.strip().lower())
            if isinstance(rule, str):
                adapter = rule
            elif isinstance(rule, dict):
                surface = {"chat/completions": "chat", "messages": "anthropic"}.get(
                    inbound, inbound
                )
                if surface in rule["inbound"] and provider.authMode in rule.get(
                    "authModes", [provider.authMode]
                ):
                    adapter = rule["wire"]
        if adapter not in WIRES:
            raise ValueError(f"The configured model adapter {adapter!r} is not migrated.")
        return WIRES[adapter]

    def chat_policy(self, body, provider, model, *, translated):
        explicit_fold = getattr(provider, "foldDeveloperRoleToSystem", None)
        preserve = getattr(provider, "preserveReasoningContentModels", None)
        thinking = (
            model in preserve
            if preserve is not None
            else bool(
                re.search(
                    r"(?:^|/)deepseek-(?:v\d+(?:\.\d+)*-(?:flash|pro)|flash|pro)(?:$|[-_:])", model
                )
            )
        )
        for message in body.get("messages", []):
            if message.get("role") == "developer" and (
                explicit_fold is True or (translated and explicit_fold is not False)
            ):
                message["role"] = "system"
            if (
                thinking
                and message.get("role") == "assistant"
                and not message.get("reasoning_content")
            ):
                calls = message.get("tool_calls", [])
                text = list(
                    dict.fromkeys(
                        self.reasoning[(provider.id, model, c["id"])]
                        for c in calls
                        if (provider.id, model, c["id"]) in self.reasoning
                    )
                )
                if text or calls or body.get("tools"):
                    message["reasoning_content"] = "\n".join(text) or " "
        effort = body.get("reasoning_effort")
        if effort:
            effort_map = getattr(provider, "modelReasoningEffortMap", {}).get(model, {})
            body["reasoning_effort"] = effort_map.get(effort, effort)
        return body

    def convert(self, body, protocol, provider, *, cache_retention=None, raw_body=None):
        chat = to_chat(body, protocol[0], preserve_tool_media=protocol[1] == "messages")
        result = from_chat(chat, protocol[1])
        if protocol[1] == "messages":
            apply_anthropic_policy(
                result,
                chat,
                provider,
                registry=self.registry,
                cache_retention=cache_retention,
                raw_body=raw_body,
            )
        return result

    def prepare(self, provider, protocol, body, headers, *, response_mode):
        return PreparedRequest(self.url(provider, protocol), headers, body, protocol, response_mode)

    async def normalize_images(self, body, images, *, native, translated):
        if native == "messages" and translated:
            await images.normalize(body.get("messages", []))

    def channel_body(self, body, *, chatgpt, claude, compact, credential):
        if chatgpt:
            return subscription_body(body, compact=compact, credential=credential), None
        if claude:
            return prepare_anthropic_body(body)
        return body, None
