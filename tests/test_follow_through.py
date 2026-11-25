"""Follow-through (src/jev.py follow_through, src/agent.py _follow_through):
when a finished reply gets the automatic "before you finish" nudge.

Each case is a reply's real Jev scores from a hosted install (2026-11-25):
of 28 nudges in 438 checked replies, 24 were not needed: scheduled runs,
"I'll check it every day at 7" (scheduled), finished answers that mention
what couldn't be checked.

    python -m unittest tests.test_follow_through
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import agent, jev  # noqa: E402


def nudged(status, shares, promise):
    answers = {"status": {"choice": status, "probabilities": shares}, "promise": {"noul": promise}}
    with mock.patch.object(jev, "enabled", lambda: True), \
            mock.patch.object(jev, "ask", lambda state, questions: answers):
        return jev.follow_through("request", [], "reply")["nudge"]


class TestNudge(unittest.TestCase):
    def test_real_work_left_undone_is_still_nudged(self):
        # 'Does it work?' -> 'No, not as a fishing tool yet'; the TV still searching.
        self.assertTrue(nudged("partway", {"done": 0.06, "partway": 0.94, "needs_user": 0, "cant": 0}, 0.07))
        self.assertTrue(nudged("partway", {"done": 0.02, "partway": 0.98, "needs_user": 0, "cant": 0}, 0.07))

    def test_a_finished_answer_mentioning_what_it_couldnt_check(self):
        self.assertFalse(nudged("partway", {"done": 0.26, "partway": 0.74, "needs_user": 0, "cant": 0}, 0.02))

    def test_scheduled_or_saved_future_work_isnt_a_broken_promise(self):
        # "Scheduled for 7 a.m. every day; I'll check…" / "I'll check that site as a common place".
        self.assertFalse(nudged("done", {"done": 0.82, "partway": 0.16, "needs_user": 0.02, "cant": 0}, 0.92))
        self.assertFalse(nudged("done", {"done": 0.94, "partway": 0.06, "needs_user": 0, "cant": 0}, 0.9))

    def test_a_promise_with_the_work_plainly_not_done(self):
        self.assertTrue(nudged("partway", {"done": 0.1, "partway": 0.6, "needs_user": 0, "cant": 0.05}, 0.9))

    def test_waiting_on_the_user_is_never_nudged(self):
        self.assertFalse(nudged("partway", {"done": 0, "partway": 0.7, "needs_user": 0.3, "cant": 0}, 0.95))


class TestScheduledRuns(unittest.TestCase):
    def setUp(self):
        self.asked = []
        self.sess = types.SimpleNamespace(
            turn_route=None, turn_tool_names=[], turn_prefix="x", turn_follow_ups=0,
            messages=[{"role": "system", "content": "s"},
                      {"role": "user", "content": f"{agent.PING_PREFIX} [due 2026-11-25 07:53]: brief"}])
        patches = [mock.patch.object(agent, "_sess", lambda: self.sess),
                   mock.patch.object(agent, "_tools_user", lambda: "u"),
                   mock.patch.object(agent.cancellation, "is_stopped", lambda: False),
                   mock.patch.object(agent.jev, "enabled", lambda: True),
                   mock.patch.object(agent.jev, "follow_through",
                                     lambda *a, **k: self.asked.append(a) or {"nudge": True})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_scheduled_tasks_run_is_never_nudged(self):
        self.assertIsNone(agent._follow_through("Here's your brief."))
        self.assertEqual(self.asked, [])

    def test_a_message_from_the_person_is_still_checked(self):
        self.sess.messages[1]["content"] = "Plan my day"
        self.assertEqual(agent._follow_through("Here's your plan."), {"nudge": True})
        self.assertTrue(self.asked)


if __name__ == "__main__":
    unittest.main()
