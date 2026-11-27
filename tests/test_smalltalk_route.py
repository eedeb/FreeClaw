"""A tiny message ("Thx") is routed without a Jev call, and the last
exchange goes with its tool calls and results, so a thanks after a tool
reply doesn't send the agent searching its history for what the tool said.

    python tests/test_smalltalk_route.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import src.agent as agent  # noqa: E402
import src.jev as jev  # noqa: E402

MESSAGES = [
    {"role": "system", "content": "system"},
    {"role": "user", "content": "Turn off the TV"},
    {"role": "assistant", "content": "The TV is off."},
    {"role": "user", "content": "Rickroll the tv"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "tv_youtube", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": '{"opened": "dQw4w9WgXcQ", "now_playing": null}'},
    {"role": "assistant", "content": "Rickrolled: the video is queued on the TV."},
]


class SmalltalkRoute(unittest.TestCase):
    def test_thanks_is_local(self):
        self.assertTrue(jev.is_tiny("Thx"))

    def test_last_reply_goes_raw(self):
        route = jev._local_route(MESSAGES, len(MESSAGES))
        self.assertEqual(route.messages, [(3, "text"), (6, "raw")])

    def test_the_tool_result_is_sent(self):
        route = jev._local_route(MESSAGES, len(MESSAGES))
        picked = dict(agent._jev_picked(MESSAGES, len(MESSAGES), route))
        self.assertEqual(picked.get(5), "full")       # the tool result
        self.assertEqual(picked.get(4), "full")       # the call that made it
        self.assertEqual(picked.get(3), "text")       # the request, as text
        self.assertNotIn(1, picked)                   # older exchanges stay out


if __name__ == "__main__":
    unittest.main(verbosity=1)
