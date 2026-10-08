"""Handing the agent's page to the user when it hits a CAPTCHA.

A CAPTCHA is a wall the agent must not climb itself, and unlike a login it
usually sits in the middle of something: a session the site has set up, a form
part-way through, a challenge that only this browser is being shown. So the
user is given *the agent's own browser*, on that page — the Browser app takes
control of it (src/browser_live.py) — rather than a fresh one.

The round trip:

  1. The agent calls `request_captcha_help` (src/browser_server.py). The
     child dumps its context's storage_state to a private temp file
     (`write_state_file`) and returns where it is: {url, title, state_file}.
  2. agent.py intercepts that result (`accept`): the dump becomes the user's
     saved logins (src/browser_profiles.py) — a superset of what was saved,
     since the agent's browser started from them — and handoff.json records
     the page and why. The model is told to end its turn, and the chat draws a
     button.
  3. The button opens /browser?handoff=1. Flask (`take`) consumes handoff.json
     and the page takes control of the agent's browser, still on the page with
     the check. If that browser closed meanwhile, the page reopens it at the
     handed-over address — and because step 2 saved its session, the site
     still sees the same visitor.
  4. The user solves the check and hands back. The agent's browser is where
     they left it, so the agent carries on from there.

Why a temp file in step 1 rather than the state in the tool result: a result
is text that gets logged, streamed to the page and stored in the conversation
by anything along the way that doesn't intercept it. Cookies are live
credentials; a temp file's path is not.

The child half of this module is imported inside the MCP child, so nothing
here may write to stdout, and browser_profiles (which sets up logging) is only
imported by the functions the Flask process calls.
"""

import json
import os
import tempfile
import time

TOOL_NAME = "request_captcha_help"

HANDOFF_FILENAME = "handoff.json"

_TEMP_PREFIX = "fc-handoff-"

# A handoff nobody opened within this long is about a page the site has long
# since expired; opening it would only show the user a stale error.
MAX_AGE = 60 * 60


def _web_url(url):
    return url if isinstance(url, str) and url.lower().startswith(("http://", "https://")) else None


def _write_private(path, data):
    """JSON to `path`, readable by this account only."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _take_file(path):
    """A one-shot file's contents, removed as it's read. None if it's absent,
    unreadable or older than MAX_AGE."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        fresh = time.time() - float(data.get("at")) <= MAX_AGE
    except (AttributeError, TypeError, ValueError):
        return None
    return data if fresh else None


def _profile_dir(user, create=False):
    import src.browser_profiles as profiles

    path = profiles.ensure_dir(user) if create else profiles.state_path(user)
    return os.path.dirname(path) if path else None


# ── the MCP child ────────────────────────────────────────────

def write_state_file(state):
    """Step 1's dump: `state` to a private temp file, and its path. mkstemp
    gives 0600 and a name nobody can guess; the reader removes it whatever
    happens."""
    fd, path = tempfile.mkstemp(prefix=_TEMP_PREFIX, suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
    return path


# ── the Flask process ────────────────────────────────────────

def read_state_file(state_file):
    """(state, None) from a dump the child made with write_state_file, which
    is removed; (None, message) otherwise. Only ever such a file: this path
    comes up the pipe and is about to be read and deleted, so it mustn't be
    steerable anywhere else."""
    state_file = str(state_file or "")
    if (os.path.dirname(os.path.realpath(state_file)) != os.path.realpath(tempfile.gettempdir())
            or not os.path.basename(state_file).startswith(_TEMP_PREFIX)):
        return None, "Error: the browser returned an unexpected handoff."
    try:
        with open(state_file, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"Error: couldn't read the browser's session ({e})."
    finally:
        try:
            os.remove(state_file)
        except OSError:
            pass
    if not isinstance(state, dict):
        return None, "Error: the browser returned an unexpected handoff."
    return state, None


def accept(user, result, reason=""):
    """Step 2: keep the agent's session as `user`'s saved logins, and note the
    page. Returns (url, None), or (None, message for the model)."""
    import src.browser_profiles as profiles

    try:
        info = json.loads(str(result))
    except ValueError:
        return None, str(result)                  # the child's own explanation
    if not isinstance(info, dict):
        return None, "Error: the browser returned an unexpected handoff."
    state, problem = read_state_file(info.get("state_file"))
    if problem:
        return None, problem
    url = _web_url(info.get("url"))
    directory = _profile_dir(user, create=True)
    if not url or not directory:
        return None, "Error: there's no page or no FreeClaw user to hand it to."
    profiles.write_state(user, state)
    _write_private(os.path.join(directory, HANDOFF_FILENAME), {
        "url": url, "title": info.get("title") or "", "reason": reason, "at": time.time(),
    })
    return url, None


def take(user):
    """Step 3: the handoff waiting for `user`, consumed — {url, title, reason}
    — or None."""
    directory = _profile_dir(user)
    data = _take_file(os.path.join(directory, HANDOFF_FILENAME)) if directory else None
    if not data or not _web_url(data.get("url")):
        return None
    return {"url": data["url"], "title": data.get("title") or "", "reason": data.get("reason") or ""}
