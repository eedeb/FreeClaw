"""Handing the agent's page to the user when it hits a CAPTCHA.

A CAPTCHA is a wall the agent must not climb itself, and unlike a login it
usually sits in the middle of something: a session the site has set up, a form
part-way through, a challenge that only this browser is being shown. Opening
the address fresh in the sign-in browser would lose all of that, so the user
gets the agent's page *with the agent's cookies*.

The round trip:

  1. The agent calls `request_captcha_help` (registered below, in the MCP
     child). The child dumps its context's storage_state to a private temp
     file and returns where it is: {url, title, state_file}.
  2. agent.py intercepts that result (`accept`): the dump moves into the
     user's profile directory as handoff.json, the model is told to end its
     turn, and the chat draws a button.
  3. The button opens /browser?handoff=1. Flask (`take`) loads and deletes
     handoff.json, and the sign-in browser (src/browser_takeover.py) adds the
     agent's cookies and opens its page — in the browser already open, if
     there is one, so nothing unsaved there is lost.
  4. The user solves the check and saves. That writes auth.json as always,
     plus resume.json: the address they finished on (`write_resume`).
  5. Saving drops the agent's browser child (Flask/main.py). The next one
     loads auth.json, and its first `snapshot` — which on a fresh child would
     otherwise fail with "call navigate first" — opens resume.json's address
     instead, so the agent carries on from the page the user left.

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
RESUME_FILENAME = "resume.json"

_TEMP_PREFIX = "fc-handoff-"

# A handoff nobody opened within this long is about a page the site has long
# since expired; opening it would only show the user a stale error.
MAX_AGE = 60 * 60


def _web_url(url):
    return url if isinstance(url, str) and url.lower().startswith(("http://", "https://")) else None


def _write_private(path, data):
    """JSON to `path`, readable by this account only — it holds cookies."""
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

def take_resume(state_path):
    """The address a finished handoff left off at, or None. `state_path` is
    the child's FC_BROWSER_STORAGE_STATE."""
    if not state_path:
        return None
    data = _take_file(os.path.join(os.path.dirname(state_path), RESUME_FILENAME))
    return _web_url((data or {}).get("url"))


def register(mcp, sw):
    """Add `request_captcha_help` to the server shadow-web built, and let a
    fresh child's first `snapshot` pick up where a handoff left off."""
    session = sw._session
    manager = mcp._tool_manager

    @mcp.tool()
    async def request_captcha_help(reason: str = ""):
        """Hand the current page to the user to solve a CAPTCHA or human check. End your turn after.
        reason: shown to them."""
        # `reason` is read by agent.py, which owns the conversation with the user.
        await sw._ensure_browser()
        page = session["page"]
        url = _web_url(page.url)
        if not url:
            return "Nothing to hand over: open the page with the check first."
        state = await session["context"].storage_state()
        try:
            title = await page.title()
        except Exception:                         # noqa: BLE001 — cosmetic
            title = ""
        # mkstemp: 0600, a name nobody can guess, and nothing to clean up
        # here — accept() removes it whatever happens.
        fd, path = tempfile.mkstemp(prefix=_TEMP_PREFIX, suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        return json.dumps({"url": url, "title": title, "state_file": path})

    upstream, navigate = manager.get_tool("snapshot"), manager.get_tool("navigate")
    if upstream is None or navigate is None:
        return

    async def snapshot(diff: bool = False, detail: str = "terse") -> dict:
        if "shadow_page" not in session:
            url = take_resume(os.environ.get("FC_BROWSER_STORAGE_STATE"))
            if url:
                return await navigate.fn(url=url, detail=detail)
        return await upstream.fn(diff=diff, detail=detail)

    mcp.remove_tool("snapshot")
    mcp.add_tool(snapshot, name="snapshot", description=upstream.description)


# ── the Flask process ────────────────────────────────────────

def accept(user, result, reason=""):
    """Step 2: file the child's dump under `user`. Returns (url, None), or
    (None, message for the model)."""
    try:
        info = json.loads(str(result))
    except ValueError:
        return None, str(result)                  # the child's own explanation
    if not isinstance(info, dict):
        return None, "Error: the browser returned an unexpected handoff."
    state_file = info.get("state_file") or ""
    # Only ever a file the child made with mkstemp: this path is about to be
    # read and deleted, so it mustn't be steerable anywhere else.
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
    url = _web_url(info.get("url"))
    directory = _profile_dir(user, create=True)
    if not url or not directory:
        return None, "Error: there's no page or no FreeClaw user to hand it to."
    _write_private(os.path.join(directory, HANDOFF_FILENAME), {
        "url": url, "title": info.get("title") or "", "reason": reason,
        "cookies": state.get("cookies") or [], "at": time.time(),
    })
    return url, None


def take(user):
    """Step 3: the handoff waiting for `user`, consumed — {url, title, reason,
    cookies} — or None."""
    directory = _profile_dir(user)
    data = _take_file(os.path.join(directory, HANDOFF_FILENAME)) if directory else None
    if not data or not _web_url(data.get("url")):
        return None
    return data


def write_resume(user, url):
    """Step 4: where the user left the handed-over page."""
    url = _web_url(url)
    directory = _profile_dir(user)
    if url and directory and os.path.isdir(directory):
        _write_private(os.path.join(directory, RESUME_FILENAME), {"url": url, "at": time.time()})
