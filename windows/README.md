# FreeClaw on Windows

A native install: no WSL, no Docker Desktop, no Python of your own.
`install.ps1` drops a self-contained tree into `%LOCALAPPDATA%\FreeClaw` and
adds a notification-area app that keeps the server running.

```
github.com/eedeb/FreeClaw   →   %LOCALAPPDATA%\FreeClaw
                                  ├── python\        private interpreter + deps
                                  ├── Flask\ src\ models\   the clone
                                  ├── windows\tray.py
                                  ├── bin\freeclaw.cmd   CLI shim, put on PATH
                                  ├── logs\
                                  ├── .env               written on first install
                                  └── .freeclaw-install  marks this as an install
```

The install *is* a shallow clone of the repo, with a private Python beside it.
Nothing is published or hosted for it to work: the source comes from GitHub and
the interpreter from python.org, so there is no release to cut and no artifact
to upload.

## Installing

### The one-liner

```powershell
irm https://freeclaw.eedeb.dev/install.ps1 | iex
```

The recommended route, for one reason above all: **there is no security
prompt.** Mark of the Web is applied by the *browser*, so a build PowerShell
fetches never carries it and never trips SmartScreen — otherwise the single
biggest obstacle to an unsigned build, and one that reads to most people as
"the download is broken" rather than "Windows is asking a question".

**Git is the one prerequisite** — `winget install --id Git.Git -e` if you
haven't got it. FreeClaw's bash tool wants Git for Windows anyway.

What it does:

- clones the repo into `%LOCALAPPDATA%\FreeClaw`;
- downloads the embeddable Python from python.org into `python\` and installs
  the dependencies there. Private to the install, never added to PATH, and it
  does not touch any Python you already have — the same role install.sh's
  virtualenv plays on Linux;
- stops a running FreeClaw first, through `freeclaw.pid`, so it is never
  replacing files that are in use;
- generates a login password and prints it once. FreeClaw fails closed without
  `FC_PASSWORD` — an unset value can never match what you type — so one is
  invented rather than left blank. Pass `-Password` to choose your own;
- adds `freeclaw` to PATH and a Start Menu shortcut, and starts it.

A first install takes a few minutes, nearly all of it pip. Re-running it is the
update path and takes seconds: the clone is refreshed path by path (never
`Flask\static`, which is where the chats are) and Python is reused.

`-NoStart`, `-NoPath`, `-NoShortcut`, `-Autostart`, `-InstallDir` and `-Branch`
adjust the rest; piped through `iex` there is nowhere to put a parameter, so
each also reads an environment variable (`FREECLAW_DIR`, `FREECLAW_PASSWORD`,
and so on — see the header of [`install.ps1`](../install.ps1)).

Removing it:

```powershell
& "$env:LOCALAPPDATA\FreeClaw\uninstall.ps1"
```

`uninstall.ps1` ships inside the install rather than on the website — only
`install.ps1` is hosted — so removing FreeClaw needs no network at all, and
running the copy inside an install defaults to removing *that* install
whatever directory it is in. If an install is too broken to run its own copy,
`irm https://raw.githubusercontent.com/eedeb/FreeClaw/main/uninstall.ps1 | iex`
fetches it from the repo.

Your data stays unless you add `-Purge`. Shortcuts, the autostart entry and the
PATH entry are only removed if they actually point at the install being
removed — a second FreeClaw elsewhere is left alone.

For the agent's bash tool, also install
[Git for Windows](https://git-scm.com/download/win) if you don't already have
it. *Platform differences* below has what it's used for and what happens
without it; nothing else in FreeClaw depends on it.

Afterwards FreeClaw appears in the notification area — the `^` chevron at the
right-hand end of the taskbar. Drag it onto the taskbar itself to keep it
visible.

### Coming from the old .exe installer

Just run the one-liner. It installs into the same directory, keeps your `.env`
and chats, and clears out the Add/Remove Programs entry and `unins000.exe` the
old installer left behind — `uninstall.ps1` is the uninstaller from then on.

The `.exe` is gone for the reason above: unsigned, it was met with *"Windows
protected your PC"*, and a warning that hides its own Run button behind **More
info** reads as a broken download rather than a question. Signing it would need
a code-signing certificate; fetching it with PowerShell costs nothing and
sidesteps the warning entirely.

## Using it

**Click the icon** to open FreeClaw in your browser.

Right-click for the rest:

| Item | What it does |
|---|---|
| Open FreeClaw | `http://127.0.0.1:6767` |
| Copy address for other devices | Copies the LAN address, for your phone or another PC |
| Restart | Restarts the server — the same thing Settings → Restart does |
| Open logs folder | `logs\` — `freeclaw.log`, `tray.log`, `server-console.log` |
| Start with Windows | Toggles the autostart entry |
| Quit FreeClaw | Stops the server and removes the icon |

The first item is a live status line: *starting*, *running*, or *stopped*.

Two addresses, on purpose. The tray always opens loopback, because that works
whether or not the LAN address does — no firewall rule, a VPN, or a laptop
that changed networks since the server started will each break the LAN address
while FreeClaw itself is perfectly healthy. **Copy address for other devices**
is the one to send to your phone.

### Reaching it from other devices

Windows Defender blocks inbound connections to port 6767 by default, so the
LAN address won't work until you allow it. That needs administrator rights, so
the install doesn't do it. From an elevated PowerShell:

```powershell
New-NetFirewallRule -DisplayName "FreeClaw" -Direction Inbound -LocalPort 6767 -Protocol TCP -Action Allow -Profile Private
```

`-Profile Private` keeps it off public networks — don't drop that unless you
mean it.

## How it stays running

Linux has systemd and macOS has Docker's restart policy. On Windows the tray
app is the supervisor: it owns one `python -m Flask.main` child and puts it
back when it goes away. The exit code is the whole protocol.

| Exit code | Meaning | Tray does |
|---|---|---|
| `42` | Settings → Restart | Restarts immediately |
| `43` | Settings → Update FreeClaw | Runs install.ps1 again, then exits |
| `0` | Clean shutdown | Stays stopped |
| anything else | Crash | Restarts with backoff, gives up after 5 and says so |

`43` exists because the server cannot update itself here. There is no git
checkout and no `update.sh`; the install is a packaged tree, and half its files
are open in the process that would be replacing them. The supervisor is the one
thing not being replaced, so it starts `install.ps1` — the same script that
installed FreeClaw — in a detached PowerShell and then quits, which removes
`freeclaw.pid` and releases every file. The updater verifies its download,
replaces the program files, and starts a fresh tray at the end.

Detached, and in its own process group, for a specific reason: the updater
stops a running FreeClaw with `taskkill /T`, which walks the process tree. A
child of the tray would be inside that tree and would be killed halfway through
replacing the install.

Set `FC_UPDATE_URL` to point that at a fork, a staging host or an internal
mirror.

It is a tray app rather than a real Windows service because a service runs in
session 0, which has no desktop. FreeClaw's sign-in browser
(`src/browser_takeover.py`) launches Chromium headful on purpose — Google and
Microsoft sign-in refuse headless browsers — and headful needs a desktop.
Running in the interactive session also means stdio MCP servers can reach the
Node and Python you actually have installed.

## Uninstalling

Settings → Apps → FreeClaw → Uninstall.

The PATH entry is taken back out — that one entry, matched whole. Every other
entry keeps its text, its order and its `%VARIABLE%` references. The one thing
that does not survive is an empty entry (a stray `;;` or a trailing `;`), which
gets dropped in passing; Windows reads one of those as "the current
directory", so it is not a search path worth putting back.

**Your data stays.** Chats, uploads, `context.md`, saved browser logins, logs
and `.env` are created at runtime, so the uninstaller never tracked them and
never removes them. `%LOCALAPPDATA%\FreeClaw` will still be there afterwards —
delete it by hand if you want it gone.

Reinstalling over an existing install is likewise safe: `.env` is merged, not
overwritten, so your password, providers and MCP servers survive.

## Releasing

There is nothing to release. `install.ps1` clones whatever is on `main`, so a
push *is* the release — the next person to run the one-liner, and everyone who
re-runs it to update, gets it.

The only hosted file is `install.ps1` itself, at
`https://freeclaw.eedeb.dev/install.ps1`. Update that when the installer
changes; nothing else needs uploading, tagging or publishing.

## The CLI

`freeclaw` opens the same conversation as the web UI, from any console:

```
freeclaw
freeclaw "Some User"
```

The installer puts a shim in `%LOCALAPPDATA%\FreeClaw\bin` and adds *that one
directory* to your user PATH — not the install root, which would also hand
your shell `python.exe` and `tray.py`. Untick **Add the "freeclaw" command to
my PATH** during setup to skip it. A console opened before installing won't
have the new PATH; open a new one.

## Platform differences worth knowing

The port is complete — everything the Linux and macOS installs do, this one
does. Four things get there by a different route, and it's worth knowing which:

- **The bash tool runs under Git Bash.** `shell=True` on Windows means
  `cmd.exe`, which has no `ls -la`, no `grep`, and no `cat … | head` — the
  commands the model actually reaches for. `src/shell.py` finds Git Bash
  instead (the standard install locations, then `%LOCALAPPDATA%\Programs\Git`,
  then derived from `git` on PATH) and runs `bash -c` directly. Note it is not
  `executable=`: with `shell=True` subprocess builds `<executable> /c
  <command>`, and `/c` is a path to bash, not a flag. **Without Git installed
  this falls back to `cmd.exe`** and the bash tool becomes close to useless —
  the log says so at startup. Nothing else depends on it.

  Deliberately not `shutil.which("bash")`: on a stock Windows that finds the
  WindowsApps stub, which launches WSL — a different filesystem, where none of
  the user's files are where the model expects them.

- **MCP launchers are resolved before spawning.** `npx`, `uvx` and friends
  install as `.cmd` shims, and `CreateProcess` applies no PATHEXT search, so a
  bare `npx` is simply not found. `shell.resolve_program()` puts the real
  filename in argv[0] first. The built-in browser server never needed it — it
  names an absolute interpreter path.

- **Command lines are split by Windows' rules, not POSIX ones.** POSIX `shlex`
  reads backslashes as escapes, so `C:\Users\me\docs` comes back as
  `C:Usersmedocs`. Wrong for an MCP server's arguments, and worse than wrong in
  `approvals.program_of()`, where the mangled token is what gets saved as an
  allow-rule — the rule you approved would not be the rule that was written.
  `shell.split_command()` implements the `CommandLineToArgvW` backslash/quote
  rules instead; it is checked against the OS parser itself and agrees on every
  token position but the first (see its docstring for why the first differs).

- **Conversation locking uses `msvcrt`, not `fcntl`.** Same guarantee, taken on
  the same sidecar file. Two differences behind it: `msvcrt.locking()` has no
  block-forever mode, so it is retried to a timeout and then proceeds without
  the lock rather than losing the turn; and `os.replace` can fail with
  `PermissionError` while any other process has the file open — a page
  refresh, the ping scheduler, an antivirus scanner — so the rename is
  retried too.

Beyond those, `src/cli.py` switches on the console's
ENABLE_VIRTUAL_TERMINAL_PROCESSING at startup, without which conhost prints
`←[38;5;154m` in front of every line instead of colouring it.
