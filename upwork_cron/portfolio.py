"""A small, user-owned portfolio built from job listings.

Items live in `portfolio.json` inside the data directory, so they survive
upgrades. `fetch` pulls the latest listings either from the local watcher
database (no network) or live from Upwork MCP; `generate` renders them into a
Markdown or HTML document.
"""

from __future__ import annotations

import html
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .paths import DATA, ensure_data_dir

PORTFOLIO = DATA / "portfolio.json"


class PortfolioError(ValueError):
    """Invalid portfolio input or unreadable portfolio file."""


def _text(value, default: str = "") -> str:
    return str(value).strip() if value is not None else default


def _normalize(item) -> dict:
    if not isinstance(item, dict):
        raise PortfolioError(f"invalid portfolio item: {item!r}")
    title = _text(item.get("title"))
    if not title:
        raise PortfolioError("a portfolio item needs a title")
    return {
        "id": _text(item.get("id")),
        "title": title,
        "url": _text(item.get("url")),
        "description": _text(item.get("description")),
        "pay": _text(item.get("pay")),
        "source": _text(item.get("source"), "manual"),
        "added_at": int(item.get("added_at") or time.time()),
    }


def load() -> list[dict]:
    if not PORTFOLIO.exists():
        return []
    try:
        raw = json.loads(PORTFOLIO.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PortfolioError(f"could not read {PORTFOLIO}: {e}") from e
    if not isinstance(raw, list):
        raise PortfolioError(f"{PORTFOLIO} must contain a JSON list")
    return [_normalize(item) for item in raw]


def save(items: list[dict]) -> None:
    ensure_data_dir()
    fd, tmp = tempfile.mkstemp(prefix="portfolio.", suffix=".tmp", dir=str(DATA))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, PORTFOLIO)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def find(selector) -> tuple[int, dict] | None:
    items = load()
    text = _text(selector)
    if text.isdigit():
        i = int(text) - 1
        return (i, items[i]) if 0 <= i < len(items) else None
    needle = text.casefold()
    for i, item in enumerate(items):
        if needle in item["title"].casefold() or needle in item["id"].casefold():
            return i, item
    return None


def _same(a: dict, b: dict) -> bool:
    if a.get("id") and a.get("id") == b.get("id"):
        return True
    if a.get("url") and a.get("url") == b.get("url"):
        return True
    return a["title"].casefold() == b["title"].casefold()


def upsert(item: dict) -> bool:
    """Add unless already present. Returns True when it was added."""
    entry = _normalize(item)
    items = load()
    for existing in items:
        if _same(existing, entry):
            return False
    items.append(entry)
    save(items)
    return True


def add(title: str, url: str = "", description: str = "", pay: str = "",
        source: str = "manual") -> bool:
    return upsert({
        "title": title, "url": url, "description": description,
        "pay": pay, "source": source,
    })


def remove(selector) -> dict | None:
    hit = find(selector)
    if hit is None:
        return None
    i, item = hit
    items = load()
    items.pop(i)
    save(items)
    return item


def clear() -> int:
    count = len(load())
    if count:
        save([])
    return count


def _from_job(job: dict, source: str) -> dict:
    from .mcp_client import clean_url

    url = clean_url(_text(job.get("url")))
    pay = _text(job.get("budget") or job.get("pay"))
    job_type = _text(job.get("job_type"))
    if job_type:
        pay = pay if pay.casefold() == job_type.casefold() else f"{pay} {job_type}".strip()
    description = _text(job.get("description") or job.get("snippet"))
    return {
        "id": _text(job.get("id")) or url,
        "title": _text(job.get("title"), "Untitled listing"),
        "url": url,
        "description": description,
        "pay": pay,
        "source": source,
    }


def fetch_stored(since_minutes: int = 0, limit: int = 20) -> int:
    """Import recent listings already stored by the watcher (no network)."""
    from .watcher import db

    conn = db()
    sql = "SELECT id,title,url,pub,pay,country,level,kind AS job_type FROM jobs"
    params: list = []
    if since_minutes:
        sql += " WHERE first_seen >= ?"
        params.append(int(time.time()) - since_minutes * 60)
    sql += " ORDER BY pub_ts DESC LIMIT ?"
    params.append(limit)
    added = 0
    for row in conn.execute(sql, params).fetchall():
        jid, title, url, pub, pay, country, level, kind = row
        job = {
            "id": jid, "title": title, "url": url, "pub": pub,
            "pay": pay, "country": country, "experience_level": level,
            "job_type": kind,
        }
        if upsert(_from_job(job, "stored")):
            added += 1
    return added


def fetch_live(limit: int = 10, use_keywords: bool = True) -> int:
    """Import the latest listings from live Upwork MCP searches."""
    from . import keywords
    from .mcp_client import search as mcp_search

    queries = keywords.queries() if use_keywords else [
        ("smart_search", {"limit": limit})
    ]
    added = 0
    for kind, params in queries:
        call = dict(params)
        call["limit"] = limit
        rows = mcp_search(kind, call)
        for job in rows:
            source = kind if kind == "smart_search" else f"search: {params.get('title') or params.get('query') or 'keyword'}"
            if upsert(_from_job(job, source)):
                added += 1
    return added


def _safe_url(url: str) -> str:
    if urlparse(url).scheme in ("http", "https"):
        return url
    return ""


def render_markdown(items: list[dict]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Portfolio",
        "",
        f"_Generated by upwork-cron on {stamp} — {len(items)} item(s)._",
        "",
    ]
    for i, item in enumerate(items, 1):
        lines += [f"## {i}. {item['title']}", ""]
        if item["pay"]:
            lines.append(f"**Pay:** {item['pay']}")
        if item["source"]:
            lines.append(f"**Source:** {item['source']}")
        if item["url"]:
            lines.append(f"**Link:** <{item['url']}>")
        lines.append("")
        if item["description"]:
            lines += [item["description"], ""]
    return "\n".join(lines).rstrip() + "\n"


def render_html(items: list[dict]) -> str:
    esc = html.escape
    cards = []
    for item in items:
        title = esc(item["title"])
        pay = f'<p class="pay">{esc(item["pay"])}</p>' if item["pay"] else ""
        source = f'<p class="source">{esc(item["source"])}</p>' if item["source"] else ""
        desc = f'<p class="desc">{esc(item["description"])}</p>' if item["description"] else ""
        url = _safe_url(item["url"])
        link = f'<a href="{esc(url, quote=True)}" target="_blank" rel="noopener">View listing</a>' if url else ""
        cards.append(
            f'    <article><h2>{title}</h2>{pay}{source}{link}{desc}</article>'
        )
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem;color:#222}}
article{{border:1px solid #ddd;border-radius:8px;padding:1rem 1.25rem;margin:1rem 0}}
h2{{margin:0 0 .5rem;font-size:1.15rem}}
.pay{{color:#0a6;font-weight:600}}
.source{{color:#777;font-size:.85rem}}
.desc{{white-space:pre-wrap}}
a{{color:#06c}}
small{{color:#888}}
</style>
</head>
<body>
<h1>Portfolio</h1>
<small>Generated by upwork-cron on {esc(stamp)} — {len(items)} item(s).</small>
{chr(10).join(cards)}
</body>
</html>
"""


def generate(output: Path | None = None, fmt: str = "md") -> Path:
    items = load()
    suffix = "html" if fmt == "html" else "md"
    path = output or (DATA / f"portfolio.{suffix}")
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render_html(items) if fmt == "html" else render_markdown(items)
    path.write_text(text, encoding="utf-8")
    return path
