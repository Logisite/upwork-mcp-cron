"""Background runner: start / stop the watcher, and own the cron schedule.

`start` installs the crontab entry (so it survives reboots) and launches the
loop. `stop` kills the loop AND removes the crontab entry, so a stop is a real
stop: cron will not resurrect the watcher. Any other crontab lines the user
already had are preserved.
"""

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .notify import log

DATA = Path(__file__).resolve().parent.parent / "data"
PID = DATA / "watcher.pid"
LOOP_LOG = DATA / "loop.log"
STDERR_LOG = DATA / "stderr.log"
WATCH_LOG = DATA / "watch.log"

ROOT = Path(__file__).resolve().parent.parent
RUN_SH = ROOT / "run.sh"
CRON_LINE = f"* * * * * {RUN_SH}"


def running_pid() -> int | None:
    """Live loop pid, or None only when we can prove nothing is running.

    Only ProcessLookupError proves the process is gone. A ValueError (empty
    or corrupt pid file) or PermissionError says nothing about the process,
    so treating those as "dead" caused start() to spawn a duplicate loop
    while the real one was still running.
    """
    if not PID.exists():
        return None
    try:
        pid = int(PID.read_text().strip())
    except ValueError:
        log("pid file corrupt; leaving it in place")
        return None
    try:
        os.kill(pid, 0)
        return pid
    except ProcessLookupError:
        PID.unlink(missing_ok=True)
        return None
    except PermissionError:
        # Alive, we just cannot signal it.
        return pid


def _read_crontab() -> list[str]:
    try:
        out = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=10
        )
        if out.returncode != 0:
            return []
        return out.stdout.splitlines()
    except Exception as e:
        log(f"crontab read failed: {e}")
        return []


def cron_installed() -> bool:
    return any(str(RUN_SH) in line for line in _read_crontab())


def install_cron() -> bool:
    """Add our crontab line, preserving everything else. Idempotent."""
    lines = _read_crontab()
    if any(str(RUN_SH) in line for line in lines):
        return True
    lines.append(CRON_LINE)
    try:
        proc = subprocess.run(
            ["crontab", "-"], input="\n".join(lines) + "\n",
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            log("crontab entry installed")
            return True
        log(f"crontab install failed: {proc.stderr.strip()}")
    except Exception as e:
        log(f"crontab install failed: {e}")
    return False


def remove_cron() -> bool:
    """Remove only our crontab line, preserving everything else. Idempotent."""
    lines = _read_crontab()
    kept = [ln for ln in lines if str(RUN_SH) not in ln]
    if len(kept) == len(lines):
        return True
    try:
        if kept:
            proc = subprocess.run(
                ["crontab", "-"], input="\n".join(kept) + "\n",
                capture_output=True, text=True, timeout=10,
            )
        else:
            proc = subprocess.run(
                ["crontab", "-r"], capture_output=True, text=True, timeout=10
            )
        if proc.returncode == 0:
            log("crontab entry removed")
            return True
        log(f"crontab remove failed: {proc.stderr.strip()}")
    except Exception as e:
        log(f"crontab remove failed: {e}")
    return False


def _spawn_loop(termux: bool = False) -> int | None:
    DATA.mkdir(parents=True, exist_ok=True)
    # Truncate the crash log on every spawn. A stale traceback sitting in
    # loop.log is worse than no log at all: it reads as live and sends you
    # debugging a bug that was fixed days ago. Fresh file, one header line.
    with open(LOOP_LOG, "w") as fh:
        fh.write(f"# loop.log - opened {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
        fh.write("# this file is TRUNCATED on every start. Anything here is from the CURRENT run.\n")
        fh.write("# live event log: watch.log   |   uncaught crashes land in this file\n")
    out = open(LOOP_LOG, "a")
    err = open(STDERR_LOG, "w")
    err.write(f"# stderr.log - opened {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
    env = dict(os.environ)
    if termux:
        # The detached loop needs to know it is on a phone; -t is per
        # invocation on the CLI side, so it is passed explicitly here.
        env["UPWORK_CRON_TERMUX"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "upwork_cron", "loop"],
        cwd=str(ROOT),
        stdout=out,
        stderr=err,
        start_new_session=True,
        env=env,
    )
    PID.write_text(str(proc.pid))
    # Stamp the pid before anyone else can check, so a concurrent start()
    # sees this instance instead of concluding the loop is dead.
    log(f"loop started pid {proc.pid}{' (termux)' if termux else ''}")
    return proc.pid


def start(termux: bool = False) -> int:
    from . import dispatch

    dispatch.set_mode(termux)
    alive = running_pid()
    if alive:
        was = dispatch.recorded_mode()
        print(f"loop already running (pid {alive}, {was} mode)")
        if was != ("termux" if termux else "desktop"):
            print(f"  it is in {was} mode; stop then start again to change it")
        return 0

    pid = _spawn_loop(termux)

    # Re-check immediately: if another start() raced us, both loops forked
    # before either pid file was visible. One wins the flock and survives;
    # the other exits immediately. Kill the loser rather than leaving two
    # pollers on the same account.
    time.sleep(1.5)
    winner = running_pid()
    if winner and winner != pid:
        with contextlib.suppress(Exception):
            os.kill(pid, signal.SIGTERM)
        PID.write_text(str(winner))
        print(f"another start won the race; kept pid {winner}, killed {pid}")
        pid = winner

    print(f"loop started (pid {pid}) — {'termux' if termux else 'desktop'} mode")
    if termux:
        print("cron: skipped (dies with the Termux app; the 1s ticker drives it)")
        print("to run detached on a phone: leave Termux in the background,")
        print("do not force stop it, and consider: termux-wake-lock")
    else:
        installed = install_cron()
        print(
            f"schedule: {'installed' if installed else 'FAILED to install'}"
            f" ({CRON_LINE})"
        )
    print(f"live log: {WATCH_LOG}")
    print(f"crash log: {LOOP_LOG}")
    return 0


def ensure() -> int:
    """Start the detached loop only if it is not already running.

    Used by the cron safety net. Deliberately does NOT touch the crontab,
    so a `stop` (which removes the schedule) stays stopped.
    """
    if running_pid():
        return 0
    from . import dispatch

    _spawn_loop(dispatch.recorded_mode() == "termux")
    return 0


def stop() -> int:
    removed = remove_cron()
    pid = running_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            time.sleep(0.1)
            if running_pid() is None:
                break
        else:
            os.kill(pid, signal.SIGKILL)
        PID.unlink(missing_ok=True)
        log("loop stopped")
    print("loop stopped" if pid else "loop was not running")
    print(
        f"schedule: {'removed' if removed else 'FAILED to remove'}"
        " — cron will no longer restart it"
    )
    return 0


def _handle_sigterm(*_a) -> None:
    from . import watcher

    # Mark shutdown first so the run_loop CancelledError handler re-raises
    # instead of swallowing a genuine stop as a transient transport failure.
    watcher.request_shutdown()
    sys.exit(0)


def loop() -> int:
    """Long-running cadence loop. One check every CHECK_EVERY_SEC."""
    from . import watcher

    signal.signal(signal.SIGTERM, _handle_sigterm)
    lock = watcher.acquire_lock()
    if lock is None:
        log("loop exiting: lock already held")
        return 0
    try:
        asyncio.run(watcher.run_loop())
    finally:
        lock.close()
    return 0