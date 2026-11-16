"""Supplement live model metadata with the selected vendor's official documentation."""

import asyncio
import copy
import json
import re
import time
from datetime import UTC, datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

DEEPSEEK_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"


class ModelTable(HTMLParser):
    """Expand the documentation table's row/column spans into labelled rows."""

    def __init__(self):
        super().__init__()
        self.tables = []
        self.table = None
        self.row = None
        self.cell = None
        self.spans = {}
        self.column = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.table = []
            self.spans = {}
        elif tag == "tr" and self.table is not None:
            self.row = {}
            self.column = 0
            for column, (remaining, value) in list(self.spans.items()):
                self.row[column] = value
                if remaining == 1:
                    del self.spans[column]
                else:
                    self.spans[column] = remaining - 1, value
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = ([], int(attrs.get("colspan", 1)), int(attrs.get("rowspan", 1)))
        elif tag == "br" and self.cell:
            self.cell[0].append(" ")

    def handle_data(self, data):
        if self.cell:
            self.cell[0].append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell:
            parts, width, height = self.cell
            value = " ".join("".join(parts).split())
            while self.column in self.row:
                self.column += 1
            for column in range(self.column, self.column + width):
                self.row[column] = value
                if height > 1:
                    self.spans[column] = height - 1, value
            self.column += width
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.table.append([self.row.get(i, "") for i in range(max(self.row, default=-1) + 1)])
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def _tokens(value):
    match = re.search(r"(\d+(?:\.\d+)?)\s*([KM])\b", value)
    return int(float(match[1]) * {"K": 1024, "M": 1024**2}[match[2]]) if match else None


def parse_deepseek(html, updated_at):
    parser = ModelTable()
    parser.feed(html)
    table = next((rows for rows in parser.tables if rows and rows[0][0] == "MODEL"), None)
    if table is None:
        raise ValueError("Official model table is unavailable.")
    text = " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())
    schedule = re.search(
        r"Peak hours are (\d{2}):(\d{2}) - (\d{2}):(\d{2}) and "
        r"(\d{2}):(\d{2}) - (\d{2}):(\d{2}) UTC, Monday through Friday, "
        r"excluding Chinese public holidays",
        text,
    )
    peak_minutes = None
    if schedule:
        parts = list(map(int, schedule.groups()))
        times = [parts[i] * 60 + parts[i + 1] for i in range(0, 8, 2)]
        if 0 <= times[0] < times[1] <= times[2] < times[3] <= 1440:
            peak_minutes = [times[:2], times[2:]]
    result = {}
    for column, label in enumerate(table[0]):
        identity = re.sub(r"\(\d+\)$", "", label).strip()
        if not identity.startswith("deepseek-"):
            continue
        metadata = {}
        bands = {}
        for row in table[1:]:
            if column >= len(row):
                continue
            value = row[column]
            if row[0] == "MODEL VERSION":
                metadata["displayName"] = value
            elif row[0] == "THINKING MODE" and "Supports both non-thinking and thinking" in value:
                metadata["supportsReasoning"] = True
                metadata["supportsThinkingToggle"] = True
                metadata["defaultThinkingEnabled"] = "thinking (default)" in value
            elif row[0] in {"CONTEXT LENGTH", "MAX OUTPUT"}:
                field = "contextWindow" if row[0] == "CONTEXT LENGTH" else "maxOutputTokens"
                if tokens := _tokens(value):
                    metadata[field] = tokens
            elif "Vision" in row[:column]:
                if value in {"✓", "Not supported"}:
                    metadata["inputModalities"] = ["text", "image"] if value == "✓" else ["text"]
            elif row[0].startswith("PRICING") and re.fullmatch(r"\$\d+(?:\.\d+)?", value):
                kind = (
                    "cacheRead"
                    if "CACHE HIT" in row[1]
                    else "input"
                    if "INPUT" in row[1]
                    else "output"
                    if "OUTPUT" in row[1]
                    else None
                )
                band = {"OFF-PEAK": "offPeak", "PEAK": "peak"}.get(row[2])
                if kind and band:
                    bands.setdefault(band, {})[kind] = float(value[1:])
        provenance = {
            "source": "official",
            "sourceRef": DEEPSEEK_SOURCE,
            "updatedAt": updated_at,
            "stale": False,
        }
        metadata["provenance"] = provenance
        if bands and (
            set(bands) != {"offPeak", "peak"}
            or any(set(rates) != {"input", "output", "cacheRead"} for rates in bands.values())
        ):
            raise ValueError("Official price table is incomplete.")
        if bands:
            metadata["pricing"] = {
                "currency": "USD",
                "unit": "per_million_tokens",
                "bands": bands,
                "peakMinutesUTC": peak_minutes,
                "sourceRef": DEEPSEEK_SOURCE,
                "verifiedAt": updated_at,
                "condition": "See the official pricing page for peak hours and holiday exceptions.",
            }
        result[identity] = metadata
    if not result:
        raise ValueError("Official model table contains no model metadata.")
    return result


class OfficialModelMetadata:
    def __init__(self, directory):
        self.path = directory / "official-model-metadata.json"
        self.lock = asyncio.Lock()
        self.checked = 0.0
        self.models = {}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("source") == DEEPSEEK_SOURCE and isinstance(saved.get("models"), dict):
                self.models = saved["models"]
                self.checked = float(saved.get("checked", 0)) if saved.get("version") == 2 else 0
        except OSError, ValueError, TypeError:
            pass

    @staticmethod
    def applies(provider):
        destination = urlsplit(provider.endpoints.base_url)
        return (
            provider.auth_mode != "oauth"
            and destination.scheme == "https"
            and destination.hostname == "api.deepseek.com"
            and destination.port in (None, 443)
            and destination.path.rstrip("/") in {"", "/v1", "/anthropic"}
        )

    def cached(self, provider):
        return copy.deepcopy(self.models) if self.applies(provider) else {}

    async def read(self, provider, *, force=False):
        if not self.applies(provider):
            return {}
        started = time.time()
        async with self.lock:
            if self.checked >= started or (not force and started - self.checked < 86400):
                return copy.deepcopy(self.models)
            try:
                async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                    response = await client.get(DEEPSEEK_SOURCE)
                    response.raise_for_status()
                updated = datetime.now(UTC).isoformat()
                models = parse_deepseek(response.text, updated)
                self.models = models
                self.checked = time.time()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    json.dumps(
                        {
                            "version": 2,
                            "source": DEEPSEEK_SOURCE,
                            "checked": self.checked,
                            "models": models,
                        }
                    ),
                    encoding="utf-8",
                )
            except httpx.HTTPError, OSError, ValueError:
                # A documentation outage does not discard the last known metadata.
                self.checked = time.time() - 86400 + 300
                for metadata in self.models.values():
                    metadata["provenance"]["stale"] = True
            return copy.deepcopy(self.models)
