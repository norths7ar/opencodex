"""Read-only legacy provider import with an explicit preview and additive apply."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from .config import Provider
from .legacy_provider_auth import resolve_legacy_provider


class LegacyProviderImport:
    def __init__(self, store):
        self.store = store

    def _source(self, source):
        path = Path(source).expanduser().resolve()
        if path.is_dir():
            path /= "config.json"
        if path == self.store.path.resolve():
            raise ValueError("Choose the original OpenCodex configuration, not this Lite instance.")
        raw = path.read_bytes()
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("The source configuration exceeds 16 MiB.")
        value = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(value, dict) or not isinstance(value.get("providers"), dict):
            raise TypeError("Expected an original OpenCodex providers object.")
        return path, raw, value

    def _candidate(self, identity, raw):
        if not isinstance(raw, dict):
            return None, "invalid", "Provider configuration is not an object."
        try:
            raw = resolve_legacy_provider(identity, raw)
        except ValueError:
            return None, "pending", "The original provider destination needs migration review."
        mode = raw.get("authMode", "key")
        if mode not in {"key", "local"}:
            return (
                None,
                "pending",
                "Subscription or forwarded authorization needs its channel migration.",
            )
        if raw.get("adapter") not in {"openai-chat", "openai-responses", "anthropic"}:
            return None, "pending", "This protocol adapter has not been migrated."
        data = {**raw, "id": identity, "name": identity, "authMode": mode}
        # Pool selection is not a single-channel credential. Preserve the active key only.
        for key in ("apiKeyPool", "protectedKey", "apiKeyEnv"):
            data.pop(key, None)
        credential = data.pop("apiKey", None)
        reason = ""
        if mode == "key":
            if isinstance(credential, str) and credential.startswith("$"):
                match = re.fullmatch(r"\$\{(\w+)\}|\$(\w+)", credential)
                if not match:
                    return None, "pending", "The environment credential reference needs review."
                data["apiKeyEnv"] = match.group(1) or match.group(2)
            elif isinstance(credential, str) and credential.startswith(("keychain:", "gcp-sa:")):
                data["legacyCredentialRef"] = credential
                reason = "Restore the referenced credential before enabling this channel."
            elif isinstance(credential, str) and credential:
                data["apiKey"] = credential
            else:
                reason = "Add an API credential before enabling this channel."
        try:
            candidate = Provider.model_validate(data)
        except ValidationError:
            return None, "pending", "This provider has settings that require migration review."
        if candidate.apiKeyEnv:
            try:
                self.store.credential(candidate)
            except ValueError:
                reason = "The credential environment variable is not available in Lite."
        if reason:
            candidate.disabled = True
        return candidate, "disabled" if candidate.disabled else "ready", reason

    def _plan(self, source):
        path, raw, source_config = self._source(source)
        current = self.store.load()
        existing = {provider.id for provider in current.providers}
        rows, candidates = [], {}
        for identity, value in source_config["providers"].items():
            if identity in existing:
                rows.append(
                    {
                        "id": identity,
                        "status": "exists",
                        "reason": "Existing Lite channel is retained.",
                    }
                )
                continue
            candidate, status, reason = self._candidate(identity, value)
            row = {"id": identity, "status": status, "reason": reason}
            if candidate:
                candidates[identity] = candidate
                row.update(adapter=candidate.adapter, disabled=candidate.disabled)
            rows.append(row)
        destination = self.store.path.read_bytes() if self.store.path.exists() else b""
        digest = hashlib.sha256(
            str(path).encode()
            + b"\0"
            + raw
            + b"\0"
            + destination
            + b"\0"
            + json.dumps(rows, sort_keys=True).encode()
        ).hexdigest()
        return (
            {
                "source": str(path),
                "digest": digest,
                "providers": rows,
                "sourceDefaultProvider": source_config.get("defaultProvider"),
                "scope": "providers-and-models",
                "notes": [
                    "Imports selected providers and their model visibility, custom models and context caps.",
                    "Global gateway settings, subscription accounts and usage history are separate migrations.",
                ],
            },
            candidates,
            current,
            destination,
            source_config,
        )

    def preview(self, source):
        return self._plan(source)[0]

    def apply(self, source, digest, selected):
        if not selected or len(selected) != len(set(selected)):
            raise ValueError("Select one or more distinct providers from the preview.")
        with self.store.mutation_lock:
            plan, candidates, current, previous, source_config = self._plan(source)
            if plan["digest"] != digest:
                raise ValueError("Source or Lite configuration changed; preview again.")
            if any(identity not in candidates for identity in selected):
                raise ValueError("Only importable providers from this preview can be selected.")
            current.providers.extend(candidates[identity] for identity in selected)
            disabled = source_config.get("disabledModels", [])
            custom = source_config.get("customModels", [])
            if not isinstance(disabled, list) or not isinstance(custom, list):
                raise TypeError("Source model settings need review before import.")
            current.disabledModels = list(
                dict.fromkeys(
                    [
                        *current.disabledModels,
                        *(
                            slug
                            for slug in disabled
                            if isinstance(slug, str) and slug.partition("/")[0] in selected
                        ),
                    ]
                )
            )
            imported_custom = [
                row for row in custom if isinstance(row, dict) and row.get("provider") in selected
            ]
            if any(
                not isinstance(row.get("modelId"), str) or not row["modelId"]
                for row in imported_custom
            ):
                raise ValueError("A selected provider has an invalid custom model.")
            existing_ids = {row.get("id") for row in current.customModels}
            for row in imported_custom:
                copied = dict(row)
                if not copied.get("id") or copied["id"] in existing_ids:
                    copied["id"] = str(uuid4())
                existing_ids.add(copied["id"])
                current.customModels.append(copied)
            source_caps = source_config.get("providerContextCaps", {})
            if isinstance(source_caps, dict):
                caps = dict(getattr(current, "providerContextCaps", {}))
                for identity in selected:
                    if identity in source_caps:
                        cap = source_caps[identity]
                        if type(cap) is not int or cap <= 0:
                            raise ValueError("A selected provider has an invalid context cap.")
                        caps[identity] = cap
                if caps:
                    current.providerContextCaps = caps
            source_default = plan["sourceDefaultProvider"]
            if not current.defaultProvider and source_default in selected:
                candidate = candidates[source_default]
                if not candidate.disabled:
                    current.defaultProvider = source_default
                    current.defaultModel = candidate.defaultModel
            backup = None
            if previous:
                folder = self.store.directory / "import-backups"
                folder.mkdir(exist_ok=True)
                name = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex + ".json"
                backup = folder / name
                backup.write_bytes(previous)
            self.store.save(current)
            return {"imported": selected, "backup": str(backup) if backup else None}
