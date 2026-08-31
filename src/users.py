"""User / conversation storage, shared by the Flask app (Flask/main.py) and
the CLI (src/cli.py) so both entry points read and write the exact same
on-disk layout under Flask/static/<user>/."""

import contextlib
import os
import re
import json
import shutil
import tempfile
import time

import src.agent as agent
import src.approvals as approvals
import src.session as sessions
from src.logging_setup import get_logger

try:
    import fcntl
except ImportError:  # not POSIX — Windows locks through msvcrt below instead
    fcntl = None

try:
    import msvcrt
except ImportError:  # not Windows
    msvcrt = None

logger = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.realpath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "..", "Flask", "static")
os.makedirs(STATIC_DIR, exist_ok=True)

CONVERSATIONS_SUBDIR = "conversations"
RESERVED_NAMES = {"conversations", "uploads"}


def safe_username(name):
    """Restrict usernames to something that's safe to use as a folder name
    and can't escape the static/ directory or collide with reserved paths."""
    if not name:
        return None
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\- ]{1,40}", name):
        return None
    if name.lower() in RESERVED_NAMES:
        return None
    return name


def user_dir(name):
    return os.path.join(STATIC_DIR, name) + os.sep


def conversation_path(name):
    return os.path.join(user_dir(name), "conversation.json")


def conv_files_dir(name):
    """Folder where this user's created/uploaded files live, e.g.
    static/<user>/files/ — also where their context.md (long-term memory)
    lives, so the agent's normal file tools can read/edit it directly.
    Kept separate from the conversation's metadata JSON file."""
    path = os.path.join(user_dir(name), "files")
    os.makedirs(path, exist_ok=True)
    return path


def _migrate_legacy_conversations(name):
    """Older versions of FreeClaw gave each user many chats, stored as
    static/<user>/conversations/<id>.json (each with its own files/
    subfolder). Now every user has exactly one conversation, so collapse
    that down: keep the most recently updated chat's history and files as
    this user's single conversation, and drop the rest."""
    legacy_dir = os.path.join(user_dir(name), CONVERSATIONS_SUBDIR)
    if not os.path.isdir(legacy_dir):
        return
    latest_id, latest_data, latest_ts = None, None, -1
    for fname in os.listdir(legacy_dir):
        if not fname.endswith(".json"):
            continue
        conv_id = fname[:-5]
        try:
            with open(os.path.join(legacy_dir, fname), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        ts = data.get("updated_at", 0)
        if ts > latest_ts:
            latest_id, latest_data, latest_ts = conv_id, data, ts
    if latest_data is not None and not os.path.exists(conversation_path(name)):
        with open(conversation_path(name), "w", encoding="utf-8") as f:
            json.dump(latest_data, f)
        legacy_files_dir = os.path.join(legacy_dir, latest_id)
        if os.path.isdir(legacy_files_dir):
            dest = conv_files_dir(name)
            for item in os.listdir(legacy_files_dir):
                shutil.move(os.path.join(legacy_files_dir, item), os.path.join(dest, item))
    shutil.rmtree(legacy_dir, ignore_errors=True)


def user_context_path(name):
    """Path to this user's context.md — inside their files folder (not
    user_dir directly) so it's reachable by the agent's normal read_file/
    edit_file/create_file tools instead of needing a dedicated tool.
    Calling this creates static/<user>/files/ as a side effect, same as
    conv_files_dir()."""
    return os.path.join(conv_files_dir(name), "context.md")


def user_ping_path(name):
    """Path to this user's ping.md — their scheduled pings, kept in the same
    files folder as context.md so the agent's normal file tools (and the
    add_ping tool) can reach it. Creates static/<user>/files/ as a side
    effect, same as user_context_path()."""
    return os.path.join(conv_files_dir(name), "ping.md")


def list_users():
    if not os.path.isdir(STATIC_DIR):
        return []
    users_found = []
    for entry in sorted(os.listdir(STATIC_DIR)):
        if entry.lower() in RESERVED_NAMES or entry.startswith('.'):
            continue
        full = os.path.join(STATIC_DIR, entry)
        try:
            if os.path.isdir(full):
                users_found.append(entry)
        except OSError:
            continue
    return users_found


def user_exists(name):
    return name in list_users()


def create_user(name):
    ctx_path = user_context_path(name)  # creates static/<user>/files/ too
    if not os.path.exists(ctx_path):
        # Seeded with headings (agent.CONTEXT_TEMPLATE) rather than blank, so
        # the model has somewhere to file each new fact from the first turn.
        with open(ctx_path, "w", encoding="utf-8") as f:
            f.write(agent.CONTEXT_TEMPLATE)
    ping_path = user_ping_path(name)
    if not os.path.exists(ping_path):
        with open(ping_path, "w", encoding="utf-8") as f:
            f.write("")
    ensure_conversation(name)


def load_conversation(name):
    path = conversation_path(name)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@contextlib.contextmanager
def _file_lock(path):
    """Hold an exclusive lock on `path` for the duration of the block.

    A Session's own lock only covers threads inside one process, and the CLI is
    a *different* process from the Flask app (on the macOS install, a whole
    separate `docker compose exec`). Both write the same conversation.json, so
    without this the read-modify-write below is last-writer-wins across the two
    and a turn taken in one can silently erase a turn taken in the other.

    The lock is taken on a sidecar rather than the file itself: the write
    replaces conversation.json by rename, so a lock held on the old inode would
    stop guarding anything the moment the first writer finished. On Windows
    the sidecar matters for a second reason — a locked file there can't be
    replaced at all, so locking conversation.json itself would deadlock the
    rename against the lock meant to protect it.
    """
    if fcntl is None and msvcrt is None:
        yield  # neither primitive: degrade to the atomic rename alone
        return

    lock_path = path + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    locked = False
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
            # Released by the os.close() below, as flock always has been.
        else:
            locked = _msvcrt_lock(fd, lock_path)
        yield
    finally:
        # Only the msvcrt path needs an explicit release, and only if it got
        # the lock in the first place — `locked` stays False on the fcntl path
        # for exactly that reason.
        if locked:
            with contextlib.suppress(OSError):
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


# Longest we'll wait for the other process to finish its write before giving up
# and going ahead anyway. A conversation write is a read, a json.dump and a
# rename — milliseconds. Anything past this is a crashed or suspended process
# holding a lock nobody is going to release.
_LOCK_TIMEOUT = 20


def _msvcrt_lock(fd, lock_path):
    """Take Windows' equivalent of flock(LOCK_EX) on `fd`. Returns whether it
    was actually acquired.

    msvcrt.locking() has no blocking-forever mode. Its one blocking flag,
    LK_LOCK, gives up after about ten seconds and raises — and it retries on a
    one-second cycle, so a lock released just after a poll costs most of a
    second. LK_NBLCK in a loop of our own is both finer-grained and honest
    about the timeout being ours.

    Failing to acquire is not fatal — we log and proceed. The atomic rename in
    _write_json_atomic still guarantees nobody reads a half-written file; what
    is lost is only the guarantee that two simultaneous writers don't overwrite
    each other, which is exactly where this degraded before the branch existed.
    """
    deadline = time.monotonic() + _LOCK_TIMEOUT
    while True:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            # One byte at offset 0 — the range is arbitrary as long as every
            # process agrees on it, and the sidecar has no contents to guard.
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                logger.warning(
                    "Gave up waiting for the lock on %s after %ds; writing "
                    "anyway. A simultaneous write from another process could "
                    "be lost.", lock_path, _LOCK_TIMEOUT)
                return False
            time.sleep(0.05)


def _replace_with_retry(tmp, path, attempts=10, delay=0.05):
    """os.replace(tmp, path), retried past a transient Windows PermissionError.

    POSIX lets a file be renamed over while another process has it open.
    Windows does not: a reader holding conversation.json — the ping scheduler,
    a page refresh, an antivirus scanner deciding to inspect a file that just
    changed — makes the replace fail with PermissionError until it lets go.

    Those holds are momentary, so a short retry turns what would be a lost
    turn into a barely measurable delay. The last attempt is left to raise: at
    that point something is holding the file open indefinitely, and the caller
    needs the error rather than a silently dropped conversation.
    """
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def _write_atomic(path, text, prefix=".tmp-"):
    """Replace `path` with `text` in one step.

    Writing in place leaves a window where the file on disk is truncated or
    half-written, and anything reading it then (the other process, the ping
    scheduler, a page refresh) sees a corrupt conversation. Writing a temp file
    in the same directory and renaming it over the target is atomic on POSIX,
    so a reader sees either the whole old file or the whole new one. Windows
    makes the same guarantee for os.replace, with one extra failure mode — see
    the retry loop.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_json_atomic(path, data):
    """`data` as JSON, replacing `path` in one step (see _write_atomic)."""
    _write_atomic(path, json.dumps(data), prefix=".conv-")


def read_user_context(name):
    """This user's context.md — their long-term memory — or "" if they have
    none yet."""
    try:
        with open(user_context_path(name), "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def write_user_context(name, content):
    """Replace this user's context.md.

    Atomic, because the agent's own tools read the same file mid-turn and a
    half-written memory is worse than a stale one. Note that the conversation
    holds a *snapshot* of this file, taken at its last reset — a caller that
    wants the change to reach the running conversation has to say so; see
    agent.refresh_context()."""
    _write_atomic(user_context_path(name), content, prefix=".ctx-")


def save_conversation(name, messages, title=None):
    """Writes the conversation and returns the updated_at stamp it wrote, so
    callers can hand that value straight to the browser (the chat page keys
    its "has this conversation changed?" poll off it).

    The read-modify-write runs under a cross-process file lock and lands via an
    atomic rename, so the web app and the CLI can hold the same conversation
    open without either one losing the other's turn or reading a half-written
    file."""
    path = conversation_path(name)
    with _file_lock(path):
        data = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                data = {}
        if title is not None:
            data["title"] = title
        elif "title" not in data:
            data["title"] = "New chat"
        data["messages"] = messages
        data["updated_at"] = time.time()
        _write_json_atomic(path, data)
    return data["updated_at"]


def derive_title(messages):
    for m in messages:
        if m.get("role") == "user":
            text = m.get("content")
            if isinstance(text, list):
                text = " ".join(b.get("text", "") for b in text if isinstance(b, dict))
            text = (text or "").strip()
            if text:
                return text[:60]
    return "New chat"


def ensure_conversation(name):
    """Make sure `name` has a conversation.json, migrating an older
    multi-chat layout if one is found, or creating a fresh conversation
    (via agent.reset(), scoped to this user's own files folder, which also
    holds their long-term context.md) if there's nothing to migrate.

    Scoped to `name`'s own Session, because several read-only routes call this
    without activating a session first — un-scoped, the reset() below would
    build the fresh conversation on whatever Session that thread last touched."""
    _migrate_legacy_conversations(name)
    if not os.path.exists(conversation_path(name)):
        with sessions.use(sessions.for_user(name)):
            agent.set_static_dir(conv_files_dir(name))
            agent.reset()
            save_conversation(name, agent.get_messages(), title="New chat")


def activate_session(name, interactive=False):
    """Bind this thread to `name`'s Session, point it at their file folder
    (which holds their context.md alongside created/uploaded files), and load
    their saved conversation messages so the next agent_stream() call continues
    the right thread. Returns the Session.

    One Session per user, from `sessions.for_user()`, so every way into that
    user's conversation — web chat, the /v1 API, a scheduled ping — works on the
    same object and the same lock rather than racing two message lists onto one
    conversation.json.

    The binding is this thread's, not the process's: another request thread can
    activate a different user at the same moment without disturbing this one.
    Callers that need the binding to be *scoped* — a background delivery on a
    pooled worker, or a nested run — should use `sessions.use()` around the
    turn instead of relying on the next activate_session() to overwrite it.

    `interactive` says whether there's someone able to answer a bash approval
    prompt for the turn about to run — True from the web chat and the CLI,
    False for a background delivery like a scheduled ping. It defaults to
    False so a caller that doesn't think about it gets refusals rather than an
    agent turn that blocks for five minutes waiting on nobody. Scoping the
    approval rules to `name` here is also what keeps one user's always-allow
    list from applying to another's."""
    sess = sessions.for_user(name)
    sessions.bind(sess)
    ensure_conversation(name)
    agent.set_static_dir(conv_files_dir(name))
    approvals.begin_turn(name, interactive)
    data = load_conversation(name)
    agent.set_messages(data.get("messages", []))
    return sess
