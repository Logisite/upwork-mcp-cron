"""upwork-cron: watch Upwork for new AI jobs and notify on the desktop.

Typical use:

  upwork-cron status          is it alive, when did it last check
  upwork-cron probe           run one live search and print what came back
  upwork-cron since 60        stored jobs from the last 60 minutes, local SQL
  upwork-cron metrics         observed notify latency distribution
  upwork-cron logs -n 50      what it has been doing
  upwork-cron auth            (re)sign in through the browser

Requires a token: run `upwork-cron auth` once.
"""

import argparse
import asyncio
import json
import os
import sys
import time

from . import auth, dispatch, runner, watcher
from .mcp_client import AuthError, probe
from .notify import LOG


def _apply_termux_flag(a) -> None:
    """-t means Termux for this invocation only.

    Not sticky: a later plain `start` is desktop again, which is what
    "regular is regular" means.
    """
    if getattr(a, "t", False):
        os.environ[dispatch.TERMUX_ENV] = "1"


def cmd_auth(_a):
    auth.login()
    return 0


def cmd_start(a):
    return runner.start(termux=dispatch.termux_mode())


def cmd_stop(_a):
    return runner.stop()


def cmd_ensure(_a):
    return runner.ensure()


def cmd_tsetup(a):
    """Diagnose Termux notification delivery."""
    from . import notify_termux

    info = notify_termux.diagnose()
    print("Termux notification diagnostics\n")
    for k, v in info.items():
        print(f"  {k:<18} {v}")
    t = info["transport"]
    print()
    if t == "http":
        print("transport: HTTP 127.0.0.1:8787 (preferred, works inside proot)")
    elif t == "cli":
        print("transport: termux-notification CLI (fallback)")
        print("  note: this path breaks on Android 15/OneUI 7 (proot #516)")
    else:
        print("transport: NONE - notifications will be skipped\n")
        print("  fix, in Termux (not inside proot):")
        print("    pkg install termux-api")
        print("    termux-api-start &")
        print("  the Termux:API app must be installed separately and")
        print("  notification permission granted in Android settings")
    if a.send:
        title = "upwork-cron test"
        body = "Termux notifications are working."
        ok = notify_termux.notify(title, body, url="https://www.upwork.com/nx/search/jobs/")
        print(f"\ntest notification sent: {'delivered' if ok else 'FAILED'}")
    return 0


def _ago(v):
    try:
        n = int(v)
        if n <= 0:
            return "never"
        d = int(time.time()) - n
        if d < 60:
            return f"{d}s ago"
        if d < 3600:
            return f"{d // 60}m ago"
        return f"{d // 3600}h ago"
    except ValueError:
        return "?"


def cmd_status(_a):
    conn = watcher.db()
    pid = runner.running_pid()
    stored = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    bucket = watcher.meta_get(conn, "bucket", "")
    print(f"background loop : {'running, pid ' + str(pid) if pid else 'stopped'}")
    print(f"mode            : {dispatch.recorded_mode()}")
    print(f"cron schedule   : {'installed' if runner.cron_installed() else 'not installed'}")
    print(f"last check      : {_ago(watcher.meta_get(conn, 'last_check', '0'))}")
    print(f"last tick       : {_ago(watcher.meta_get(conn, 'last_tick', '0'))}")
    if bucket == "":
        print("bucket          : not drawn yet")
    else:
        try:
            print(f"bucket          : {int(bucket)}s remaining")
        except ValueError:
            print("bucket          : ?")
    print(f"jobs stored     : {stored} (retention {watcher.RETENTION_DAYS}d)")
    print(f"token           : {'stored' if auth.TOKEN_PATH.exists() else 'missing'}")
    print(f"log             : {LOG}")
    return 0


def cmd_probe(_a):
    try:
        r = probe()
    except AuthError as e:
        print("probe failed: auth:", e)
        return 2
    except Exception as e:
        print(f"probe failed: {type(e).__name__}: {e}")
        return 1
    jobs = r.get("jobs", [])
    print(f"probe OK - {len(jobs)} jobs")
    for j in jobs[:10]:
        print(f"  {j.get('published_date')}  {j.get('title','')[:64]}")
    return 0


def cmd_since(a):
    """Local SQL over stored jobs. No MCP calls."""
    conn = watcher.db()
    cutoff = int(time.time()) - a.minutes * 60
    rows = conn.execute(
        """SELECT pub_ts, tier, pay, country, title, url, notified_at
           FROM jobs WHERE pub_ts >= ? ORDER BY pub_ts DESC LIMIT ?""",
        (cutoff, a.n),
    ).fetchall()
    if not rows:
        print(f"no stored jobs in the last {a.minutes} minutes")
        return 0
    print(f"{len(rows)} job(s) from the last {a.minutes} minutes:\n")
    for pub_ts, tier, pay, country, title, url, notified in rows:
        age = watcher.age_label(pub_ts)
        flag = "notified" if notified else "stored only"
        print(f"{age}  {tier}  {country}  [{flag}]")
        print(f"   {title[:90]}")
        print(f"   {pay}  {url}")
    return 0


def cmd_metrics(_a):
    """Observed latency: time from posting to notification."""
    conn = watcher.db()
    rows = conn.execute(
        """SELECT notified_at - pub_ts FROM jobs
           WHERE notified_at IS NOT NULL ORDER BY notified_at DESC LIMIT 500"""
    ).fetchall()
    if not rows:
        print("no notifications recorded yet")
        return 0
    lats = sorted(r[0] for r in rows)
    n = len(lats)

    def pct(p):
        return lats[min(n - 1, int(n * p))]

    print(f"notify latency over {n} notification(s), seconds from posting to popup:\n")
    print(f"  min    {lats[0]:>6}s")
    print(f"  p50    {pct(0.50):>6}s")
    print(f"  p90    {pct(0.90):>6}s")
    print(f"  max    {lats[-1]:>6}s")
    under2 = sum(1 for x in lats if x <= 120)
    print(f"\n  within 2 min target: {under2}/{n} ({100*under2//n}%)")
    return 0


def cmd_exec(a):
    from .mcp_client import search as mcp_search

    try:
        params = json.loads(a.query) if a.query else {}
    except json.JSONDecodeError:
        print("exec failed: query must be valid JSON", file=sys.stderr)
        return 1
    try:
        rows = mcp_search(a.kind, params)
    except AuthError as e:
        print("exec failed: auth:", e, file=sys.stderr)
        return 2
    except Exception as e:
        print(f"exec failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(json.dumps(rows, indent=2))
    return 0


def cmd_check(a):
    """Force one check now, with its own short-lived session."""
    lock = watcher.acquire_lock()
    if lock is None:
        print("another run holds the lock; nothing to do")
        return 0
    try:
        tok = auth.refresh()
        if not tok:
            print("no token; run: upwork-cron auth", file=sys.stderr)
            return 2
        conn = watcher.db()

        async def _go():
            from .mcp_client import session

            async with session(tok["access_token"]) as s:
                await watcher.check_once(conn, s)

        asyncio.run(_go())
        print("check done")
        print("last check:", watcher.meta_get(conn, "last_check", "0"))
        return 0
    except AuthError as e:
        print("auth failed:", e, file=sys.stderr)
        return 2
    except Exception as e:
        print(f"check failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        lock.close()


def cmd_stats(a):
    """Uptime, request rate, and coverage, from recorded per-check stats."""
    conn = watcher.db()
    since = int(time.time()) - a.hours * 3600
    rows = conn.execute(
        "SELECT ts,requests,pages,new_jobs,notified,secs FROM checks"
        " WHERE ts >= ? ORDER BY ts",
        (since,),
    ).fetchall()
    if not rows:
        print(f"no checks recorded in the last {a.hours}h")
        return 0

    first_ts = int(conn.execute("SELECT MIN(ts) FROM checks").fetchone()[0] or 0)
    uptime_h = (int(time.time()) - first_ts) / 3600 if first_ts else 0
    reqs = [r[1] for r in rows]
    pages = [r[2] for r in rows]
    newj = [r[3] for r in rows]
    notif = [r[4] for r in rows]
    secs = [r[5] for r in rows]

    # Peak: requests in any single clock hour within the window.
    by_hour: dict = {}
    for ts, rq, *_ in rows:
        h = ts - ts % 3600
        by_hour[h] = by_hour.get(h, 0) + rq
    peak_hour = max(by_hour.values()) if by_hour else 0

    total_req = sum(reqs)
    # With few checks the elapsed span is tiny; clamp to an hour so the
    # per-hour rate is a sane rate, not a division by ~0.
    span_h = max((rows[-1][0] - rows[0][0]) / 3600, 1.0)

    def avg(xs):
        return sum(xs) / len(xs) if xs else 0

    print(f"window              : last {a.hours}h ({len(rows)} checks)")
    print(f"loop uptime         : {uptime_h:.1f}h")
    print(f"check interval      : {span_h*3600/len(rows):.0f}s avg")
    print()
    print(f"requests total      : {total_req}")
    print(f"  per check         : {avg(reqs):.1f} avg, {max(reqs)} max")
    print(f"  per hour          : {total_req/span_h:.0f} avg, {peak_hour} peak")
    print(f"  as rqps           : {total_req/span_h/3600:.3f} avg (ceiling 10)")
    print()
    print(f"pages per check     : {avg(pages):.1f} avg, {max(pages)} max")
    print(f"jobs found          : {sum(newj)} (unique across queries)")
    print(f"notifications       : {sum(notif)}")
    print(f"check duration      : {avg(secs):.1f}s avg, {max(secs):.1f}s max")
    stored = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    print(f"jobs stored         : {stored} (retention {watcher.RETENTION_DAYS}d)")
    return 0


def cmd_logs(a):
    if not LOG.exists():
        print("no log yet")
        return 0
    for line in LOG.read_text().splitlines()[-a.n :]:
        print(line)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="upwork-cron",
        description="Watch Upwork for new AI jobs and notify on the desktop.",
        epilog=(
            "examples:\n"
            "  upwork-cron status\n"
            "  upwork-cron metrics\n"
            "  upwork-cron since 60\n"
            "  upwork-cron probe\n"
            "  upwork-cron exec search '{\"title\": \"AI\", \"sort\": \"recency\"}'\n"
            "\n"
            "cron keeps the loop alive; status and metrics observe it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "-t",
        action="store_true",
        help="Termux mode for this run: Android notifications instead of desktop",
    )
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")

    def add(name, fn, help_text, **kw):
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.add_argument(
            "-t",
            action="store_true",
            help="Termux mode for this run",
        )
        for arg, opts in kw.items():
            p.add_argument(*opts[0], **opts[1])
        p.set_defaults(fn=fn)
        return p

    add("auth", cmd_auth, "Sign in through the browser (needed once)")
    add(
        "tsetup",
        cmd_tsetup,
        "Diagnose Termux notification delivery",
        send=(
            ("--send",),
            dict(action="store_true", help="also send a test notification"),
        ),
    )
    add("start", cmd_start, "Start the watcher and install the cron schedule")
    add("stop", cmd_stop, "Stop the watcher AND remove the cron schedule")
    add("ensure", cmd_ensure, "Internal: start the loop only if not running")
    add("status", cmd_status, "Loop state, bucket, jobs stored, token")
    add("probe", cmd_probe, "One live MCP search, bypassing stored data")
    add("check", cmd_check, "Force one check right now")
    add("metrics", cmd_metrics, "Observed notify latency (posting to popup)")
    add(
        "stats",
        cmd_stats,
        "Uptime, requests/hour, pages, coverage",
        hours=(
            ("-H", "--hours"),
            dict(type=int, default=24, help="window in hours (default 24)"),
        ),
    )
    add("loop", lambda _a: runner.loop(), "Internal: the 1s bucket ticker")

    p = sub.add_parser(
        "since",
        help="Stored jobs from the last N minutes (local SQL, no MCP calls)",
        description="Query stored jobs from the last N minutes. Local SQL only.",
    )
    p.add_argument("minutes", type=int, help="how many minutes back")
    p.add_argument("-n", type=int, default=20, help="max rows (default 20)")
    p.set_defaults(fn=cmd_since)

    p = sub.add_parser(
        "exec",
        help="Run an arbitrary find_jobs query and print raw JSON",
        description="Run an arbitrary find_jobs query.\nkind is 'search' or 'smart_search'.",
    )
    p.add_argument("kind", nargs="?", default="search", help="search|smart_search")
    p.add_argument("query", nargs="?", default="{}", help="JSON params")
    p.set_defaults(fn=cmd_exec)

    p = sub.add_parser("logs", help="Print the last N log lines")
    p.add_argument("-n", type=int, default=30, help="how many lines (default 30)")
    p.set_defaults(fn=cmd_logs)

    args = ap.parse_args(argv)
    _apply_termux_flag(args)
    return args.fn(args)