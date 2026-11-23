"""User-managed keyword searches, stored next to the rest of the user's data.

`smart_search` is always query #1. Every keyword added with `upcron keyword add`
adds a `search` query after it. Keeping the list in the data directory means it
survives upgrades and is per-user, with no hardcoded keywords in the package.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .paths import DATA, ensure_data_dir

KEYWORDS = DATA / "keywords.json"
KINDS = ("title", "query")
MAX_KEYWORDS = 20
MAX_TEXT = 80
DEFAULT_LIMIT = 10


class KeywordError(ValueError):
    """Invalid keyword input or an unreadable keyword file."""


def _clean_text(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        raise KeywordError("keyword cannot be empty")
    if len(text) > MAX_TEXT:
        raise KeywordError(f"keyword is too long (max {MAX_TEXT} characters)")
    return text


def _clean_kind(kind: str) -> str:
    kind = str(kind or "title").strip().lower()
    if kind not in KINDS:
        raise KeywordError(f"kind must be one of: {', '.join(KINDS)}")
    return kind


def _clean_limit(limit) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError) as e:
        raise KeywordError("limit must be a number between 1 and 10") from e
    if not 1 <= limit <= 10:
        raise KeywordError("limit must be between 1 and 10")
    return limit


def _normalize(item) -> dict:
    """Accept our own dict format and the simple list-of-strings format."""
    if isinstance(item, str):
        item = {"text": item}
    if not isinstance(item, dict):
        raise KeywordError(f"invalid keyword entry: {item!r}")
    return {
        "text": _clean_text(item.get("text") or item.get("keyword")),
        "kind": _clean_kind(item.get("kind", "title")),
        "limit": _clean_limit(item.get("limit", DEFAULT_LIMIT)),
    }


def load() -> list[dict]:
    if not KEYWORDS.exists():
        return []
    try:
        raw = json.loads(KEYWORDS.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise KeywordError(f"could not read {KEYWORDS}: {e}") from e
    if not isinstance(raw, list):
        raise KeywordError(f"{KEYWORDS} must contain a JSON list")
    return [_normalize(item) for item in raw]


def save(entries: list[dict]) -> None:
    ensure_data_dir()
    fd, tmp = tempfile.mkstemp(prefix="keywords.", suffix=".tmp", dir=str(DATA))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, KEYWORDS)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def find(selector) -> tuple[int, dict] | None:
    """Look up by 1-based index (from `keyword list`) or by keyword text."""
    entries = load()
    text = str(selector).strip()
    if text.isdigit():
        i = int(text) - 1
        if 0 <= i < len(entries):
            return i, entries[i]
        return None
    needle = text.casefold()
    for i, entry in enumerate(entries):
        if entry["text"].casefold() == needle:
            return i, entry
    return None


def entry_for(text: str, kind: str = "title", limit: int = DEFAULT_LIMIT) -> dict:
    """Validate input and return a keyword entry without saving it."""
    return _normalize({"text": text, "kind": kind, "limit": limit})


def add(text: str, kind: str = "title", limit: int = DEFAULT_LIMIT) -> tuple[bool, dict]:
    entry = entry_for(text, kind, limit)
    entries = load()
    for existing in entries:
        if existing["text"].casefold() == entry["text"].casefold():
            return False, existing
    if len(entries) >= MAX_KEYWORDS:
        raise KeywordError(f"at most {MAX_KEYWORDS} keywords are allowed")
    entries.append(entry)
    save(entries)
    return True, entry


def remove(selector) -> dict | None:
    hit = find(selector)
    if hit is None:
        return None
    i, entry = hit
    entries = load()
    entries.pop(i)
    save(entries)
    return entry


def clear() -> int:
    count = len(load())
    if count:
        save([])
    return count


def params_for(entry: dict) -> dict:
    """Turn a stored keyword into Upwork MCP `search` params."""
    return {
        entry["kind"]: entry["text"],
        "sort": "recency",
        "limit": entry["limit"],
    }


def queries() -> list[tuple[str, dict]]:
    """smart_search is always first; keywords are appended, in list order."""
    out = [("smart_search", {"limit": DEFAULT_LIMIT})]
    for entry in load():
        out.append(("search", params_for(entry)))
    return out
