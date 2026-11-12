"""The live browser's messages to its child never hold anyone up
(src/browser_live.py _Outbox).

    python tests/test_browser_live.py
"""
import os
import sys
import threading
import time
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

import src.browser_live as live  # noqa: E402


class FakeChild:
    """A child whose stdin can be made to stop reading (a full pipe)."""

    def __init__(self):
        self.sent = []
        self.reading = threading.Event()
        self.reading.set()
        self.dead = False

    def notify(self, method, params=None):
        self.reading.wait()
        if self.dead:
            return False
        self.sent.append((method, params))
        return True

    def alive(self):
        return not self.dead


def wait_for(cond, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def timed(fn, *a):
    started = time.monotonic()
    out = fn(*a)
    return out, time.monotonic() - started


class TestOutbox(unittest.TestCase):

    def setUp(self):
        live._feeds.clear()
        live._outboxes.clear()
        self.user = "u" + os.urandom(3).hex()
        self.child = FakeChild()
        live.publish(self.user, self.child, "live", {"url": "https://example.com"})
        self.assertTrue(live.take(self.user))

    def tearDown(self):
        self.child.reading.set()

    def inputs(self):
        return [p for m, p in self.child.sent if m == live.INPUT]

    def test_control_outlasts_a_trip_away(self):
        """Hidden, the Browser app can't renew: the lease it takes on the way
        out lasts the trip, and the next ordinary renewal is short again."""
        real = time.monotonic
        try:
            self.assertTrue(live.take(self.user, away=True))
            time.monotonic = lambda: real() + live.CONTROL_LEASE + 60
            self.assertTrue(live.holding(self.user))
            time.monotonic = lambda: real() + live.AWAY_LEASE + 5
            self.assertFalse(live.holding(self.user))
            time.monotonic = real
            self.assertTrue(live.take(self.user))
            time.monotonic = lambda: real() + live.CONTROL_LEASE + 5
            self.assertFalse(live.holding(self.user))
        finally:
            time.monotonic = real
        self.assertTrue(wait_for(lambda: any(
            m.endswith("/control") and p == {"seconds": live.AWAY_LEASE} for m, p in self.child.sent)))

    def test_in_order(self):
        live.send_input(self.user, [{"kind": "text", "text": "pass"}, {"kind": "key", "key": "Enter"}])
        self.assertTrue(wait_for(lambda: len(self.inputs()) == 2))
        self.assertEqual([p["kind"] for p in self.inputs()], ["text", "key"])

    def test_a_child_that_stops_reading_holds_nobody_up(self):
        """Tonight's hang: the pipe full, the next keystrokes, the polls and
        the frames coming up all used to wait behind the write."""
        self.child.reading.clear()
        live.send_input(self.user, [{"kind": "click", "x": 1, "y": 1}])
        ok, took = timed(live.send_input, self.user, [{"kind": "key", "key": "a"}])
        self.assertTrue(ok)
        self.assertLess(took, 0.5)
        _, took = timed(live.status, self.user)
        self.assertLess(took, 0.5, "status waited on the child")
        _, took = timed(live.take, self.user)
        self.assertLess(took, 0.5, "the control poll waited on the child")
        _, took = timed(live.publish, self.user, self.child, "frame", {"data": "AAAA"})
        self.assertLess(took, 0.5, "the frames from the child waited")
        other = FakeChild()
        _, took = timed(live.publish, "someone-else", other, "live", {"url": "x"})
        self.assertLess(took, 0.5, "another user's browser waited")
        self.child.reading.set()
        self.assertTrue(wait_for(lambda: [p["kind"] for p in self.inputs()] == ["click", "key"]))

    def test_mouse_moves_dont_pile_up(self):
        self.child.reading.clear()
        live.send_input(self.user, [{"kind": "down", "x": 0, "y": 0}])
        for i in range(300):
            live.send_input(self.user, [{"kind": "move", "x": i, "y": i}])
        live.send_input(self.user, [{"kind": "up", "x": 299, "y": 299}])
        self.child.reading.set()
        self.assertTrue(wait_for(lambda: self.inputs() and self.inputs()[-1]["kind"] == "up"))
        kinds = [p["kind"] for p in self.inputs()]
        self.assertEqual(kinds.count("move"), 1, "the moves behind each other became the last one")
        self.assertEqual([p for p in self.inputs() if p["kind"] == "move"][0]["x"], 299)
        self.assertEqual(kinds[0], "down")

    def test_keys_are_never_dropped_until_the_cap(self):
        self.child.reading.clear()
        sent = [live.send_input(self.user, [{"kind": "key", "key": "a"}]) for _ in range(live.OUTBOX_MAX + 20)]
        self.assertTrue(all(sent[:live.OUTBOX_MAX - 5]))
        self.assertFalse(sent[-1], "a child that far behind refuses more")

    def test_a_gone_child_closes_its_outbox(self):
        box = live._outboxes[self.child]
        live.gone(self.user, self.child)
        self.assertTrue(box.closed)
        self.assertNotIn(self.child, live._outboxes)
        self.assertFalse(live.send_input(self.user, [{"kind": "key", "key": "a"}]))

    def test_a_dead_child_stops_the_writer(self):
        box = live._outboxes[self.child]
        self.child.dead = True
        live.send_input(self.user, [{"kind": "key", "key": "a"}])
        self.assertTrue(wait_for(lambda: box.closed))

    def test_release_still_waits_for_the_handback(self):
        def hand_back():
            wait_for(lambda: any(m == "notifications/freeclaw/control" and p == {"seconds": 0}
                                 for m, p in self.child.sent))
            live.publish(self.user, self.child, "control", {"held": False, "url": "https://example.com"})
        threading.Thread(target=hand_back, daemon=True).start()
        result = live.release(self.user, timeout=5)
        self.assertEqual(result and result.get("held"), False)


if __name__ == "__main__":
    unittest.main()
