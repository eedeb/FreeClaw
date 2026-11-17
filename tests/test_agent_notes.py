"""Messages FreeClaw writes itself (a sub-agent's report, the advisor's
check-up) carry auto_kind so the chat shows them as notes that open; and
edit_file's miss says what to do next.

    python -m unittest tests.test_agent_notes
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestNotes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import src.agent as agent
            import src.session as sessions
        except Exception as e:                               # pragma: no cover
            raise unittest.SkipTest(f"Couldn't import FreeClaw: {e}")
        cls.agent, cls.sessions = agent, sessions

    def run_turn(self, **kw):
        agent = self.agent
        from types import SimpleNamespace as NS

        def fake_create(**k):
            return iter([NS(choices=[NS(delta=NS(content="ok", tool_calls=None, reasoning=None),
                                        finish_reason="stop")], usage=None)]), "test"
        sess = self.sessions.Session(name="notestest")
        sess.messages.append({"role": "system", "content": "s"})
        with self.sessions.use(sess), \
                mock.patch.object(agent, "_create_completion", fake_create), \
                mock.patch.object(agent, "_jev_route", lambda *a, **k: None), \
                mock.patch.object(agent, "_follow_through", lambda *a, **k: None), \
                mock.patch.object(agent, "_refresh_volatile", lambda *a, **k: None), \
                mock.patch.object(agent, "_refresh_stale_context", lambda *a, **k: None), \
                mock.patch.object(agent.Classy, "classify", lambda *a, **k: (["Followup"], [1.0])):
            list(agent.agent_stream(user_input="SUB-AGENT RESULT from \"x\", which finished.", **kw))
        return [m for m in sess.messages if m.get("role") == "user"]

    def test_auto_kind_marks_the_message_but_not_auto(self):
        msg = self.run_turn(ping=True, auto_kind="subagent")[-1]
        self.assertEqual(msg["auto_kind"], "subagent")
        self.assertNotIn("auto", msg)                      # still this turn's request

    def test_a_typed_message_has_none(self):
        self.assertNotIn("auto_kind", self.run_turn()[-1])

    def test_edit_miss(self):
        miss = self.agent._edit_miss
        text = "## Work\n- Elliot runs Talon, a hosted agent.\n"
        out = miss("context.md", text, "- Elliot runs Talon, a SaaS.")
        self.assertIn("nothing was changed", out)
        self.assertIn("a hosted agent.", out)
        self.assertIn("search_context", miss("context.md", text, "Gone sentence that was removed."))
        self.assertIn("read_file", miss("notes.txt", text, "Gone sentence that was removed."))


if __name__ == "__main__":
    unittest.main()
