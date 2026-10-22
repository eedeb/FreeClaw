"""browser_do's handback says the task goes on (src/fast_browser.py).

    python tests/test_fast_browser_handback.py
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import src.fast_browser as fast_browser  # noqa: E402

PAGE = {"title": "Home", "url": "https://example.org/home",
        "elements": [{"kind": "link", "text": "Event", "x": 1, "y": 2}]}


def run(decisions):
    with mock.patch.object(fast_browser.jev, "browser_step", side_effect=decisions):
        return fast_browser.run("open the event", lambda args: dict(PAGE), lambda *a: None,
                                lambda: False)[0]


class TestHandback(unittest.TestCase):
    def test_unsure_before_anything(self):
        text = run([{"move": "unsure"}])
        self.assertIn("Nothing was done", text)
        self.assertIn("isn't a dead end", text)

    def test_unsure_after_a_click(self):
        text = run([{"move": "click", "element": PAGE["elements"][0]}, {"move": "unsure"}])
        self.assertIn("Did: clicked", text)
        self.assertNotIn("Nothing was done", text)
        self.assertIn("isn't a dead end", text)

    def test_done_is_not_a_handback(self):
        self.assertNotIn("dead end", run([{"move": "done"}]))


if __name__ == "__main__":
    unittest.main()
