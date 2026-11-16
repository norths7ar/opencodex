"""Anthropic history tool ID allocation and immediate result pairing."""

import copy
import hashlib
import json
import re


def allocator(raw_ids):
    mapping = {
        raw: raw
        for raw in raw_ids
        if isinstance(raw, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw)
    }
    occupied = set(mapping)

    def allocate(raw):
        if not isinstance(raw, str) or not raw:
            return None
        if raw in mapping:
            return mapping[raw]
        clean = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
        tail = "_" + hashlib.sha256(raw.encode()).hexdigest()[:8]
        candidate, n = clean[: 64 - len(tail)] + tail, 2
        while candidate in occupied:
            suffix = "_" + str(n)
            candidate = clean[: 64 - len(tail) - len(suffix)] + tail + suffix
            n += 1
        mapping[raw] = candidate
        occupied.add(candidate)
        return candidate

    return mapping, allocate


def sanitize_native_ids(messages):
    fields = []
    for row in messages:
        for block in row.get("content", []) if isinstance(row.get("content"), list) else []:
            kind = block.get("type", "")
            field = (
                "id"
                if kind.endswith("tool_use")
                else "tool_use_id"
                if kind.endswith("tool_result")
                else None
            )
            if field and isinstance(block.get(field), str):
                fields.append((block, field))
    _, allocate = allocator(block[field] for block, field in fields)
    for block, field in fields:
        value = allocate(block[field])
        if value is None:
            raise ValueError(f"Anthropic {block['type']} block has an empty {field}.")
        block[field] = value


def repair_messages(messages):
    rows = copy.deepcopy(messages)
    raw_ids = [
        part.get("id") if part.get("type") == "tool_use" else part.get("tool_use_id")
        for row in rows
        for part in (row.get("content", []) if isinstance(row.get("content"), list) else [])
    ]
    mapping, allocate = allocator(raw_ids)

    def orphan(part):
        content = part.get("content", "")
        label = "[tool_result without adjacent tool_use: " + str(part.get("tool_use_id", "")) + "]"
        if isinstance(content, list) and any(p.get("type") == "image" for p in content):
            return [{"type": "text", "text": label}, *content]
        return [
            {
                "type": "text",
                "text": label
                + "\n"
                + (
                    content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                ),
            }
        ]

    output = []
    i = 0
    while i < len(rows):
        row = rows[i]
        parts = row.get("content", [])
        parts = [{"type": "text", "text": parts or "(empty)"}] if isinstance(parts, str) else parts
        if row.get("role") == "assistant":
            required = []
            clean = []
            for part in parts:
                if part.get("type") == "tool_use":
                    wire = allocate(part.get("id"))
                    if wire is None:
                        clean.append(
                            {
                                "type": "text",
                                "text": "[tool call without representable id] "
                                + json.dumps(part, ensure_ascii=False),
                            }
                        )
                        continue
                    part["id"] = wire
                    required.append(wire)
                clean.append(part)
            if clean:
                output.append({**row, "content": clean})
            if required:
                results, extra, seen = [], [], set()
                j = i + 1
                while j < len(rows):
                    following = rows[j].get("content")
                    if (
                        rows[j].get("role") != "user"
                        or not isinstance(following, list)
                        or not following
                        or any(p.get("type") != "tool_result" for p in following)
                    ):
                        break
                    for part in following:
                        wire = mapping.get(part.get("tool_use_id"))
                        if wire in required and wire not in seen:
                            part["tool_use_id"] = wire
                            results.append(part)
                            seen.add(wire)
                        else:
                            extra.extend(orphan(part))
                    j += 1
                for wire in required:
                    if wire not in seen:
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": wire,
                                "content": "[missing tool_result for this tool_use in history]",
                                "is_error": True,
                            }
                        )
                output.append({"role": "user", "content": [*results, *extra]})
                i = j - 1
        else:
            content = []
            for part in parts:
                content.extend(orphan(part) if part.get("type") == "tool_result" else [part])
            output.append({**row, "content": content or [{"type": "text", "text": "(empty)"}]})
        i += 1
    if not output or output[-1]["role"] == "assistant":
        output.append({"role": "user", "content": [{"type": "text", "text": "(continue)"}]})
    return output
