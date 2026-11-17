"""DSH's original managed provider fragment, with YAML-preserving apply and rollback.

Desktop profile patches supersede the legacy settings file. Only the OpenCodex
provider belongs to this integration; other loaders, providers and comments survive.
"""

import copy
import hashlib
import inspect
import io
import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.emitter import RoundTripEmitter
from ruamel.yaml.error import CommentMark, YAMLError
from ruamel.yaml.tokens import CommentToken

from backend.platform.files import instance_lock

from .files import ClientConfigError, atomic_write, read_text

LEGACY_PATH = "llm-pi-ai.providers.opencodex"
PROFILE_PATH = "[id=llm-pi-ai].config.providers.opencodex"
LIMIT = 16 << 20


class _BoundaryComment(CommentToken):
    """A standalone comment reattached as leading trivia for a surviving item."""


class _DshEmitter(RoundTripEmitter):
    def write_comment(self, comment, pre=False):
        if pre and isinstance(comment, _BoundaryComment):
            # The emitter normalizes indentation. Use the current block's output
            # indent, not the column of the original comment's discarded owner.
            comment.start_mark.column = self.indent or 0
        super().write_comment(comment, pre)


def _digest(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest() if source is not None else None


class DshError(RuntimeError):
    def __init__(self, message, reason="unsafe"):
        super().__init__(message)
        self.reason = reason


def provider_block(rows, base_url):
    models = []
    for row in sorted(rows, key=lambda item: item.get("namespaced", "")):
        if row.get("selected") is False or row.get("hidden") is True or row.get("disabled"):
            continue
        declared = row.get("inputModalities") or []
        modalities = list(dict.fromkeys(value for value in declared if value in {"text", "image"}))
        if declared and not modalities and all(value == "audio" for value in declared):
            continue
        model_id = row.get("namespaced") or f"{row['provider']}/{row['id']}"
        model = {
            "id": model_id,
            "name": f"{row.get('displayName') or row.get('id') or model_id} ({'native' if row.get('native') else row.get('provider', 'routed')})",
            "input": modalities or ["text"],
        }
        context = row.get("contextWindow")
        if isinstance(context, int) and not isinstance(context, bool) and context > 0:
            model["contextWindow"] = context
            output = row.get("maxOutputTokens") or row.get("maxTokens")
            if isinstance(output, int) and not isinstance(output, bool) and output > 0:
                model["maxTokens"] = min(output, context)
        offered = row.get("reasoningEfforts") or []
        efforts = {
            value: value for value in ("low", "medium", "high", "xhigh", "max") if value in offered
        }
        if "max" not in efforts and "ultra" in offered:
            efforts["max"] = "ultra"
        if efforts:
            model["reasoningEfforts"] = efforts
        models.append(model)
    return {
        "displayName": "OpenCodex Lite",
        "api": "openai-responses",
        "baseURL": base_url,
        "headers": {"Authorization": "Bearer ocx_data_dsh"},
        "models": models,
    }


def _yaml():
    parser = YAML()
    parser.preserve_quotes = True
    parser.width = 4096
    return parser


def _parse(text, profile):
    try:
        data = _yaml().load(text) if text is not None else ([] if profile else {})
    except YAMLError, ValueError, RecursionError:
        raise DshError("DSH configuration is not valid YAML.", "unparseable") from None
    if data is None and not profile:
        data = {}
    if profile:
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise DshError(
                "DSH profile patch must be a list of loader mappings.", "blocked-container"
            )
    elif not isinstance(data, dict):
        raise DshError("DSH settings must be a mapping.", "blocked-container")
    return data


def _container(data, profile, create=False, *, path=None):
    if profile:
        matched = [row for row in data if row.get("id") == "llm-pi-ai"]
        if len(matched) > 1:
            raise DshError("DSH profile contains multiple llm-pi-ai rows.", "ambiguous-selector")
        if not matched:
            if not create:
                return None
            data.append({"id": "llm-pi-ai", "config": {}})
            matched = [data[-1]]
        cursor = matched[0]
        if path is not None:
            path.append((data, next(i for i, row in enumerate(data) if row is cursor)))
        parts = ("config", "providers")
    else:
        cursor, parts = data, ("llm-pi-ai", "providers")
    for part in parts:
        if isinstance(cursor, CommentedMap) and cursor.yaml_anchor():
            raise DshError(
                "DSH provider container uses a YAML anchor; edit it into an independent mapping before applying.",
                "blocked-container",
            )
        if part not in cursor:
            if not create:
                return None
            cursor[part] = {}
        if path is not None:
            path.append((cursor, part))
        cursor = cursor[part]
        if not isinstance(cursor, dict):
            raise DshError("DSH provider container is not a mapping.", "blocked-container")
    if isinstance(cursor, CommentedMap) and cursor.yaml_anchor():
        raise DshError("DSH provider container uses a YAML anchor.", "blocked-container")
    return cursor


def _fragment(data, profile):
    container = _container(data, profile)
    return copy.deepcopy(container.get("opencodex")) if container is not None else None


def _owned_comment_tokens(node):
    seen = set()
    stack = [node]
    while stack:
        item = stack.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, CommentToken):
            yield item
            continue
        comments = getattr(item, "ca", None)
        if comments is not None:
            stack.extend((comments.comment, comments.end, *comments.items.values()))
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)


def _boundary_comments(container, source, *, remove):
    """Recover standalone comments ruamel attached to the owned value at an outer indent."""
    location = getattr(container, "lc", None)
    if not source or location is None or "opencodex" not in (location.data or {}):
        return []
    key_line, key_column = location.key("opencodex")
    lines = source.splitlines()
    key_comments = container.ca.items.get("opencodex")
    discarded = (key_comments or []) if remove else (key_comments or [])[2:]
    boundary = {}
    for token in _owned_comment_tokens([container["opencodex"], discarded]):
        line_no = token.start_mark.line
        for part in token.value.splitlines():
            if not part.strip():
                continue
            # A token starts at its first '#', but may combine an inline comment and
            # subsequent standalone comments. Read original lines for their true indent.
            while line_no < len(lines) and not lines[line_no].strip():
                line_no += 1
            if line_no >= len(lines):
                break
            line = lines[line_no]
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            if line_no != key_line and stripped.startswith("#") and indent <= key_column:
                boundary[line_no] = _BoundaryComment(stripped + "\n", CommentMark(0))
            line_no += 1
    if key_comments and not remove:
        key_comments[2:] = [None, None]
    return [boundary[line] for line in sorted(boundary)]


def _edit_fragment(data, profile, source, block):
    path = []
    container = _container(data, profile, block is not None, path=path)
    if container is None:
        return []
    comments = _boundary_comments(container, source, remove=block is None)
    if block is None:
        container.pop("opencodex", None)
        if isinstance(container, CommentedMap):
            container.ca.items.pop("opencodex", None)
    else:
        container["opencodex"] = copy.deepcopy(block)
    if not comments:
        return []
    for parent, key in reversed([*path, (container, "opencodex")]):
        # Deletion removes the key from iteration, but ruamel retains its original
        # location. Find the next surviving item at this level or an outer one.
        if isinstance(parent, dict):
            # Boundary comments only come from ruamel-loaded nodes, not new plain mappings.
            mapping = cast(CommentedMap, parent)
            if key not in (mapping.lc.data or {}):
                continue
            line = mapping.lc.key(key)[0]
            siblings = (
                other
                for other in parent
                if other in mapping.lc.data and mapping.lc.key(other)[0] > line
            )
        else:
            # Sequence path entries are created with enumerate() in _container.
            siblings = iter(range(cast(int, key) + 1, len(parent)))
        for next_key in siblings:
            slots = cast(CommentedMap | CommentedSeq, parent).ca.items.setdefault(
                next_key, [None, None, None, None]
            )
            slots[1] = comments + (slots[1] or [])
            return []
    return comments


def _render(data, original, tail_comments=()):
    stream = io.StringIO()
    yaml = _yaml()
    yaml.Emitter = _DshEmitter
    yaml.dump(data, stream)
    rendered = stream.getvalue() + "".join(token.value for token in tail_comments)
    if original and "\r\n" in original and original.count("\n") == original.count("\r\n"):
        rendered = rendered.replace("\r\n", "\n").replace("\n", "\r\n")
    return rendered


@contextmanager
def _writer_lock(path, profile):
    # DSH uses proper-lockfile: profile writes lock package.json, legacy writes settings.yaml.
    target = path.parent / "package.json" if profile else path
    lock = target.with_name(target.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError:
        raise DshError(
            "DSH is editing its configuration. Retry when that operation completes."
        ) from None
    try:
        yield
    finally:
        lock.rmdir()


class DshIntegration:
    def __init__(self, directory, port, model_rows, home):
        self.directory = Path(directory)
        self.home = Path(home).expanduser().resolve()
        self.base_url = f"http://127.0.0.1:{port}/v1"
        self.model_rows = model_rows
        self.state_path = self.directory / "clients" / "dsh-state.json"

    async def block(self):
        rows = self.model_rows()
        if inspect.isawaitable(rows):
            rows = await rows
        return provider_block(rows, self.base_url)

    async def sync(self) -> dict:
        """Refresh an owned provider fragment; never acquire ownership automatically."""
        block = await self.block()
        state, _, _, _, _, current, reason = self._inspect(block)
        if not state.get("owned"):
            return {"ok": True, "status": "skipped", "changed": False}
        if current == "conflict":
            return {
                "ok": False,
                "status": "needsAttention",
                "changed": False,
                "reason": reason,
                "message": "DSH configuration changed outside Lite; review it before synchronizing.",
            }
        plan = self.preview("apply", block)
        if not plan["canApply"]:
            return {
                "ok": False,
                "status": "needsAttention",
                "changed": False,
                "reason": plan.get("refusalReason", "unsafe"),
            }
        result = self.mutate("apply", block, plan["fingerprint"])
        return {**result, "status": "applied" if result["ok"] else "needsAttention"}

    def _state(self):
        text = read_text(self.state_path, 256 << 20, "DSH integration state")
        return json.loads(text) if text else {"operations": [], "owned": None}

    def _save(self, state):
        atomic_write(
            self.state_path,
            json.dumps(state, ensure_ascii=False, indent=2),
            "DSH integration state",
        )

    def _target(self):
        profile = self.home / "profiles" / "desktop" / "cordis.patch.yml"
        if profile.exists():
            return profile, True
        if (profile.parent / "package.json").exists():
            raise DshError(
                f"Create {profile} containing [] before enabling DSH integration.", "missing-store"
            )
        return self.home / "settings.yaml", False

    def _read_target(self, path, profile):
        source = read_text(path, LIMIT, "DSH configuration")
        return source, _parse(source, profile)

    def _recover(self, state):
        pending = state.get("pending")
        if not pending:
            return
        source = read_text(Path(pending["path"]), LIMIT, "DSH configuration")
        if source == pending["after"]:
            state.update(pending["next"])
        elif source != pending["before"]:
            raise DshError(
                "DSH configuration changed during an interrupted operation; resolve it before retrying.",
                "foreign-edit",
            )
        del state["pending"]
        self._save(state)

    def _inspect(self, block):
        state = self._state()
        self._recover(state)
        path, profile = self._target()
        source, data = self._read_target(path, profile)
        fragment = _fragment(data, profile)
        owned = state.get("owned")
        reason = None
        if owned and owned["path"] != str(path):
            raise DshError(
                "DSH moved to another configuration store; restore the recorded integration before applying the new store.",
                "foreign-edit",
            )
        if owned:
            if fragment != owned["fragment"]:
                status, reason = "conflict", "foreign-edit"
            else:
                status = "current" if fragment == block else "stale"
        elif fragment is not None:
            status, reason = "conflict", "unowned-key"
        else:
            status = "absent"
        return state, path, profile, source, data, status, reason

    def status(self, block):
        operations = self._state().get("operations", [])
        result = {
            "clientId": "dsh",
            "installed": self.home.is_dir(),
            "snapshotCount": sum(row.get("snapshot", "stored") == "stored" for row in operations),
            "retentionDegraded": False,
        }
        try:
            state, path, _, _, _, status, reason = self._inspect(block)
            result.update(state=status, configPath=str(path))
            if reason:
                result["reason"] = reason
            if state.get("owned"):
                result.update(lastOpId=state["owned"]["opId"], appliedAt=state["owned"]["at"])
        except (DshError, ClientConfigError) as exc:
            result.update(
                state="unsafe",
                reason=getattr(exc, "reason", "not-regular-file"),
                configPath=str(self.home / "settings.yaml"),
                error=str(exc),
            )
            if getattr(exc, "reason", None) == "missing-store":
                result.update(
                    reason="blocked-container",
                    supersededBy=str(self.home / "profiles" / "desktop" / "cordis.patch.yml"),
                    supersededReason="missing-store",
                    missingStoreDocument="[]",
                )
        return result

    def journal(self):
        rows = self._state().get("operations", [])
        return [
            {
                key: value
                for key, value in row.items()
                if key in {"opId", "clientId", "kind", "at", "configPath"}
            }
            | {
                "snapshot": row.get("snapshot", "stored"),
                "undoable": row.get("snapshot") != "expired",
                "deletable": index != len(rows) - 1,
            }
            for index, row in reversed(list(enumerate(rows)))
        ]

    def _plan(self, operation, block, op_id=None, confirm_drift=False):
        refusal = None
        foreign = "none"
        if operation == "restore":
            state = self._state()
            self._recover(state)
            owned = state.get("owned")
            row = next((row for row in state["operations"] if row["opId"] == op_id), None)
            if row is None or row.get("snapshot") == "expired":
                raise DshError("DSH rollback snapshot is unavailable.", "snapshot_expired")
            path = Path(row["configPath"])
            profile = row.get("profile", path.name == "cordis.patch.yml")
            source = read_text(path, LIMIT, "DSH configuration")
            status = "current" if owned else "absent"
            if _digest(source) != row.get("afterDigest", _digest(row.get("after"))):
                foreign = "drift"
                if not confirm_drift:
                    refusal = "drift_requires_confirm"
            after, after_owned = row["before"], row["beforeOwned"]
        else:
            state, path, profile, source, data, status, reason = self._inspect(block)
            owned = state.get("owned")
            after_owned = owned
            if not self.home.is_dir():
                refusal = "not_installed"
            if status == "conflict":
                foreign = "unowned" if reason == "unowned-key" else "foreign-edit"
                if operation != "overwrite":
                    refusal = "conflict"
            if operation == "disable":
                if owned:
                    comments = _edit_fragment(data, profile, source, None)
                    after_owned = None
                    after = _render(data, source, comments)
                else:
                    after = source
            else:
                comments = _edit_fragment(data, profile, source, block)
                after = _render(data, source, comments)
                after_owned = {"path": str(path), "fragment": block}
        will_change = source != after or (operation == "overwrite" and not owned)
        fingerprint = hashlib.sha256(
            json.dumps(
                [operation, str(path), source, after, state, op_id, confirm_drift], sort_keys=True
            ).encode()
        ).hexdigest()[:32]
        plan = {
            "version": 1,
            "clientId": "dsh",
            "operation": operation,
            "state": status,
            "foreignEdit": foreign,
            "changes": [
                {
                    "kind": "snapshot"
                    if operation == "restore"
                    else "remove"
                    if operation == "disable"
                    else "replace"
                    if owned
                    else "add",
                    "path": "$snapshot"
                    if operation == "restore"
                    else PROFILE_PATH
                    if profile
                    else LEGACY_PATH,
                }
            ]
            if will_change
            else [],
            "fingerprint": f"p1:{fingerprint}",
            "canApply": refusal is None,
            "willChange": will_change,
        }
        if refusal:
            plan["refusalReason"] = refusal
        return plan, state, path, profile, source, after, after_owned

    def preview(self, operation, block, op_id=None, confirm_drift=False):
        try:
            return self._plan(operation, block, op_id, confirm_drift)[0]
        except (DshError, ClientConfigError) as exc:
            reason = getattr(exc, "reason", "unsafe")
            plan = {
                "version": 1,
                "clientId": "dsh",
                "operation": operation,
                "state": "unsafe",
                "foreignEdit": "none",
                "changes": [],
                "fingerprint": "p1:unbound",
                "canApply": False,
                "willChange": False,
                "refusalReason": "snapshot_expired"
                if reason == "snapshot_expired"
                else "superseded_store"
                if reason == "missing-store"
                else "unsafe",
            }
            if reason == "missing-store":
                plan.update(supersededReason="missing-store", missingStoreDocument="[]")
            return plan

    def mutate(self, operation, block, fingerprint, op_id=None, confirm_drift=False):
        with instance_lock(self.directory, "dsh-integration.lock"):
            plan, state, path, profile, before, after, owned = self._plan(
                operation, block, op_id, confirm_drift
            )
            with _writer_lock(path, profile):
                # The external writer lock and preview fingerprint bind the reviewed file.
                locked_path = path
                current = self._plan(operation, block, op_id, confirm_drift)
                plan, state, path, profile, before, after, owned = current
                if path != locked_path or fingerprint != plan["fingerprint"]:
                    return {"ok": False, "code": "integration_preview_stale", "plan": plan}
                if not plan["canApply"]:
                    raise DshError(
                        "The reviewed DSH operation cannot be applied.", plan["refusalReason"]
                    )
                if not plan["willChange"]:
                    return {
                        "ok": True,
                        "clientId": "dsh",
                        "changed": False,
                        "state": plan["state"],
                        "message": "DSH configuration is already up to date.",
                    }
                now = datetime.now(UTC).isoformat()
                new_id = uuid.uuid4().hex
                if owned is not None:
                    owned = {**owned, "opId": new_id, "at": now}
                row = {
                    "opId": new_id,
                    "clientId": "dsh",
                    "kind": operation,
                    "at": now,
                    "configPath": str(path),
                    "profile": profile,
                    "before": before,
                    "afterDigest": _digest(after),
                    "snapshot": "none" if before is None else "stored",
                    "beforeOwned": state.get("owned"),
                }
                operations = [*state["operations"], row]
                # Original integration journal retains ten actual backups per client;
                # operations that created a previously absent file do not consume a slot.
                stored = [item for item in operations if item.get("snapshot", "stored") == "stored"]
                for expired in stored[:-10]:
                    expired["snapshot"] = "expired"
                    for key in ("before", "after", "beforeOwned"):
                        expired.pop(key, None)
                next_state = {"operations": operations, "owned": owned}
                state["pending"] = {
                    "path": str(path),
                    "before": before,
                    "after": after,
                    "next": next_state,
                }
                self._save(state)
                if after is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, after, "DSH configuration")
                self._save(next_state)
                return {
                    "ok": True,
                    "clientId": "dsh",
                    "changed": True,
                    "state": "current" if owned else "absent",
                    "opId": new_id,
                    "message": "DSH configuration updated.",
                }

    def delete_snapshot(self, op_id):
        with instance_lock(self.directory, "dsh-integration.lock"):
            state = self._state()
            self._recover(state)
            rows = state["operations"]
            if not rows or not any(row["opId"] == op_id for row in rows):
                raise DshError("DSH rollback snapshot is unavailable.", "snapshot_expired")
            if rows[-1]["opId"] == op_id:
                raise DshError("The newest rollback entry must be retained.")
            state["operations"] = [row for row in rows if row["opId"] != op_id]
            self._save(state)
            return {"ok": True, "clientId": "dsh", "opId": op_id, "snapshotRemoved": True}
