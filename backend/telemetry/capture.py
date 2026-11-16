"""Observe upstream usage and terminal facts without retaining conversation payloads."""

import codecs
import contextlib
import json
import re
import time


def classify_client(headers):
    origin = headers.get("originator", "")
    agent = headers.get("user-agent", "")
    if max(len(origin), len(agent)) > 1024:
        return "unknown"
    origin_client = {
        "codex_cli_rs": "codex_cli",
        "codex-tui": "codex_cli",
        "Codex Desktop": "codex_desktop",
        "codex_desktop": "codex_desktop",
        "codex_vscode": "codex_ide",
        "opencode": "opencode",
        "dsh": "dsh",
        "deepseek-harness": "dsh",
    }.get(origin)
    agent_client = None
    entry = re.match(
        r"^claude-cli/[A-Za-z0-9][A-Za-z0-9._+-]* \(external, ([A-Za-z0-9._-]+)(?:, [^,()]+)*\)$",
        agent,
        re.IGNORECASE,
    )
    if entry:
        name = entry[1].lower()
        if name in {"cli", "sdk-cli"}:
            agent_client = "claude_code"
        elif name in {"claude-desktop", "claude-desktop-3p", "local-agent"}:
            agent_client = "claude_desktop"
    if not agent_client:
        for prefix, client in (
            ("dsh", "dsh"),
            ("deepseek-harness", "dsh"),
            ("claude-code", "claude_code"),
            ("opencode", "opencode"),
            ("codex_cli_rs", "codex_cli"),
        ):
            if re.match(rf"^{prefix}/[A-Za-z0-9][A-Za-z0-9._+-]*(?:\s|$)", agent, re.IGNORECASE):
                agent_client = client
                break
    if (
        origin_client
        and agent_client
        and origin_client != agent_client
        and not (origin_client.startswith("codex_") and agent_client.startswith("codex_"))
    ):
        return "unknown"
    return origin_client or agent_client or "unknown"


def conversation_id(headers, body, protocol):
    metadata = body.get("metadata")
    candidates = []
    if protocol.startswith("messages") and isinstance(metadata, dict):
        candidates.append(metadata.get("user_id"))
    candidates.extend(
        headers.get(key)
        for key in (
            "x-codex-parent-thread-id",
            "session_id",
            "session-id",
            "thread-id",
            "x-hermes-conversation-id",
        )
    )
    for candidate in candidates:
        if (
            isinstance(candidate, str)
            and candidate.strip()
            and len(candidate) <= 4096
            and not re.search(r"[\x00-\x1f\x7f]", candidate)
        ):
            return candidate.strip()
    return None


class UsageCapture:
    def __init__(self):
        self.started = time.monotonic()
        self.first_output_ms = None
        self.generation_start_ms = None
        self.last_output_ms = None
        self.logical_status = None
        self.usage = None
        self.terminal_status = None
        self.served_model = None
        self.service_tier = None
        self.error_code = None
        self.streaming = False
        self.body_sample = ""
        self.chat_wire = False
        self._real_terminal = False
        self._pending_cr = False
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self._overflow = False

    def output(self):
        self.last_output_ms = round((time.monotonic() - self.started) * 1000)
        if self.first_output_ms is None:
            self.first_output_ms = self.last_output_ms

    def value(self, value):
        if not isinstance(value, dict):
            return
        kind = value.get("type", "")
        if not isinstance(kind, str):
            kind = ""
        now = round((time.monotonic() - self.started) * 1000)
        if kind in {"content_block_start", "response.output_item.added"}:
            if self.generation_start_ms is None:
                self.generation_start_ms = now
        elif kind == "content_block_delta" or (
            kind.startswith("response.") and kind.endswith(".delta")
        ):
            if self.generation_start_ms is None:
                self.generation_start_ms = now
            self.last_output_ms = now
        payload = value.get("response") or value.get("message") or value
        if not isinstance(payload, dict):
            return
        usage = payload.get("usage")
        if isinstance(usage, dict) and usage:
            self.usage = {**(self.usage or {}), **usage}
        if isinstance(payload.get("model"), str):
            self.served_model = payload["model"]
        if payload.get("service_tier") in {"auto", "default", "flex", "priority", "scale"}:
            self.service_tier = payload["service_tier"]
        if kind in {"response.completed", "response.failed", "response.incomplete"}:
            self._real_terminal = True
            self.terminal_status = kind.removeprefix("response.")
            if self.terminal_status == "completed":
                self.logical_status = 200
                self.error_code = None
            details = payload.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            if not isinstance(reason, str):
                reason = None
            error = payload.get("error") or {}
            code = error.get("code") or error.get("type") if isinstance(error, dict) else None
            if not isinstance(code, str):
                code = None
            if self.terminal_status != "completed":
                self.logical_status = (
                    200
                    if reason == "max_output_tokens"
                    else {
                        "usage_limit_reached": 429,
                        "rate_limit_exceeded": 429,
                        "insufficient_quota": 429,
                        "insufficient_credits": 402,
                        "invalid_api_key": 401,
                        "authentication_error": 401,
                        "permission_denied": 403,
                        "invalid_request_error": 400,
                    }.get(reason or code, 502)
                )
        elif kind == "message_stop":
            self.terminal_status = "completed"
        elif (kind == "error" or value.get("error")) and not self._real_terminal:
            self.terminal_status = "failed"
            error = value.get("error") or value
            code = error.get("code") or error.get("type") if isinstance(error, dict) else None
            self.logical_status = {
                "rate_limit_exceeded": 429,
                "usage_limit_reached": 429,
                "insufficient_quota": 429,
                "insufficient_credits": 402,
            }.get(code if isinstance(code, str) else "", 502)
            # Closed diagnostic labels; provider error strings can contain credentials.
            self.error_code = "upstream_error"
        elif not self.streaming and payload.get("status") in {"completed", "failed", "incomplete"}:
            self.terminal_status = payload["status"]
        if kind in {
            "response.output_text.delta",
            "response.function_call_arguments.delta",
            "response.reasoning_text.delta",
            "response.reasoning_summary_text.delta",
            "response.custom_tool_call_input.delta",
            "content_block_delta",
        }:
            self.output()
        choices = payload.get("choices")
        if isinstance(choices, list):
            self.chat_wire = True
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta") or choice.get("message") or {}
                if not isinstance(delta, dict):
                    continue
                if any(
                    delta.get(field)
                    for field in ("content", "tool_calls", "reasoning_content", "reasoning")
                ):
                    self.output()
                finish = choice.get("finish_reason")
                if finish:
                    self.terminal_status = (
                        "failed"
                        if finish == "error"
                        else "incomplete"
                        if finish == "length"
                        else "completed"
                    )
                    self.logical_status = 502 if finish == "error" else 200
        if not self.streaming and payload.get("content"):
            self.output()
            self.terminal_status = (
                "incomplete" if payload.get("stop_reason") == "max_tokens" else "completed"
            )

    def feed(self, chunk):
        if self._overflow:
            return
        text = ("\r" if self._pending_cr else "") + self._decoder.decode(chunk)
        self._pending_cr = self.streaming and text.endswith("\r")
        if self._pending_cr:
            text = text[:-1]
        self._buffer += text.replace("\r\n", "\n").replace("\r", "\n") if self.streaming else text
        if self.streaming:
            while "\n\n" in self._buffer:
                frame, self._buffer = self._buffer.split("\n\n", 1)
                self.frame(frame)
        if len(self._buffer) > 4 * 1024 * 1024:
            self._buffer = ""
            self._overflow = True

    def frame(self, frame):
        data = "\n".join(
            line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")
        )
        if data == "[DONE]" and self.chat_wire and self.terminal_status is None:
            self.terminal_status = "completed"
            self.logical_status = 200
        elif data and data != "[DONE]":
            with contextlib.suppress(ValueError, TypeError, RecursionError):
                self.value(json.loads(data))

    def finish(self):
        if self.streaming and not self._overflow:
            self.frame(self._buffer)
        if not self.streaming and not self._overflow:
            with contextlib.suppress(ValueError, TypeError, RecursionError):
                self.value(json.loads(self._buffer))
        self._buffer = ""
