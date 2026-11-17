"""Opt-in diagnostics streams matching the original dashboard's cursor contracts."""

import json
import logging
import os
import re
import threading
import time
import unicodedata
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import cast, overload

ENV = {
    "debug": "OCX_DEBUG",
    "usage": "OPENCODEX_USAGE_DEBUG",
    "injection": "OCX_INJECTION_DEBUG",
}
SUPPORTED = ("debug", "usage", "injection")
STREAM_FLAGS = {"provider": "debug", "usage": "usage", "injection": "injection"}
LINE_BYTES = 16 * 1024
SAMPLE_BYTES = 2048
_SECRET_NAME = r"authorization|proxy-authorization|cookie|set-cookie|set-cookie2|api[-_]?key|x-api-key|x-goog-api-key|x-amz-security-token|x-dsh-auth-token|x-openai-agent-assertion|copilot[-_]?session[-_]?token|session[-_]?token|access[-_]?token|refresh[-_]?token|id[-_]?token|token|secret|client[-_]?secret|password|profile[-_]?arn|exa[-_]?api[-_]?key"
_SECRET_KEY = re.compile(rf"^(?:{_SECRET_NAME})$", re.IGNORECASE)
_LABEL = re.compile(rf"(?<![\w-])[\"']?(?:{_SECRET_NAME})[\"']?\s*[:=][^\r\n]*", re.IGNORECASE)


def redact_text(value, secrets=()):
    text = str(value)
    for secret in secrets:
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "[REDACTED]")
    # Fold label confusables/invisible format characters before matching echoed credentials.
    text = "".join(
        char for char in unicodedata.normalize("NFKC", text) if unicodedata.category(char) != "Cf"
    )
    text = _LABEL.sub("credential: [REDACTED]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", text)
    return re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)


@overload
def _safe(value: dict[str, object], secrets=()) -> dict[str, object]: ...


@overload
def _safe(value: object, secrets=(), depth: int = 0) -> object: ...


def _safe(value, secrets=(), depth=0) -> object:
    if depth > 12:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if _SECRET_KEY.fullmatch(str(key))
            else _safe(item, secrets, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item, secrets, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return redact_text(value, secrets)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return redact_text(value, secrets)


def _truncate(text, size):
    raw = text.encode("utf-8", errors="replace")
    return text if len(raw) <= size else raw[:size].decode("utf-8", errors="ignore") + "…"


class _DiagnosticHandler(logging.Handler):
    def __init__(self, state):
        super().__init__()
        self.state = state

    def emit(self, record):
        with suppress(ValueError, TypeError, RecursionError):
            stream = getattr(record, "ocx_debug_stream", "provider")
            if stream in STREAM_FLAGS:
                self.state.append(stream, f"[{record.name}] {record.getMessage()}")


class DebugState:
    def __init__(self, directory: Path, *, environ=None):
        self.directory = Path(directory)
        self.environ = os.environ if environ is None else environ
        self.overrides = {}
        self.lock = threading.RLock()
        self.buffers = {
            name: deque(maxlen=200 if name == "usage" else 2000) for name in STREAM_FLAGS
        }
        self.next_seq = {name: 1 for name in STREAM_FLAGS}
        self.handler = None
        self.previous_level = None
        self._load_usage()

    @property
    def usage_path(self):
        return self.directory / "usage-debug.jsonl"

    def _load_usage(self):
        try:
            with self.usage_path.open(encoding="utf-8") as source:
                lines = deque(source, maxlen=200)
            for line in lines:
                record = json.loads(line)
                if not isinstance(record, dict):
                    continue
                sequence = max(self.next_seq["usage"], int(record.pop("_debugSeq", 0)))
                self.buffers["usage"].append(
                    {
                        "seq": sequence,
                        "at": record.get("ts", 0),
                        "line": json.dumps(_safe(record), ensure_ascii=False),
                    }
                )
                self.next_seq["usage"] = sequence + 1
        except OSError, ValueError, TypeError:
            # A malformed historical diagnostic file cannot prevent gateway startup.
            pass

    def _env(self, flag):
        return self.environ.get(ENV[flag]) == "1" or (
            flag == "debug" and self.environ.get("OCX_DEBUG_FRAMES") == "1"
        )

    def enabled(self, flag):
        flag = STREAM_FLAGS.get(flag, flag)
        return flag in SUPPORTED and self.overrides.get(flag, self._env(flag))

    def settings(self):
        with self.lock:
            return {
                "enabled": self.enabled("debug"),
                "usage": self.enabled("usage"),
                "injection": self.enabled("injection"),
                "runtimeOverride": dict(self.overrides),
                "env": {flag: self._env(flag) for flag in ENV},
                "supportedFlags": list(SUPPORTED),
            }

    def configure(self, body):
        if not isinstance(body, dict):
            raise TypeError("Provide debug/usage/injection booleans or reset:true.")
        with self.lock:
            reset = body.get("reset")
            if reset is True:
                self.overrides.clear()
            elif isinstance(reset, str) and reset in {*SUPPORTED, "provider"}:
                self.overrides.pop("debug" if reset == "provider" else reset, None)
            else:
                patch = {key: value for key, value in body.items() if key in SUPPORTED}
                if not patch or any(type(value) is not bool for value in patch.values()):
                    raise ValueError("Provide debug/usage/injection booleans or reset:true.")
                self.overrides.update(patch)
            self._logging_level()
            return self.settings()

    def install(self):
        """Capture existing ocx diagnostics while the server lifespan is active."""
        if self.handler is None:
            logger = logging.getLogger("ocx")
            self.previous_level = logger.level
            self.handler = _DiagnosticHandler(self)
            logger.addHandler(self.handler)
            self._logging_level()

    def _logging_level(self):
        if self.handler:
            # install records the integer level before publishing the active handler.
            logging.getLogger("ocx").setLevel(
                logging.DEBUG
                if self.enabled("debug") or self.enabled("injection")
                else cast(int, self.previous_level)
            )

    def close(self):
        if self.handler:
            logger = logging.getLogger("ocx")
            logger.removeHandler(self.handler)
            logger.setLevel(cast(int, self.previous_level))
            self.handler.close()
            self.handler = None

    def append(self, stream, line, *, secrets=()):
        if stream not in STREAM_FLAGS or not self.enabled(stream):
            return
        with self.lock:
            entry = {
                "seq": self.next_seq[stream],
                "at": time.time_ns() // 1_000_000,
                "line": _truncate(redact_text(line, secrets), LINE_BYTES),
            }
            self.next_seq[stream] += 1
            self.buffers[stream].append(entry)

    def event(self, stream, event, **fields):
        if self.enabled(stream):
            self.append(stream, json.dumps({"event": event, **_safe(fields)}, ensure_ascii=False))

    def sample(self, text, *, secrets=()):
        return _truncate(redact_text(text, secrets), SAMPLE_BYTES) if self.enabled("usage") else ""

    def usage(self, record: dict[str, object], *, secrets=()):
        if not self.enabled("usage"):
            return
        with self.lock:
            try:
                safe = _safe(record, secrets)
                safe.setdefault("ts", time.time_ns() // 1_000_000)
                safe["bodySample"] = self.sample(safe.get("bodySample", ""), secrets=secrets)
                line = json.dumps(safe, ensure_ascii=False)
                if len(line.encode()) > LINE_BYTES:
                    safe["bodySample"] = "[diagnostic record too large]"
                    line = json.dumps(safe, ensure_ascii=False)
                entry = {"seq": self.next_seq["usage"], "at": safe["ts"], "line": line}
                self.next_seq["usage"] += 1
                # Original rolling file trims 201 entries to the most recent 100.
                if len(self.buffers["usage"]) == 200:
                    retained = list(self.buffers["usage"])[-99:]
                    self.buffers["usage"].clear()
                    self.buffers["usage"].extend(retained)
                self.buffers["usage"].append(entry)
                self.directory.mkdir(parents=True, exist_ok=True)
                temporary = self.usage_path.with_suffix(".jsonl.tmp")
                with temporary.open("w", encoding="utf-8") as output:
                    for item in self.buffers["usage"]:
                        output.write(
                            json.dumps(
                                {**json.loads(item["line"]), "_debugSeq": item["seq"]},
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                temporary.replace(self.usage_path)
            except OSError, ValueError, TypeError, RecursionError:
                pass

    def entries(self, stream, *, after=0, limit=500):
        with self.lock:
            return [dict(entry) for entry in self.buffers[stream] if entry["seq"] > after][-limit:]
