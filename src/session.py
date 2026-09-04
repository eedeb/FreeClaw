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
    context_writes           what add_context saved, echoed back every turn
    turn_usage               token tally for the turn in flight
    turn_prefix              pinned history window + tool set for the turn
    turn_tool_names          which tools the turn in flight actually ran
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

The tool catalogue stays out of here too, though it isn't quite process-wide
any more: `agent.py` keys it by FreeClaw *user*, since which MCP servers are
switched on is each user's own choice. Still not per conversation — a user's
two conversations are offered the same tools, and the stdio child behind a
server is one process shared by all of them.
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

        # Everything add_context saved during this conversation, newest last,
        # as (header, entry) pairs.
        #
        # context.md itself reaches the prompt as a snapshot taken at reset(),
        # so without this a fact the model saves mid-conversation is invisible
        # to it for the rest of that conversation — it writes the roster down
        # and then, once the message scrolls out of the recent-history window,
        # has no way to read it back. new_sections was a partial patch on the
        # same hole: it carries the *names* of sections created, so the model
        # knows to go looking. This carries what was actually written, so it
        # doesn't have to.
        #
        # Rendered into the volatile tail of the system message, which is
        # rebuilt every turn, so these survive regardless of how tight the
        # turn's history window is.
        self.context_writes = []

        # How many sub-agents deep this conversation is. 0 for a user's own
        # conversation; a child spawned by agent.spawn_subagent gets parent + 1.
        # agent.MAX_SUBAGENT_DEPTH is what stops that recursing without end.
        self.depth = depth

        # ── per-turn state ──
        self.turn_usage = new_turn_usage()
        self.turn_prefix = {}
        # Winning classifier tag for the turn in flight. Set once when the user
        # message is classified and read back when the assistant message is
        # built — which can be several tool hops later, in a recursive
        # agent_stream call where the local `tag` is long out of scope.
        self.turn_tag = None
        self.consecutive_tool_calls = 0
        self.last_tool_name = None
        # Every tool this turn ran, in order. Distinct from last_tool_name
        # (which is only the throttle's one-deep run): this is the whole
        # turn's record, and it's what lets the finished reply be stamped
        # with whether it consulted a source or answered from weights alone.
        self.turn_tool_names = []

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
        allowance regardless of how the last one ended. Also starts the turn's
        tool record over — both are per-turn, and tool continuations
        deliberately don't call this, so a turn's hops accumulate into one
        record the same way its token tally does."""
        self.consecutive_tool_calls = 0
        self.last_tool_name = None
        self.turn_tool_names = []

    def note_tool_used(self, name):
        """Record that this turn ran `name`."""
        if name:
            self.turn_tool_names.append(name)

    def pin_turn_prefix(self, start, turn_tools):
        self.turn_prefix = {"start": start, "tools": turn_tools}

    def clear_turn_prefix(self):
        self.turn_prefix = {}
        # Same lifecycle as the prefix: both belong to one user turn. Cleared
        # after the assistant message that ends the turn has been appended, so
        # that message still carries the tag.
        self.turn_tag = None

    def note_new_section(self, name):
        """Remember a context.md section created mid-conversation so the next
        turn's prompt mentions it. No-op for one already listed."""
        if name and name not in self.new_sections:
            self.new_sections.append(name)

    # ── context.md written this conversation ─────────────────

    # How many saved entries are echoed back into the prompt. Re-sent on every
    # request for the rest of the conversation, so it's capped — but generously,
    # because the whole point is that a long task can keep writing things down
    # and still see all of them. Past the cap the oldest is dropped: it is
    # still in context.md, and search_context can fetch it back.
    MAX_ECHOED_WRITES = 25

    def note_context_write(self, header, entry):
        """Record one add_context save so the rest of the conversation can see
        it without re-reading the file."""
        if not header or not entry:
            return
        # add_context turns a one-liner into a list item before it lands in the
        # file; the echo renders its own bullet, so strip that one back off.
        entry = " ".join(entry.split()).lstrip("-*").strip()
        if not entry:
            return
        # A correction supersedes what it corrects rather than sitting next to
        # it: two entries under one header where the later contradicts the
        # earlier is exactly the shape that produces confidently stale advice.
        # Only an exact repeat is deduped here — anything cleverer would be
        # guessing at which of two different lines is meant to win.
        self.context_writes = [(h, e) for h, e in self.context_writes
                               if not (h == header and e == entry)]
        self.context_writes.append((header, entry))
        del self.context_writes[:-self.MAX_ECHOED_WRITES]

    def clear_context_writes(self):
        self.context_writes = []


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
