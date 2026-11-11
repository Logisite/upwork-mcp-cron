"""OAuth sign-in for the Upwork MCP.

Standard authorization-code + PKCE (S256) against Upwork's public client
document. Spins up a localhost callback listener, opens the browser, and
stores access + refresh tokens.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

from .paths import TOKEN_PATH, ensure_data_dir

CLIENT_ID = "https://opencode.ai/oauth/opencode/client.json"
AUTHZ_URL = "https://www.upwork.com/ab/account-security/oauth2/authorize"
TOKEN_URL = "https://www.upwork.com/api/v3/oauth2/token"
RESOURCE = "https://mcp.upwork.com/mcp"

PKCE = "/.well-known/pkce"


class AuthError(Exception):
    """The credential itself is bad: missing, revoked, or rejected."""


class NetworkError(Exception):
    """The token endpoint could not be reached, or answered unusably.

    Distinct from AuthError on purpose. Both arrive from the same call site,
    but only one of them means "the user must sign in again". Collapsing them
    turns every proxy wobble or DNS failure into a bogus re-auth prompt.
    """


class _Callback(http.server.BaseHTTPRequestHandler):
    holder: dict = {}

    def do_GET(self):  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("code"):
            _Callback.holder["code"] = q["code"][0]
        if q.get("error"):
            _Callback.holder["error"] = q.get("error", ["unknown"])[0]
        body = b"Upwork approved. You can close this tab."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _token_request(form: dict) -> dict:
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(form).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "upwork-cron/0.1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Must precede the URLError branch: HTTPError subclasses URLError.
        body = e.read().decode(errors="replace")[:400]
        # 400/401/403 mean the credential is the problem. 5xx and 429 mean the
        # server or the path to it is, and no amount of re-signing helps.
        if e.code in (400, 401, 403):
            raise AuthError(f"token endpoint {e.code}: {body}") from e
        raise NetworkError(f"token endpoint {e.code}: {body}") from e
    except json.JSONDecodeError as e:
        # Usually a captive portal or proxy error page served with a 200.
        raise NetworkError(f"token endpoint returned non-JSON: {e}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise NetworkError(f"token endpoint unreachable: {e}") from e


def _save(data: dict) -> None:
    ensure_data_dir()
    TOKEN_PATH.write_text(
        json.dumps(
            {
                "access_token": data["access_token"],
                "refresh_token": data["refresh_token"],
                "expires_at": int(time.time())
                + int(data.get("expires_in", 3600))
                - 30,
            }
        ),
        encoding="utf-8",
    )
    if sys.platform != "win32":
        try:
            os.chmod(TOKEN_PATH, 0o600)
        except OSError:
            pass


def login() -> dict:
    """Run the browser sign-in flow and persist tokens."""
    verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(16)

    server = None
    port = None
    try:
        server = http.server.HTTPServer(("127.0.0.1", 0), _Callback)
        port = server.server_address[1]
        server.timeout = 1
    except OSError:
        server = None

    redirect = (
        f"http://127.0.0.1:{port}/callback" if port else "http://127.0.0.1/callback"
    )
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": RESOURCE,
    }
    url = AUTHZ_URL + "?" + urllib.parse.urlencode(params)

    print("Open this URL to approve:")
    print(url)
    try:
        webbrowser.open(url)
    except Exception:
        pass

    code = None
    if server is not None:
        print("Waiting for browser callback (5 min)...")
        _Callback.holder.clear()
        deadline = time.time() + 300
        while time.time() < deadline and "code" not in _Callback.holder and "error" not in _Callback.holder:
            # handle_request respects server.timeout (1s), which is portable.
            # Avoid select() on a server socket: Windows select only accepts sockets.
            try:
                server.handle_request()
            except Exception:
                pass
        server.server_close()
        code = _Callback.holder.get("code")
        if _Callback.holder.get("error") and not code:
            raise AuthError(f"browser callback error: {_Callback.holder.get('error')}")

    if not code:
        print("No automatic callback. Paste the code from the browser address bar.")
        code = input("code> ").strip()
    if not code:
        raise RuntimeError("no authorization code captured")

    data = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        }
    )
    _save(data)
    print("Signed in. Tokens stored at", TOKEN_PATH)
    return data


def refresh() -> Optional[dict]:
    """Refresh the stored access token if it is expired. Returns new token."""
    if not TOKEN_PATH.exists():
        return None
    tok = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    if tok.get("expires_at", 0) > time.time() + 60:
        return tok
    data = _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": CLIENT_ID,
        }
    )
    _save(data)
    return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))