"""The browser the human drives, so the agent can reach sites behind a login.

FreeClaw runs headless on a machine the user may never see the screen of — a
VPS, or a container on their Mac. The agent's browser therefore has no way to
show a login form to anybody, and any site requiring sign-in is simply a wall.

This module puts a second, *separate* Chromium behind FreeClaw's web UI. The
user opens /browser-login, sees the page rendered as a stream of screenshots,
clicks and types into it, and signs in. On "Done", the context's cookies and
localStorage are written to that user's storage_state (src/browser_profiles.py)
and the browser closes. The agent's next tool call spawns an MCP child that
loads them (src/browser_mcp_shim.py).

Two decisions worth knowing about:

**Screenshots, not a remote desktop.** The obvious build is Xvfb + x11vnc +
noVNC, or a CDP screencast; both are more code, and one of them is an
unauthenticated remote desktop that must never be allowed near a public
interface. `page.screenshot()` polled a few times a second is worse to *use*
and hugely better to reason about: it's an image behind the same session check
as every other route, on the port FreeClaw already listens on, with no second
service and no new dependency. For the job — click a field, type a password,
press a button — the latency doesn't matter.

**Headful under Xvfb.** Google and Microsoft sign-in refuse browsers they can
tell are automated, and that's the exact thing this feature exists to do. A
real (non-headless) Chromium on a virtual display gets past that where
`headless=True` does not. Where Xvfb isn't installed we fall back to headless
rather than failing outright — most sites are fine with it — and `status()`
says so, because "the password was right and it still wouldn't log in" is a
miserable thing to debug without the hint.

Playwright's **sync** API is used deliberately. Each session owns one thread,
that thread owns the browser, and Flask talks to it through a queue — so no
part of Flask ever touches a playwright object, which is what would otherwise
make this a threading problem. The sync API refuses to run inside a live
asyncio loop, and a plain worker thread has none.
"""

import os
import queue
import shutil
import subprocess
import sys
import threading
import time

import src.browser_profiles as profiles
from src.browser_mcp_shim import VIEWPORT, context_kwargs
from src.logging_setup import get_logger

logger = get_logger(__name__)

# How often the worker grabs a fresh frame when nothing is happening. Fast
# enough that typing feels attached to the page, slow enough that an idle
# session isn't screenshotting a browser 30 times a second for no reason.
FRAME_INTERVAL = 0.35
# A command (click/type) makes the page change, so grab a frame promptly after
# one instead of waiting out the full interval.
POST_COMMAND_DELAY = 0.12

# Signing in involves reading email for a code, finding a phone, giving up and
# starting again. This is generous on purpose; the ceiling below is the real
# stop. Measured from the last thing the user did, not from the start.
IDLE_TIMEOUT = 15 * 60
# Nothing holds a browser open longer than this, however active it looks.
MAX_SESSION = 60 * 60

# JPEG rather than PNG: a screenshot of a text-heavy page is several hundred KB
# as PNG and a tenth of that as JPEG, and this is re-sent a few times a second.
FRAME_QUALITY = 60

# The virtual display we start when there isn't one. :99 by convention.
XVFB_DISPLAY = ":99"

_sessions = {}                    # FreeClaw user -> TakeoverSession
_registry_lock = threading.Lock()

_xvfb_proc = None
_xvfb_lock = threading.Lock()


# ── virtual display ──────────────────────────────────────────

def ensure_display():
    """Make a display available for a headful browser. Returns (ok, note).

    `ok` False means callers should launch headless and tell the user why.
    """
    if sys.platform == "darwin":
        # A native macOS install has a real window server. (The Docker install
        # is Linux inside the container and takes the branch below.)
        return True, ""
    if (os.environ.get("DISPLAY") or "").strip():
        return True, ""
    if not shutil.which("Xvfb"):
        return False, (
            "Xvfb isn't installed, so the sign-in browser is running headless. "
            "Most sites are fine with that, but Google and Microsoft sign-in "
            "will refuse it. Install it with:  apt-get install -y xvfb"
        )

    global _xvfb_proc
    with _xvfb_lock:
        if _xvfb_proc is not None and _xvfb_proc.poll() is None:
            os.environ["DISPLAY"] = XVFB_DISPLAY
            return True, ""
        try:
            _xvfb_proc = subprocess.Popen(
                ["Xvfb", XVFB_DISPLAY, "-screen", "0",
                 f"{VIEWPORT['width']}x{VIEWPORT['height']}x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            logger.exception("Couldn't start Xvfb")
            return False, f"Couldn't start the virtual display ({e}); running headless."
        # Xvfb takes a moment to create the socket, and a browser that launches
        # into a display that isn't listening yet fails with a confusing error.
        for _ in range(50):
            if _xvfb_proc.poll() is not None:
                return False, "The virtual display exited immediately; running headless."
            if os.path.exists(f"/tmp/.X11-unix/X{XVFB_DISPLAY.lstrip(':')}"):
                break
            time.sleep(0.1)
        os.environ["DISPLAY"] = XVFB_DISPLAY
        logger.info("Started Xvfb on %s for the sign-in browser", XVFB_DISPLAY)
        return True, ""


# ── one user's sign-in browser ───────────────────────────────

class TakeoverSession:
    """A headful browser owned by one worker thread, driven through a queue."""

    def __init__(self, user, url):
        self.user = user
        self.start_url = url
        self.started_at = time.time()
        self.touched_at = time.time()

        self._commands = queue.Queue()
        self._frame = None            # latest JPEG bytes
        self._meta = {"url": "", "title": ""}
        self._state_lock = threading.Lock()

        self.status = "starting"      # starting | running | saving | closed | error
        self.error = ""
        self.note = ""                # e.g. the headless warning
        self.saved = False

        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"takeover-{user}", daemon=True)
        self._thread.start()

    # ── what Flask calls ──

    def touch(self):
        self.touched_at = time.time()

    def send(self, command):
        """Queue one input command. Cheap and non-blocking: the worker applies
        it when it gets there, which keeps a slow page from blocking the HTTP
        request that delivered the click."""
        self.touch()
        self._commands.put(command)

    def frame(self):
        with self._state_lock:
            return self._frame

    def meta(self):
        with self._state_lock:
            return dict(self._meta)

    def finish(self):
        """Ask the worker to save the logins and shut down. Blocks until it
        has, because the caller's next move is to tell the user it's saved —
        and saying so before the file exists would be a lie the agent then
        acts on."""
        self.touch()
        self._commands.put({"kind": "finish"})
        self._done.wait(timeout=30)
        return self.saved

    def cancel(self):
        """Shut down without saving."""
        self._commands.put({"kind": "cancel"})
        self._done.wait(timeout=15)

    def alive(self):
        return self.status in ("starting", "running", "saving")

    # ── the worker thread ──

    def _run(self):
        from playwright.sync_api import sync_playwright

        headful, note = ensure_display()
        self.note = note

        playwright = browser = context = None
        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(headless=not headful)

            # Start from whatever this user is already signed into, so adding a
            # second site doesn't silently drop the first — storage_state is
            # saved whole, not merged, so what we don't load we lose.
            existing = profiles.state_path(self.user)
            state = existing if existing and os.path.exists(existing) else None
            context = browser.new_context(**context_kwargs(browser, state))

            page = context.new_page()
            self._active = page
            # An OAuth flow routinely opens a second window; without this the
            # user would be staring at the opener while the real form is on a
            # page they can't see or reach. Registered after `_active` is set,
            # so a popup arriving mid-setup isn't immediately overwritten.
            context.on("page", self._on_new_page)

            page.goto(self.start_url, wait_until="domcontentloaded", timeout=45000)
            self.status = "running"
            self._loop(context)
        except Exception as e:                    # noqa: BLE001 — reported to the UI
            logger.exception("Sign-in browser failed for %r", self.user)
            self.status = "error"
            self.error = str(e)
        finally:
            # Ordered innermost-out. Each is best-effort: a browser that
            # already died makes close() raise, and that must not stop the
            # driver being stopped or the session being deregistered below.
            for target, method in ((context, "close"), (browser, "close"),
                                   (playwright, "stop")):
                if target is None:
                    continue
                try:
                    getattr(target, method)()
                except Exception:                 # noqa: BLE001 — teardown
                    pass
            if self.status != "error":
                self.status = "closed"
            self._done.set()
            with _registry_lock:
                if _sessions.get(self.user) is self:
                    _sessions.pop(self.user, None)

    def _on_new_page(self, page):
        """Follow popups. The newest page is almost always the one the user
        needs to interact with."""
        self._active = page

    def _current_page(self):
        """The page to screenshot and send input to, skipping any that closed
        under us (an OAuth popup closes itself on success)."""
        page = getattr(self, "_active", None)
        if page is not None:
            try:
                if not page.is_closed():
                    return page
            except Exception:                     # noqa: BLE001
                pass
        # Fall back to the last surviving page in the context.
        try:
            pages = [p for p in page.context.pages if not p.is_closed()] if page else []
        except Exception:                         # noqa: BLE001
            pages = []
        self._active = pages[-1] if pages else None
        return self._active

    def _loop(self, context):
        while True:
            now = time.time()
            if now - self.touched_at > IDLE_TIMEOUT:
                logger.info("Sign-in browser for %r idle; saving and closing", self.user)
                self._save(context)
                return
            if now - self.started_at > MAX_SESSION:
                logger.info("Sign-in browser for %r hit the time limit; saving", self.user)
                self._save(context)
                return

            acted = False
            try:
                command = self._commands.get(timeout=FRAME_INTERVAL)
            except queue.Empty:
                command = None

            if command is not None:
                kind = command.get("kind")
                if kind == "finish":
                    self._save(context)
                    return
                if kind == "cancel":
                    self.status = "closed"
                    return
                self._apply(command)
                acted = True
                # Drain anything queued behind it — a burst of keystrokes
                # should replay in order without a frame between each.
                while True:
                    try:
                        extra = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    if extra.get("kind") in ("finish", "cancel"):
                        self._commands.put(extra)
                        break
                    self._apply(extra)

            if acted:
                time.sleep(POST_COMMAND_DELAY)
            self._capture()

    def _apply(self, command):
        """One input command against the active page. Every failure here is
        swallowed: a click that lands while the page is navigating raises, and
        killing the whole sign-in session over it would be absurd."""
        page = self._current_page()
        if page is None:
            return
        kind = command.get("kind")
        try:
            if kind == "click":
                page.mouse.click(
                    float(command.get("x", 0)), float(command.get("y", 0)),
                    button=command.get("button") or "left",
                    click_count=int(command.get("clicks") or 1),
                )
            elif kind == "text":
                # `insert_text` rather than `type`: this is the browser's own
                # composed input, so accents and non-Latin scripts arrive
                # intact instead of as a stream of synthetic keydowns.
                page.keyboard.insert_text(command.get("text") or "")
            elif kind == "key":
                page.keyboard.press(command.get("key") or "")
            elif kind == "scroll":
                page.mouse.wheel(0, float(command.get("dy") or 0))
            elif kind == "nav":
                page.goto(command.get("url") or "", wait_until="domcontentloaded",
                          timeout=45000)
            elif kind == "back":
                page.go_back(wait_until="domcontentloaded", timeout=30000)
            elif kind == "reload":
                page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception as e:                    # noqa: BLE001 — see docstring
            logger.debug("Sign-in input %r failed: %s", kind, e)

    def _capture(self):
        page = self._current_page()
        if page is None:
            return
        try:
            shot = page.screenshot(type="jpeg", quality=FRAME_QUALITY)
            meta = {"url": page.url, "title": page.title()}
        except Exception:                         # noqa: BLE001
            # Mid-navigation the page has no renderer to screenshot. Keeping
            # the previous frame is better than blanking the user's view.
            return
        with self._state_lock:
            self._frame = shot
            self._meta = meta

    def _save(self, context):
        self.status = "saving"
        path = profiles.ensure_dir(self.user)
        if not path:
            self.status = "error"
            self.error = "Couldn't work out where to save this user's logins."
            return
        try:
            context.storage_state(path=path)
            os.chmod(path, 0o600)
            self.saved = True
            logger.info("Saved browser logins for %r (%s)", self.user,
                        ", ".join(profiles.domains(self.user)) or "no cookies")
        except OSError:
            logger.exception("Couldn't save browser logins for %r", self.user)
            self.status = "error"
            self.error = "Couldn't write the saved logins to disk."
            return
        except Exception as e:                    # noqa: BLE001 — reported to the UI
            logger.exception("Couldn't capture storage state for %r", self.user)
            self.status = "error"
            self.error = f"Couldn't capture the browser session: {e}"
            return
        self.status = "closed"


# ── module-level API used by Flask ───────────────────────────

def start(user, url):
    """Open a sign-in browser for `user` at `url`, replacing any existing one.

    One session per user on purpose: two would race on the same storage_state
    file, and the second to save would quietly drop the first's login."""
    if not profiles.state_path(user):
        raise ValueError("That user name can't have a browser profile.")
    with _registry_lock:
        existing = _sessions.get(user)
        if existing is not None and existing.alive():
            existing.cancel()
        session = TakeoverSession(user, url)
        _sessions[user] = session
    return session


def get(user):
    with _registry_lock:
        return _sessions.get(user)


def status(user):
    """What the page polls to decide what to render."""
    session = get(user)
    if session is None or not session.alive():
        return {
            "running": False,
            "saved_domains": profiles.domains(user),
        }
    meta = session.meta()
    return {
        "running": True,
        "status": session.status,
        "url": meta.get("url", ""),
        "title": meta.get("title", ""),
        "note": session.note,
        "error": session.error,
        "viewport": dict(VIEWPORT),
        "saved_domains": profiles.domains(user),
    }


def shutdown_all():
    """Close every open sign-in browser. Registered with atexit by Flask/main.py
    so a restart from Settings doesn't strand a Chromium holding a display."""
    with _registry_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        try:
            session.cancel()
        except Exception:                         # noqa: BLE001 — shutdown
            pass
    global _xvfb_proc
    with _xvfb_lock:
        if _xvfb_proc is not None and _xvfb_proc.poll() is None:
            _xvfb_proc.terminate()
        _xvfb_proc = None
