"""The main agent can add to a running sub-agent's work, or stop it
(src/subagents.py message / take_messages).

    python -m unittest tests.test_subagent_messages
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.subagents as subagents  # noqa: E402


class TestMessages(unittest.TestCase):
    def setUp(self):
        self.child = types.SimpleNamespace()
        self.parent = types.SimpleNamespace(name="acme__Boss")
        p = [mock.patch.object(subagents, "children", lambda parent: ["acme__Boss-subagent-1"]),
             mock.patch.object(subagents, "display_name", lambda n: n.split("__", 1)[-1]),
             mock.patch.object(subagents.sessions, "for_user", lambda n: self.child),
             mock.patch.object(subagents.cancellation, "request_stop", lambda s: setattr(s, "stop", True))]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def test_by_any_of_its_names(self):
        for name in ("Boss-subagent-1", "acme__Boss-subagent-1", "1", '"boss-subagent-1"'):
            self.assertEqual(subagents._find("acme__Boss", name), "acme__Boss-subagent-1", name)
        self.assertIsNone(subagents._find("acme__Boss", "2"))

    def test_messages_wait_and_are_taken_once(self):
        out = subagents.message(self.parent, "Boss-subagent-1", "Also run the tests.")
        self.assertIn("Sent to", out)
        subagents.message(self.parent, "1", "And push.")
        self.assertEqual(subagents.take_messages(self.child), ["Also run the tests.", "And push."])
        self.assertEqual(subagents.take_messages(self.child), [])

    def test_stop(self):
        subagents.message(self.parent, "1", "pending")
        out = subagents.message(self.parent, "1", "", stop=True)
        self.assertIn("Stopping", out)
        self.assertTrue(self.child.stop and self.child.stopped_by_parent)
        self.assertEqual(subagents.take_messages(self.child), [])

    def test_unknown_or_empty(self):
        self.assertIn("running: \"Boss-subagent-1\"", subagents.message(self.parent, "7", "x"))
        self.assertIn("Nothing to send", subagents.message(self.parent, "1", "  "))


if __name__ == "__main__":
    unittest.main()
