# upwork-cron

Watches Upwork for new jobs matching your profile (`smart_search`) and any
keywords you add. Built on the official
[`mcp`](https://pypi.org/project/mcp/) Python SDK.

- **Linux / Plasma / Termux:** clickable desktop notifications via
  `notify-send`, or Android notifications via Termux (see below).
- **Windows 10/11 (per-user):** Windows toast notifications with Task Scheduler
  keepalive. See **[README-WINDOWS.md](README-WINDOWS.md)** for details.

## Linux installation

Requirements: desktop Linux (X11 or Wayland), **Python 3.10+**, `cron`, and
`notify-send` (libnotify). No root access is needed.

One-line install:

```sh
bash <(curl -fsSL https://raw.githubusercontent.com/Logisite/upwork-mcp-cron/main/install.sh)
```

The `bash <(...)` form is a single command and leaves stdin free for the
browser sign-in fallback. It needs bash or zsh (not plain `sh`).

Or, from a clone or extracted folder:

```sh
./install.sh
```

The installer is per-user. It installs to `~/.local/share/upwork-cron`
(`app/`, `venv/`, `data/`), creates `~/.local/bin/upcron` and
`~/.local/bin/upwork-cron`, adds `~/.local/bin` to your PATH through
`~/.profile` if needed, runs browser sign-in, starts the watcher, installs the
user crontab self-heal line, and sends a test notification.

After installation (open a new terminal if PATH was just changed):

```sh
upcron status
upcron doctor
upcron logs -n 50
upcron stop
upcron start
```

Options: `--skip-auth`, `--skip-start`, `--ref <branch/tag/commit>`.

After installation, upgrade in place with `upcron update` (see Updates below).
Re-running the installer also works: app code and dependencies are refreshed
while `~/.local/share/upwork-cron/data` (token, database, logs, keywords) is
preserved.

Updates:

```sh
upcron update --check   # report whether a newer version is available
upcron update           # fetch, install, and restart if the watcher was running
upcron update --ref main   # explicit branch/tag/commit (default: main)
```

`update` keeps `keywords.json`, the token, the database and logs. It stops the
watcher for the swap and restarts it afterwards if it was running or scheduled.
Ctrl+C cancels cleanly and restores the previous app.

Uninstall:

```sh
bash <(curl -fsSL https://raw.githubusercontent.com/Logisite/upwork-mcp-cron/main/uninstall.sh)
```

Or `./uninstall.sh` from the clone. Data is copied to
`~/.local/share/upwork-cron-data-backup`; add `--purge-data` to remove
everything. Ctrl+C cancels the installer or uninstaller cleanly; re-run it
when ready.

## Windows installation

Requirements: Windows 10/11, **Python 3.10+** installed (`py -3`), no admin rights.

One-line install (PowerShell):

```powershell
iwr https://raw.githubusercontent.com/Logisite/upwork-mcp-cron/main/install.ps1 -OutFile "$env:TEMP\upcron-install.ps1"; powershell -NoProfile -ExecutionPolicy Bypass -File "$env:TEMP\upcron-install.ps1"
```

One-line install (CMD):

```bat
curl.exe -fL https://raw.githubusercontent.com/Logisite/upwork-mcp-cron/main/install.cmd -o "%TEMP%\upcron-install.cmd" && "%TEMP%\upcron-install.cmd"
```

Or, from an extracted/cloned project folder:

```bat
install.cmd
```

The installer is per-user: it installs to `%LOCALAPPDATA%\upwork-cron`, creates
a venv, installs dependencies, runs `upwork-cron auth`, installs the per-user
`\UpworkCron` Task Scheduler job, starts the watcher, and sends a test toast. It
also adds `%LOCALAPPDATA%\upwork-cron\bin` to your user PATH, so after opening a
**new** terminal you can manage everything with the short `upcron` command:

```bat
upcron status
upcron doctor
upcron logs -n 50
upcron stop
upcron start
```

Re-run the installer to upgrade (app code only; data/token/db/logs preserved).
Uninstall with `uninstall.cmd` (add `-PurgeData` to remove token/db/logs). Full
paths, one-liners, and troubleshooting: **[README-WINDOWS.md](README-WINDOWS.md)**.

---

## Commands

After installation, use `upcron`:

```sh
upcron status          # loop state, bucket remaining, jobs stored
upcron since 60        # stored jobs from the last 60 min (local SQL)
upcron metrics         # observed notify latency vs the 2 min target
upcron probe           # one live MCP smart_search, bypassing storage
upcron check           # force a check now
upcron logs -n 50      # what it has been doing
upcron auth            # (re)sign in via browser
upcron start|stop      # background loop + cron schedule
upcron keyword ...     # manage extra keyword searches (see Queries)
upcron update          # update in place (see Updates)
upcron portfolio ...   # build a portfolio from job listings (see Portfolio)
```

From a source checkout, prefix with `uv run` (for example
`uv run upwork-cron status`).

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

- **Linux (Plasma):** sent via `notify-send`; click the notification body (on
  Plasma) or its **Open job** action to open the job in your browser.
- **Windows:** sent as Windows toasts (`windows-toasts`); clicking opens the job.
  Use `notify-test` / `doctor --send-test` and check notification settings/Focus
  Assist if nothing appears.
- **Termux:** Android notifications via Termux:API (`-t` / `tsetup`); tapping
  opens the job URL.

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
  keywords.py      per-user keyword list + smart_search query builder
  updater.py       in-place self-update from GitHub
  portfolio.py     job-listing portfolio: fetch + generate
run.sh             cron self-heal entrypoint (Linux)
install.sh         per-user Linux installer
uninstall.sh       per-user Linux uninstaller
install.cmd/.ps1   per-user Windows installer
uninstall.cmd/.ps1 per-user Windows uninstaller
README-WINDOWS.md  Windows-specific instructions
data/              local project data (gitignored); installed copies use
                   ~/.local/share/upwork-cron/data (Linux) or
                   %LOCALAPPDATA%\upwork-cron\data (Windows)
```

## Queries

`smart_search` (Upwork's profile-matched feed) **always runs first**, on every
platform. Nothing is hardcoded beyond that: every other search comes from the
keywords you add, stored per-user in `keywords.json` inside the data directory.
Changes take effect on the next check — no restart needed.

```sh
upcron keyword add "machine learning"        # match the job title (default)
upcron keyword add "LLM automation" --kind query   # match the whole posting
upcron keyword add "Python developer" --limit 5
upcron keyword list                          # smart_search + your keywords
upcron keyword remove "machine learning"     # or use the number from `list`
upcron keyword clear                         # remove all keywords
upcron keyword test "machine learning"       # live search, print results
```

`--kind title` matches whole, stemmed words in the job title (1–3 words work
best; terms are ANDed). `--kind query` matches across title and description
with semantic expansion. `--limit` is 1–10 per page. Up to 20 keywords are
kept; duplicates are ignored.

`upcron keyword test` with no argument runs all saved keyword searches live
(or just `smart_search` when none are saved), so you can verify what a keyword
returns before relying on it. The watcher de-duplicates jobs across searches
by id, so overlapping keywords do not produce duplicate notifications.

## Portfolio

Build a portfolio from job listings. Items are stored per-user in
`portfolio.json` inside the data directory and survive upgrades.

```sh
upcron portfolio add "My project" --url https://example.com --pay "$40/hr"
upcron portfolio list
upcron portfolio fetch               # import latest listings from the local DB
upcron portfolio fetch --live        # fetch live from Upwork MCP
upcron portfolio build --live --format html -o ~/portfolio.html
upcron portfolio generate            # re-render from stored items
upcron portfolio remove 1
upcron portfolio clear
```

`fetch` imports the latest listings the watcher has already stored (no network
calls). Add `--live` to query Upwork directly using `smart_search` plus any
keywords you configured; `--smart-only` uses just `smart_search`. `--limit`
caps results per query and `--since` limits the local fetch to jobs seen in the
last N minutes. Duplicates are skipped by job id, URL, or title.

`build` fetches and then generates in one step. `generate` defaults to
`<data>/portfolio.md`; use `--format html` for a standalone page.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| No notifications | `upcron status`: is the loop `running`? Is `token: stored`? Is `bucket` draining? Also run `upcron notify-test`; on Linux install `notify-send`/libnotify if missing. |
| "sign-in needed" | `upcron auth` |
| Bucket frozen | loop died; `upcron start` to force |
| `ordering guard tripped` in logs | Upwork returned unordered pages; paging stopped early, worth a look |
| Want it fully off | `upcron stop` |
| `upcron: command not found` | Open a new terminal (PATH changed), or run `~/.local/bin/upcron` |
| Installer says Python 3.10+ was not found | Run `python3 --version`; install Python 3.10+ and re-run the installer |
| Installer says `crontab` was not found | Install cron (Debian/Ubuntu: `sudo apt install cron`), then `upcron stop && upcron start` |
| Change search | `upcron keyword add/list/remove/clear`; smart_search is always on |

## Exit codes

`0` ok · `1` check failed (backs off) · `2` auth invalid (re-run `auth`).