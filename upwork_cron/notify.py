"""Notification delivery and logging, cross-platform."""

from __future__ import annotations

import json
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

# A detached helper owns the click lifecycle. It shows a sticky notification,
# waits for the "default" action, opens the URL, then re-shows the SAME
# notification (--replace-id) so it stays on screen until the user dismisses
# it. Running it as a child (not a thread) means a click still works after
# `upcron notify-test` or any other one-shot command has exited.
_ACTION_PY = r'''
import json, shutil, subprocess, sys
p = json.loads(sys.argv[1])
cmd = list(p["cmd"])
url = p["url"]

def has_persistence():
    if not shutil.which("gdbus"):
        return False
    try:
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
             "--object-path", "/org/freedesktop/Notifications", "--method",
             "org.freedesktop.Notifications.GetCapabilities"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return "persistence" in out
    except Exception:
        return False

# With the `resident` hint, a server that supports persistence keeps the
# notification on screen after the action. Only re-show it ourselves on older
# servers that ignore that hint.
resident = has_persistence()
replace = ""
while True:
    run = list(cmd)
    if replace:
        run += ["-r", str(replace)]
    run += ["-p"]
    try:
        out = subprocess.run(run, capture_output=True, text=True).stdout.splitlines()
    except Exception:
        break
    lines = [x.strip() for x in out if x.strip()]
    if not lines:
        break
    ident = next((x for x in lines if x.isdigit()), lines[0])
    action = next((x for x in lines if not x.isdigit()), "")
    if action not in ("default", "0"):
        break
    opener = None
    if shutil.which("xdg-open"):
        opener = ["xdg-open", url]
    elif shutil.which("gio"):
        opener = ["gio", "open", url]
    if opener:
        try:
            subprocess.Popen(opener, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except Exception:
            pass
    if resident:
        break
    replace = ident
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
    sticky: bool = True,
    url: str = "",
) -> None:
    """Fire a sticky desktop notification via notify-send (Linux/Plasma).

    A notification stays on screen until the user dismisses it. When a URL is
    given and notify-send supports actions, clicking it (or its "Open job"
    action) opens the URL in the browser and immediately re-shows the
    notification, so a click never removes it.
    """
    if sys.platform == "win32":
        # Windows dispatch is handled by notify_windows; this is a fallback.
        log(f"notify-send unavailable on Windows: {summary[:80]}")
        return
    cmd = [NOTIFY_SEND, "-a", "Upwork Watcher", "-u", urgency]
    # Sticky: no timer removes it. `resident` additionally tells a compliant
    # server to keep it on screen after an action is invoked (click).
    cmd += ["-t", "0", "-h", "boolean:resident:true"]
    clickable = bool(url) and _supports_actions()
    if clickable:
        cmd += ["-A", "default=Open job"]
    cmd.append(summary)
    if body:
        cmd.append(body)
    try:
        if clickable:
            payload = json.dumps({"cmd": cmd, "url": url})
            proc = subprocess.Popen(
                [sys.executable, "-c", _ACTION_PY, payload],
                env=session_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
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