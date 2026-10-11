"""MCP client for the Upwork watcher, built on the official Python SDK.

Remote Streamable HTTP MCP server. A session is opened once and held across
many checks, which removes the per-check handshake (2 requests) that would
otherwise dominate the request budget.

The SDK's `streamable_http_client` yields a 2-tuple of (read, write) streams;
`ClientSession` drives initialize / tools/list / tools/call over it. A
pre-built httpx2 client is passed in so we control headers and timeouts.
"""

import contextlib
import json
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

MCP_URL = "https://mcp.upwork.com/mcp"
ORG_UID = "2028571967613634984"

DEFAULT_HEADERS = {
    "User-Agent": "upwork-cron/0.2.0 (python-mcp-sdk)",
    "Accept": "application/json, text/event-stream",
}

TOKEN_PATH = Path(__file__).resolve().parent.parent / "data" / "token.json"

# Live request counter. Incremented on every real MCP round trip so the stats
# command reports what actually went over the wire, not an inference from page
# counts. Reset by the watcher after each check.
_request_count = 0


def reset_requests() -> None:
    global _request_count
    _request_count = 0


def requests_made() -> int:
    return _request_count


def _require_token() -> str:
    if not TOKEN_PATH.exists():
        raise AuthError("no token stored; run 'upwork-cron auth'")
    tok = json.loads(TOKEN_PATH.read_text())
    if not tok.get("access_token"):
        raise AuthError("no access token stored; run 'upwork-cron auth'")
    return tok["access_token"]


class AuthError(Exception):
    """Token missing, expired past refresh, or rejected by the server."""


class Session:
    """An initialized MCP session held open across checks."""

    def __init__(self, token: str, timeout: float = 30.0):
        self.token = token
        self.timeout = timeout
        self.tool: Optional[str] = None
        self._stack = None

    async def __aenter__(self) -> "Session":
        self._stack = contextlib.AsyncExitStack()
        await self._stack.__aenter__()
        headers = dict(DEFAULT_HEADERS)
        headers["Authorization"] = f"Bearer {self.token}"
        http_client = await self._stack.enter_async_context(
            httpx2.AsyncClient(
                headers=headers,
                timeout=httpx2.Timeout(self.timeout),
                follow_redirects=True,
            )
        )
        streams = await self._stack.enter_async_context(
            streamable_http_client(
                MCP_URL, http_client=http_client, terminate_on_close=False
            )
        )
        self._session = await self._stack.enter_async_context(
            ClientSession(streams[0], streams[1])
        )
        await self._session.initialize()
        tools = await self._session.list_tools()
        global _request_count
        # initialize + notifications/initialized + tools/list
        _request_count += 3
        for t in tools.tools:
            if t.name.endswith("find_jobs"):
                self.tool = t.name
                break
        if not self.tool:
            raise RuntimeError("find_jobs tool not present on server")
        return self

    async def __aexit__(self, *exc):
        return await self._stack.__aexit__(*exc)

    async def call(self, action: str, params: dict) -> dict:
        global _request_count
        _request_count += 1
        result = await self._session.call_tool(
            self.tool, {"action": action, "org_uid": ORG_UID, "params": params}
        )
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"raw": text}
        return {}


@contextlib.asynccontextmanager
async def session(token: str, timeout: float = 30.0) -> AsyncIterator[Session]:
    async with Session(token, timeout) as s:
        yield s


async def fetch_page(sess: Session, kind: str, params: dict) -> list:
    payload = await sess.call(kind, params)
    return payload.get("jobs", []) or []


async def probe(timeout: float = 30.0) -> dict:
    """One live search, exactly as the watcher does it. Returns raw payload."""
    tok = _require_token()
    async with session(tok, timeout) as s:
        return await s.call("smart_search", {"limit": 5})


def search(kind: str, params: dict, timeout: float = 30.0) -> list:
    """Synchronous single query, for the CLI `exec` command."""
    tok = _require_token()

    async def _go():
        async with session(tok, timeout) as s:
            payload = await s.call(kind, params)
            return payload.get("jobs", []) or []

    return asyncio.run(_go())


def clean_url(url: str) -> str:
    """Strip tracking query params so the bare job URL is all that remains."""
    if not url:
        return ""
    base, _, _q = url.partition("?")
    return base.rstrip("/")


async def get_job(sess: "Session", job_id: str) -> dict:
    """Full job detail. Only fields absent from search results live here:
    connects cost, client spend/hire-rate, and invite activity."""
    payload = await sess.call("get", {"job_id": job_id})
    post = ((payload.get("data") or {}).get("marketplaceJobPosting") or {})
    # client_record sits at the top level of the response, not in the posting.
    client = payload.get("client_record") or {}
    activity = ((post.get("activityStat") or {}).get("jobActivity") or {})
    return {
        "connects_cost": payload.get("connects_cost"),
        "jobs_posted": client.get("jobs_posted"),
        "hire_rate": client.get("hire_rate_percent"),
        "spend_total": client.get("spend_total"),
        "feedback_score": client.get("feedback_score"),
        "total_hired": activity.get("totalHired"),
        "invites_sent": activity.get("invitesSent"),
        "invited_to_interview": activity.get("totalInvitedToInterview"),
        "category": ((post.get("classification") or {}).get("category") or {}).get(
            "preferredLabel"
        ),
        "url": clean_url(post.get("url") or ""),
    }