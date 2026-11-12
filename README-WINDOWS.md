# upwork-cron on Windows

Per-user Windows support for `upwork-cron`: polls the Upwork MCP for new jobs,
shows Windows toast notifications, and keeps itself running with a per-user
Task Scheduler job.

No administrator rights are required. Your Upwork token and data stay in your
user profile.

## Requirements

- Windows 10 or 11 (x64 recommended)
- Python **3.10+** installed (the Python Launcher `py` is preferred)
  - Download: https://www.python.org/downloads/windows/
  - The installer does **not** install Python automatically; it stops with a
    clear message if Python is missing.
- An Upwork account you can sign into in a browser
- Windows notifications enabled for the Python/toast app context and Focus
  Assist not blocking toasts

## Install

From the folder containing this project (zip extract or repo clone), run:

```bat
install.cmd
```

What it does:

1. Checks Windows and finds Python 3.10+ (`py -3` / `python`).
2. Copies the app to `%LOCALAPPDATA%\upwork-cron\app`.
3. Creates a virtual environment at `%LOCALAPPDATA%\upwork-cron\venv`.
4. Installs dependencies (including `windows-toasts`).
5. Runs `upwork-cron auth` unless a token already exists.
6. Runs `upwork-cron start` to install the `\UpworkCron` scheduled task and
   start the detached watcher loop (`pythonw.exe`, no console window).
7. Sends a test toast.

Re-running `install.cmd` upgrades the installed app code while preserving
`%LOCALAPPDATA%\upwork-cron\data` (token, database, logs).

## Installed layout

```
%LOCALAPPDATA%\upwork-cron\
  app\               installed source (editable install target)
  venv\              Python virtual environment
  data\
    token.json       Upwork OAuth tokens
    state.db         jobs, schedule/bucket state, stats
    watch.log        live event log
    loop.log         current-run loop log (truncated on start)
    stderr.log       uncaught stderr for the detached loop
  upwork-cron.cmd    convenience command wrapper
```

## Commands

Use the installed wrapper:

```bat
%LOCALAPPDATA%\upwork-cron\upwork-cron.cmd status
%LOCALAPPDATA%\upwork-cron\upwork-cron.cmd doctor
%LOCALAPPDATA%\upwork-cron\upwork-cron.cmd notify-test
%LOCALAPPDATA%\upwork-cron\upwork-cron.cmd logs -n 50
%LOCALAPPDATA%\upwork-cron\upwork-cron.cmd logs -n 50 --loop
%LOCALAPPDATA%\upwork-cron\upwork-cron.cmd probe
%LOCALAPPDATA%\upwork-cron\upwork-cron.cmd auth
%LOCALAPPDATA%\upwork-cron\upwork-cron.cmd start
%LOCALAPPDATA%\upwork-cron\upwork-cron.cmd stop
```

Key commands:

- `status` — loop, schedule, last check/tick, health, bucket, token, paths.
- `doctor` — full diagnostics (Python, paths, token expiry, task state, last
  task result, notification backend). Add `--send-test` to also send a toast.
- `notify-test` — sends a sample toast.
- `start` — starts the detached loop and installs the Task Scheduler task.
- `stop` — **real stop**: removes the task and stops the loop so it will not
  restart automatically.
- `auth` — (re)sign in if the token is missing/invalid.

## Scheduling and behavior

- The loop runs detached via `pythonw.exe` and uses a 1-second ticker with the
  same bucket cadence as the core watcher.
- Task Scheduler task: `\UpworkCron`, runs every minute, action:
  `pythonw.exe -m upwork_cron ensure`.
- The task is configured to start when available (helps after sleep/boot),
  ignore overlapping runs, and not stop on battery.
- `ensure` is a self-heal: it starts the loop only if it is not already
  running and never changes the schedule.

## Notifications

- Toasts are sent with `windows-toasts`.
- Clicking a job toast opens the job URL in your browser.
- If toasts do not appear, run `doctor --send-test` and check:
  - **Settings → System → Notifications** (notifications enabled)
  - **Focus Assist / Do not disturb**
  - Whether the test toast appears while you are actively using the session

## Sleep, reboot, and logoff

- If the machine sleeps, checks pause; the task is set to start when available
  and the watcher resumes.
- After reboot/logon, the per-user task can run again in your session; `ensure`
  restarts the loop if needed.
- Toasts require an interactive user session.

## What is watched

On Windows the watcher uses **`smart_search` only** — Upwork's profile-matched
feed, with no hardcoded keyword/title search. This means results depend on your
Upwork profile and job preferences.

If you need to change this behavior, edit `QUERIES` in
`%LOCALAPPDATA%\upwork-cron\app\upwork_cron\watcher.py`, then run:

```bat
%LOCALAPPDATA%\upwork-cron\upwork-cron.cmd stop
%LOCALAPPDATA%\upwork-cron\upwork-cron.cmd start
```

(An install/upgrade will overwrite app code; keep custom edits in mind.)

## Uninstall

```bat
uninstall.cmd
```

By default this stops/removes the task and app but preserves a backup of your
data. To remove everything, including token/database/logs:

```bat
uninstall.cmd -PurgeData
```

(or run `powershell -ExecutionPolicy Bypass -File uninstall.ps1 -PurgeData`)

## Troubleshooting

| Symptom | What to try |
|---|---|
| No toasts | `notify-test`, `doctor --send-test`, check Notifications/Focus Assist |
| "sign-in needed" or auth errors | `upwork-cron.cmd auth` |
| Loop not running | `status`, `doctor`, `logs -n 50 --loop`, then `start` |
| Task not running after reboot | `schtasks /Query /TN UpworkCron /V /FO LIST`, then `doctor` |
| Want to inspect failures | `doctor`, `logs -n 80`, `logs -n 80 --stderr` |
