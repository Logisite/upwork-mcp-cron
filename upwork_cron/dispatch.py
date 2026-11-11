"""Dispatch a notification to whichever backend is active.

Desktop (default): Plasma via notify-send on Linux, Windows toasts on Windows.
Termux (-t):       Android system notification via the Termux:API.
"""

from __future__ import annotations

import os
import sys

from .notify import log, notify as notify_desktop
from .paths import MODE_FILE, ensure_data_dir

TERMUX_ENV = "UPWORK_CRON_TERMUX"


def termux_mode() -> bool:
    """Per-invocation mode: -t on this run means Termux for this run only."""
    return os.environ.get(TERMUX_ENV) == "1"


def set_mode(termux: bool) -> None:
    """Record the last start mode so `status` can report it."""
    ensure_data_dir()
    MODE_FILE.write_text("termux" if termux else ("windows" if sys.platform == "win32" else "desktop"))


def recorded_mode() -> str:
    try:
        return MODE_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except Exception:
        return "unknown"


def backend_name() -> str:
    if termux_mode() or recorded_mode() == "termux":
        return "termux"
    if sys.platform == "win32":
        return "windows"
    return "desktop"


def notify(
    summary: str,
    body: str = "",
    url: str = "",
    sticky: bool = False,
    urgency: str = "normal",
) -> None:
    """Send one notification to the active backend."""
    if termux_mode():
        from . import notify_termux

        ok = notify_termux.notify(summary, body, url=url)
        if not ok:
            log(f"termux notify not delivered: {summary[:60]}")
        return
    if sys.platform == "win32":
        from . import notify_windows

        ok = notify_windows.notify(summary, body, url=url, sticky=sticky, urgency=urgency)
        if not ok:
            log(f"windows notify not delivered: {summary[:60]}")
        return
    notify_desktop(summary, body, urgency=urgency, sticky=sticky)