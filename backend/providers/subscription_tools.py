"""Tool compatibility for native ChatGPT Responses and compact requests."""

import re
from collections import defaultdict

_SCHEMA_NAME_BAGS = {
    "properties",
    "patternProperties",
    "$defs",
    "definitions",
    "dependencies",
    "dependentSchemas",
    "dependentRequired",
}
_PRESERVED_PATTERN_SUBTREES = {
    "patternProperties",
    "not",
    "oneOf",
    "if",
    "contains",
    "$defs",
    "definitions",
    "const",
    "default",
    "enum",
    "examples",
}
_LEGACY_PREFIX = "default."
_CALL_TYPES = ("function_call", "custom_tool_call")
_VALID_NAME = re.compile(r"[a-zA-Z0-9_-]+")
_CODE_MODE_HELPERS = {
    "exec_command",
    "shell_command",
    "write_stdin",
    "apply_patch",
    "view_image",
    "create_goal",
    "get_goal",
    "update_goal",
}


def _tool_groups(body):
    if isinstance(body.get("tools"), list):
        yield body["tools"]
    if isinstance(body.get("input"), list):
        for item in body["input"]:
            if (
                isinstance(item, dict)
                and item.get("type") == "additional_tools"
                and isinstance(item.get("tools"), list)
            ):
                yield item["tools"]


def _wire_name(tool):
    if not isinstance(tool, dict):
        return None
    name = tool.get("name")
    if isinstance(name, str) and name:
        return name
    function = tool.get("function")
    if tool.get("type") == "function" and isinstance(function, dict):
        name = function.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def _repair_legacy_replay(body, groups):
    items = body.get("input")
    if not isinstance(items, list):
        return
    candidates = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("type") in _CALL_TYPES
        and "namespace" not in item
        and isinstance(item.get("name"), str)
        and item["name"].startswith(_LEGACY_PREFIX)
    ]
    if not candidates:
        return

    spellings = set()
    owners = defaultdict(set)
    for group in groups:
        for tool in group:
            if not isinstance(tool, dict):
                continue
            children = tool.get("tools")
            if tool.get("type") == "namespace" and isinstance(children, list):
                namespace = tool.get("name")
                if not isinstance(namespace, str):
                    continue
                for child in children:
                    if name := _wire_name(child):
                        if namespace:
                            spellings.update((f"{namespace}__{name}", f"{namespace}.{name}"))
                        else:
                            spellings.add(name)
                        owners[name].add(namespace)
            elif name := _wire_name(tool):
                spellings.add(name)
                owners[name].add(None)

    for item in candidates:
        name = item["name"]
        suffix = name.removeprefix(_LEGACY_PREFIX)
        if name in spellings or _VALID_NAME.fullmatch(suffix) is None:
            continue
        declared_owners = owners.get(suffix)
        if (declared_owners is not None and len(declared_owners) == 1) or (
            declared_owners is None and suffix in _CODE_MODE_HELPERS
        ):
            item["name"] = suffix


def _compatible_pattern(pattern):
    """Return None for an unsupported Unicode property escape, or rewrite bare NUL escapes."""
    rewritten = []
    copied = 0
    index = 0
    while (index := pattern.find("\\", index)) != -1:
        escape = pattern[index + 1 : index + 3]
        if escape in ("p{", "P{"):
            return None
        if escape.startswith("0") and (len(escape) == 1 or escape[1] not in "0123456789"):
            rewritten.extend((pattern[copied:index], r"\x00"))
            copied = index + 2
        # Skip the escaped character, including another backslash.
        index += 2
    if not rewritten:
        return pattern
    rewritten.append(pattern[copied:])
    return "".join(rewritten)


def _normalize_function(tool):
    # Keep schema-key names distinct from caller-defined property names and literal values.
    # The explicit traversal stack handles caller-controlled nesting without recursion.
    stack = [(iter(tool.items()), False, tool, False)]
    while stack:
        entries, in_name_bag, container, drop_pattern = stack[-1]
        entry = next(entries, None)
        if entry is None:
            stack.pop()
            if drop_pattern:
                del container["pattern"]
            continue
        key, value = entry
        if not in_name_bag:
            if key == "pattern" and isinstance(value, str):
                compatible = _compatible_pattern(value)
                if compatible is None:
                    # Deleting while iterating a dict invalidates the iterator. A frame has only
                    # one pattern key; remove it when that frame finishes instead.
                    stack[-1] = (entries, in_name_bag, container, True)
                elif compatible != value:
                    container[key] = compatible
                continue
            if key in _PRESERVED_PATTERN_SUBTREES:
                continue
        if isinstance(value, dict):
            stack.append(
                (iter(value.items()), not in_name_bag and key in _SCHEMA_NAME_BAGS, value, False)
            )
        elif isinstance(value, list):
            stack.append((enumerate(value), False, value, False))

    parameters = tool.get("parameters")
    if isinstance(parameters, dict):
        parameters["type"] = "object"
    else:
        tool["parameters"] = {"type": "object"}


def normalize_subscription_tools(body, *, compact=False):
    """Update a private outgoing body; leave native namespace and custom tools intact."""
    groups = list(_tool_groups(body))
    _repair_legacy_replay(body, groups)
    if not compact:
        for group in groups:
            for tool in group:
                if isinstance(tool, dict) and tool.get("type") == "function":
                    _normalize_function(tool)
