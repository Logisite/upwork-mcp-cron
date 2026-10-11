"""Watcher core: bucket cadence, adaptive paging, storage, notifications.

Cadence is bucket-based. After each real check a bucket of
90 + random(0..150) seconds is drawn and stored. The ticker drains it one
second at a time; when it reaches zero the next check runs and a fresh
bucket is drawn. Buckets persist in SQLite so they survive the cron
process boundary.

Every job seen is stored, not just notified ones, so "most recent in the
last N minutes" is a local SQL query rather than another round trip.
"""

import asyncio
import contextlib
import fcntl
import random
import socket
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import auth, dispatch
from .mcp_client import AuthError, clean_url, fetch_page, get_job, session
from .notify import log

DATA = Path(__file__).resolve().parent.parent / "data"
DB = DATA / "state.db"
LOCK = DATA / "watch.lock"

# Bucket cadence. 90 base + 0..150 jitter, drained 1s per tick.
BUCKET_BASE_SEC = 90
BUCKET_JITTER_SEC = 150

# Paging. A query stops as soon as a page contributes no new jobs.
PAGE_CAP = 8
PACE_SEC = 0.12

# Storage retention for the job table.
RETENTION_DAYS = 30
STATS_RETENTION_DAYS = 7    # per-check stats rows
VACUUM_EVERY_SEC = 24 * 3600  # reclaim file space once a day

# Notification. A post that is genuinely fresh notifies immediately. A post
# first seen a little late (brief sleep, laptop closed) still notifies, up to
# LATE_NOTIFY_SEC. Anything older is stored but never pops up, so a long
# outage or a first run does not blast a backlog of stale posts.
FRESH_WINDOW_SEC = 300      # 5 min: definitely fresh, full latency recorded
LATE_NOTIFY_SEC = 900       # 15 min: surfaced late, excluded from latency stats

REAUTH_NAG_SEC = 6 * 3600
BACKOFF_CAP_SEC = 1800

QUERIES = [
    ("smart_search", {"limit": 10}),
    ("search", {"title": "AI", "sort": "recency", "limit": 10}),
]


def now() -> int:
    return int(time.time())


def db() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=15)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, title TEXT, url TEXT, pub TEXT, pub_ts INTEGER,
            pay TEXT, country TEXT, tier TEXT, level TEXT, kind TEXT,
            first_seen INTEGER, notified_at INTEGER)"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_pub ON jobs(pub_ts DESC)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    # Rolling per-check stats, for the `stats` command.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS checks(
            ts INTEGER PRIMARY KEY, requests INTEGER, pages INTEGER,
            new_jobs INTEGER, notified INTEGER, secs REAL)"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checks_ts ON checks(ts DESC)")
    conn.commit()
    return conn


def meta_get(conn, k, default=""):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def meta_set(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))
    conn.commit()


def acquire_lock():
    """Non-blocking exclusive lock; None if another run holds it."""
    DATA.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except BlockingIOError:
        return None


def parse_time(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def age_label(pub_ts: int) -> str:
    age_min = max(0, (now() - pub_ts) // 60)
    if age_min < 60:
        return f"{age_min}m ago"
    return f"{age_min // 60}h ago"


def draw_bucket() -> int:
    return BUCKET_BASE_SEC + random.randint(0, BUCKET_JITTER_SEC)


def store_jobs(conn, jobs: list) -> list:
    """Insert jobs, return only the ones not previously seen (new)."""
    new = []
    t = now()
    for j in jobs:
        jid = j.get("id")
        if not jid:
            continue
        pub = j.get("published_date", "")
        try:
            pub_ts = parse_time(pub)
        except Exception:
            pub_ts = t
        client = j.get("client") or {}
        cur = conn.execute(
            """INSERT OR IGNORE INTO jobs
               (id,title,url,pub,pub_ts,pay,country,tier,level,kind,first_seen)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                jid,
                (j.get("title") or "").strip(),
                clean_url(j.get("url", "")),
                pub,
                pub_ts,
                j.get("budget") or j.get("job_type", ""),
                client.get("country", ""),
                j.get("proposals_tier", ""),
                j.get("experience_level", ""),
                j.get("job_type", ""),
                t,
            ),
        )
        if cur.rowcount:
            new.append(j)
    conn.commit()
    return new


async def page_query(sess, kind: str, params: dict, conn) -> tuple:
    """Page one query until it stops yielding new jobs. Guard against
    non-monotonic server ordering: if a page's oldest job is not older than
    the previous page's oldest, stop and flag (ordering is unreliable)."""
    new_all: list = []
    prev_oldest = None
    pages = 0
    stop_reason = "cap"
    for _ in range(PAGE_CAP):
        jobs = await fetch_page(sess, kind, dict(params))
        pages += 1
        if not jobs:
            stop_reason = "empty"
            break
        new = store_jobs(conn, jobs)
        new_all.extend(new)
        if not new:
            stop_reason = "no-new"
            break
        try:
            oldest = min(parse_time(j["published_date"]) for j in jobs)
        except Exception:
            oldest = None
        if oldest is not None and prev_oldest is not None and oldest >= prev_oldest:
            stop_reason = "ordering-guard"
            log(f"ordering guard tripped on {kind}: page not older than previous")
            break
        if oldest is not None:
            prev_oldest = oldest
        await asyncio.sleep(PACE_SEC)
    return new_all, pages, stop_reason


def _notifyable(pub_ts: int) -> bool:
    age = now() - pub_ts
    return 0 <= age <= LATE_NOTIFY_SEC


def _is_fresh(pub_ts: int) -> bool:
    """Fresh enough that latency counts toward the 2 min benchmark."""
    return 0 <= now() - pub_ts <= FRESH_WINDOW_SEC


def job_body(j: dict, pub_ts: int, extra: dict | None = None) -> str:
    """Layout 3: pay line first, then the link, then cost and client stats.

    The proposals/age line is dropped because the notification summary
    already carries both. Skills are dropped to keep the popup short.
    """
    extra = extra or {}
    kind = "hourly" if j.get("job_type") == "hourly" else "fixed"
    country = (j.get("client") or {}).get("country", "?")
    pay = j.get("budget") or j.get("job_type", "?")

    lines = [
        (j.get("title") or "").strip(),
        "",
        f"{pay} {kind} · {country} · {j.get('experience_level', '?')}",
        clean_url(j.get("url", "")),
    ]

    bits = []
    if extra.get("connects_cost"):
        bits.append(f"{extra['connects_cost']} Connects")
    if extra.get("jobs_posted"):
        bits.append(f"{extra['jobs_posted']} jobs")
    if extra.get("hire_rate") is not None:
        bits.append(f"{extra['hire_rate']}% hire")
    # One invite already indicates an invite-first client rather than an
    # open competition, so flag from 1 upward.
    if (extra.get("invites_sent") or 0) >= 1:
        bits.append(f"sent {extra['invites_sent']} invites")
    if bits:
        lines.append(" · ".join(bits))

    return "\n".join(lines)


def job_summary(j: dict, pub_ts: int) -> str:
    return f"{age_label(pub_ts)} · {j.get('proposals_tier', '?')} proposals"


async def check_once(conn, sess) -> None:
    from . import mcp_client

    t_start = time.time()
    mcp_client.reset_requests()
    total_new = 0
    total_pages = 0
    fresh: list = []
    for kind, params in QUERIES:
        new, pages, stop = await page_query(sess, kind, params, conn)
        total_new += len(new)
        total_pages += pages
        for j in new:
            try:
                pub_ts = parse_time(j["published_date"])
            except Exception:
                pub_ts = now()
            fresh.append((j, pub_ts))
        log(f"query {kind}: {pages} page(s), {len(new)} new, stop={stop}")
    # de-dup across queries by id (same job can appear in both feeds)
    seen_ids = set()
    unique = []
    for j, pub_ts in fresh:
        if j["id"] not in seen_ids:
            seen_ids.add(j["id"])
            unique.append((j, pub_ts))
    unique.sort(key=lambda x: x[1])  # oldest first
    notified = 0
    for j, pub_ts in unique:
        if not _notifyable(pub_ts):
            continue
        # One extra call per NEW job only, to pull the facts that search
        # results omit: connects cost, client history, invite behaviour.
        try:
            extra = await get_job(sess, j["id"])
        except Exception as e:
            log(f"enrich failed for {j['id']}: {e}")
            extra = {}
        if extra.get("url"):
            conn.execute("UPDATE jobs SET url=? WHERE id=?", (extra["url"], j["id"]))
        t = now()
        dispatch.notify(
            job_summary(j, pub_ts),
            job_body(j, pub_ts, extra),
            url=extra.get("url") or clean_url(j.get("url", "")),
            sticky=True,
        )
        notified += 1
        if _is_fresh(pub_ts):
            # Only fresh posts count toward the latency benchmark; a late
            # catch-up would otherwise poison the distribution.
            conn.execute("UPDATE jobs SET notified_at=? WHERE id=?", (t, j["id"]))
        log(f"notified {j['id']} +{t - pub_ts}s {(j.get('title') or '')[:50]}")
    conn.commit()
    reqs = mcp_client.requests_made()
    record_check(
        conn, reqs, total_pages, len(unique), notified, time.time() - t_start
    )
    meta_set(conn, "last_check", now())
    meta_set(conn, "backoff_level", 0)
    meta_set(conn, "backoff_until", 0)
    log(
        f"check done: {len(unique)} new, {notified} notified, "
        f"{total_pages} pages, {reqs} requests, {time.time() - t_start:.1f}s"
    )


def prune(conn) -> None:
    """Retention. Runs every loop iteration, not only on success, and vacuums
    periodically so the file actually shrinks instead of only freeing pages."""
    cutoff = now() - RETENTION_DAYS * 86400
    cur = conn.execute("DELETE FROM jobs WHERE first_seen < ?", (cutoff,))
    conn.execute(
        "DELETE FROM checks WHERE ts < ?", (now() - STATS_RETENTION_DAYS * 86400,)
    )
    conn.commit()
    # VACUUM reclaims file space; it is expensive so run it rarely.
    try:
        last = int(meta_get(conn, "last_vacuum", "0"))
    except ValueError:
        last = 0
    if now() - last > VACUUM_EVERY_SEC:
        conn.isolation_level = None
        conn.execute("VACUUM")
        conn.isolation_level = ""
        meta_set(conn, "last_vacuum", now())
        if cur.rowcount:
            log(f"pruned {cur.rowcount} old job(s), vacuumed")


def record_check(conn, requests: int, pages: int, new_jobs: int, notified: int, secs: float) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO checks(ts,requests,pages,new_jobs,notified,secs)"
        " VALUES(?,?,?,?,?,?)",
        (now(), requests, pages, new_jobs, notified, round(secs, 2)),
    )
    conn.commit()


def reauth_nag(conn) -> None:
    try:
        last = int(meta_get(conn, "reauth_at", "0"))
    except ValueError:
        last = 0
    if now() - last > REAUTH_NAG_SEC:
        notify(
            "Upwork watcher: sign-in needed",
            "Run: uv run --project ~/projects/efficiency/upwork-cron upwork-cron auth",
            urgency="critical",
        )
        meta_set(conn, "reauth_at", now())
    log("auth missing/invalid")


def _backoff(conn) -> int:
    try:
        lvl = int(meta_get(conn, "backoff_level", "0")) + 1
    except ValueError:
        lvl = 1
    wait = min(30 * 2 ** min(lvl, 6), BACKOFF_CAP_SEC)
    meta_set(conn, "backoff_level", lvl)
    meta_set(conn, "backoff_until", now() + wait)
    return wait


async def open_session(token: str):
    """Create AND enter a fresh session in one call.

    contextlib async context managers are single-use: re-entering an exited
    one raises `AttributeError: ... has no attribute 'args'`. A previous
    version reused one object across checks, so a single transient failure
    poisoned every later check permanently. Never store and re-enter;
    always build a new object per open.
    """
    ctx = session(token)
    sess = await ctx.__aenter__()
    return ctx, sess


async def close_session(ctx) -> None:
    if ctx is None:
        return
    try:
        await ctx.__aexit__(None, None, None)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        # Includes CancelledError from orphaned SDK scopes during teardown,
        # and RuntimeError from exiting a never-entered context. Either way
        # there is nothing left worth keeping open.
        pass


async def fresh_token() -> str:
    """Access token, refreshing silently. AuthError on any token problem so
    callers take the re-auth path instead of retrying with a dead token."""
    try:
        tok = auth.refresh()
    except Exception as e:
        raise AuthError(f"refresh failed: {e}")
    if not tok or not tok.get("access_token"):
        raise AuthError("no token stored")
    return tok["access_token"]


# Set by the SIGTERM handler so the CancelledError handler below can tell a
# genuine shutdown apart from an SDK task-group cancellation (which arrives
# as the same exception type).
_shutting_down = False


def request_shutdown() -> None:
    global _shutting_down
    _shutting_down = True


def _dns_note() -> str:
    """What does this machine's resolver say about the MCP host? A sinkhole
    answer (0.0.0.0 / ::) means DNS-level blocking, not an Upwork outage."""
    try:
        addrs = sorted({i[4][0] for i in socket.getaddrinfo("mcp.upwork.com", 443)})
    except Exception as e:
        return f"resolver failed: {e}"
    if any(a in ("0.0.0.0", "::") for a in addrs):
        return f"resolver returns sinkhole {addrs} for mcp.upwork.com"
    return f"resolver returns {addrs}"


async def _run_check(conn) -> tuple:
    """Run one full check: open session, page, notify, close.

    MUST run inside its own child task (see run_loop). The MCP SDK leaks
    anyio cancel scopes into the calling task when a session fails: a
    scope entered here can only ever cancel THIS task, never the ticker.
    Everything, including CancelledError, is converted to a plain outcome
    tuple so the parent only ever receives strings. Genuine shutdown
    signals (SystemExit/KeyboardInterrupt) still propagate.
    """
    try:
        sess_ctx, sess = await open_session(await fresh_token())
        try:
            await check_once(conn, sess)
        finally:
            await close_session(sess_ctx)
        return ("ok", "")
    except (KeyboardInterrupt, SystemExit):
        raise
    except AuthError as e:
        return ("auth", str(e))
    except asyncio.CancelledError:
        return ("cancelled", _dns_note())
    except Exception as e:
        return ("error", str(e))


async def run_loop() -> None:
    """1s ticker. Drains the bucket; runs a check when it hits zero.

    Each check runs in a child task so leaked SDK cancel scopes die with
    the child. The ticker itself never awaits SDK coroutines directly and
    therefore can never be scope-cancelled; the only CancelledError it can
    see is a genuine external shutdown, which re-raises.
    """
    conn = db()
    while True:
        meta_set(conn, "last_tick", now())
        # Prune every iteration so retention holds even while checks are
        # failing, not only on the success path.
        prune(conn)
        bucket = meta_get(conn, "bucket", "")
        # No bucket yet: draw one and store.
        if bucket == "":
            b = draw_bucket()
            meta_set(conn, "bucket", b)
            log(f"bucket drawn: {b}s")
            await asyncio.sleep(1)
            continue
        b = int(bucket)
        if b <= 0:
            # time to check; isolated in a child task (see _run_check)
            try:
                checker = asyncio.create_task(_run_check(conn))
                status, detail = await checker
            except (KeyboardInterrupt, SystemExit):
                raise
            except asyncio.CancelledError:
                raise  # not from a check (those are strings now): real shutdown
            if status == "auth":
                log(f"auth error: {detail}")
                reauth_nag(conn)
                await asyncio.sleep(60)
                continue
            if status != "ok":
                log(f"check {status}: {detail}")
                wait = _backoff(conn)
                await asyncio.sleep(wait)
                continue
            # fresh bucket for next cycle
            nb = draw_bucket()
            meta_set(conn, "bucket", nb)
            log(f"bucket redrawn: {nb}s")
        else:
            meta_set(conn, "bucket", b - 1)
        await asyncio.sleep(1)