"""Request defaults for known destinations; explicit channel settings take precedence."""

from backend.configuration.models import Channel, ChannelRequestPolicy

from .registry import match_destination


def request_policy(channel: Channel) -> ChannelRequestPolicy:
    """Resolve transport defaults without making routing decisions from model names."""
    match = match_destination(channel)
    defaults = ChannelRequestPolicy()
    if match is not None:
        if match.service.id == "deepseek":
            defaults.statelessResponses = True
            defaults.requiresAdjacentResponsesToolResults = True
        elif match.service.id == "opencode-go":
            defaults.statelessResponses = True
        elif match.service.id == "mimo":
            if match.destination.id in {"api", "token-plan"}:
                defaults.reasoningEffortMap = {"xhigh": "high", "max": "high", "ultra": "high"}
            if match.destination.id == "token-plan":
                # The token-plan Responses endpoint accepts plain turns, but not custom tools.
                defaults.supportsResponsesCustomTools = False
    return ChannelRequestPolicy.model_validate(
        defaults.model_dump(exclude_none=True)
        | channel.request_policy.model_dump(exclude_none=True)
    )
