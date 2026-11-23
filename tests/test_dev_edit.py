"""The Developer app's forgiving edit and its patch tool (src/dev_edit.py,
src/dev_server.py t_edit / t_patch).

    python -m unittest tests.test_dev_edit
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.dev_edit as dev_edit  # noqa: E402
import src.dev_server as dev_server  # noqa: E402


class TestFind(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(dev_edit.find("a = 1\nb = 2\n", "b = 2")[2], 0)

    def test_trailing_spaces_in_the_file(self):
        text = "def f():   \n    return 'x'\n"
        start, end, tier = dev_edit.find(text, "def f():\n    return 'x'\n")
        self.assertEqual((tier, text[start:end]), (1, text))

    def test_curly_quotes_and_dashes_from_the_model(self):
        text = 'print("it\'s - here")\nx = 1\n'
        start, end, tier = dev_edit.find(text, "print(“it’s – here”)")
        self.assertEqual(tier, 2)
        self.assertEqual(text[start:end], 'print("it\'s - here")')

    def test_a_loose_match_that_is_ambiguous_is_no_match(self):
        start, count, _ = dev_edit.find("x = 'a'  \ny\nx = 'a'\n", "x = ‘a’")
        self.assertIsNone(start)
        self.assertEqual(count, 2)

    def test_nowhere(self):
        self.assertEqual(dev_edit.find("abc\n", "zzz")[:2], (None, 0))


class TestPatchFormat(unittest.TestCase):
    def test_parse_all_three(self):
        ops = dev_edit.parse("*** Begin Patch\n*** Add File: a.txt\n+hi\n*** Update File: b.py\n"
                             "*** Move to: c.py\n@@ def f():\n-    x\n+    y\n*** Delete File: d.txt\n*** End Patch")
        self.assertEqual([o["op"] for o in ops], ["add", "update", "delete"])
        self.assertEqual(ops[0]["content"], "hi\n")
        self.assertEqual(ops[1]["move_to"], "c.py")
        self.assertEqual(ops[1]["hunks"][0]["header"], "def f():")

    def test_fenced_patch_is_fine(self):
        self.assertEqual(len(dev_edit.parse("```\n*** Begin Patch\n*** Delete File: x\n*** End Patch\n```")), 1)

    def test_mistakes_are_said(self):
        for bad, said in (("", "empty"), ("*** Add File: x\n+a", "starts with"),
                          ("*** Begin Patch\n*** Add File: x\nno plus\n*** End Patch", "'+'"),
                          ("*** Begin Patch\nhello\n*** End Patch", "isn't a file header")):
            with self.assertRaises(dev_edit.EditError) as e:
                dev_edit.parse(bad)
            self.assertIn(said, str(e.exception))

    def test_update_keeps_crlf_and_applies_in_order(self):
        text = "a\r\nb\r\nc\r\nb\r\n"
        hunks = dev_edit.parse("*** Begin Patch\n*** Update File: f\n@@ c\n-b\n+B\n*** End Patch")[0]["hunks"]
        self.assertEqual(dev_edit.apply_update(text, hunks), "a\r\nb\r\nc\r\nB\r\n")

    def test_update_that_does_not_match_says_what(self):
        hunks = dev_edit.parse("*** Begin Patch\n*** Update File: f\n-nope\n+x\n*** End Patch")[0]["hunks"]
        with self.assertRaises(dev_edit.EditError) as e:
            dev_edit.apply_update("a\n", hunks, "f")
        self.assertIn("nope", str(e.exception))


class TestTools(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp()
        self.root = os.path.join(self.top, "projects")
        os.makedirs(os.path.join(self.root, "app"))
        self.tools = dev_server.Tools()
        with open(os.path.join(self.root, "app", "main.py"), "w", newline="") as f:
            f.write("def greet():\r\n    print('Hi')   \r\n")
        with open(os.path.join(self.root, "app", "old.txt"), "w") as f:
            f.write("bye\n")

    def tearDown(self):
        shutil.rmtree(self.top, ignore_errors=True)

    def read(self, rel):
        with open(os.path.join(self.root, rel), newline="") as f:
            return f.read()

    def test_edit_forgives_and_keeps_crlf(self):
        out = self.tools.t_edit(self.root, {"path": "projects/app/main.py", "old": "    print(‘Hi’)\n",
                                            "new": "    print('Hello')\n    return 1\n"})
        self.assertIn("matched ignoring", out)
        self.assertEqual(self.read("app/main.py"), "def greet():\r\n    print('Hello')\r\n    return 1\r\n")

    def test_patch_does_it_all(self):
        out = self.tools.t_patch(self.root, {"patch": (
            "*** Begin Patch\n*** Add File: projects/app/new.py\n+x = 1\n"
            "*** Update File: projects/app/main.py\n*** Move to: projects/app/app.py\n"
            "@@ def greet():\n-    print('Hi')\n+    print('Hello')\n"
            "*** Delete File: projects/app/old.txt\n*** End Patch")})
        self.assertIn("added projects/app/new.py", out)
        self.assertEqual(self.read("app/new.py"), "x = 1\n")
        self.assertEqual(self.read("app/app.py"), "def greet():\r\n    print('Hello')\r\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "app", "main.py")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "app", "old.txt")))

    def test_patch_all_or_nothing(self):
        with self.assertRaises(dev_server.ToolError) as e:
            self.tools.t_patch(self.root, {"patch": (
                "*** Begin Patch\n*** Add File: projects/app/new.py\n+x = 1\n"
                "*** Update File: projects/app/main.py\n-not there\n+y\n*** End Patch")})
        self.assertIn("Nothing was changed", str(e.exception))
        self.assertFalse(os.path.exists(os.path.join(self.root, "app", "new.py")))

    def test_patch_stays_in_the_workspace(self):
        with self.assertRaises(dev_server.ToolError):
            self.tools.t_patch(self.root, {"patch": "*** Begin Patch\n*** Add File: ../x\n+a\n*** End Patch"})


if __name__ == "__main__":
    unittest.main()


class TestProjectInstructions(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp()
        self.root = os.path.join(self.top, "projects")
        os.makedirs(os.path.join(self.root, "repo", "src"))
        with open(os.path.join(self.root, "repo", "src", "a.py"), "w") as f:
            f.write("x = 1\n")
        self.tools = dev_server.Tools()

    def tearDown(self):
        shutil.rmtree(self.top, ignore_errors=True)

    def test_none_without_them(self):
        self.assertNotIn("own instructions", self.tools.t_overview(self.root, {"path": "projects/repo"}))

    def test_agents_md_from_the_repo_even_in_a_subfolder(self):
        with open(os.path.join(self.root, "repo", "CLAUDE.md"), "w") as f:
            f.write("Claude notes")
        with open(os.path.join(self.root, "repo", "AGENTS.md"), "w") as f:
            f.write("Run tests with make test.\n")
        out = self.tools.t_overview(self.root, {"path": "projects/repo/src"})
        self.assertIn("# AGENTS.md instructions for projects/repo\n\n<INSTRUCTIONS>", out)
        self.assertIn("Run tests with make test.", out)
        self.assertNotIn("Claude notes", out)

    def test_claude_md_when_no_agents_md_and_long_ones_point_to_the_rest(self):
        with open(os.path.join(self.root, "repo", "CLAUDE.md"), "w") as f:
            f.write("rule\n" * 2000)
        out = self.tools.t_overview(self.root, {"path": "projects/repo"})
        self.assertIn("CLAUDE.md", out)
        self.assertIn("the rest is in projects/repo/CLAUDE.md", out)
