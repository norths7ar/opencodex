"""Server-sent event framing and decoding."""

import json


def sse(data, event=None):
    prefix = f"event: {event}\n" if event else ""
    return (
        prefix + "data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n\n"
    ).encode()


async def events(response, *, include_done=False):
    lines = []
    async for line in response.aiter_lines():
        if not line:
            if lines:
                data = "\n".join(lines)
                lines = []
                if data == "[DONE]":
                    if include_done:
                        yield {"type": "ocx.done"}
                    return
                yield json.loads(data)
        elif line.startswith("data:"):
            lines.append(line[5:].lstrip(" "))
    if lines:
        if "\n".join(lines) == "[DONE]":
            if include_done:
                yield {"type": "ocx.done"}
        else:
            yield json.loads("\n".join(lines))
