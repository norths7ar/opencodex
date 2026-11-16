"""Portability of locally stored Responses continuations between ChatGPT accounts."""

from hashlib import sha256
from sqlite3 import Connection

_FILE_SCOPE_MESSAGE = (
    "The previous response belongs to a different or unknown ChatGPT account, and this "
    "conversation references uploaded files. The request was not sent upstream. Start a new "
    "conversation and re-upload the files using the active account. Retrying the same request "
    "will not resolve it."
)
_OPAQUE_COMPACTION_NOTE = (
    "[An earlier context compaction checkpoint could not be read by the active ChatGPT account.]"
)
_COMPACTION_TYPES = {"compaction", "compaction_summary", "context_compaction"}
_PART_FIELDS = ("content", "output")
_TURN_STATE = "x-codex-turn-state"


def ensure_history_issuer(db: Connection) -> None:
    """Leave older history readable, without assuming who issued it."""
    if not any(column[1] == "issuer" for column in db.execute("PRAGMA table_info(responses)")):
        db.execute("ALTER TABLE responses ADD COLUMN issuer TEXT")


def subscription_issuer(account_id: str) -> str:
    """Stable across refresh and reauth without storing the account ID in plaintext."""
    return sha256(f"ocx-chatgpt-history\0{account_id}".encode()).hexdigest()


def _parts(item: dict):
    for key in _PART_FIELDS:
        parts = item.get(key)
        if isinstance(parts, list):
            yield key, parts


def _has_file_reference(items: list) -> bool:
    pending = list(items)
    while pending:
        item = pending.pop()
        if not isinstance(item, dict):
            continue
        file_id = item.get("file_id")
        file_ids = item.get("file_ids")
        if isinstance(file_id, str) and file_id.strip():
            return True
        if isinstance(file_ids, list) and any(isinstance(i, str) and i.strip() for i in file_ids):
            return True
        for _, parts in _parts(item):
            pending.extend(parts)
    return False


def _portable_item(item):
    if not isinstance(item, dict):
        return item
    kind = item.get("type")
    if isinstance(kind, str) and kind in _COMPACTION_TYPES and "encrypted_content" in item:
        return {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": _OPAQUE_COMPACTION_NOTE}],
        }
    value = {key: entry for key, entry in item.items() if key != "encrypted_content"}
    if kind == "reasoning":
        value.pop("id", None)
        value.pop("status", None)
        value.setdefault("summary", [])
        if value.get("content"):
            # ChatGPT accepts reasoning summaries, not raw reasoning text in input.
            value["content"] = []
    return value


def _portable_items(items: list) -> list:
    output = [
        _portable_item(item)
        for item in items
        if not isinstance(item, dict) or item.get("type") != "item_reference"
    ]
    pending = list(output)
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, parts in _parts(item):
                clean_parts = [_portable_item(part) for part in parts]
                item[key] = clean_parts
                pending.extend(clean_parts)
    return output


def scrub_subscription_replay(body: dict) -> dict:
    """Port expanded local history whose issuer differs from the serving account."""
    items = body["input"]
    if _has_file_reference(items):
        raise ValueError(_FILE_SCOPE_MESSAGE)
    value = {**body}
    for key in ("previous_response_id", "conversation"):
        value.pop(key, None)
    metadata = value.get("client_metadata")
    if isinstance(metadata, dict) and _TURN_STATE in metadata:
        value["client_metadata"] = {
            key: entry for key, entry in metadata.items() if key != _TURN_STATE
        }
    value["input"] = _portable_items(items)
    return value
