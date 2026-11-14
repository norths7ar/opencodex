"""Bounded public citations, ported from web-search/sources.ts."""

import json
import re
from urllib.parse import urlsplit

CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _bytes(value):
    return len(value.encode("utf-8", errors="replace"))


def _size(value):
    return _bytes(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def append_source(target, source):
    if len(target) >= 20 or not isinstance(source, dict):
        return False
    url = source.get("url")
    if (
        not isinstance(url, str)
        or not url
        or url.strip() != url
        or CONTROL.search(url)
        or _bytes(url) > 2048
        or any(item["url"] == url for item in target)
    ):
        return False
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        _ = parsed.port
    except ValueError:
        return False
    title = source.get("title")
    item = {"url": url}
    if (
        isinstance(title, str)
        and title.strip()
        and not CONTROL.search(title)
        and _bytes(title) <= 256
    ):
        item["title"] = title
    if sum(map(_size, target)) + _size(item) > 16384:
        return False
    target.append(item)
    return True


def _header(line):
    line = line.lstrip()
    if line.startswith("#"):
        hashes = len(line) - len(line.lstrip("#"))
        if hashes > 6:
            return False
        line = line[hashes:].lstrip()
    line = line.lstrip(" \t\r\n-*>\v\f")
    match = re.match(r"sources?", line, re.IGNORECASE)
    if not match:
        return False
    tail = line[match.end() :].lstrip().lstrip("*").lstrip()
    if tail.startswith(":"):
        tail = tail[1:].lstrip()
    return not tail.lstrip("*").strip()


def _title(prefix):
    value = re.sub(r"^[-*+>\d.)\s]+", "", prefix).strip()
    value = re.sub(r"^\[", "", value)
    value = re.sub(r"\]\(?$", "", value)
    return value.rstrip(":-—(<` \t").strip()


def _mask_code(line):
    runs = list(re.finditer(r"`+", line))
    following, last = {}, {}
    for index in range(len(runs) - 1, -1, -1):
        length = runs[index].end() - runs[index].start()
        following[index] = last.get(length)
        last[length] = index
    chars = list(line)
    index = 0
    while index < len(runs):
        close = following[index]
        if close is None:
            index += 1
            continue
        start, end = runs[index].start(), runs[close].end()
        chars[start:end] = [" "] * (end - start)
        index = close + 1
    return "".join(chars)


def extract_trailing_sources(text):
    """Move the last Sources list into citations, retaining notes and following prose."""
    lines = text.split("\n")
    header = next((i for i in range(len(lines) - 1, -1, -1) if _header(lines[i])), None)
    if header is None:
        return text, []
    sources, notes = [], []
    consumed, last_consumed = False, header
    pending, pending_index = None, -1
    for index in range(header + 1, len(lines)):
        raw = lines[index].strip()
        if not raw:
            if consumed or pending is not None:
                break
            continue
        http = re.search(r"https?://[^\s<>()\[\]]+", raw, re.IGNORECASE)
        match = http or re.search(
            r"[a-z][a-z0-9+.-]{0,31}:[^\s<>()\[\]]+", _mask_code(raw), re.IGNORECASE
        )
        list_item = bool(re.match(r"[-*+>\d.)]", raw))
        if not match:
            if not list_item:
                break
            if pending is not None:
                notes.append(pending_index)
            pending, pending_index = raw, index
            continue
        if not http and not list_item and match.start() != 0:
            break
        url = match.group().lstrip("<").rstrip(")>].,;:`")
        if not url:
            break
        inline = _title(raw[: match.start()])
        starts_list = bool(re.match(r"(?:[-*+>]|\d+[.)])\s", raw))
        paired = pending is not None and not inline and not starts_list
        if pending is not None and not paired:
            notes.append(pending_index)
        title = inline or (_title(pending) if paired else "")
        pending = None
        consumed, last_consumed = True, index
        append_source(sources, {"url": url, **({"title": title} if title else {})})
    if not consumed:
        return text, []
    before = "\n".join(lines[:header]).rstrip()
    after = "\n".join(
        part
        for part in [
            *(lines[index] for index in notes if index < last_consumed),
            "\n".join(lines[last_consumed + 1 :]).lstrip(),
        ]
        if part
    )
    return "\n\n".join(part for part in (before, after) if part), sources
