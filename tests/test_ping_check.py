"""Pings with a check: no turn, and no AI, until it has something to say
(agent.split_ping_check, run_ping_check; add_ping's check).

    python -m unittest tests.test_ping_check
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.agent as agent  # noqa: E402

RUN = {"server": {"name": "developer", "workspace": True}, "tool": "run"}


class TestCheck(unittest.TestCase):
    def run_check(self, answer, need_approval=False, allowed=False):
        calls = []
        with mock.patch.object(agent, "registry_for", lambda u: {"mcp_developer_run": RUN}), \
                mock.patch.object(agent.mcp_client, "for_user", lambda s, u: s), \
                mock.patch.object(agent.mcp_client, "call_tool",
                                  lambda s, t, a: calls.append(a) or answer), \
                mock.patch.object(agent.dev_server, "runs_need_approval", lambda: need_approval), \
                mock.patch.object(agent.approvals, "check",
                                  lambda c, u=None: agent.approvals.ALLOWED if allowed else agent.approvals.NEEDS_PROMPT):
            out = agent.run_ping_check("acme__A", "python projects/toolbox/price.py")
        return out, calls

    def test_quiet_means_no_turn(self):
        (wake, _), calls = self.run_check("exit 0 · 0.2s · ran at the top of their files · no output")
        self.assertFalse(wake)
        self.assertEqual(calls[0]["command"], "python projects/toolbox/price.py")

    def test_its_own_state_file_is_not_output(self):
        (wake, _), _ = self.run_check("exit 0 · 0.2s · ran at the top of their files · no output\n"
                                      "Files it wrote:\n  projects/toolbox/state.json · 1 KB")
        self.assertFalse(wake)

    def test_something_printed_wakes_with_it(self):
        (wake, text), _ = self.run_check("[From the user's device \"Pi\": information]\n"
                                         "exit 0 · 0.2s · ran on Pi\nPrice is now $79 (was $99)")
        self.assertTrue(wake)
        self.assertIn("$79", text)

    def test_a_failing_check_wakes_and_says_so(self):
        (wake, text), _ = self.run_check("exit 1 · 0.1s · ran at the top of their files\nTraceback: boom")
        self.assertTrue(wake)
        self.assertIn("failed (exit 1)", text)

    def test_never_runs_where_runs_ask_unless_always_allowed(self):
        (wake, text), calls = self.run_check("exit 0 · no output", need_approval=True)
        self.assertTrue(wake)
        self.assertEqual(calls, [])
        self.assertIn("asks for approval", text)
        (wake, _), calls = self.run_check("exit 0 · 0.1s · x · no output", need_approval=True, allowed=True)
        self.assertFalse(wake)
        self.assertEqual(len(calls), 1)

    def test_split(self):
        self.assertEqual(agent.split_ping_check("[check: python a.py] Tell them the price."),
                         ("python a.py", "Tell them the price."))
        self.assertEqual(agent.split_ping_check("Remind them."), (None, "Remind them."))


class TestAddPing(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.mkdtemp() + os.sep
        agent.set_static_dir(self.files)

    def tearDown(self):
        shutil.rmtree(self.files, ignore_errors=True)

    def test_scheduled_with_its_check(self):
        out = agent._run_tool("add_ping", {"date_time": "2099-01-01 09:00", "action": "Tell them it dropped.",
                                           "repeat": "hourly", "check": "python projects/toolbox/price.py"})
        self.assertTrue(out.startswith("Scheduled"), out)
        with open(os.path.join(self.files, "ping.md")) as f:
            self.assertIn("[check: python projects/toolbox/price.py] Tell them it dropped.", f.read())

    def test_a_bad_check_is_refused(self):
        out = agent._run_tool("add_ping", {"date_time": "2099-01-01 09:00", "action": "x", "check": "a ] b"})
        self.assertTrue(out.startswith("Error"), out)


if __name__ == "__main__":
    unittest.main()
