"""Windows toast notifications via windows-toasts."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from .notify import log

_INIT_LOCK = threading.Lock()
_TOASTER = None
_INIT_ERROR: str | None = None


def _init():
    global _TOASTER, _INIT_ERROR
    with _INIT_LOCK:
        if _TOASTER is not None or _INIT_ERROR is not None:
            return _TOASTER
        if sys.platform != "win32":
            _INIT_ERROR = "not windows"
            return None
        try:
            from windows_toasts import WindowsToaster

            _TOASTER = WindowsToaster("Upwork Cron")
            return _TOASTER
        except Exception as e:
            _INIT_ERROR = str(e)
            log(f"windows toast init failed: {e}")
            return None


def available() -> bool:
    return _init() is not None


def diagnose() -> dict:
    t = _init()
    info = {
        "platform": sys.platform,
        "backend": "windows-toasts",
        "available": t is not None,
    }
    if _INIT_ERROR:
        info["error"] = _INIT_ERROR
    try:
        import windows_toasts

        info["version"] = getattr(windows_toasts, "__version__", "?")
    except Exception as e:
        info["import_error"] = str(e)
    return info


def notify(
    summary: str,
    body: str = "",
    url: str = "",
    sticky: bool = False,
    urgency: str = "normal",
) -> bool:
    """Show a Windows toast. Returns True if shown."""
    toaster = _init()
    if toaster is None:
        return False
    try:
        from windows_toasts import Toast, ToastDuration, ToastScenario

        toast = Toast()
        # Split body into readable lines for text fields.
        lines = [ln for ln in (body or "").splitlines() if ln.strip()]
        if lines:
            toast.text_fields = [summary] + lines[:3]
        else:
            toast.text_fields = [summary]
        if url:
            toast.launch_action = url
        toast.duration = ToastDuration.Long
        if sticky or urgency == "critical":
            # Reminder keeps the toast more persistent in the action center.
            toast.scenario = ToastScenario.Reminder
        toaster.show_toast(toast)
        return True
    except Exception as e:
        log(f"windows toast failed: {e}")
        return False
