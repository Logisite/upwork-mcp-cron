"""Notification delivery and logging, cross-platform."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .paths import LOG, LOOP_LOG, ensure_data_dir

NOTIFY_SEND = shutil.which("notify-send") or "/usr/bin/notify-send"
LOG_MAX = 1_000_000      # rotate a file once it passes this
LOG_KEEP = 5             # how many rotated generations to retain

_ENV_CACHE: dict = {}
_ACTION_SUPPORT = None

# Run notify-send in a detached helper: it waits for the action because -A
# implies --wait, then opens the job URL. Doing this in a child rather than a
# thread means a click still works after `upcron notify-test` (or any other
# one-shot command) has already exited. The subshell is backgrounded so the
# short-lived parent can be reaped immediately and the long wait is reparented
# to init instead of lingering as a zombie under the watcher.
_ACTION_SCRIPT = '''
(
  out=$("$@")
  if [ "$out" = "default" ]; then
    if command -v xdg-open >/dev/null 2>&1; then
      exec xdg-open "$UPCRON_URL" >/dev/null 2>&1
    elif command -v gio >/dev/null 2>&1; then
      exec gio open "$UPCRON_URL" >/dev/null 2>&1
    fi
  fi
  exit 0
) >/dev/null 2>&1 &
'''


def _supports_actions() -> bool:
    global _ACTION_SUPPORT
    if _ACTION_SUPPORT is None:
        try:
            out = subprocess.run(
                [NOTIFY_SEND, "--help"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            text = (out.stdout or "") + (out.stderr or "")
            _ACTION_SUPPORT = "--action" in text
        except Exception:
            _ACTION_SUPPORT = False
    return _ACTION_SUPPORT


def _rotate(path: Path) -> None:
    """Generational rotation: watch.log -> .1 -> .2 ... keeping LOG_KEEP."""
    if not path.exists() or path.stat().st_size <= LOG_MAX:
        return
    ensure_data_dir()
    oldest = path.with_suffix(path.suffix + f".{LOG_KEEP}")
    oldest.unlink(missing_ok=True)
    for i in range(LOG_KEEP - 1, 0, -1):
        src = path.with_suffix(path.suffix + f".{i}")
        if src.exists():
            src.replace(path.with_suffix(path.suffix + f".{i + 1}"))
    path.replace(path.with_suffix(path.suffix + ".1"))


def log(msg: str) -> None:
    ensure_data_dir()
    _rotate(LOG)
    _rotate(LOOP_LOG)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{ts} {msg}\n")


def session_env() -> dict:
    """Environment carrying the live desktop session (Linux/KDE helper).

    On Windows this returns the current environment; Windows notifications do
    not go through notify-send.
    """
    if _ENV_CACHE:
        return _ENV_CACHE
    env = dict(os.environ)
    if sys.platform == "win32":
        _ENV_CACHE.update(env)
        return env
    try:
        pids = subprocess.run(
            ["pgrep", "-x", "plasmashell"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.split()
        if pids:
            with open(f"/proc/{pids[0]}/environ", "rb") as f:
                raw = f.read().split(b"\0")
            live = {}
            for item in raw:
                if b"=" in item:
                    k, v = item.split(b"=", 1)
                    live[k.decode()] = v.decode()
            for k in (
                "DBUS_SESSION_BUS_ADDRESS",
                "DISPLAY",
                "WAYLAND_DISPLAY",
                "XDG_CURRENT_DESKTOP",
                "XDG_RUNTIME_DIR",
            ):
                if live.get(k):
                    env[k] = live[k]
    except Exception as e:
        log(f"session_env: {e}")

    uid = os.getuid() if hasattr(os, "getuid") else 0
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    env.setdefault("DISPLAY", ":0")
    env.setdefault("XDG_CURRENT_DESKTOP", "KDE")
    _ENV_CACHE.update(env)
    return env


def notify(
    summary: str,
    body: str = "",
    urgency: str = "normal",
    sticky: bool = False,
    url: str = "",
) -> None:
    """Fire a desktop notification via notify-send (Linux/Plasma).

    When a URL is given and the installed notify-send supports actions, the
    notification is clickable: choosing "Open job" (or clicking the body)
    opens it in the browser. The waiting is done by a detached helper so it
    survives the caller exiting.
    """
    if sys.platform == "win32":
        # Windows dispatch is handled by notify_windows; this is a fallback.
        log(f"notify-send unavailable on Windows: {summary[:80]}")
        return
    cmd = [NOTIFY_SEND, "-a", "Upwork Watcher", "-u", urgency]
    if sticky:
        cmd += ["-t", "0"]
    else:
        cmd += ["-t", "15000"]
    clickable = bool(url) and _supports_actions()
    if clickable:
        cmd += ["-A", "default=Open job"]
    cmd.append(summary)
    if body:
        cmd.append(body)
    try:
        if clickable:
            env = dict(session_env())
            env["UPCRON_URL"] = url
            proc = subprocess.Popen(
                ["/bin/sh", "-c", _ACTION_SCRIPT, "sh", *cmd],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        else:
            subprocess.run(cmd, env=session_env(), timeout=10, check=False)
    except Exception as e:
        log(f"notify failed: {e}")