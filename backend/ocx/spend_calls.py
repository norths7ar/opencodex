"""Prepared-wire token estimates and one physical send's reservation lifecycle."""

import base64
import contextlib
import copy
import io
import json
import math
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from .spend_policy import current_policy


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def estimate_tokens(text, model=""):
    """Port token-estimate.ts segmented UTF-16/CJK heuristic, not measured usage."""
    if not isinstance(text, str) or not text:
        return 0
    model = model.lower()
    ratio = (
        2.8
        if model.startswith("kiro")
        else 3.5
        if model.startswith(("claude", "deepseek", "minimax", "glm", "qwen"))
        else 4
    )
    cjk = sum(
        any(
            low <= ord(c) <= high
            for low, high in (
                (0xAC00, 0xD7A3),
                (0x1100, 0x11FF),
                (0x3130, 0x318F),
                (0x4E00, 0x9FFF),
                (0x3400, 0x4DBF),
                (0x3040, 0x30FF),
            )
        )
        for c in text
    )
    units = len(text.encode("utf-16-le", errors="surrogatepass")) // 2
    return max(1, math.ceil((units - cjk) / ratio + cjk / 1.5))


def _attachment(data):
    """Original Claude headers-only dimension sniff, otherwise decoded bytes/512."""
    try:
        prefix = data[:65536]
        raw = base64.b64decode(prefix[: len(prefix) - len(prefix) % 4], validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            if image.format in {"PNG", "JPEG", "GIF", "WEBP"}:
                return max(256, math.ceil(image.width * image.height / 750))
    except ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError:
        pass
    return max(256, math.ceil(math.floor(len(data.rstrip("=")) * 3 / 4) / 512))


def _claude_input(body, model):
    attachments = 0

    def sanitize(block):
        nonlocal attachments
        if not isinstance(block, dict):
            return block
        source = block.get("source")
        if (
            block.get("type") in {"image", "document"}
            and isinstance(source, dict)
            and source.get("type") == "base64"
            and isinstance(source.get("data"), str)
        ):
            attachments += _attachment(source["data"])
            return {**block, "source": {**source, "data": ""}}
        if block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            return {**block, "content": [sanitize(part) for part in block["content"]]}
        return block

    parts = []
    if "system" in body:
        parts.append(body["system"] if isinstance(body["system"], str) else _json(body["system"]))
    if "messages" in body:
        messages = [
            {**message, "content": [sanitize(part) for part in message["content"]]}
            if isinstance(message, dict) and isinstance(message.get("content"), list)
            else message
            for message in body["messages"]
        ]
        parts.append(_json(messages))
    if "tools" in body:
        parts.append(_json(body["tools"]))
    return max(1, estimate_tokens("\n".join(parts), model) + attachments)


def _media(url, divisor=750):
    if not isinstance(url, str) or not url.startswith("data:") or "," not in url:
        return 850
    size = len(url.partition(",")[2])
    return max(1, math.ceil(math.floor(size * 3 / 4) / divisor)) if size else 0


def _responses_input(body, model):
    def content(value):
        if isinstance(value, str):
            return estimate_tokens(value, model)
        if isinstance(value, list):
            return sum(content(part) for part in value)
        if not isinstance(value, dict):
            return 0
        kind = value.get("type")
        if kind in {"input_image", "input_video"}:
            return _media(value.get("image_url", value.get("video_url")))
        if kind == "input_audio":
            audio = value.get("input_audio", value)
            return _media("data:audio;base64," + audio.get("data", ""), 256)
        if kind == "input_file" and isinstance(value.get("file_data"), str):
            data = value["file_data"]
            return _media(data if data.startswith("data:") else "data:file;base64," + data)
        if kind in {"function_call", "custom_tool_call"}:
            args = value.get("arguments", value.get("input", ""))
            return estimate_tokens(value.get("name", ""), model) + estimate_tokens(
                args if isinstance(args, str) else _json(args), model
            )
        if kind in {"function_call_output", "custom_tool_call_output"}:
            return content(value.get("output"))
        if "text" in value:
            return estimate_tokens(value["text"], model)
        if "content" in value:
            return content(value["content"])
        if kind == "reasoning":
            return content(value.get("summary", [])) + estimate_tokens(
                value.get("encrypted_content", ""), model
            )
        # Unknown native items still ride the wire; keep their serialized bytes in the estimate.
        return estimate_tokens(_json(value), model)

    total = content(body.get("instructions", "")) + content(body.get("input", []))
    for tool in body.get("tools", []):
        if not isinstance(tool, dict):
            continue
        definition = tool.get("function", tool)
        total += estimate_tokens(definition.get("name", ""), model)
        total += estimate_tokens(definition.get("description", ""), model)
        total += estimate_tokens(_json(definition.get("parameters", {})), model)
    return max(1, total)


def prepared_estimate(body, protocol, model=None):
    """Estimate the fully prepared send; omit invented output defaults or cache discounts."""
    model = model or body.get("model", "")
    if protocol.startswith("messages"):
        input_tokens, output = _claude_input(body, model), body.get("max_tokens")
    elif protocol == "chat/completions":
        parts = [_json(body.get("messages", []))]
        if "tools" in body:
            parts.append(_json(body["tools"]))
        input_tokens = max(1, estimate_tokens("\n".join(parts), model))
        output = body.get("max_completion_tokens")
        if output is None:
            output = body.get("max_tokens")
    else:
        input_tokens, output = _responses_input(body, model), body.get("max_output_tokens")
    output = (
        math.trunc(output)
        if type(output) in {int, float} and math.isfinite(output) and output > 0
        else 0
    )
    return input_tokens, output


class PhysicalSpend:
    """One proof per HTTP send. Repeated send/settlement never reuses an allowance."""

    def __init__(self, ledger, proof):
        self.ledger, self.proof = ledger, proof
        self.dispatched, self.finished = False, False

    def dispatch(self):
        if self.finished or self.dispatched or not self.ledger.mark_dispatched(self.proof):
            raise ValueError("The spend reservation cannot authorize another physical send.")
        self.dispatched = True

    def finish(self, usage=None):
        if self.finished:
            return False
        if not self.dispatched:
            done = self.ledger.abandon(self.proof)
        elif usage and not usage.get("estimated"):
            done = self.ledger.settle(self.proof, usage)
        else:
            done = self.ledger.mark_lost(self.proof)
        self.finished = done
        return done


class RequestSpend:
    """Capture ceilings for a logical request; each actual send owns its own proof."""

    def __init__(self, ledger, *, admission_policy=None):
        self.ledger = ledger
        self.policy = copy.deepcopy(admission_policy or current_policy(ledger.loader))
        self.sends = []

    def prepare(self, body, protocol, scopes, *, reserve_count=None, model=None, send_id=None):
        inputs, ceiling = prepared_estimate(body, protocol, model)
        proof = self.ledger.reserve(
            send_id or str(uuid4()), scopes, inputs, ceiling, admission_policy=self.policy
        )
        send = PhysicalSpend(self.ledger, proof)
        try:
            if reserve_count:
                reserve_count()
        except BaseException:
            send.finish()
            raise
        self.sends.append(send)
        return send

    def close(self):
        """Settle any forgotten dispatched send as unresolved, or refund unstarted work."""
        for send in self.sends:
            send.finish()
            if send.finished:
                # The proof may already be forgotten by a prior close or bounded pressure eviction.
                with contextlib.suppress(ValueError):
                    self.ledger.forget_resolved(send.proof)
