"""FreeClaw's Developer tools, as an MCP server the agent codes with.

Run as `python -m src.dev_server` (src/mcp_client.py: BUILTIN_SERVERS). One
child per FreeClaw user, each pinned to that user's own workspace,
``Flask/static/<user>/files/projects/``, through FC_DEV_WORKSPACE (see
mcp_client.for_user). The Developer app (Flask/templates/developer.html) is the
same folder seen by a person: an editor, a file list and a Run button.

**Built to be cheap to drive.** A coding task is mostly reading and editing,
and every byte a tool returns is resent with every later step of the turn. So
nothing here hands back a whole file or a whole log: `read` gives a numbered
slice and says how long the file is, `edit` replaces one exact string and
answers in a line, `search` answers in `path:line: text`, and `run` keeps the
head and the tail of the output and says how much it cut. The descriptions are
one line each for the same reason: they ride along on every request.

**Confined to the workspace.** Every path is resolved, symlinks included,
before it's compared against the workspace, and anything that lands outside it
is refused — an absolute path, a `..`, a link pointing out. That's a fence for
the file tools, not a sandbox for `run`: an approved command can do anything
the FreeClaw process can, exactly like `run_bash_command`.

**Never runs what didn't come through the agent.** The only thing that talks
to this process is FreeClaw, over a pipe private to the two of them, and the
only caller that sends `run` is agent.py, after the same approval gate as
`run_bash_command` (`command_line()` below is what that gate shows and checks,
so what's approved is what runs). The Developer app's own Run button doesn't
come here at all — Flask runs the person's file itself, with the helpers below.

Like the browser server this speaks newline-delimited JSON-RPC itself rather
than pulling in the `mcp` package, and nothing may write to stdout except
protocol messages. Standard library only.
"""

import base64
import fnmatch
import functools
import http.server
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import src.dev_check as dev_check
import src.dev_git as dev_git
import src.dev_map as dev_map

# ── limits ───────────────────────────────────────────────────
#
# Each is a cap on what one tool result can cost, not on what the agent can
# do: every result that's cut says so and says how to get the rest.

# read: lines per call when the agent doesn't say, and the most it may ask
# for. A line is cut at READ_LINE_CHARS — a minified bundle is one line.
READ_DEFAULT_LINES = 120
READ_MAX_LINES = 500
READ_LINE_CHARS = 400

# ls: how deep, and how many entries before it stops listing.
LS_DEFAULT_DEPTH = 2
LS_MAX_DEPTH = 5
LS_MAX_ENTRIES = 200
# More files than this of one kind in a folder are one line ("480 .jpg files:
# 0000.jpg … 0479.jpg"): rendered frames, a dataset or a photo dump would
# otherwise fill the listing with names nobody reads. ls with all names each.
LS_GROUP_OVER = 20

# search: hits per call, characters of each hit's line, and the largest file
# it bothers to open (anything bigger is data, not source).
SEARCH_MAX_HITS = 50
SEARCH_LINE_CHARS = 200
SEARCH_MAX_FILE_BYTES = 1_000_000

# write: the largest file the agent may write in one go.
WRITE_MAX_BYTES = 1_000_000

# run: what's kept of the combined output — the start (where the usage error
# or the first traceback is) and the end (where the result or the last
# traceback is).
OUTPUT_HEAD = 3000
OUTPUT_TAIL = 3000

# run: seconds. The default sits under mcp_client's 60s read timeout for a call
# that names no wait of its own; a call that passes `timeout` gets that long
# plus headroom from the client (mcp_client._read_timeout_for), whose own
# ceiling is 600s — so MAX_TIMEOUT is that less the headroom.
DEFAULT_TIMEOUT = 45
MAX_TIMEOUT = 580

# Background jobs (start_job / check_job / stop_job): runs the agent starts
# and comes back to. A job nobody has checked on for JOB_IDLE_SECONDS is
# stopped (a job nobody is watching is a job nobody needs), and every job
# ends at JOB_MAX_SECONDS and with this process.
JOB_IDLE_SECONDS = int(os.environ.get("FC_DEV_JOB_IDLE_SECONDS", "1800"))
JOB_MAX_SECONDS = int(os.environ.get("FC_DEV_JOB_MAX_SECONDS", str(6 * 3600)))
JOB_MAX_RUNNING = int(os.environ.get("FC_DEV_JOB_MAX_RUNNING", "3"))
JOB_OUTPUT_LINES = 2000
JOB_WAIT_MAX = 300

# Folders nobody wants listed or searched: version control, dependencies,
# virtualenvs, caches and build output. A large node_modules alone would eat
# every entry ls is allowed.
SKIP_DIRS = frozenset((
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".env", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache",
    "dist", "build", ".next", ".nuxt", "target", ".gradle", ".idea", ".vscode",
))

_TRUTHY = ("1", "true", "yes", "on")


def _log(message):
    """Stderr, never stdout — see the module docstring."""
    print(f"[dev-server] {message}", file=sys.stderr, flush=True)


class ToolError(Exception):
    """A mistake the model can fix — reported to it, not logged as a crash."""


# ── the off switch ───────────────────────────────────────────

def disabled():
    """Whether this install has the Developer app switched off.

    `FC_DISABLE_DEVELOPER=1` switches it off, and so does an install that has
    bash switched off: `run` is a shell by another name, so a host that took
    the bash tool away hasn't taken anything away while this is still offered.
    Where src/shell.py has a `bash_disabled()` it's what decides that;
    otherwise FC_DISABLE_BASH is read the same way it would be.

    Read from the process environment at every call rather than once at
    import, so the answer is the same whichever module asks first. Unset — a
    self-hosted install — means on."""
    if os.getenv("FC_DISABLE_DEVELOPER", "").strip().lower() in _TRUTHY:
        return True
    try:
        import src.shell as shell
        bash_disabled = getattr(shell, "bash_disabled", None)
    except Exception:                                        # noqa: BLE001
        bash_disabled = None
    if bash_disabled is not None:
        return bool(bash_disabled())
    return os.getenv("FC_DISABLE_BASH", "").strip().lower() in _TRUTHY


# ── the workspace ────────────────────────────────────────────

def workspace():
    """The workspace this process serves, created if it's missing. A
    ToolError when FreeClaw didn't pass one — the tools/list child FreeClaw
    starts without a user has none, and never needs one."""
    root = (os.environ.get("FC_DEV_WORKSPACE") or "").strip()
    if not root:
        raise ToolError("No workspace for this user. Ask them to open the Developer app.")
    os.makedirs(root, exist_ok=True)
    return os.path.realpath(root)


# Where the workspace is in the person's files: mcp_client.for_user gives
# every user files/projects/. Every path these tools show starts with it, and
# every path they take may, so a file has the same name here as in Files and
# in the agent's other file tools.
FILES_FOLDER = "projects"


def _inner(rel):
    """`rel` inside the workspace: a path the tools showed (projects/a.py, or
    files/projects/a.py) without its projects/; anything else as it was."""
    parts = [p for p in re.split(r"[\\/]+", rel) if p not in ("", ".")]
    if parts[:2] == ["files", FILES_FOLDER]:
        parts = parts[1:]
    if parts[:1] == [FILES_FOLDER]:
        return "/".join(parts[1:]) or "."
    return rel


def shown_path(rel):
    """A path inside the workspace (either form) as the tools show it."""
    inner = _inner(str(rel or "")).strip("/")
    return FILES_FOLDER if inner in ("", ".") else f"{FILES_FOLDER}/{inner}"


def files_top(root):
    """The top of the person's files: the folder the workspace is projects/
    of, and where an agent's run starts, so the paths in its commands are the
    paths every tool shows."""
    return os.path.dirname(os.path.normpath(root))


def resolve(root, rel, must_exist=False, shown=True):
    """Absolute path of `rel` inside `root`, or a ToolError.

    `rel` is a path as the tools show it (projects/…), or one relative to
    the workspace. `shown=False` takes it only as the latter: the Developer
    app's page names files relative to the workspace, where a folder called
    projects at its top is just that folder.

    Refused before anything is resolved: an absolute path, a drive letter, a
    NUL, and any `..` segment — the agent never needs one, and a path that
    climbs out and back in is one that was going somewhere else. Then both
    sides are resolved (realpath) and compared, so a symlink inside the
    workspace can't be followed out of it either."""
    rel = "" if rel is None else str(rel)
    rel = rel.strip()
    if "\x00" in rel:
        raise ToolError("Bad path.")
    if rel in ("", "."):
        return root
    if os.path.isabs(rel) or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        raise ToolError(f"{rel}: use a path relative to the workspace, like src/app.py.")
    if ".." in re.split(r"[\\/]", rel):
        raise ToolError(f"{rel}: '..' isn't allowed; paths stay inside the workspace.")
    asked, rel = rel, (_inner(rel) if shown else rel)
    if rel == ".":
        return root
    full = os.path.realpath(os.path.join(root, rel))
    root_cmp, full_cmp = os.path.normcase(root), os.path.normcase(full)
    if full_cmp != root_cmp and not full_cmp.startswith(root_cmp.rstrip(os.sep) + os.sep):
        raise ToolError(f"{rel}: points outside the workspace.")
    if must_exist and not os.path.exists(full):
        raise ToolError(f"{asked}: not found. Use ls or search to find it.")
    return full


def inner_rel(root, path):
    """`path` relative to the workspace, forward slashes."""
    rel = os.path.relpath(path, root).replace(os.sep, "/")
    return "." if rel == "." else rel


def rel_of(root, path):
    """`path` as the tools show it: its path in the person's files."""
    rel = inner_rel(root, path)
    return FILES_FOLDER if rel == "." else f"{FILES_FOLDER}/{rel}"


def _is_text(path):
    """A file that's valid-looking UTF-8 with no NULs in its first chunk."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
    except OSError:
        return False
    return b"\x00" not in chunk


# Images `read` hands back as the picture itself, for the model to look at:
# a post or graphic it rendered (check's save, or a script), a photo.
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
# Bigger than this is shrunk to fit (it's resent with every request while it
# stays in the conversation), keeping its shape. Without Pillow it's sent as
# it is, up to VIEW_MAX_BYTES.
VIEW_MAX_EDGE = 1600
VIEW_MAX_BYTES = 4 * 1024 * 1024


def _view_image(path):
    """(bytes, mime type, "WxH" or "") of an image file, shrunk if large."""
    mime = IMAGE_TYPES[os.path.splitext(path)[1].lower()]
    with open(path, "rb") as f:
        data = f.read(VIEW_MAX_BYTES * 8 + 1)
    try:
        from PIL import Image
    except ImportError:
        if len(data) > VIEW_MAX_BYTES:
            raise ToolError(f"{os.path.basename(path)} is too big to look at "
                            f"({len(data) // 1024:,} KB; up to {VIEW_MAX_BYTES // 1024:,} KB).")
        return data, mime, ""
    import io
    try:
        with Image.open(io.BytesIO(data)) as im:
            size = f"{im.width}x{im.height}"
            if max(im.size) <= VIEW_MAX_EDGE and len(data) <= VIEW_MAX_BYTES:
                return data, mime, size
            im.seek(0)
            im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
            im.thumbnail((VIEW_MAX_EDGE, VIEW_MAX_EDGE))
            out = io.BytesIO()
            if im.mode == "RGBA":
                im.save(out, "PNG", optimize=True)
                mime = "image/png"
            else:
                im.save(out, "JPEG", quality=88)
                mime = "image/jpeg"
            return out.getvalue(), mime, size + f" (shown at {im.width}x{im.height})"
    except (OSError, ValueError, Image.DecompressionBombError):
        raise ToolError(f"{os.path.basename(path)} couldn't be opened as an image.")


def _read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def _write_text(path, text):
    """Write atomically: a temp file beside it, then a rename, so the editor
    polling this file never loads it half-written."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def _notes(path, text, long_lines=True):
    """What's worth knowing straight after a write or edit: a syntax error
    (dev_check.syntax), and lines too long for read to show whole."""
    out = []
    problem = dev_check.syntax(path, text)
    if problem:
        out.append(f"\n{problem}")
    if long_lines:
        long = sum(1 for line in text.splitlines() if len(line) > READ_LINE_CHARS)
        if long:
            out.append(f"\nNote: {long} line{'s are' if long > 1 else ' is'} over {READ_LINE_CHARS} chars, "
                       "which read cuts off. One statement per line keeps the code readable and editable.")
    return "".join(out)


def _int(value, name, default, low, high):
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a whole number.")
    return max(low, min(n, high))


# ── output trimming ──────────────────────────────────────────

class Trimmer:
    """Keeps the first OUTPUT_HEAD and last OUTPUT_TAIL characters of a stream
    and counts what fell between.

    Shared by the agent's `run`, which only wants `text()` at the end, and the
    Developer app's Run button, which streams: `feed()` returns the part of a
    chunk that's still inside the head, to show live, and `tail()` is what to
    show once the process ends. Either way the person and the agent see the
    same cap."""

    def __init__(self, head=OUTPUT_HEAD, tail=OUTPUT_TAIL):
        self.head_cap, self.tail_cap = head, tail
        self.head = ""
        self._tail = ""
        self.cut = 0

    def feed(self, chunk):
        room = self.head_cap - len(self.head)
        live = chunk[:max(room, 0)]
        self.head += live
        rest = chunk[len(live):]
        if rest:
            self._tail += rest
            if len(self._tail) > self.tail_cap:
                self.cut += len(self._tail) - self.tail_cap
                self._tail = self._tail[-self.tail_cap:]
        return live

    def tail(self):
        """What follows the head: the cut note, if anything was cut, and the
        tail."""
        if not self._tail:
            return ""
        note = f"\n[… {self.cut:,} characters cut …]\n" if self.cut else ""
        return note + self._tail

    def text(self):
        return self.head + self.tail()


# ── running ──────────────────────────────────────────────────

PYTHON_EXTS = (".py",)
SHELL_EXTS = (".sh", ".bash")


def python_exe():
    """The Python a project runs under: the one on PATH, as the person would
    get typing `python3`, rather than FreeClaw's own venv — their script
    shouldn't see FreeClaw's packages, or break when FreeClaw's change. Falls
    back to FreeClaw's interpreter where there's no other."""
    return shutil.which("python3") or shutil.which("python") or sys.executable


def bash_exe():
    return shutil.which("bash")


# The names a model reaches for when it doesn't recall the schema, mapped to
# the ones the tools take: one wasted step each time otherwise. Only filled in
# when the real name is absent; anything else unknown is still refused.
_ALIASES = {
    "path": ("filename", "file", "file_path", "filepath", "dir", "directory", "folder"),
    "old": ("old_string", "old_str", "old_text", "find", "search"),
    "new": ("new_string", "new_str", "new_text", "replace", "replacement"),
    "start": ("offset", "line", "from", "start_line"),
    "lines": ("limit", "count", "num_lines"),
    "pattern": ("query", "regex"),
}
_GIT_ALIASES = {"repo": ("path", "folder", "dir", "repository")}
_RUN_ALIASES = {"command": ("cmd", "bash", "shell"), "file": ("script",),
                "where": ("path", "cwd", "dir", "directory", "folder", "workdir")}


def normalize(name, args):
    """`args` with the aliases above put under their real names."""
    args = dict(args) if isinstance(args, dict) else {}
    table = (_RUN_ALIASES if name == "run" else _GIT_ALIASES if name.startswith("git_")
             else _ALIASES)
    for real, others in table.items():
        if real in args:
            continue
        for other in others:
            if other in args:
                args[real] = args.pop(other)
                break
    return args


def command_line(args, files=True):
    """(command line, error) for a `run` call: the exact text the approval
    gate shows and saved rules are matched against, or the reason the call is
    malformed. agent.py calls this before asking anyone anything, and
    `argv_for` builds what actually runs from the same arguments, so the two
    can't describe different things.

    A file runs as `python <file> <args>` (or `bash <file> <args>`); a command
    is shown exactly as given. The agent's runs start at the top of the
    person's files (`files`, files_top), so a file or folder is named the way
    every tool shows it, projects/…; the Developer app's Run button
    (files=False) runs from the workspace and names files inside it. A folder
    to run in is said in the command, `cd projects/app && …`, never as a
    separate working directory: that would be one more thing that changes what
    runs without changing the text a saved rule matches.

    Anything else in `args` is refused rather than ignored, for the same
    reason."""
    args = normalize("run", args)
    unknown = set(args) - {"command", "file", "args", "timeout", "where"}
    if unknown:
        return None, f"Unknown argument: {', '.join(sorted(unknown))}."
    # A folder to run from (the model passes path or cwd): said in the command,
    # `cd <folder> && …`, so the approval card and saved rules see it too.
    where = args.get("where")
    if where is not None:
        if not isinstance(where, str):
            return None, "path (the folder to run in) must be a folder name, like app."
        where = where.strip()
        if os.path.isabs(where) or ".." in re.split(r"[\\/]", where):
            return None, "path (the folder to run in) must be a folder inside the workspace, like app."
        given = where.strip("/")
        where = _inner(given).strip("/")
        if files and given not in ("", "."):
            # The workspace itself (projects) is a folder to cd into from
            # the top of their files, like any other.
            where = shown_path(where)
        elif where in ("", "."):
            where = None
    command = args.get("command")
    file = args.get("file")
    extra = args.get("args")
    if command and file:
        return None, "Pass command or file, not both."
    if file:
        if not isinstance(file, str):
            return None, "file must be a path string."
        file = file.strip()
        if not where:
            file = shown_path(file) if files else _inner(file)
        if extra is None:
            extra = []
        if not isinstance(extra, list) or not all(isinstance(a, str) for a in extra):
            return None, "args must be a list of strings."
        ext = os.path.splitext(file)[1].lower()
        if ext in PYTHON_EXTS:
            runner = "python"
        elif ext in SHELL_EXTS:
            runner = "bash"
        else:
            return None, f"{file}: only .py and .sh files run directly; use command for anything else."
        line = " ".join([runner] + [shlex.quote(a) for a in [file] + extra])
        return (f"cd {shlex.quote(where)} && {line}" if where else line), None
    if isinstance(command, str) and command.strip():
        if extra:
            return None, "args go with file; put them in the command instead."
        return (f"cd {shlex.quote(where)} && {command.strip()}" if where else command.strip()), None
    return None, "Pass command (a bash command line) or file (a .py or .sh to run)."


def argv_for(root, args, files=True):
    """(argv or command string, popen kwargs) to run a `run` call's arguments,
    from files_top(root) (or, files=False, from `root`). Validated the same
    way as `command_line`."""
    shown, error = command_line(args, files)
    if error:
        raise ToolError(error)
    args = normalize("run", args)
    file = _inner(args["file"].strip()) if isinstance(args.get("file"), str) else args.get("file")
    where = _inner((args.get("where") or "").strip().strip("/")).strip("/")
    if where and where != ".":
        # Run exactly the line the approval showed: `cd <folder> && …`.
        folder = resolve(root, where, must_exist=True)
        if not os.path.isdir(folder):
            raise ToolError(f"{where}: not a folder.")
        if file:
            # Named from that folder, as the command says (cd folder && …).
            resolve(folder, args["file"].strip(), must_exist=True, shown=False)
        return shell_argv(shown)
    if file:
        path = resolve(root, file, must_exist=True)
        if not os.path.isfile(path):
            raise ToolError(f"{file}: not a file.")
        ext = os.path.splitext(path)[1].lower()
        runner = python_exe() if ext in PYTHON_EXTS else (bash_exe() or "sh")
        return [runner, path] + list(args.get("args") or []), {}
    return shell_argv(shown)


def shell_argv(command):
    """A bash command line as Popen arguments. Bash itself where there is one
    (the tool is called a bash command, and `[[`, arrays and `source` are what
    the model writes); src/shell.py's answer otherwise, which is Git Bash on
    Windows and /bin/sh on a POSIX box with no bash."""
    bash = bash_exe()
    if bash and os.name != "nt":
        return [bash, "-c", command], {}
    import src.shell as shell
    return shell.bash_argv(command)


# The folder holding freeclaw_tools.py, put first on a run's PYTHONPATH.
RUN_HELPERS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_helpers")


class RunTools:
    """One run's way to the agent's connected apps (src/workbench.py): a Unix
    socket of its own, handed to the program as FREECLAW_TOOLS. Every request
    through it is stamped here with the user this Developer process serves
    (FC_DEV_USER, from FreeClaw), whatever the request says, and passed on to
    FreeClaw's workbench socket (FC_DEV_TOOLS_SOCKET). Gone with the run."""

    MAX_REQUEST = 1024 * 1024

    def __init__(self, user, target):
        import tempfile
        self.user, self.target = user, target
        self.folder = tempfile.mkdtemp(prefix="fc-run-tools-")
        os.chmod(self.folder, 0o700)
        self.path = os.path.join(self.folder, "tools.sock")
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(self.path)
        self.server.listen(8)
        self.closed = False
        threading.Thread(target=self._accept, daemon=True, name="run-tools").start()

    def _accept(self):
        while not self.closed:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(30)
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
                if len(data) > self.MAX_REQUEST:
                    return
            try:
                request = json.loads(data.decode("utf-8"))
            except ValueError:
                request = None
            if not isinstance(request, dict):
                conn.sendall(b'{"ok": false, "error": "Bad request."}\n')
                return
            request = {"op": request.get("op"), "tool": request.get("tool"),
                       "arguments": request.get("arguments"), "user": self.user}
            up = socket.socket(socket.AF_UNIX)
            up.settimeout(150)
            try:
                up.connect(self.target)
                up.sendall(json.dumps(request).encode() + b"\n")
                reply = b""
                while not reply.endswith(b"\n"):
                    chunk = up.recv(65536)
                    if not chunk:
                        break
                    reply += chunk
            except OSError:
                reply = b'{"ok": false, "error": "Connected apps are unavailable right now."}\n'
            finally:
                up.close()
            conn.settimeout(60)
            conn.sendall(reply if reply.endswith(b"\n") else reply + b"\n")
        except Exception:                                    # noqa: BLE001 — one request
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self.closed = True
        try:
            self.server.close()
        except OSError:
            pass
        shutil.rmtree(self.folder, ignore_errors=True)


def run_tools():
    """A RunTools for the run about to start, or None where there's no
    workbench to reach (no user, no socket, no Unix sockets)."""
    user = (os.environ.get("FC_DEV_USER") or "").strip()
    target = (os.environ.get("FC_DEV_TOOLS_SOCKET") or "").strip()
    if not user or not target or not hasattr(socket, "AF_UNIX") or os.name == "nt":
        return None
    try:
        return RunTools(user, target)
    except OSError as e:
        _log(f"no connected apps for this run: {e}")
        return None


def spawn(argv, cwd, popen_kwargs=None):
    """Start a run with stdout and stderr merged, stdin closed, and on POSIX
    in a session of its own so a timeout or Stop ends everything it started,
    not just the shell. The program can reach the agent's connected apps
    (freeclaw_tools, RunTools) for as long as it runs."""
    kwargs = dict(popen_kwargs or {})
    if os.name == "nt":
        kwargs.setdefault("creationflags", getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                          | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    # The workspace is this process's business, not the program's; the GitHub
    # token is git's (src/dev_git.py), never the program's; the workbench's
    # own socket and user are RunTools', which the program gets instead.
    for key in ("FC_DEV_WORKSPACE", "FC_DEV_GITHUB_TOKEN", "FC_DEV_USER", "FC_DEV_TOOLS_SOCKET"):
        env.pop(key, None)
    tools = run_tools()
    if tools is not None:
        env["FREECLAW_TOOLS"] = tools.path
        env["PYTHONPATH"] = RUN_HELPERS + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env, **kwargs)
    except BaseException:
        if tools is not None:
            tools.close()
        raise
    if tools is not None:
        def close_when_done():
            while proc.returncode is None:
                time.sleep(0.5)
            tools.close()
        threading.Thread(target=close_when_done, daemon=True, name="run-tools-close").start()
    return proc


def kill(proc):
    """End `proc` and everything it started."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


# What a run's report lists of the files it wrote: this many lines at most, a
# folder of more than RUN_FILES_GROUP new files as one line, and no more than
# RUN_FILES_SCAN entries looked at (a huge workspace just gets a shorter list).
RUN_FILES_LINES = 12
RUN_FILES_GROUP = 4
RUN_FILES_SCAN = 40000
# Not looked in for new files: version control, dependencies and caches.
# (Unlike ls, a build/ or dist/ a run writes *is* reported.)
_RUN_FILES_SKIP = SKIP_DIRS - {"dist", "build", "target"}


def _fmt_size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def snapshot(root):
    """{path relative to `root`: (mtime_ns, size)} of the files under it (a
    run's: the top of their files, so these are the paths the tools show),
    and whether the
    scan stopped early: what a run's report is measured against."""
    state, seen = {}, 0
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _RUN_FILES_SKIP
                   and not os.path.islink(os.path.join(folder, d))]
        for name in files:
            seen += 1
            if seen > RUN_FILES_SCAN:
                return state, True
            full = os.path.join(folder, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            state[inner_rel(root, full)] = (st.st_mtime_ns, st.st_size)
    return state, False


def written(before, after):
    """[(rel path, size)] of the files in `after` that are new or changed
    since `before`."""
    return [(rel, size) for rel, (mtime, size) in after.items()
            if before.get(rel) != (mtime, size)]


def files_report(found, partial=False):
    """What a run's result says about the files it wrote, each by the path
    the tools show, so it can be opened, checked or sent from any of them."""
    if not found:
        return ""
    groups = {}
    for rel, size in found:
        groups.setdefault(os.path.dirname(rel), []).append((rel, size))
    lines = []
    for folder in sorted(groups, key=lambda f: (f.count("/"), f)):
        items = sorted(groups[folder])
        if len(items) > RUN_FILES_GROUP:
            total = sum(size for _, size in items)
            lines.append(f"  {folder}/ · {len(items)} files, {_fmt_size(total)}")
        else:
            lines += [f"  {rel} · {_fmt_size(size)}" for rel, size in items]
    more = len(lines) - RUN_FILES_LINES
    lines = lines[:RUN_FILES_LINES] + ([f"  …and {more} more"] if more > 0 else [])
    out = "Files it wrote:\n" + "\n".join(lines)
    if partial:
        out += "\n  (the workspace is too big to check every file)"
    return out


def run(root, args):
    """The `run` tool: run, wait up to the timeout, and report the exit code,
    the time it took, the folder it ran in, the trimmed output and the files
    it wrote."""
    timeout = _int(args.get("timeout"), "timeout", DEFAULT_TIMEOUT, 1, MAX_TIMEOUT)
    argv, popen_kwargs = argv_for(root, args)
    top = files_top(root)
    try:
        before, _ = snapshot(top)
    except OSError:
        before = None
    started = time.monotonic()
    try:
        proc = spawn(argv, top, popen_kwargs)
    except OSError as e:
        raise ToolError(f"Couldn't start it: {e}")
    trim = Trimmer()
    timed_out = threading.Event()

    def expire():
        timed_out.set()
        kill(proc)

    timer = threading.Timer(timeout, expire)
    timer.start()
    try:
        read = getattr(proc.stdout, "read1", proc.stdout.read)
        for chunk in iter(lambda: read(4096), b""):
            trim.feed(chunk.decode("utf-8", "replace"))
        proc.wait()
    finally:
        timer.cancel()
        proc.stdout.close()
    took = time.monotonic() - started
    status = (f"timed out after {timeout}s (killed)" if timed_out.is_set()
              else f"exit {proc.returncode}")
    output = trim.text().strip("\n")
    where = _inner((normalize("run", args).get("where") or "").strip().strip("/")).strip("/")
    ran_in = f"ran in {shown_path(where)}/" if where not in ("", ".") else "ran at the top of their files"
    head = f"{status} · {took:.1f}s · {ran_in}"
    result = f"{head}\n{output}" if output else f"{head} · no output"
    report = ""
    if before is not None:
        try:
            after, partial = snapshot(top)
            report = files_report(written(before, after), partial)
        except OSError:
            pass
    return result + ("\n" + report if report else "")


# ── background jobs ──────────────────────────────────────────

def _clock(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class Job:
    """One background run: its output kept (the last JOB_OUTPUT_LINES lines)
    as it comes, and when it was last looked at."""

    def __init__(self, jid, command, proc):
        self.id, self.command, self.proc = jid, command, proc
        self.started = self.touched = time.monotonic()
        self.lines, self.partial, self.dropped = [], "", 0
        self.ended = None
        self.why = None
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True, name=f"job-{jid}").start()

    def _read(self):
        read = getattr(self.proc.stdout, "read1", self.proc.stdout.read)
        try:
            for chunk in iter(lambda: read(4096), b""):
                text = self.partial + chunk.decode("utf-8", "replace")
                *done, self.partial = text.split("\n")
                with self.lock:
                    self.lines.extend(done)
                    over = len(self.lines) - JOB_OUTPUT_LINES
                    if over > 0:
                        del self.lines[:over]
                        self.dropped += over
        except (OSError, ValueError):
            pass
        self.proc.wait()
        with self.lock:
            if self.partial:
                self.lines.append(self.partial)
                self.partial = ""
            self.ended = time.monotonic()
        try:
            self.proc.stdout.close()
        except OSError:
            pass

    def running(self):
        return self.ended is None

    def stop(self, why):
        if self.running():
            self.why = why
            kill(self.proc)

    def report(self, lines=40):
        self.touched = time.monotonic()
        try:
            lines = max(1, min(int(lines or 40), 400))
        except (TypeError, ValueError):
            lines = 40
        with self.lock:
            tail = self.lines[-lines:]
            dropped = self.dropped + max(len(self.lines) - len(tail), 0)
        took = (self.ended or time.monotonic()) - self.started
        if self.running():
            state = f"running for {_clock(took)}"
        else:
            state = f"exit {self.proc.returncode} after {_clock(took)}" + (f" ({self.why})" if self.why else "")
        head = f"Job {self.id} · {state} · {self.command[:120]}"
        if dropped:
            head += f"\n(… {dropped} earlier lines not shown)"
        return head + ("\n" + "\n".join(tail) if tail else "\n(no output yet)")


class Jobs:
    """This process's background jobs, and the watch that ends the ones
    nobody's looking at, or that have run too long."""

    def __init__(self, idle=None, longest=None):
        self.jobs = {}
        self.next = 1
        self.idle = JOB_IDLE_SECONDS if idle is None else idle
        self.longest = JOB_MAX_SECONDS if longest is None else longest
        self.lock = threading.Lock()
        threading.Thread(target=self._watch, daemon=True, name="job-watch").start()

    def start(self, shown, launch):
        with self.lock:
            running = [j for j in self.jobs.values() if j.running()]
            if len(running) >= JOB_MAX_RUNNING:
                raise ToolError(f"{len(running)} jobs are already running ({', '.join(j.id for j in running)}), "
                                "the most at once. Check on one or stop it first.")
            try:
                proc = launch()
            except OSError as e:
                raise ToolError(f"Couldn't start it: {e}")
            jid = f"j{self.next}"
            self.next += 1
            self.jobs[jid] = job = Job(jid, shown, proc)
        return (f"Started job {job.id}: {shown[:160]}\nIt runs in the background. check_job {job.id} shows "
                f"its output (and can wait up to {JOB_WAIT_MAX}s for it to finish); it's stopped if nobody "
                f"checks on it for {self.idle // 60} minutes, and after {_clock(self.longest)} whatever happens.")

    def get(self, jid):
        job = self.jobs.get(str(jid or "").strip())
        if job is None:
            raise ToolError(f"No job {jid!r}. Jobs: {', '.join(self.jobs) or 'none'}.")
        return job

    def check(self, jid, wait=0, lines=None):
        if not str(jid or "").strip():
            if not self.jobs:
                return "No background jobs."
            return "\n\n".join(j.report(lines=5) for j in self.jobs.values())
        job = self.get(jid)
        try:
            wait = max(0, min(int(wait or 0), JOB_WAIT_MAX))
        except (TypeError, ValueError):
            wait = 0
        deadline = time.monotonic() + wait
        while job.running() and time.monotonic() < deadline:
            job.touched = time.monotonic()
            time.sleep(0.25)
        return job.report(lines)

    def stop(self, jid):
        job = self.get(jid)
        job.stop("stopped on request")
        for _ in range(40):
            if not job.running():
                break
            time.sleep(0.05)
        return job.report(lines=10)

    def _watch(self):
        while True:
            time.sleep(max(min(20, self.idle / 3, self.longest / 3), 0.1))
            self.sweep()

    def sweep(self):
        now = time.monotonic()
        for job in list(self.jobs.values()):
            if not job.running():
                continue
            if now - job.touched > self.idle:
                job.stop(f"stopped: nobody checked on it for {max(self.idle // 60, 1)} minutes")
            elif now - job.started > self.longest:
                job.stop(f"stopped: jobs run at most {_clock(self.longest)}")

    def stop_all(self, why):
        for job in list(self.jobs.values()):
            job.stop(why)


# ── preview ──────────────────────────────────────────────────

class _PreviewHandler(http.server.SimpleHTTPRequestHandler):
    """Static files from one folder, and nothing a symlink points at outside
    it. Silent: the default handler logs every request to stderr, which
    FreeClaw would drain into its own log for nothing."""

    def translate_path(self, path):
        full = os.path.realpath(super().translate_path(path))
        root = os.path.realpath(self.directory)
        self.outside = full != root and not full.startswith(root.rstrip(os.sep) + os.sep)
        return root if self.outside else full

    def send_head(self):
        self.translate_path(self.path)
        if self.outside:
            self.send_error(404)
            return None
        return super().send_head()

    def log_message(self, *args):
        pass


class Preview:
    """One static server per workspace process, on 127.0.0.1 and a port the
    OS picks. Reused while it's serving the same folder; asked for another
    folder, it moves there. Only reachable from this machine — which is where
    the agent's browser runs."""

    def __init__(self):
        self.server = None
        self.folder = None

    def start(self, folder):
        if self.server and self.folder == folder:
            return self.url()
        self.stop()
        handler = functools.partial(_PreviewHandler, directory=folder)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.folder = folder
        threading.Thread(target=self.server.serve_forever, daemon=True, name="preview").start()
        return self.url()

    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/"

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.server = self.folder = None


# ── tools ────────────────────────────────────────────────────

# Kept short on purpose: every description is resent with every request.
_PATH = {"type": "string", "description": "As the tools show it."}

TOOLS = [
    {
        "name": "ls",
        "description": "Tree of a workspace folder (default: the root). Skips .git, node_modules, venvs, builds.",
        "inputSchema": {"type": "object", "properties": {
            "path": _PATH,
            "depth": {"type": "integer", "description": f"Default {LS_DEFAULT_DEPTH}, max {LS_MAX_DEPTH}."},
            "all": {"type": "boolean", "description": "Name every file, even in a folder of many."},
        }},
    },
    {
        "name": "read",
        "description": f"Numbered lines of a file, {READ_DEFAULT_LINES} at a time (max {READ_MAX_LINES}). "
                       "Gives the total; read only what you need. An image comes back as the picture.",
        "inputSchema": {"type": "object", "properties": {
            "path": _PATH,
            "start": {"type": "integer", "description": "First line, from 1."},
            "lines": {"type": "integer"},
        }, "required": ["path"]},
    },
    {
        "name": "write",
        "description": "Create or overwrite a whole file, making folders as needed. For changes, use edit.",
        "inputSchema": {"type": "object", "properties": {
            "path": _PATH, "content": {"type": "string"},
        }, "required": ["path", "content"]},
    },
    {
        "name": "edit",
        "description": "Replace an exact string in a file. old must match once, unless all is true.",
        "inputSchema": {"type": "object", "properties": {
            "path": _PATH, "old": {"type": "string"}, "new": {"type": "string"},
            "all": {"type": "boolean"},
        }, "required": ["path", "old", "new"]},
    },
    # The project map (src/dev_map.py): understand before reading.
    {
        "name": "overview",
        "description": "One-screen summary of a project or folder: languages, folders, README, "
                       "manifests, entry points. Start here.",
        "inputSchema": {"type": "object", "properties": {"path": _PATH}},
    },
    {
        "name": "outline",
        "description": "Classes, functions and headings of a file or folder with line numbers, no bodies.",
        "inputSchema": {"type": "object", "properties": {
            "path": _PATH, "glob": {"type": "string", "description": "Only files matching, e.g. *.py."},
        }},
    },
    {
        "name": "find",
        "description": "Where a function, class or other name is defined: path:line and signature.",
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string"}, "path": _PATH,
            "partial": {"type": "boolean", "description": "Match names containing it."},
        }, "required": ["name"]},
    },
    {
        "name": "search",
        "description": f"Regex search of the workspace: path:line: text per hit, up to {SEARCH_MAX_HITS}.",
        "inputSchema": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "glob": {"type": "string", "description": "Only files matching, e.g. *.py."},
            "path": _PATH,
            "ignore_case": {"type": "boolean"},
            "files_only": {"type": "boolean", "description": "Just the files that match, with hit counts."},
        }, "required": ["pattern"]},
    },
    {
        "name": "run",
        "description": "Run a bash command or a .py/.sh file, from the top of their files (or path). "
                       "Returns exit code, output and the files it wrote.",
        "inputSchema": {"type": "object", "properties": {
            "command": {"type": "string"},
            "file": _PATH,
            "path": {"type": "string", "description": "Folder to run in (default: the workspace)."},
            "args": {"type": "array", "items": {"type": "string"}},
            "timeout": {"type": "integer", "description": f"Seconds; default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT}."},
        }},
    },
    {
        "name": "start_job",
        "description": "Run a long command or .py/.sh file in the background, without waiting. Returns "
                       "a job id; stopped if unchecked for 30 min.",
        "inputSchema": {"type": "object", "properties": {
            "command": {"type": "string"},
            "file": _PATH,
            "path": {"type": "string", "description": "Folder to run in (default: the workspace)."},
            "args": {"type": "array", "items": {"type": "string"}},
        }},
    },
    {
        "name": "check_job",
        "description": "A background job's state and latest output (no id: all). wait: seconds (max 300) "
                       "to let it finish first.",
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "string"},
            "wait": {"type": "integer", "description": "Seconds to wait for it to finish (0-300)."},
            "lines": {"type": "integer", "description": "Output lines to show (default 40)."},
        }},
    },
    {
        "name": "stop_job",
        "description": "Stop a background job.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    # Git, for a GitHub repo cloned into the workspace (src/dev_git.py).
    # `repo` is the repo's folder in the workspace.
    {
        "name": "git_clone",
        "description": "Clone a GitHub repo (https URL) into the workspace.",
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string"}, "folder": {"type": "string", "description": "Default: the repo's name."},
        }, "required": ["url"]},
    },
    {
        "name": "git_status",
        "description": "Branch and changed files of a repo.",
        "inputSchema": {"type": "object", "properties": {"repo": _PATH}, "required": ["repo"]},
    },
    {
        "name": "git_diff",
        "description": "Changes in a repo: a stat, then hunks with 1 line of context.",
        "inputSchema": {"type": "object", "properties": {
            "repo": _PATH, "file": {"type": "string"}, "staged": {"type": "boolean"},
        }, "required": ["repo"]},
    },
    {
        "name": "git_commit",
        "description": "Commit all changes in a repo (or just files).",
        "inputSchema": {"type": "object", "properties": {
            "repo": _PATH, "message": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}},
        }, "required": ["repo", "message"]},
    },
    {
        "name": "git_push",
        "description": "Push a repo's current commit to a branch on GitHub (default: the current one).",
        "inputSchema": {"type": "object", "properties": {"repo": _PATH, "branch": {"type": "string"}},
                        "required": ["repo"]},
    },
    {
        "name": "git_fetch",
        "description": "Check origin: fetch from GitHub and list commits there that aren't here yet.",
        "inputSchema": {"type": "object", "properties": {"repo": _PATH}, "required": ["repo"]},
    },
    {
        "name": "git_pull",
        "description": "Fast-forward a repo from GitHub.",
        "inputSchema": {"type": "object", "properties": {"repo": _PATH}, "required": ["repo"]},
    },
    {
        "name": "git_branch",
        "description": "List branches, or create one (switch: true to move onto it).",
        "inputSchema": {"type": "object", "properties": {
            "repo": _PATH, "name": {"type": "string"}, "switch": {"type": "boolean"},
        }, "required": ["repo"]},
    },
    {
        "name": "preview",
        "description": "Serve a workspace folder on localhost and return its URL, to open in the browser.",
        "inputSchema": {"type": "object", "properties": {"path": _PATH}},
    },
    {
        "name": "check",
        "description": "Test a web page in a headless browser: JS errors at file:line, console, failed "
                       "requests, text. Then steps, eval, screenshot.",
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "An .html file, or a folder with index.html."},
            "steps": {"type": "array", "items": {"type": "string"},
                      "description": "In order, e.g. " + dev_check.STEPS_HELP + "."},
            "eval": {"type": "string", "description": "JS expression, evaluated last; result as JSON."},
            "wait": {"type": "integer", "description": f"ms after load before the steps; default "
                                                       f"{dev_check.WAIT_DEFAULT_MS}."},
            "screenshot": {"type": "boolean"},
            "size": {"type": "string", "description": "Viewport WIDTHxHEIGHT, e.g. 1080x1350."},
            "save": {"type": "string", "description": "Write the page as an image here (.png/.jpg) at "
                                                      "`size`: a post, card or diagram drawn in HTML/CSS. "
                                                      "You get the saved image back to check."},
        }, "required": ["path"]},
    },
]

TOOL_NAMES = frozenset(t["name"] for t in TOOLS)


class Tools:
    def __init__(self):
        self.preview = Preview()
        self.jobs = Jobs()

    def call(self, name, args):
        if name not in TOOL_NAMES:
            raise ToolError(f"Unknown tool: {name}")
        # `run` normalizes in command_line, where the approval gate reads it.
        args = args if name == "run" else normalize(name, args)
        return getattr(self, "t_" + name)(workspace(), args if isinstance(args, dict) else {})

    # ls

    def t_ls(self, root, args):
        top = resolve(root, args.get("path"), must_exist=True)
        if not os.path.isdir(top):
            raise ToolError(f"{args.get('path')}: not a folder. Use read for files.")
        depth = _int(args.get("depth"), "depth", LS_DEFAULT_DEPTH, 1, LS_MAX_DEPTH)
        every = bool(args.get("all"))
        lines, more = [], 0

        def grouped(folder, names, level):
            """The files of `names` as lines, in name order: each one, except
            that more than LS_GROUP_OVER of one kind (rendered frames, a photo
            dump) are one line, where the first of them would be."""
            files = [n for n in names if not os.path.isdir(os.path.join(folder, n))]
            kinds = {}
            for n in files:
                kinds.setdefault(os.path.splitext(n)[1].lower(), []).append(n)
            big = {ext: group for ext, group in kinds.items() if len(group) > LS_GROUP_OVER and not every}
            out = []
            for n in files:
                ext = os.path.splitext(n)[1].lower()
                if ext not in big:
                    out.append("  " * level + n)
                elif n == big[ext][0]:
                    group = big[ext]
                    kind = f"{ext} files" if ext else "files with no extension"
                    out.append("  " * level + f"{len(group)} {kind}: {group[0]} … {group[-1]}")
            return out

        def walk(folder, level):
            nonlocal more
            try:
                names = sorted(os.listdir(folder), key=lambda n: (not os.path.isdir(os.path.join(folder, n)), n.lower()))
            except OSError:
                return
            dirs = [n for n in names if os.path.isdir(os.path.join(folder, n))]
            for name in dirs:
                full = os.path.join(folder, name)
                if len(lines) >= LS_MAX_ENTRIES:
                    more += 1
                    continue
                if name in SKIP_DIRS:
                    lines.append("  " * level + name + "/ (skipped)")
                    continue
                lines.append("  " * level + name + "/")
                # Not into a link: it could lead out of the workspace, or round
                # in a circle.
                if level + 1 < depth and not os.path.islink(full):
                    walk(full, level + 1)
            for line in grouped(folder, names, level):
                if len(lines) >= LS_MAX_ENTRIES:
                    more += 1
                    continue
                lines.append(line)

        walk(top, 0)
        if not lines:
            return f"{rel_of(root, top)}/: empty"
        if more:
            lines.append(f"[… {more}+ more entries; ls a subfolder]")
        return f"{rel_of(root, top)}/\n" + "\n".join("  " + line for line in lines)

    # read

    def t_read(self, root, args):
        path = resolve(root, args.get("path"), must_exist=True)
        rel = rel_of(root, path)
        if os.path.isdir(path):
            raise ToolError(f"{rel}: a folder. Use ls.")
        if os.path.splitext(path)[1].lower() in IMAGE_TYPES:
            data, mime, size = _view_image(path)
            return (f"{rel} · {size + ' · ' if size else ''}{os.path.getsize(path):,} bytes. "
                    "Here it is: look it over before you show or send it.", data, mime)
        if not _is_text(path):
            raise ToolError(f"{rel}: not a text file.")
        start = _int(args.get("start"), "start", 1, 1, 10**9)
        count = _int(args.get("lines"), "lines", READ_DEFAULT_LINES, 1, READ_MAX_LINES)
        all_lines = _read_text(path).splitlines()
        total = len(all_lines)
        if total == 0:
            return f"{rel}: empty file"
        if start > total:
            raise ToolError(f"{rel} has {total} lines; start must be 1–{total}.")
        end = min(start + count - 1, total)
        width = len(str(end))
        out = []
        for n in range(start, end + 1):
            line = all_lines[n - 1]
            if len(line) > READ_LINE_CHARS:
                line = line[:READ_LINE_CHARS] + f" [… {len(line) - READ_LINE_CHARS} chars]"
            out.append(f"{n:>{width}}\t{line}")
        head = f"{rel} · lines {start}-{end} of {total}"
        if end < total:
            head += f" (next: start={end + 1})"
        return head + "\n" + "\n".join(out)

    # write

    def t_write(self, root, args):
        path = resolve(root, args.get("path"))
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("content must be a string.")
        if path == root or os.path.isdir(path):
            raise ToolError(f"{args.get('path')}: a folder, not a file.")
        size = len(content.encode("utf-8"))
        if size > WRITE_MAX_BYTES:
            raise ToolError(f"Too big ({size:,} bytes; max {WRITE_MAX_BYTES:,}). Split it up.")
        existed = os.path.exists(path)
        _write_text(path, content)
        lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
        return (f"{'Overwrote' if existed else 'Created'} {rel_of(root, path)} ({lines} lines)"
                + _notes(path, content))

    # edit

    def t_edit(self, root, args):
        path = resolve(root, args.get("path"), must_exist=True)
        rel = rel_of(root, path)
        old, new = args.get("old"), args.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ToolError("old and new must be strings.")
        if not old:
            raise ToolError("old is empty. To create a file, use write.")
        if os.path.isdir(path) or not _is_text(path):
            raise ToolError(f"{rel}: not a text file.")
        text = _read_text(path)
        count = text.count(old)
        if count == 0:
            # The usual cause is whitespace or a line that changed since it was
            # read, so say where to look rather than just "no".
            hint = " Check whitespace and indentation, or read the lines again."
            if old.strip() and old.strip() in text:
                hint = " It matches with different leading/trailing whitespace."
            raise ToolError(f"old not found in {rel}.{hint}")
        if count > 1 and not args.get("all"):
            raise ToolError(f"old matches {count} times in {rel}. Add surrounding lines to make it "
                            f"unique, or pass all: true.")
        line = text[:text.index(old)].count("\n") + 1
        text = text.replace(old, new) if args.get("all") else text.replace(old, new, 1)
        _write_text(path, text)
        replaced = count if args.get("all") else 1
        return (f"Edited {rel}: {replaced} replacement{'s' if replaced > 1 else ''}, first at line {line}"
                + _notes(path, text, long_lines=False))

    # search

    def t_search(self, root, args):
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ToolError("pattern is required.")
        try:
            regex = re.compile(pattern, re.IGNORECASE if args.get("ignore_case") else 0)
        except re.error as e:
            raise ToolError(f"Bad regex: {e}")
        glob = args.get("glob") or ""
        top = resolve(root, args.get("path"), must_exist=True)
        files_only = bool(args.get("files_only"))
        hits, more, counts = [], 0, {}
        files = [top] if os.path.isfile(top) else self._files(top, root)
        for path in files:
            rel = rel_of(root, path)
            if not dev_map.match_glob(path, inner_rel(root, path), glob):
                continue
            try:
                if os.path.getsize(path) > SEARCH_MAX_FILE_BYTES or not _is_text(path):
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for n, line in enumerate(f, 1):
                        if regex.search(line):
                            if files_only:
                                counts[rel] = counts.get(rel, 0) + 1
                                continue
                            if len(hits) >= SEARCH_MAX_HITS:
                                more += 1
                                continue
                            line = line.rstrip("\r\n").strip()
                            if len(line) > SEARCH_LINE_CHARS:
                                line = line[:SEARCH_LINE_CHARS] + "…"
                            hits.append(f"{rel}:{n}: {line}")
            except OSError:
                continue
        if files_only:
            if not counts:
                return "No matches."
            ranked = sorted(counts.items(), key=lambda kv: -kv[1])
            lines = [f"{rel} ({n})" for rel, n in ranked[:SEARCH_MAX_HITS]]
            if len(ranked) > SEARCH_MAX_HITS:
                lines.append(f"[… {len(ranked) - SEARCH_MAX_HITS} more files]")
            return "\n".join(lines)
        if not hits:
            return "No matches."
        if more:
            hits.append(f"[… {more} more hits; narrow the pattern, glob or path]")
        return "\n".join(hits)

    def _files(self, top, root=None):
        """Every file under `top` as git sees it inside a repo (ignored files
        left out), else minus SKIP_DIRS; never following links."""
        return dev_map.files_under(root or top, top, SKIP_DIRS, git=dev_git.git)

    # the project map

    def _map_files(self, root, args):
        top = resolve(root, args.get("path"), must_exist=True)
        files = [top] if os.path.isfile(top) else self._files(top, root)
        glob = args.get("glob") or ""
        return top, [p for p in files if dev_map.match_glob(p, inner_rel(root, p), glob)]

    def t_overview(self, root, args):
        top, files = self._map_files(root, args)
        if os.path.isfile(top):
            top = os.path.dirname(top)
            files = self._files(top, root)
        return dev_map.overview(root, top, rel_of, files)

    def t_outline(self, root, args):
        top, files = self._map_files(root, args)
        return dev_map.outline(root, top, rel_of, files)

    def t_find(self, root, args):
        _, files = self._map_files(root, args)
        return dev_map.find(root, args.get("name"), rel_of, files, bool(args.get("partial")))

    # run

    def t_run(self, root, args):
        return run(root, args)

    # background jobs

    def t_start_job(self, root, args):
        args = {k: v for k, v in dict(args or {}).items() if k != "timeout"}
        shown, invalid = command_line(args)
        if invalid:
            raise ToolError(invalid)
        argv, popen_kwargs = argv_for(root, args)
        return self.jobs.start(shown, lambda: spawn(argv, files_top(root), popen_kwargs))

    def t_check_job(self, root, args):
        return self.jobs.check(args.get("id"), args.get("wait"), args.get("lines"))

    def t_stop_job(self, root, args):
        return self.jobs.stop(args.get("id"))

    # git

    def _repo(self, root, args):
        return resolve(root, args.get("repo"), must_exist=True)

    def _git(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except dev_git.GitError as e:
            raise ToolError(str(e))

    def t_git_clone(self, root, args):
        return self._git(dev_git.clone, root, args.get("url"), args.get("folder"))

    def t_git_status(self, root, args):
        return self._git(dev_git.status, self._repo(root, args))

    def t_git_diff(self, root, args):
        return self._git(dev_git.diff, self._repo(root, args), args.get("file"), bool(args.get("staged")))

    def t_git_commit(self, root, args):
        return self._git(dev_git.commit, self._repo(root, args), args.get("message"), args.get("files"))

    def t_git_push(self, root, args):
        return self._git(dev_git.push, self._repo(root, args), args.get("branch"))

    def t_git_fetch(self, root, args):
        return self._git(dev_git.fetch, self._repo(root, args))

    def t_git_pull(self, root, args):
        return self._git(dev_git.pull, self._repo(root, args))

    def t_git_branch(self, root, args):
        return self._git(dev_git.branch, self._repo(root, args), args.get("name"), bool(args.get("switch")))

    # check

    def t_check(self, root, args):
        target = resolve(root, args.get("path"), must_exist=True)
        if os.path.isdir(target):
            folder, page = target, "index.html"
            if not os.path.isfile(os.path.join(folder, page)):
                raise ToolError(f"{rel_of(root, folder)}: no index.html there. Name the .html file.")
        else:
            folder, page = os.path.dirname(target), os.path.basename(target)
        steps = args.get("steps") or []
        if isinstance(steps, str):
            steps = [steps]
        if not isinstance(steps, list):
            raise ToolError("steps must be a list of strings.")
        wait = _int(args.get("wait"), "wait", dev_check.WAIT_DEFAULT_MS, 0, dev_check.WAIT_MAX_MS)
        expression = args.get("eval")
        if expression is not None and not isinstance(expression, str):
            raise ToolError("eval must be a string.")
        size = None
        if args.get("size"):
            m = re.fullmatch(r"\s*(\d{1,5})\s*[x×]\s*(\d{1,5})\s*", str(args["size"]))
            if not m or not all(16 <= int(v) <= dev_check.SIZE_MAX for v in m.groups()):
                raise ToolError(f"size is WIDTHxHEIGHT, each 16 to {dev_check.SIZE_MAX}, e.g. 1080x1350.")
            size = (int(m.group(1)), int(m.group(2)))
        save = None
        if args.get("save"):
            save = resolve(root, args.get("save"))
            if not save.lower().endswith((".png", ".jpg", ".jpeg")):
                raise ToolError("save must end in .png, .jpg or .jpeg.")
            if os.path.isdir(save):
                raise ToolError(f"{rel_of(root, save)} is a folder.")
        try:
            text, png = dev_check.check(folder, page, rel_of(root, folder), wait, [str(s) for s in steps],
                                        expression or None, bool(args.get("screenshot")), size, save)
        except dev_check.CheckError as e:
            raise ToolError(str(e))
        except ImportError:
            raise ToolError("check needs playwright and Chromium, which aren't installed here.")
        if save:
            text = text.replace(f"Saved {os.path.basename(save)} ", f"Saved {rel_of(root, save)} ", 1)
            if png is None and os.path.isfile(save):
                # The finished image, for the model to look over before it's
                # shown or posted: exactly what was saved, not a fresh shot.
                data, mime, _size = _view_image(save)
                return text + "\nThe saved image is attached: check it, fix the page and save again "\
                    "if anything is off.", data, mime
        return text if png is None else (text, png)

    # preview

    def t_preview(self, root, args):
        folder = resolve(root, args.get("path"), must_exist=True)
        if not os.path.isdir(folder):
            folder = os.path.dirname(folder)
        url = self.preview.start(folder)
        index = "" if os.path.exists(os.path.join(folder, "index.html")) else " (no index.html there)"
        return f"Serving {rel_of(root, folder)} at {url}{index}. Open it with the browser's navigate tool."


# ── protocol ─────────────────────────────────────────────────

SERVER_INFO = {"name": "freeclaw-developer", "version": "1.0"}
# Newest first; an initialize asking for one of these gets it back.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class Server:
    """One request at a time, in order: FreeClaw sends one at a time to a
    stdio child anyway, and a tool here is a file operation or a process this
    thread waits on."""

    def __init__(self, out=None):
        self._out = out or sys.stdout.buffer
        self.tools = Tools()

    def write(self, message):
        # ensure_ascii keeps every byte on the pipe ASCII, whatever the console
        # code page; the client decodes UTF-8 either way.
        line = json.dumps(message, separators=(",", ":")) + "\n"
        try:
            self._out.write(line.encode("ascii"))
            self._out.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass                                             # FreeClaw went away; EOF follows

    def reply(self, request_id, result=None, error=None):
        message = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result
        self.write(message)

    def call(self, name, args):
        try:
            result = self.tools.call(name, args)
            if isinstance(result, tuple):
                text, image = result[0], result[1]
                mime = result[2] if len(result) > 2 else "image/png"
                return {"content": [{"type": "text", "text": text},
                                    {"type": "image", "mimeType": mime,
                                     "data": base64.b64encode(image).decode("ascii")}]}
            return {"content": [{"type": "text", "text": result}]}
        except ToolError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception as e:                               # noqa: BLE001
            _log(f"{name} failed: {type(e).__name__}: {e}")
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                    "isError": True}

    def request(self, message):
        request_id, method = message.get("id"), message.get("method")
        params = message.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            self.reply(request_id, {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })
        elif method == "ping":
            self.reply(request_id, {})
        elif method == "tools/list":
            self.reply(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            self.reply(request_id, self.call(params.get("name"), params.get("arguments")))
        else:
            self.reply(request_id, error={"code": -32601, "message": f"Method not found: {method}"})

    def serve(self, stdin=None):
        for raw in (stdin or sys.stdin.buffer):
            try:
                message = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("method") and "id" in message:
                self.request(message)
        self.tools.preview.stop()
        self.tools.jobs.stop_all("stopped: the Developer app closed")


def main():
    Server().serve()


if __name__ == "__main__":
    main()
