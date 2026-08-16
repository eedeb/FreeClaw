"""Where a FreeClaw user's browser logins live.

The agent's browser starts from a blank slate on every spawn — shadow-web's MCP
server builds an in-memory context and throws it away with the process — so any
site behind a login is simply unreachable to it. What fixes that is a
`storage_state`: playwright's JSON dump of a context's cookies and localStorage,
which one browser can write and another can load.

This module owns nothing but the *location* of those files, because the
location is the security decision. Two rules it exists to enforce:

  * **Per FreeClaw user, never shared.** A storage_state is a live credential —
    whoever loads it is signed in as whoever created it. FreeClaw is
    multi-user, so user B's agent must never be handed user A's file.
  * **Outside `Flask/static/`.** That whole tree is reachable over HTTP by any
    logged-in session (Flask/main.py: serve_static), which is fine for chats
    and uploads and is where `.bash_approvals.json` sensibly lives. A cookie
    jar there would be one URL away from every other user of the same install.
    So these sit in a sibling directory that nothing serves.

The trade-off in using storage_state rather than a full profile directory:
cookies and localStorage travel, IndexedDB and service workers don't. Sites
that keep their session token in IndexedDB won't survive the handoff, and the
fix for those is a real `user_data_dir` — which brings Chromium's single-process
profile lock with it, and a much larger change. Cookies cover the common case.
"""

import json
import os
import re

from src.logging_setup import get_logger

logger = get_logger(__name__)

# Repo root, the same way src/approvals.py computes STATIC_ROOT.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(BASE_DIR, ".."))

# Deliberately a sibling of Flask/, not a child of Flask/static/. Bind-mounted
# by docker-compose so logins survive ./update-mac.sh recreating the container.
PROFILES_ROOT = os.environ.get("FC_BROWSER_PROFILES") or os.path.join(
    REPO_ROOT, "browser-profiles"
)

STATE_FILENAME = "auth.json"

# Usernames reach this from the session, which got them from the URL. Anything
# that isn't a plain name can't be allowed to steer the path — `../` in a user
# name would otherwise write a credential file anywhere the process can reach.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_user(user):
    name = (user or "").strip()
    if not name or name in (".", "..") or not _SAFE_NAME.match(name):
        return None
    return name


def state_path(user):
    """Absolute path to `user`'s storage_state file, or None if the name isn't
    one we're willing to build a path out of. The file need not exist — callers
    treat "missing" as "this user has signed into nothing yet"."""
    name = _safe_user(user)
    if name is None:
        logger.warning("Refusing a browser profile path for user name %r", user)
        return None
    return os.path.join(PROFILES_ROOT, name, STATE_FILENAME)


def ensure_dir(user):
    """`user`'s profile directory, created if absent. Returns the state path.

    0700 because the contents are credentials and a Linux install shares the
    machine with whatever else runs on it."""
    path = state_path(user)
    if path is None:
        return None
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        # Best effort — a bind mount may not let us, and failing the login
        # flow over directory permissions helps nobody.
        pass
    return path


def has_state(user):
    """Whether this user has any saved logins at all."""
    path = state_path(user)
    return bool(path) and os.path.exists(path) and os.path.getsize(path) > 0


def domains(user):
    """The domains `user` currently has cookies for, for display in the UI.

    Reads only cookie *names*' domains, never values — this feeds a "you are
    signed into these sites" list, and the whole point of that list is to be
    safe to render."""
    path = state_path(user)
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.exception("Couldn't read the browser profile for %r", user)
        return []
    seen = set()
    for cookie in state.get("cookies") or []:
        domain = (cookie.get("domain") or "").lstrip(".")
        if domain:
            seen.add(domain)
    return sorted(seen)


def clear(user):
    """Delete this user's saved logins. Returns True if a file was removed.

    The counterpart to the warning on the login page: signing the agent into a
    site is a grant, and a grant needs a way to take it back."""
    path = state_path(user)
    if not path or not os.path.exists(path):
        return False
    try:
        os.remove(path)
    except OSError:
        logger.exception("Couldn't clear the browser profile for %r", user)
        return False
    logger.info("Cleared saved browser logins for %r", user)
    return True
