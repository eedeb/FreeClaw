"""Tests for the theme-coloured scrollbars (Flask/templates/_scrollbars.html):
every page includes them, they're coloured from tokens each page defines, and
they render.

Run from the repo root:  python -m unittest discover -s tests
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "Flask", "templates")
INCLUDE = '{% include "_scrollbars.html" %}'


def pages():
    for name in sorted(os.listdir(TEMPLATES)):
        if name.endswith(".html") and not name.startswith("_"):
            with open(os.path.join(TEMPLATES, name), encoding="utf-8") as f:
                yield name, f.read()


class TestScrollbars(unittest.TestCase):

    def test_every_page_includes_them_at_the_end_of_its_head(self):
        found = list(pages())
        self.assertTrue(found)
        for name, text in found:
            with self.subTest(page=name):
                self.assertEqual(text.count(INCLUDE), 1)
                self.assertLess(text.index(INCLUDE), text.index("</head>"))
                self.assertNotIn("<", text[text.index(INCLUDE) + len(INCLUDE):text.index("</head>")])

    def test_every_page_defines_the_tokens_they_use(self):
        for name, text in pages():
            with self.subTest(page=name):
                self.assertRegex(text, r"--muted\s*:")
                self.assertRegex(text, r"--accent-line\s*:")

    def test_no_scroller_swaps_them_for_the_default(self):
        # In Chrome a scroller with scrollbar-width set (other than none, which
        # hides it) drops the ::-webkit-scrollbar styling for the browser's own
        # bar: the chat's sidebar had "thin" and showed the default one.
        import re
        for name in os.listdir(TEMPLATES):
            if not name.endswith(".html") or name == "_scrollbars.html":
                continue
            with open(os.path.join(TEMPLATES, name), encoding="utf-8") as f:
                text = f.read()
            for value in re.findall(r"scrollbar-width:\s*([a-z-]+)", text):
                with self.subTest(page=name):
                    self.assertEqual(value, "none")

    def test_they_render(self):
        from jinja2 import Environment, FileSystemLoader
        out = Environment(loader=FileSystemLoader(TEMPLATES)).get_template("_scrollbars.html").render()
        self.assertTrue(out.lstrip().startswith("<style>"))
        self.assertIn("::-webkit-scrollbar-thumb", out)
        self.assertIn("@supports not selector(::-webkit-scrollbar)", out)
        self.assertEqual(out.count("{"), out.count("}"))


if __name__ == "__main__":
    unittest.main()
