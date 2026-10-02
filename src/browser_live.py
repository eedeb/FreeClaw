"""The agent's browser, live, for the Browser app to show.

Each FreeClaw user's browser child (src/browser_server.py) sends what it's
doing up the stdio pipe as notifications — screencast frames, where its cursor
is going, which page it's on — and src/mcp_client.py hands them here. This
module keeps the latest of each per user and lets the Browser app's stream
(Flask/main.py: /api/browser/agent/stream) wait for the next.

Frames are only sent while somebody is watching: a viewer holds a *lease*
(`watch`), which this renews on the child every few seconds and lets lapse when
the viewer goes. A viewer can be open before the agent has started browsing;
the child says hello when it starts, and is put under watch then, so the
person sees the first page load rather than joining part way.

Nothing here touches playwright or the child's protocol beyond one
notification, so it's cheap to import from Flask and holds no browser state of
its own: when the child goes, so does its feed.
"""

import collections
import threading
import time

# What a viewer's lease asks of the child, and how often it's renewed. The
# child stops sending frames once a lease runs out unrenewed, so a FreeClaw
# that stopped (or a viewer that closed) costs at most this long of frames.
LEASE = 15.0
RENEW_EVERY = 5.0

# A viewer counts as watching for this long after it last asked.
VIEWER_TIMEOUT = 12.0

# The live items a slow viewer can still catch up on. Frames are only ever
# shown newest-first, so these are mostly cursor moves.
BACKLOG = 64

_lock = threading.Lock()
_feeds = {}                       # FreeClaw user -> _Feed


class _Feed:
    def __init__(self):
        self.proc = None              # the child sending it; None once gone
        self.seq = 0
        self.items = collections.deque(maxlen=BACKLOG)   # (seq, kind, payload)
        self.frame = None             # newest frame, for a viewer joining now
        self.meta = {}                # url, title, action, busy
        self.viewers = 0.0            # when a viewer last asked (monotonic)
        self.lease_sent = 0.0
        self.ready = threading.Condition(_lock)


def _feed(user):
    feed = _feeds.get(user)
    if feed is None:
        feed = _feeds[user] = _Feed()
    return feed


def _add(feed, kind, payload):
    feed.seq += 1
    feed.items.append((feed.seq, kind, payload))
    feed.ready.notify_all()


def _watching(feed):
    return time.monotonic() - feed.viewers < VIEWER_TIMEOUT


def _send_lease(feed):
    """Called with _lock held; the write itself is a quick pipe write."""
    proc = feed.proc
    if proc is None:
        return
    feed.lease_sent = time.monotonic()
    proc.notify("notifications/freeclaw/watch", {"seconds": LEASE})


# ── what mcp_client calls ────────────────────────────────────

def publish(user, proc, kind, params):
    """One notification from `user`'s browser child."""
    with _lock:
        feed = _feed(user)
        if feed.proc is not proc:
            # A new child for this user (the last one was dropped after a
            # login was saved, or crashed): what it shows starts fresh.
            feed.proc = proc
            feed.frame = None
            feed.meta = {}
            feed.lease_sent = 0.0
        if kind == "frame":
            data = params.get("data")
            if isinstance(data, str) and data:
                feed.frame = data
                _add(feed, "frame", data)
        elif kind == "cursor":
            _add(feed, "cursor", {k: params.get(k) for k in ("x", "y", "kind", "ms")})
        elif kind == "live":
            meta = {k: params[k] for k in ("url", "title", "action", "busy") if k in params}
            feed.meta.update(meta)
            _add(feed, "live", dict(feed.meta))
        if _watching(feed) and time.monotonic() - feed.lease_sent > RENEW_EVERY:
            # Covers a child that has just started under an open viewer: its
            # hello is the first chance to put it under watch.
            _send_lease(feed)


def gone(user, proc):
    """`user`'s browser child exited."""
    with _lock:
        feed = _feeds.get(user)
        if feed is None or feed.proc is not proc:
            return
        feed.proc = None
        feed.frame = None
        feed.meta = {}
        _add(feed, "gone", {})


# ── what Flask calls ─────────────────────────────────────────

def watch(user):
    """A viewer is open on `user`'s agent browser. Call every few seconds for
    as long as it stays open."""
    with _lock:
        feed = _feed(user)
        feed.viewers = time.monotonic()
        if feed.proc is not None and time.monotonic() - feed.lease_sent > RENEW_EVERY:
            _send_lease(feed)


def status(user):
    """{live, url, title, action, busy} — whether `user`'s agent has a browser
    open, and what it's on."""
    with _lock:
        feed = _feeds.get(user)
        if feed is None or feed.proc is None:
            return {"live": False}
        return {"live": True, **feed.meta}


def snapshot(user):
    """(seq, frame, meta, live) for a viewer starting now."""
    with _lock:
        feed = _feed(user)
        return feed.seq, feed.frame, dict(feed.meta), feed.proc is not None


def wait(user, after, timeout):
    """(seq, items) — everything published after `after`, waiting up to
    `timeout` seconds for something. Of the frames, only the newest is
    returned: a viewer that fell behind skips to now rather than replaying."""
    with _lock:
        feed = _feed(user)
        feed.ready.wait_for(lambda: feed.seq != after, timeout)
        items = [item for item in feed.items if item[0] > after]
        seq = feed.seq
    newest_frame = max((s for s, kind, _ in items if kind == "frame"), default=None)
    return seq, [(kind, payload) for s, kind, payload in items
                 if kind != "frame" or s == newest_frame]
