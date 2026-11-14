"""Jev decides each MCP server on its own (src/jev.py SERVER_THRESHOLD): one
request can need several, and a server it doesn't need sends none of its
tools. Jev itself is faked; nothing leaves the machine.

    python -m unittest tests.test_jev_servers
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.jev as jev  # noqa: E402

TOOLS = [
    ("read_file", "files", "Reads a file."),
    ("add_ping", "files", "Schedules a task."),
    ("mcp_Composio_SEARCH", "mcp:Composio", "Finds connected-app tools."),
    ("mcp_Composio_EXECUTE", "mcp:Composio", "Runs connected-app tools."),
    ("mcp_developer_preview", "mcp:developer", "Publishes a folder as a site."),
    ("mcp_developer_write", "mcp:developer", "Writes a file in a project."),
    ("mcp_iPhone_send_push_notification", "mcp:iPhone", "Sends a push notification."),
    ("mcp_browser_navigate", "mcp:browser", "Opens a page."),
]
SERVERS = [("mcp:Composio", "connected apps"), ("mcp:developer", "code and sites"),
           ("mcp:iPhone", "the user's phone"), ("mcp:browser", "the agent's browser")]
MESSAGES = [{"role": "system", "content": "sys"}]


def route(server_p, tool_p=0.9, request="Check my inbox, put it on a site and send me the link."):
    """Route `request` with Jev answering `server_p` {group: p} for servers
    (a group left out goes unanswered) and `tool_p` for every tool."""
    seen = {}

    def fake_ask(state, questions, key=None, timeout=None):
        seen["questions"] = questions
        answers = {"tag": {"choice": "Files"}, "style": {"choice": "exact"}}
        for qid in questions:
            if qid.startswith("t_"):
                answers[qid] = {"noul": tool_p}
        for gid, p in server_p.items():
            answers[jev._server_qid(gid)] = {"noul": p}
        return answers

    with mock.patch.object(jev, "api_key", lambda: "k"), mock.patch.object(jev, "ask", fake_ask):
        r = jev.route(request, MESSAGES, len(MESSAGES), TOOLS, [], SERVERS)
    return r, seen["questions"]


class TestServers(unittest.TestCase):
    def test_one_request_gets_every_server_it_needs(self):
        r, _ = route({"mcp:Composio": 0.8, "mcp:developer": 0.9, "mcp:iPhone": 0.9, "mcp:browser": 0.1})
        self.assertEqual(set(r.servers), {"mcp:Composio", "mcp:developer", "mcp:iPhone"})
        for name in ("mcp_Composio_SEARCH", "mcp_developer_preview", "mcp_iPhone_send_push_notification"):
            self.assertIn(name, r.tools)
        self.assertNotIn("mcp_browser_navigate", r.tools)
        self.assertEqual(r.servers["mcp:iPhone"], 0.9)

    def test_a_server_it_does_not_need_sends_none_of_its_tools(self):
        r, _ = route({"mcp:Composio": 0.05, "mcp:developer": 0.05, "mcp:iPhone": 0.05, "mcp:browser": 0.05},
                     request="Remind me tomorrow at 5 to call mom.")
        self.assertEqual(r.servers, {})
        self.assertEqual(r.tools, {"read_file", "add_ping"})

    def test_the_threshold(self):
        just = jev.SERVER_THRESHOLD
        r, _ = route({"mcp:Composio": just, "mcp:developer": just - 0.01, "mcp:iPhone": 0, "mcp:browser": 0})
        self.assertIn("mcp:Composio", r.servers)
        self.assertNotIn("mcp:developer", r.servers)

    def test_an_unanswered_server_goes(self):
        # Leaning wide, as for tools: Jev not saying is not Jev saying no.
        r, _ = route({"mcp:Composio": 0.9, "mcp:developer": 0, "mcp:iPhone": 0})
        self.assertIn("mcp:browser", r.servers)
        self.assertIsNone(r.servers["mcp:browser"])

    def test_tools_inside_a_server_are_still_picked_one_by_one(self):
        r, _ = route({"mcp:Composio": 0.9, "mcp:developer": 0.9, "mcp:iPhone": 0, "mcp:browser": 0}, tool_p=0.1)
        self.assertEqual(r.tools & {"mcp_Composio_SEARCH", "mcp_developer_preview"}, set())

    def test_one_question_per_server_and_no_single_choice(self):
        _, qs = route({})
        for gid, _about in SERVERS:
            self.assertEqual(qs[jev._server_qid(gid)]["type"], "noul")
        self.assertFalse([q for q, v in qs.items() if v["type"] == "choice" and q not in ("tag", "style")])

    def test_no_servers_no_server_questions(self):
        with mock.patch.object(jev, "api_key", lambda: "k"), \
                mock.patch.object(jev, "ask", lambda *a, **k: {"tag": {"choice": "Files"}, "t_read_file": {"noul": 1}}):
            r = jev.route("read my notes", MESSAGES, 1, TOOLS[:2], [], ())
        self.assertEqual(r.servers, {})
        self.assertIn("read_file", r.tools)


if __name__ == "__main__":
    unittest.main()
