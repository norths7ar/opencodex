"""Shared tool identities and Responses tool declaration conversion."""

import copy
import uuid


def identifier(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


def response_tools(body):
    """Flatten the original Responses tool identities, retaining their return addresses."""
    flattened, identities = [], {}
    declarations = list(body.get("tools", []))
    if isinstance(body.get("input"), list):
        for item in body["input"]:
            if item.get("type") in {"additional_tools", "tool_search_output"}:
                declarations.extend(item.get("tools", []))
    declared_names = {
        (child.get("function") or child).get("name")
        for spec in declarations
        for child in (spec.get("tools", []) if spec.get("type") == "namespace" else [spec])
        if child.get("type") != "tool_search"
    }
    search_name = "tool_search"
    if search_name in declared_names:
        search_name = "opencodex_tool_search"
        suffix = 2
        while search_name in declared_names:
            search_name = f"opencodex_tool_search_{suffix}"
            suffix += 1
    for spec in declarations:
        namespace = spec.get("name") if spec.get("type") == "namespace" else None
        namespace = None if namespace == "functions" else namespace
        children = spec.get("tools", []) if spec.get("type") == "namespace" else [spec]
        for child in children:
            tool = copy.deepcopy(child)
            if tool.get("type") == "tool_search":
                tool = {
                    "type": "function",
                    "name": search_name,
                    "description": tool.get(
                        "description", "Search for additional tools to load for the next turn."
                    ),
                    "parameters": tool.get(
                        "parameters",
                        {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "Search query for tools to load.",
                                },
                                "limit": {
                                    "type": "number",
                                    "description": "Maximum number of tools to return.",
                                },
                            },
                            "required": ["query"],
                        },
                    ),
                    "_tool_search": True,
                }
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                tool = {"type": "function", **tool["function"]}
            if not isinstance(tool.get("name"), str):
                flattened.append(tool)
                continue
            name = tool["name"]
            wire = f"{namespace}__{name}" if namespace else name
            identity = {
                "name": name,
                **({"namespace": namespace} if namespace else {}),
                "custom": tool.get("type") == "custom",
                "search": tool.pop("_tool_search", False),
            }
            if wire in identities and identities[wire] != identity:
                raise ValueError("Ambiguous tool catalog.")
            identities[wire] = identity
            tool["name"] = wire
            flattened.append(tool)
    # Original providers can echo ns.name or an unambiguous bare name. Neither alias
    # may shadow another declared identity; helper names never gain a bare alias.
    aliases = {}
    helpers = {
        "exec",
        "apply_patch",
        "exec_command",
        "write_stdin",
        "shell_command",
        "view_image",
        "create_goal",
        "get_goal",
        "update_goal",
    }
    for identity in identities.values():
        if "namespace" not in identity:
            continue
        candidates = [f"{identity['namespace']}.{identity['name']}"]
        if identity["name"] not in helpers:
            candidates.append(identity["name"])
        for alias in candidates:
            if alias in aliases and aliases[alias] != identity:
                aliases[alias] = None
            elif alias not in aliases:
                aliases[alias] = identity
    for alias, identity in aliases.items():
        if identity and alias not in identities:
            identities[alias] = identity
    return flattened, identities


def response_tool_identity(name, tool_names=None):
    identity = (tool_names or {}).get(name, {"name": name})
    return {key: value for key, value in identity.items() if key in {"name", "namespace"}}
