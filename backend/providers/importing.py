"""Read-only original provider preview and atomic selected-channel import."""

import base64
import hashlib
import json
import os
import re
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from backend.auth.storage import protect
from backend.configuration.migration import migrate_legacy
from backend.configuration.models import CredentialRecord
from backend.providers.registry import SERVICES, match_destination

_NAMES = {
    "openai-apikey": "openai",
    "deepseek": "deepseek",
    "mimo": "mimo",
    "opencode-zen": "opencode-zen",
    "opencode-go": "opencode-go",
    "chatgpt": "chatgpt",
}


class LegacyProviderImport:
    def __init__(self, store, *, environment=None, encrypt=protect):
        self.store = store
        self.environment = os.environ if environment is None else environment
        self.encrypt = encrypt

    def _source(self, source):
        path = Path(source).expanduser().resolve()
        if path.is_dir():
            path = path / "config.json"
        if path == self.store.path.resolve():
            raise ValueError("Choose the original configuration.")
        with path.open("rb") as handle:
            raw = handle.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("Source exceeds size limit.")
        value = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(value, dict) or not isinstance(value.get("providers"), dict):
            raise ValueError("Expected an original provider map.")
        return path, raw, value

    def _candidate(self, identity, source):
        if not isinstance(source, dict):
            return None, "invalid", "Provider is not an object."
        row = deepcopy(source)
        service_id = _NAMES.get(identity)
        url = row.get("baseUrl")
        mode = row.get("authMode", "key")
        if service_id is None and not url:
            return None, "pending", "Unselected or unknown service requires review."
        if service_id == "chatgpt" or mode == "oauth":
            return None, "pending", "Subscription login is managed separately."
        if mode not in {"key", "local"}:
            return None, "pending", "Authorization mode requires review."
        if service_id and not url:
            definition = next(item for item in SERVICES if item.id == service_id)
            url = definition.destinations[0].base_url
        if not isinstance(url, str):
            return None, "pending", "Provider destination requires review."
        if row.get("apiKeyPool"):
            return None, "pending", "Multiple credential pools require review."
        row.pop("apiKeyPool", None)
        secret = row.pop("apiKey", None)
        if secret is not None and not isinstance(secret, str):
            return None, "pending", "Credential requires review."
        env = None
        if secret and secret.startswith("$"):
            match = re.fullmatch(
                r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", secret
            )
            if not match:
                return None, "pending", "Environment credential reference requires review."
            env = match.group(1) or match.group(2)
            secret = None
        if secret and secret.startswith(("keychain:", "gcp-sa:")):
            return None, "pending", "External credential reference requires review."
        row.update(id=identity, name=row.get("name") or identity, baseUrl=url, authMode=mode)
        if "adapter" not in row:
            if service_id:
                row["adapter"] = (
                    next(item for item in SERVICES if item.id == service_id)
                    .destinations[0]
                    .default_protocol
                )
            else:
                return None, "pending", "Protocol requires review."
        try:
            migration = migrate_legacy({"providers": [row]})
            channel = migration.configuration.channels[0]
            matched = match_destination(channel)
            if not matched or matched.service.id not in {
                "openai",
                "deepseek",
                "mimo",
                "opencode-zen",
                "opencode-go",
            }:
                return None, "pending", "Destination is not a selected service."
            if migration.remainder:
                return None, "pending", "Provider contains unmapped settings requiring review."
            channel.service_id = matched.service.id
            credential = migration.credentials.get(identity, CredentialRecord())
            if env:
                credential.env_var = env
            reason = ""
            if mode == "key" and not (secret or credential.protected_key or credential.env_var):
                reason = "Add an API credential before enabling this channel."
            if credential.env_var and not self.environment.get(credential.env_var):
                reason = "The credential environment variable is not available in Lite."
            if reason:
                channel.disabled = True
            return (
                (channel, credential, secret),
                "disabled" if channel.disabled else "ready",
                reason,
            )
        except ValueError, TypeError, KeyError:
            return None, "pending", "Provider settings require migration review."

    def _plan(self, source, state):
        path, raw, data = self._source(source)
        existing = {c.id for c in state.configuration.channels}
        rows = []
        candidates = {}
        for identity, value in data["providers"].items():
            if not isinstance(identity, str):
                raise ValueError("Invalid provider identity.")
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
            item = {"id": identity, "status": status, "reason": reason}
            if candidate:
                candidates[identity] = candidate
                channel = candidate[0]
                item.update(adapter=channel.protocol, disabled=channel.disabled)
            rows.append(item)
        digest = hashlib.sha256(
            str(path).encode()
            + b"\0"
            + raw
            + b"\0"
            + str(state.revision).encode()
            + b"\0"
            + json.dumps(rows, sort_keys=True).encode()
        ).hexdigest()
        return (
            {
                "source": str(path),
                "digest": digest,
                "providers": rows,
                "sourceDefaultProvider": data.get("defaultProvider"),
                "scope": "providers-and-models",
                "notes": [
                    "Selected importable channels are added atomically; subscription login and global settings remain separate."
                ],
            },
            candidates,
            data,
        )

    def preview(self, source):
        return self._plan(source, self.store.read())[0]

    def apply(self, source, digest, selected):
        if (
            not isinstance(selected, list)
            or not selected
            or any(not isinstance(v, str) for v in selected)
            or len(selected) != len(set(selected))
        ):
            raise ValueError("Select distinct providers.")

        def change(state):
            plan, candidates, data = self._plan(source, state)
            if plan["digest"] != digest or any(identity not in candidates for identity in selected):
                raise ValueError("Preview is stale or selection is unavailable.")
            cfg = state.configuration
            channels = list(cfg.channels)
            for identity in selected:
                channel, credential, secret = candidates[identity]
                if secret:
                    credential.protected_key = base64.b64encode(
                        self.encrypt(secret.encode())
                    ).decode()
                channels.append(channel)
                state.credentials[identity] = credential
            cfg.channels = channels
            disabled = data.get("disabledModels", [])
            custom = data.get("customModels", [])
            caps = data.get("providerContextCaps", {})
            if (
                not isinstance(disabled, list)
                or not isinstance(custom, list)
                or not isinstance(caps, dict)
            ):
                raise ValueError("Model settings require review.")
            cfg.models.disabled = list(
                dict.fromkeys(
                    [
                        *cfg.models.disabled,
                        *[
                            v
                            for v in disabled
                            if isinstance(v, str) and v.partition("/")[0] in selected
                        ],
                    ]
                )
            )
            existing = {item.id for item in cfg.models.custom}
            imported = []
            from backend.configuration.models import CustomModel

            for entry in custom:
                if not isinstance(entry, dict) or entry.get("provider") not in selected:
                    continue
                row = {k: v for k, v in entry.items() if k in CustomModel.model_fields}
                if not row.get("id") or row["id"] in existing:
                    row["id"] = str(uuid4())
                model = CustomModel.model_validate(row)
                existing.add(model.id)
                imported.append(model)
            cfg.models.custom = [*cfg.models.custom, *imported]
            active = dict(cfg.models.context_caps or {})
            for identity in selected:
                if identity in caps:
                    if type(caps[identity]) is not int or caps[identity] <= 0:
                        raise ValueError("Invalid context cap.")
                    active[identity] = caps[identity]
            cfg.models.context_caps = active
            cfg.models = cfg.models
            default = data.get("defaultProvider")
            if (
                not cfg.default_channel_id
                and default in selected
                and not candidates[default][0].disabled
            ):
                cfg.default_channel_id = default
                cfg.default_model_id = candidates[default][0].models.default or data.get(
                    "defaultModel"
                )

        self.store.update(change)
        return {"imported": selected, "backup": None}
