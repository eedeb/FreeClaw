"""Minimal Model Context Protocol (MCP) client for FreeClaw.

Speaks two transports, both JSON-RPC 2.0 and both without an external MCP SDK:

  * **http** — Streamable HTTP: one POST per message, the response arriving
    either as a JSON body or an SSE `text/event-stream`. Leans only on
    `requests`, which FreeClaw already depends on.
  * **stdio** — a locally spawned child process, newline-delimited JSON on its
    stdin/stdout. This is how most of the MCP ecosystem actually ships
    (`npx …`, `uvx …`), so it's what makes the published servers reachable.
    Children inherit FreeClaw's environment, already populated from `.env` by
    `load_dotenv()`, so a server wanting e.g. `GITHUB_TOKEN` is configured by
    putting that key in `.env` rather than through a second mechanism here.

Server definitions live in `.env` as parallel JSON lists (MCP_NAMES, _URLS,
_TOKENS, _ENABLED, _TRANSPORTS, _COMMANDS) so they're editable by hand or from
the web UI. `read_servers()` parses those into
`{"name","url","token","enabled","transport","command"}` dicts and
`servers_to_env()` reverses it. Entries saved before a field existed keep the
old behaviour — no MCP_ENABLED means enabled, no MCP_TRANSPORTS means http —
so an existing install is untouched.

`list_tools()` / `call_tool()` dispatch on the transport, so callers never have
to care which kind of server they're holding.
"""

import atexit
import json
import os
import queue
import shlex
import subprocess
import threading
import time
import uuid

import requests
from dotenv import dotenv_values

from src.logging_setup import get_logger

logger = get_logger(__name__)


# `.env` lives at the repo root (one level up from this src/ folder), the same
# place Flask/main.py reads and writes it.
ENV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
)

# The parallel lists we persist under.
NAMES_KEY = "MCP_NAMES"
URLS_KEY = "MCP_URLS"
TOKENS_KEY = "MCP_TOKENS"
ENABLED_KEY = "MCP_ENABLED"
TRANSPORTS_KEY = "MCP_TRANSPORTS"
COMMANDS_KEY = "MCP_COMMANDS"

HTTP = "http"
STDIO = "stdio"
TRANSPORTS = (HTTP, STDIO)

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "FreeClaw", "version": "1.0"}

# tools/list results cached by _sig(server) so rebuilding the agent's tool
# list (which can happen on every conversation reset) doesn't re-hit every
# server each time. Cleared with clear_cache() when the server list changes.
_tool_cache = {}

# MCP session ids cached by _sig(server) so we don't pay a full
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
    """The configured MCP servers, read fresh from `.env` on every call so
    runtime edits need no restart.

    Sized to the longest list rather than keyed off URLs the way it was when
    http was the only transport — a stdio server has no URL, so that would
    have dropped every stdio entry. An entry with nothing to connect to (no URL
    on an http server, no command on a stdio one) is skipped."""
    if not os.path.exists(ENV_PATH):
        return []
    env = dotenv_values(ENV_PATH)
    names = _parse_list(env.get(NAMES_KEY))
    urls = _parse_list(env.get(URLS_KEY))
    tokens = _parse_list(env.get(TOKENS_KEY))
    enabled = _parse_list(env.get(ENABLED_KEY))
    transports = _parse_list(env.get(TRANSPORTS_KEY))
    commands = _parse_list(env.get(COMMANDS_KEY))
    count = max(len(names), len(urls), len(commands))
    servers = []
    for i in range(count):
        transport = transports[i] if i < len(transports) and transports[i] else HTTP
        if transport not in TRANSPORTS:
            transport = HTTP
        url = urls[i] if i < len(urls) else ""
        command = commands[i] if i < len(commands) else ""
        if transport == HTTP and not url:
            continue
        if transport == STDIO and not command:
            continue
        servers.append({
            "name": names[i] if i < len(names) and names[i] else f"mcp{i + 1}",
            "url": url,
            "token": tokens[i] if i < len(tokens) else "",
            "enabled": bool(enabled[i]) if i < len(enabled) else True,
            "transport": transport,
            "command": command,
        })
    return servers


def servers_to_env(servers):
    """Turn a list of server dicts into the `{ENV_KEY: value}` mapping to
    persist. Values are single-quote-wrapped JSON so brackets and the inner
    double quotes survive a round-trip through python-dotenv untouched.
    (Callers validate that no field contains a single quote or newline — a
    double quote is fine, which is what lets a stdio command quote a path
    with spaces in it.)"""
    return {
        NAMES_KEY: "'" + json.dumps([s.get("name", "") for s in servers]) + "'",
        URLS_KEY: "'" + json.dumps([s.get("url", "") for s in servers]) + "'",
        TOKENS_KEY: "'" + json.dumps([s.get("token", "") for s in servers]) + "'",
        ENABLED_KEY: "'" + json.dumps([bool(s.get("enabled", True)) for s in servers]) + "'",
        TRANSPORTS_KEY: "'" + json.dumps([s.get("transport") or HTTP for s in servers]) + "'",
        COMMANDS_KEY: "'" + json.dumps([s.get("command", "") for s in servers]) + "'",
    }


def _sig(server):
    """Cache key identifying one server's live connection. Includes the
    transport so an http and a stdio server can never share a cache slot, and
    the token so a rotated credential opens a fresh session rather than
    reusing one authorized with the old one."""
    if (server.get("transport") or HTTP) == STDIO:
        return (STDIO, server.get("command") or "")
    return (HTTP, server.get("url"), server.get("token"))


def describe(server):
    """Short human-readable target for log/error messages."""
    if (server.get("transport") or HTTP) == STDIO:
        return server.get("command") or "(no command)"
    return server.get("url") or "(no url)"


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
    sig = _sig(server)
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
    sig = _sig(server)
    session_id = _open_session(server)
    try:
        return _rpc(server, method, params, session_id=session_id, timeout=timeout)
    except requests.HTTPError:
        _session_cache.pop(sig, None)
        session_id = _open_session(server, force=True)
        return _rpc(server, method, params, session_id=session_id, timeout=timeout)


# ── stdio JSON-RPC (local child process) ─────────────────────
#
# One long-lived child per configured stdio server, spawned on first use and
# reused after — spawning `npx …` per tool call would add seconds to each one.

_stdio_procs = {}                        # _sig(server) -> _StdioServer
_stdio_registry_lock = threading.Lock()  # guards the dict, not the per-process I/O

# Generous enough for `npx` to fetch a package it hasn't cached yet, the
# slowest thing that legitimately happens on a first connect.
STDIO_STARTUP_TIMEOUT = 60
STDIO_CALL_TIMEOUT = 60


class StdioUnavailable(RuntimeError):
    """The child died or stopped answering — worth retrying with a fresh one."""


class StdioSpawnFailed(StdioUnavailable):
    """The child could never start (missing binary, unparseable command line).
    A retry would fail identically, so `_stdio_call` doesn't bother and the
    user gets the real reason instead of a doubled respawn log."""


class _StdioServer:
    """A single MCP server running as a child process.

    Stdout is drained by a thread rather than read inline: a server that writes
    nothing (hung, or waiting on a credential prompt) would otherwise block the
    agent's whole turn with no way out, and a stray non-JSON line — startup
    banners are common — would desync every later response. The thread keeps
    what parses, drops what doesn't, and queues messages so RPCs can wait with
    a timeout."""

    def __init__(self, command):
        self.command = command
        self._inbox = queue.Queue()
        self._lock = threading.Lock()  # one in-flight request at a time
        self._next_id = 0

        try:
            argv = shlex.split(command)
        except ValueError as e:
            raise StdioSpawnFailed(f"couldn't parse the command ({e})")
        if not argv:
            raise StdioSpawnFailed("the command is empty")

        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
                bufsize=1,             # line buffered — newline-delimited JSON
                env=os.environ.copy(),  # .env is already loaded into it
            )
        except FileNotFoundError:
            raise StdioSpawnFailed(
                f"'{argv[0]}' isn't installed or isn't on PATH. Stdio MCP servers run a "
                f"local command, so whatever it needs (node/npx, uv/uvx, python…) has to "
                f"exist on the machine FreeClaw runs on — inside the container, for a "
                f"Docker install."
            )
        except OSError as e:
            raise StdioSpawnFailed(f"couldn't start '{argv[0]}': {e}")

        for target in (self._read_stdout, self._read_stderr):
            threading.Thread(target=target, daemon=True,
                             name=f"mcp-stdio:{argv[0]}").start()

    def _read_stdout(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._inbox.put(json.loads(line))
                except json.JSONDecodeError:
                    # Not a protocol message — a banner, a progress line, or
                    # something logging to the wrong stream. Recorded, ignored.
                    logger.debug("[mcp stdio] non-JSON stdout from %r: %.300r",
                                 self.command, line)
        except (ValueError, OSError):
            pass  # pipe closed — alive() reports the death
        finally:
            # Unblock anyone waiting on a reply that can now never arrive.
            self._inbox.put(None)

    def _read_stderr(self):
        """Drain stderr so a chatty server can't fill the pipe buffer and
        deadlock itself. MCP servers use stderr for ordinary logging, so this
        goes to the debug log, not the error log."""
        try:
            for line in self.proc.stderr:
                line = line.strip()
                if line:
                    logger.debug("[mcp stdio] %r: %.500s", self.command, line)
        except (ValueError, OSError):
            pass

    def alive(self):
        return self.proc.poll() is None

    def _send(self, payload):
        if not self.alive():
            raise StdioUnavailable(f"process exited (code {self.proc.returncode})")
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as e:
            raise StdioUnavailable(f"couldn't write to the process: {e}")

    def _await(self, request_id, timeout):
        """The response carrying `request_id`, skipping anything that arrives
        first (notifications, server-initiated requests, a late reply to a
        timed-out call). The deadline is absolute, so a chatty server can't keep
        extending the wait."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StdioUnavailable(f"no response within {timeout}s")
            try:
                msg = self._inbox.get(timeout=remaining)
            except queue.Empty:
                raise StdioUnavailable(f"no response within {timeout}s")
            if msg is None:
                raise StdioUnavailable(
                    f"process exited (code {self.proc.returncode}) before replying")
            if msg.get("id") == request_id:
                return msg

    def rpc(self, method, params, timeout):
        with self._lock:
            self._next_id += 1
            self._send({"jsonrpc": "2.0", "id": self._next_id,
                        "method": method, "params": params})
            return self._await(self._next_id, timeout)

    def initialize(self):
        msg = self.rpc("initialize", {"protocolVersion": PROTOCOL_VERSION,
                                      "capabilities": {}, "clientInfo": CLIENT_INFO},
                       timeout=STDIO_STARTUP_TIMEOUT)
        if "error" in msg:
            raise RuntimeError(_err_text(msg["error"]))
        with self._lock:
            try:
                self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            except StdioUnavailable:
                pass  # a notification gets no reply and isn't worth failing over

    def shutdown(self):
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except OSError:
            pass
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _get_stdio(server, force=False):
    """The live child process for `server`, spawning and initializing it if
    needed. force=True replaces a process we've decided is unusable."""
    sig = _sig(server)
    command = server.get("command") or ""
    with _stdio_registry_lock:
        existing = _stdio_procs.get(sig)
        if existing is not None and (force or not existing.alive()):
            existing.shutdown()
            _stdio_procs.pop(sig, None)
            existing = None
        if existing is not None:
            return existing
        proc = _StdioServer(command)
        _stdio_procs[sig] = proc
    # Handshake outside the registry lock — it can take seconds (npx fetching
    # a package), and holding the lock would stall every other server's
    # first connect behind this one.
    try:
        proc.initialize()
    except Exception:
        with _stdio_registry_lock:
            if _stdio_procs.get(sig) is proc:
                _stdio_procs.pop(sig, None)
        proc.shutdown()
        raise
    return proc


def _stdio_call(server, method, params, timeout=STDIO_CALL_TIMEOUT):
    """Run one RPC against `server`'s child process, respawning it once if the
    existing one has died (the stdio equivalent of the HTTP path's stale
    session retry — a server that crashed on the last call shouldn't
    permanently break the tool). A command that can't be spawned at all is
    reported as-is: a second identical attempt would fail identically."""
    try:
        proc = _get_stdio(server)
        return proc.rpc(method, params, timeout)
    except StdioSpawnFailed:
        raise
    except StdioUnavailable as e:
        logger.warning("[mcp stdio] %r unavailable (%s) — respawning", server.get("name"), e)
        proc = _get_stdio(server, force=True)
        return proc.rpc(method, params, timeout)


def shutdown_stdio_servers():
    """Terminate every child process we started. Registered with atexit, and
    called by clear_cache() when the server list changes so a removed or
    edited server doesn't leave an orphan running."""
    with _stdio_registry_lock:
        procs = list(_stdio_procs.values())
        _stdio_procs.clear()
    for proc in procs:
        try:
            proc.shutdown()
        except Exception:
            logger.exception("Couldn't shut down stdio MCP process %r", proc.command)


atexit.register(shutdown_stdio_servers)


# ── transport-agnostic API ───────────────────────────────────

def _rpc_for(server, method, params, http_timeout=(6, 20), stdio_timeout=STDIO_CALL_TIMEOUT):
    """Run one JSON-RPC call over whichever transport `server` uses, and
    return the response message (or None)."""
    if (server.get("transport") or HTTP) == STDIO:
        return _stdio_call(server, method, params, timeout=stdio_timeout)
    msg, _ = _call_with_session(server, method, params, timeout=http_timeout)
    return msg


def list_tools(server, use_cache=True):
    """Return the raw MCP tool descriptors offered by `server` (each has at
    least `name`, and usually `description` and `inputSchema`)."""
    sig = _sig(server)
    if use_cache and sig in _tool_cache:
        return _tool_cache[sig]
    msg = _rpc_for(server, "tools/list", {}, stdio_timeout=STDIO_STARTUP_TIMEOUT)
    if msg is None:
        raise RuntimeError("no response to tools/list")
    if "error" in msg:
        raise RuntimeError(_err_text(msg["error"]))
    tools = (msg.get("result") or {}).get("tools", []) or []
    _tool_cache[sig] = tools
    return tools


def call_tool(server, tool_name, arguments):
    """Invoke `tool_name` on `server` and return its result as plain text."""
    msg = _rpc_for(server, "tools/call",
                   {"name": tool_name, "arguments": arguments or {}},
                   http_timeout=(6, 60))
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
    """Drop every cached tool list and live session. Called whenever the
    server list changes, so an edited or removed server is re-connected from
    scratch — which for stdio means killing the child process it was using
    rather than leaving it running with no way to reach it."""
    _tool_cache.clear()
    _session_cache.clear()
    shutdown_stdio_servers()
