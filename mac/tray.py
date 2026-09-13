"""FreeClaw — macOS menu bar app and process supervisor.

This is the macOS answer to systemd on Linux, and it is a near twin of
windows/tray.py. It owns one child process (`python -m Flask.main`), keeps it
alive, and puts an icon in the menu bar so FreeClaw feels like an installed
app rather than a terminal you must not close.

Why a menu bar app rather than a LaunchAgent with KeepAlive
-----------------------------------------------------------
launchd can absolutely keep a process alive, and that was the obvious thing
to reach for. Three things argued against making it the whole answer:

* FreeClaw's sign-in browser (src/browser_takeover.py) launches Chromium
  *headful* on purpose, because Google and Microsoft sign-in refuse headless
  browsers. A headful browser needs a real GUI session, which an Aqua
  LaunchAgent has — but so does this, and this one also has somewhere to put
  a status icon, a Restart item and a visible answer to "is it running?".
* KeepAlive cannot tell "the user chose Quit" from "it crashed", so Quit
  would resurrect the server a second later. The exit-code protocol below is
  what distinguishes them.
* A menu bar app is where a Mac user looks for a background app.

launchd still has a job here, just a smaller one: a LaunchAgent plist starts
*this* process at login, which is what "Start at Login" writes. See
set_autostart().

The restart contract
--------------------
Settings -> Restart makes the server exit with RESTART_EXIT_CODE (see
Flask/main.py: api_restart). This process is what puts it back. The exit code
is the whole protocol:

    42          restart me, this was deliberate
    0           stop, this was a clean shutdown
    anything    crash — restart with backoff, then give up and say so

Unlike Windows, the server here updates itself: a macOS install is a git
checkout with a private interpreter beside it, so Settings -> Update FreeClaw
runs update-mac.sh in place (Flask/main.py: install_kind) exactly as the Linux
install runs update.sh, and then asks for an ordinary restart. There is no
hand-off to the installer, which is why UPDATE_EXIT_CODE is treated as a
restart here rather than as a hand-off the way windows/tray.py treats it.
"""

import fcntl
import functools
import io
import logging
import os
import plistlib
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler

import pystray
from PIL import Image

# AppKit is used for two things only: hiding the Dock icon, and drawing the
# menu bar icon at the display's real pixel density. Both are commented where
# they happen. PyObjCTools.MachSignals is how a Cocoa app receives signals —
# see _install_signal_handlers().
import AppKit
import Foundation
import PyObjCTools.MachSignals

# mac/tray.py -> repo root, matching how src/logging_setup.py and
# src/telemetry.py locate the install directory.
HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
LOG_DIR = os.path.join(APP_DIR, "logs")
ICON_PATH = os.path.join(HERE, "freeclaw.png")

PORT = 6767
RESTART_EXIT_CODE = 42

# Flask/main.py's UPDATE_EXIT_CODE. A macOS install never sends it — the
# server runs update-mac.sh itself and then asks for a plain restart — but it
# is handled as a restart anyway so that a server which ever did send it
# comes back rather than staying dead.
UPDATE_EXIT_CODE = 43

# Restart storm guard. A server that dies on a bad .env would otherwise be
# respawned forever, hammering the disk and hiding the real error behind
# thousands of log lines.
MAX_CONSECUTIVE_FAILURES = 5
FAILURE_BACKOFF_SECONDS = (2, 5, 10, 20, 30)

# A server that ran at least this long before dying was not a failure to
# start, so it clears the counter above. The guard exists for an install that
# cannot come up at all — a bad .env, a missing dependency — and those die in
# seconds. Without this, update-mac.sh stopping the server to restart it, five
# times over the life of one login session, would eventually leave FreeClaw
# refusing to come back; the exit code for an externally signalled process is
# indistinguishable from a crash.
HEALTHY_RUN_SECONDS = 60

# How long to let the server bind its port before we stop calling it
# "starting". The first run of a fresh install imports nltk and loads the
# classifier, which is slower than every run after it.
STARTUP_GRACE_SECONDS = 45

# Written at startup, removed on a clean exit. install-mac.sh and
# uninstall-mac.sh read them to stop a running FreeClaw before touching its
# files. Two files and not one: if this process is killed outright, nothing
# removes the server's, and the child would otherwise be left holding port
# 6767 with no way to find it that doesn't involve matching on an install
# path — which a username containing a quote is enough to break.
PID_FILE = os.path.join(APP_DIR, "freeclaw.pid")
SERVER_PID_FILE = os.path.join(APP_DIR, "freeclaw-server.pid")

# Held open for the life of the process; the flock on it is the singleton.
LOCK_FILE = os.path.join(APP_DIR, "freeclaw.lock")

# The LaunchAgent that "Start at Login" writes. Reverse-DNS on the project's
# own domain, matching CFBundleIdentifier in the app bundle install-mac.sh
# generates — macOS treats the two as the same app.
LAUNCH_AGENT_LABEL = "dev.eedeb.freeclaw"
LAUNCH_AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
LAUNCH_AGENT_PATH = os.path.join(LAUNCH_AGENTS_DIR,
                                 f"{LAUNCH_AGENT_LABEL}.plist")

STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_RESTARTING = "restarting"
STATE_STOPPED = "stopped"
STATE_FAILED = "failed"

STATE_LABELS = {
    STATE_STARTING: "FreeClaw — starting…",
    STATE_RUNNING: "FreeClaw — running",
    STATE_RESTARTING: "FreeClaw — restarting…",
    STATE_STOPPED: "FreeClaw — stopped",
    STATE_FAILED: "FreeClaw — stopped (see logs)",
}

logger = logging.getLogger("freeclaw.tray")


def _setup_logging():
    """The tray's own log, separate from the server's logs/freeclaw.log.

    Started from the app bundle or from launchd there is no console and
    nowhere for stderr to go, so an unlogged traceback here is simply
    invisible — the icon never appears and there is nothing to look at. This
    file is the only way to debug a bad install.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "tray.log"), maxBytes=512 * 1024, backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# ── addresses ────────────────────────────────────────────────

def _custom_domain():
    """CUSTOM_DOMAIN out of .env, if the user set one.

    Parsed by hand rather than with load_dotenv: this process is a supervisor,
    not the app, and it has no business pulling the app's whole environment
    (provider keys included) into itself just to read one string.
    """
    path = os.path.join(APP_DIR, ".env")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == "CUSTOM_DOMAIN":
                    return value.strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def _lan_ip():
    """This machine's LAN address — the same UDP-connect trick src/agent.py
    uses in _server_base_url(), so the address shown here matches the one the
    agent puts in the links it generates."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # doesn't actually send anything
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


def local_url():
    """Where to send *this* machine's browser.

    Deliberately loopback and not the LAN IP, even though the installer prints
    the LAN one. Clicking the menu bar item has to work every time, and the
    LAN address can fail for reasons that have nothing to do with FreeClaw
    being up: a VPN, or a laptop that moved networks since the server started.
    The LAN address is one menu item down, for the phone.
    """
    return f"http://127.0.0.1:{PORT}"


def shareable_url():
    """The address to hand to another device on the network."""
    return _custom_domain() or f"http://{_lan_ip()}:{PORT}"


def _port_open(timeout=0.4):
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=timeout):
            return True
    except OSError:
        return False


# ── the environment the server runs in ───────────────────────

_login_path = None
_login_path_lock = threading.Lock()


def login_shell_path():
    """PATH as the user's login shell sees it, cached for the process.

    This matters more on macOS than anywhere else FreeClaw runs. A process
    started by launchd at login — which is exactly what "Start at Login" sets
    up — inherits launchd's own minimal PATH: /usr/bin:/bin:/usr/sbin:/sbin,
    with no /opt/homebrew/bin and no ~/.local/bin. The same app started from
    Finder gets the same treatment. FreeClaw would then start with a PATH that
    has none of the tools it spawns on it: node and npx for stdio MCP servers
    (src/mcp_client.py), uv/uvx, and whatever the agent's bash tool reaches
    for. The symptom is maddening — MCP servers that work when you start
    FreeClaw from a terminal and are "not installed" when it starts at login.

    So ask the login shell. `-l` and not `-i`: Homebrew's shellenv goes in a
    login profile (.zprofile, .bash_profile), an interactive shell can print
    banners and prompts, and some configurations of the latter simply never
    return.
    """
    global _login_path
    with _login_path_lock:
        if _login_path is not None:
            return _login_path
        _login_path = _read_login_shell_path() or os.environ.get("PATH", "")
        return _login_path


_PATH_MARKER = "__freeclaw_path__:"


def _read_login_shell_path():
    shell = os.environ.get("SHELL") or "/bin/zsh"
    try:
        proc = subprocess.run(
            [shell, "-l", "-c", f'printf "{_PATH_MARKER}%s\\n" "$PATH"'],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("couldn't ask %s for the login PATH", shell,
                       exc_info=True)
        return None

    # The marker rather than the whole of stdout: a login profile is free to
    # print things, and plenty do.
    value = None
    for line in proc.stdout.splitlines():
        if line.startswith(_PATH_MARKER):
            value = line[len(_PATH_MARKER):].strip()
    if not value or "/usr/bin" not in value:
        logger.warning("the login shell returned no usable PATH; keeping ours")
        return None

    # Union, login shell first: whatever started this process may itself have
    # been given something useful that a login shell doesn't set.
    entries = [p for p in value.split(":") if p]
    for extra in os.environ.get("PATH", "").split(":"):
        if extra and extra not in entries:
            entries.append(extra)
    merged = ":".join(entries)
    logger.info("server PATH resolved from %s (%d entries)", shell,
                len(entries))
    return merged


# ── the supervised server ────────────────────────────────────

class Server:
    """Owns the `python -m Flask.main` child and the loop that keeps it up."""

    def __init__(self, on_state_change=lambda: None):
        self._proc = None
        self._lock = threading.Lock()
        self._quitting = threading.Event()
        self._restarting = False
        self._thread = None
        self.state = STATE_STOPPED
        self.on_state_change = on_state_change

    # ── state ──
    def _set_state(self, state):
        if state != self.state:
            self.state = state
            logger.info("state -> %s", state)
            try:
                self.on_state_change()
            except Exception:
                logger.exception("state change callback failed")

    # ── process ──
    def _spawn(self):
        env = dict(os.environ)
        # Same value the Linux service sets. The reloader would fork a second
        # process and leave us supervising the wrong one — our wait() would
        # return on the parent while the child kept the port.
        env["FC_DEBUG"] = "0"
        env["PATH"] = login_shell_path()

        os.makedirs(LOG_DIR, exist_ok=True)
        console_log = open(
            os.path.join(LOG_DIR, "server-console.log"), "ab", buffering=0)
        proc = subprocess.Popen(
            [sys.executable, "-m", "Flask.main"],
            cwd=APP_DIR,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=console_log,
            stderr=subprocess.STDOUT,
        )
        _write_pid_file(SERVER_PID_FILE, proc.pid)
        logger.info("server started (pid %s)", proc.pid)
        return proc, console_log

    def _await_port(self):
        """Flip starting -> running once the server answers, so the menu is
        telling the truth rather than assuming a spawn means a working app."""
        deadline = time.monotonic() + STARTUP_GRACE_SECONDS
        while time.monotonic() < deadline:
            if self._quitting.is_set():
                return
            with self._lock:
                proc = self._proc
            if proc is None or proc.poll() is not None:
                return  # died during startup; the run loop handles it
            if _port_open():
                self._set_state(STATE_RUNNING)
                return
            time.sleep(0.5)
        logger.warning("server did not answer on port %s within %ss",
                       PORT, STARTUP_GRACE_SECONDS)

    def _run(self):
        failures = 0
        while not self._quitting.is_set():
            self._set_state(STATE_STARTING)
            started = time.monotonic()
            try:
                proc, console_log = self._spawn()
            except Exception:
                logger.exception("couldn't start the server process")
                self._set_state(STATE_FAILED)
                return

            with self._lock:
                self._proc = proc
                self._restarting = False
            threading.Thread(target=self._await_port, daemon=True,
                             name="freeclaw-port-probe").start()

            code = proc.wait()
            console_log.close()
            with self._lock:
                self._proc = None
                asked_for_restart = self._restarting
            _remove_file(SERVER_PID_FILE)
            logger.info("server exited with code %s", code)

            if self._quitting.is_set():
                return

            if code in (RESTART_EXIT_CODE, UPDATE_EXIT_CODE) or asked_for_restart:
                # Deliberate. Not a failure, so it must not count toward the
                # storm guard — otherwise five Settings saves in a row would
                # leave FreeClaw refusing to come back up.
                failures = 0
                self._set_state(STATE_RESTARTING)
                continue

            if code == 0:
                self._set_state(STATE_STOPPED)
                return

            if time.monotonic() - started >= HEALTHY_RUN_SECONDS:
                failures = 0
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error("server failed %s times in a row — giving up",
                             failures)
                self._set_state(STATE_FAILED)
                return
            delay = FAILURE_BACKOFF_SECONDS[
                min(failures - 1, len(FAILURE_BACKOFF_SECONDS) - 1)]
            logger.warning("restarting in %ss (failure %s of %s)",
                           delay, failures, MAX_CONSECUTIVE_FAILURES)
            self._set_state(STATE_RESTARTING)
            if self._quitting.wait(delay):
                return

    # ── control ──
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._quitting.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="freeclaw-supervisor")
        self._thread.start()

    def restart(self):
        """Kill the child and let the run loop bring it back."""
        with self._lock:
            proc = self._proc
            self._restarting = True
        self._set_state(STATE_RESTARTING)
        if proc is not None:
            self._terminate(proc)
        else:
            self.start()

    def stop(self):
        self._quitting.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            self._terminate(proc)
        _remove_file(SERVER_PID_FILE)
        self._set_state(STATE_STOPPED)

    @staticmethod
    def _terminate(proc):
        """SIGTERM, then insist.

        Ten seconds is generous for a Flask process with nothing to flush;
        conversation writes are atomic (src/users.py: _write_json_atomic), so
        there is no half-written state for a graceful shutdown to protect —
        which is the same reason /api/restart is free to use os._exit.
        """
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("server didn't exit on SIGTERM — killing pid %s",
                           proc.pid)
            try:
                proc.kill()
            except OSError:
                logger.exception("couldn't kill the server process")
        except OSError:
            logger.exception("couldn't terminate the server process")


# ── macOS integration ────────────────────────────────────────

# The flock that makes this a singleton. Module-level because the lock lives
# exactly as long as the open file does, and the point is for it to live as
# long as the process.
_lock_handle = None


def already_running():
    """True if another tray instance holds the lock file.

    flock rather than "is the pid in freeclaw.pid alive?": the kernel drops
    the lock when the holder dies however it dies, so a crash cannot leave
    behind a file that keeps FreeClaw from ever starting again.
    """
    global _lock_handle
    try:
        handle = open(LOCK_FILE, "a+")
    except OSError:
        logger.exception("couldn't open %s — skipping the singleton check",
                         LOCK_FILE)
        return False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return True
    _lock_handle = handle
    return False


def _write_pid_file(path, pid):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(pid))
    except OSError:
        # Not fatal: the installer falls back to telling the user to quit
        # FreeClaw themselves, which is worse but not broken.
        logger.exception("couldn't write %s", path)


def _remove_file(path):
    try:
        os.remove(path)
    except OSError:
        pass


def autostart_enabled():
    return os.path.exists(LAUNCH_AGENT_PATH)


def set_autostart(enabled):
    """Write or remove the LaunchAgent plist.

    Deliberately no `launchctl` call in either direction.

    Enabling: launchd reads ~/Library/LaunchAgents at login, which is the only
    moment RunAtLoad means anything, so writing the file is the whole job.
    Bootstrapping it now would try to start a second FreeClaw immediately —
    the one thing already_running() exists to refuse.

    Disabling: `launchctl bootout` would kill the running process, because if
    FreeClaw *did* start at login then this process is that job. Unticking a
    "Start at Login" checkbox must not quit the app. Removing the file is
    enough; nothing starts it next time.
    """
    try:
        if enabled:
            os.makedirs(LAUNCH_AGENTS_DIR, exist_ok=True)
            plist = {
                "Label": LAUNCH_AGENT_LABEL,
                "ProgramArguments": [sys.executable,
                                     os.path.abspath(__file__)],
                "RunAtLoad": True,
                "WorkingDirectory": APP_DIR,
                # Aqua, not Background: the sign-in browser is headful and
                # needs a GUI session, and a menu bar icon needs one to appear
                # in. This is the default for a LaunchAgent, said out loud
                # because it is load-bearing here.
                "ProcessType": "Interactive",
                "StandardErrorPath": os.path.join(LOG_DIR, "launchd.log"),
            }
            with open(LAUNCH_AGENT_PATH, "wb") as f:
                plistlib.dump(plist, f)
            logger.info("autostart enabled: %s", LAUNCH_AGENT_PATH)
        else:
            _remove_file(LAUNCH_AGENT_PATH)
            logger.info("autostart disabled")
    except OSError:
        logger.exception("couldn't update the LaunchAgent")


def copy_to_clipboard(text):
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
    except (OSError, subprocess.SubprocessError):
        logger.exception("couldn't copy to the clipboard")


def open_folder(path):
    os.makedirs(path, exist_ok=True)
    try:
        subprocess.run(["open", path], check=False)
    except OSError:
        logger.exception("couldn't open %s in Finder", path)


def _on_main_thread(fn):
    """Run fn on the main thread, where AppKit expects to be touched.

    Menu and title updates arrive from the supervisor thread — that is the
    whole point of a status line that tracks the server — and NSStatusItem and
    NSMenu are main-thread-only like the rest of AppKit. pystray calls them
    wherever it is called from, which is usually fine and occasionally is not:
    the failure mode is a menu that redraws wrongly or a crash inside AppKit,
    both of them rare enough to be maddening to chase.

    The main thread is inside NSApplication.run() for the life of the app, so
    the queued block runs on the next turn of that loop.
    """
    try:
        Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(fn)
    except Exception:
        # Better a possibly-off-thread update than no update at all: this
        # drives the status line people read to see whether FreeClaw is up.
        logger.exception("couldn't dispatch to the main thread")
        try:
            fn()
        except Exception:
            logger.exception("the main-thread callback failed")


def _hide_dock_icon():
    """Accessory, not Regular: a menu bar app with no Dock tile and no app
    menu of its own.

    The app bundle install-mac.sh writes carries LSUIElement, which covers the
    same ground — but only for the bundle. Started any other way (a developer
    running this file, the LaunchAgent pointing straight at the interpreter),
    the bundle's Info.plist is not in the picture and Python would bounce into
    the Dock without this.
    """
    try:
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory)
    except Exception:
        logger.exception("couldn't set the activation policy")


class RetinaIcon(pystray.Icon):
    """pystray's macOS icon, drawn at the display's real pixel density.

    The backend builds an NSImage from a PNG the same number of *pixels* as
    the menu bar is *points* tall — 22x22 — which is half the resolution of
    every Retina Mac and looks it. Rendering at 2x and then telling the
    NSImage it measures 22 points is the whole fix: AppKit picks the
    representation that matches the display.

    An override of a private method, so it is written to degrade rather than
    break: if a future pystray renames it, this simply stops being called and
    the icon goes back to being blurry.
    """

    def _assert_image(self):
        thickness = self._status_bar.thickness()
        size = (int(thickness), int(thickness))
        if self._icon_image is not None and tuple(self._icon_image.size()) == size:
            return
        scaled = self._icon.resize((size[0] * 2, size[1] * 2),
                                   Image.LANCZOS)
        buffer = io.BytesIO()
        scaled.save(buffer, "png")
        image = AppKit.NSImage.alloc().initWithData_(
            Foundation.NSData(buffer.getvalue()))
        image.setSize_(AppKit.NSMakeSize(*size))
        self._icon_image = image
        self._status_item.button().setImage_(image)


# ── tray ─────────────────────────────────────────────────────

def _menu_action(fn):
    """Run a menu handler off the main thread.

    pystray's macOS backend invokes a menu callback straight from AppKit's
    action selector, which means the main thread — the one drawing the menu
    and pumping every event the app receives. The handlers below wait for a
    port to open (up to twenty seconds), wait for a child process to exit (up
    to ten), and shell out to pbcopy and open. Run inline, each of those is a
    frozen menu bar for exactly as long as it takes, complete with spinning
    beachball.

    So the handler returns at once and does its work on a thread of its own.
    Anything it needs to put back on the main thread goes through
    _on_main_thread(). On Windows none of this applies — pystray runs those
    callbacks on its own thread there, which is why windows/tray.py calls them
    directly.
    """
    @functools.wraps(fn)
    def wrapper(self, *args):
        threading.Thread(target=fn, args=(self,) + args, daemon=True,
                         name=f"freeclaw-menu-{fn.__name__}").start()
    return wrapper


class Tray:
    def __init__(self):
        self.server = Server(on_state_change=self._refresh)
        self.icon = RetinaIcon(
            "freeclaw",
            Image.open(ICON_PATH),
            STATE_LABELS[STATE_STOPPED],
            menu=self._menu(),
        )

    def _menu(self):
        item = pystray.MenuItem
        return pystray.Menu(
            item(lambda _: STATE_LABELS.get(self.server.state, "FreeClaw"),
                 None, enabled=False),
            pystray.Menu.SEPARATOR,
            # default=True only renders the item bold on macOS — clicking a
            # status item always opens its menu here, which is what a Mac user
            # expects. On Windows the same flag makes a left-click open the
            # app directly.
            item("Open FreeClaw", self._on_open, default=True),
            item("Copy address for other devices", self._on_copy),
            pystray.Menu.SEPARATOR,
            item("Restart", self._on_restart),
            item("Open logs folder", self._on_logs),
            pystray.Menu.SEPARATOR,
            item("Start at Login", self._on_toggle_autostart,
                 checked=lambda _: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            item("Quit FreeClaw", self._on_quit),
        )

    def _refresh(self):
        """Called by the supervisor whenever the server changes state — from
        the supervisor's own thread, never the main one."""
        state = self.server.state
        _on_main_thread(lambda: self._apply_state(state))
        # Deliberately not on the main thread: a notification is an osascript
        # subprocess, and the main thread is the one drawing the menu.
        if state == STATE_FAILED:
            self._notify("FreeClaw stopped",
                         "It failed to start several times. "
                         "Open the logs folder for the reason.")

    def _apply_state(self, state):
        self.icon.title = STATE_LABELS.get(state, "FreeClaw")
        try:
            self.icon.update_menu()
        except Exception:
            logger.exception("couldn't refresh the menu")

    def _notify(self, title, message):
        try:
            self.icon.notify(message, title)
        except Exception:
            logger.exception("couldn't show a notification")

    # ── menu handlers ──
    @_menu_action
    def _on_open(self, *_):
        # A click during startup should still land on a working page rather
        # than a connection error, so give the port a moment before giving up.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not _port_open():
            if self.server.state in (STATE_STOPPED, STATE_FAILED):
                break
            time.sleep(0.4)
        webbrowser.open(local_url())

    @_menu_action
    def _on_copy(self, *_):
        url = shareable_url()
        copy_to_clipboard(url)
        self._notify("Address copied", f"{url}\nOpen this on another device.")

    @_menu_action
    def _on_restart(self, *_):
        self.server.restart()

    @_menu_action
    def _on_logs(self, *_):
        open_folder(LOG_DIR)

    def _on_toggle_autostart(self, *_):
        # Not backgrounded, unlike its neighbours: writing one small plist is
        # immediate, and staying synchronous is what lets pystray's own redraw
        # — which runs the moment this returns — show the new checkmark.
        set_autostart(not autostart_enabled())

    @_menu_action
    def _on_quit(self, *_):
        logger.info("quit requested from the menu")
        self.shutdown()

    def shutdown(self):
        self.server.stop()
        self.icon.stop()

    def run(self):
        _hide_dock_icon()
        _install_signal_handlers(self.shutdown)

        # setup= runs on pystray's own thread once the icon exists, which is
        # the earliest point notifications and menu updates are safe.
        def _started(icon):
            icon.visible = True
            _check_icon_placed(icon)
            self.server.start()
        self.icon.run(setup=_started)


def _check_icon_placed(icon):
    """Say so in the log if the menu bar icon never made it onto the bar.

    Worth the twenty lines because of how this fails: everything else works.
    The app starts, the server comes up, the log reads clean, and the only
    symptom is an icon that is not there — with nothing to search for and
    nothing to read. It happened for real: the app bundle's launcher used
    `exec`, which replaced the process Launch Services had checked in, and the
    status item was then silently never placed (see install-mac.sh).

    `window().isVisible()` is the signal, and only that one. A placed item
    reports a window that is visible but has no `screen()`; an unplaced one
    reports the opposite of both, which is exactly backwards from what you
    would guess and why this checks the narrow thing it measured rather than
    the obvious one.

    A warning, never an error: it is a heuristic against a private detail of
    pystray's backend, so a wrong guess should cost one log line and nothing
    else.
    """
    def _look():
        try:
            window = icon._status_item.button().window()
        except Exception:
            logger.debug("couldn't inspect the status item", exc_info=True)
            return
        if window is not None and not window.isVisible():
            logger.warning(
                "the menu bar icon was not placed — FreeClaw is running and "
                "the web UI works, but there is no icon to click. Open "
                "http://127.0.0.1:%s directly, and see mac/README.md "
                "(\"The app bundle\") for what causes this.", PORT)

    # After a beat: placement is not synchronous, and asking too early reports
    # a window that simply has not been positioned yet.
    threading.Timer(3.0, lambda: _on_main_thread(_look)).start()


def _install_signal_handlers(shutdown):
    """Stop cleanly on SIGTERM, so the server child never outlives us.

    Through PyObjCTools.MachSignals rather than `signal.signal`, and that is
    not a preference: a Python signal handler only runs between bytecodes on
    the main thread, and this main thread spends its whole life blocked inside
    Cocoa's event loop. A plain handler would be recorded and never called.
    MachSignals routes the signal through a Mach port the run loop is already
    watching. pystray does the same for ctrl-c, for the same reason.

    Without this, `kill <tray pid>` — which is what uninstall-mac.sh and the
    updater do — would take the supervisor down and leave `python -m
    Flask.main` running, still holding port 6767, with nothing supervising it.
    """
    def _handler(*_args):
        logger.info("signal received — shutting down")
        shutdown()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            PyObjCTools.MachSignals.signal(sig, _handler)
        except Exception:
            logger.exception("couldn't install a handler for signal %s", sig)


def main():
    _setup_logging()

    if already_running():
        # Opening the app again while FreeClaw is up should show you FreeClaw,
        # not start a second copy that loses the race for port 6767 and dies
        # in the background.
        logger.info("another instance is already running — opening the UI")
        webbrowser.open(local_url())
        return 0

    logger.info("FreeClaw menu bar app starting (app dir: %s)", APP_DIR)
    _write_pid_file(PID_FILE, os.getpid())
    try:
        Tray().run()
    except Exception:
        logger.exception("the menu bar app crashed")
        return 1
    finally:
        _remove_file(PID_FILE)
        _remove_file(SERVER_PID_FILE)
    logger.info("FreeClaw menu bar app exited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
