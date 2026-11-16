"""Shared text and media conversions through Chat content."""

import copy


def text_content(content):
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return "".join(
        p.get("text", "") for p in content if p.get("type") in {"text", "input_text", "output_text"}
    )


def chat_content(content):
    if isinstance(content, str) or content is None:
        return content
    result = []
    for part in content:
        kind = part.get("type")
        if kind in {"text", "input_text", "output_text"}:
            result.append({"type": "text", "text": part.get("text", "")})
        elif kind == "input_image":
            result.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": part["image_url"],
                        **({"detail": part["detail"]} if "detail" in part else {}),
                    },
                }
            )
        elif kind in {"image_url", "input_audio"}:
            result.append(copy.deepcopy(part))
        elif kind == "image":
            source = part["source"]
            url = source.get("url") or f"data:{source['media_type']};base64,{source['data']}"
            result.append({"type": "image_url", "image_url": {"url": url}})
        else:
            raise ValueError(f"The translated route cannot represent content type {kind!r}.")
    return result
