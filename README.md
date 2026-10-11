# upwork-cron

Watches the Upwork MCP server for new AI job posts and fires a sticky desktop
notification for each one. Built on the official [`mcp`](https://pypi.org/project/mcp/)
Python SDK.

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

Every command has `--help`.

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
2. **cron** fires `run.sh` every minute as a self-heal, calling
   `upwork-cron ensure` which starts the loop only if it is not already running.

`start` and `stop` own the crontab line, and only that line. Other crontab
entries are preserved.

| Command | Loop | Cron |
|---|---|---|
| `start` | starts | **installs** |
| `stop` | stops | **removes** |
| `ensure` | starts only if dead | untouched |

`stop` is a **real stop**: cron is gone and nothing restarts the watcher.
Verified by stopping and waiting past multiple cron boundaries.

No systemd unit, because `systemctl --user` is degraded on this machine.

---

## Auth

`upwork-cron auth` runs PKCE (S256) against Upwork's public client document and
stores tokens at `data/token.json` (0600). Refresh is silent and automatic.

If a token is rejected you get a **critical** "sign-in needed" notification
rather than a generic error, at most once every 6 hours. Check failures back off
exponentially from 30s to 30 min; a successful check resets it immediately.

## Notifications

Desktop env is read from the live Plasma session (`/proc/<plasmashell>/environ`)
so it survives reboots and display renumbering. Sent sticky (`-t 0`).

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
  cli.py          argparse CLI
  mcp_client.py   official-SDK MCP client, held session, paged fetch
  watcher.py      bucket cadence, paging + ordering guard, storage, notify
  auth.py         PKCE login + silent refresh
  notify.py       desktop notifications + log rotation
  runner.py       start/stop, cron install/remove, detached loop
run.sh            cron self-heal entrypoint
data/             token.json (0600), state.db, watch.log  (gitignored)
```

## Queries

```python
QUERIES = [
    ("smart_search", {"limit": 10}),                          # profile-matched
    ("search", {"title": "AI", "sort": "recency", "limit": 10}),
]
```

`smart_search` is the higher-recall net and is what caught a post within a
minute of publishing during testing. Edit `QUERIES` in `watcher.py` to change
what is watched, then `stop` and `start`.

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