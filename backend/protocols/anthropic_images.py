"""Age-tier image normalization and deterministic Anthropic request-limit backstop."""

import asyncio
import base64
import hashlib
import io
import threading
from collections import OrderedDict

import anyio
from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener

register_heif_opener()

MIB = 1024 * 1024
TIERS = (
    (2000, (80, 60, 40, 30), 2 * MIB),
    (1024, (70, 50), 512 * 1024),
    (700, (60, 40), 192 * 1024),
    (500, (40,), 100 * 1024),
    (400, (30,), 100 * 1024),
    (320, (25,), float("inf")),
)
NATIVE_MEDIA = {"image/jpeg", "image/png", "image/gif", "image/webp"}
CORRUPT = "[image omitted: undecodable or corrupt image data]"
BOMB = "[image omitted: image too large to process safely]"
OLD = "[image omitted: Anthropic request exceeded the 20-image limit for large images; older screenshots were dropped]"
TOTAL = "[image omitted: total image payload exceeded Anthropic's 32MB request limit; older screenshots were dropped]"


def image_refs(messages):
    result = []

    def visit(parts):
        if not isinstance(parts, list):
            return
        for index, block in enumerate(parts):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image":
                result.append((parts, index))
            elif block.get("type") == "tool_result":
                visit(block.get("content"))

    for message in messages:
        visit(message.get("content"))
    return result


def replace(ref, body):
    parts, index = ref
    control = parts[index].get("cache_control")
    parts[index] = {**body, **({"cache_control": control} if control is not None else {})}


def drop(ref, note):
    replace(ref, {"type": "text", "text": note})


class AnthropicImages:
    def __init__(self):
        self.cache, self.positions = OrderedDict(), OrderedDict()
        self.cache_bytes = 0
        self.lock = threading.RLock()
        self.admission = asyncio.Semaphore(4)

    def _remember(self, key, value):
        size = len(value[0]) + len(key) + 64
        if size > 20 * MIB:
            return
        with self.lock:
            old = self.cache.pop(key, None)
            if old:
                self.cache_bytes -= old[1]
            self.cache[key] = (value, size)
            self.cache_bytes += size
            while len(self.cache) > 4096 or self.cache_bytes > 64 * MIB:
                self.cache_bytes -= self.cache.popitem(last=False)[1][1]

    def _process(self, data, media, start, identity):
        raw = None
        try:
            for position in range(start, len(TIERS)):
                key = f"{identity}:{position}"
                with self.lock:
                    cached = self.cache.get(key)
                    if cached:
                        self.cache.move_to_end(key)
                        value = cached[0]
                        return (*value, position)
                if raw is None:
                    raw = base64.b64decode(data, validate=True)
                with Image.open(io.BytesIO(raw)) as source:
                    width, height = source.size
                    if width <= 0 or height <= 0 or width * height > 16_000_000:
                        return None, BOMB
                    source.load()
                    edge, qualities, cap = TIERS[position]
                    if media in NATIVE_MEDIA and max(width, height) <= edge and len(data) <= cap:
                        result = (data, media, (width, height))
                        self._remember(key, result)
                        return (*result, position)
                    image = source.convert("RGB")
                    if max(width, height) > edge:
                        ratio = edge / max(width, height)
                        image = image.resize(
                            (max(1, int(width * ratio + 0.5)), max(1, int(height * ratio + 0.5))),
                            Image.Resampling.LANCZOS,
                        )
                    for quality in qualities:
                        encoded = io.BytesIO()
                        image.save(encoded, format="JPEG", quality=quality)
                        payload = base64.b64encode(encoded.getvalue()).decode()
                        if len(payload) <= cap:
                            result = (payload, "image/jpeg", image.size)
                            self._remember(key, result)
                            return (*result, position)
        except UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError:
            return None, CORRUPT
        return None, CORRUPT

    async def normalize(self, messages, *, tier_bias=0, budget=20 * MIB):
        refs = image_refs(messages)
        if not refs:
            return
        entries = [None] * len(refs)
        snapshots = []
        with self.lock:
            for parts, index in refs:
                source = parts[index].get("source", {})
                data = source.get("data") if source.get("type") == "base64" else None
                media = str(source.get("media_type", "")).lower()
                identity = (
                    hashlib.sha256((media + "\0" + data).encode()).hexdigest()
                    if isinstance(data, str)
                    else None
                )
                snapshots.append((data, media, identity, self.positions.get(identity)))

        async def process(data, media, position, identity):
            async with self.admission:
                return await anyio.to_thread.run_sync(
                    self._process, data, media, position, identity
                )

        async def initial(index):
            data, media, identity, recorded = snapshots[index]
            newest = len(refs) - 1 - index
            if not isinstance(data, str) or newest >= 100:
                return
            if len(data) > 64 * MIB:
                drop(refs[index], BOMB)
                return
            position = min(
                (recorded if recorded is not None else 0 if newest < 6 else 1 if newest < 20 else 2)
                + max(0, tier_bias),
                len(TIERS) - 1,
            )
            value = await process(data, media, position, identity)
            if value[0] is None:
                drop(refs[index], value[1])
                return
            payload, output_media, dimensions, position = value
            replace(
                refs[index],
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": output_media, "data": payload},
                },
            )
            entries[index] = {
                "data": data,
                "media": media,
                "identity": identity,
                "size": len(payload),
                "pos": position,
                "dimensions": dimensions,
            }

        async with asyncio.TaskGroup() as group:
            for index in range(len(refs)):
                group.create_task(initial(index))
        total = sum(e["size"] for e in entries if e)
        while total > budget:
            index = next(
                (i for i, e in enumerate(entries) if e and e["pos"] < len(TIERS) - 1), None
            )
            if index is None:
                break
            entry = entries[index]
            value = await process(
                entry["data"], entry["media"], entry["pos"] + 1, entry["identity"]
            )
            total -= entry["size"]
            if value[0] is None:
                drop(refs[index], value[1])
                entries[index] = None
                continue
            payload, media, dimensions, position = value
            replace(
                refs[index],
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media, "data": payload},
                },
            )
            entry.update(size=len(payload), pos=position, dimensions=dimensions)
            total += len(payload)
        with self.lock:
            for entry in entries:
                if entry:
                    key = entry["identity"]
                    self.positions[key] = max(entry["pos"], self.positions.pop(key, 0))
            while len(self.positions) > 4096:
                self.positions.popitem(last=False)
        live = []
        for index, (parts, offset) in enumerate(refs):
            block = parts[offset]
            if block.get("type") != "image":
                continue
            source = block.get("source", {})
            data = source.get("data") if source.get("type") == "base64" else None
            dimensions = entries[index]["dimensions"] if entries[index] else None
            if dimensions is None and isinstance(data, str) and len(data) <= 64 * MIB:
                try:
                    # Older-than-processing-window images still participate in the
                    # guard's dimension classification without decoding pixel planes.
                    with Image.open(io.BytesIO(base64.b64decode(data, validate=True))) as image:
                        dimensions = image.size
                except UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError:
                    pass
            if dimensions and max(dimensions) > 8000:
                drop(refs[index], "[image omitted: exceeds Anthropic's 8000px per-side limit]")
            elif isinstance(data, str) and len(data) > 5 * MIB:
                drop(refs[index], "[image omitted: exceeds Anthropic's 5MB per-image limit]")
            else:
                live.append((index, len(data) if isinstance(data, str) else 0, dimensions))
        limit = 20 if any(d is None or max(d) > 2000 for _, _, d in live) else 100
        while len(live) > limit:
            drop(refs[live.pop(0)[0]], OLD)
        total = sum(size for _, size, _ in live)
        for index, size, _ in live:
            if total <= budget:
                break
            if size:
                drop(refs[index], TOTAL)
                total -= size
