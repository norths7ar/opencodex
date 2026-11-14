"""Resolve original registry-owned authentication and transport during import.

The adjacent projection comes from backend/src/providers/registry.ts. Matching and
precedence follow routed-config.ts, registry.ts and static-model-discovery.ts;
unsupported effective authentication is left for the import preview to explain.
"""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

REGISTRY = {
    row["id"]: row
    for row in json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))
}


def _endpoint(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    parsed = urlsplit(value)
    host = parsed.hostname
    if not host:
        return value.rstrip("/")
    port = parsed.port
    authority = host.lower()
    if ":" in authority:
        authority = f"[{authority}]"
    if port and (parsed.scheme.lower(), port) not in {("https", 443), ("http", 80)}:
        authority += f":{port}"
    if parsed.username is not None:
        authority = parsed.netloc.rsplit("@", 1)[0] + "@" + authority
    return urlunsplit(
        (parsed.scheme.lower(), authority, parsed.path.rstrip("/"), parsed.query, parsed.fragment)
    ).rstrip("/")


def _matches(identity, row, entry):
    if identity == "anthropic2":
        return (
            entry.get("authKind") == "oauth"
            and entry.get("oauthId") == identity
            and entry.get("oauthFamily") == "anthropic"
            and row.get("anthropicOAuthInstance") == identity
            and row.get("adapter") == "anthropic"
            and row.get("authMode") == "oauth"
        )
    exact = identity == "mimo-free" or (
        entry["authKind"] == "key" and entry.get("preserveCustomDestination") is True
    )
    if not exact:
        return True
    allowed = {None, "key", "local"} if identity == "mimo-free" else {None, "key"}
    return (
        not entry.get("allowBaseUrlOverride")
        and not re.search(r"\{[^}]*\}", entry["baseUrl"])
        and row.get("adapter") == entry["adapter"]
        and row.get("authMode") in allowed
        and _endpoint(row.get("baseUrl")) == _endpoint(entry["baseUrl"])
    )


def resolve_legacy_provider(identity: str, raw: dict) -> dict:
    """Return a new row with original effective auth/transport, without resolving secrets."""
    row = dict(raw)
    entry = REGISTRY.get(identity)
    if not entry or not _matches(identity, row, entry):
        row.setdefault("authMode", "key")
        return row
    kind = entry["authKind"]
    key_override = (
        kind == "oauth"
        and entry.get("allowKeyAuthOverride") is True
        and row.get("authMode") == "key"
    )
    if key_override or (identity == "mimo-free" and row.get("authMode") in {None, "local"}):
        mode = "key"
    elif kind in {"forward", "oauth"}:
        mode = kind
    else:
        # Original returns undefined for forward on key/local registry entries.
        mode = row.get("authMode")
        if mode in {None, "forward"}:
            mode = "key"
    user_url = row.get("baseUrl")
    user_url = user_url.strip() if isinstance(user_url, str) else ""
    resolved = bool(user_url) and not re.search(r"\{[^}]*\}", user_url)
    template = re.search(r"\{[^}]*\}", entry["baseUrl"])
    if entry.get("allowBaseUrlOverride") and not resolved:
        raise ValueError("The original provider requires a resolved custom base URL.")
    row.update(
        authMode=mode,
        adapter=entry["adapter"],
        baseUrl=user_url
        if resolved and (template or entry.get("allowBaseUrlOverride"))
        else entry["baseUrl"],
    )
    for key in ("responsesPath", "chatCompletionsPath"):
        if key not in row and key in entry:
            row[key] = entry[key]
    return row
