"""Project private Codex agent-message items onto public protocol messages."""

import base64
import json
import re

_OMITTED = "[Encrypted content omitted: the selected route cannot read it.]"
_RUN = re.compile(r"(?<![A-Za-z0-9_=-])g[A-Za-z0-9_-]{97,}={0,2}(?![A-Za-z0-9_=-])")
_SHAPE = re.compile(r"g[A-Za-z0-9_-]+={0,2}")
_PREAMBLE = re.compile(
    r"(?:^|\n)\[CXC-[A-Z0-9-]+\][^\n]*(?:\n(?!\n|Message Type\s*:)[^\n]*)*"
    r"(?=\n{2,}|\nMessage Type\s*:|$)",
    re.IGNORECASE,
)
_ENVELOPE = re.compile(
    r"(?:^|\n)Message Type\s*:\s*(?:NEW_TASK|MESSAGE|FOLLOWUP_TASK)[^\n]*\n"
    r"\s*Task name\s*:[^\n]*\n\s*Sender\s*:[^\n]*\n\s*Payload\s*:\s*(?:\n|$)"
    r"|(?:^|\n)Message Type\s*:\s*FINAL_ANSWER[^\n]*\n"
    r"\s*(?:Task name\s*:[^\n]*\n\s*)?Sender\s*:[^\n]*\n\s*Payload\s*:\s*(?:\n|$)",
    re.IGNORECASE,
)
_TRAILING_CONTROL = {"compaction_trigger", "additional_tools", "configuration_update"}


class UnreadableAgentTask(ValueError):
    code = "unreadable_encrypted_agent_task"

    def __init__(self) -> None:
        super().__init__(
            "The selected upstream cannot read this encrypted agent task. "
            "Use the native ChatGPT connection or start a new task with readable input."
        )


def _token(value: str) -> bool:
    if len(value) < 100 or len(value) % 4 or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        return False
    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
    except ValueError:
        return False
    return (
        len(decoded) >= 73
        and decoded[0] == 0x80
        and (len(decoded) - 57) % 16 == 0
        and base64.urlsafe_b64encode(decoded).decode("ascii") == value
    )


def _text(value: str) -> tuple[str, str, bool]:
    """Return display text, readable text, and whether ciphertext was removed."""
    spans: list[tuple[int, int]] = []
    for match in _RUN.finditer(value):
        if _token(match[0]):
            spans.append(match.span())
            if len(spans) > 64:
                return _OMITTED, "", True
    if not spans:
        stripped = value.strip()
        if len(stripped) >= 100 and len(stripped) % 4 == 0 and _SHAPE.fullmatch(stripped):
            return _OMITTED, "", True
        return value, value, False
    display, readable = [], []
    start = 0
    for left, right in spans:
        display.extend((value[start:left], _OMITTED))
        readable.extend((value[start:left], "\n\n"))
        start = right
    return (
        "".join([*display, value[start:]]),
        "".join([*readable, value[start:]]),
        True,
    )


def _split_tokens(parts: list) -> set[int]:
    """Treat adjacent text fragments as opaque only if they form one complete token."""
    run = []
    indexes = set()

    def finish():
        if len(run) > 1 and len(run) <= 256 and _token("".join(text for _, text in run)):
            indexes.update(index for index, _ in run)
        run.clear()

    for index, part in enumerate(parts):
        if (
            isinstance(part, dict)
            and part.get("type") in {"input_text", "text"}
            and isinstance(part.get("text"), str)
            and re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", part["text"])
        ):
            run.append((index, part["text"]))
        else:
            finish()
    finish()
    return indexes


def _lower(item: dict, *, current: bool, include_identities: bool) -> dict:
    raw = item.get("content")
    parts = [{"type": "input_text", "text": raw}] if isinstance(raw, str) else raw
    if not isinstance(parts, list):
        raise ValueError("The selected route cannot represent this agent message content.")
    lowered, readable_parts = [], []
    opaque = False
    split = _split_tokens(parts)
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            raise ValueError("The selected route cannot represent this agent message part.")
        kind = part.get("type")
        if (
            kind in {"input_text", "text"}
            and isinstance(part.get("text"), str)
            and index not in split
        ):
            text, readable, hidden = _text(part["text"])
            lowered.append({"type": "input_text", "text": text})
            readable_parts.append(readable)
            opaque |= hidden
        elif kind == "encrypted_content":
            encrypted = part.get("encrypted_content")
            display, readable, hidden = (
                _text(encrypted) if isinstance(encrypted, str) else ("", "", False)
            )
            lowered.append({"type": "input_text", "text": display if hidden else _OMITTED})
            if hidden:
                readable_parts.append(readable)
            opaque = True
        elif index in split:
            lowered.append({"type": "input_text", "text": _OMITTED})
            opaque = True
        elif kind in {"input_image", "input_file"}:
            lowered.append(part)
        else:
            raise ValueError(f"The selected route cannot represent agent message part {kind!r}.")
    if current and opaque:
        readable = _PREAMBLE.sub("\n", "\n\n".join(readable_parts))
        if not _ENVELOPE.sub("\n", readable).strip():
            raise UnreadableAgentTask()
    identities = {
        key: item[key] for key in ("author", "recipient") if isinstance(item.get(key), str)
    }
    if include_identities and identities:
        encoded = json.dumps(identities, ensure_ascii=False, separators=(",", ":"))
        lowered.insert(0, {"type": "input_text", "text": f"Agent message {encoded}"})
    if not lowered:
        lowered.append({"type": "input_text", "text": "(sub-agent message received)"})
    return {"type": "message", "role": "user", "content": lowered}


def routed_agent_messages(body: dict, *, include_identities: bool = True) -> dict:
    """Lower readable replay and reject an unreadable current task before upstream I/O."""
    items = body.get("input")
    if not isinstance(items, list):
        return body
    tail = len(items) - 1
    while (
        tail >= 0 and isinstance(items[tail], dict) and items[tail].get("type") in _TRAILING_CONTROL
    ):
        tail -= 1
    if not any(isinstance(item, dict) and item.get("type") == "agent_message" for item in items):
        return body
    return {
        **body,
        "input": [
            _lower(item, current=index == tail, include_identities=include_identities)
            if isinstance(item, dict) and item.get("type") == "agent_message"
            else item
            for index, item in enumerate(items)
        ],
    }
