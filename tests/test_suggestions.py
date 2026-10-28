"""Tests for the chat's welcome-screen suggestions (Flask/templates/chat.html):
a pool of whole errands, four shown each visit, always one that runs on a
schedule, and the four in the page itself drawn from the same pool.

Run from the repo root:  python -m unittest discover -s tests

The picking runs with node when it's installed.
"""

import html
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAT = os.path.join(ROOT, "Flask", "templates", "chat.html")


class TestSuggestions(unittest.TestCase):

    def setUp(self):
        with open(CHAT, encoding="utf-8") as f:
            self.text = f.read()
        start = self.text.index("  const SUGGESTIONS = [")
        end = self.text.index("  (function pickSuggestions()")
        self.pool_js = self.text[start:end]
        self.pick_js = self.text[end:self.text.index("  // They only fill the composer")]

    def pool(self):
        if not shutil.which("node"):
            raise unittest.SkipTest("node isn't installed")
        out = subprocess.run(["node", "-e", self.pool_js + "console.log(JSON.stringify(SUGGESTIONS));"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_pool(self):
        pool = self.pool()
        self.assertGreaterEqual(len(pool), 8)
        self.assertGreaterEqual(sum(1 for s in pool if s.get("scheduled")), 2)
        self.assertGreaterEqual(sum(1 for s in pool if not s.get("scheduled")), 3)
        self.assertEqual(len({s["title"] for s in pool}), len(pool))
        for s in pool:
            self.assertTrue(s["title"] and s["sub"] and s["prompt"], s)
            self.assertGreater(len(s["prompt"]), 60, "a whole errand, not a one-liner")
            self.assertNotIn("weather", s["prompt"].lower())

    def test_page_shows_four_from_the_pool(self):
        pool = {s["prompt"] for s in self.pool()}
        shown = [html.unescape(p) for p in re.findall(r'class="suggestion" data-prompt="([^"]*)"', self.text)]
        self.assertEqual(len(shown), 4)
        for p in shown:
            self.assertIn(p, pool)

    def test_picking(self):
        """Every visit: four, the last on a schedule, the first two (all a
        narrow screen shows) errands."""
        self.pool()
        dom = """
        const made = [];
        const box = { set textContent(_) { made.length = 0; },
                      appendChild(b) { made.push(b); return b; } };
        const document = {
          querySelector: () => box,
          createElement: tag => ({ tag, dataset: {}, kids: [], textContent: '',
                                   appendChild(c) { this.kids.push(c); return c; } }),
        };
        """
        prog = (dom + self.pool_js + "const runs = [];\nfor (let i = 0; i < 200; i++) {\n"
                + self.pick_js + "\nruns.push(made.map(b => ({ title: b.kids[0].textContent, "
                "scheduled: !!SUGGESTIONS.find(s => s.title === b.kids[0].textContent).scheduled })));\n}\n"
                "console.log(JSON.stringify(runs));")
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        runs = json.loads(out.stdout)
        seen = set()
        for run in runs:
            self.assertEqual(len(run), 4)
            self.assertEqual(len({c["title"] for c in run}), 4)
            self.assertEqual([c["scheduled"] for c in run], [False, False, False, True])
            seen.update(c["title"] for c in run)
        self.assertEqual(len(seen), len(self.pool()), "every suggestion gets shown sometimes")


if __name__ == "__main__":
    unittest.main()
