"""Activity: what the agent did while nobody was watching.

Every turn nobody typed in the chat — a scheduled task firing, a message
from Discord or another bridge — leaves one entry: when, what it was asked,
how it went, what it said, and which apps it used. "How it went" is the
point. A turn that wanted the user's approval and had nobody to ask
finished, but did not do the thing; the entry says so ("needs you"), where
the reply alone reads like a success.

The record is the user's own data, kept beside their conversation in
static/<user>/activity.json (not in files/, where the agent's tools work):
at most KEEP entries and DAYS days, removed with the user, and cleared from
the Activity window. Nothing here goes to the log.

observe() is the whole interface: wrap a turn's event stream in it at the
place the turn runs (Flask/main.py does, for pings and the bridge). A host
can teach it more refusals and reply kinds through WAITING and REPLY_STATUS.
"""

import json
import os
import re
import threading
import time

from src.users import STATIC_DIR

KEEP = 200
DAYS = 60
FILE = "activity.json"
SUMMARY_MAX = 600
ASK_MAX = 240

# Tool results that mean the turn wanted something it couldn't get, as
# (pattern, status, what it wanted). Status "needs_you" is an approval
# nobody could give; "switched_off" something the user has turned off.
# A host appends its own (a permissions gate, say), with `what` taken from
# the pattern's first group when it has one.
WAITING = [
    (re.compile(r"^The command was NOT run: it needs the user's approval"), "needs_you", "run a command"),
]

# Replies that mean the turn ended for a reason of its own, as
# (pattern, status): a host's "out of credits", say.
REPLY_STATUS = []

# Where a turn came from, as the window labels it.
SOURCES = ("schedule", "discord", "teams", "email", "bridge")

_write_lock = threading.Lock()


def path_for(user):
    """activity.json in `user`'s folder, beside files/ and conversations/."""
    return os.path.join(STATIC_DIR, user, FILE)


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def clear(path):
    with _write_lock:
        try:
            os.remove(path)
        except OSError:
            pass


def _append(path, entry):
    cutoff = time.time() - DAYS * 86400
    with _write_lock:
        entries = [e for e in read(path) if (e.get("ts") or 0) >= cutoff]
        entries = (entries + [entry])[-KEEP:]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f)
        os.replace(tmp, path)


def clean_ask(text):
    """(title, ask) for a turn's message. A ping loses its "PING [due …]:"
    header; one whose note starts "Name: …." (a routine, say) is titled
    by that name."""
    text = " ".join(str(text or "").split())
    m = re.match(r"^PING \[[^\]]*\]:\s*", text)
    if m:
        text = text[m.end():]
    if text.startswith("(retry) "):
        text = text[len("(retry) "):]
    title = ""
    m = re.match(r"^Routine: ([^.]{1,80})\.\s*", text)
    if m:
        title, text = m.group(1), text[m.end():]
    return title, text[:ASK_MAX]


# Composio's tools say which app they reach in their slugs.
_APP_NAMES = {"GMAIL": "Gmail", "GOOGLECALENDAR": "Google Calendar", "OUTLOOK": "Outlook",
              "GOOGLEDRIVE": "Google Drive", "GOOGLEDOCS": "Google Docs", "GOOGLESHEETS": "Google Sheets",
              "SLACK": "Slack", "NOTION": "Notion", "GITHUB": "GitHub", "LINEAR": "Linear",
              "TRELLO": "Trello", "ASANA": "Asana", "HUBSPOT": "HubSpot", "DISCORD": "Discord",
              "YOUTUBE": "YouTube", "TODOIST": "Todoist", "DROPBOX": "Dropbox", "CALENDLY": "Calendly"}
_BUILTIN_APPS = {"web_search": "Web", "open_url": "Web", "browser_do": "Browser",
                 "add_ping": "Scheduled tasks", "cancel_ping": "Scheduled tasks",
                 "create_file": "Files", "write_file": "Files", "edit_file": "Files"}


def app_of(name, args, server=None):
    """The app a tool call used, as a person would name it, or None.
    `server` is the MCP server's name when the tool is one of its."""
    n = (name or "").lower()
    if "browser" in n:
        return "Browser"
    if server:
        if "composio" in server.lower():
            if "multi_execute" not in n:
                return None                 # looking for tools, not using an app
            slugs = [t.get("tool_slug", "") for t in ((args or {}).get("tools") or [])
                     if isinstance(t, dict)]
            apps = {_APP_NAMES.get(s.split("_", 1)[0].upper(), s.split("_", 1)[0].title())
                    for s in slugs if s}
            return ", ".join(sorted(apps)) or None
        return server
    return _BUILTIN_APPS.get(n)


def observe(events, path, source, ask, get_messages, title="", server_of=None):
    """Pass a turn's `events` through unchanged, and when the turn ends add
    its entry to `path`. `get_messages` reads the conversation after the
    turn (its reply is the summary); `server_of(name)` names the MCP server
    behind a tool, if any. The turn's own errors still raise: the entry just
    says it didn't finish."""
    started = time.time()
    clean_title, clean = clean_ask(ask)
    title = title or clean_title
    try:
        before = len(get_messages() or [])
    except Exception:                                    # noqa: BLE001
        before = 0
    apps, steps, waiting, statuses = [], 0, [], []
    status = "done"
    try:
        for event in events:
            if isinstance(event, dict):
                kind = event.get("type")
                if kind == "tool_call":
                    steps += 1
                    name = event.get("name")
                    try:
                        server = server_of(name) if server_of else None
                    except Exception:                    # noqa: BLE001
                        server = None
                    app = app_of(name, event.get("arguments"), server)
                    if app and app not in apps:
                        apps.append(app)
                elif kind == "tool_result":
                    result = str(event.get("result") or "")
                    for pattern, why, what in WAITING:
                        m = pattern.match(result)
                        if m:
                            waiting.append((m.group(1) if m.groups() else what).strip())
                            statuses.append(why)
                            break
                elif kind == "stopped":
                    status = "stopped"
            yield event
    except GeneratorExit:
        status = "stopped"
        raise
    except Exception:
        status = "failed"
        raise
    finally:
        try:
            summary = ""
            for message in reversed((get_messages() or [])[before:]):
                content = message.get("content")
                if message.get("role") == "assistant" and isinstance(content, str) and content.strip():
                    summary = content.strip()
                    break
            if status == "done":
                for pattern, why in REPLY_STATUS:
                    if pattern.match(summary):
                        status = why
                        break
                else:
                    if "needs_you" in statuses:
                        status = "needs_you"
                    elif statuses:
                        status = statuses[0]
            _append(path, {
                "ts": started, "took": round(time.time() - started, 1),
                "source": source if source in SOURCES else "bridge",
                "title": title or "", "ask": clean, "status": status,
                "waiting": waiting[:5], "apps": apps[:8], "steps": steps,
                "summary": summary[:SUMMARY_MAX] + ("…" if len(summary) > SUMMARY_MAX else ""),
            })
        except Exception:                                # noqa: BLE001 — never the turn's problem
            pass
