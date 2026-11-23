"""A refusal can say what to do instead (src/approvals.py resolve, denial_message).

    python -m unittest tests.test_approval_feedback
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.approvals as approvals  # noqa: E402


class TestFeedback(unittest.TestCase):
    def ask(self, decision, feedback=None):
        req = approvals.open_request("rm -rf build", user="u")
        ok, _ = approvals.resolve(req.id, decision, feedback)
        self.assertTrue(ok)
        return req, approvals.wait(req)

    def test_deny_with_words_passes_them_on(self):
        req, decision = self.ask(approvals.DECISION_DENY, "  just delete build/cache\n please ")
        self.assertEqual(req.feedback, "just delete build/cache please")
        text = approvals.denial_message(decision, req.feedback)
        self.assertIn('"just delete build/cache please"', text)
        self.assertIn("Do what they said", text)

    def test_plain_deny_still_stops(self):
        req, decision = self.ask(approvals.DECISION_DENY)
        self.assertIn("stop", approvals.denial_message(decision, req.feedback))

    def test_words_with_an_allow_are_ignored(self):
        req, decision = self.ask(approvals.DECISION_ONCE, "whatever")
        self.assertEqual(req.feedback, "")
        self.assertTrue(approvals.was_approved(decision))

    def test_capped(self):
        req, _ = self.ask(approvals.DECISION_DENY, "x" * 5000)
        self.assertEqual(len(req.feedback), approvals.FEEDBACK_MAX)


if __name__ == "__main__":
    unittest.main()
