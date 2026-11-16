"""Request codec dispatch by wire protocol."""

import copy

from .messages import chat_to_messages, messages_to_chat
from .responses import chat_to_responses, responses_to_chat


def to_chat(body, protocol, *, preserve_tool_media=False):
    if protocol == "responses":
        return responses_to_chat(body, preserve_tool_media=preserve_tool_media)
    if protocol == "messages":
        return messages_to_chat(body)
    return copy.deepcopy(body)


def from_chat(body, protocol):
    if protocol == "responses":
        return chat_to_responses(body)
    if protocol == "messages":
        return chat_to_messages(body)
    return body
