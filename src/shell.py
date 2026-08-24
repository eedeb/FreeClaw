"""Spawning helpers whose right answer differs between POSIX and Windows.

Three of them, each needed by more than one caller:

* `split_command` — turn a command line into argv. POSIX `shlex` eats the
  backslashes out of Windows paths, which is wrong in src/mcp_client.py and
  unsafe in src/approvals.py.
* `resolve_program` — find the executable argv[0] names. On Windows the
  interesting MCP launchers (`npx`, `uvx`) are `.cmd` shims that
  `subprocess.Popen` won't find on its own.
* `bash_argv` — how to run the bash tool's command line, since `shell=True`
  on Windows means `cmd.exe`.

Everything here behaves as before on POSIX, so callers don't branch.
"""

import os
import shlex
import shutil

from src.logging_setup import get_logger

logger = get_logger(__name__)

IS_WINDOWS = os.name == "nt"


# ── command lines → argv ─────────────────────────────────────

def split_command(command):
    r"""Split a command line into argv the way this platform's spawn does.

    On POSIX that is `shlex.split`. On Windows it can't be: POSIX shlex reads
    a backslash as an escape, so every unquoted path loses its separators —
    `C:\Users\me\docs` comes back as `C:Usersmedocs`. That silently breaks an
    MCP server's arguments, and in `approvals.program_of` it is worse than
    broken: the mangled token is what gets saved as an allow-rule, so the rule
    the user thinks they approved is not the one that was written.

    So Windows gets the parser Windows actually uses — the backslash/quote
    rules of `CommandLineToArgvW`, which is what a spawned process re-parses
    its own command line with. Reimplemented rather than reached through
    ctypes so it stays testable off Windows.

    One deliberate divergence: `CommandLineToArgvW` parses the *first* token by
    a looser rule than the rest (backslashes never escape there, and leading
    whitespace yields an empty argv[0]). That rule exists to find an .exe on
    disk, not to express intent, and applying it here would mean
    `"C:\dir\\" next` splitting differently depending on its position. The
    standard rules are applied uniformly instead. Every token position but the
    first matches the OS exactly.

    Raises ValueError on an unbalanced quote, matching `shlex.split`. (The OS
    parser instead closes the quote at end-of-string; refusing is the safer
    answer for a command line that is about to become an approval rule.)
    """
    if not IS_WINDOWS:
        return shlex.split(command)
    return _split_windows(command)


def _split_windows(command):
    argv = []
    token = []
    in_token = False
    in_quotes = False
    backslashes = 0
    i = 0

    while i < len(command):
        ch = command[i]
        if ch == "\\":
            backslashes += 1
            in_token = True
        elif ch == '"':
            # 2n backslashes before a quote are n literal backslashes and the
            # quote keeps its meaning; 2n+1 are n backslashes and a literal
            # quote. This is the rule the C runtime and CommandLineToArgvW
            # share, and the reason `"C:\path\"` reads as a still-open quote
            # rather than a closed string.
            token.append("\\" * (backslashes // 2))
            if backslashes % 2:
                token.append('"')
            elif in_quotes and i + 1 < len(command) and command[i + 1] == '"':
                # "" inside a quoted run is one literal quote — and it ends the
                # quoted run, so a following `"` opens a fresh one. Verified
                # against CommandLineToArgvW rather than assumed: the MSVC
                # runtime's own parser keeps the run open here, and the two
                # disagree on `"a""b" c`.
                token.append('"')
                in_quotes = False
                i += 1
            else:
                in_quotes = not in_quotes
            backslashes = 0
            in_token = True
        elif ch in " \t" and not in_quotes:
            token.append("\\" * backslashes)
            backslashes = 0
            if in_token:
                argv.append("".join(token))
                token = []
                in_token = False
        else:
            token.append("\\" * backslashes)
            backslashes = 0
            token.append(ch)
            in_token = True
        i += 1

    token.append("\\" * backslashes)
    if in_quotes:
        raise ValueError("No closing quotation")
    if in_token:
        argv.append("".join(token))
    return argv


# ── finding the program ──────────────────────────────────────

def resolve_program(program):
    """Absolute path to `program`, or `program` unchanged if it isn't found.

    Only Windows needs this. `npx`, `uvx`, `pnpm` and friends install as
    `.cmd` shims, and a bare `npx` in argv[0] is not something the spawn will
    resolve on its own — `shutil.which` applies PATHEXT and turns it into a
    real filename. Returning the input unchanged on a miss keeps the "isn't
    installed" error coming from the spawn, where the message already explains
    what to install.
    """
    if not IS_WINDOWS or os.path.dirname(program):
        return program
    return shutil.which(program) or program


# ── the bash tool's shell ────────────────────────────────────

# Where Git for Windows puts bash, most likely first. Deliberately not
# `shutil.which("bash")`: on a stock Windows install that resolves to the
# WindowsApps stub, which either launches WSL — a different filesystem, with
# none of the user's files where the model expects them — or opens the
# Microsoft Store.
_GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)

_bash_shell_cached = False
_bash_shell = None


def bash_shell():
    """Path to a POSIX shell for the bash tool, or None if there isn't one.

    None on POSIX too, where `shell=True` already means a POSIX shell and
    there is nothing to look up.
    """
    global _bash_shell_cached, _bash_shell
    if _bash_shell_cached:
        return _bash_shell
    _bash_shell_cached = True
    if not IS_WINDOWS:
        return None
    _bash_shell = _find_bash()
    if _bash_shell:
        logger.info("bash tool will run under %s", _bash_shell)
    else:
        logger.warning(
            "No Git Bash found — the bash tool falls back to cmd.exe, where the "
            "ls/grep/cat the model reaches for do not exist. Install Git for "
            "Windows (https://git-scm.com/download/win) to fix it.")
    return _bash_shell


def _find_bash():
    for path in _GIT_BASH_CANDIDATES:
        if os.path.isfile(path):
            return path

    # %LOCALAPPDATA%\Programs\Git — where the per-user (no-admin) Git
    # installer puts it, which is the one someone who couldn't run the
    # machine-wide installer will have. FreeClaw itself installs per-user for
    # the same reason, so this is not an unlikely machine.
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = os.path.join(local, "Programs", "Git", "bin", "bash.exe")
        if os.path.isfile(candidate):
            return candidate

    # Last resort: derive it from git itself, which covers a Git installed
    # somewhere non-standard or by a package manager. git.exe sits in
    # <root>\cmd or <root>\mingw64\bin; bash is always <root>\bin\bash.exe.
    git = shutil.which("git")
    if git:
        directory = os.path.dirname(git)
        for root in (os.path.dirname(directory),
                     os.path.dirname(os.path.dirname(directory))):
            candidate = os.path.join(root, "bin", "bash.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


def bash_argv(command):
    r"""Popen arguments for running `command` as a shell command line.

    Returns `(args, kwargs)` to hand to `subprocess.Popen`.

    On POSIX this is `shell=True` — /bin/sh, as it has always been. On Windows
    `shell=True` means `cmd.exe`, which understands none of the `ls -la`,
    `grep`, `cat … | head` the model writes, so the shell is named explicitly
    instead.

    `executable=` is not the way to name it. With `shell=True` on Windows,
    subprocess builds `<executable> /c <command>` — and `/c` is not a bash
    flag, it is a path, so bash would try to run a script called `/c` and
    ignore the command entirely. Passing argv directly is what works.
    """
    shell = bash_shell()
    if shell is None:
        return command, {"shell": True}
    # No -l: a login shell re-reads the user's profile on every single command,
    # which is slow and can print a banner into the tool output. Git Bash puts
    # its own bin directories on PATH without one.
    return [shell, "-c", command], {"shell": False}
