"""A human-verification check in a run nobody is watching (a scheduled ping)
is reported, not handed to a user who isn't there (agent._hand_over_page).

    python tests/test_captcha_unattended.py
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import src.agent as agent  # noqa: E402


class CaptchaUnattended(unittest.TestCase):
    def test_scheduled_run_reports_instead_of_waiting(self):
        with mock.patch.object(agent.approvals, "is_interactive", lambda: False), \
                mock.patch.object(agent.browser_handoff, "accept") as accept:
            out = agent._hand_over_page("shot", "Cloudflare check")
        accept.assert_not_called()
        self.assertIn("Nobody can solve this check right now", out)
        self.assertIn("couldn't look", out)

    def test_someone_watching_still_gets_the_button(self):
        with mock.patch.object(agent.approvals, "is_interactive", lambda: True), \
                mock.patch.object(agent.approvals, "current_user", lambda: "alice"), \
                mock.patch.object(agent.browser_handoff, "accept",
                                  return_value=("https://shop.example/", None)) as accept:
            out = agent._hand_over_page("shot", "Cloudflare check")
        accept.assert_called_once()
        self.assertIn("press it, solve it", out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
