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

import fnmatch
import functools
import http.server
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

# ── limits ───────────────────────────────────────────────────
#
# Each is a cap on what one tool result can cost, not on what the agent can
# do: every result that's cut says so and says how to get the rest.

# read: lines per call when the agent doesn't say, and the most it may ask
# for. A line is cut at READ_LINE_CHARS — a minified bundle is one line.
READ_DEFAULT_LINES = 200
READ_MAX_LINES = 500
READ_LINE_CHARS = 400

# ls: how deep, and how many entries before it stops listing.
LS_DEFAULT_DEPTH = 2
LS_MAX_DEPTH = 5
LS_MAX_ENTRIES = 200

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


def resolve(root, rel, must_exist=False):
    """Absolute path of `rel` inside `root`, or a ToolError.

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
    full = os.path.realpath(os.path.join(root, rel))
    root_cmp, full_cmp = os.path.normcase(root), os.path.normcase(full)
    if full_cmp != root_cmp and not full_cmp.startswith(root_cmp.rstrip(os.sep) + os.sep):
        raise ToolError(f"{rel}: points outside the workspace.")
    if must_exist and not os.path.exists(full):
        raise ToolError(f"{rel}: not found. Use ls or search to find it.")
    return full


def rel_of(root, path):
    """`path` as the agent names it: relative, forward slashes."""
    rel = os.path.relpath(path, root).replace(os.sep, "/")
    return "." if rel == "." else rel


def _is_text(path):
    """A file that's valid-looking UTF-8 with no NULs in its first chunk."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
    except OSError:
        return False
    return b"\x00" not in chunk


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


def command_line(args):
    """(command line, error) for a `run` call: the exact text the approval
    gate shows and saved rules are matched against, or the reason the call is
    malformed. agent.py calls this before asking anyone anything, and
    `argv_for` builds what actually runs from the same arguments, so the two
    can't describe different things.

    A file runs as `python <file> <args>` (or `bash <file> <args>`); a command
    is shown exactly as given. Everything runs from the workspace root, and
    there's deliberately no separate working-directory argument: it would be
    one more thing that changes what runs without changing the text a saved
    rule matches. `cd sub && make` says it in the command, where it's seen.

    Anything else in `args` is refused rather than ignored, for the same
    reason."""
    args = args if isinstance(args, dict) else {}
    unknown = set(args) - {"command", "file", "args", "timeout"}
    if unknown:
        return None, f"Unknown argument: {', '.join(sorted(unknown))}."
    command = args.get("command")
    file = args.get("file")
    extra = args.get("args")
    if command and file:
        return None, "Pass command or file, not both."
    if file:
        if not isinstance(file, str):
            return None, "file must be a path string."
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
        return " ".join([runner] + [shlex.quote(a) for a in [file] + extra]), None
    if isinstance(command, str) and command.strip():
        if extra:
            return None, "args go with file; put them in the command instead."
        return command.strip(), None
    return None, "Pass command (a bash command line) or file (a .py or .sh to run)."


def argv_for(root, args):
    """(argv or command string, popen kwargs) to run a `run` call's arguments
    from `root`. Validated the same way as `command_line`."""
    shown, error = command_line(args)
    if error:
        raise ToolError(error)
    file = args.get("file")
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


def spawn(argv, cwd, popen_kwargs=None):
    """Start a run with stdout and stderr merged, stdin closed, and on POSIX
    in a session of its own so a timeout or Stop ends everything it started,
    not just the shell."""
    kwargs = dict(popen_kwargs or {})
    if os.name == "nt":
        kwargs.setdefault("creationflags", getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                          | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    # The workspace is this process's business, not the program's.
    env.pop("FC_DEV_WORKSPACE", None)
    return subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, **kwargs)


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


def run(root, args):
    """The `run` tool: run, wait up to the timeout, and report the exit code,
    the time it took and the trimmed output."""
    timeout = _int(args.get("timeout"), "timeout", DEFAULT_TIMEOUT, 1, MAX_TIMEOUT)
    argv, popen_kwargs = argv_for(root, args)
    started = time.monotonic()
    try:
        proc = spawn(argv, root, popen_kwargs)
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
    return f"{status} · {took:.1f}s\n{output}" if output else f"{status} · {took:.1f}s · no output"


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
_PATH = {"type": "string", "description": "Relative to the workspace."}

TOOLS = [
    {
        "name": "ls",
        "description": "Tree of a workspace folder (default: the root). Skips .git, node_modules, venvs, builds.",
        "inputSchema": {"type": "object", "properties": {
            "path": _PATH,
            "depth": {"type": "integer", "description": f"Default {LS_DEFAULT_DEPTH}, max {LS_MAX_DEPTH}."},
        }},
    },
    {
        "name": "read",
        "description": f"Numbered lines of a file, {READ_DEFAULT_LINES} at a time (max {READ_MAX_LINES}). "
                       "Gives the total; read only what you need.",
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
    {
        "name": "search",
        "description": f"Regex search of the workspace: path:line: text per hit, up to {SEARCH_MAX_HITS}.",
        "inputSchema": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "glob": {"type": "string", "description": "Only files matching, e.g. *.py."},
            "path": _PATH,
            "ignore_case": {"type": "boolean"},
        }, "required": ["pattern"]},
    },
    {
        "name": "run",
        "description": "Run a bash command, or a .py/.sh file with args, in the workspace. "
                       "Returns exit code and trimmed output.",
        "inputSchema": {"type": "object", "properties": {
            "command": {"type": "string"},
            "file": _PATH,
            "args": {"type": "array", "items": {"type": "string"}},
            "timeout": {"type": "integer", "description": f"Seconds; default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT}."},
        }},
    },
    {
        "name": "preview",
        "description": "Serve a workspace folder on localhost and return its URL, to open in the browser.",
        "inputSchema": {"type": "object", "properties": {"path": _PATH}},
    },
]

TOOL_NAMES = frozenset(t["name"] for t in TOOLS)


class Tools:
    def __init__(self):
        self.preview = Preview()

    def call(self, name, args):
        if name not in TOOL_NAMES:
            raise ToolError(f"Unknown tool: {name}")
        args = args if isinstance(args, dict) else {}
        return getattr(self, "t_" + name)(workspace(), args)

    # ls

    def t_ls(self, root, args):
        top = resolve(root, args.get("path"), must_exist=True)
        if not os.path.isdir(top):
            raise ToolError(f"{args.get('path')}: not a folder. Use read for files.")
        depth = _int(args.get("depth"), "depth", LS_DEFAULT_DEPTH, 1, LS_MAX_DEPTH)
        lines, more = [], 0

        def walk(folder, level):
            nonlocal more
            try:
                names = sorted(os.listdir(folder), key=lambda n: (not os.path.isdir(os.path.join(folder, n)), n.lower()))
            except OSError:
                return
            for name in names:
                full = os.path.join(folder, name)
                is_dir = os.path.isdir(full)
                if len(lines) >= LS_MAX_ENTRIES:
                    more += 1
                    continue
                if is_dir and name in SKIP_DIRS:
                    lines.append("  " * level + name + "/ (skipped)")
                    continue
                lines.append("  " * level + name + ("/" if is_dir else ""))
                # Not into a link: it could lead out of the workspace, or round
                # in a circle.
                if is_dir and level + 1 < depth and not os.path.islink(full):
                    walk(full, level + 1)

        walk(top, 0)
        if not lines:
            return f"{rel_of(root, top)}: empty"
        if more:
            lines.append(f"[… {more}+ more entries; ls a subfolder]")
        return "\n".join(lines)

    # read

    def t_read(self, root, args):
        path = resolve(root, args.get("path"), must_exist=True)
        rel = rel_of(root, path)
        if os.path.isdir(path):
            raise ToolError(f"{rel}: a folder. Use ls.")
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
        return f"{'Overwrote' if existed else 'Created'} {rel_of(root, path)} ({lines} lines)"

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
        return f"Edited {rel}: {replaced} replacement{'s' if replaced > 1 else ''}, first at line {line}"

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
        hits, more = [], 0
        files = [top] if os.path.isfile(top) else self._files(top)
        for path in files:
            rel = rel_of(root, path)
            if glob and not (fnmatch.fnmatch(os.path.basename(path), glob) or fnmatch.fnmatch(rel, glob)):
                continue
            try:
                if os.path.getsize(path) > SEARCH_MAX_FILE_BYTES or not _is_text(path):
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for n, line in enumerate(f, 1):
                        if regex.search(line):
                            if len(hits) >= SEARCH_MAX_HITS:
                                more += 1
                                continue
                            line = line.rstrip("\r\n").strip()
                            if len(line) > SEARCH_LINE_CHARS:
                                line = line[:SEARCH_LINE_CHARS] + "…"
                            hits.append(f"{rel}:{n}: {line}")
            except OSError:
                continue
        if not hits:
            return "No matches."
        if more:
            hits.append(f"[… {more} more hits; narrow the pattern, glob or path]")
        return "\n".join(hits)

    def _files(self, top):
        """Every file under `top`, minus SKIP_DIRS, not following links."""
        for folder, dirs, names in os.walk(top):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS
                             and not os.path.islink(os.path.join(folder, d)))
            for name in sorted(names):
                full = os.path.join(folder, name)
                if not os.path.islink(full):
                    yield full

    # run

    def t_run(self, root, args):
        return run(root, args)

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
            text = self.tools.call(name, args)
            return {"content": [{"type": "text", "text": text}]}
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


def main():
    Server().serve()


if __name__ == "__main__":
    main()
