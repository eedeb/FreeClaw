"""Routed turns can recover both whole and partially selected groups."""
import types
import unittest
from unittest import mock

import src.agent as agent
import src.advisor as advisor


def tool(name, description="Does something."):
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": {}}}}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.a = tool("mcp_mail_send_email", "Send from the agent's address; not the person's inbox.")
        self.b = tool("mcp_mail_other", "Another mail capability.")
        self.c = tool("mcp_Other_read", "Read elsewhere.")
        self.groups = [("mcp:mail", "mail", [self.a, self.b]),
                       ("mcp:Other", "other", [self.c])]
        self.s = types.SimpleNamespace(depth=0, turn_route_notes="", turn_route_groups=self.groups,
                                       turn_route=None)
        self.tools = agent._jev_turn_tools(self.groups, {agent._tool_name(self.a)}, self.s)
        self.s.turn_prefix = {"tools": self.tools}

    def test_partial_and_whole_groups_are_loadable(self):
        self.assertEqual(set(agent._jev_available(self.s)), {"mcp:mail", "mcp:Other"})
        self.assertEqual(self.s.turn_trimmed["mcp:mail"], [self.b])
        load = next(t for t in self.tools if agent._tool_name(t) == "load_tools")
        self.assertEqual(set(load["function"]["parameters"]["properties"]["groups"]["items"]["enum"]),
                         {"mcp:mail", "mcp:Other"})
        self.assertIn("mcp:mail (other)", agent._jev_notes(self.s))
        self.assertTrue(any(advisor._tool_fn(t).get("name") == "mcp_mail_other" and can_load
                            for t, can_load in advisor._agent_tools(self.s)))

    def test_load_partial_once_then_whole_and_update_route(self):
        self.s.turn_route = {"loaded": [], "withheld": [{"group": "mcp:Other"}],
                             "trimmed": [{"group": "mcp:mail"}], "tools": []}
        with mock.patch.object(agent, "_sess", return_value=self.s):
            self.assertIn("Loaded: mcp:mail", agent._load_tools({"groups": ["mail", "mcp:mail"]}))
            names = [agent._tool_name(t) for t in self.s.turn_prefix["tools"]]
            self.assertEqual(names.count("mcp_mail_send_email"), 1)
            self.assertEqual(names.count("mcp_mail_other"), 1)
            self.assertEqual(self.s.turn_route["trimmed"], [])
            self.assertEqual(self.s.turn_route["loaded"], ["mcp:mail"])
            self.assertNotIn("mcp:mail", self.s.turn_trimmed)
            self.assertIn("mcp:Other", self.s.turn_withheld)
            self.assertIn("Nothing loaded", agent._load_tools({"groups": ["mail"]}))
            self.assertIn("Loaded: mcp:Other", agent._load_tools({"groups": ["Other"]}))
            names = [agent._tool_name(t) for t in self.s.turn_prefix["tools"]]
            self.assertNotIn("load_tools", names)
            self.assertEqual(names.count("mcp_Other_read"), 1)
            self.assertEqual(self.s.turn_route["withheld"], [])

    def test_depth_limit_does_not_offer_disallowed_tool(self):
        barred = tool(agent.SUBAGENT_TOOL_NAME)
        self.s.depth = agent.MAX_SUBAGENT_DEPTH
        result = agent._jev_turn_tools([("utility", "utility", [barred])], set(), self.s)
        self.assertNotIn("utility", agent._jev_available(self.s))
        self.assertNotIn("load_tools", [agent._tool_name(t) for t in result])

    def test_server_summary_includes_bounded_capability(self):
        summary = agent._jev_server_about({"name": "mail"}, [self.a], None)
        self.assertIn("send_email: Send from the agent's address", summary)
        many = [tool("mcp_mail_" + str(n), "x" * 300) for n in range(8)]
        summary = agent._jev_server_about({"name": "mail"}, many, None)
        self.assertLess(len(summary), 600)
        self.assertNotIn("3: ", summary)


if __name__ == "__main__":
    unittest.main()
