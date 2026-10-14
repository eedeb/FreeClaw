"""The turn a user's agent is working on right now, for anyone watching.

A turn the chat page started itself streams straight back to that page. Every
other turn used to be invisible until it finished: a Discord message (the
bridge), a phone call or a chat from the iPhone app (another device's /chat), a
scheduled ping, an API call. Each of those publishes its events here too, and
the chat page follows along (/api/turn/live) — the same thinking, tool calls,
agent browser and reply it would have shown had the message been typed there.

One turn per user at a time, which is all there can be: a user's turns run
under their conversation lock. A watcher who arrives mid-turn first gets what
has happened so far (snapshot), compacted so the reply's hundreds of token
events arrive as one, then each new event as it comes (follow).

In memory only, and gone when the turn ends: the saved conversation is the
record, this is just the view of it while it's being written.
"""

import contextlib
import threading
import time

# Events a watcher has no use for: 'done' carries the whole conversation (the
# watcher fetches it instead), and approval prompts are answered where the turn
# was started.
_SKIP = {"done"}
# Text that streams in pieces, joined back up in a snapshot.
_JOINABLE = ("token", "reasoning")

_lock = threading.Lock()
_turns = {}                      # user -> Turn in progress


class Turn:
    def __init__(self, user, message, source):
        self.user = user
        self.message = message
        self.source = source
        self.started = time.time()
        self.events = []
        self.done = False
        self._changed = threading.Condition()

    def start_event(self):
        return {"type": "live_start", "message": self.message, "source": self.source,
                "started": self.started}

    def publish(self, event):
        if not isinstance(event, dict) or event.get("type") in _SKIP:
            return
        with self._changed:
            self.events.append(event)
            self._changed.notify_all()

    def watch(self, events):
        """Pass `events` (an agent_stream) through unchanged, publishing each."""
        for event in events:
            self.publish(event)
            yield event

    def finish(self):
        with self._changed:
            self.done = True
            self._changed.notify_all()

    def snapshot(self):
        """(what's happened so far, compacted; how many events that covers)."""
        with self._changed:
            events, n = list(self.events), len(self.events)
        out = []
        for event in events:
            kind = event.get("type")
            if kind in _JOINABLE and out and out[-1].get("type") == kind:
                out[-1] = {**out[-1], "text": (out[-1].get("text") or "") + (event.get("text") or "")}
            else:
                out.append(event)
        return out, n

    def follow(self, start, heartbeat=15.0):
        """The events from index `start` on, as they arrive, until the turn ends.
        Yields None every `heartbeat` seconds of quiet, so the caller can keep
        its connection open (and find out the watcher left)."""
        while True:
            with self._changed:
                if start >= len(self.events) and not self.done:
                    self._changed.wait(heartbeat)
                new = self.events[start:]
                done = self.done
            start += len(new)
            if not new and not done:
                yield None
            yield from new
            if done and start >= len(self.events):
                return


def current(user):
    """This user's turn in progress, or None."""
    with _lock:
        return _turns.get(user)


@contextlib.contextmanager
def turn(user, message, source):
    """A turn for watchers, from here to the end of the block. The caller
    saves the conversation inside it, so a watcher told the turn is over
    finds it saved."""
    live = Turn(user, message, source)
    with _lock:
        _turns[user] = live
    try:
        yield live
    finally:
        live.finish()
        with _lock:
            if _turns.get(user) is live:
                del _turns[user]


def source_of(message, default):
    """Where a /chat message came from, when it says: the iPhone app marks
    what was said on a phone call with 📞."""
    return "call" if (message or "").startswith("📞") else default
