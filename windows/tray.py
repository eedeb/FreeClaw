"""FreeClaw — Windows notification-area app and process supervisor.

This is the Windows answer to systemd on Linux and `restart: unless-stopped`
on macOS/Docker. It owns one child process (`python -m Flask.main`), keeps it
alive, and puts an icon in the notification area so FreeClaw feels like an
installed app rather than a terminal you must not close.

Why a tray app rather than a real Windows service
-------------------------------------------------
A service runs in session 0, which has no desktop. FreeClaw's sign-in browser
(src/browser_takeover.py) launches Chromium *headful* on purpose, because
Google and Microsoft sign-in refuse headless browsers — and headful needs a
desktop to draw on. A tray app runs in the interactive session, so that flow
works here without the Xvfb dance the Linux install needs. Stdio MCP servers
inherit the same session, so they can reach the user's real node/python too.

The restart contract
--------------------
Settings -> Restart makes the server exit with RESTART_EXIT_CODE (see
Flask/main.py: api_restart). This process is what puts it back. The exit code
is the whole protocol:

    42          restart me, this was deliberate
    0           stop, this was a clean shutdown
    anything    crash — restart with backoff, then give up and say so

Distinguishing those is why the code is 42 and not 0. systemd's Restart=always
and Docker's restart policy respawn on any exit, so they never needed to tell
"restart" from "stop"; a tray icon does, or Quit would resurrect the server.
"""

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import logging
from logging.handlers import RotatingFileHandler

import pystray
from PIL import Image

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import ctypes
    import winreg

# windows/tray.py -> repo root, matching how src/logging_setup.py and
# src/telemetry.py locate the install directory.
HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
LOG_DIR = os.path.join(APP_DIR, "logs")
ICON_PATH = os.path.join(HERE, "freeclaw.ico")

PORT = 6767
RESTART_EXIT_CODE = 42

# "Settings -> Update FreeClaw" (Flask/main.py: UPDATE_EXIT_CODE). The server
# cannot update itself on Windows: there is no git checkout and no update.sh,
# the install is a packaged tree, and half its files are open in the very
# process that would be replacing them. So the server exits with this code and
# the supervisor — which is not being replaced — runs install.ps1, the same
# script that installed FreeClaw in the first place. Re-running it is the
# documented update path: it verifies the download, replaces the program files
# and leaves .env, chats and logs alone.
UPDATE_EXIT_CODE = 43

# Where install.ps1 is published. Overridable so a fork, a staging host or an
# air-gapped mirror does not need a code change.
UPDATE_URL = os.environ.get(
    "FC_UPDATE_URL", "https://freeclaw.eedeb.dev/install.ps1")

# Restart storm guard. A server that dies on a bad .env would otherwise be
# respawned forever, hammering the disk and hiding the real error behind
# thousands of log lines.
MAX_CONSECUTIVE_FAILURES = 5
FAILURE_BACKOFF_SECONDS = (2, 5, 10, 20, 30)

# How long to let the server bind its port before we stop calling it "starting".
# The first run of a fresh install imports nltk and loads the classifier, which
# is slower than every run after it.
STARTUP_GRACE_SECONDS = 45

# On Windows a console child spawned from a parent that has no console gets a
# brand new console window unless this flag says otherwise. The tray runs under
# pythonw.exe, so without it the server would pop a black window on every
# start. (0 elsewhere, which keeps this module importable off Windows.)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Written at startup, removed on a clean exit. The installer and uninstaller
# read it to stop a running FreeClaw before touching its files — a PID is
# immune to the quoting problems that matching on an install path would have
# (a username containing an apostrophe is enough to break that).
PID_FILE = os.path.join(APP_DIR, "freeclaw.pid")

MUTEX_NAME = "Local\\FreeClawTraySingleton"
ERROR_ALREADY_EXISTS = 183

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "FreeClaw"

STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_RESTARTING = "restarting"
STATE_UPDATING = "updating"
STATE_STOPPED = "stopped"
STATE_FAILED = "failed"

STATE_LABELS = {
    STATE_STARTING: "FreeClaw — starting…",
    STATE_RUNNING: "FreeClaw — running",
    STATE_RESTARTING: "FreeClaw — restarting…",
    STATE_UPDATING: "FreeClaw — updating…",
    STATE_STOPPED: "FreeClaw — stopped",
    STATE_FAILED: "FreeClaw — stopped (see logs)",
}

logger = logging.getLogger("freeclaw.tray")


def _setup_logging():
    """The tray's own log, separate from the server's logs/freeclaw.log.

    Under pythonw.exe there is no console and no stderr, so an unlogged
    traceback here is simply invisible — the icon never appears and there is
    nothing to look at. This file is the only way to debug a bad install.
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

    Deliberately loopback and not the LAN IP, even though the LAN IP is what
    the installer prints. Clicking the tray icon has to work every time, and
    the LAN address can fail for reasons that have nothing to do with FreeClaw
    being up: no firewall rule yet, VPN, a laptop that moved networks since
    the server started. The LAN address is one menu item down, for the phone.
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
        # Set by Tray. Called once the updater is running, to take this tray
        # down: the update replaces the files this process runs from, and the
        # new one is started by install.ps1 at the end.
        self.on_update_handoff = None

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
    @staticmethod
    def _console_python():
        """python.exe next to the pythonw.exe we are running under.

        The tray wants no console; the server wants real stdout/stderr. Under
        pythonw both sys.stdout and sys.stderr are None, and anything the
        child writes to them is silently dropped — including the startup
        traceback you most need when an install is broken. Running the child
        under python.exe with CREATE_NO_WINDOW gets both: real streams to
        redirect into a file, and no window.
        """
        exe = sys.executable
        if os.path.basename(exe).lower() == "pythonw.exe":
            candidate = os.path.join(os.path.dirname(exe), "python.exe")
            if os.path.exists(candidate):
                return candidate
        return exe

    def _spawn(self):
        env = dict(os.environ)
        # Same value the container sets. The reloader would fork a second
        # process and leave us supervising the wrong one — our wait() would
        # return on the parent while the child kept the port.
        env["FC_DEBUG"] = "0"
        # Windows still defaults stdio to the ANSI code page in some consoles,
        # and the agent's output is full of text that isn't cp1252.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        os.makedirs(LOG_DIR, exist_ok=True)
        console_log = open(
            os.path.join(LOG_DIR, "server-console.log"), "ab", buffering=0)
        proc = subprocess.Popen(
            [self._console_python(), "-m", "Flask.main"],
            cwd=APP_DIR,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=console_log,
            stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW,
        )
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
            logger.info("server exited with code %s", code)

            if self._quitting.is_set():
                return

            if code == UPDATE_EXIT_CODE:
                # Not a failure either, so the storm guard is reset for the
                # same reason as a restart.
                failures = 0
                self._set_state(STATE_UPDATING)
                if self._run_updater():
                    # Hand over completely. The updater replaces the files this
                    # process is running from and starts a fresh tray at the
                    # end, so this one has to go — supervising through an
                    # update would only put the old server back and hold its
                    # files open while they are being replaced.
                    if self.on_update_handoff:
                        self.on_update_handoff()
                    return
                # Launching the updater failed — say so and put the server
                # back, because the alternative is a FreeClaw that vanished
                # when the user clicked Update.
                logger.error("update failed to start; restarting the current version")
                self._set_state(STATE_RESTARTING)
                continue

            if code == RESTART_EXIT_CODE or asked_for_restart:
                # Deliberate. Not a failure, so it must not count toward the
                # storm guard — otherwise five Settings saves in a row would
                # leave FreeClaw refusing to come back up.
                failures = 0
                self._set_state(STATE_RESTARTING)
                continue

            if code == 0:
                self._set_state(STATE_STOPPED)
                return

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

    def _run_updater(self):
        """Start install.ps1 in a detached PowerShell. True if it launched.

        True means "the updater is running", not "the update worked" — the
        script outlives this process by design, so there is nothing left here
        to report its result to. It writes its own log; failures leave the
        current install untouched because it only replaces files once the
        download has passed its checksum.

        Detached, and in a new process group, for one specific reason: the
        updater stops the running FreeClaw with `taskkill /T`, which walks the
        process tree. A child of this process would be inside that tree and
        would be killed halfway through replacing the install.
        """
        try:
            command = "irm '{}' | iex".format(UPDATE_URL)
            logger.info("starting the updater: %s", UPDATE_URL)
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-WindowStyle", "Hidden", "-Command", command],
                cwd=os.environ.get("TEMP", APP_DIR),
                creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                               | NO_WINDOW),
                close_fds=True,
                # Update this install, wherever it happens to live, rather than
                # install.ps1's default location. Piped through `iex` there is
                # no way to pass a parameter, so the script reads these.
                env={**os.environ,
                     "FREECLAW_DIR": APP_DIR,
                     "FREECLAW_NO_SHORTCUT": "1",
                     "FREECLAW_NO_PATH": "1"},
            )
            return True
        except Exception:
            logger.exception("couldn't start the updater")
            return False

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
        self._set_state(STATE_STOPPED)

    @staticmethod
    def _terminate(proc):
        """Ask, then insist.

        terminate() is TerminateProcess on Windows — already abrupt, with no
        chance to run atexit handlers. That is acceptable here for the same
        reason /api/restart uses os._exit: conversation writes are atomic
        (src/users.py: _write_json_atomic), so there is no half-written state
        for a graceful shutdown to protect.
        """
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("server didn't exit on terminate — killing pid %s",
                           proc.pid)
            try:
                proc.kill()
            except OSError:
                logger.exception("couldn't kill the server process")
        except OSError:
            logger.exception("couldn't terminate the server process")


# ── Windows integration ──────────────────────────────────────

def already_running():
    """True if another tray instance holds the singleton mutex.

    The handle is deliberately leaked: it must stay open for the lifetime of
    the process, and Windows drops it when we exit.
    """
    if not IS_WINDOWS:
        return False
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return kernel32.GetLastError() == ERROR_ALREADY_EXISTS


def write_pid_file():
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        # Not fatal: the installer falls back to telling the user to quit
        # FreeClaw themselves, which is a worse experience but not a broken one.
        logger.exception("couldn't write %s", PID_FILE)


def remove_pid_file():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def autostart_enabled():
    if not IS_WINDOWS:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except OSError:
        return False


def set_autostart(enabled):
    """Add or remove the HKCU Run entry.

    HKCU and not HKLM: the installer is per-user, so this needs no admin and
    no elevation prompt for a checkbox in a menu.
    """
    if not IS_WINDOWS:
        return
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                command = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
                logger.info("autostart enabled: %s", command)
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                    logger.info("autostart disabled")
                except FileNotFoundError:
                    pass
    except OSError:
        logger.exception("couldn't update the autostart entry")


def copy_to_clipboard(text):
    """Hand text to clip.exe.

    Windows has no stdlib clipboard, tkinter is not in the embeddable Python
    this ships with, and pulling in pywin32 for one menu item is not worth a
    dependency. clip.exe is present on every supported Windows.
    """
    if not IS_WINDOWS:
        return
    try:
        subprocess.run(["clip"], input=text.encode("utf-16-le"),
                       creationflags=NO_WINDOW, check=True)
    except (OSError, subprocess.SubprocessError):
        logger.exception("couldn't copy to the clipboard")


def open_folder(path):
    os.makedirs(path, exist_ok=True)
    if IS_WINDOWS:
        os.startfile(path)  # noqa: S606 — a folder we own, not user input


# ── tray ─────────────────────────────────────────────────────

class Tray:
    def __init__(self):
        self.server = Server(on_state_change=self._refresh)
        self.server.on_update_handoff = self._on_update_handoff
        self.icon = pystray.Icon(
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
            # default=True is what makes a plain left-click on the icon open
            # the app, which is the one interaction most people will ever use.
            item("Open FreeClaw", self._on_open, default=True),
            item("Copy address for other devices", self._on_copy),
            pystray.Menu.SEPARATOR,
            item("Restart", self._on_restart),
            item("Open logs folder", self._on_logs),
            pystray.Menu.SEPARATOR,
            item("Start with Windows", self._on_toggle_autostart,
                 checked=lambda _: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            item("Quit FreeClaw", self._on_quit),
        )

    def _refresh(self):
        self.icon.title = STATE_LABELS.get(self.server.state, "FreeClaw")
        try:
            self.icon.update_menu()
        except Exception:
            logger.exception("couldn't refresh the tray menu")
        if self.server.state == STATE_FAILED:
            self._notify("FreeClaw stopped",
                         "It failed to start several times. "
                         "Open the logs folder for the reason.")

    def _notify(self, title, message):
        try:
            self.icon.notify(message, title)
        except Exception:
            logger.exception("couldn't show a notification")

    # ── menu handlers ──
    def _on_open(self, *_):
        # A click during startup should still land on a working page rather
        # than a connection error, so give the port a moment before giving up.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not _port_open():
            if self.server.state in (STATE_STOPPED, STATE_FAILED):
                break
            time.sleep(0.4)
        webbrowser.open(local_url())

    def _on_copy(self, *_):
        url = shareable_url()
        copy_to_clipboard(url)
        self._notify("Address copied", f"{url}\nOpen this on another device.")

    def _on_restart(self, *_):
        self.server.restart()

    def _on_logs(self, *_):
        open_folder(LOG_DIR)

    def _on_toggle_autostart(self, *_):
        set_autostart(not autostart_enabled())
        self.icon.update_menu()

    def _on_quit(self, *_):
        logger.info("quit requested from the tray menu")
        self.server.stop()
        self.icon.stop()

    def _on_update_handoff(self):
        """The updater is running; get out of its way.

        Same shutdown as Quit, and deliberately so: main() removes
        freeclaw.pid on the way out, which is exactly what the updater's
        "stop the running FreeClaw" step looks for. Leaving it behind would
        have install.ps1 taskkill a PID that is either gone or, worse,
        recycled.
        """
        logger.info("handing over to the updater and exiting")
        self.server.stop()
        self.icon.stop()

    def run(self):
        # setup= runs on pystray's own thread once the icon exists, which is
        # the earliest point notifications and menu updates are safe.
        def _started(icon):
            icon.visible = True
            self.server.start()
        self.icon.run(setup=_started)


def main():
    _setup_logging()

    if already_running():
        # Double-clicking the Start Menu entry while FreeClaw is up should
        # show you FreeClaw, not start a second copy that loses the race for
        # port 6767 and dies in the background.
        logger.info("another instance is already running — opening the UI")
        webbrowser.open(local_url())
        return 0

    logger.info("FreeClaw tray starting (app dir: %s)", APP_DIR)
    write_pid_file()
    try:
        Tray().run()
    except Exception:
        logger.exception("the tray app crashed")
        return 1
    finally:
        remove_pid_file()
    logger.info("FreeClaw tray exited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
