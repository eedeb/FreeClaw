"""The agent's connected apps, from code it runs.

The Developer app runs code; this lets that code use the agent's own MCP
tools, the apps the person connected (not the built-ins), so a script can go
through 200 emails in a loop instead of the agent making 200 tool calls:

    import freeclaw_tools as ft
    print(ft.tools())                                  # what it can call
    out = ft.call("GMAIL_FETCH_EMAILS", {"max_results": 5})

How it's wired:

  * This process serves one Unix socket (socket_path(), a folder only this
    user can open). Only the Developer app's own processes are told where it is.
  * The Developer process gives each run a socket of its own (src/dev_server.py
    RunTools), passes it to the program as FREECLAW_TOOLS, and stamps every
    request that comes through it with the user the run belongs to, never
    taken from the request. That's how code can only act as the agent that
    ran it.
  * Here every request goes through CHECKS before it runs. Each check is
    check(user, entry, arguments) and returns a sentence (refused, and why)
    or None. An install adds its own: an approval policy, a rate limit, a
    server it doesn't want reached from code.

The program already ran with the person's approval (the run tool asks), so
on a self-hosted install that's the only gate; RATE_PER_MINUTE keeps a loop
from hammering a service.
"""
import json
import os
import socket
import tempfile
import threading
import time

from src.logging_setup import get_logger

logger = get_logger(__name__)

REQUEST_MAX = 1024 * 1024
RESULT_MAX = 2 * 1024 * 1024
RATE_PER_MINUTE = int(os.environ.get("FC_WORKBENCH_PER_MINUTE", "120"))

# check(user, entry, arguments) -> refusal sentence or None. See the docstring.
CHECKS = []

_stamps = {}
_lock = threading.Lock()
_server = None


def socket_path():
    configured = (os.environ.get("FC_WORKBENCH_SOCKET") or "").strip()
    if configured:
        return configured
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return os.path.join(tempfile.gettempdir(), f"freeclaw-workbench-{uid}", "wb.sock")


def _rate_ok(user, now=None):
    now = now or time.time()
    with _lock:
        stamps = [t for t in _stamps.get(user, []) if now - t < 60]
        if len(stamps) >= RATE_PER_MINUTE:
            _stamps[user] = stamps
            return False
        stamps.append(now)
        _stamps[user] = stamps
        return True


def _app_entries(user):
    """{exposed name: entry} for `user`'s connected apps' tools: MCP servers
    that aren't FreeClaw's own built-ins (no running code from code, no
    driving the browser from a script)."""
    import src.agent as agent
    return {name: entry for name, entry in agent.registry_for(user).items()
            if not (entry.get("server") or {}).get("builtin")}


def find(entries, tool):
    """The entry `tool` names: its exposed name ("mcp_Gmail_send"), or the
    server's own name for it when that's unambiguous."""
    tool = str(tool or "").strip()
    if tool in entries:
        return entries[tool]
    matches = [e for e in entries.values() if e.get("tool") == tool]
    if not matches:
        matches = [e for e in entries.values() if str(e.get("tool") or "").lower() == tool.lower()]
    return matches[0] if len(matches) == 1 else None


def handle(user, request):
    """One request from a run of `user`'s: {"op": "tools"} or {"op": "call",
    "tool": ..., "arguments": {...}}. Returns the reply dict."""
    import src.users as users
    if not user or not users.user_exists(user):
        return {"ok": False, "error": "Unknown user."}
    op = request.get("op")
    entries = _app_entries(user)
    if op == "tools":
        return {"ok": True, "result": [
            {"name": name, "server": (e.get("server") or {}).get("name"), "tool": e.get("tool")}
            for name, e in sorted(entries.items())]}
    if op != "call":
        return {"ok": False, "error": "op must be tools or call."}
    arguments = request.get("arguments") or {}
    if not isinstance(arguments, dict):
        return {"ok": False, "error": "arguments must be an object."}
    entry = find(entries, request.get("tool"))
    if entry is None:
        return {"ok": False, "error": f"No connected-app tool called {request.get('tool')!r} "
                                      "(tools() lists them)."}
    if not _rate_ok(user):
        return {"ok": False, "error": f"Too many calls: at most {RATE_PER_MINUTE} a minute from code."}
    for check in list(CHECKS):
        try:
            refused = check(user, entry, arguments)
        except Exception:                                    # noqa: BLE001 — a broken check refuses
            logger.exception("A workbench check failed")
            refused = "Not done: a safety check couldn't run."
        if refused:
            return {"ok": False, "error": refused}
    return {"ok": True, "result": _call(user, entry, arguments)}


def _call(user, entry, arguments):
    import src.mcp_client as mcp_client
    import src.session as sessions
    import src.users as users
    sess = sessions.Session(name=user, static_dir=users.conv_files_dir(user) + os.sep)
    sess.approval_user = user
    sess.approval_interactive = False
    with sessions.use(sess):
        result = mcp_client.call_tool(entry["server"], entry["tool"], arguments)
    text = str(result)[:RESULT_MAX]
    try:
        return json.loads(text)
    except ValueError:
        return text


def _serve_one(conn):
    try:
        conn.settimeout(30)
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > REQUEST_MAX:
                return
        try:
            request = json.loads(data.decode("utf-8"))
        except ValueError:
            request = None
        if isinstance(request, dict):
            reply = handle(str(request.get("user") or ""), request)
        else:
            reply = {"ok": False, "error": "Bad request."}
        conn.settimeout(120)
        conn.sendall(json.dumps(reply, ensure_ascii=False, default=str).encode() + b"\n")
    except Exception:                                        # noqa: BLE001 — one request
        logger.exception("A workbench request failed")
    finally:
        conn.close()


def start(path=None):
    """Serve the socket (once). Returns its path, or None where Unix sockets
    aren't available."""
    global _server
    if _server is not None:
        return _server.getsockname()
    if not hasattr(socket, "AF_UNIX"):
        return None
    path = path or socket_path()
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX)
    server.bind(path)
    os.chmod(path, 0o600)
    server.listen(32)
    _server = server

    def accept():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=_serve_one, args=(conn,), daemon=True).start()

    threading.Thread(target=accept, daemon=True, name="workbench").start()
    return path
