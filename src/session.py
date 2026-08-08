"""One conversation's worth of agent state, lifted out of module globals.

`agent.py` used to *be* the conversation: `agent_messages`, `static_dir` and
the per-turn counters were module-level, and `users.activate_session()` pointed
those globals at whichever user was about to take a turn. That worked because
`main.py` held a single global `agent_lock` across every turn, so exactly one
was ever in flight — at the cost of serialising the whole server behind one
conversation, and of making a second, nested conversation (a sub-agent)
impossible to represent at all.

A Session holds that state instead. Which one is "current" is a
`ContextVar`, so it's per-thread (and per-task) rather than per-process: two
request threads can each run a turn against their own Session at the same time,
and a nested run just binds a different Session for the duration.

## What lives here (per-conversation)

    messages                 the conversation itself
    static_dir               which user's files/ and context.md the tools see
    new_sections             context.md headers created during this conversation
    turn_usage               token tally for the turn in flight
    turn_prefix              pinned history window + tool set for the turn
    consecutive_tool_calls   } the runaway-tool throttle's run,
    last_tool_name           } counted within a turn
    approval_user            } who bash approvals are checked against,
    approval_interactive     } and whether anyone can answer a prompt
    stop_event, turn_active  the Stop button's flag for this turn
    lock                     serialises turns *in this conversation*

## What deliberately does NOT live here (process-wide)

Provider health and capabilities — `_provider_clients`, `_provider_cooldown`,
`_provider_size_ceiling`, `_unsupported_extras`, `_last_provider` — stay module
globals in `agent.py`. They describe the outside world, not a conversation:
which endpoint is rate-limited and which one 400s on `stream_options` is the
same answer for every user. Moving them here would make every new conversation
re-learn each provider's quirks by burning a wasted request.

The tool catalogue (`tools`, `mcp_tool_registry`) stays global for the same
reason: MCP servers are configured per install, and a stdio server is one child
process shared by everyone, not one per conversation.
"""

import contextlib
import contextvars
import os
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Matches agent.py's original module-level default. Only the fallback Session
# below ever uses it; a real conversation is repointed at
# static/<user>/files/ by agent.set_static_dir().
DEFAULT_STATIC_DIR = BASE_DIR + '/../Flask/static/'


def new_turn_usage():
    """A zeroed token tally. `requests` counts every LLM call the turn made,
    `reported` only those that came back with numbers — so "zero tokens" stays
    distinguishable from "this provider doesn't say"."""
    return {"prompt_tokens": 0, "completion_tokens": 0,
            "cached_tokens": 0, "requests": 0, "reported": 0}


class Session:
    """One conversation, plus whatever the turn currently running in it needs.

    Not thread-safe on its own: `lock` is the thing that keeps two turns from
    running in the same conversation at once, and callers take it (see
    main.py's `_session_lock`).
    """

    def __init__(self, name=None, static_dir=None, depth=0):
        # The FreeClaw user this conversation belongs to. None for the
        # process-wide fallback Session, which belongs to nobody.
        self.name = name
        self.messages = []
        self.static_dir = static_dir or DEFAULT_STATIC_DIR
        self.new_sections = []

        # How many sub-agents deep this conversation is. 0 for a user's own
        # conversation; a child spawned by agent.spawn_subagent gets parent + 1.
        # agent.MAX_SUBAGENT_DEPTH is what stops that recursing without end.
        self.depth = depth

        # ── per-turn state ──
        self.turn_usage = new_turn_usage()
        self.turn_prefix = {}
        self.consecutive_tool_calls = 0
        self.last_tool_name = None

        # Both default to the safe answer, so a turn that never called
        # approvals.begin_turn() gets refusals rather than unattended
        # execution.
        self.approval_user = None
        self.approval_interactive = False

        self.stop_event = threading.Event()
        self.turn_active = False

        # Reentrant: a nested run inside a turn that already holds it (a
        # sub-agent on the same thread) must not deadlock against itself.
        self.lock = threading.RLock()

    def __repr__(self):
        return f"<Session {self.name or '(default)'}>"

    # ── per-turn resets ──────────────────────────────────────

    def reset_turn_usage(self):
        """Start a fresh turn's token tally. Tool continuations deliberately
        don't call this — their requests add to the same turn's total."""
        self.turn_usage = new_turn_usage()

    def reset_tool_run(self):
        """Clear the consecutive-tool-call run, so a new request gets its full
        allowance regardless of how the last one ended."""
        self.consecutive_tool_calls = 0
        self.last_tool_name = None

    def pin_turn_prefix(self, start, turn_tools):
        self.turn_prefix = {"start": start, "tools": turn_tools}

    def clear_turn_prefix(self):
        self.turn_prefix = {}

    def note_new_section(self, name):
        """Remember a context.md section created mid-conversation so the next
        turn's prompt mentions it. No-op for one already listed."""
        if name and name not in self.new_sections:
            self.new_sections.append(name)


# ── which Session is current ─────────────────────────────────
#
# A ContextVar rather than a thread-local: it's the primitive designed for
# "ambient state scoped to the current unit of work", it behaves per-thread for
# the threaded Flask/CLI callers we have today, and it already does the right
# thing for the async and nested cases a sub-agent would introduce.

_current = contextvars.ContextVar("freeclaw_session", default=None)

# Used when nothing has been bound. Keeps every pre-existing entry point that
# never heard of Sessions working exactly as it did when these were globals:
# one process-wide conversation.
_fallback = Session()


def current():
    """The Session this thread is working in — the fallback if none is bound."""
    return _current.get() or _fallback


def bind(sess):
    """Make `sess` current for this thread/context. Returns the token `unbind`
    needs; callers that just want a scoped switch should use `use()`."""
    return _current.set(sess)


def unbind(token):
    _current.reset(token)


@contextlib.contextmanager
def use(sess):
    """Run a block against `sess`, restoring the previous binding afterwards.

    This is what a nested run wants — a sub-agent, or a background delivery
    that must not leave its Session bound to a pooled worker thread.
    """
    token = _current.set(sess)
    try:
        yield sess
    finally:
        _current.reset(token)


# ── the per-user registry ────────────────────────────────────
#
# One Session per FreeClaw user, so every entry point into that user's
# conversation — web chat, the /v1 API, a scheduled ping delivery — works on
# the same object and the same lock. Two Sessions for one user would mean two
# divergent message lists racing each other onto the same conversation.json.

_registry = {}
_registry_lock = threading.Lock()


def for_user(name):
    """The Session for `name`, created on first use."""
    with _registry_lock:
        sess = _registry.get(name)
        if sess is None:
            sess = Session(name=name)
            _registry[name] = sess
        return sess


def existing(name):
    """The Session for `name` if one has been created, else None. For callers
    that want to act on a live conversation (e.g. stopping a turn) without
    conjuring one for a user who has never taken a turn."""
    with _registry_lock:
        return _registry.get(name)


def discard(name):
    """Forget a user's Session — for a user being deleted. Any turn already
    holding it keeps running against the object it has."""
    with _registry_lock:
        return _registry.pop(name, None)
