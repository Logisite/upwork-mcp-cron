"""Watcher core: bucket cadence, adaptive paging, storage, notifications.

Cadence is bucket-based. After each real check a bucket of
90 + random(0..150) seconds is drawn and stored. The ticker drains it one
second at a time; when it reaches zero the next check runs and a fresh
bucket is drawn. Buckets persist in SQLite so they survive the cron
process boundary.

Every job seen is stored, not just notified ones, so "most recent in the
last N minutes" is a local SQL query rather than another round trip.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import socket
import sqlite3
import time
from datetime import datetime

from . import auth, dispatch, lockfile
from .mcp_client import (
    AuthError,
    NetworkError,
    clean_url,
    fetch_page,
    get_job,
    session,
)
from .notify import log
from .paths import DB, ensure_data_dir

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
BACKOFF_LEVEL_CAP = 6
BACKOFF_BASE_SEC = 30

# While backing off, wake this often to re-test the host instead of sleeping
# the whole penalty in one uninterruptible block. A bare TCP connect costs
# ~0.25s and needs no token, so recovery lands in seconds rather than up to
# BACKOFF_CAP_SEC. This only shortens the wait for FRESH jobs; it never widens
# the notify window.
BACKOFF_PROBE_SEC = 5.0
PROBE_TIMEOUT_SEC = 3.0

import sys as _sys

if _sys.platform == "win32":
    # Windows uses Upwork's profile-matched feed only.
    QUERIES = [("smart_search", {"limit": 10})]
else:
    QUERIES = [
        ("smart_search", {"limit": 10}),
        ("search", {"title": "AI", "sort": "recency", "limit": 10}),
    ]


def now() -> int:
    return int(time.time())


def db() -> sqlite3.Connection:
    ensure_data_dir()
    conn = sqlite3.connect(str(DB), timeout=15)
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
    return lockfile.acquire()


def request_stop(conn) -> None:
    meta_set(conn, "stop_requested", 1)


def clear_stop(conn) -> None:
    meta_set(conn, "stop_requested", 0)


def stop_requested(conn) -> bool:
    try:
        return int(meta_get(conn, "stop_requested", "0") or 0) != 0
    except ValueError:
        return False


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
    skipped_stale = 0
    for j, pub_ts in unique:
        if not _notifyable(pub_ts):
            # Counted, not just dropped. The window is deliberate; the silence
            # was not: an outage produced no record of what it cost.
            skipped_stale += 1
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
    clear_failures(conn)
    log(
        f"check done: {len(unique)} new, {notified} notified, "
        f"{skipped_stale} skipped (older than {LATE_NOTIFY_SEC}s), "
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
    """Tell the user their sign-in is needed. At most once per nag window.

    Reached only for a genuine AuthError, never for a network fault: telling
    someone to sign in again when their token is fine wastes their time and
    teaches them to ignore the message.
    """
    try:
        last = int(meta_get(conn, "reauth_at", "0"))
    except ValueError:
        last = 0
    if now() - last > REAUTH_NAG_SEC:
        # dispatch.notify, not a bare notify: this module imports only `log`
        # from .notify, and the bare name raised NameError, killing the loop.
        dispatch.notify(
            "Upwork watcher: sign-in needed",
            "Run: uv run --project ~/projects/efficiency/upwork-cron upwork-cron auth",
            urgency="critical",
        )
        meta_set(conn, "reauth_at", now())
    log("auth missing/invalid")


def _backoff(conn, reason: str = "") -> int:
    """Escalating wait after a failed check.

    The stored level is clamped alongside the wait. It used to increment
    without limit while the wait itself was capped, so the number in the
    database stopped describing anything.
    """
    try:
        lvl = int(meta_get(conn, "backoff_level", "0")) + 1
    except ValueError:
        lvl = 1
    lvl = min(lvl, BACKOFF_LEVEL_CAP)
    wait = min(BACKOFF_BASE_SEC * 2**lvl, BACKOFF_CAP_SEC)
    meta_set(conn, "backoff_level", lvl)
    meta_set(conn, "consec_failures", lvl)
    meta_set(conn, "backoff_until", now() + wait)
    if reason:
        meta_set(conn, "last_error", reason[:200])
        meta_set(conn, "last_error_at", now())
    return wait


def clear_failures(conn) -> None:
    """Reset the failure streak after a check that actually worked."""
    meta_set(conn, "backoff_level", 0)
    meta_set(conn, "consec_failures", 0)
    meta_set(conn, "backoff_until", 0)


def _host_reachable(host: str = "mcp.upwork.com", port: int = 443) -> bool:
    """Can we open a TCP connection to the MCP host right now?

    Deliberately not an MCP call: no token, no handshake, ~0.25s. It answers
    only "is the network up", which is all the backoff loop needs to know
    before spending a real request.
    """
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_SEC):
            return True
    except OSError:
        return False


async def sleep_backoff(conn, wait: int) -> None:
    """Wait out a backoff, but return early the moment the host is reachable.

    A single asyncio.sleep(BACKOFF_CAP_SEC) cannot observe recovery, so the
    loop's retry schedule was pure luck. This re-tests every BACKOFF_PROBE_SEC
    so a restored network is picked up in seconds. Also exits early on stop.
    """
    deadline = now() + wait
    while True:
        if stop_requested(conn):
            return
        remaining = deadline - now()
        if remaining <= 0:
            return
        await asyncio.sleep(min(BACKOFF_PROBE_SEC, remaining))
        if stop_requested(conn) or now() >= deadline:
            return
        if await asyncio.to_thread(_host_reachable):
            log(
                f"host reachable {deadline - now()}s early; "
                "ending backoff and retrying now"
            )
            return


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
    except asyncio.CancelledError as e:
        # Swallowed so a torn-down session cannot fail an otherwise good check,
        # but no longer silent: orphaned SDK scopes are a real leak and were
        # invisible because this was a bare `except BaseException: pass`.
        log(f"session teardown cancelled ({_failure_detail(e)})")
    except Exception as e:
        log(f"session teardown failed ({_failure_detail(e)})")


async def fresh_token() -> str:
    """Access token, refreshing silently.

    Raises NetworkError when the token endpoint is merely unreachable and
    AuthError only when the credential is actually bad. Wrapping every
    exception as AuthError made a dead proxy or a DNS blip look like an
    expired login, which prompted a pointless re-sign-in and skipped backoff.
    """
    try:
        tok = auth.refresh()
    except NetworkError as e:
        raise NetworkError(f"token refresh: {e}") from e
    except AuthError:
        raise
    except OSError as e:
        # Belt and braces. _token_request already wraps transport faults, but
        # URLError, TimeoutError and ConnectionError are all OSError, and none
        # of them may ever be read as an auth problem regardless of which
        # layer let them through.
        raise NetworkError(f"token refresh: {e}") from e
    except Exception as e:
        # Anything else here is a malformed token file or similar: that is a
        # credential problem, and re-auth is the right answer.
        raise AuthError(f"refresh failed: {e}") from e
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
    """What does this machine's resolver say about the MCP host?

    Returns an EMPTY STRING when DNS is healthy. Those addresses are the
    provider's real ones, and logging them on every failure reads like a DNS
    fault when the fault was a connect timeout or a dropped stream. Only a
    genuine anomaly is worth a line: a sinkhole answer (0.0.0.0 / ::) means
    DNS-level blocking, and a resolution failure means the resolver is down.
    """
    try:
        addrs = sorted({i[4][0] for i in socket.getaddrinfo("mcp.upwork.com", 443)})
    except Exception as e:
        return f"DNS resolution failed: {e}"
    if any(a in ("0.0.0.0", "::") for a in addrs):
        return f"DNS sinkholed {addrs} for mcp.upwork.com"
    return ""


def _exception_chain(e: BaseException, depth: int = 5) -> str:
    """Flatten an exception and its causes into one readable line.

    The MCP SDK raises CancelledError with the real fault (ConnectTimeout,
    ReadError, ...) buried in __cause__/__context__. Reporting only the
    CancelledError threw the diagnosis away and left stderr.log as the sole
    source of truth.
    """
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = e
    while cur is not None and len(parts) < depth and id(cur) not in seen:
        seen.add(id(cur))
        msg = str(cur).strip()
        parts.append(f"{type(cur).__name__}: {msg}" if msg else type(cur).__name__)
        cur = cur.__cause__ or cur.__context__
    return " <- ".join(parts)


def _failure_detail(e: BaseException) -> str:
    """Human-readable cause for a failed check, plus DNS only if it misbehaved."""
    parts = [_exception_chain(e)]
    note = _dns_note()
    if note:
        parts.append(note)
    return "; ".join(p for p in parts if p)


# anyio reports every transport fault the same way: "Cancelled via cancel
# scope <hex> <- WouldBlock". That names no cause, it just buries the real
# diagnosis under a scope id. Dropped unless something specific is chained
# below it.
_SDK_NOISE = ("WouldBlock", "Cancelled via cancel scope", "CancelledError")


def _meaningful_chain(e: BaseException) -> str:
    """The exception chain with anyio's contentless markers removed."""
    parts = [
        p
        for p in _exception_chain(e).split(" <- ")
        if not any(marker in p for marker in _SDK_NOISE)
    ]
    return " <- ".join(parts)


async def _cancel_detail(e: BaseException) -> str:
    """Cause for a cancelled check.

    The MCP SDK runs its requests in an anyio task group. When one of those
    fails, the group cancels the awaiting task and keeps the original
    exception to itself, so all that escapes is a bare CancelledError with no
    message and an unrelated context. Rather than report that, re-test the
    host directly and state what is actually wrong.
    """
    parts = []
    chain = _meaningful_chain(e)
    if chain:
        parts.append(chain)
    note = _dns_note()
    if note:
        parts.append(note)
    if not await asyncio.to_thread(_host_reachable):
        parts.append("host unreachable: TCP connect to mcp.upwork.com:443 failed")
    elif not chain:
        parts.append(
            "host reachable, so not the network "
            "(SDK task-group teardown; see stderr.log)"
        )
    return "; ".join(parts) or "cancelled with no detail"


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
        return ("auth", _failure_detail(e))
    except NetworkError as e:
        return ("network", _failure_detail(e))
    except asyncio.CancelledError as e:
        return ("cancelled", await _cancel_detail(e))
    except Exception as e:
        return ("error", _failure_detail(e))


async def run_loop() -> None:
    """1s ticker. Drains the bucket; runs a check when it hits zero.

    Each check runs in a child task so leaked SDK cancel scopes die with
    the child. The ticker itself never awaits SDK coroutines directly and
    therefore can never be scope-cancelled; the only CancelledError it can
    see is a genuine external shutdown, which re-raises.
    """
    conn = db()
    # A requested stop from a previous run must not prevent this one from starting.
    if stop_requested(conn):
        clear_stop(conn)
    log("loop running")
    while True:
        if stop_requested(conn):
            log("stop requested; loop exiting cleanly")
            clear_stop(conn)
            return
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
            # A backoff recorded before a restart still applies; without this
            # the crash-loop restarts burned through the penalty immediately.
            try:
                pending = int(meta_get(conn, "backoff_until", "0") or 0)
            except ValueError:
                pending = 0
            if pending > now():
                left = pending - now()
                log(f"resuming backoff, {left}s remaining")
                await sleep_backoff(conn, left)
                if stop_requested(conn):
                    continue
            # time to check; isolated in a child task (see _run_check)
            try:
                checker = asyncio.create_task(_run_check(conn))
                status, detail = await checker
            except (KeyboardInterrupt, SystemExit):
                raise
            except asyncio.CancelledError:
                raise  # not from a check (those are strings now): real shutdown
            if stop_requested(conn):
                continue
            if status == "ok":
                nb = draw_bucket()
                meta_set(conn, "bucket", nb)
                log(f"bucket redrawn: {nb}s")
                await asyncio.sleep(1)
                continue
            # Every failure class shares one escalating, probe-aware backoff.
            # Auth used to take a flat 60s and never escalate, so a persistent
            # auth problem retried forever at a fixed rate.
            if status == "auth":
                log(f"auth error: {detail}")
                reauth_nag(conn)
            else:
                log(f"check {status}: {detail}")
            wait = _backoff(conn, f"{status}: {detail}")
            log(f"backoff level {backoff_level(conn)}, retrying in {wait}s")
            await sleep_backoff(conn, wait)
            continue
        else:
            meta_set(conn, "bucket", b - 1)
        await asyncio.sleep(1)


def backoff_level(conn) -> int:
    try:
        return int(meta_get(conn, "backoff_level", "0"))
    except ValueError:
        return 0