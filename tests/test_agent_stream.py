"""The agent loop's handling of what a provider streams (src/agent.py):
tool calls numbered badly, calls cut short, and results too long to keep.

Run from the repo root:  python -m unittest discover -s tests
"""

import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def chunk(content=None, calls=None, finish=None):
    delta = types.SimpleNamespace(content=content, tool_calls=calls, reasoning=None,
                                  reasoning_content=None)
    return types.SimpleNamespace(usage=None, reasoning_items=None,
                                 choices=[types.SimpleNamespace(delta=delta, finish_reason=finish)])


def call(index, call_id, name, arguments):
    return types.SimpleNamespace(index=index, id=call_id,
                                 function=types.SimpleNamespace(name=name, arguments=arguments))


class Broken(Exception):
    pass


def breaking(chunks):
    yield from chunks
    raise Broken("connection reset")


class TestStream(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import src.agent as agent
            import src.session as sessions
        except Exception as e:                               # pragma: no cover
            raise unittest.SkipTest(f"Couldn't import FreeClaw: {e}")
        cls.agent, cls.sessions = agent, sessions

    def run_turn(self, streams):
        """One turn against `streams` (one per request); (events, tool calls run)."""
        agent = self.agent
        streams = list(streams)
        ran = []

        def fake_create(**kw):
            return streams.pop(0), "test"

        def fake_run(name, args, **kw):
            ran.append((name, args))
            return "ok"

        sess = self.sessions.Session(name="streamtest")
        sess.messages.append({"role": "system", "content": "s"})
        with self.sessions.use(sess), \
                mock.patch.object(agent, "_create_completion", fake_create), \
                mock.patch.object(agent, "_run_tool", fake_run), \
                mock.patch.object(agent, "_jev_route", lambda *a, **k: None), \
                mock.patch.object(agent, "_follow_through", lambda *a, **k: None), \
                mock.patch.object(agent, "_refresh_volatile", lambda *a, **k: None), \
                mock.patch.object(agent, "_refresh_stale_context", lambda *a, **k: None), \
                mock.patch.object(agent, "_note_provider_failure", lambda *a, **k: None), \
                mock.patch.object(agent.Classy, "classify", lambda *a, **k: (["Followup"], [1.0])):
            events = list(agent.agent_stream(user_input="do it"))
        return events, ran, sess.messages

    def test_calls_all_numbered_zero_stay_separate(self):
        first = [chunk(calls=[call(0, "a", "read_file", '{"filename": "one.txt"}')]),
                 chunk(calls=[call(0, "b", "read_file", '{"filename": "two.txt"}')], finish="tool_calls")]
        _, ran, _ = self.run_turn([first, [chunk(content="done", finish="stop")]])
        self.assertEqual(ran, [("read_file", {"filename": "one.txt"}),
                               ("read_file", {"filename": "two.txt"})])

    def test_a_call_streamed_in_pieces_is_one_call(self):
        first = [chunk(calls=[call(0, "a", "create_file", '{"filename": "a.txt", ')]),
                 chunk(calls=[call(0, None, None, '"contents": "hi"}')], finish="tool_calls")]
        _, ran, _ = self.run_turn([first, [chunk(content="done", finish="stop")]])
        self.assertEqual(ran, [("create_file", {"filename": "a.txt", "contents": "hi"})])

    def test_a_call_with_no_arguments_runs(self):
        first = [chunk(calls=[call(0, "a", "get_time", "")], finish="tool_calls")]
        _, ran, _ = self.run_turn([first, [chunk(content="done", finish="stop")]])
        self.assertEqual(ran, [("get_time", {})])

    def test_a_call_cut_off_by_a_broken_stream_is_not_run(self):
        first = breaking([chunk(calls=[call(0, "a", "create_file", '{"filename": "a.txt", "contents": "half of the')])])
        events, ran, _ = self.run_turn([first, [chunk(content="sorry, again", finish="stop")]])
        self.assertEqual(ran, [])
        self.assertIn("retry", [e["type"] for e in events])

    def test_a_call_cut_off_by_the_length_limit_is_not_run(self):
        first = [chunk(calls=[call(0, "a", "create_file", '{"filename": "a.txt", "contents": "half of the')],
                       finish="length")]
        _, ran, messages = self.run_turn([first, [chunk(content="ok", finish="stop")]])
        self.assertEqual(ran, [])
        tool = next(m for m in messages if m.get("role") == "tool")
        self.assertIn("NOT run", tool["content"])

    def test_a_huge_result_is_clipped_and_keeps_its_images(self):
        agent = self.agent
        big = agent.mcp_client.ToolText("a" * (agent.TOOL_RESULT_CHAR_LIMIT * 3) + "END", ["data:x"])
        clipped = agent._clip_tool_result(big)
        self.assertLess(len(clipped), agent.TOOL_RESULT_CHAR_LIMIT + 400)
        self.assertTrue(clipped.endswith("END"))
        self.assertEqual(clipped.images, ("data:x",))
        self.assertEqual(agent._clip_tool_result("short"), "short")


if __name__ == "__main__":
    unittest.main()
