"""Minimal Model Context Protocol (MCP) client for FreeClaw.

Talks to remote MCP servers over the Streamable HTTP transport — JSON-RPC 2.0
over HTTP POST, with responses arriving either as a single JSON body or as an
SSE `text/event-stream`. No external MCP SDK is required; this leans only on
`requests`, which FreeClaw already depends on.

Server definitions live in the project's `.env` file as three parallel,
JSON-encoded lists so they're easy to edit by hand or from the web UI:

    MCP_NAMES='["github","weather"]'
    MCP_URLS='["https://…/mcp","https://…/mcp"]'
    MCP_TOKENS='["ghp_…",""]'

`read_servers()` parses those into `{"name","url","token"}` dicts, and
`servers_to_env()` turns a list of such dicts back into the `{key: value}`
mapping the caller writes to `.env`. `list_tools()` / `call_tool()` do the
actual protocol work.
"""

import json
import os
import uuid

import requests
from dotenv import dotenv_values


# `.env` lives at the repo root (one level up from this src/ folder), the same
# place Flask/main.py reads and writes it.
ENV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
)

# The three parallel lists we persist under.
NAMES_KEY = "MCP_NAMES"
URLS_KEY = "MCP_URLS"
TOKENS_KEY = "MCP_TOKENS"

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "FreeClaw", "version": "1.0"}

# tools/list results cached by (url, token) so rebuilding the agent's tool
# list (which can happen on every conversation reset) doesn't re-hit every
# server each time. Cleared with clear_cache() when the server list changes.
_tool_cache = {}

# MCP session ids cached by (url, token) so we don't pay a full
# initialize + notifications/initialized round trip before every single
# tools/call. Reused until the server rejects it (see _call_with_session).
_session_cache = {}

# Shared connection pool across all requests to all MCP servers, so repeat
# calls to the same server reuse an existing TCP/TLS connection instead of
# renegotiating one every time.
_http = requests.Session()


# ── .env storage (read + serialize) ──────────────────────────

def _parse_list(raw):
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return val if isinstance(val, list) else []


def read_servers():
    """Return the configured MCP servers as a list of
    {"name", "url", "token"} dicts, read fresh from `.env` on every call so
    runtime edits are picked up without needing a restart."""
    if not os.path.exists(ENV_PATH):
        return []
    env = dotenv_values(ENV_PATH)
    names = _parse_list(env.get(NAMES_KEY))
    urls = _parse_list(env.get(URLS_KEY))
    tokens = _parse_list(env.get(TOKENS_KEY))
    servers = []
    for i, url in enumerate(urls):
        if not url:
            continue
        servers.append({
            "name": names[i] if i < len(names) and names[i] else f"mcp{i + 1}",
            "url": url,
            "token": tokens[i] if i < len(tokens) else "",
        })
    return servers


def servers_to_env(servers):
    """Turn a list of server dicts into the `{ENV_KEY: value}` mapping to
    persist. Values are single-quote-wrapped JSON so brackets and the inner
    double quotes survive a round-trip through python-dotenv untouched.
    (Callers validate that names/urls/tokens contain no single quotes.)"""
    names = [s.get("name", "") for s in servers]
    urls = [s.get("url", "") for s in servers]
    tokens = [s.get("token", "") for s in servers]
    return {
        NAMES_KEY: "'" + json.dumps(names) + "'",
        URLS_KEY: "'" + json.dumps(urls) + "'",
        TOKENS_KEY: "'" + json.dumps(tokens) + "'",
    }


# ── Streamable HTTP JSON-RPC ─────────────────────────────────

def _headers(server, session_id=None):
    headers = {
        "Content-Type": "application/json",
        # Spec requires the client to accept both response shapes.
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    token = (server.get("token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return headers


def _extract_message(resp):
    """Pull the JSON-RPC response object out of `resp`, whether it came back
    as a plain JSON body or as an SSE stream. Returns the dict, or None."""
    ctype = resp.headers.get("Content-Type", "")
    if "text/event-stream" in ctype:
        found = None
        for line in resp.text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            # Keep the last message that actually carries a result/error.
            if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                found = obj
        return found
    try:
        return resp.json()
    except ValueError:
        return None


def _err_text(error):
    if isinstance(error, dict):
        return error.get("message") or json.dumps(error)
    return str(error)


def _rpc(server, method, params, session_id=None, timeout=(6, 20)):
    payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()),
               "method": method, "params": params}
    resp = _http.post(server["url"], headers=_headers(server, session_id),
                       json=payload, timeout=timeout)
    resp.raise_for_status()
    new_session = resp.headers.get("Mcp-Session-Id") or session_id
    return _extract_message(resp), new_session


def _notify(server, method, session_id=None, timeout=(6, 20)):
    payload = {"jsonrpc": "2.0", "method": method}
    try:
        _http.post(server["url"], headers=_headers(server, session_id),
                   json=payload, timeout=timeout)
    except requests.RequestException:
        # Notifications get no response and aren't worth failing over.
        pass


def _open_session(server, force=False):
    """Run the MCP `initialize` handshake and return the session id (or None
    if the server doesn't use one). Cached per (url, token) so repeat calls
    reuse the same session instead of re-handshaking every time — pass
    force=True to discard a cached session that the server has rejected.
    Raises on any protocol/transport error."""
    sig = (server.get("url"), server.get("token"))
    if not force and sig in _session_cache:
        return _session_cache[sig]
    params = {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": CLIENT_INFO,
    }
    msg, session_id = _rpc(server, "initialize", params)
    if msg is None:
        raise RuntimeError("no response to initialize")
    if "error" in msg:
        raise RuntimeError(_err_text(msg["error"]))
    _notify(server, "notifications/initialized", session_id=session_id)
    _session_cache[sig] = session_id
    return session_id


def _call_with_session(server, method, params, timeout=(6, 20)):
    """Run an RPC call against `server` using its cached session. If the
    session has gone stale server-side (surfaces as an HTTP error, typically
    404, on the session id), transparently reopen it and retry once instead
    of failing the whole tool call."""
    sig = (server.get("url"), server.get("token"))
    session_id = _open_session(server)
    try:
        return _rpc(server, method, params, session_id=session_id, timeout=timeout)
    except requests.HTTPError:
        _session_cache.pop(sig, None)
        session_id = _open_session(server, force=True)
        return _rpc(server, method, params, session_id=session_id, timeout=timeout)


def list_tools(server, use_cache=True):
    """Return the raw MCP tool descriptors offered by `server` (each has at
    least `name`, and usually `description` and `inputSchema`)."""
    sig = (server.get("url"), server.get("token"))
    if use_cache and sig in _tool_cache:
        return _tool_cache[sig]
    msg, _ = _call_with_session(server, "tools/list", {})
    if msg is None:
        raise RuntimeError("no response to tools/list")
    if "error" in msg:
        raise RuntimeError(_err_text(msg["error"]))
    tools = (msg.get("result") or {}).get("tools", []) or []
    _tool_cache[sig] = tools
    return tools


def call_tool(server, tool_name, arguments):
    """Invoke `tool_name` on `server` and return its result as plain text."""
    msg, _ = _call_with_session(server, "tools/call",
                                 {"name": tool_name, "arguments": arguments or {}},
                                 timeout=(6, 60))
    if msg is None:
        return "MCP server returned no response."
    if "error" in msg:
        return "MCP error: " + _err_text(msg["error"])
    return _stringify_result(msg.get("result") or {})


def _stringify_result(result):
    """Flatten an MCP tools/call result into text the LLM can read."""
    parts = []
    for block in result.get("content", []) or []:
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "resource":
            res = block.get("resource", {}) or {}
            parts.append(res.get("text") or res.get("uri", ""))
        else:
            parts.append(f"[{btype} content omitted]")
    text = "\n".join(p for p in parts if p)
    if not text and result.get("structuredContent") is not None:
        text = json.dumps(result["structuredContent"])
    if result.get("isError"):
        return "Tool reported an error: " + (text or "unknown error")
    return text or "Tool returned no content."


def clear_cache():
    _tool_cache.clear()
    _session_cache.clear()
