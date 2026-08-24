"""Hardcoded approval gate for the agent's bash tool.

Every `run_bash_command` call comes through here first. The decision is not the
model's and it isn't asked: it can't request permission, can't grant itself
any, and can't phrase its way past the gate. The only inputs are a per-user
allow-list on disk and a direct answer from the person at the keyboard.

`check()` returns ALLOWED (a saved rule matches — runs silently), NEEDS_PROMPT
(ask now; the agent's turn blocks until an answer or APPROVAL_TIMEOUT, which
counts as a refusal), or NO_COMMAND.

"Always allow" saves a rule, per FreeClaw user and never global. Two kinds:
**exact** matches the whole command byte for byte, so what runs is exactly what
was approved; **program** matches the leading token, so `ls` covers `ls -la`.
Program rules are only offered for a *simple* command — see `program_of()`,
which refuses anything with shell chaining, substitution or redirection in it,
since `ls; rm -rf ~` would otherwise satisfy a rule that says `ls`.

Rules live in ``Flask/static/<user>/.bash_approvals.json``. Outside
``static/<user>/files/`` on purpose — that's the folder the agent's own file
tools operate on, so a rule-file there would be one it could rewrite. Still
under ``static/`` because that tree is bind-mounted by the Docker install, so
the list survives ``update-mac.sh``.

Not a sandbox: an approved command can do anything the FreeClaw process can,
including rewriting this file, so approving one arbitrary command is
effectively approving all of them. Running FreeClaw as a user that can't harm
anything important is still the real containment story.
"""

import json
import os
import threading
import uuid

import src.session as sessions
import src.shell as shell
from src.logging_setup import get_logger

logger = get_logger(__name__)

# Computed the same way src/agent.py computes STATIC_ROOT rather than imported
# from src/users.py: users.py imports agent.py, agent.py imports this module,
# so reaching back into users.py would close an import cycle.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_ROOT = os.path.normpath(os.path.join(BASE_DIR, "..", "Flask", "static"))

RULES_FILENAME = ".bash_approvals.json"

# Verdicts from check().
ALLOWED = "allowed"
NEEDS_PROMPT = "needs_prompt"
NO_COMMAND = "no_command"

# Decisions a caller may hand to resolve().
DECISION_ONCE = "once"
DECISION_ALWAYS_EXACT = "always_exact"
DECISION_ALWAYS_PROGRAM = "always_program"
DECISION_DENY = "deny"
DECISIONS = (DECISION_ONCE, DECISION_ALWAYS_EXACT, DECISION_ALWAYS_PROGRAM, DECISION_DENY)

# Internal, never accepted from a caller.
_DECISION_TIMEOUT = "timeout"
_DECISION_ABANDONED = "abandoned"

# How long a pending prompt waits for an answer. The agent turn is blocked for
# this long at worst, holding its own conversation's lock — so an unanswered
# prompt now stalls only that conversation rather than the whole app, but it can
# still wedge the user's own next turn: five minutes is enough for someone who
# stepped away mid-answer, and short enough that a closed browser tab doesn't
# leave their next message queued behind a prompt nobody is going to answer.
APPROVAL_TIMEOUT = 300

# Shell syntax that makes a command more than one command, or lets it write
# somewhere the visible text doesn't show. A command containing any of these
# can never be matched by — or turned into — a program-level rule.
_COMPOUND = (";", "&", "|", "`", "$(", ">", "<", "\n", "\r")


def program_of(command):
    """The program a *simple* command runs, or None if the command isn't
    simple enough for a program-level rule to be safe.

    None is returned for anything containing shell chaining/substitution/
    redirection (`_COMPOUND`), for an unparseable command line, and for a
    leading `VAR=value` assignment. The token is returned exactly as written:
    `ls` and `/usr/bin/ls` are different rules on purpose, so a rule for one
    can't be satisfied by a lookalike binary somewhere else on PATH."""
    if not command:
        return None
    if any(tok in command for tok in _COMPOUND):
        return None
    try:
        parts = shell.split_command(command)
    except ValueError:
        return None
    if not parts:
        return None
    program = parts[0]
    if "=" in program:
        return None
    return program


# ── rule storage ─────────────────────────────────────────────

# Serialises read-modify-write on the rule files. Contended only between a
# Flask request thread resolving a prompt and whichever thread is running the
# agent turn, so a single lock is plenty.
_rules_lock = threading.Lock()

_EMPTY_RULES = {"exact": [], "programs": []}


def rules_path(user):
    """Where `user`'s allow-rules live. Returns None for a name that isn't a
    single path segment — this is the one place a bad user name could
    otherwise escape the static tree."""
    if not user:
        return None
    if os.sep in user or (os.altsep and os.altsep in user) or user in (".", ".."):
        return None
    return os.path.join(STATIC_ROOT, user, RULES_FILENAME)


def _read_rules(user):
    path = rules_path(user)
    if not path or not os.path.exists(path):
        return {"exact": [], "programs": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        # A corrupt rule file must fail closed — asking again is a nuisance,
        # silently running unapproved commands is not.
        logger.exception("Couldn't read bash approvals for user=%r; treating as empty", user)
        return {"exact": [], "programs": []}
    if not isinstance(data, dict):
        return {"exact": [], "programs": []}
    exact = [s for s in (data.get("exact") or []) if isinstance(s, str)]
    programs = [s for s in (data.get("programs") or []) if isinstance(s, str)]
    return {"exact": exact, "programs": programs}


def _write_rules(user, rules):
    path = rules_path(user)
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rules, f, indent=2)
    os.replace(tmp, path)  # atomic, so a crash can't leave a half-written file


def list_rules(user):
    """This user's saved rules as {"exact": [...], "programs": [...]}."""
    with _rules_lock:
        return _read_rules(user)


def add_exact_rule(user, command):
    command = (command or "").strip()
    if not command:
        return False
    with _rules_lock:
        rules = _read_rules(user)
        if command not in rules["exact"]:
            rules["exact"].append(command)
            _write_rules(user, rules)
    logger.info("Bash approval rule added for user=%r: exact %r", user, command)
    return True


def add_program_rule(user, command):
    """Save a program-level rule derived from `command`. Refuses (returning
    False) when the command isn't simple enough for one to be safe."""
    program = program_of(command)
    if not program:
        return False
    with _rules_lock:
        rules = _read_rules(user)
        if program not in rules["programs"]:
            rules["programs"].append(program)
            _write_rules(user, rules)
    logger.info("Bash approval rule added for user=%r: program %r", user, program)
    return True


def remove_rule(user, kind, value):
    """Drop one saved rule. `kind` is "exact" or "programs"."""
    if kind not in ("exact", "programs"):
        return False
    with _rules_lock:
        rules = _read_rules(user)
        if value not in rules[kind]:
            return False
        rules[kind] = [v for v in rules[kind] if v != value]
        _write_rules(user, rules)
    logger.info("Bash approval rule removed for user=%r: %s %r", user, kind, value)
    return True


def clear_rules(user):
    with _rules_lock:
        _write_rules(user, {"exact": [], "programs": []})
    logger.info("All bash approval rules cleared for user=%r", user)


# ── per-turn context ─────────────────────────────────────────
#
# Which user the current turn belongs to, and whether there's anyone able to
# answer a prompt for it. Held on the current Session (src/session.py) rather
# than in module globals, so two conversations taking a turn at once can't read
# each other's answer to either question — one user's interactive web turn must
# never lend its "someone is watching" to another user's background ping. Both
# default to the safe answer, so an entry point that forgets to call
# begin_turn() gets refusals rather than unattended execution.


def begin_turn(user, interactive):
    """Declare who the coming turn belongs to and whether a prompt can be
    answered. `interactive=False` (a scheduled ping, an API call — anything
    with no one watching) means saved rules still apply, but a command that
    would need asking is refused instead of hanging."""
    sess = sessions.current()
    sess.approval_user = user
    sess.approval_interactive = bool(interactive)


def current_user():
    return sessions.current().approval_user


def is_interactive():
    return sessions.current().approval_interactive


# ── pending prompts ──────────────────────────────────────────

class Request:
    """One outstanding "may I run this?" question."""

    def __init__(self, user, command):
        self.id = uuid.uuid4().hex
        self.user = user
        self.command = command
        self.program = program_of(command)
        self.decision = None
        self._answered = threading.Event()

    def as_event(self):
        """The payload the frontend/CLI needs to draw the prompt. `program` is
        None when a program-level rule isn't on offer for this command."""
        return {
            "id": self.id,
            "command": self.command,
            "program": self.program,
            "timeout": APPROVAL_TIMEOUT,
        }


_pending = {}
_pending_lock = threading.Lock()


def check(command, user=None):
    """Verdict for `command` without prompting or running anything."""
    command = (command or "").strip()
    if not command:
        return NO_COMMAND
    rules = list_rules(current_user() if user is None else user)
    if command in rules["exact"]:
        return ALLOWED
    program = program_of(command)
    if program and program in rules["programs"]:
        return ALLOWED
    return NEEDS_PROMPT


def open_request(command, user=None):
    """Register a prompt and return the Request. The caller is expected to
    show it (SSE event, terminal prompt) and then call wait()."""
    req = Request(current_user() if user is None else user, (command or "").strip())
    with _pending_lock:
        _pending[req.id] = req
    return req


def wait(req):
    """Block until `req` is answered, then return the decision.

    An unanswered prompt resolves to a refusal — a closed tab or a user who
    walked away must not leave a command to fire later unattended."""
    answered = req._answered.wait(APPROVAL_TIMEOUT)
    with _pending_lock:
        _pending.pop(req.id, None)
    if not answered:
        req.decision = _DECISION_TIMEOUT
        logger.warning("Bash approval timed out after %ss for user=%r: %r",
                       APPROVAL_TIMEOUT, req.user, req.command)
    return req.decision


def resolve(request_id, decision):
    """Answer a pending prompt. Called from whatever thread heard back from the
    user — the Flask route in the web UI, the input loop in the CLI. Writes any
    always-allow rule *before* releasing the waiter, so the rule is on disk by
    the time the command runs.

    Returns (ok, error_message)."""
    if decision not in DECISIONS:
        return False, "Unknown decision."
    with _pending_lock:
        req = _pending.get(request_id)
    if req is None:
        # Already answered, or timed out and cleaned up.
        return False, "That approval request is no longer pending."

    if decision == DECISION_ALWAYS_EXACT:
        add_exact_rule(req.user, req.command)
    elif decision == DECISION_ALWAYS_PROGRAM:
        if not add_program_rule(req.user, req.command):
            # Offered only for simple commands, so this is a client sending a
            # decision that doesn't apply. Downgrade rather than refuse: the
            # user did say yes, they just can't have the broader rule.
            logger.warning("Program rule refused for user=%r (not a simple command): %r",
                           req.user, req.command)
            decision = DECISION_ONCE

    req.decision = decision
    req._answered.set()
    logger.info("Bash approval for user=%r: %s — %r", req.user, decision, req.command)
    return True, None


def abandon_all(user=None):
    """Refuse pending prompts. For a caller tearing down a turn (a dropped
    connection, a stop, a shutdown) so a waiter can't sit out the full timeout.

    `user` limits it to that user's prompts, which is what a caller tearing
    down one conversation wants now that turns can run concurrently — without
    it, one user's dropped connection would refuse a command another user was
    part-way through approving. Omit it to abandon every prompt in the process
    (a shutdown)."""
    with _pending_lock:
        reqs = [r for r in _pending.values() if user is None or r.user == user]
        for req in reqs:
            del _pending[req.id]
    for req in reqs:
        req.decision = _DECISION_ABANDONED
        req._answered.set()


def was_approved(decision):
    return decision in (DECISION_ONCE, DECISION_ALWAYS_EXACT, DECISION_ALWAYS_PROGRAM)


def denial_message(decision):
    """What the model is told when a command doesn't run. Phrased as a fact
    about what happened, with an explicit instruction not to work around it —
    a model that reads "denied" and then tries a different spelling of the same
    command is the failure mode worth heading off."""
    if decision == _DECISION_TIMEOUT:
        return ("The command was NOT run: the approval prompt went unanswered, so it timed out. "
                "Tell the user it needs their approval and stop. Do not retry it and do not try "
                "another way to achieve the same thing.")
    if decision == _DECISION_ABANDONED:
        return ("The command was NOT run: the approval prompt was dismissed. Tell the user and stop. "
                "Do not retry it and do not try another way to achieve the same thing.")
    if decision == NO_COMMAND:
        return "No command was provided, so nothing was run."
    if decision == "not_interactive":
        return ("The command was NOT run: it needs the user's approval, and this turn has nobody to ask "
                "(it's a scheduled or background run). Say plainly that this step needs them present "
                "and stop. Do not retry it and do not try another way to achieve the same thing.")
    return ("The command was NOT run: the user refused it. Accept that, tell them it was refused, and "
            "stop. Do not retry it, do not reword it, and do not try another way to achieve the same "
            "thing unless they explicitly ask.")
