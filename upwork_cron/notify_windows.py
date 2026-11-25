"""Windows toast notifications via windows-toasts.

Toasts use the Reminder scenario (stays on screen until acted on) with an
"Open job" button, and the toast is re-shown on activation so a click opens the
job URL without removing the notification. Only the user's own dismissal
removes it. Falls back to the basic toaster where an interactable AUMID is not
available.
"""

from __future__ import annotations

import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .notify import log

_INIT_LOCK = threading.Lock()
_TOASTER = None
_INTERACTABLE = False
_INIT_ERROR: str | None = None
_ACTIVE: dict[str, object] = {}
_ACTIVE_LOCK = threading.Lock()


def _init():
    global _TOASTER, _INTERACTABLE, _INIT_ERROR
    with _INIT_LOCK:
        if _TOASTER is not None or _INIT_ERROR is not None:
            return _TOASTER
        if sys.platform != "win32":
            _INIT_ERROR = "not windows"
            return None
        try:
            from windows_toasts import InteractableWindowsToaster

            # Default AUMID (Command Prompt) is registered on Windows, so no
            # extra registry setup or admin rights are needed for buttons.
            _TOASTER = InteractableWindowsToaster("Upwork Cron")
            _INTERACTABLE = True
            return _TOASTER
        except Exception as e:
            log(f"interactable toast init failed, falling back: {e}")
        try:
            from windows_toasts import WindowsToaster

            _TOASTER = WindowsToaster("Upwork Cron")
            _INTERACTABLE = False
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
        "toaster": "interactable" if _INTERACTABLE else ("basic" if t else "none"),
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
    sticky: bool = True,
    urgency: str = "normal",
) -> bool:
    """Show a persistent Windows toast. Returns True if shown."""
    toaster = _init()
    if toaster is None:
        return False
    try:
        from windows_toasts import (
            Toast,
            ToastButton,
            ToastDuration,
            ToastScenario,
        )

        toast = Toast(
            tag=f"upcron-{uuid.uuid4().hex}",
            duration=ToastDuration.Long,
            scenario=ToastScenario.Reminder if _INTERACTABLE else ToastScenario.Default,
            expiration_time=datetime.now(timezone.utc) + timedelta(days=30),
            expires_on_reboot=False,
        )
        lines = [ln for ln in (body or "").splitlines() if ln.strip()]
        toast.text_fields = [summary] + lines[:3] if lines else [summary]
        if url:
            toast.launch_action = url
            if _INTERACTABLE:
                # Reminder keeps the toast on screen until the user acts, and
                # Windows requires a button for that scenario.
                toast.AddAction(ToastButton(content="Open job", launch=url))

        def on_activated(_args):
            # Windows removes a toast after activation. Showing the same tag
            # again brings it straight back, so a click never removes it.
            try:
                toaster.show_toast(toast)
            except Exception as e:
                log(f"windows toast re-show failed: {e}")

        def on_dismissed(_args):
            with _ACTIVE_LOCK:
                _ACTIVE.pop(toast.tag, None)

        toast.on_activated = on_activated
        toast.on_dismissed = on_dismissed
        with _ACTIVE_LOCK:
            _ACTIVE[toast.tag] = toast
        toaster.show_toast(toast)
        return True
    except Exception as e:
        log(f"windows toast failed: {e}")
        return False
