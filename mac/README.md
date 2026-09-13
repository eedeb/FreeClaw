# FreeClaw on macOS

A native install: no Docker Desktop, no Homebrew, no Python of your own.
`install-mac.sh` drops a self-contained tree into `~/.freeclaw` and adds a menu
bar app that keeps the server running.

```
github.com/eedeb/FreeClaw   →   ~/.freeclaw
                                  ├── python/        private interpreter + deps
                                  ├── Flask/ src/ models/   the clone
                                  ├── mac/tray.py    the menu bar app
                                  ├── logs/
                                  ├── .env               written on first install
                                  └── .freeclaw-install  marks this as an install

                                ~/Applications/FreeClaw.app   launcher + icon
```

The install *is* a shallow clone of the repo, with a private Python beside it —
the same shape as the Windows install, and the reason the two directories look
so alike. Nothing is published or hosted for it to work: the source comes from
GitHub and the interpreter from python-build-standalone, so there is no release
to cut and no artifact to upload.

## Installing

```bash
curl -fsSL https://freeclaw.eedeb.dev/install-mac.sh | bash
```

**Git is the one prerequisite** — `xcode-select --install` if you haven't got
it. FreeClaw's bash tool wants real command line tools anyway.

What it does:

- clones the repo into `~/.freeclaw`;
- downloads a relocatable CPython 3.12 into `python/`, checks it against a
  pinned SHA-256, and installs the dependencies there. Private to the install,
  never added to `PATH`, and it does not touch any Python you already have —
  the same role `install.sh`'s virtualenv plays on Linux;
- stops a running FreeClaw first, through `freeclaw.pid`, so it is never
  replacing files that are in use;
- asks for a login password (or generates one and prints it, when there is no
  terminal to ask at);
- builds `~/Applications/FreeClaw.app`, installs the `freeclaw` command, offers
  to start FreeClaw at login, and opens it.

A first install takes a few minutes, nearly all of it pip. Re-running it is a
supported update path and takes seconds: the clone is refreshed path by path
(never `Flask/static`, which is where the chats are) and Python is reused.

`--dir`, `--branch`, `--no-start`, `--no-app`, `--no-cli`, `--autostart` and
`--no-autostart` adjust the rest. Piped through `bash` there is nowhere to put
a flag, so each also reads an environment variable (`FREECLAW_DIR`,
`FREECLAW_PASSWORD`, and so on — see the header of
[`install-mac.sh`](../install-mac.sh)).

### Why 3.12 and not the Python already on the Mac

Apple ships 3.9, and `requirements.txt` gates the shipped browser MCP server on
`python_version >= "3.10"` — so an install built on the system Python would come
up quietly missing it. A private interpreter also means an install can never be
broken by a `brew upgrade python`, and FreeClaw is free to spawn
`sys.executable` knowing exactly what it will get: `src/mcp_client.py` starts
the built-in browser MCP server that way.

### Removing it

```bash
~/.freeclaw/uninstall-mac.sh
```

`uninstall-mac.sh` ships inside the install rather than on the website — only
`install-mac.sh` is hosted — so removing FreeClaw needs no network at all, and
running the copy inside an install defaults to removing *that* install whatever
directory it is in.

Your data stays unless you add `--purge`. The app, the login item and the
`freeclaw` command are only removed if they actually point at the install being
removed — a second FreeClaw elsewhere is left alone.

### Coming from the Docker install

macOS used to run FreeClaw in a container, because there is no systemd here and
a supervisor had to come from somewhere. The menu bar app is that supervisor
now, and it is a better one: the sign-in browser gets a real window server
instead of Xvfb, stdio MCP servers can reach the Node and Python you actually
have installed, and Settings → Update FreeClaw works, which it never could from
inside a container.

Run the one-liner, then remove the old install yourself:

```bash
docker rm -f freeclaw
docker rmi freeclaw:local
```

Nothing removes it for you, because that container's bind mounts are the only
copy of your old chats. They are in the old checkout's `Flask/static/` — copy
what you want to keep into `~/.freeclaw/Flask/static/` before deleting the
folder. `.env` is worth copying over too, providers and all; the installer
merges rather than overwrites, so anything already in the new one wins.

## Using it

**Click the icon** in the menu bar — the claw, at the top right.

| Item | What it does |
|---|---|
| Open FreeClaw | `http://127.0.0.1:6767` |
| Copy address for other devices | Copies the LAN address, for your phone or another Mac |
| Restart | Restarts the server — the same thing Settings → Restart does |
| Open logs folder | `logs/` — `freeclaw.log`, `tray.log`, `server-console.log` |
| Start at Login | Writes or removes the LaunchAgent |
| Quit FreeClaw | Stops the server and removes the icon |

The first item is a live status line: *starting*, *running*, or *stopped*.

Two addresses, on purpose. The menu always opens loopback, because that works
whether or not the LAN address does — a VPN, or a laptop that changed networks
since the server started, will each break the LAN address while FreeClaw itself
is perfectly healthy. **Copy address for other devices** is the one to send to
your phone.

### Reaching it from other devices

macOS's application firewall is off by default, and when it is on it asks the
first time something listens rather than blocking silently. If the LAN address
doesn't answer, check **System Settings → Network → Firewall** and allow
incoming connections for FreeClaw — no `sudo`, and nothing the install does for
you.

## How it stays running

Linux has systemd and Windows has the notification-area app. On macOS the menu
bar app is the supervisor: it owns one `python -m Flask.main` child and puts it
back when it goes away. The exit code is the whole protocol.

| Exit code | Meaning | The app does |
|---|---|---|
| `42` | Settings → Restart | Restarts immediately |
| `0` | Clean shutdown | Stays stopped |
| anything else | Crash | Restarts with backoff, gives up after 5 and says so |

Five *consecutive* failures, and a run that lasted a minute clears the count —
the guard is there for an install that cannot come up at all, and those die in
seconds.

### Why not a LaunchAgent with `KeepAlive`

launchd can keep a process alive, and it is the obvious thing to reach for.
Three things argued against making it the whole answer:

- `KeepAlive` cannot tell "the user chose Quit" from "it crashed", so Quit
  would resurrect the server a second later. The exit codes above are what
  distinguish them.
- A menu bar app is where a Mac user looks for a background app, and it can
  answer "is it running?" without a terminal.
- The sign-in browser (`src/browser_takeover.py`) launches Chromium *headful*
  on purpose, because Google and Microsoft sign-in refuse headless browsers. It
  needs a real GUI session — which an Aqua LaunchAgent has too, but so does
  this, and this one also has somewhere to put an icon.

launchd still has a job, just a smaller one: **Start at Login** writes
`~/Library/LaunchAgents/dev.eedeb.freeclaw.plist`, whose `RunAtLoad` starts the
menu bar app. Nothing calls `launchctl` in either direction — enabling it only
matters at the next login, and `bootout` on the way out would kill the running
app, which is not what unticking a checkbox should do.

### Updating

Settings → Update FreeClaw, or `~/.freeclaw/update-mac.sh`. Unlike Windows, the
server updates itself in place: the install is a git checkout with its own
updater sitting in it, exactly like the Linux one, so `Flask/main.py`
(`install_kind`) runs `update-mac.sh --no-restart` and the browser asks for a
restart when the log shows a clean finish.

A change to `mac/tray.py` itself only takes effect when the app is next
started — it is the one process an update cannot replace under itself. The
updater says so when that happens.

## The CLI

`freeclaw` opens the same conversation as the web UI, from any terminal:

```
freeclaw
freeclaw "Some User"
```

The installer writes it to `/usr/local/bin/freeclaw`, falling back to `sudo` and
then to `~/.local/bin/freeclaw` if you would rather not give a password. Pass
`--no-cli` to skip it; the web UI is unaffected either way.

## The app bundle

`~/Applications/FreeClaw.app` is generated by the installer, not checked in.
Its executable is a two-line shell script that runs `python/bin/python3
mac/tray.py`; the bundle exists for its `Info.plist` and its icon, and gives
FreeClaw a name in Spotlight, in Launchpad, and in the Login Items list.

`LSUIElement` in that plist is what keeps a background app out of the Dock.
`mac/tray.py` sets the same activation policy at runtime, for the times it is
started without going through the bundle — a developer running the file, or the
LaunchAgent pointing straight at the interpreter.

`~/Applications` and not `/Applications`: per-user, so the whole install needs
no administrator rights at any point.

## The icon

`freeclaw.png` (the menu bar) and `freeclaw.icns` (the bundle) are both
generated by `mac/make_icon.py`, which renders the mark defined in
`windows/make_icon.py` — three tapered talons in the accent lime, stdlib only,
no image dependency. One mark, two platforms, no chance of the two drifting
apart:

```bash
python3 mac/make_icon.py
```

It needs a full repo checkout, since a macOS install doesn't check out
`windows/`. It also takes about half a minute: the renderer is a pure-Python
supersampler and the 512px sizes are most of that.

The menu bar draws at the display's real pixel density — `RetinaIcon` in
`tray.py` renders at 2x and tells the `NSImage` it measures 22 points, because
pystray's own path builds a 22-*pixel* image for a 22-*point* slot and looks it
on every Mac made in the last decade.

## Platform differences worth knowing

- **`PATH` is resolved from your login shell.** A process launched by launchd at
  login — which is exactly what *Start at Login* sets up — inherits launchd's
  minimal `PATH`, with no `/opt/homebrew/bin` and no `~/.local/bin`. The same
  app launched from Finder gets the same treatment. FreeClaw would then start
  with none of the tools it spawns on its `PATH`: `node`/`npx` for stdio MCP
  servers, `uv`/`uvx`, and whatever the bash tool reaches for. The symptom is
  maddening — MCP servers that work when you start FreeClaw from a terminal and
  are "not installed" when it starts at login. `login_shell_path()` asks
  `$SHELL -l` once and hands the result to the server. `-l` and not `-i`:
  Homebrew's `shellenv` goes in a login profile, and an interactive shell can
  print banners or never return at all.

- **Signals arrive through Mach ports.** A Python signal handler runs between
  bytecodes on the main thread, and that thread spends its whole life inside
  Cocoa's event loop — a plain `signal.signal` handler would be recorded and
  never called. `PyObjCTools.MachSignals` is what makes `SIGTERM` reach the app,
  which is how the uninstaller stops it without orphaning the server. pystray
  does the same for ctrl-c.

- **The singleton is an flock, not a pid check.** `freeclaw.lock` is held open
  for the life of the process, so the kernel drops it however the process dies.
  A crash can never leave behind a file that keeps FreeClaw from starting again.
  (Windows uses a named mutex for the same reason.) Opening the app a second
  time opens the web UI instead of racing for port 6767.

- **Two pid files.** `freeclaw.pid` is the menu bar app, `freeclaw-server.pid`
  the server it supervises. The second exists because a supervisor killed
  outright leaves nothing to clean up after its child — and finding that child
  again without a pid would mean matching on an install path, which one quote in
  a username is enough to break.
