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

Which of those servers is switched *on* is a per-FreeClaw-user choice stored
outside `.env` — see "per-user selection" below, and pass `read_servers(user)`
to get one person's view of the list.

`list_tools()` / `call_tool()` dispatch on the transport, so callers never have
to care which kind of server they're holding.
"""

import atexit
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

import requests

import src.shell as shell
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


# ── servers that ship with FreeClaw ──────────────────────────
#
# A builtin is an ordinary stdio server the user never has to add: it's always
# in the MCP list, and the only thing `.env` stores for it is whether it's
# switched on. The command, the environment and the tools we hold back are
# defined here rather than persisted, so an entry saved by an older FreeClaw
# can't pin a stale command line across an update.
#
# They ship disabled. A browser is a large dependency and a wide new path for
# the agent to reach the network through, so switching it on is the user's
# decision, not a side effect of installing FreeClaw.

BUILTIN_SERVERS = [
    {
        "name": "shadow-web",
        "transport": STDIO,
        # `-m` rather than the `shadow-web-mcp` console script: on Linux
        # FreeClaw runs out of a venv whose bin/ isn't necessarily on the
        # child's PATH, while sys.executable always points at the interpreter
        # that has the package installed.
        #
        # src.browser_mcp_shim rather than shadow_web.mcp.server itself: the
        # package's MCP server hardcodes a fresh in-memory browser context, so
        # a user who signs into a site is signed out again the moment the child
        # restarts. The shim replaces that one function with a version that
        # loads FC_BROWSER_STORAGE_STATE, and runs shadow-web unchanged
        # otherwise. See src/browser_mcp_shim.py for why it's a shim and not a
        # patch or a fork.
        "command": f'"{sys.executable}" -m src.browser_mcp_shim',
        "env": {
            # shadow-web defaults to camoufox — a second ~150MB browser
            # download, and an anti-detect Firefox whose fingerprint spoofing
            # we have no business switching on for a user. Chromium is what
            # `playwright install chromium` puts on disk, so ask for it by
            # name; without this the server raises rather than falling back.
            "SHADOW_WEB_BROWSER": "chromium",
        },
        # `agent_run` starts its own LLM agent loop inside the MCP server,
        # reading DEEPSEEK_API_KEY / OPENAI_API_KEY straight out of the
        # environment it inherited from us. That routes around provider
        # fallback, the token counter, the Stop button and bash approvals in
        # one call, so the model is never shown it.
        "exclude_tools": ("agent_run",),
        "description": "Browser automation with token-compressed page snapshots.",
        # Tools don't work until `playwright install chromium` has run; see
        # src/browser_setup.py, which the enable path drives.
        "needs_browser": True,
        "builtin": True,
    },
]

BUILTIN_NAMES = frozenset(s["name"] for s in BUILTIN_SERVERS)


def is_builtin(name):
    """Whether `name` is one of the servers FreeClaw ships with. Those can be
    toggled but not deleted or overwritten."""
    return name in BUILTIN_NAMES


def for_user(server, user):
    """`server` with this FreeClaw user's browser logins bound to it.

    Only does anything for a server that drives a browser; everything else is
    returned untouched, so callers can apply it blindly.

    The mechanism is `_sig()` below: a stdio server's identity includes its
    overridden environment, so handing two users two different
    FC_BROWSER_STORAGE_STATE paths gets them two separate child processes
    holding two separate cookie jars — without a per-user process registry
    existing anywhere. That isolation is the whole reason this is applied at
    call time rather than baked into BUILTIN_SERVERS: a storage_state is a live
    credential, and one shared child would mean every FreeClaw user browsing as
    whoever signed in last.

    A user with no saved logins gets no variable at all rather than a path to a
    file that isn't there, so the child's signature (and its process) is shared
    with every other signed-out user instead of one being spawned per name."""
    if not server.get("needs_browser"):
        return server
    # Imported here rather than at module scope: browser_profiles imports the
    # logger, and mcp_client is imported by agent.py during startup before that
    # is necessarily configured.
    import src.browser_profiles as browser_profiles

    path = browser_profiles.state_path(user)
    if not path or not os.path.exists(path):
        return server
    out = dict(server)
    out["env"] = {**(server.get("env") or {}), "FC_BROWSER_STORAGE_STATE": path}
    return out

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "FreeClaw", "version": "1.0"}

# Ceiling on one image's base64, ~7MB — comfortably above a full-screen
# screenshot (a 1280px-wide JPEG is around 90KB) and below the request size a
# provider will take.
MAX_IMAGE_B64 = 7_000_000


class ToolText(str):
    """A tool result's text, with any images the tool returned carried
    alongside it as `data:` URLs.

    A `str` subclass rather than a (text, images) pair because a tool result is
    a string everywhere else in FreeClaw: it's returned through the thirty-odd
    exits of agent._run_tool, streamed to the page as a `tool_result` event,
    and stored as a message's `content`. Subclassing leaves every one of those
    working unchanged and lets the one place that cares —
    agent._append_tool_response — pick the images up. Anything that builds a
    new string from it (an f-string, a slice) drops them, which is the right
    default: the text no longer describes the same result."""

    # Class-level default so the attribute is always there, including on an
    # instance str machinery built without going through __new__.
    images = ()

    def __new__(cls, text, images=()):
        obj = super().__new__(cls, text)
        obj.images = tuple(images)
        return obj


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


# ── per-user selection ───────────────────────────────────────
#
# *Which* servers exist is an install-wide decision — someone adds "composio"
# once, with its URL and its token, and it's there for everyone to see. Whether
# a given server is switched *on* is not: one FreeClaw user may want the
# browser and another may not want their agent carrying twenty extra tools it
# never uses. So the on/off choice is stored per user, and the `enabled` flag
# in `.env` is demoted to the default a user inherits until they make a choice
# of their own — which is what keeps an existing install's servers on for
# everybody after an update, and keeps a builtin shipping switched off.
#
# The choices live in `Flask/static/<user>/.mcp_enabled.json`, alongside that
# user's bash approval rules and there for the same two reasons: outside
# `static/<user>/files/`, so the agent's own file tools can't rewrite the list
# of tools it's allowed, and still under `static/`, which the Docker install
# bind-mounts so a choice survives `update.sh` recreating the container.

PREFS_FILENAME = ".mcp_enabled.json"

# Computed the way src/approvals.py computes it rather than imported from
# src/users.py: users.py imports agent.py and agent.py imports this module, so
# reaching back into users.py would close an import cycle.
STATIC_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Flask", "static")
)

# Serialises the read-modify-write in set_enabled(). Contended only between two
# people in Settings at once, so one lock for every user is plenty.
_prefs_lock = threading.Lock()


def prefs_path(user):
    """Where `user`'s on/off choices live. None for a name that isn't a single
    path segment — this is the one place a bad user name could otherwise steer
    a write out of the static tree."""
    if not user:
        return None
    if os.sep in user or (os.altsep and os.altsep in user) or user in (".", ".."):
        return None
    return os.path.join(STATIC_ROOT, user, PREFS_FILENAME)


def read_prefs(user):
    """`{server name: bool}` for the servers this user has actually chosen
    for. A server missing from the mapping hasn't been chosen either way and
    takes the install default, so this is deliberately not a complete list.

    An unreadable or corrupt file is treated as "no choices made". The failure
    mode is then a user seeing the install defaults rather than their tools
    disappearing mid-conversation."""
    path = prefs_path(user)
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.exception("Couldn't read MCP choices for user=%r; using defaults", user)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: bool(v) for k, v in data.items() if isinstance(k, str)}


def set_enabled(user, name, enabled):
    """Record that `user` wants server `name` switched on or off, and return
    their full mapping. Writes via a temp file and a rename so a reader never
    catches the file half-written."""
    path = prefs_path(user)
    if not path:
        raise ValueError(f"Not a valid user name: {user!r}")
    with _prefs_lock:
        prefs = read_prefs(user)
        prefs[name] = bool(enabled)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
        os.replace(tmp, path)
    logger.info("MCP server %r switched %s for user=%r",
                name, "on" if enabled else "off", user)
    return prefs


def forget_everywhere(name):
    """Drop every user's choice about `name` — for a server being removed from
    the install. Without this, adding a server back under a name someone had
    once switched off would silently come back switched off for them, with
    nothing on screen explaining why."""
    if not os.path.isdir(STATIC_ROOT):
        return
    for user in os.listdir(STATIC_ROOT):
        path = prefs_path(user)
        if not path or not os.path.exists(path):
            continue
        with _prefs_lock:
            prefs = read_prefs(user)
            if name not in prefs:
                continue
            prefs.pop(name)
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(prefs, f)
                os.replace(tmp, path)
            except OSError:
                logger.exception("Couldn't drop MCP choice %r for user=%r", name, user)


def _apply_user_prefs(servers, user):
    """`servers` with each `enabled` flag resolved against `user`'s choices.
    Copies rather than mutating: the same dicts are handed to callers holding
    the install-wide view, which must keep the defaults it's about to write
    back to `.env`."""
    if not user:
        return servers
    prefs = read_prefs(user)
    if not prefs:
        return servers
    return [{**s, "enabled": prefs[s["name"]]} if s.get("name") in prefs else s
            for s in servers]


# ── .env storage (read + serialize) ──────────────────────────

def read_env_values(path=ENV_PATH):
    """`.env` read as literal text: {KEY: value} with one layer of wrapping
    quotes removed and nothing else interpreted.

    Deliberately *not* `dotenv_values()`, and this is the whole reason the
    function exists. Inside a quoted value python-dotenv unescapes backslash
    sequences, so a Windows command that `json.dumps` correctly wrote as
    `C:\\\\Users\\\\…` reads back as `C:\\Users\\…` — no longer valid JSON, so
    `parse_env_list` below returned [] for the *entire* list. One Windows path
    anywhere in MCP_COMMANDS therefore made every stdio server disappear on
    read, builtins included, while the add that put it there still reported
    success from its in-memory copy.

    The file on disk is already valid JSON, so reading it verbatim is both the
    fix and the smaller behaviour. This understands exactly the one-line
    `KEY=value` shape `_write_env` produces; the rest of what dotenv does
    (interpolation, multi-line values, escapes) has never applied to the keys
    read through here, all of which we write ourselves.

    Public because agent.py's provider lists are stored the same way and are
    parsed through the same pair of functions."""
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key:
            continue
        value = value.strip()
        # Same one layer dotenv strips — and, unlike dotenv, that's all.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def parse_env_list(raw):
    """One of the single-quote-wrapped JSON lists we persist in .env, parsed
    back into a Python list — or [] for anything missing or malformed. Public
    because agent.py stores its provider lists the exact same way and shares
    this parser rather than keeping a copy.

    Feed it values from `read_env_values()`, not `dotenv_values()` — see there
    for what the difference costs."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # Logged rather than swallowed silently: returning [] here drops every
        # entry in the list at once, which is invisible from the outside — the
        # symptom is servers that were configured simply not being there.
        logger.warning("Ignoring a malformed .env list (%.120r)", raw)
        return []
    return val if isinstance(val, list) else []


def read_servers(user=None):
    """The configured MCP servers, read fresh from `.env` on every call so
    runtime edits need no restart.

    Sized to the longest list rather than keyed off URLs the way it was when
    http was the only transport — a stdio server has no URL, so that would
    have dropped every stdio entry. An entry with nothing to connect to (no URL
    on an http server, no command on a stdio one) is skipped.

    The builtins (BUILTIN_SERVERS) are always present in the result, whether or
    not `.env` exists yet — a fresh install has them in the list from the first
    page load, switched off.

    `user` resolves each `enabled` flag against that FreeClaw user's own
    choices (see "per-user selection" above), which is what a turn wants: the
    servers to offer *this* person's agent. Leave it out for the install-wide
    view — what the add and remove paths want, since they write the list
    straight back to `.env` and must not persist one user's choice as
    everyone's default."""
    if not os.path.exists(ENV_PATH):
        return _apply_user_prefs(_merge_builtins([]), user)
    env = read_env_values(ENV_PATH)
    names = parse_env_list(env.get(NAMES_KEY))
    urls = parse_env_list(env.get(URLS_KEY))
    tokens = parse_env_list(env.get(TOKENS_KEY))
    enabled = parse_env_list(env.get(ENABLED_KEY))
    transports = parse_env_list(env.get(TRANSPORTS_KEY))
    commands = parse_env_list(env.get(COMMANDS_KEY))
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
    return _apply_user_prefs(_merge_builtins(servers), user)


def _merge_builtins(servers):
    """`servers` with the shipped entries folded in.

    A stored entry contributes exactly one thing: its `enabled` flag. Command,
    environment and tool exclusions always come from BUILTIN_SERVERS, so an
    install whose `.env` still carries what shipped two versions ago picks up
    the current definition on upgrade. Builtins sort first so they sit at the
    top of the Settings list rather than below whatever the user has added."""
    stored = {s.get("name"): s for s in servers if s.get("name") in BUILTIN_NAMES}
    out = []
    for spec in BUILTIN_SERVERS:
        entry = dict(spec)
        prior = stored.get(spec["name"])
        entry["url"] = ""
        entry["token"] = ""
        entry["enabled"] = bool(prior.get("enabled")) if prior else False
        out.append(entry)
    out.extend(s for s in servers if s.get("name") not in BUILTIN_NAMES)
    return out


def servers_to_env(servers):
    """Turn a list of server dicts into the `{ENV_KEY: value}` mapping to
    persist. Values are single-quote-wrapped JSON so brackets and the inner
    double quotes survive the trip through `.env` intact.
    (Callers validate that no field contains a single quote or newline — a
    double quote is fine, which is what lets a stdio command quote a path
    with spaces in it.)

    What comes back out has to be read with `read_env_values()`: JSON escapes
    every backslash in a Windows path, and python-dotenv would unescape them
    again on the way back in."""
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
    reusing one authorized with the old one. For stdio the overridden
    environment is part of the key too: the same command run with a different
    SHADOW_WEB_BROWSER is a different server, and reusing the running child
    would silently ignore the change."""
    if (server.get("transport") or HTTP) == STDIO:
        env = server.get("env") or {}
        return (STDIO, server.get("command") or "", tuple(sorted(env.items())))
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

# See src/agent.py: NO_WINDOW. Under the Windows tray app every stdio server
# would otherwise pop its own console window and leave it on screen for as long
# as the server stayed connected. A no-op off Windows.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Generous enough for `npx` to fetch a package it hasn't cached yet, the
# slowest thing that legitimately happens on a first connect.
STDIO_STARTUP_TIMEOUT = 60
STDIO_CALL_TIMEOUT = 60

# ── how long one tool call may hold the line ─────────────────
#
# A short read timeout is what stops a hung server from stalling a whole turn,
# and for almost every tool 60s is already generous. But a few exist precisely
# to block: Composio's COMPOSIO_WAIT_FOR_CONNECTIONS sits on the socket until
# the user finishes an OAuth flow in a browser, which routinely outlasts a
# minute. Against those a flat timeout fires while the tool is doing exactly
# what it was asked to, and the model is handed a transport error for a call
# that never actually failed — so the wait is derived from the call instead.
CALL_READ_TIMEOUT = 60
# For a tool that is evidently a long poll but didn't say how long to wait.
LONG_POLL_READ_TIMEOUT = 180
# Nothing waits past this, whatever the arguments ask for. The read blocks
# inside requests and can't be interrupted, so this is also the longest the
# Stop button can sit there looking dead.
MAX_READ_TIMEOUT = 600
# Headroom over a wait the tool was itself told to perform, so the server gets
# to return its own "timed out" result — which the model can read and act on —
# instead of us severing the socket a moment before it answers.
TIMEOUT_HEADROOM = 15

# Tool names whose job is to wait. Matched on the name because at call time
# that and the arguments are all we have; deliberately narrow, since a wrong
# guess here buys a genuinely stuck server three extra minutes to hang a turn.
_LONG_POLL_NAME_RE = re.compile(r"(^|_)(wait|poll)(_|$)", re.I)

# What servers call the "how long may this block" argument.
_WAIT_ARG_KEYS = ("timeout", "timeout_seconds", "timeout_s", "timeout_ms",
                  "wait_timeout", "wait_seconds", "wait_for", "max_wait",
                  "max_wait_seconds", "poll_timeout")


def _read_timeout_for(tool_name, arguments):
    """Seconds to let this particular tool call hold the socket.

    A tool that was handed its own wait length is taken at its word: blocking
    for that long is the call working, not hanging, and cutting it short turns
    a functioning tool into one that can never succeed. Anything with no such
    argument and no wait-ish name keeps the short default, so an unresponsive
    server still fails fast and the chain moves on."""
    stated = 0.0
    for key in _WAIT_ARG_KEYS:
        value = (arguments or {}).get(key)
        # bools are ints in Python, and mean "should I wait", never "how long".
        if value is None or isinstance(value, bool):
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        # A key that names its unit is believed outright. One that doesn't is
        # read as milliseconds only when seconds would be absurd: a timeout of
        # 120000 is two minutes, not a day and a half.
        if key.endswith("_ms") or seconds > MAX_READ_TIMEOUT * 10:
            seconds /= 1000
        stated = max(stated, seconds)
    if stated > 0:
        return min(stated + TIMEOUT_HEADROOM, MAX_READ_TIMEOUT)
    if _LONG_POLL_NAME_RE.search(tool_name or ""):
        return LONG_POLL_READ_TIMEOUT
    return CALL_READ_TIMEOUT


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

    def __init__(self, command, env=None):
        self.command = command
        self._inbox = queue.Queue()
        self._lock = threading.Lock()  # one in-flight request at a time
        self._next_id = 0

        try:
            argv = shell.split_command(command)
        except ValueError as e:
            raise StdioSpawnFailed(f"couldn't parse the command ({e})")
        if not argv:
            raise StdioSpawnFailed("the command is empty")

        # `npx`, `uvx` and most other Node/Python launchers install on Windows
        # as `.cmd` shims. Popen resolves argv[0] through CreateProcess, which
        # applies no PATHEXT search, so a bare `npx` is simply not found —
        # every npx-based MCP server would fail to start. shutil.which does
        # apply PATHEXT, so resolving first is what makes them spawnable.
        # No-op on POSIX and for the built-in browser server, which already
        # names an absolute interpreter path.
        argv[0] = shell.resolve_program(argv[0])

        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
                bufsize=1,             # line buffered — newline-delimited JSON
                creationflags=NO_WINDOW,   # no console window (see above)
                # .env is already loaded into our own environment; `env` on top
                # of it is how a builtin pins settings the user shouldn't have
                # to know about (see BUILTIN_SERVERS).
                env={**os.environ, **(env or {})},
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
        proc = _StdioServer(command, server.get("env"))
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
    """Invoke `tool_name` on `server` and return its result as text — a
    ToolText, if the tool returned images along with it.

    How long the call may block is decided per call — see _read_timeout_for."""
    read_timeout = _read_timeout_for(tool_name, arguments)
    try:
        msg = _rpc_for(server, "tools/call",
                       {"name": tool_name, "arguments": arguments or {}},
                       http_timeout=(6, read_timeout),
                       stdio_timeout=read_timeout)
    except requests.Timeout:
        # Answered rather than raised, so the model gets a usable sentence
        # instead of a stack of urllib3 wrapping. The last line is the point:
        # a timed-out wait is the one failure a model is most tempted to retry
        # immediately, which is how a single slow tool becomes a runaway turn.
        logger.warning("MCP tool '%s' on '%s' held the connection past %ss",
                       tool_name, server.get("name"), read_timeout)
        return (f"No result: '{tool_name}' was still running after {read_timeout:.0f}s, "
                "so the connection was closed. It may have finished anyway — check "
                "before doing anything else, and do not simply call it again.")
    if msg is None:
        return "MCP server returned no response."
    if "error" in msg:
        return "MCP error: " + _err_text(msg["error"])
    return _stringify_result(msg.get("result") or {})


def _image_data_url(block):
    """A `data:` URL for one MCP image block, or None if there's nothing
    usable in it. Oversized images are refused rather than truncated: the
    payload is resent on every request for as long as it stays in the
    conversation, so one enormous screenshot would be paid for many times."""
    data = block.get("data") or ""
    if not data or len(data) > MAX_IMAGE_B64:
        if data:
            logger.info("Dropping a %d-byte MCP image — over the %d-byte limit",
                        len(data), MAX_IMAGE_B64)
        return None
    mime = block.get("mimeType") or "image/png"
    return f"data:{mime};base64,{data}"


def _stringify_result(result):
    """Flatten an MCP tools/call result into text the LLM can read, as a
    ToolText carrying any images the tool returned alongside it.

    Images used to be flattened to "[image content omitted]" like every other
    non-text block, which meant a screenshot tool could be called but never
    seen. They're kept here and turned into image content on the request in
    agent.py; see ToolText for why they ride on a string."""
    parts = []
    images = []
    for block in result.get("content", []) or []:
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "resource":
            res = block.get("resource", {}) or {}
            parts.append(res.get("text") or res.get("uri", ""))
        elif btype == "image":
            url = _image_data_url(block)
            if url:
                images.append(url)
            elif block.get("data"):
                parts.append("[image too large to include]")
            else:
                parts.append("[image content omitted]")
        else:
            parts.append(f"[{btype} content omitted]")
    text = "\n".join(p for p in parts if p)
    if not text and result.get("structuredContent") is not None:
        text = json.dumps(result["structuredContent"])
    if result.get("isError"):
        return ToolText("Tool reported an error: " + (text or "unknown error"), images)
    if not text:
        # An image and nothing else is a complete answer — say so rather than
        # reporting the call as empty.
        text = (f"Returned {len(images)} image(s)." if images
                else "Tool returned no content.")
    return ToolText(text, images)


def clear_cache():
    """Drop every cached tool list and live session. Called whenever something
    changes that could affect any server — so each is re-connected from
    scratch, which for stdio means killing the child process it was using
    rather than leaving it running with no way to reach it.

    Scorched earth, and every user's servers go with it. When only one server
    is actually affected — added, removed, or switched off by the last user who
    had it on — `release()` below does the same job to that server alone."""
    _tool_cache.clear()
    _session_cache.clear()
    shutdown_stdio_servers()


def release(server):
    """Drop everything cached for one server: its tool list, its HTTP session,
    and — for stdio — every child process running its command.

    *Every* child, not one: a server that drives a browser is spawned once per
    FreeClaw user, since each child carries that user's logins in its
    environment (see `for_user`), so one server can have several processes
    behind it. They're matched on the command rather than the full signature
    for exactly that reason.

    Used where clear_cache() would be too broad — now that a server can be on
    for one user and off for another, tearing down every connection in the
    process because one person flipped one switch would interrupt whatever
    everyone else's agent was in the middle of."""
    stdio = (server.get("transport") or HTTP) == STDIO
    command = server.get("command") or ""

    def mine(sig):
        if sig[0] != (STDIO if stdio else HTTP):
            return False
        return sig[1] == (command if stdio else server.get("url"))

    for cache in (_tool_cache, _session_cache):
        for sig in [k for k in cache if mine(k)]:
            cache.pop(sig, None)
    if not stdio:
        return
    with _stdio_registry_lock:
        sigs = [k for k in _stdio_procs if mine(k)]
        procs = [_stdio_procs.pop(sig) for sig in sigs]
    for proc in procs:
        try:
            proc.shutdown()
        except Exception:
            logger.exception("Couldn't shut down stdio MCP process %r", proc.command)
