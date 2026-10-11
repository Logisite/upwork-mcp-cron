"""Android notifications via the Termux:API.

Two transports, tried in order and cached:

1. HTTP  - POST to 127.0.0.1:8787/notification. This is the robust path from
   inside proot: it is plain loopback HTTP, so there is no Android-bionic
   binary being executed inside a glibc guest, no UID mismatch, no $PREFIX
   binding requirement, and no root problem.
2. CLI   - `termux-notification` on PATH. Works on Termux proper and in a
   default proot container (proot-distro binds $PREFIX and adds $PREFIX/bin to
   PATH). Known to break on Android 15 / OneUI 7 (proot-distro issue #516) and
   unavailable entirely under `--isolated`.

If neither works we log once and skip. We deliberately do NOT try to launch
`termux-api-start` from inside proot: that means executing a Termux binary
across the ABI boundary, which is exactly the fragile path the HTTP-first
design exists to avoid.

These are Android *system* notifications: they appear in the status bar and
shade, outside Termux, with Termux not foregrounded.
"""

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

API_HOST = "127.0.0.1"
API_PORT = 8787
NOTIFICATION_ID = "upwork-cron"
API_TOKEN_ENV = "UPWORK_CRON_API_TOKEN"

_cache: dict = {}


def api_url(path: str) -> str:
    return f"http://{API_HOST}:{API_PORT}/{path.lstrip('/')}"


def http_available(timeout: float = 2.0) -> bool:
    """True if something answers on the Termux:API HTTP port."""
    if "http_ok" in _cache:
        return _cache["http_ok"]
    ok = False
    try:
        # A notification endpoint is a POST, but any HTTP response proves the
        # server is alive, so probe with a cheap GET on the root.
        req = urllib.request.Request(api_url(""))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ok = r.status < 500
    except urllib.error.HTTPError:
        ok = True  # it spoke HTTP, it is up
    except Exception:
        ok = False
    _cache["http_ok"] = ok
    return ok


def cli_available() -> bool:
    if "cli_ok" in _cache:
        return _cache["cli_ok"]
    ok = shutil.which("termux-notification") is not None
    _cache["cli_ok"] = ok
    return ok


def _headers() -> dict:
    h = {"Content-Type": application_json_header()}
    tok = os.environ.get(API_TOKEN_ENV)
    if tok:
        h["X-Api-Auth"] = tok
    return h


def application_json_header() -> str:
    return "application/json"


def _send_http(payload: dict) -> bool:
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            api_url("notification"), data=data, headers=_headers(), method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status < 300
    except Exception as e:
        log_once(f"termux http notify failed: {e}")
        _cache["http_ok"] = False
        return False


def _send_cli(args: list) -> bool:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=10)
        if p.returncode != 0:
            log_once(f"termux-notification rc={p.returncode}: {p.stderr.strip()[:200]}")
        return p.returncode == 0
    except Exception as e:
        log_once(f"termux cli notify failed: {e}")
        return False


def log_once(msg: str) -> None:
    from .notify import log

    if msg in _cache.get("logged", ()):  # pragma: no cover
        return
    _cache.setdefault("logged", set()).add(msg)
    log(msg)


def transport() -> str:
    """Which transport is in use: http, cli, or none."""
    if http_available():
        return "http"
    if cli_available():
        return "cli"
    return "none"


def notify(
    summary: str,
    body: str = "",
    url: str = "",
    sound: bool = True,
    vibrate: str = "500,200",
) -> bool:
    """Send an Android notification. Returns True if a transport accepted it."""
    mode = transport()
    if mode == "none":
        log_once(
            "termux: no transport available (API server on 127.0.0.1:8787 not "
            "reachable and termux-notification not on PATH); notifications skipped"
        )
        return False

    payload = {
        "id": NOTIFICATION_ID,
        "title": summary,
        "content": body,
        "priority": "high",
        "ongoing": False,
        "sound": "true" if sound else "false",
    }
    if vibrate:
        payload["vibrate"] = vibrate
    # Tapping opens the job URL in the browser.
    if url:
        payload["action"] = (
            f"am start -a android.intent.action.VIEW -d '{url}'"
        )

    if mode == "http":
        if _send_http(payload):
            return True
        # fall through to the CLI if HTTP just failed

    args = ["termux-notification", "-i", NOTIFICATION_ID, "-t", summary, "--priority", "high"]
    if body:
        args += ["-c", body]
    if sound:
        args.append("--sound")
    if vibrate:
        args += ["--vibrate", vibrate]
    if url:
        args += ["--action", f"am start -a android.intent.action.VIEW -d '{url}'"]
    return _send_cli(args)


def diagnose() -> dict:
    """Everything needed to work out why Termux notifications are not landing."""
    import platform

    info = {
        "platform": platform.platform(),
        "in_termux_env": "com.termux" in os.environ.get("PREFIX", ""),
        "prefix": os.environ.get("PREFIX", "(unset)"),
        "api_http": f"{API_HOST}:{API_PORT}",
        "http_available": http_available(),
        "cli_on_path": cli_available(),
        "cli_path": shutil.which("termux-notification"),
        "api_token_set": bool(os.environ.get(API_TOKEN_ENV)),
        "transport": transport(),
    }
    # A GET on /battery-status is a harmless read-only probe of the API.
    try:
        with urllib.request.urlopen(api_url("battery-status"), timeout=2) as r:
            info["battery_probe"] = r.status
    except urllib.error.HTTPError as e:
        info["battery_probe"] = f"http {e.code}"
    except Exception as e:
        info["battery_probe"] = f"unreachable ({type(e).__name__})"
    return info