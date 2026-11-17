"""Responses reasoning replay sanitation, independent of transport."""

from copy import deepcopy


def sanitize_reasoning(
    body,
    *,
    preserve_plaintext=False,
    drop_nonarray_content=False,
    strip_encrypted=False,
    drop_foreign_id=False,
    require_plaintext=False,
):
    result = deepcopy(body)
    if not isinstance(result, dict) or not isinstance(result.get("input"), list):
        return result
    items = result["input"]
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        content = item.get("content")
        envelope = isinstance(item.get("encrypted_content"), str) and item[
            "encrypted_content"
        ].startswith("ocxr1:")
        item.setdefault("summary", [])
        item.pop("status", None)
        if strip_encrypted or envelope:
            item.pop("encrypted_content", None)
        if drop_foreign_id:
            item.pop("id", None)
        if drop_nonarray_content and "content" in item and not isinstance(content, list):
            item.pop("content")
        elif not preserve_plaintext and (isinstance(content, list) and content or envelope):
            item["content"] = []
        replay_content = item.get("content")
        if require_plaintext and not any(
            isinstance(p, dict)
            and p.get("type") == "reasoning_text"
            and isinstance(p.get("text"), str)
            and p["text"]
            for p in (replay_content if isinstance(replay_content, list) else [])
        ):
            raw_summary = item.get("summary")
            summary = raw_summary if isinstance(raw_summary, list) else []
            item["content"] = [
                {"type": "reasoning_text", "text": p["text"]}
                for p in summary
                if isinstance(p, dict)
                and p.get("type") == "summary_text"
                and isinstance(p.get("text"), str)
                and p["text"]
            ] or [{"type": "reasoning_text", "text": " "}]
    if require_plaintext:
        output = []
        turn = []

        def flush():
            if any(
                x.get("type") in {"function_call", "custom_tool_call", "local_shell_call"}
                for x in turn
            ) and not any(x.get("type") == "reasoning" for x in turn):
                output.append(
                    {
                        "type": "reasoning",
                        "summary": [],
                        "content": [{"type": "reasoning_text", "text": " "}],
                    }
                )
            output.extend(turn)
            turn.clear()

        for item in items:
            typ = (
                item.get("type", "message" if "role" in item else "")
                if isinstance(item, dict)
                else ""
            )
            assistant = isinstance(item, dict) and (
                typ == "reasoning"
                or typ == "message"
                and item.get("role") == "assistant"
                or typ.endswith("_call")
            )
            if assistant:
                turn.append(item)
            else:
                flush()
                output.append(item)
        flush()
        result["input"] = output
    return result
