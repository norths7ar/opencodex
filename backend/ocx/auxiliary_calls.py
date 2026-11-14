"""Vision helpers enter the normal local gateway for routing and usage accounting."""

import json

import httpx

from .auxiliary import TERMINAL_HEADER, WORKFLOW_HEADER


async def vision_completion(port, call, *, transport=None):
    body = {
        "model": call["model"],
        "reasoning_effort": call["reasoning"],
        "messages": [
            {"role": "system", "content": call["system"]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "User's request context: " + call["context"]},
                    {"type": "image_url", "image_url": {"url": call["image"]}},
                ],
            },
        ],
    }
    try:
        async with (
            httpx.AsyncClient(
                trust_env=False, timeout=call["timeoutMs"] / 1000, transport=transport
            ) as client,
            client.stream(
                "POST",
                f"http://127.0.0.1:{port}/v1/chat/completions",
                json=body,
                headers={
                    TERMINAL_HEADER: "1",
                    **(
                        {WORKFLOW_HEADER: call["workflowToken"]}
                        if call.get("workflowToken")
                        else {}
                    ),
                },
            ) as response,
        ):
            if response.status_code != 200:
                return {"error": f"HTTP {response.status_code}"}
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 4 * 1024 * 1024:
                    return {"error": "description response too large"}
        result = json.loads(content)
        text = result["choices"][0]["message"]["content"]
        if isinstance(text, list):
            text = "".join(
                part.get("text", "")
                for part in text
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        return {"text": text} if isinstance(text, str) else {"error": "no description text"}
    except httpx.HTTPError, ValueError, TypeError, KeyError, IndexError:
        return {"error": "vision request could not be completed"}
