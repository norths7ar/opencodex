"""Recognize Codex direct, code-mode and deferred collaboration surfaces."""

import re

_HEADING = re.compile(r"^### `([A-Za-z_$][\w$]*)`(?: \(`([^`]+)`\))?$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_V2 = {"send_message", "followup_task", "interrupt_agent", "list_agents"}
_V1 = {"send_input", "resume_agent", "close_agent"}
_SPAWN = "multi_agent_v1__spawn_agent"


def _tools(body):
    for spec in body.get("tools", []):
        if not isinstance(spec, dict):
            continue
        namespace = spec.get("name", "") if spec.get("type") == "namespace" else ""
        for tool in spec.get("tools", []) if namespace else [spec]:
            if isinstance(tool, dict):
                yield namespace, tool.get("function", tool)


def _opaque_end(text, start):
    quote = text[start]
    if text.startswith("//", start):
        match = re.search(r"[\n\r\u2028\u2029]", text[start + 2 :])
        return start + 2 + match.start() if match else None
    if text.startswith("/*", start):
        end = text.find("*/", start + 2)
        return end + 1 if end >= 0 else None
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == quote:
            return index
        else:
            index += 1
    return None


def _opaque(text, index):
    return text[index] in "\"'`" or text.startswith(("//", "/*"), index)


def _declaration(text):
    prefix, suffix = "declare const tools: { ", ">; };"
    if len(text) > 65536 or not text.startswith(prefix) or not text.endswith(suffix):
        return None
    signature = text[len(prefix) : -len(suffix)]
    name, separator, rest = signature.partition("(")
    if not separator or not re.fullmatch(r"[A-Za-z_$][\w$]*", name):
        return None
    typed = 6 if rest.startswith("args: ") else 7 if rest.startswith("input: ") else -1
    if typed < 0 or len(rest) <= typed or rest[typed].isspace():
        return None
    depth, content, close, index = 1, False, -1, typed
    while index < len(rest):
        char = rest[index]
        if _opaque(rest, index):
            content = content or char != "/"
            end = _opaque_end(rest, index)
            if end is None:
                return None
            index = end + 1
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if depth == 0:
            close = index
            break
        content = content or not char.isspace()
        index += 1
    separator = "): Promise<"
    if close < 0 or not content or not rest.startswith(separator, close):
        return None
    index = close + len(separator)
    if index >= len(rest) or rest[index] == ">" or rest[index].isspace():
        return None
    generic = 1
    while index < len(rest):
        if _opaque(rest, index):
            end = _opaque_end(rest, index)
            if end is None:
                return None
            index = end + 1
            continue
        if rest[index] == "<":
            generic += 1
        elif rest[index] == ">" and rest[index - 1] != "=":
            generic -= 1
        if generic == 0:
            return None
        index += 1
    return name if generic == 1 else None


def _closes(line, fence):
    match = _CLOSE.match(line)
    return bool(match and match[1][0] == fence[0] and len(match[1]) >= len(fence))


def _rendered(description):
    if len(description) > 1 << 20:
        return []
    lines = description.split("\n")
    pending, rendered = [], []
    fence = None
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if fence:
            if _closes(line, fence):
                fence = None
            continue
        heading = _HEADING.match(line)
        if heading:
            name, raw = heading[1], heading[2] or heading[1]
            normalized = (
                "".join(
                    char
                    if re.fullmatch(r"[A-Za-z_$]" if i == 0 else r"[A-Za-z0-9_$]", char)
                    else "_"
                    for i, char in enumerate(raw)
                )
                or "_"
            )
            if heading[2] is None or raw != name and normalized == name:
                pending.append((name, raw))
            continue
        if line == "exec tool declaration:" and index < len(lines) and lines[index] == "```ts":
            close = index + 1
            while close < len(lines) and not _closes(lines[close], "```"):
                close += 1
            if close == len(lines):
                break
            declared = _declaration("\n".join(lines[index + 1 : close]))
            owner = next((i for i, row in enumerate(pending) if row[0] == declared), None)
            if owner is not None:
                rendered.append(pending[owner][1])
                del pending[owner:]
            index = close + 1
            continue
        opened = _FENCE.match(line)
        if opened and not (opened[1][0] == "`" and "`" in opened[2]):
            fence = opened[1]
    return rendered


def _foreign(description):
    names = []
    for line in description.split("\n"):
        heading = _HEADING.match(line)
        if heading:
            names.extend(value for value in heading.groups() if value)
        names.extend(re.findall(r"declare const tools: \{ ([A-Za-z_$][\w$]*)\(", line))
    return any(
        not name.startswith("mcp__")
        and name != _SPAWN
        and any(name == tool or name.endswith("__" + tool) for tool in {"spawn_agent", *_V2})
        for name in names
    )


def _fragment(text):
    text = text.strip()
    if not text.startswith("<tools>"):
        return None
    if not text.endswith("</tools>"):
        return False
    groups = {
        "Deferred tool namespaces:": "snapshot",
        "Added deferred tool namespaces:": "added",
        "Removed deferred tool namespaces:": "removed",
    }
    rows = {"snapshot": set(), "added": set(), "removed": set()}
    seen = set()
    group, has_entries, empty, omitted = None, True, False, False
    for raw in text[len("<tools>") : -len("</tools>")].split("\n"):
        line = raw.removesuffix("\r")
        if not line:
            continue
        if empty:
            return False
        if line in groups:
            next_group = groups[line]
            if next_group in seen or not has_entries or next_group == "added" and "removed" in seen:
                return False
            seen.add(next_group)
            group, has_entries = next_group, False
            continue
        if line == "No deferred tool namespaces remain.":
            if not has_entries:
                return False
            empty = True
            continue
        if group is None:
            return False
        has_entries = True
        if re.fullmatch(r"\.\.\. \d+ additional namespaces omitted\.", line):
            omitted = omitted or group == "removed"
            continue
        match = re.fullmatch(r"- ([^:\s]+)(?:: [\s\S]*)?", line)
        if not match:
            return False
        rows[group].add(match[1])
    if (
        not has_entries
        or "snapshot" in seen
        and len(seen) > 1
        or not seen
        and not empty
        or rows["added"] & rows["removed"]
    ):
        return False
    return rows, "snapshot" in seen, empty, omitted


def _deferred(body):
    current = set()
    items = body.get("input", [])
    if not isinstance(items, list):
        return False
    for message in items:
        if not isinstance(message, dict) or message.get("role") != "developer":
            continue
        content = message.get("content", [])
        texts = (
            [content]
            if isinstance(content, str)
            else [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in {"text", "input_text"}
            ]
        )
        for text in texts:
            fragment = _fragment(text)
            if fragment is None:
                continue
            if fragment is False:
                current.clear()
                continue
            rows, snapshot, empty, omitted = fragment
            if snapshot or omitted:
                current.clear()
            current.update(rows["snapshot"] if snapshot else rows["added"])
            current.difference_update(rows["removed"])
            if empty:
                current.clear()
    return "multi_agent_v1" in current


def collaboration_surface(body):
    tools = list(_tools(body))
    names, spawn_shapes = set(), set()
    for namespace, tool in tools:
        name = tool.get("name", "")
        if namespace.startswith("mcp__") or name.startswith("mcp__"):
            continue
        names.add(name)
        if name == "spawn_agent":
            spawn_shapes.add(bool(namespace))
    direct = None
    if len(spawn_shapes) == 1 and not (names & _V1 and names & _V2):
        direct = "v1" if names & _V1 or (not names & _V2 and True in spawn_shapes) else "v2"
    bare = {tool.get("name") for namespace, tool in tools if not namespace}
    genuine, rendered, foreign = False, False, False
    if "exec" in bare and not bare & {"exec_command", "shell_command"}:
        for namespace, tool in tools:
            description = tool.get("description", "")
            if (
                namespace
                or tool.get("name") != "exec"
                or tool.get("type") != "custom"
                or not description.startswith(
                    "Run JavaScript code to orchestrate/compose tool calls"
                )
            ):
                continue
            genuine = True
            rendered = rendered or _SPAWN in _rendered(description)
            foreign = foreign or _foreign(description)
    searchable = genuine or any(
        not namespace and tool.get("type") == "tool_search" for namespace, tool in tools
    )
    inferred = rendered or searchable and _deferred(body)
    if inferred:
        native_tools = names & ({"spawn_agent"} | _V1 | _V2)
        return "v1" if not foreign and (not native_tools or direct == "v1") else None
    return direct
