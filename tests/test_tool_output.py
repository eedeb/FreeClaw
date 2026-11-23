"""A tool result too long to keep whole is kept whole in a file too
(src/agent.py _clip_tool_result, _save_tool_output).

    python -m unittest tests.test_tool_output
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.agent as agent  # noqa: E402


class TestToolOutput(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.mkdtemp()
        agent.set_static_dir(self.files)

    def tearDown(self):
        shutil.rmtree(self.files, ignore_errors=True)

    def test_short_results_are_untouched(self):
        self.assertEqual(agent._clip_tool_result("ok", "x"), "ok")
        self.assertFalse(os.path.exists(os.path.join(self.files, agent.TOOL_OUTPUT_DIR)))

    def test_a_long_one_is_clipped_and_kept_whole(self):
        text = "".join(f"line {n}\n" for n in range(40_000))
        out = agent._clip_tool_result(text, "mcp_developer_run")
        self.assertLess(len(out), len(text))
        saved = out.split("The whole result is saved in ")[1].split(":")[0]
        self.assertTrue(saved.startswith("projects/.tool-output/") and saved.endswith("mcp_developer_run.txt"))
        with open(os.path.join(self.files, saved)) as f:
            self.assertEqual(f.read(), text)
        self.assertIn("line 20000", open(os.path.join(self.files, saved)).read())

    def test_old_and_extra_files_go(self):
        folder = os.path.join(self.files, agent.TOOL_OUTPUT_DIR)
        os.makedirs(folder)
        for n in range(agent.TOOL_OUTPUT_KEEP + 5):
            p = os.path.join(folder, f"old{n:02}.txt")
            open(p, "w").close()
            os.utime(p, (time.time() - 3600 + n, time.time() - 3600 + n))
        stale = os.path.join(folder, "stale.txt")
        open(stale, "w").close()
        os.utime(stale, (time.time() - 8 * 86400,) * 2)
        agent._clip_tool_result("x" * (agent.TOOL_RESULT_CHAR_LIMIT + 10), "t")
        left = os.listdir(folder)
        self.assertEqual(len(left), agent.TOOL_OUTPUT_KEEP)
        self.assertNotIn("stale.txt", left)
        self.assertNotIn("old00.txt", left)


if __name__ == "__main__":
    unittest.main()
