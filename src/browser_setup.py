"""Chromium bootstrap for the shipped `shadow-web` MCP server.

FreeClaw installs the `shadow-web` Python package like any other dependency —
it's a couple of hundred kilobytes. The browser it drives is not: Chromium is a
few hundred megabytes on disk plus a set of system libraries, which is most of
what a FreeClaw install weighs today. Downloading that for every user, when the
server ships switched off, would make the common install pay for a feature it
never turns on.

So the browser is fetched the first time someone enables the server in
Settings, not at install time. `start()` kicks that off in a background thread
and returns immediately; the Settings page polls `state()` and shows what's
happening. Nothing else in FreeClaw imports playwright, so a user who never
enables the server never loads it into the web process either.

Everything here runs `sys.executable -m playwright …` in a child rather than
importing playwright in-process, for the same reason: the Flask worker's memory
footprint shouldn't grow because a feature *could* be switched on.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import threading

from src.logging_setup import get_logger

logger = get_logger(__name__)

# Statuses reported to the Settings UI.
READY = "ready"          # browser present and it launches
MISSING = "missing"      # never fetched
INSTALLING = "installing"
ERROR = "error"          # fetched but unusable, or the fetch failed

# A cold Chromium download over a slow VPS link is genuinely slow, and killing
# it half way leaves a partial extract the next attempt has to redo.
DOWNLOAD_TIMEOUT = 1800
# The smoke test either launches in a couple of seconds or is never going to.
SMOKE_TEST_TIMEOUT = 120
# A few MB from a mirror that apt has already refreshed.
XVFB_TIMEOUT = 300

_lock = threading.Lock()
_state = {"status": None, "message": ""}


def _browsers_dir():
    """Where playwright keeps its browser builds. PLAYWRIGHT_BROWSERS_PATH wins
    when set, which is how the container keeps them on a mounted volume instead
    of in a layer that `update-mac.sh` throws away (see docker-compose.yml)."""
    override = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    # "0" is playwright's "put them next to the package", not a path.
    if override and override != "0":
        return override
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Caches/ms-playwright")
    return os.path.expanduser("~/.cache/ms-playwright")


def chromium_present():
    """Whether a Chromium build has been unpacked. A directory check rather
    than a launch, because this is on the path of every Settings page load and
    a real launch costs seconds. `start()` still smoke-tests after installing,
    so "present but broken" is caught once, at the point it can be reported."""
    root = _browsers_dir()
    try:
        entries = os.listdir(root)
    except OSError:
        return False
    return any(
        e.startswith(("chromium-", "chromium_headless_shell-"))
        and os.listdir(os.path.join(root, e))
        for e in entries
        if os.path.isdir(os.path.join(root, e))
    )


def package_available():
    """Whether `shadow_web` is importable. It's pinned to Python 3.10+ in
    requirements.txt, so a FreeClaw running on 3.9 has everything else but not
    this. `find_spec` locates the package without executing it, which keeps
    playwright out of the web process."""
    try:
        return importlib.util.find_spec("shadow_web") is not None
    except (ImportError, ValueError):
        return False


def state():
    """Current status as `{"status": …, "message": …}`.

    Nothing has to have called `start()` first: with no install in flight this
    answers from what's on disk, so a restart mid-download reports `missing`
    and the next enable picks it up again rather than reporting `installing`
    forever against a thread that no longer exists."""
    if not package_available():
        return {"status": ERROR,
                "message": "The shadow-web package isn't installed — it needs Python 3.10 "
                           f"or newer, and FreeClaw is running on "
                           f"{sys.version_info.major}.{sys.version_info.minor}."}
    with _lock:
        if _state["status"] in (INSTALLING, ERROR):
            return dict(_state)
    return {"status": READY if chromium_present() else MISSING, "message": ""}


def _run(args, timeout):
    """A playwright CLI call. Returns (ok, output)."""
    proc = subprocess.run(
        [sys.executable, "-m", "playwright", *args],
        capture_output=True, text=True, timeout=timeout,
    )
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, out


def _smoke_test():
    """Launch Chromium once and close it. The download succeeding doesn't mean
    it runs: on a slim base image the binary is there and the shared libraries
    it needs are not, which otherwise surfaces much later as a failed tool call
    in the middle of a conversation."""
    script = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p:\n"
        "    p.chromium.launch(headless=True).close()\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=SMOKE_TEST_TIMEOUT,
    )
    return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _missing_deps_hint(output):
    """Turn playwright's "host system is missing dependencies" wall of text into
    the one command that fixes it. Needs root, which the FreeClaw process
    doesn't have on a Linux install, so this is something to tell the user
    rather than something to run."""
    lowered = (output or "").lower()
    if "missing dependencies" in lowered or "error while loading shared libraries" in lowered:
        # The full interpreter path, not its basename: on a Linux install this
        # is the venv's python, and plain `sudo python` would run the system
        # one, which has no playwright and fails confusingly.
        return ("Chromium downloaded but won't start — the machine is missing its system "
                f"libraries. Run:  sudo {sys.executable} -m playwright install-deps chromium")
    return None


def _install_args():
    """`playwright install` plus `--with-deps` where we can actually use it.

    Chromium needs a set of system libraries that a slim base image doesn't
    carry, and installing those is apt's job, which means root. In the
    container we are root, so fetch them here and keep them out of the image —
    baking them into every build would charge each install for a server that
    ships switched off. On a Linux install FreeClaw runs as an ordinary user,
    so the libraries stay the user's job and `_missing_deps_hint` tells them
    the command if the smoke test proves they're absent."""
    root = hasattr(os, "geteuid") and os.geteuid() == 0
    return ["install", "--with-deps", "chromium"] if root else ["install", "chromium"]


def _install_xvfb():
    """A virtual display for the human sign-in browser (src/browser_takeover.py).

    Only the sign-in browser needs it: the agent's own browser is headless, but
    Google and Microsoft refuse to *sign in* a browser they can tell is
    headless, which is the one thing that flow exists to do. A few MB of apt,
    and like the Chromium libraries above it needs root — so in the container
    we fetch it and on a Linux install we don't, and browser_takeover falls
    back to headless with a message saying which command fixes it.

    Best effort throughout: failing here costs the user Google sign-in, not
    their browser tools, so it must not turn a working install into an error."""
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        return
    if shutil.which("Xvfb"):
        return
    try:
        subprocess.run(["apt-get", "install", "-y", "--no-install-recommends", "xvfb"],
                       capture_output=True, text=True, timeout=XVFB_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("Couldn't install Xvfb; the sign-in browser will run headless")
        return
    if shutil.which("Xvfb"):
        logger.info("Xvfb installed for the sign-in browser")
    else:
        logger.warning("Xvfb still absent after apt-get; the sign-in browser will run headless")


def _install():
    try:
        ok, out = _run(_install_args(), DOWNLOAD_TIMEOUT)
        if not ok:
            _finish(ERROR, f"Couldn't download Chromium: {out[-400:] or 'unknown error'}")
            return
        # After the Chromium install, which is what runs `apt-get update`.
        _install_xvfb()
        ok, out = _smoke_test()
        if not ok:
            _finish(ERROR, _missing_deps_hint(out)
                    or f"Chromium downloaded but won't start: {out[-400:] or 'unknown error'}")
            return
        _finish(READY, "")
    except subprocess.TimeoutExpired:
        _finish(ERROR, "Timed out downloading Chromium. Enable the server again to retry.")
    except Exception as e:                       # noqa: BLE001 — reported, not raised
        logger.exception("Chromium install failed")
        _finish(ERROR, f"Couldn't install Chromium: {e}")


def _finish(status, message):
    with _lock:
        _state["status"] = status
        _state["message"] = message
    if status == READY:
        logger.info("Chromium ready for the shadow-web MCP server")
    else:
        logger.warning("Chromium setup finished as %s: %s", status, message)
    # The server's tools couldn't be listed while the browser was missing, so
    # the agent's tool list is currently short one server. Rebuild it now the
    # answer has changed. Imported here because agent.py imports this module's
    # package at startup and a top-level import would be circular.
    try:
        import src.agent as agent
        agent.refresh_tools()
    except Exception:
        logger.exception("Couldn't refresh tools after Chromium setup")


def start():
    """Ensure Chromium is on disk, downloading it in the background if not.
    Returns the state to report right now — `ready` when there was nothing to
    do, `installing` when a download is under way."""
    # No point spending a few hundred MB on a browser for a server that can't
    # start regardless.
    if not package_available():
        return state()
    with _lock:
        if _state["status"] == INSTALLING:
            return dict(_state)
        if chromium_present():
            _state["status"] = None       # fall back to the on-disk check
            _state["message"] = ""
            # Not inside _install(): an install that already has Chromium never
            # reaches it, which would leave every existing FreeClaw without the
            # virtual display the sign-in browser wants. Backgrounded because
            # this is on the Settings request path, and it's a no-op when Xvfb
            # is already there or we aren't root.
            threading.Thread(target=_install_xvfb, name="xvfb-install",
                             daemon=True).start()
            return {"status": READY, "message": ""}
        _state["status"] = INSTALLING
        _state["message"] = "Downloading Chromium (a few hundred MB) — this runs once."
        snapshot = dict(_state)
    threading.Thread(target=_install, name="chromium-install", daemon=True).start()
    return snapshot
