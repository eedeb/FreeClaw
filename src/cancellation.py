"""Cooperative stop for an in-flight agent turn.

There is no thread to kill and no single loop to break. `agent_stream()` is a
generator that recurses once per tool hop, and the work it's blocked on is
somebody else's — a provider's streaming response, a subprocess, an MCP call
over the network. So stopping means asking the turn to notice, at the few points
where stopping is *safe*, that it should wind down. This module is that flag.

Safe has a specific meaning here. An assistant message that declares
`tool_calls` must be followed by exactly one `tool` message per call id, or an
OpenAI-compatible provider rejects the whole history — permanently, since the
conversation is resent in full on every later turn. A stop that just returned
would leave that hole behind and brick the conversation. So the agent answers
the calls it skipped with `STOP_NOTICE` instead of dropping them, and a stop
that lands mid-stream (where tool call arguments are still half-arrived, and so
can't be run at all) discards them and ends the turn on whatever text made it
out.

The flag lives on the Session (src/session.py), like `approvals.py`'s per-turn
context and for the same reason: turns can run concurrently, so "stop" has to
mean *this* conversation's turn rather than whichever one happens to be in
flight. It defaults to "not stopped" and is cleared at the start of every turn,
so a stop that arrives after its turn already finished can't carry over and kill
the next one.
"""

import threading

import src.session as sessions
from src.logging_setup import get_logger

logger = get_logger(__name__)


# What the model is told about a tool call that never ran. Phrased the way
# approvals.denial_message() phrases a refusal, and for the same reason: a model
# that reads "stopped" and then quietly retries the same call is precisely the
# runaway this button exists to end.
STOP_NOTICE = (
    "This tool was NOT run: the user pressed Stop and ended the turn. Do not retry it, "
    "do not try another way to achieve the same thing, and do not continue the task. "
    "Wait for their next instruction."
)

# The assistant message left in the transcript so a stopped turn reads as
# deliberate on reload, rather than as a reply that mysteriously trails off.
STOPPED_TEXT = "_Stopped._"


# Guards the flip of `turn_active` against a stop landing at the same instant.
# One process-wide lock is plenty: it's held for two attribute writes, never
# across any work.
_state_lock = threading.Lock()


def begin_turn(sess=None):
    """Arm a fresh turn. Clears any stop left over from a previous one — a
    button press that raced the end of its own turn must not kill the next."""
    sess = sess or sessions.current()
    with _state_lock:
        sess.stop_event.clear()
        sess.turn_active = True


def end_turn(sess=None):
    """The turn is over; a stop arriving now has nothing to act on."""
    sess = sess or sessions.current()
    with _state_lock:
        sess.turn_active = False
        sess.stop_event.clear()


def request_stop(sess=None):
    """Ask `sess`'s running turn to wind down. Returns True if there was one.

    Called from a different thread than the turn — the Flask route, which
    deliberately doesn't take the conversation's lock (the turn being stopped is
    holding it). Setting an Event is all that crosses the boundary, which is
    also why the caller has to name the Session: the stopping thread never has
    the target conversation bound to its own context."""
    if sess is None:
        sess = sessions.current()
    with _state_lock:
        if not sess.turn_active:
            return False
        sess.stop_event.set()
    logger.info("Stop requested for the in-flight turn of %s", sess)
    return True


def is_stopped(sess=None):
    """Whether this conversation's turn has been asked to stop. Checked at the
    points in agent_stream() where winding down leaves a valid conversation."""
    return (sess or sessions.current()).stop_event.is_set()


def sleep_unless_stopped(seconds, sess=None):
    """Pause for `seconds`, returning early (True) if the turn is stopped while
    we're waiting. For the one place the agent deliberately idles — waiting out
    a provider cooldown in _create_completion — where a plain time.sleep() would
    leave the Stop button looking dead for the length of the wait."""
    return (sess or sessions.current()).stop_event.wait(seconds)


def turn_active(sess=None):
    return (sess or sessions.current()).turn_active
