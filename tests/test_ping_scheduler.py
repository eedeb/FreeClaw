"""The ping scheduler's pass (Flask/main.py): it waits on nobody. A user who
is mid-turn is passed over and keeps their ping, and everybody else's fire.

Run from the repo root:  python -m unittest discover -s tests
"""

import os
import shutil
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class TestSchedulerPass(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(ROOT, "Flask"))
        try:
            import src.agent as agent
            import main
        except Exception as e:                               # pragma: no cover
            raise unittest.SkipTest(f"Couldn't import FreeClaw: {e}")
        cls.agent, cls.main = agent, main

    def make_user(self):
        user = "pingtest-" + os.urandom(3).hex()
        os.makedirs(os.path.join(self.main.STATIC_DIR, user, "files"), exist_ok=True)
        self.addCleanup(shutil.rmtree, os.path.join(self.main.STATIC_DIR, user), True)
        when = (datetime.now() - timedelta(minutes=1)).strftime(self.agent.PING_TIME_FORMAT)
        with open(self.main.user_ping_path(user), "w") as f:
            f.write(f"{when} - say hello\n")
        return user

    def test_a_busy_user_does_not_hold_up_the_others(self):
        busy, free = self.make_user(), self.make_user()
        ran = []

        def fake_stream(user_input=None, ping=False, **kw):
            ran.append(self.agent._sess().name)
            self.agent.get_messages().append({"role": "assistant", "content": "hello"})
            yield {"type": "token", "text": "hello"}

        # Somebody else's turn holds `busy`'s conversation lock.
        release = threading.Event()
        held = threading.Event()

        def hold():
            with self.main.sessions.for_user(busy).lock:
                held.set()
                release.wait(20)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        held.wait(5)
        try:
            with mock.patch.object(self.main, "list_users", return_value=[busy, free]), \
                    mock.patch.object(self.agent, "agent_stream", fake_stream):
                started = time.monotonic()
                self.main._fire_due_pings(wait=True)
                self.assertLess(time.monotonic() - started, 10)
                self.assertEqual(ran, [free])
                # Still on file for the pass after their turn ends.
                self.assertEqual(len(self.agent.read_ping_lines(self.main.user_ping_path(busy))), 1)
                release.set()
                holder.join(5)
                self.main._fire_due_pings(wait=True)
                self.assertEqual(ran, [free, busy])
        finally:
            release.set()
        self.assertEqual(self.agent.read_ping_lines(self.main.user_ping_path(busy)), [])


if __name__ == "__main__":
    unittest.main()
