"""Wire fields consumed by the streaming decoder, not runtime input validators."""

from typing import Literal, NotRequired, TypedDict


class ChatChoice(TypedDict, total=False):
    finish_reason: str | None


class ChatEvent(TypedDict, total=False):
    type: str
    choices: list[ChatChoice]


class ResponseUsage(TypedDict, total=False):
    input_tokens: int
    output_tokens: int


class ResponseBody(TypedDict, total=False):
    usage: ResponseUsage | None


class FunctionItem(TypedDict):
    type: Literal["function_call"]
    id: str
    call_id: str
    name: str
    arguments: NotRequired[str]


class PassiveItem(TypedDict):
    type: Literal["message", "reasoning"]


class ResponseTextDelta(TypedDict):
    type: Literal["response.output_text.delta", "response.reasoning_summary_text.delta"]
    delta: str


class ResponseItemAdded(TypedDict):
    type: Literal["response.output_item.added"]
    item: FunctionItem | PassiveItem


class ResponseArgumentsDelta(TypedDict):
    type: Literal["response.function_call_arguments.delta"]
    item_id: str
    delta: str


class ResponseTerminal(TypedDict):
    type: Literal["response.completed", "response.incomplete"]
    response: ResponseBody


class ResponseSignal(TypedDict):
    type: Literal["response.failed", "response.created", "response.in_progress"]


type ResponsesEvent = (
    ResponseTextDelta
    | ResponseItemAdded
    | ResponseArgumentsDelta
    | ResponseTerminal
    | ResponseSignal
)


class ToolBlock(TypedDict):
    type: Literal["tool_use"]
    id: str
    name: str
    input: object


class TextBlock(TypedDict):
    type: Literal["text"]
    text: str


class ThinkingBlock(TypedDict):
    type: Literal["thinking"]
    thinking: str
    signature: NotRequired[str]


class RedactedBlock(TypedDict):
    type: Literal["redacted_thinking"]
    data: str


class ThinkingState(TypedDict):
    """Mutable accumulator for either form of Anthropic thinking."""

    type: Literal["thinking", "redacted_thinking"]
    thinking: NotRequired[str]
    signature: NotRequired[str]
    data: NotRequired[str]


class AnthropicMessage(TypedDict, total=False):
    usage: dict[str, object]


class MessageStart(TypedDict):
    type: Literal["message_start"]
    message: AnthropicMessage


class BlockStart(TypedDict):
    type: Literal["content_block_start"]
    index: int
    content_block: ToolBlock | TextBlock | ThinkingBlock | RedactedBlock


class TextDelta(TypedDict):
    type: Literal["text_delta"]
    text: str


class ThinkingDelta(TypedDict):
    type: Literal["thinking_delta"]
    thinking: str


class SignatureDelta(TypedDict):
    type: Literal["signature_delta"]
    signature: str


class InputDelta(TypedDict):
    type: Literal["input_json_delta"]
    partial_json: str


class BlockDelta(TypedDict):
    type: Literal["content_block_delta"]
    index: int
    delta: TextDelta | ThinkingDelta | SignatureDelta | InputDelta


class BlockStop(TypedDict):
    type: Literal["content_block_stop"]
    index: int


class StopDelta(TypedDict, total=False):
    stop_reason: str | None


class MessageDelta(TypedDict):
    type: Literal["message_delta"]
    delta: StopDelta
    usage: NotRequired[dict[str, object]]


class MessageSignal(TypedDict):
    type: Literal["message_stop", "ping"]


type AnthropicEvent = (
    MessageStart | BlockStart | BlockDelta | BlockStop | MessageDelta | MessageSignal
)
