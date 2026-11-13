"""What a request sends so the provider's cache keeps matching (src/agent.py,
src/responses_api.py): the whole tool list with the turn's own allowed in
tool_choice, and a new message re-sending the last turn's context when that's
cheaper than Jev's fresh pick.

Run from the repo root:  python -m unittest tests.test_cache_layout
"""

import os
import sys
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def tool(name, desc="d"):
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": {}}}}


def chunk(content=None, finish="stop"):
    delta = types.SimpleNamespace(content=content, tool_calls=None, reasoning=None,
                                  reasoning_content=None)
    return types.SimpleNamespace(usage=None, reasoning_items=None,
                                 choices=[types.SimpleNamespace(delta=delta, finish_reason=finish)])


class Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import src.agent as agent
            import src.jev as jev
            import src.responses_api as responses_api
            import src.session as sessions
        except Exception as e:                               # pragma: no cover
            raise unittest.SkipTest(f"Couldn't import FreeClaw: {e}")
        cls.agent, cls.jev, cls.rapi, cls.sessions = agent, jev, responses_api, sessions


class TestAllowedTools(Base):

    def build(self, tools, universe):
        kw = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "tools": tools}
        if universe is not None:
            kw[self.rapi.TOOL_UNIVERSE_KEY] = universe
        return self.rapi.build_request(kw)

    def test_sends_every_tool_sorted_and_allows_the_turns_own(self):
        req = self.build([tool("b"), tool("a")], [tool("c"), tool("a"), tool("b")])
        self.assertEqual([t["name"] for t in req["tools"]], ["a", "b", "c"])
        self.assertEqual(req["tool_choice"], {"type": "allowed_tools", "mode": "auto", "tools": [
            {"type": "function", "name": "b"}, {"type": "function", "name": "a"}]})

    def test_the_tool_list_is_the_same_whatever_the_turn_allows(self):
        universe = [tool(n) for n in "zyxabc"]
        one = self.build([tool("a")], universe)
        two = self.build([tool("x"), tool("y")], universe)
        self.assertEqual(one["tools"], two["tools"])
        self.assertNotEqual(one["tool_choice"], two["tool_choice"])

    def test_the_list_defines_each_tool_not_the_turn(self):
        # load_tools' turn copy names this turn's groups; the list's copy doesn't.
        req = self.build([tool("a", "turn's own words")], [tool("a", "the list's")])
        self.assertEqual(req["tools"][0]["description"], "the list's")

    def test_no_tools_this_turn_keeps_the_list_and_allows_none(self):
        req = self.build(None, [tool("a"), tool("b")])
        self.assertEqual(len(req["tools"]), 2)
        self.assertEqual(req["tool_choice"], "none")

    def test_a_tool_the_list_lacks_falls_back_to_the_turns_tools(self):
        req = self.build([tool("a"), tool("extra")], [tool("a"), tool("b")])
        self.assertEqual([t["name"] for t in req["tools"]], ["a", "extra"])
        self.assertNotIn("tool_choice", req)

    def test_too_long_a_list_falls_back(self):
        many = [tool(f"t{i}") for i in range(self.rapi.MAX_UNIVERSE_TOOLS + 1)]
        req = self.build([tool("t1")], many)
        self.assertEqual([t["name"] for t in req["tools"]], ["t1"])

    def test_without_a_list_nothing_changes(self):
        req = self.build([tool("a")], None)
        self.assertEqual([t["name"] for t in req["tools"]], ["a"])
        self.assertNotIn("tool_choice", req)

    def test_load_tools_in_the_list_reads_the_same_every_time(self):
        one = self.agent._load_tools_tool(None)
        self.assertNotIn("enum", one["function"]["parameters"]["properties"]["groups"]["items"])
        self.assertEqual(one, self.agent._load_tools_tool(None))
        turn = self.agent._load_tools_tool({"mcp:browser": ("b", [])})
        self.assertEqual(turn["function"]["parameters"]["properties"]["groups"]["items"]["enum"],
                         ["mcp:browser"])


class TestProviders(Base):
    """The list only goes to a Responses provider, and not again to one that
    refused it."""

    def run_chain(self, providers, fail_with_list=()):
        agent = self.agent
        sent = []

        def fake_send(client, api, call_kwargs):
            sent.append((api, dict(call_kwargs)))
            if self.rapi.TOOL_UNIVERSE_KEY in call_kwargs and api in fail_with_list:
                raise Exception("400 bad request: unknown parameter tool_choice")
            return iter([]), None

        with mock.patch.object(agent, "_active_providers", lambda: providers), \
                mock.patch.object(agent, "_send", fake_send), \
                mock.patch.object(agent, "_client_for", lambda *a: None), \
                mock.patch.object(agent, "_classify_error", lambda e: "bad_request"), \
                mock.patch.object(agent, "_is_oversized_error", lambda e: False), \
                mock.patch.object(agent, "_note_provider_success", lambda *a: None), \
                mock.patch.object(agent, "_cooldown_reason", lambda *a: None), \
                mock.patch.object(agent, "_unsupported_extras", set()):
            agent._create_completion(model="m", messages=[{"role": "user", "content": "x"}],
                                     tools=[tool("a")], stream=True,
                                     **{self.rapi.TOOL_UNIVERSE_KEY: [tool("a"), tool("b")]})
        return sent

    def test_responses_provider_gets_the_list(self):
        sent = self.run_chain([("p", "u", "k", None, None, "responses")])
        self.assertIn(self.rapi.TOOL_UNIVERSE_KEY, sent[0][1])

    def test_chat_provider_never_sees_it(self):
        sent = self.run_chain([("p", "u", "k", None, None, "chat")])
        self.assertNotIn(self.rapi.TOOL_UNIVERSE_KEY, sent[0][1])

    def test_a_refusal_is_retried_without_it(self):
        sent = self.run_chain([("p", "u", "k", None, None, "responses")], fail_with_list=("responses",))
        self.assertEqual(len(sent), 2)
        self.assertNotIn(self.rapi.TOOL_UNIVERSE_KEY, sent[1][1])


class TestResendOrRepick(Base):
    """_append_or_trim, on a conversation whose last routed turn has ended."""

    def conversation(self, tool_output=200, turns=3, answer=20):
        sess = self.sessions.Session(name="cachetest")
        m = sess.messages
        m.append({"role": "system", "content": "s" * 400})
        for n in range(turns):
            m.append({"role": "user", "content": f"question {n} " * 10})
            m.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{n}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
            m.append({"role": "tool", "tool_call_id": f"c{n}", "content": "x" * tool_output})
            m.append({"role": "assistant", "content": f"answer {n} " * answer})
        return sess

    def end_turn(self, sess, start, picked, requests=6):
        sess.turn_prefix = {"start": start, "tools": [], "lean_start": start, "picked": picked}
        sess.turn_usage = {"requests": requests}
        sess.clear_turn_prefix()

    def decide(self, sess, picked):
        sess.messages.append({"role": "user", "content": "and now this"})
        full_start = len(sess.messages) - 1
        with self.sessions.use(sess):
            return self.agent._append_or_trim(sess, sess.messages, full_start, picked)

    def test_cheap_to_resend_resends(self):
        # Jev drops the short first question; a re-pick would write the long
        # answers after it again, while re-sending reads it for almost nothing.
        sess = self.conversation(answer=400)
        sess.messages[12]["content"] = "Done."          # the last turn's own reply is short
        self.end_turn(sess, 9, [(1, "text"), (4, "text")])
        start, picked, why = self.decide(sess, [(4, "text")])
        self.assertTrue(why["resend"], why)
        self.assertEqual((start, picked), (9, [(1, "text"), (4, "text")]))

    def test_a_long_tool_result_over_many_steps_is_repicked(self):
        sess = self.conversation(tool_output=40_000)
        self.end_turn(sess, 9, [(1, "text")], requests=30)
        start, picked, why = self.decide(sess, [(1, "text")])
        self.assertFalse(why["resend"], why)
        self.assertEqual(start, len(sess.messages) - 1)

    def test_writes_dearer_than_reads_lean_toward_resending(self):
        # Last turn sent everything verbatim; Jev now wants only the answers,
        # so a re-pick differs right after the system message and writes all
        # of it again. The model whose writes cost more relative to reads
        # keeps the old context where the other re-picks.
        sess = self.sessions.Session(name="ratio")
        m = sess.messages
        m.append({"role": "system", "content": "s" * 400})
        for n in range(3):
            m.append({"role": "user", "content": f"q{n} " * 30})
            m.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{n}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
            m.append({"role": "tool", "tool_call_id": f"c{n}", "content": "x" * 2000})
            m.append({"role": "assistant", "content": "a" * 2000})
        self.end_turn(sess, 1, [], requests=10)
        m.append({"role": "user", "content": "and now this"})
        fs = len(m) - 1
        answers = [(4, "text"), (8, "text"), (12, "text")]
        with self.sessions.use(sess):
            with mock.patch.object(self.agent, "_write_read_ratio", lambda s: 3):
                cheap = self.agent._append_or_trim(sess, m, fs, answers)[2]
            with mock.patch.object(self.agent, "_write_read_ratio", lambda s: 25):
                dear = self.agent._append_or_trim(sess, m, fs, answers)[2]
        self.assertFalse(cheap["resend"], cheap)
        self.assertTrue(dear["resend"], dear)

    def test_dropping_only_the_end_is_free_so_it_repicks(self):
        # Cutting the last turn's tool output leaves everything before it
        # cached: nothing to rewrite, so the smaller context wins.
        sess = self.conversation(tool_output=4000)
        self.end_turn(sess, 9, [(1, "text")], requests=10)
        with mock.patch.object(self.agent, "_write_read_ratio", lambda s: 25):
            _start, _picked, why = self.decide(sess, [(1, "text")])
        self.assertFalse(why["resend"], why)

    def test_jev_wanting_something_the_old_context_lacks_repicks(self):
        sess = self.conversation()
        self.end_turn(sess, 9, [(4, "text")])
        _start, picked, why = self.decide(sess, [(1, "text")])
        self.assertIsNone(why)
        self.assertEqual(picked, [(1, "text")])

    def test_jev_wanting_the_raw_detail_of_a_text_pick_repicks(self):
        sess = self.conversation()
        self.end_turn(sess, 9, [(4, "text")])
        self.assertIsNone(self.decide(sess, [(4, "full")])[2])

    def test_a_cold_cache_repicks(self):
        sess = self.conversation()
        self.end_turn(sess, 9, [(1, "text")])
        sess.last_layout["at"] = time.monotonic() - self.agent.CACHE_WARM_SECONDS - 1
        self.assertIsNone(self.decide(sess, [(1, "text")])[2])

    def test_past_the_size_cap_repicks(self):
        sess = self.conversation()
        self.end_turn(sess, 9, [(1, "text")])
        with mock.patch.object(self.agent, "_append_max_tokens", lambda s: 10):
            self.assertIsNone(self.decide(sess, [(1, "text")])[2])

    def test_an_edited_history_repicks(self):
        sess = self.conversation()
        self.end_turn(sess, 9, [(1, "text")])
        del sess.messages[2]
        self.assertIsNone(self.decide(sess, [(1, "text")])[2])

    def test_a_reset_forgets_the_layout(self):
        sess = self.conversation()
        self.end_turn(sess, 9, [(1, "text")])
        self.assertIsNotNone(sess.last_layout)
        with self.sessions.use(sess), mock.patch.object(self.agent, "_archive_conversation", lambda s: None), \
                mock.patch.object(self.agent, "_context_path", lambda: os.devnull):
            try:
                self.agent.reset()
            except Exception:                               # pragma: no cover — the field is what's checked
                pass
        self.assertIsNone(sess.last_layout)

    def test_off_switch(self):
        sess = self.conversation()
        self.end_turn(sess, 9, [(1, "text")])
        with mock.patch.object(self.agent, "APPEND_TURNS", False):
            self.assertIsNone(self.decide(sess, [(1, "text")])[2])

    def test_unrouted_turn_leaves_nothing_to_resend(self):
        sess = self.conversation()
        self.end_turn(sess, 9, None)
        self.assertIsNone(sess.last_layout)

    def test_expected_requests_follow_the_conversation(self):
        sess = self.sessions.Session(name="k")
        self.assertEqual(self.agent._expected_requests(sess), self.agent.EXPECTED_REQUESTS)
        sess.recent_requests = [2, 4]
        self.assertEqual(self.agent._expected_requests(sess), 3)


class TestRoutedTurns(Base):
    """Routed messages through agent_stream: what each request carried."""

    def test_resent_context_and_the_full_list_on_every_request(self):
        agent, jev = self.agent, self.jev
        sent = []

        def fake_create(**kw):
            sent.append(kw)
            return iter([chunk("done " * 5)]), "test"

        # Turn 2 is sent the first message; turn 3 is sent only the second,
        # which a re-pick would put straight after the system message.
        routes = [jev.Route("Followup", "balanced", {"read_file"}, picks, [], 1, {})
                  for picks in ([], [(1, "text")], [(3, "text")])]
        sess = self.sessions.Session(name="routetest")
        sess.messages.append({"role": "system", "content": "s"})
        with self.sessions.use(sess), \
                mock.patch.object(agent, "_create_completion", fake_create), \
                mock.patch.object(agent, "_jev_route", lambda *a, **k: (routes.pop(0), [], [])), \
                mock.patch.object(agent, "_follow_through", lambda *a, **k: None), \
                mock.patch.object(agent, "_refresh_volatile", lambda *a, **k: None), \
                mock.patch.object(agent, "_refresh_stale_context", lambda *a, **k: None), \
                mock.patch.object(agent, "_tool_universe", lambda s: [tool("read_file"), tool("x")]), \
                mock.patch.object(agent, "_jev_route_details", lambda *a, **k: {}), \
                mock.patch.object(agent, "_JEV_KEEP_LAST_EXCHANGE", False):
            list(agent.agent_stream(user_input="first"))
            list(agent.agent_stream(user_input="second, with a lot to say " * 60))
            events = list(agent.agent_stream(user_input="third"))
            reuse = next(e["route"].get("reuse") for e in events if e.get("type") == "route")
        key = self.rapi.TOOL_UNIVERSE_KEY
        self.assertEqual([len(kw[key]) for kw in sent], [2, 2, 2])
        self.assertTrue(reuse and reuse["resend"], reuse)
        # The third request starts with exactly what the second sent.
        two, three = sent[1]["messages"], sent[2]["messages"]
        self.assertEqual([m.get("content") for m in three[:len(two)]],
                         [m.get("content") for m in two])


if __name__ == "__main__":
    unittest.main()
