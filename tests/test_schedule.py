"""Tests for the Scheduled tasks window: /schedule and /api/schedule
(Flask/main.py), and the ping.md lock it shares with add_ping and the
scheduler (src/agent.py).

Run from the repo root:  python -m unittest discover -s tests

Imports FreeClaw itself, so it needs its requirements installed; skipped with
a note where they aren't.
"""

import os
import shutil
import sys
import threading
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def stamp(delta):
    return (datetime.now() + delta).strftime("%Y-%m-%d %H:%M")


class TestScheduleApi(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(ROOT, "Flask"))
        try:
            import src.agent as agent
            import main
        except Exception as e:                               # pragma: no cover
            raise unittest.SkipTest(f"Couldn't import FreeClaw: {e}")
        cls.agent, cls.main = agent, main

    def setUp(self):
        self.user = "schedtest-" + os.urandom(3).hex()
        os.makedirs(os.path.join(self.main.STATIC_DIR, self.user, "files"), exist_ok=True)
        self.addCleanup(shutil.rmtree, os.path.join(self.main.STATIC_DIR, self.user), True)
        self.path = self.main.user_ping_path(self.user)
        self.client = self.main.app.test_client()
        with self.client.session_transaction() as s:
            s["authenticated"] = True
            s["current_user"] = self.user

    def lines(self):
        return self.agent.read_ping_lines(self.path)

    def add(self, when, action, repeat=""):
        return self.client.post("/api/schedule", json={"user": self.user, "when": when,
                                                       "action": action, "repeat": repeat})

    def test_page(self):
        r = self.client.get(f"/schedule?user={self.user}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Scheduled tasks", r.data)
        self.assertIn(b"icon-schedule", self.client.get(f"/chat?user={self.user}").data)

    def test_signed_out(self):
        anon = self.main.app.test_client()
        self.assertEqual(anon.get(f"/api/schedule?user={self.user}").status_code, 401)
        self.assertEqual(anon.get("/schedule").status_code, 302)

    def test_list_add_edit_remove(self):
        empty = self.client.get(f"/api/schedule?user={self.user}").get_json()
        self.assertEqual(empty["tasks"], [])
        self.assertEqual(empty["repeats"], ["hourly", "daily", "weekly"])

        later, sooner = stamp(timedelta(days=2)), stamp(timedelta(hours=3))
        r = self.add(later, "Water the plants\n  on the balcony", "daily")
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.add(sooner, "Call the dentist")
        tasks = r.get_json()["tasks"]
        self.assertEqual([t["action"] for t in tasks], ["Call the dentist", "Water the plants on the balcony"])
        self.assertEqual(tasks[1]["repeat"], "daily")
        self.assertEqual(tasks[1]["when"], later)
        self.assertEqual(self.lines(), [f"{sooner} - Call the dentist",
                                        f"{later} [daily] - Water the plants on the balcony"])

        # Edit: the line is replaced, and the file stays sorted.
        moved = stamp(timedelta(days=5))
        r = self.client.put("/api/schedule", json={"user": self.user, "line": tasks[0]["line"],
                                                   "when": moved, "action": "Call the dentist back", "repeat": "weekly"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.lines(), [f"{later} [daily] - Water the plants on the balcony",
                                        f"{moved} [weekly] - Call the dentist back"])

        # A refused edit leaves the task as it was.
        r = self.client.put("/api/schedule", json={"user": self.user, "line": self.lines()[0],
                                                   "when": stamp(-timedelta(days=1)), "action": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("in the past", r.get_json()["error"])
        self.assertNotIn("Work the date out", r.get_json()["error"])
        self.assertEqual(len(self.lines()), 2)
        self.assertIn("Water the plants", self.lines()[0])

        r = self.client.delete(f"/api/schedule?user={self.user}&line=" + self.lines()[0].replace(" ", "%20").replace("[", "%5B").replace("]", "%5D"))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.lines(), [f"{moved} [weekly] - Call the dentist back"])

    def test_gone_line_is_409_with_the_list(self):
        self.add(stamp(timedelta(hours=1)), "Stretch")
        r = self.client.delete(f"/api/schedule?user={self.user}&line=nope")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(r.get_json()["tasks"]), 1)
        r = self.client.put("/api/schedule", json={"user": self.user, "line": "nope",
                                                   "when": stamp(timedelta(hours=2)), "action": "x"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self.lines()), 1)

    def test_refusals(self):
        self.assertIn("required", self.add(stamp(timedelta(hours=1)), "   ").get_json()["error"])
        self.assertIn("couldn't parse", self.add("soon", "x").get_json()["error"].lower())
        self.assertIn("isn't a repeat", self.add(stamp(timedelta(hours=1)), "x", "yearly").get_json()["error"])
        when = stamp(timedelta(hours=1))
        self.assertEqual(self.add(when, "Same").status_code, 200)
        self.assertIn("already scheduled", self.add(when, "Same").get_json()["error"])
        self.assertEqual(len(self.lines()), 1)

    def test_lists_retries_and_unreadable_lines(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(f"{stamp(timedelta(minutes=5))} - (retry) Send the report\nsometime - Mystery\n")
        tasks = self.client.get(f"/api/schedule?user={self.user}").get_json()["tasks"]
        self.assertEqual(tasks[0]["action"], "Send the report")
        self.assertTrue(tasks[0]["retry"])
        self.assertIsNone(tasks[1]["when"])
        r = self.client.delete(f"/api/schedule?user={self.user}&line=sometime%20-%20Mystery")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.lines()), 1)

    def test_window_adds_through_schedule_ping(self):
        calls = []
        real = self.agent.schedule_ping
        self.agent.schedule_ping = lambda *a: calls.append(a) or "Error: held — no"
        try:
            r = self.add(stamp(timedelta(hours=1)), "x")
            self.assertEqual(r.get_json()["error"], "Held.")
        finally:
            self.agent.schedule_ping = real
        self.assertEqual(len(calls), 1)

    def test_add_ping_tool(self):
        sessions = self.main.sessions
        sess = sessions.for_user(self.user)
        sess.static_dir = os.path.dirname(self.path) + os.sep
        when = stamp(timedelta(hours=4))
        with sessions.use(sess):
            out = self.agent._run_tool("add_ping", {"date_time": when, "action": "Feed the cat", "repeat": "daily"})
            self.assertTrue(out.startswith("Scheduled for"), out)
            self.assertEqual(self.lines(), [f"{when} [daily] - Feed the cat"])
            self.assertTrue(self.agent._run_tool("cancel_ping", {"match": "cat"}).startswith("Cancelled"))
        self.assertEqual(self.lines(), [])

    def test_scheduler_waits_for_the_file_lock(self):
        due = stamp(-timedelta(minutes=1))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(f"{due} - Now\n")
        out = []
        with self.agent.ping_file_lock(self.path):
            t = threading.Thread(target=lambda: out.append(self.main._pop_due_pings(self.user, datetime.now())))
            t.start()
            t.join(0.3)
            self.assertTrue(t.is_alive(), "the scheduler read ping.md while it was locked")
        t.join(5)
        self.assertEqual(out[0][0][2], "Now")
        self.assertEqual(self.lines(), [])


if __name__ == "__main__":
    unittest.main()
