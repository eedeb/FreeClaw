"""The Developer workspace always on (its switch is GitHub repos only), code as
the way round what no tool does, toolbox usage lines, and the plan tool."""
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.agent as agent  # noqa: E402
import src.mcp_client as mc  # noqa: E402


def developer():
    return next(dict(s) for s in mc.BUILTIN_SERVERS if s["name"] == mc.DEVELOPER_SERVER)


class TestDeveloperSwitch(unittest.TestCase):
    def apply(self, prefs, user="alice"):
        with mock.patch.object(mc, "read_prefs", lambda u: dict(prefs.get(u, {}))):
            return next(s for s in mc._apply_user_prefs([developer()], user)
                        if s["name"] == mc.DEVELOPER_SERVER)

    def test_switched_off_still_on_without_git(self):
        s = self.apply({"alice": {"developer": False}})
        self.assertTrue(s["enabled"])
        self.assertFalse(s["repos"])
        self.assertTrue(set(mc.REPO_TOOLS) <= set(s["exclude_tools"]))

    def test_switched_on_has_git(self):
        s = self.apply({"alice": {"developer": True}})
        self.assertTrue(s["enabled"] and s["repos"])
        self.assertFalse(set(mc.REPO_TOOLS) & set(s["exclude_tools"]))

    def test_no_choice_is_the_default(self):
        s = self.apply({})
        self.assertTrue(s["enabled"])
        self.assertEqual(s["repos"], bool(developer().get("default_enabled")))

    def test_other_servers_keep_their_switch(self):
        with mock.patch.object(mc, "read_prefs", lambda u: {"browser": False}):
            out = mc._apply_user_prefs([{"name": "browser", "enabled": True}], "alice")
        self.assertFalse(out[0]["enabled"])

    def test_sub_agent_follows_its_main_agent(self):
        with mock.patch.object(mc, "_workspace_owner", lambda u: "alice"), \
                mock.patch.object(mc, "read_prefs", lambda u: {"developer": False} if u == "alice" else {}):
            self.assertFalse(mc.repos_on("alice-subagent-1"))

    def test_install_wide_view_untouched(self):
        self.assertEqual(mc._apply_user_prefs([developer()], None)[0], developer())


class TestInstructions(unittest.TestCase):
    def test_code_as_the_way_out_always(self):
        for repos in (False, True):
            text = agent._instructions(coding=True, repos=repos)
            self.assertIn("Code is also how you solve what no tool does", text)
            self.assertIn("Usage:", text)

    def test_git_only_with_repos(self):
        self.assertNotIn("git_clone", agent._instructions(coding=True, repos=False))
        self.assertIn("git_clone", agent._instructions(coding=True, repos=True))
        self.assertNotIn("git_clone", agent._instructions(coding=False, repos=True))


class TestToolbox(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="fc-box-")
        os.makedirs(os.path.join(self.ws, "toolbox"))
        self.addCleanup(shutil.rmtree, self.ws, True)

    def write(self, name, text):
        with open(os.path.join(self.ws, "toolbox", name), "w") as f:
            f.write(text)

    def test_usage_shown(self):
        self.write("price_watch.py", '"""Checks a page\'s price and says if it dropped.\n\n'
                                     'Usage: python price_watch.py <url> <max price>\n"""\n')
        line = agent._toolbox_line(self.ws)
        self.assertIn("price_watch.py (Checks a page's price and says if it dropped.; "
                      "usage: python price_watch.py <url> <max price>)", line)
        self.assertIn("path projects/toolbox", line)

    def test_without_usage(self):
        self.write("a.sh", "# Says hi\necho hi\n")
        self.assertIn("a.sh (Says hi)", agent._toolbox_line(self.ws))

    def test_empty_says_to_add_usage(self):
        shutil.rmtree(os.path.join(self.ws, "toolbox"))
        self.assertIn("Usage: line", agent._toolbox_line(self.ws))


class TestPlan(unittest.TestCase):
    def test_kept_and_shown(self):
        sess = types.SimpleNamespace(turn_plan=[])
        with mock.patch.object(agent, "_sess", lambda: sess):
            out = agent._update_plan(sess, [{"step": "read the data", "status": "done"},
                                            {"step": "chart it", "status": "doing"},
                                            {"step": "email it", "status": "maybe"}])
            self.assertEqual(out, "Plan kept: 1 of 3 done.")
            self.assertEqual(agent._plan_block(),
                             "\nYour plan for this task: [x] read the data; [>] chart it; [ ] email it")

    def test_bad_input_changes_nothing(self):
        sess = types.SimpleNamespace(turn_plan=[{"step": "x", "status": "todo"}])
        self.assertTrue(agent._update_plan(sess, "nope").startswith("Error"))
        self.assertTrue(agent._update_plan(sess, [{"step": " "}]).startswith("Error"))
        self.assertEqual(sess.turn_plan, [{"step": "x", "status": "todo"}])

    def test_capped(self):
        sess = types.SimpleNamespace(turn_plan=[])
        agent._update_plan(sess, [{"step": "s" * 999, "status": "todo"}] * 50)
        self.assertEqual(len(sess.turn_plan), agent.PLAN_MAX_STEPS)
        self.assertEqual(len(sess.turn_plan[0]["step"]), agent.PLAN_STEP_CHARS)

    def test_new_turn_starts_without_one(self):
        import src.session as sessions
        s = sessions.Session(name="alice", static_dir="/tmp/")
        s.turn_plan = [{"step": "x", "status": "todo"}]
        s.reset_tool_run()
        self.assertEqual(s.turn_plan, [])

    def test_always_sent(self):
        self.assertIn(agent.PLAN_TOOL_NAME, agent._JEV_ALWAYS)
        self.assertIn(agent.PLAN_TOOL_NAME, [t["function"]["name"] for t in agent.build_context_tools()])


class TestRunApproval(unittest.TestCase):
    def test_runs_ask_by_default(self):
        import src.dev_server as dev_server
        self.assertTrue(dev_server.runs_need_approval())

    def test_gate_consults_it_and_never_for_push_or_bash(self):
        import inspect
        src_text = inspect.getsource(agent.agent_stream)
        i = src_text.index("dev_server.runs_need_approval()")
        gate = src_text[i - 300:i]
        self.assertIn("command_name != 'run_bash_command'", gate)
        self.assertIn('where != "GitHub"', gate)


if __name__ == "__main__":
    unittest.main()
