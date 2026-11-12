# upwork-cron

Watches the Upwork MCP server for new AI job posts and fires notifications for
each one. Built on the official [`mcp`](https://pypi.org/project/mcp/) Python SDK.

- **Linux / Plasma / Termux:** desktop notifications via `notify-send` or Termux
  (see below).
- **Windows 10/11 (per-user):** Windows toast notifications with Task Scheduler
  keepalive. See **[README-WINDOWS.md](README-WINDOWS.md)** for details.

## Windows installation

Requirements: Windows 10/11, **Python 3.10+** installed (`py -3`), no admin rights.

From the extracted/cloned project folder, run:

```bat
install.cmd
```

This copies the app to `%LOCALAPPDATA%\upwork-cron`, creates a venv, installs
dependencies, runs `upwork-cron auth`, installs the per-user `\UpworkCron` Task
Scheduler job, starts the watcher, and sends a test toast. Re-run to upgrade
(app code only; data/token/db/logs preserved).

Uninstall: `uninstall.cmd` (add `-PurgeData` to remove token/db/logs). Full
paths, commands, and troubleshooting: **[README-WINDOWS.md](README-WINDOWS.md)**.

---

## Commands

```sh
cd ~/projects/efficiency/upwork-cron

uv run upwork-cron status          # loop state, bucket remaining, jobs stored
uv run upwork-cron since 60        # stored jobs from the last 60 min (local SQL)
uv run upwork-cron metrics         # observed notify latency vs the 2 min target
uv run upwork-cron probe           # one live MCP search, bypassing storage
uv run upwork-cron check           # force a check now
uv run upwork-cron logs -n 50      # what it has been doing
uv run upwork-cron auth            # (re)sign in via browser
uv run upwork-cron start|stop      # background loop + cron schedule
```

Every command has `--help`. Useful cross-platform diagnostics: `notify-test`,
`doctor` (`--send-test`), and `logs --loop` / `logs --stderr`.

---

## Cadence: bucket, not fixed

After each real check, a bucket of **`90 + random(0, 150)` seconds** is drawn and
stored. A 1 second ticker drains it one second at a time. At zero the next check
runs and a fresh bucket is drawn.

| | |
|---|---|
| Range | 90 to 240s |
| Average | 165s |
| Worst case latency on a fresh job | up to 4 min |

Buckets persist in SQLite, so they survive the process boundary. The bucket
resumes from where it froze after a crash rather than firing immediately, which
is the deliberate trade for genuine variable timing over a fixed schedule.

## Paging: early stop, not fixed depth

Each query pages until it stops yielding **new** jobs, capped at 8 pages. So a
quiet feed costs 1 page, a busy one costs more and is captured fully.

| Scenario | Requests/check | Per hour | rqps |
|---|---|---|---|
| Quiet (typical) | 2 | ~44 | 0.012 |
| Moderate burst | 6 to 10 | ~130 to 220 | 0.04 to 0.06 |
| Burst at cap | 16 | ~350 | 0.10 |

Ceiling is 10 rqps, so there is a large margin.

The MCP session is **held open across checks**, which removes the 2-request
handshake that would otherwise be paid on every check.

### Ordering guard

Upwork's `recency` sort is **not reliable**. A real observed case returned a
3-day-old post in the middle of a "newest first" page, and a `smart_search`
result set spanned 61 hours. So:

- Results are sorted by `published_date` **client side**, never trusting server order.
- If a page's oldest job is not older than the previous page's oldest, paging
  stops and logs `ordering guard tripped`. This turns a silent correctness bug
  (skipping or looping forever) into a visible one.

Note: `from_date` / `to_date` were tested and **ignored by the server**, so all
time filtering is client side.

---

## Storage: everything, kept

Every job seen is written to SQLite, not just notified ones. That makes any time
question a local query rather than another round trip:

```sh
uv run upwork-cron since 30      # anything in the last 30 minutes
uv run upwork-cron since 240 -n 50
```

Retention is 30 days, pruned automatically.

### Notification thresholds

| Post age when first seen | Behaviour |
|---|---|
| under 5 min | notify immediately, latency recorded |
| 5 to 15 min | notify as a late catch-up, **latency not recorded** |
| over 15 min | stored only, never pops up |

This means a brief sleep or closed laptop still surfaces what you missed, but a
long outage or a first run does not blast a backlog of stale posts. Hour-old
posts are never notified.

---

## Metrics: the 2 minute benchmark

Every notification records how long after `published_date` it fired. Only fresh
posts count, so late catch-ups never poison the distribution.

```
$ uv run upwork-cron metrics
notify latency over N notification(s), seconds from posting to popup:
  min    ...
  p50    ...
  p90    ...
  within 2 min target: x/N (z%)
```

This is the only honest measure of the original goal. Upwork's index lag (the gap
between a post being published and becoming searchable) cannot be observed from
outside, so it shows up inside these numbers rather than as a separate figure.

---

## Schedule

Two layers:

1. **The loop** does the work: 1 second ticker, detached, survives closing the
   terminal.
2. **The schedule** is a 1-minute self-heal that runs `upwork-cron ensure`
   (starts the loop only if it is not already running).

- **Linux:** a user crontab line (`run.sh` / `ensure`). `start` and `stop` own
  only that line and preserve other crontab entries.
- **Windows:** a per-user Task Scheduler task `\UpworkCron` running
  `pythonw.exe -m upwork_cron ensure` every minute (installed/removed by
  `start`/`stop`).

| Command | Loop | Schedule |
|---|---|---|
| `start` | starts | **installs** |
| `stop` | stops | **removes** |
| `ensure` | starts only if dead | untouched |

`stop` is a **real stop**: the schedule is removed and nothing restarts the
watcher. On Linux this was historically cron-only because `systemctl --user`
was degraded on that machine.

---

## Auth

`upwork-cron auth` runs PKCE (S256) against Upwork's public client document and
stores tokens in the data directory (`token.json`, mode 0600 on Unix). On
Windows the default data directory is `%LOCALAPPDATA%\upwork-cron\data` (or
`UPWORK_CRON_DATA` if set). Refresh is silent and automatic.

If a token is rejected you get a **critical** "sign-in needed" notification
rather than a generic error, at most once every 6 hours. Check failures back off
exponentially from 30s to 30 min; a successful check resets it immediately.

## Notifications

- **Linux (Plasma):** sent via `notify-send`; the desktop env is read from the
  live Plasma session so it survives reboots and display renumbering.
- **Windows:** sent as Windows toasts (`windows-toasts`); clicking opens the job.
  Use `notify-test` / `doctor --send-test` and check notification settings/Focus
  Assist if nothing appears.
- **Termux:** Android notifications via Termux:API (`-t` / `tsetup`).

```
Summary:  12m ago · 20 to 50 proposals
Body:     AI Agent Developer - SaaS E-commerce Chatbot

          20 to 50 proposals · posted 12m ago
          $15.00-20.00/hr hourly · Australia · expert
          https://www.upwork.com/jobs/...
```

Age is computed from the server's `published_date` against live system time:
minutes under an hour, hours and minutes above.

---

## Files

```
upwork_cron/
  cli.py           argparse CLI (status/doctor/notify-test/...)
  mcp_client.py    official-SDK MCP client, held session, paged fetch
  watcher.py       bucket cadence, paging + ordering guard, storage, notify
  auth.py          PKCE login + silent refresh
  notify.py        logging + Linux desktop notifications
  notify_windows.py  Windows toast notifications
  dispatch.py      notification backend selection
  schedule.py      cron (Linux) / Task Scheduler (Windows) ownership
  runner.py        start/stop/ensure, detached loop, graceful stop
  paths.py         platform-aware data/log paths
  lockfile.py      cross-platform loop lock
run.sh             cron self-heal entrypoint (Linux)
install.cmd/.ps1   per-user Windows installer
uninstall.cmd/.ps1 per-user Windows uninstaller
README-WINDOWS.md  Windows-specific instructions
data/              Linux/local project data (gitignored); Windows uses %LOCALAPPDATA%\upwork-cron\data
```

## Queries

```python
# Linux / default
QUERIES = [
    ("smart_search", {"limit": 10}),                          # profile-matched
    ("search", {"title": "AI", "sort": "recency", "limit": 10}),
]

# Windows
QUERIES = [("smart_search", {"limit": 10})]                  # profile-matched only
```

`smart_search` is the higher-recall, profile-matched feed and is what caught a
post within a minute of publishing during testing. On Windows the watcher uses
`smart_search` only. On other platforms it also runs the keyword `search` query
above. Edit `QUERIES` in `watcher.py` to change what is watched, then `stop`
and `start`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| No notifications | `status`: is the loop `running`? Is `token: stored`? Is `bucket` draining? |
| "sign-in needed" | `uv run upwork-cron auth` |
| Bucket frozen | loop died; `start` to force |
| `ordering guard tripped` in logs | Upwork returned unordered pages; paging stopped early, worth a look |
| Want it fully off | `uv run upwork-cron stop` |
| Change search | edit `QUERIES` in `watcher.py`, then `stop` + `start` |

## Exit codes

`0` ok · `1` check failed (backs off) · `2` auth invalid (re-run `auth`).