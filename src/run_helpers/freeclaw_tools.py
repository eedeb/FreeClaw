"""The agent's connected apps, from code (FreeClaw's Developer app).

    import freeclaw_tools as ft
    ft.tools()                                      # [{"name", "server", "tool"}, ...]
    ft.call("GMAIL_FETCH_EMAILS", {"max_results": 5})

`call` takes a tool's name as tools() lists it, or the app's own name for it.
Only available inside a Developer app run (FREECLAW_TOOLS is set there).
"""
import json
import os
import socket


class ToolsError(RuntimeError):
    pass


def _ask(payload):
    path = os.environ.get("FREECLAW_TOOLS")
    if not path or not hasattr(socket, "AF_UNIX"):
        raise ToolsError("Connected apps aren't available here (only in a Developer app run).")
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(180)
    try:
        s.connect(path)
        s.sendall(json.dumps(payload).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except OSError as e:
        raise ToolsError(f"Connected apps aren't reachable: {e}")
    finally:
        s.close()
    reply = json.loads(data.decode("utf-8") or "{}")
    if not reply.get("ok"):
        raise ToolsError(reply.get("error") or "It didn't work.")
    return reply.get("result")


def tools():
    """The connected apps' tools this agent can call."""
    return _ask({"op": "tools"})


def call(tool, arguments=None):
    """Call one tool with `arguments` and return what it answered (parsed
    JSON when it's JSON)."""
    return _ask({"op": "call", "tool": str(tool), "arguments": arguments or {}})
