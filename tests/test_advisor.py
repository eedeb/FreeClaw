"""The advisor (src/advisor.py): when it's called in, what it sees, how its
advice reaches the agent, and what keeps it cheap.

Run from the repo root:  python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def chunk(content=None, calls=None, finish=None, usage=None):
    delta = types.SimpleNamespace(content=content, tool_calls=calls, reasoning=None,
                                  reasoning_content=None)
    return types.SimpleNamespace(usage=usage, reasoning_items=None,
                                 choices=[types.SimpleNamespace(delta=delta, finish_reason=finish)])


def call(index, call_id, name, arguments):
    return types.SimpleNamespace(index=index, id=call_id,
                                 function=types.SimpleNamespace(name=name, arguments=arguments))


def tool_step(i, name=None, args=None):
    # Alternating tools, so FreeClaw's repeat guard has nothing to hold back.
    name = name or ("read_file" if i % 2 else "list_files")
    args = args or json.dumps({"filename": f"x{i}.txt"})
    return [chunk(calls=[call(0, f"c{i}", name, args)], finish="tool_calls")]


def answer(text="done"):
    return [chunk(content=text, finish="stop")]


class Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import src.advisor as advisor
            import src.agent as agent
            import src.session as sessions
        except Exception as e:                               # pragma: no cover
            raise unittest.SkipTest(f"Couldn't import FreeClaw: {e}")
        cls.agent, cls.sessions, cls.advisor = agent, sessions, advisor

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"FC_ADVISOR": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_turn(self, streams, results=None, follow=None, verdicts=(), allowed=True):
        """One turn against `streams` (one per agent request). `results` gives
        each tool call's result in order; `verdicts` what the advisor answers.
        Returns (events, requests the agent sent, advisor calls, messages)."""
        agent, advisor = self.agent, self.advisor
        streams, results, verdicts = list(streams), list(results or []), list(verdicts)
        sent, asked = [], []

        def fake_create(**kw):
            sent.append(kw)
            return streams.pop(0), "test"

        def fake_run(name, args, **kw):
            return results.pop(0) if results else "ok"

        def fake_call(user, kind, content, **kw):
            asked.append((kind, content))
            v = verdicts.pop(0) if verdicts else {"verdict": "fine", "advice": ""}
            return {**v, "ms": 1, "kind": kind}

        sess = self.sessions.Session(name="advisortest")
        sess.static_dir = tempfile.mkdtemp() + "/files/"
        sess.messages.append({"role": "system", "content": "s"})
        with self.sessions.use(sess), \
                mock.patch.object(agent, "_create_completion", fake_create), \
                mock.patch.object(agent, "_run_tool", fake_run), \
                mock.patch.object(agent, "_jev_route", lambda *a, **k: None), \
                mock.patch.object(agent, "_follow_through",
                                  lambda *a, **k: (follow.pop(0) if follow else None)
                                  if isinstance(follow, list) else follow), \
                mock.patch.object(agent, "_refresh_volatile", lambda *a, **k: None), \
                mock.patch.object(agent, "_refresh_stale_context", lambda *a, **k: None), \
                mock.patch.object(agent, "_note_provider_failure", lambda *a, **k: None), \
                mock.patch.object(advisor, "call", fake_call), \
                mock.patch.object(advisor, "allowed", lambda user: allowed), \
                mock.patch.object(agent.Classy, "classify", lambda *a, **k: (["Followup"], [1.0])):
            events = list(agent.agent_stream(user_input="do it"))
        return events, sent, asked, sess.messages


class TestTriggers(Base):

    def test_a_quiet_turn_never_calls_it(self):
        _, _, asked, _ = self.run_turn([tool_step(1), tool_step(2), answer()])
        self.assertEqual(asked, [])

    def test_third_failure_brings_advice_onto_that_result(self):
        fix = {"verdict": "fix", "advice": "Use list_files to find the right name."}
        events, sent, asked, msgs = self.run_turn(
            [tool_step(1), tool_step(2), tool_step(3), answer()],
            results=["Error: not found", "Error: not found", "Error: not found"],
            verdicts=[fix])
        self.assertEqual([k for k, _ in asked], ["trouble"])
        self.assertIn("3 tool steps failed", asked[0][1])
        third = [m for m in msgs if m.get("role") == "tool"][2]["content"]
        self.assertIn(self.advisor.MID_TURN_PREFIX, third)
        self.assertIn("list_files", third)
        self.assertTrue(any(e.get("type") == "advisor" for e in events))
        # The agent's next request carries it.
        self.assertIn("list_files", json.dumps(sent[-1]["messages"]))

    def test_fine_mid_turn_adds_nothing(self):
        _, _, asked, msgs = self.run_turn(
            [tool_step(1), tool_step(2), tool_step(3), answer()],
            results=["Error: x", "Error: x", "Error: x"])
        self.assertEqual(len(asked), 1)
        self.assertNotIn(self.advisor.MID_TURN_PREFIX, json.dumps(msgs))

    def test_a_refusal_by_the_person_is_not_a_failure(self):
        no = "This was NOT done (Gmail: send email): the user said no."
        _, _, asked, _ = self.run_turn([tool_step(1), tool_step(2), tool_step(3), answer()],
                                       results=[no, no, no])
        self.assertEqual(asked, [])

    def test_reply_check_flag_resumes_the_turn_with_the_note(self):
        fix = {"verdict": "fix", "advice": "Tell them the email was not sent."}
        events, sent, asked, msgs = self.run_turn(
            [answer("Sent it!"), answer("Sorry, it wasn't sent.")],
            follow={"nudge": False, "advisor": True}, verdicts=[fix])
        self.assertEqual([k for k, _ in asked], ["reply"])
        self.assertIn("Sent it!", asked[0][1])
        notes = [m for m in msgs if m.get("auto") == "advisor"]
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0]["content"].startswith(self.advisor.NOTE_PREFIX))
        self.assertEqual(len(sent), 2)
        self.assertTrue(any(e.get("type") == "advisor" and e.get("resume") for e in events))

    def test_only_once_a_turn(self):
        fix = {"verdict": "fix", "advice": "Do it again properly."}
        _, sent, asked, _ = self.run_turn(
            [answer("a"), answer("b")],
            follow={"nudge": False, "advisor": True}, verdicts=[fix, fix])
        self.assertEqual(len(asked), 1)
        self.assertEqual(len(sent), 2)

    def test_reply_after_unseen_trouble_without_jev(self):
        _, _, asked, _ = self.run_turn(
            [tool_step(i) for i in range(1, 26)] + [answer()])
        self.assertEqual([k for k, _ in asked], ["trouble"])

    def test_off_means_nothing(self):
        with mock.patch.dict(os.environ, {"FC_ADVISOR": ""}):
            _, sent, asked, _ = self.run_turn(
                [tool_step(1), tool_step(2), tool_step(3), answer()],
                results=["Error: x"] * 3, follow={"nudge": False, "advisor": True})
        self.assertEqual(asked, [])

    def test_not_allowed_means_nothing(self):
        _, _, asked, _ = self.run_turn(
            [tool_step(1), tool_step(2), tool_step(3), answer()],
            results=["Error: x"] * 3, follow={"nudge": False, "advisor": True}, allowed=False)
        self.assertEqual(asked, [])

    def test_follow_through_nudge_goes_first(self):
        fix = {"verdict": "fix", "advice": "x"}
        _, sent, asked, msgs = self.run_turn(
            [answer("half"), answer("all")],
            follow=[{"nudge": True, "advisor": True}, {"nudge": False, "advisor": False}],
            verdicts=[fix])
        # The nudge resumed the turn first; the advisor wasn't also called on
        # the reply the nudge replaced.
        self.assertTrue(any(m.get("auto") is True for m in msgs))
        self.assertEqual(asked, [])
        self.assertEqual(len(sent), 2)


    def test_a_loop_the_guard_keeps_holding_back(self):
        same = lambda i: tool_step(i, name="add_context", args=json.dumps({"header": "W", "string": f"n{i}"}))
        _, _, asked, _ = self.run_turn([same(i) for i in range(1, 8)] + [answer()])
        self.assertEqual([k for k, _ in asked], ["trouble"])
        self.assertIn("repeating", asked[0][1])

    def test_one_hold_back_alone_is_not_trouble(self):
        same = lambda i: tool_step(i, name="add_context", args=json.dumps({"header": "W", "string": f"n{i}"}))
        _, _, asked, _ = self.run_turn([same(1), same(2), same(3), answer()])
        self.assertEqual(asked, [])


class TestPieces(Base):

    def test_parse(self):
        p = self.advisor.parse
        self.assertEqual(p('{"verdict": "fine", "advice": ""}')["verdict"], "fine")
        self.assertEqual(p('```json\n{"verdict": "fix", "advice": "Cancel the duplicate."}\n```'),
                         {"verdict": "fix", "advice": "Cancel the duplicate."})
        self.assertEqual(p('{"verdict": "fix", "advice": ""}')["verdict"], "fine")
        self.assertEqual(p("Looks fine.")["verdict"], "fine")
        self.assertEqual(p("Cancel the duplicate ping.")["verdict"], "fix")
        self.assertEqual(p("")["verdict"], "fine")

    def test_digest_is_short_whatever_the_turn(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q" * 9000}]
        for i in range(200):
            msgs.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": f"t{i}", "function": {"name": "read_file", "arguments": "a" * 5000}}]})
            msgs.append({"role": "tool", "tool_call_id": f"t{i}", "content": "r" * 5000})
        d = self.advisor.digest(msgs, reply="x" * 9000, trouble="why")
        self.assertLess(len(d), 30000)
        self.assertIn("earlier steps not shown", d)

    def test_failure_signals(self):
        f = self.advisor.failed
        self.assertTrue(f("Error: nope"))
        self.assertTrue(f('{"successful": false}'))
        self.assertTrue(f("'add_context' was NOT run: nothing was saved."))
        self.assertFalse(f("Saved under 'Work'."))
        self.assertFalse(f("This was NOT done (send): the user said no."))

    def test_the_marker_never_reaches_a_chat_provider(self):
        a = self.agent
        with mock.patch.dict(os.environ, {"FC_ADVISOR_MODEL": "big-model"}):
            chat = a._prepare_kwargs({"model": "m", "messages": [], self.advisor.ADVISOR_KEY: True},
                                     "small", None, "chat")
            resp = a._prepare_kwargs({"model": "m", "messages": [], self.advisor.ADVISOR_KEY: True},
                                     "small", None, "responses")
            plain = a._prepare_kwargs({"model": "m", "messages": []}, "small", None, "chat")
        self.assertEqual(chat["model"], "big-model")
        self.assertNotIn(self.advisor.ADVISOR_KEY, chat)
        self.assertEqual(resp["model"], "big-model")
        self.assertEqual(plain["model"], "small")

    def test_one_round_of_read_only_tools_then_an_answer(self):
        advisor = self.advisor
        sent = []
        streams = [[chunk(calls=[call(0, "m1", "read_memory", "{}")], finish="tool_calls")],
                   [chunk(content='{"verdict": "fix", "advice": "Remove the duplicate line."}',
                          finish="stop")]]

        def fake_create(**kw):
            sent.append(kw)
            return streams.pop(0), "test"
        sess = self.sessions.Session(name="advisortest2")
        with self.sessions.use(sess), \
                mock.patch.object(self.agent, "_create_completion", fake_create), \
                mock.patch.object(self.agent, "_read_context", lambda: "## Work\n- a\n- a\n"):
            out = advisor.call("u", "asked", "digest")
        self.assertEqual(out["verdict"], "fix")
        self.assertEqual(len(sent), 2)
        self.assertTrue(sent[0]["tools"] and not sent[1]["tools"])
        self.assertTrue(sent[0][advisor.ADVISOR_KEY])
        self.assertIn("- a", json.dumps(sent[1]["messages"]))
        names = {t["function"]["name"] for t in advisor.TOOLS}
        self.assertEqual(names, {"read_memory", "read_file", "search_history", "list_scheduled"})

    def test_ask_tool_once_a_turn(self):
        advisor = self.advisor
        sess = self.sessions.Session(name="advisortest3")
        with mock.patch.object(advisor, "call", lambda *a, **k: {"verdict": "fine", "advice": "",
                                                                  "ms": 1, "kind": "asked"}):
            first = advisor.ask(sess, "u", [], "am I right?")
            second = advisor.ask(sess, "u", [], "and now?")
        self.assertIn("nothing wrong", first)
        self.assertIn("already been asked", second)

    def test_ask_tool_only_when_on(self):
        names = lambda: {t["function"]["name"] for t in self.agent.build_context_tools()}
        self.assertIn("ask_advisor", names())
        with mock.patch.dict(os.environ, {"FC_ADVISOR": ""}):
            self.assertNotIn("ask_advisor", names())

    def test_checkup_once_a_day_and_only_after_a_change(self):
        advisor = self.advisor
        home = tempfile.mkdtemp()
        files = os.path.join(home, "files") + "/"
        os.makedirs(files)
        with open(files + "context.md", "w") as f:
            f.write("## Work\n- a\n")
        self.assertTrue(advisor.checkup_due("u", files))
        with mock.patch.object(advisor, "call", lambda *a, **k: {"verdict": "fine", "advice": "",
                                                                  "ms": 1, "kind": "checkup"}):
            advisor.checkup("u", files, "## Work\n- a\n")
        self.assertFalse(advisor.checkup_due("u", files))
        self.assertTrue(os.path.exists(os.path.join(home, advisor.STATE_FILE)))
        self.assertFalse(os.path.exists(files + advisor.STATE_FILE))
        later = time.time() + advisor.CHECKUP_EVERY + 60
        self.assertFalse(advisor.checkup_due("u", files, now=later))   # unchanged
        os.utime(files + "context.md", (later - 30, later - 30))
        self.assertTrue(advisor.checkup_due("u", files, now=later))
        self.assertFalse(advisor.checkup_due("u", files, now=time.time() + 60))


if __name__ == "__main__":
    unittest.main()
