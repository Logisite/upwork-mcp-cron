"""Desktop notification delivery, resilient to cron having no session bus."""

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

NOTIFY_SEND = "/usr/bin/notify-send"
DATA = Path(__file__).resolve().parent.parent / "data"
LOG = DATA / "watch.log"
LOOP_LOG = DATA / "loop.log"
LOG_MAX = 1_000_000      # rotate a file once it passes this
LOG_KEEP = 5             # how many rotated generations to retain

_ENV_CACHE: dict = {}


def _rotate(path: Path) -> None:
    """Generational rotation: watch.log -> .1 -> .2 ... keeping LOG_KEEP."""
    if not path.exists() or path.stat().st_size <= LOG_MAX:
        return
    DATA.mkdir(parents=True, exist_ok=True)
    oldest = path.with_suffix(path.suffix + f".{LOG_KEEP}")
    oldest.unlink(missing_ok=True)
    for i in range(LOG_KEEP - 1, 0, -1):
        src = path.with_suffix(path.suffix + f".{i}")
        if src.exists():
            src.replace(path.with_suffix(path.suffix + f".{i + 1}"))
    path.replace(path.with_suffix(path.suffix + ".1"))


def log(msg: str) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    _rotate(LOG)
    _rotate(LOOP_LOG)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(LOG, "a") as f:
        f.write(f"{ts} {msg}\n")


def session_env() -> dict:
    """Environment carrying the live Plasma session, found from plasmashell.

    Cron starts with almost nothing, and notify-send needs DISPLAY and a
    session bus to reach the desktop. Read them from the running compositor
    so this survives reboots and display renumbering.
    """
    if _ENV_CACHE:
        return _ENV_CACHE
    env = dict(os.environ)
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

    uid = os.getuid()
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    env.setdefault("DISPLAY", ":0")
    env.setdefault("XDG_CURRENT_DESKTOP", "KDE")
    _ENV_CACHE.update(env)
    return env


def notify(summary: str, body: str = "", urgency: str = "normal", sticky: bool = False) -> None:
    """Fire a desktop notification.

    sticky=True keeps it on screen until dismissed: Plasma treats a
    notification with no expire-time as resident, and critical urgency
    additionally survives the session's auto-hide timeout.
    """
    cmd = [NOTIFY_SEND, "-a", "Upwork Watcher", "-u", urgency]
    if sticky:
        # 0 = never auto-expire in Plasma.
        cmd += ["-t", "0"]
    else:
        cmd += ["-t", "15000"]
    cmd.append(summary)
    if body:
        cmd.append(body)
    try:
        subprocess.run(cmd, env=session_env(), timeout=10, check=False)
    except Exception as e:
        log(f"notify failed: {e}")