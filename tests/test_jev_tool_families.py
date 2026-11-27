"""A server's line to Jev names every kind of tool it has, not just the
first ten: a home server's 64 tools began with ten TV controls, and Jev sent
"3-D print me a smiley face" to the browser (2026-11-26).

    python tests/test_jev_tool_families.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import src.agent as agent  # noqa: E402

HOME = (["smarthome_tv_" + v for v in "status power press_keys launch_app list_apps volume type_text "
                                     "open_url youtube screenshot".split()]
        + ["smarthome_plug_set", "smarthome_alexa_announce", "smarthome_printer3d_status",
           "smarthome_printer3d_print_toy", "smarthome_tree_lights_off", "apps_mail_send",
           "apps_notes_create", "printing_print_file"])


def tool(name):
    return {"type": "function", "function": {"name": "mcp_desktop_" + name}}


class ToolFamilies(unittest.TestCase):
    def test_a_short_list_goes_whole(self):
        self.assertEqual(agent._tool_families(["send_email", "list_inbox"]), "send_email, list_inbox")

    def test_a_long_list_keeps_every_kind(self):
        line = agent._tool_families(HOME)
        for kind in ("tv", "plug", "alexa", "printer3d", "tree", "mail", "notes", "printing_print"):
            self.assertIn(kind, line)
        self.assertNotIn("press_keys", line)          # folded, not listed one by one

    def test_the_server_line_reaches_the_printer(self):
        line = agent._jev_server_about({"name": "desktop", "description": "Home Pi"},
                                       [tool(n) for n in HOME], None)
        self.assertIn("printer3d", line)
        self.assertNotIn("…", line)


if __name__ == "__main__":
    unittest.main(verbosity=1)
