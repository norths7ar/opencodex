"""Claude subscription Messages wire requirements from the original Anthropic adapter."""

import copy
import hashlib
import platform
from uuid import uuid4

OAUTH_BETA = "claude-code-20250219,oauth-2025-04-20"
USER_AGENT = "claude-cli/2.1.280 (external, cli)"
SYSTEM_IDENTITY = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
BUILTINS = {"web_search", "code_execution", "text_editor", "computer"}


def session_id(access, session_key=None):
    seed = access or "opencodex-anon"
    conversation = session_key.strip() if isinstance(session_key, str) else ""
    source = f"claude-code-session:{seed}"
    if conversation:
        source += f":conv:{conversation}"
    value = hashlib.sha256(source.encode()).hexdigest()
    variant = format((int(value[16], 16) & 3) | 8, "x")
    return f"{value[:8]}-{value[8:12]}-4{value[13:16]}-{variant}{value[17:20]}-{value[20:32]}"


def anthropic_headers(access: str, incoming=None, session_key=None):
    headers = {
        key: value
        for key, value in (incoming or {}).items()
        if key.lower() not in {"authorization", "x-api-key", "host", "content-length"}
    }
    required = {
        "Authorization": f"Bearer {access}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "X-App": "cli",
        "anthropic-dangerous-direct-browser-access": "true",
        "X-Stainless-Retry-Count": "0",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Lang": "js",
        "X-Stainless-Timeout": "600",
        "X-Stainless-Arch": "arm64"
        if platform.machine().lower() in {"arm64", "aarch64"}
        else "x64",
        "X-Stainless-OS": "Windows",
        "X-Stainless-Package-Version": "0.112.1",
        "X-Stainless-Runtime-Version": "v26.3.0",
        "X-Claude-Code-Session-Id": session_id(access, session_key),
        "x-client-request-id": str(uuid4()),
    }
    owned = {key.lower() for key in required}
    headers = {key: value for key, value in headers.items() if key.lower() not in owned}
    headers.update(required)
    return headers


def _wire_name(name):
    if not isinstance(name, str):
        return name
    return (
        name if name.lower() in BUILTINS or name.lower().startswith("custom_") else "custom_" + name
    )


def prepare_anthropic_body(body: dict):
    """Return native Messages body and exact reverse map (including already-prefixed tools)."""
    result = copy.deepcopy(body)
    names = {}

    def encode(name):
        wire = _wire_name(name)
        if isinstance(name, str):
            if wire in names and names[wire] != name:
                raise ValueError("Claude tool names collide after subscription encoding.")
            names[wire] = name
        return wire

    system = result.get("system", [])
    if isinstance(system, str):
        system = [{"type": "text", "text": system}] if system else []
    if not isinstance(system, list):
        raise TypeError("Claude system instructions must be text or text blocks.")
    if not system or not isinstance(system[0], dict) or system[0].get("text") != SYSTEM_IDENTITY:
        system.insert(0, {"type": "text", "text": SYSTEM_IDENTITY})
    result["system"] = system
    for tool in result.get("tools", []):
        if isinstance(tool, dict) and "name" in tool:
            tool["name"] = encode(tool["name"])
    choice = result.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") == "tool":
        choice["name"] = encode(choice.get("name"))
    for message in result.get("messages", []):
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for item in message["content"]:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                item["name"] = encode(item.get("name"))
    return result, names


def restore_anthropic_payload(payload: dict, name_map: dict):
    """Restore JSON or SSE content_block_start names before generic protocol translation."""
    result = copy.deepcopy(payload)

    def visit(value):
        if isinstance(value, dict):
            if value.get("type") == "tool_use" and isinstance(value.get("name"), str):
                name = value["name"]
                value["name"] = name_map.get(name, name)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    return result
