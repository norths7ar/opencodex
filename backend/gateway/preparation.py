"""Resolve and prepare one generation attempt without sending it upstream."""

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from backend.auth.access import ChannelAuthentication, validate_subscription
from backend.catalog.metadata import resolve_metadata
from backend.configuration.models import Channel, ChannelRequestPolicy, Configuration, Protocol
from backend.policies.compaction import replay_projection
from backend.protocols import from_chat, response_tools, to_chat
from backend.protocols.agent_messages import routed_agent_messages
from backend.protocols.anthropic_history import sanitize_native_ids
from backend.protocols.anthropic_images import AnthropicImages
from backend.protocols.native_tools import NativeToolAdapter
from backend.protocols.reasoning_replay import sanitize_reasoning
from backend.protocols.tool_adjacency import adjacent_results
from backend.providers import GenerationEndpoint, generation_endpoint, request_policy
from backend.providers.chatgpt import subscription_body
from backend.providers.registry import match_destination

from .messages_policy import apply_anthropic_policy, direct_messages_endpoint

Wire = Literal["chat/completions", "responses", "messages"]
Inbound = Literal[
    "chat/completions", "responses", "messages", "responses/compact", "messages/count_tokens"
]
WIRES: dict[Protocol, Wire] = {
    "openai-chat": "chat/completions",
    "openai-responses": "responses",
    "anthropic": "messages",
}
_REASONING_REPLAY = re.compile(
    r"(?:^|/)deepseek-(?:v\d+(?:\.\d+)*-(?:flash|pro)|flash|pro)(?:$|[-_:])"
)


_EMPTY_TOOL_OUTPUT = "[ocx] empty tool output: the tool ran but produced no stdout or return value; do not treat this as success, failure, or user-provided input."


def _empty_tool_output(value):
    if isinstance(value, str):
        return not value.strip()
    if not isinstance(value, list):
        return False
    parts = []
    for item in value:
        if not isinstance(item, dict):
            return False
        kind = item.get("type")
        field = (
            "text"
            if kind in {"text", "input_text", "output_text"}
            else "refusal"
            if kind == "refusal"
            else None
        )
        if field is None or not isinstance(item.get(field), str):
            return False
        parts.append(item[field])
    return not "".join(parts).strip()


def _annotate_empty_outputs(body, inbound):
    if inbound in {"responses", "responses/compact"}:
        for item in body.get("input", []) if isinstance(body.get("input"), list) else []:
            if (
                isinstance(item, dict)
                and item.get("type") in {"function_call_output", "custom_tool_call_output"}
                and _empty_tool_output(item.get("output"))
            ):
                item["output"] = _EMPTY_TOOL_OUTPUT
    elif inbound == "chat/completions":
        for item in body.get("messages", []):
            if (
                isinstance(item, dict)
                and item.get("role") == "tool"
                and _empty_tool_output(item.get("content"))
            ):
                item["content"] = _EMPTY_TOOL_OUTPUT
    elif inbound == "messages":
        for message in body.get("messages", []):
            for item in (
                message.get("content", [])
                if isinstance(message, dict) and isinstance(message.get("content"), list)
                else []
            ):
                if (
                    isinstance(item, dict)
                    and item.get("type") == "tool_result"
                    and _empty_tool_output(item.get("content"))
                ):
                    item["content"] = _EMPTY_TOOL_OUTPUT


@dataclass(frozen=True, slots=True)
class PreparationRoute:
    endpoint: GenerationEndpoint
    wire: Wire
    inbound: Inbound
    translated: bool
    stateless: bool
    special: bool
    chatgpt: bool


@dataclass(slots=True)
class PreparedGeneration:
    url: str
    headers: dict[str, str]
    body: dict
    wire: Wire
    translated: bool
    stateless: bool
    progress_updates: bool
    hide_thinking: bool
    native_tools: NativeToolAdapter | None
    custom_tools: set[str]
    tool_names: dict
    response_mode: Literal["auto", "event_stream"]


class GenerationPreparation:
    def __init__(
        self, authentication: ChannelAuthentication, *, images: AnthropicImages | None = None
    ):
        self.authentication = authentication
        self.images = images if images is not None else AnthropicImages()
        self._reasoning: dict[tuple[str, str, str], str] = {}

    @staticmethod
    def route(
        channel: Channel,
        model: str,
        inbound: Inbound,
        body: dict,
        *,
        fallback_origin: str | None = None,
    ) -> PreparationRoute:
        chatgpt = channel.auth_mode == "oauth"
        if chatgpt:
            validate_subscription(channel)
        compact = inbound == "responses/compact"
        count = inbound == "messages/count_tokens"
        endpoint = generation_endpoint(channel, model, compact=compact)
        wire = WIRES[endpoint.protocol]
        if count:
            if wire != "messages":
                raise ValueError(f"{inbound} requires a provider using its native protocol.")
            parsed = urlsplit(endpoint.url)
            endpoint = GenerationEndpoint(
                parsed._replace(path=parsed.path + "/count_tokens").geturl(), endpoint.protocol
            )
        special = compact or count
        policy = request_policy(channel)
        stateless = inbound == "responses" and (
            chatgpt
            or bool(policy.statelessResponses)
            or fallback_origin is not None
            or str(body.get("previous_response_id", "")).startswith(
                ("resp_ocxcmp_", "resp_ocxsearch_")
            )
        )
        return PreparationRoute(
            endpoint, wire, inbound, not special and inbound != wire, stateless, special, chatgpt
        )

    def remember_reasoning(self, channel_id: str, model: str, chat: dict) -> None:
        message = chat["choices"][0]["message"]
        reasoning = message.get("reasoning_content")
        if reasoning:
            for call in message.get("tool_calls", []):
                self._reasoning[channel_id, model, call["id"]] = reasoning

    def _chat_policy(
        self,
        body: dict,
        raw: dict,
        channel_id: str,
        model: str,
        policy: ChannelRequestPolicy,
        *,
        translated: bool,
    ) -> None:
        explicit_fold = policy.foldDeveloperRoleToSystem
        preserve = policy.preserveReasoningContentModels
        thinking = (
            model in preserve if preserve is not None else bool(_REASONING_REPLAY.search(model))
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
                        self._reasoning[channel_id, model, call["id"]]
                        for call in calls
                        if (channel_id, model, call["id"]) in self._reasoning
                    )
                )
                if text or calls or body.get("tools"):
                    message["reasoning_content"] = "\n".join(text) or " "
        effort = body.get("reasoning_effort")
        if effort:
            maps = policy.modelReasoningEffortMap or {}
            effort_map = maps.get(model, policy.reasoningEffortMap or {})
            body["reasoning_effort"] = effort_map.get(effort, effort)
        for models, fields in (
            (policy.noTemperatureModels, ("temperature",)),
            (policy.noTopPModels, ("top_p",)),
            (policy.noPenaltyModels, ("presence_penalty", "frequency_penalty")),
        ):
            if model in (models or []):
                for field in fields:
                    body.pop(field, None)
        if (
            translated
            and model in (policy.autoToolChoiceOnlyModels or [])
            and body.get("tools")
            and "tool_choice" in body
            and body["tool_choice"] != "none"
        ):
            body["tool_choice"] = "auto"
        if policy.promptCacheKey and "prompt_cache_key" in raw:
            body["prompt_cache_key"] = raw["prompt_cache_key"]
        if body.get("stream"):
            options = body.get("stream_options")
            body["stream_options"] = {
                **(options if isinstance(options, dict) else {}),
                "include_usage": True,
            }

    async def prepare(
        self,
        configuration: Configuration,
        channel: Channel,
        model: str,
        route: PreparationRoute,
        body: dict,
        *,
        incoming: Mapping[str, str] | None = None,
        subscription: dict | None = None,
        metadata: dict | None = None,
    ) -> PreparedGeneration:
        """Prepare a body after the caller has expanded history and projected turn state."""
        policy = request_policy(channel)
        raw = copy.deepcopy(body)
        raw["model"] = model
        if (
            route.wire in {"responses", "chat/completions"}
            and policy.annotateEmptyToolOutputs is True
        ):
            _annotate_empty_outputs(raw, route.inbound)
        if route.inbound == "responses":
            raw = replay_projection(raw, opaque_note=route.translated, frame=not route.chatgpt)
        if route.inbound in {"responses", "responses/compact"} and not (
            route.chatgpt or route.translated
        ):
            raw = routed_agent_messages(raw)
        names = response_tools(raw)[1] if route.inbound == "responses" and route.translated else {}
        custom_tools = {name for name, identity in names.items() if identity["custom"]}
        outbound = raw
        if route.translated:
            # Convert from a disposable copy: the codecs may retain nested values.
            chat = to_chat(
                copy.deepcopy(raw), route.inbound, preserve_tool_media=route.wire == "messages"
            )
            outbound = from_chat(chat, route.wire)
            if route.wire == "messages":
                resolved_metadata = (
                    metadata if metadata is not None else resolve_metadata(channel, model).values
                )
                apply_anthropic_policy(
                    outbound,
                    chat,
                    policy,
                    resolved_metadata,
                    direct=direct_messages_endpoint(route.endpoint.url),
                    cache_retention=configuration.features.request.cache_retention,
                    raw_body=raw,
                )
                await self.images.normalize(outbound.get("messages", []))
        elif route.wire == "messages":
            sanitize_native_ids(outbound.get("messages", []))

        tools = None
        if route.inbound == "responses" and not route.translated and not route.chatgpt:
            tools = NativeToolAdapter(raw, policy.supportsResponsesCustomTools)
            outbound = tools.request(outbound)
        if (
            route.wire == "responses"
            and policy.requiresAdjacentResponsesToolResults
            and isinstance(outbound.get("input"), list)
        ):
            outbound["input"] = adjacent_results(outbound["input"])
        if route.stateless:
            outbound.pop("previous_response_id", None)
        if route.wire == "responses":
            destination = match_destination(channel)
            official = destination is not None and destination.service.id in {"openai", "chatgpt"}
            plaintext = channel.preserve_responses_reasoning_content is True
            strict_plaintext = plaintext and bool(policy.requiresAdjacentResponsesToolResults)
            outbound = sanitize_reasoning(
                outbound,
                preserve_plaintext=plaintext,
                drop_nonarray_content=not official,
                strip_encrypted=strict_plaintext,
                require_plaintext=strict_plaintext,
            )
        if route.wire == "chat/completions":
            self._chat_policy(outbound, raw, channel.id, model, policy, translated=route.translated)
        if route.chatgpt:
            outbound = subscription_body(
                outbound,
                compact=route.special,
                installation_id=subscription["installationId"] if subscription else None,
            )
        progress_updates = (
            route.wire == "messages" and outbound.get("thinking", {}).get("display") == "updates"
        )
        reasoning = body.get("reasoning") or {}
        summary = reasoning.get("summary", reasoning.get("generate_summary"))
        hide_thinking = (
            route.inbound == "responses"
            and not progress_updates
            and (
                summary == "none"
                or (
                    summary is None
                    and reasoning.get("effort")
                    not in {"minimal", "low", "medium", "high", "xhigh", "max"}
                )
            )
        )
        headers = self.authentication.headers(
            channel, route.endpoint.protocol, incoming=incoming, credential=subscription
        )
        if progress_updates:
            beta = "thinking-display-updates-2026-08-18"
            betas = [value for value in headers.get("anthropic-beta", "").split(",") if value]
            headers["anthropic-beta"] = ",".join(dict.fromkeys([*betas, beta]))
        return PreparedGeneration(
            url=route.endpoint.url,
            headers=headers,
            body=outbound,
            wire=route.wire,
            translated=route.translated,
            stateless=route.stateless,
            progress_updates=progress_updates,
            hide_thinking=hide_thinking,
            native_tools=tools,
            custom_tools=custom_tools,
            tool_names=names,
            response_mode="event_stream" if route.chatgpt and not route.special else "auto",
        )
