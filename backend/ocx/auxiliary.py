"""Optional image description for text-only targets; independent of image generation."""

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
from collections import OrderedDict

from .workflow_budget import WorkflowLease

DESCRIBE_INSTRUCTION = (
    "You are a vision describer for a text-only model that cannot see the image. Describe the image "
    "thoroughly and factually so that model can fully reason about it: transcribe any visible text "
    "verbatim, and note UI/layout, colors, branding/logos, charts, and notable details. Focus on "
    "what's relevant to the user's request. Output only the description."
)
TERMINAL_HEADER = "x-opencodex-vision-describe"
WORKFLOW_HEADER = "x-opencodex-vision-workflow"
OMITTED = "[image omitted: this model is text-only and the vision sidecar is unavailable]"


def usable_subscription(status):
    accounts = status.get("accounts")
    if isinstance(accounts, list):
        return any(
            row.get("active", True) and not row.get("needsReauth") and not row.get("paused")
            for row in accounts
            if isinstance(row, dict)
        )
    return bool(status.get("loggedIn") and not status.get("needsReauth"))


def settings(config, auth_status=None):
    value = {
        "enabled": True,
        "reasoning": "low",
        "maxDescriptionsPerTurn": 8,
        "timeoutMs": 45000,
        **getattr(config, "visionSidecar", {}),
    }
    backend = value.get("backend")
    if backend == "routed" and "/" in (value.get("model") or ""):
        return value
    if backend not in {"openai", "anthropic"}:
        healthy = auth_status("anthropic") if auth_status else {}
        provider = next((p for p in config.providers if p.id == "anthropic"), None)
        backend = (
            "anthropic"
            if (
                provider
                and provider.authMode == "oauth"
                and not provider.disabled
                and usable_subscription(healthy)
            )
            else "openai"
        )
    model = value.get("model")
    value["backend"] = backend
    value["model"] = (
        model
        if model and "/" not in model
        else ("claude-sonnet-5" if backend == "anthropic" else "gpt-5.6-luna")
    )
    return value


def helper_selector(cfg):
    return cfg["model"] if cfg["backend"] == "routed" else f"{cfg['backend']}/{cfg['model']}"


def modalities(config, provider, model, rows):
    explicit = getattr(provider, "modelCapabilities", {}).get(model, {}).get("inputModalities")
    if explicit is not None:
        return explicit
    custom = next(
        (
            row
            for row in config.customModels
            if row.get("provider") == provider.id and row.get("modelId") == model
        ),
        {},
    )
    if custom.get("inputModalities"):
        return custom["inputModalities"]
    if model in getattr(provider, "noVisionModels", []):
        return ["text"]
    row = next((row for row in rows if row["provider"] == provider.id and row["id"] == model), {})
    if custom.get("inputModalities") == [] and row.get("inputModalities") == []:
        row = {}
    return row.get(
        "inputModalities",
        getattr(provider, "modelInputModalities", {}).get(
            model, getattr(provider, "inputModalities", None)
        ),
    )


def image_url(part):
    if part.get("type") == "input_image":
        return part.get("image_url") or ""
    if part.get("type") == "image_url":
        value = part.get("image_url", {})
        return value if isinstance(value, str) else value.get("url", "")
    if part.get("type") == "image":
        source = part.get("source", {})
        if source.get("type") == "base64":
            return f"data:{source.get('media_type')};base64,{source.get('data', '')}"
        return source.get("url", "")
    return None


def image_jobs(body, protocol):
    """Keep native item identities/tool calls; replace only image-bearing content parts."""
    jobs = []

    def visit(parts, context):
        if not isinstance(parts, list):
            return
        own = " ".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") in {"text", "input_text"}
        )
        context = (own or context)[:800]
        for index, part in enumerate(parts):
            if not isinstance(part, dict):
                continue
            url = image_url(part)
            if url is not None:
                nested = part.get("image_url")
                detail = (
                    nested.get("detail", "high")
                    if isinstance(nested, dict)
                    else part.get("detail", "high")
                )
                jobs.append((parts, index, url, context, detail))
            elif part.get("type") == "tool_result":
                visit(part.get("content"), context)

    for item in body.get("input" if protocol == "responses" else "messages", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"function_call_output", "custom_tool_call_output"}:
            visit(item.get("output"), "")
        elif (
            item.get("role") in {"user", "developer", "tool"} or item.get("type") == "agent_message"
        ):
            visit(item.get("content"), "")
    return jobs


class BorrowedWorkflow(WorkflowLease):
    """Helper sends share their admitted parent; they do not own another turn slot."""

    def release(self):
        pass


class Auxiliary:
    def __init__(self, store, inventory, auth_status=None):
        self.store, self.inventory = store, inventory
        self.auth_status = auth_status
        self.complete = None
        self.cache = OrderedDict()
        self.workflows = {}

    def borrow_workflow(self, headers):
        parent = self.workflows.get(headers.get(WORKFLOW_HEADER))
        if parent is None or parent.released:
            return None
        return BorrowedWorkflow(parent.ledger, parent.root_id)

    async def prepare(self, body, protocol, provider, model, headers, workflow_lease=None):
        if protocol == "compact":
            return False
        jobs = image_jobs(body, protocol)
        if not jobs:
            return False
        config = self.store.load()
        rows = await self.inventory.rows()
        modes = modalities(config, provider, model, rows)
        if modes is None or "image" in modes:
            return False
        cfg = settings(config, self.auth_status)
        terminal = headers.get(TERMINAL_HEADER) == "1"
        enabled = cfg.get("enabled") is not False and not terminal and self.complete is not None
        helper = helper_selector(cfg)
        try:
            if "/" in helper and not any(p.id == helper.split("/", 1)[0] for p in config.providers):
                raise ValueError("Helper provider is unavailable")
            target, native = self.store.route(helper)
            helper_modes = modalities(config, target, native, rows)
            if cfg["backend"] != "routed":
                auth = self.auth_status(cfg["backend"]) if self.auth_status else {}
                enabled = enabled and target.authMode == "oauth" and usable_subscription(auth)
            enabled = (
                enabled
                and not target.disabled
                and (helper_modes is None or "image" in helper_modes)
            )
        except ValueError, KeyError:
            enabled = False
        if not enabled:
            for parts, index, *_ in jobs:
                parts[index] = {
                    "type": "input_text" if protocol == "responses" else "text",
                    "text": OMITTED,
                }
            return True
        limit = cfg.get("maxDescriptionsPerTurn", 8)
        limit = limit if type(limit) is int and limit >= 0 else 8
        timeout = cfg.get("timeoutMs", 45000)
        timeout = timeout if type(timeout) is int and 1 <= timeout <= 2147483647 else 45000
        semaphore, pending = asyncio.Semaphore(3), {}
        misses = 0

        async def describe(url, context, key, persistent):
            if not (
                url.startswith("https://")
                or re.match(
                    r"^data:image/(?:png|jpeg|jpg|webp|gif)(?:;base64)?,", url, re.IGNORECASE
                )
            ):
                return "[An image was attached but could not be processed: unsupported image URL]"
            if url.startswith("data:") and len(url.partition(",")[2]) * 3 // 4 > 20 * 1024 * 1024:
                return "[An image was attached but could not be processed: image too large]"
            try:
                async with semaphore, asyncio.timeout(timeout / 1000):
                    token = secrets.token_urlsafe(32) if workflow_lease is not None else None
                    if token:
                        self.workflows[token] = workflow_lease
                    try:
                        result = await self.complete(
                            {
                                "model": helper,
                                "system": DESCRIBE_INSTRUCTION,
                                "context": context,
                                "image": url,
                                "timeoutMs": timeout,
                                "reasoning": cfg.get("reasoning", "low"),
                                "workflowToken": token,
                            }
                        )
                    finally:
                        if token:
                            self.workflows.pop(token, None)
                text = result.get("text", "").strip()
                if result.get("error") or not text:
                    raise ValueError("no description")
                text = text[:2000] + ("\n…[description truncated]" if len(text) > 2000 else "")
                if persistent:
                    self.cache[key] = text
                    self.cache.move_to_end(key)
                    while (
                        len(self.cache) > 256
                        or sum(len(k.encode()) + len(v.encode()) for k, v in self.cache.items())
                        > 1024 * 1024
                    ):
                        self.cache.popitem(last=False)
                return (
                    "[Image content — described by a vision model because you cannot see images directly:\n"
                    + text
                    + "]"
                )
            except TimeoutError, ValueError, OSError:
                return (
                    "[An image was attached but could not be processed: vision helper unavailable]"
                )

        replacements = []
        async with asyncio.TaskGroup() as tasks:
            for parts, index, url, context, detail in jobs:
                persistent = url.startswith("data:") and ";base64," in url
                try:
                    identity = (
                        base64.b64decode(url.partition(",")[2]) if persistent else url.encode()
                    )
                except ValueError:
                    identity = url.encode()
                key = json.dumps(
                    [
                        helper,
                        cfg.get("reasoning"),
                        detail,
                        hashlib.sha256(identity).hexdigest(),
                        " ".join(context.split()),
                    ]
                )
                if persistent and key in self.cache:
                    self.cache.move_to_end(key)
                    outcome = (
                        "[Image content — described by a vision model because you cannot see images directly:\n"
                        + self.cache[key]
                        + "]"
                    )
                elif key in pending:
                    outcome = pending[key]
                elif misses >= limit:
                    outcome = "[An image was attached but could not be processed: description cap reached for this turn]"
                else:
                    misses += 1
                    outcome = pending[key] = tasks.create_task(
                        describe(url, context, key, persistent)
                    )
                replacements.append((parts, index, outcome))
        for parts, index, outcome in replacements:
            parts[index] = {
                "type": "input_text" if protocol == "responses" else "text",
                "text": outcome.result() if isinstance(outcome, asyncio.Task) else outcome,
            }
        logging.getLogger("ocx.auxiliary").debug(
            "Vision images=%d descriptions=%d",
            len(jobs),
            misses,
            extra={"ocx_debug_stream": "injection"},
        )
        return True
