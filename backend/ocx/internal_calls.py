"""User-requested helper calls use the ordinary gateway and its usage accounting."""

import json
import logging

import httpx


async def sizing_completion(port, call, *, transport=None):
    body = {
        "model": call["model"],
        "messages": [
            {"role": "system", "content": call["system"]},
            {"role": "user", "content": call["user"]},
        ],
    }
    try:
        async with (
            httpx.AsyncClient(trust_env=False, timeout=180, transport=transport) as client,
            client.stream(
                "POST", f"http://127.0.0.1:{port}/v1/chat/completions", json=body
            ) as response,
        ):
            if response.status_code != 200:
                return {"error": f"HTTP {response.status_code}"}
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 1 << 20:
                    return {"error": "the response was too large"}
        result = json.loads(content)
        text = result["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            return {"error": "the response had no text"}
        return {"text": text}
    except httpx.HTTPError, ValueError, TypeError, KeyError, IndexError:
        logging.getLogger(__name__).warning("Role sizing request could not be completed.")
        return {"error": "the request could not be completed"}
