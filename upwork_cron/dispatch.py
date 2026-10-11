"""Dispatch a notification to whichever backend is active.

Desktop (default): Plasma via notify-send.
Termux (-t):       Android system notification via the Termux:API.
"""

import os
from pathlib import Path

from .notify import log, notify as notify_desktop

TERMUX_ENV = "UPWORK_CRON_TERMUX"
_DATA = Path(__file__).resolve().parent.parent / "data"
_MODE_FILE = _DATA / "mode"


def termux_mode() -> bool:
    """Per-invocation mode: -t on this run means Termux for this run only."""
    return os.environ.get(TERMUX_ENV) == "1"


def set_mode(termux: bool) -> None:
    """Record the last start mode so `status` can report it."""
    _DATA.mkdir(parents=True, exist_ok=True)
    _MODE_FILE.write_text("termux" if termux else "desktop")


def recorded_mode() -> str:
    try:
        return _MODE_FILE.read_text().strip() or "unknown"
    except Exception:
        return "unknown"


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
    notify_desktop(summary, body, urgency=urgency, sticky=sticky)