"""Hosted search helper wire contracts; credentials and physical sends belong to the gateway."""

import asyncio
import copy
import json

from .search_sources import append_source, extract_trailing_sources

BASE_INSTRUCTION = (
    "You are a web-search assistant. Use the web_search tool to find current information for the "
    "user's query, then reply with a concise, factual answer. End your reply with a `Sources:` "
    "section listing each source you used on its own line as `- Title: URL` (one per line)."
)
IMAGE_INSTRUCTION = (
    " The model that will read your answer is TEXT-ONLY and cannot see images: if the results include "
    "relevant images, describe what they show in words and include their source URLs in your answer."
)


def helper_request(query, plan):
    """Return protocol/body; dispatch applies normal OAuth fingerprint, refresh and accounting."""
    instruction = BASE_INSTRUCTION + (IMAGE_INSTRUCTION if plan.get("describeImages") else "")
    if plan["backend"] == "openai":
        return "responses", {
            "model": plan["model"],
            "instructions": instruction,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": query}]}],
            "tools": [copy.deepcopy(plan["hostedTool"])],
            "tool_choice": "auto",
            "reasoning": {"effort": plan["reasoning"]},
            "store": False,
            "stream": True,
        }
    raise ValueError("Selected search backend has not been migrated")


async def execute_search(query, plan, dispatch):
    """Dispatch yields decoded native SSE dictionaries (byte bounds enforced by transport)."""
    result = {"text": "", "sources": []}
    terminal = False
    stop_reason = None
    size = 0

    def source(value):
        append_source(result["sources"], value)

    try:
        protocol, body = helper_request(query, plan)
        async with asyncio.timeout(plan["timeoutMs"] / 1000):
            async for event in dispatch(protocol, body, plan):
                size += len(json.dumps(event, ensure_ascii=False).encode())
                limit = 65536 if protocol == "messages" else 1024 * 1024
                if size > limit or len(result["text"]) > 65536:
                    result["error"] = "search helper exceeded response limit"
                    break
                kind = event.get("type")
                if terminal:
                    result["error"] = "search helper returned data after completion"
                if protocol == "responses":
                    if kind == "response.output_text.delta":
                        result["text"] += event.get("delta", "")
                    elif kind == "response.output_text.annotation.added":
                        source(event.get("annotation", {}))
                    elif kind in {"response.completed", "response.failed", "response.incomplete"}:
                        terminal = True
                        response = event.get("response", {})
                        if kind != "response.completed":
                            result["error"] = "search helper did not complete"
                        final_text = []
                        for item in response.get("output", []):
                            if item.get("type") == "web_search_call":
                                for hit in item.get("action", {}).get("sources", []):
                                    source(hit)
                            for content in item.get("content", []):
                                if content.get("type") == "output_text":
                                    final_text.append(content.get("text", ""))
                                for annotation in content.get("annotations", []):
                                    source(annotation)
                        if final_text:
                            result["text"] = "".join(final_text)
                    elif kind == "error":
                        result["error"] = "search helper returned a stream error"
                else:
                    if kind == "content_block_start":
                        block = event.get("content_block", {})
                        if block.get("type") == "web_search_tool_result":
                            content = block.get("content", [])
                            if isinstance(content, list):
                                for hit in content:
                                    if hit.get("type") == "web_search_result":
                                        source(hit)
                            else:
                                result["error"] = "search helper tool failed"
                    elif kind == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            result["text"] += delta.get("text", "")
                        elif delta.get("type") == "citations_delta":
                            source(delta.get("citation", {}))
                    elif kind == "message_delta":
                        stop_reason = event.get("delta", {}).get("stop_reason")
                    elif kind == "message_stop":
                        terminal = True
                    elif kind == "error":
                        result["error"] = "search helper returned a stream error"
        if not terminal or (protocol == "messages" and stop_reason != "end_turn"):
            result["error"] = "search helper did not complete"
        if len(result["text"]) > 65536:
            result["text"] = result["text"][:65536]
            result["error"] = "search helper exceeded decoded text limit"
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - helper failures become tool results; cancellation propagates above.
        result["error"] = "search helper could not complete"
    result["text"], text_sources = extract_trailing_sources(result["text"])
    for citation in text_sources:
        append_source(result["sources"], citation)
    return result
