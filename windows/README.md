# FreeClaw on Windows

A native install: no WSL, no Docker Desktop, no Python of your own. The
installer drops a self-contained tree into `%LOCALAPPDATA%\FreeClaw` and adds
a notification-area app that keeps the server running.

```
FreeClaw-Setup-<version>.exe   →   %LOCALAPPDATA%\FreeClaw
                                     ├── python\        bundled interpreter + deps
                                     ├── Flask\ src\ models\
                                     ├── windows\tray.py
                                     ├── logs\
                                     └── .env           written on first install
```

## Installing

Run the installer. It asks for one thing — the password for the web UI — and
takes about a minute. No administrator rights are needed at any point.

> **SmartScreen.** The build is unsigned, so the first run shows *"Windows
> protected your PC"*. Click **More info → Run anyway**. Signing it needs a
> code-signing certificate (~$300–400/year); until then this is expected.

Afterwards FreeClaw appears in the notification area — the `^` chevron at the
right-hand end of the taskbar. Drag it onto the taskbar itself to keep it
visible.

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
the installer doesn't do it. From an elevated PowerShell:

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
| `0` | Clean shutdown | Stays stopped |
| anything else | Crash | Restarts with backoff, gives up after 5 and says so |

It is a tray app rather than a real Windows service because a service runs in
session 0, which has no desktop. FreeClaw's sign-in browser
(`src/browser_takeover.py`) launches Chromium headful on purpose — Google and
Microsoft sign-in refuse headless browsers — and headful needs a desktop.
Running in the interactive session also means stdio MCP servers can reach the
Node and Python you actually have installed.

## Uninstalling

Settings → Apps → FreeClaw → Uninstall.

**Your data stays.** Chats, uploads, `context.md`, saved browser logins, logs
and `.env` are created at runtime, so the uninstaller never tracked them and
never removes them. `%LOCALAPPDATA%\FreeClaw` will still be there afterwards —
delete it by hand if you want it gone.

Reinstalling over an existing install is likewise safe: `.env` is merged, not
overwritten, so your password, providers and MCP servers survive.

## Building the installer

Needs a Windows machine with [Inno Setup 6.3+](https://jrsoftware.org/isdl.php)
(`choco install innosetup -y`). Everything else is downloaded by the script.

```powershell
.\windows\build.ps1
```

The result lands in `dist\FreeClaw-Setup-<version>.exe`. To stage the tree
without compiling an installer — useful for running the tray straight out of
the build directory:

```powershell
.\windows\build.ps1 -SkipInstaller
```

Pass `-PythonSha256 <hash>` (from python.org's download page) for a build you
intend to sign, so a bad mirror can't end up inside it.

CI builds the same thing: `.github/workflows/build-windows.yml`, on manual
dispatch or a `v*` tag.

### Why embeddable Python and not PyInstaller

FreeClaw shells out to `sys.executable -m …` in two places that matter —
`src/mcp_client.py` spawns the built-in browser MCP server that way, and
`src/browser_setup.py` drives playwright with it. Frozen into a PyInstaller
executable, `sys.executable` is `FreeClaw.exe` and both calls become nonsense.
Bundling a real `python.exe` keeps them working with no changes.

### Regenerating the icon

`windows/freeclaw.ico` is generated, not drawn. It is nine sizes of a
three-talon mark in the app's own accent lime (`#c8f04a`), with fatter talons
below 24px so it survives the notification area.

```
python windows\make_icon.py
```

## Not done yet

The installer and the tray app are complete. The wider Windows port is not —
these are the known gaps, roughly in the order they will bite:

- **The bash tool runs under `cmd.exe`.** `src/agent.py` passes `shell=True`,
  which on Windows means `cmd`, so the `ls -la` / `grep` / `cat … | head` the
  model reaches for will fail. The intended fix is to point `executable=` at
  Git Bash, which is already on the machine if Git is.
- **`npx`/`uvx` MCP servers won't spawn.** They are `.cmd` shims, and
  `subprocess.Popen` won't find them without resolving through
  `shutil.which()` first (`src/mcp_client.py`). The built-in browser server is
  unaffected — it uses an absolute interpreter path.
- **`shlex.split()` mangles Windows paths.** It eats backslashes in POSIX
  mode, which affects both the MCP command line and — more seriously —
  `approvals.program_of()`, where a mis-parsed command could produce a saved
  approval rule that doesn't mean what the user thought they approved.
- **Conversation locking is weaker.** `src/users.py` degrades to atomic rename
  alone without `fcntl`; it wants an `msvcrt.locking()` branch. Separately,
  `os.replace` can raise `PermissionError` on Windows when another process
  holds the file open, so `_write_json_atomic` needs a retry loop.
- **No `freeclaw` CLI on PATH.** `src/cli.py` works, but nothing installs a
  shim for it, and its ANSI colours need VT processing enabled on older
  consoles.
