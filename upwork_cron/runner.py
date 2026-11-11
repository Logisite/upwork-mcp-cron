"""Background runner: start / stop the watcher, and own its schedule.

`start` installs the schedule and launches the loop. `stop` removes the schedule
and stops the loop, so a stop is a real stop. `ensure` only starts the loop if
it is not already running and never touches the schedule.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import schedule
from .notify import log
from .paths import LOOP_LOG, PID, ROOT, STDERR_LOG, WATCH_LOG, ensure_data_dir

# Windows process flags
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_STILL_ACTIVE = 259


def _read_pid() -> int | None:
    if not PID.exists():
        return None
    try:
        return int(PID.read_text(encoding="utf-8").strip())
    except Exception:
        log("pid file corrupt; leaving it in place")
        return None


def _windows_pid_alive(pid: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied -> probably alive
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            if code.value != _STILL_ACTIVE:
                return False
            # Best-effort guard: ensure it looks like our Python process.
            try:
                buf = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(buf))
                if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    name = Path(buf.value).name.lower()
                    if name in {"python.exe", "pythonw.exe", "python", "pythonw"}:
                        return True
                    # Some launchers; be conservative and treat as alive.
                    return True
            except Exception:
                return True
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _posix_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_pid_alive(pid)
    return _posix_pid_alive(pid)


def running_pid() -> int | None:
    """Live loop pid, or None when we can prove nothing is running."""
    pid = _read_pid()
    if pid is None:
        return None
    if pid_alive(pid):
        return pid
    try:
        PID.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def cron_installed() -> bool:
    return schedule.installed()


def schedule_installed() -> bool:
    return schedule.installed()


def install_cron() -> bool:
    return schedule.install()


def remove_cron() -> bool:
    return schedule.remove()


def _python_executable(detached: bool = False) -> str:
    exe = Path(sys.executable)
    if detached and sys.platform == "win32":
        cand = exe.with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


def _spawn_loop(termux: bool = False) -> int | None:
    ensure_data_dir()
    with open(LOOP_LOG, "w", encoding="utf-8") as fh:
        fh.write(f"# loop.log - opened {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
        fh.write("# this file is TRUNCATED on every start. Anything here is from the CURRENT run.\n")
        fh.write("# live event log: watch.log   |   uncaught crashes land in this file\n")
    out = open(LOOP_LOG, "a", encoding="utf-8")
    err = open(STDERR_LOG, "w", encoding="utf-8")
    err.write(f"# stderr.log - opened {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
    env = dict(os.environ)
    if termux:
        env["UPWORK_CRON_TERMUX"] = "1"
    cmd = [_python_executable(detached=True), "-m", "upwork_cron", "loop"]
    popen_kw = {
        "cwd": str(ROOT),
        "stdout": out,
        "stderr": err,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    else:
        popen_kw["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kw)
    PID.write_text(str(proc.pid), encoding="utf-8")
    log(f"loop started pid {proc.pid}{' (termux)' if termux else ''}")
    return proc.pid


def start(termux: bool = False) -> int:
    from . import dispatch

    dispatch.set_mode(termux)
    alive = running_pid()
    if alive:
        was = dispatch.recorded_mode()
        print(f"loop already running (pid {alive}, {was} mode)")
        if was != ("termux" if termux else ("windows" if sys.platform == "win32" else "desktop")):
            print(f"  it is in {was} mode; stop then start again to change it")
        return 0

    pid = _spawn_loop(termux)

    # Re-check immediately: if another start raced us, one wins the lock.
    time.sleep(1.5)
    winner = running_pid()
    if winner and winner != pid and pid_alive(pid):
        _terminate_pid(pid)
        PID.write_text(str(winner), encoding="utf-8")
        print(f"another start won the race; kept pid {winner}, killed {pid}")
        pid = winner

    mode = "termux" if termux else ("windows" if sys.platform == "win32" else "desktop")
    print(f"loop started (pid {pid}) — {mode} mode")
    if termux:
        print("schedule: skipped (dies with the Termux app; the 1s ticker drives it)")
        print("to run detached on a phone: leave Termux in the background,")
        print("do not force stop it, and consider: termux-wake-lock")
    else:
        installed = schedule.install()
        print(f"schedule: {'installed' if installed else 'FAILED to install'} ({schedule.describe()})")
    print(f"live log: {WATCH_LOG}")
    print(f"crash log: {LOOP_LOG}")
    return 0


def ensure() -> int:
    """Start the detached loop only if it is not already running.

    Used by the schedule safety net. Deliberately does NOT touch the schedule.
    """
    if running_pid():
        return 0
    from . import dispatch

    mode = dispatch.recorded_mode()
    _spawn_loop(mode == "termux")
    return 0


def _terminate_pid(pid: int) -> None:
    if not pid_alive(pid):
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        return
    for _ in range(30):
        time.sleep(0.1)
        if not pid_alive(pid):
            return
    with contextlib.suppress(Exception):
        os.kill(pid, signal.SIGKILL)


def _request_graceful_stop() -> None:
    try:
        from . import watcher

        conn = watcher.db()
        watcher.request_stop(conn)
        conn.close()
    except Exception as e:
        log(f"could not set stop flag: {e}")


def stop() -> int:
    removed = schedule.remove()
    _request_graceful_stop()
    pid = running_pid()
    was_running = pid is not None
    if pid:
        # Give the ticker a chance to exit cleanly (it checks every ~1s).
        for _ in range(80):
            time.sleep(0.1)
            if running_pid() is None:
                pid = None
                break
        if pid and running_pid():
            _terminate_pid(pid)
            for _ in range(20):
                time.sleep(0.1)
                if running_pid() is None:
                    pid = None
                    break
        try:
            PID.unlink(missing_ok=True)
        except OSError:
            pass
        log("loop stopped")
    if not was_running:
        try:
            PID.unlink(missing_ok=True)
        except OSError:
            pass
    print("loop stopped" if was_running else "loop was not running")
    print(
        f"schedule: {'removed' if removed else 'FAILED to remove'}"
        " — it will no longer restart automatically"
    )
    return 0


def _handle_sigterm(*_a) -> None:
    from . import watcher

    watcher.request_shutdown()
    try:
        conn = watcher.db()
        watcher.request_stop(conn)
        conn.close()
    except Exception:
        pass
    raise SystemExit(0)


def loop() -> int:
    """Long-running cadence loop."""
    from . import watcher

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sigterm)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _handle_sigterm)
    lock = watcher.acquire_lock()
    if lock is None:
        log("loop exiting: lock already held")
        return 0
    try:
        import asyncio

        asyncio.run(watcher.run_loop())
    finally:
        try:
            lock.close()
        except Exception:
            pass
    return 0
