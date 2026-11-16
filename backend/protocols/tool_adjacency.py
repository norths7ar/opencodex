"""Normalize unambiguous Responses call/result batches for upstreams requiring adjacency."""


def _positions(items):
    calls, outputs = {}, {}
    for index, item in enumerate(items):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("call_id"), str)
            or not item["call_id"]
        ):
            continue
        kind = item.get("type")
        if kind in {"function_call", "local_shell_call", "custom_tool_call"}:
            target = calls
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            target = outputs
        else:
            continue
        key = ("custom" if kind.startswith("custom") else "function", item["call_id"])
        target.setdefault(key, []).append(index)
    return calls, outputs


def adjacent_results(items):
    """Merge readable repeated outputs, then move only globally unambiguous batches."""
    calls, outputs = _positions(items)
    merged, dropped = {}, set()
    for key, indexes in outputs.items():
        positions = calls.get(key, [])
        if len(indexes) < 2 or len(positions) != 1 or indexes[0] < positions[0]:
            continue
        pieces = [items[index].get("output") for index in indexes]
        if not all(isinstance(piece, (str, list)) for piece in pieces):
            continue
        if all(isinstance(piece, str) for piece in pieces):
            joined = "\n\n".join(piece for piece in pieces if piece)
        else:
            joined = []
            for piece in pieces:
                joined.extend(
                    piece
                    if isinstance(piece, list)
                    else [{"type": "input_text", "text": piece}]
                    if piece
                    else []
                )
        merged[indexes[0]] = {**items[indexes[0]], "output": joined}
        dropped.update(indexes[1:])
    items = [merged.get(index, item) for index, item in enumerate(items) if index not in dropped]
    calls, outputs = _positions(items)
    pairs = []
    for key in calls.keys() | outputs.keys():
        a, b = calls.get(key, []), outputs.get(key, [])
        if len(a) != 1 or len(b) != 1 or b[0] <= a[0]:
            return items
        pairs.append((a[0], b[0]))
    pairs.sort()
    batches, moved = {}, set()
    cursor = 0
    while cursor < len(pairs):
        group = [pairs[cursor]]
        first_output = pairs[cursor][1]
        cursor += 1
        while cursor < len(pairs) and pairs[cursor][0] < first_output:
            group.append(pairs[cursor])
            first_output = min(first_output, pairs[cursor][1])
            cursor += 1
        if any(group[i][1] < group[i - 1][1] for i in range(1, len(group))):
            return items
        indexes = [pair[0] for pair in group] + [pair[1] for pair in group]
        anchor = group[0][0]
        if indexes != list(range(anchor, anchor + len(indexes))):
            batches[anchor] = [items[index] for index in indexes]
            moved.update(indexes)
    result = []
    for index, item in enumerate(items):
        result.extend(batches.get(index, []))
        if index not in moved:
            result.append(item)
    return result
