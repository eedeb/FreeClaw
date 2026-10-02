"""The browser the human drives, so the agent can reach sites behind a login.

FreeClaw runs headless on a machine the user may never see the screen of — a
VPS, or a container on their Mac. The agent's browser therefore has no way to
show a login form to anybody, and any site requiring sign-in is simply a wall.

This module puts a second, *separate* Chromium behind FreeClaw's web UI. The
user opens /browser?user=…, sees the page rendered as a stream of screenshots,
clicks and types into it, and signs in. On "Done", the context's cookies and
localStorage are written to that user's storage_state (src/browser_profiles.py)
and the browser closes. The agent's next tool call spawns an MCP child that
loads them (src/browser_mcp_shim.py).

Two decisions worth knowing about:

**A screencast, not a remote desktop.** The obvious build is Xvfb + x11vnc +
noVNC, which is an unauthenticated remote desktop that must never be allowed
near a public interface. Instead Chromium's own CDP screencast hands this
thread a JPEG whenever the page actually repaints, and Flask streams those out
behind the same session check as every other route, on the port FreeClaw
already listens on — no second service, no new dependency. A page that isn't
changing sends nothing, which is what a screenshot polled on a timer couldn't
do: that paid a capture and a download several times a second to show the
same login form. Screenshots remain as the fallback if the screencast can't be
started.

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

import base64
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

import src.browser_handoff as browser_handoff
import src.browser_profiles as profiles
from src.browser_mcp_shim import VIEWPORT, context_kwargs, launch_kwargs
from src.logging_setup import get_logger

logger = get_logger(__name__)

# Sync playwright only delivers events — screencast frames included — while
# this thread is inside a playwright call, so the worker idles in a short
# `wait_for_timeout` rather than blocking on its queue. Also the most a
# queued click waits before it's applied.
PUMP_INTERVAL = 0.03
# Chromium sends the next screencast frame only once the last is acked, so the
# ack is the frame-rate cap: a spinner or a video would otherwise be encoded,
# decoded and shipped at the compositor's 60fps.
MIN_FRAME_GAP = 1 / 20
# How often the url/title in the status bar are refreshed. title() is a round
# trip to the renderer, so it isn't asked on every pump.
META_INTERVAL = 0.5

# Screenshot fallback, for a browser the screencast couldn't be started on.
# Fast enough that typing feels attached to the page, slow enough that an idle
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
# Held only long enough to read or swap an entry — never across a blocking
# wait, because every request that touches a browser needs it (see start()).
_registry_lock = threading.Lock()
# Serialises start() against itself, which is the slow part: tearing an old
# session down and launching a Chromium. Separate from _registry_lock so a
# start in progress doesn't stall the frame and status polls.
_start_lock = threading.Lock()

_xvfb_proc = None
_xvfb_lock = threading.Lock()


# ── virtual display ──────────────────────────────────────────

def ensure_display():
    """Make a display available for a headful browser. Returns (ok, note).

    `ok` False means callers should launch headless and tell the user why.
    """
    if sys.platform == "darwin":
        # macOS has a real window server, and the menu bar app runs in the
        # user's own GUI session — so a headful browser has somewhere to draw.
        return True, ""
    if sys.platform == "win32":
        # Same reasoning as macOS, and the reason windows/tray.py is a tray app
        # rather than a Windows service: a service runs in session 0, which has
        # no desktop, while the tray runs in the interactive session the user is
        # signed into. So a headful Chromium has a real desktop to draw on and
        # there is no virtual display to arrange.
        #
        # Before this branch existed Windows fell through to the Linux path,
        # found no Xvfb — there is no such thing on Windows — and launched the
        # sign-in browser headless while advising the user to run
        # `sudo apt-get install xvfb`. Headless is the one mode this flow
        # cannot use: refusing automated browsers is exactly what Google and
        # Microsoft sign-in do.
        return True, ""
    if (os.environ.get("DISPLAY") or "").strip():
        return True, ""
    if not shutil.which("Xvfb"):
        # install.sh, update.sh and the container image all install this now,
        # so reaching here means an install that predates that and hasn't been
        # updated, or a distro whose package manager none of them knew — hence
        # both the update and the manual command.
        return False, (
            "Xvfb isn't installed, so the sign-in browser is running headless. "
            "Most sites are fine with that, but Google and Microsoft sign-in "
            "will refuse it. Run ./update.sh, or install it yourself with:  "
            "sudo apt-get install -y xvfb"
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

    def __init__(self, user, url, cookies=None):
        self.user = user
        self.start_url = url
        self._start_cookies = cookies
        # Opened on a page the agent handed over (src/browser_handoff.py), so
        # a save also records where the user left it, for the agent to resume.
        self.handoff = cookies is not None
        self.started_at = time.time()
        self.touched_at = time.time()
        self._orphaned = False        # its profile was deleted under it

        self._commands = queue.Queue()
        self._frame = None            # latest JPEG bytes
        self._frame_seq = 0           # bumped per frame, so a stream can wait for the next
        self._meta = {"url": "", "title": ""}
        self._state_lock = threading.Lock()
        self._frame_ready = threading.Condition(self._state_lock)

        # Screencast state, touched only by the worker thread.
        self._cast_ok = True          # False once it's failed; screenshots from then on
        self._cast_page = None        # the page being cast
        self._cdp = None              # its CDP session
        self._pending_ack = None      # (cdp, sessionId) of the frame not yet acked
        self._last_ack_at = 0.0
        self._held_frame = None       # painted before the first page committed
        self._published_at = 0.0      # when the last frame, of either kind, went out

        self.status = "starting"      # starting | running | saving | closed | error
        self.error = ""
        self.note = ""                # e.g. the headless warning
        self.saved = False

        # A navigation blocks this session's worker thread until the page
        # loads, and no frame is captured while it does — so the UI shows a
        # frozen screenshot of the page being *left*, with nothing to say why.
        # These are what status() reports so it can say "Loading…" instead, and
        # name a load that failed rather than looking identical to one that is
        # merely slow.
        self.navigating = False
        self.nav_error = ""

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

    def wait_frame(self, after_seq, timeout):
        """(seq, frame) for the first frame newer than `after_seq`, blocking up
        to `timeout` seconds for one. On a timeout, or once the session ends,
        returns the current pair as-is — the caller compares `seq` to tell."""
        with self._frame_ready:
            self._frame_ready.wait_for(
                lambda: self._frame_seq != after_seq or not self.alive(), timeout)
            return self._frame_seq, self._frame

    def meta(self):
        with self._state_lock:
            return dict(self._meta)

    def save(self):
        """Write the logins out and keep browsing.

        Separate from `finish` because this is a browser, not a wizard: someone
        signs into one site, saves so the agent has it, and carries on to the
        next. Blocks until the file is on disk — the caller's next move is to
        tell the user it's saved, and saying so early would be a lie the agent
        then acts on."""
        self.touch()
        done = threading.Event()
        self._commands.put({"kind": "save", "done": done})
        done.wait(timeout=30)
        return self.saved

    def finish(self):
        """Save and shut down. Blocks, for the same reason as `save`."""
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
        # The profile directory exists from here on, so _save can tell "not
        # created yet" from "deleted while this browser was open".
        profiles.ensure_dir(self.user)

        playwright = browser = context = None
        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(**launch_kwargs(headless=not headful))

            # Start from whatever this user is already signed into, so adding a
            # second site doesn't silently drop the first — storage_state is
            # saved whole, not merged, so what we don't load we lose.
            existing = profiles.state_path(self.user)
            state = existing if existing and os.path.exists(existing) else None
            context = browser.new_context(**context_kwargs(browser, state))
            if self._start_cookies:
                self._add_cookies(context, self._start_cookies)

            page = context.new_page()
            self._active = page
            # An OAuth flow routinely opens a second window; without this the
            # user would be staring at the opener while the real form is on a
            # page they can't see or reach. Registered after `_active` is set,
            # so a popup arriving mid-setup isn't immediately overwritten.
            context.on("page", self._on_new_page)
            self._cast(page)

            # See _wait_until: with the screencast up, navigations return as
            # soon as the response starts, so the user watches the page load.
            self.navigating = True
            try:
                page.goto(self.start_url, wait_until=self._wait_until(), timeout=45000)
            finally:
                self.navigating = False
            self.status = "running"
            if self._held_frame is not None:
                self._publish(self._held_frame)
                self._held_frame = None
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
            # Wake any stream waiting on a frame, so it ends now rather than
            # at its next keepalive.
            with self._frame_ready:
                self._frame_ready.notify_all()
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
        last_meta = last_shot = 0.0
        # When the user last did something the screencast hasn't answered yet;
        # 0 once it has. Starts armed so a page that never paints still shows.
        nudge_at = time.time()
        while True:
            if self._orphaned:
                return
            now = time.time()
            if now - self.touched_at > IDLE_TIMEOUT:
                logger.info("Browser for %r idle; saving and closing", self.user)
                self._save(context, close=True)
                return
            if now - self.started_at > MAX_SESSION:
                logger.info("Browser for %r hit the time limit; saving", self.user)
                self._save(context, close=True)
                return

            # Follows a popup opening, or closing back to its opener.
            page = self._current_page()
            if self._cast_ok and page is not self._cast_page:
                self._cast(page)

            acted = False
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                command = None
                self._pump(page)

            if command is not None:
                kind = command.get("kind")
                if kind == "finish":
                    self._save(context, close=True)
                    return
                if kind == "cancel":
                    self.status = "closed"
                    return
                if kind == "save":
                    self._save(context, close=False)
                    # Always set, even when the save failed — the waiter reads
                    # `saved`/`error` for the outcome, and leaving it unset
                    # would hang the request until its timeout instead.
                    command["done"].set()
                    continue
                # Drain anything queued behind it — a burst of keystrokes
                # should replay in order without a frame between each.
                batch = [command]
                while True:
                    try:
                        extra = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    # Anything with its own control flow or a waiter behind it
                    # goes back on the queue for the main loop to handle —
                    # passing a "save" to _apply would silently drop it and
                    # leave its caller blocked until the timeout.
                    if extra.get("kind") in ("finish", "cancel", "save"):
                        self._commands.put(extra)
                        break
                    batch.append(extra)
                for item in self._compact(batch):
                    self._apply(item)
                acted = True
                nudge_at = time.time()

            self._ack_if_due()
            now = time.time()
            if self._cast_ok and nudge_at and now - nudge_at > 1.0:
                # Safety net. A window Chromium decides is hidden can stop
                # repainting, and then the screencast has nothing to send —
                # while a screenshot forces a frame regardless. One per action,
                # and only if the screencast has stayed silent since it.
                if self._published_at < nudge_at:
                    self._capture()
                nudge_at = 0
            if not self._cast_ok and (acted or now - last_shot >= FRAME_INTERVAL):
                if acted:
                    time.sleep(POST_COMMAND_DELAY)
                self._capture()
                last_shot = last_meta = time.time()
            elif now - last_meta >= META_INTERVAL:
                self._update_meta(page)
                last_meta = now

    @staticmethod
    def _compact(batch):
        """Drop navigations that a later one in the same batch supersedes, and
        merge runs of scrolls into one.

        `nav` blocks this thread until the response starts, so someone who
        retyped an address — because the first attempt looked like it did
        nothing — would otherwise wait out every attempt in turn. Retrying made
        it strictly worse. Only the last address can be the one they want.

        A flick of a trackpad is dozens of wheel events; summed, it's one
        scroll to the same place and one repaint instead of dozens.

        `back`/`forward`/`reload` are deliberately left alone: two Backs mean
        go back twice, and collapsing them would swallow the second."""
        last_nav = -1
        for i, item in enumerate(batch):
            if item.get("kind") == "nav":
                last_nav = i
        out = []
        for i, item in enumerate(batch):
            kind = item.get("kind")
            if kind == "nav" and i != last_nav:
                continue
            if kind == "scroll" and out and out[-1].get("kind") == "scroll":
                try:
                    out[-1] = {"kind": "scroll",
                               "dy": float(out[-1].get("dy") or 0) + float(item.get("dy") or 0)}
                except (TypeError, ValueError):
                    out.append(item)
                continue
            out.append(item)
        return out

    # ── the screencast ──

    def _cast(self, page):
        """Point the screencast at `page`, stopping it on whatever page it was
        on. Any failure turns it off for the rest of the session and the loop
        falls back to screenshots — a picture that updates slowly beats none."""
        self._stop_cast()
        self._cast_page = page
        if page is None:
            return
        try:
            cdp = page.context.new_cdp_session(page)
            # Bound to this session: frames still in flight from the page
            # being left must not be acked against, or published over, the new
            # one.
            cdp.on("Page.screencastFrame",
                   lambda params, c=cdp: self._on_cast_frame(c, params))
            cdp.send("Page.startScreencast", {
                "format": "jpeg", "quality": FRAME_QUALITY,
                "maxWidth": VIEWPORT["width"], "maxHeight": VIEWPORT["height"],
            })
            self._cdp = cdp
        except Exception:                         # noqa: BLE001 — fall back, don't die
            self._cdp = None
            # A popup that closed itself while this attached isn't a reason to
            # give up on the screencast; the loop moves on to the next page.
            try:
                closed = page.is_closed()
            except Exception:                     # noqa: BLE001
                closed = True
            if not closed:
                logger.warning("Screencast unavailable for %r; using screenshots",
                               self.user, exc_info=True)
                self._cast_ok = False

    def _stop_cast(self):
        cdp, self._cdp, self._pending_ack = self._cdp, None, None
        if cdp is None:
            return
        # Both best-effort: the page may already be closed, taking the session
        # with it.
        for call in (lambda: cdp.send("Page.stopScreencast"), cdp.detach):
            try:
                call()
            except Exception:                     # noqa: BLE001
                pass

    def _on_cast_frame(self, cdp, params):
        """A repaint. Runs on this thread, from inside whatever playwright call
        it's in. Publishes the frame; the ack — which asks for the next one —
        waits for the loop, so it can be spaced out to MIN_FRAME_GAP."""
        if cdp is not self._cdp:
            return
        self._pending_ack = (cdp, params.get("sessionId"))
        try:
            frame = base64.b64decode(params.get("data") or "")
        except (ValueError, TypeError):
            return
        # Held back until the first page has actually started arriving: before
        # that the page is about:blank, and a white frame would take down the
        # page's "Loading" overlay to show the user nothing. Kept rather than
        # dropped, because a page that painted once and never again would
        # otherwise leave the overlay up for good (see _run).
        if self.status == "starting":
            self._held_frame = frame
            return
        self._publish(frame)
        # Straight away when the gap allows; waiting for the loop's next pass
        # instead roughly halved the frame rate an animating page got.
        self._ack_if_due()

    def _ack_if_due(self):
        if self._pending_ack is None or time.time() - self._last_ack_at < MIN_FRAME_GAP:
            return
        cdp, session_id = self._pending_ack
        self._pending_ack = None
        self._last_ack_at = time.time()
        try:
            cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:                         # noqa: BLE001 — page went away
            pass

    def _pump(self, page):
        """Idle for PUMP_INTERVAL inside playwright, which is what lets
        screencast events reach this thread at all. Shorter when an ack is
        waiting: sleeping past the moment it falls due is time the screencast
        sits stopped, and cost an animating page about half its frames."""
        wait = PUMP_INTERVAL
        if self._pending_ack is not None:
            due = self._last_ack_at + MIN_FRAME_GAP - time.time()
            wait = min(wait, max(due, 0.005))
        if page is not None:
            try:
                page.wait_for_timeout(wait * 1000)
                return
            except Exception:                     # noqa: BLE001 — closed mid-wait
                pass
        time.sleep(wait)

    def _publish(self, frame):
        self._published_at = time.time()
        with self._frame_ready:
            self._frame = frame
            self._frame_seq += 1
            self._frame_ready.notify_all()

    def _update_meta(self, page):
        if page is None:
            return
        try:
            meta = {"url": page.url, "title": page.title()}
        except Exception:                         # noqa: BLE001 — mid-navigation
            return
        with self._state_lock:
            self._meta = meta

    def _wait_until(self):
        """How long a navigation blocks this thread. With the screencast up,
        only until the response starts: returning then is what lets the loop
        get back to pumping frames, so the user watches the page load instead
        of a frozen picture of the one being left — and a bad address still
        fails before commit. The screenshot fallback has no frames to pump, and
        a capture of a page that has only just committed is a blank one."""
        return "commit" if self._cast_ok else "domcontentloaded"

    def _apply(self, command):
        """One input command against the active page. Every failure here is
        swallowed: a click that lands while the page is navigating raises, and
        killing the whole sign-in session over it would be absurd.

        A failed *navigation* is swallowed too, but recorded in `nav_error`
        first. A typo'd or unreachable address is the one failure the user has
        to be told about — it used to go to the debug log and nowhere else,
        leaving a page that simply never changed and no way to tell that from
        one still loading."""
        page = self._current_page()
        if page is None:
            return
        kind = command.get("kind")
        moving = kind in ("nav", "handoff", "back", "forward", "reload")
        if moving:
            # Read by status() from a Flask thread while this one is blocked
            # in the goto below.
            self.navigating = True
            self.nav_error = ""
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
                page.goto(command.get("url") or "", wait_until=self._wait_until(),
                          timeout=45000)
            elif kind == "handoff":
                self._add_cookies(page.context, command.get("cookies") or [])
                self.handoff = True
                page.goto(command.get("url") or "", wait_until=self._wait_until(),
                          timeout=45000)
            elif kind == "back":
                page.go_back(wait_until=self._wait_until(), timeout=30000)
            elif kind == "forward":
                page.go_forward(wait_until=self._wait_until(), timeout=30000)
            elif kind == "reload":
                page.reload(wait_until=self._wait_until(), timeout=30000)
        except Exception as e:                    # noqa: BLE001 — see docstring
            if moving:
                # Playwright's messages are several lines of stack-ish detail;
                # the first is the part that names what went wrong.
                lines = str(e).strip().splitlines()
                self.nav_error = lines[0] if lines else "The page wouldn't load."
            logger.debug("Sign-in input %r failed: %s", kind, e)
        finally:
            if moving:
                self.navigating = False

    def _add_cookies(self, context, cookies):
        """The agent's cookies, over whatever this browser already holds. Added
        rather than loaded as the context's storage_state, so the user's own
        saved logins survive — a save writes the context out whole."""
        try:
            context.add_cookies(cookies)
        except Exception:                         # noqa: BLE001 — the page may not need them
            logger.warning("Couldn't add the agent's cookies for %r", self.user, exc_info=True)

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
            self._meta = meta
        self._publish(shot)

    def _save(self, context, close):
        """Write storage_state out. `close` decides whether this was the end of
        the session or just a checkpoint in the middle of it.

        Never into a profile directory that has gone: that is the user having
        been deleted while this browser was open, and writing now would hand
        these logins to whoever is created under that name next. The session
        ends instead, unsaved."""
        state = profiles.state_path(self.user)
        if state and not os.path.isdir(os.path.dirname(state)):
            logger.warning("User %r was deleted; closing their sign-in browser unsaved",
                           self.user)
            self._orphaned = True
            self.status = "closed"
            return
        was = self.status
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
            if self.handoff:
                try:
                    page = self._current_page()
                    if page is not None:
                        browser_handoff.write_resume(self.user, page.url)
                except Exception:                 # noqa: BLE001 — the logins did save
                    logger.warning("Couldn't record where %r left the handed-over page",
                                   self.user, exc_info=True)
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
        self.status = "closed" if close else (was or "running")


# ── module-level API used by Flask ───────────────────────────

def start(user, url, cookies=None):
    """Open a sign-in browser for `user` at `url`, replacing any existing one.
    `cookies` are the agent's, from a handoff — see handoff().

    One session per user on purpose: two would race on the same storage_state
    file, and the second to save would quietly drop the first's login."""
    if not profiles.state_path(user):
        raise ValueError("That user name can't have a browser profile.")
    # `_start_lock` is what keeps one session per user, not `_registry_lock`.
    # cancel() blocks until the old worker notices — up to 15s if it is stuck
    # mid-navigation — and _registry_lock was previously held across that wait.
    # Every other browser route goes through get() for its session, so opening
    # a new address while the old browser was busy froze the whole page for as
    # long as the cancel took. Nothing reading the registry waits on this lock.
    with _start_lock:
        with _registry_lock:
            existing = _sessions.pop(user, None)
        if existing is not None and existing.alive():
            existing.cancel()
        session = TakeoverSession(user, url, cookies)
        with _registry_lock:
            _sessions[user] = session
    return session


def handoff(user, url, cookies):
    """Open the page the agent handed over (src/browser_handoff.py) with its
    cookies: in the browser already open if there is one, so nothing the user
    hasn't saved there is lost, else in a new one."""
    session = get(user)
    if session is not None and session.alive():
        session.send({"kind": "handoff", "url": url, "cookies": cookies})
        return session
    return start(user, url, cookies or [])


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
        "navigating": session.navigating,
        "nav_error": session.nav_error,
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
