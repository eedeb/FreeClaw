"""What OpenAI's GPT-6 models are trained on, as FreeClaw sends it
(src/responses_api.py, the plan tool, AGENTS.md in tool results).

    python -m unittest tests.test_gpt6_shapes
"""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.agent as agent  # noqa: E402
import src.responses_api as ra  # noqa: E402


def fn(name, props, required=None):
    return {"type": "function", "function": {"name": name, "description": "d", "parameters": {
        "type": "object", "properties": {k: {"type": "string"} for k in props}, "required": required or []}}}


CONV = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "fix it"},
    {"role": "assistant", "content": "Looking first.", "tool_calls": [
        {"id": "c1", "function": {"name": "mcp_developer_patch",
                                  "arguments": json.dumps({"patch": "*** Begin Patch\n*** End Patch"})}},
        {"id": "c2", "function": {"name": "mcp_developer_run", "arguments": json.dumps({"command": "ls"})}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "Patched"},
    {"role": "tool", "tool_call_id": "c2", "content": "exit 0"},
    {"role": "assistant", "content": "Done."},
    {"role": "user", "content": "now test it"},
]
TOOLS = [fn("mcp_developer_patch", ["patch"], ["patch"]), fn("mcp_developer_run", ["command", "path", "timeout"])]


class TestRequest(unittest.TestCase):
    def build(self, **extra):
        return ra.build_request({"model": "gpt-6-luna", "messages": CONV, "tools": TOOLS, **extra})

    def test_phase_on_assistant_messages(self):
        items = self.build()["input"]
        said = [i for i in items if i.get("role") == "assistant"]
        self.assertEqual([(i["content"], i["phase"], i["type"]) for i in said],
                         [("Looking first.", "commentary", "message"), ("Done.", "final_answer", "message")])
        self.assertFalse(any("phase" in i for i in items if i.get("role") == "user"))
        off = self.build(**{ra.PHASE_KEY: False})["input"]
        self.assertFalse(any("phase" in i for i in off))

    def test_a_different_effort_is_an_input_item_and_the_request_stays_put(self):
        req = self.build(**{ra.EFFORT_KEY: "high"})
        self.assertEqual(req["reasoning"]["effort"], ra.REASONING_EFFORT)
        items = req["input"]
        at = items.index({"type": "configuration_update", "reasoning": {"effort": "high"}})
        self.assertEqual(items[at + 1], {"role": "user", "content": "now test it"})
        plain = self.build()
        self.assertEqual(plain["input"], [i for i in items if i.get("type") != "configuration_update"])
        self.assertNotIn("configuration_update", json.dumps(self.build(**{ra.EFFORT_KEY: ra.REASONING_EFFORT})))

    def test_the_effort_item_stays_put_when_a_tool_returns_an_image(self):
        conv = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "old", "ts": "2026-11-23 10:00"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "add horses", "ts": "2026-11-23 11:00"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c1", "function": {"name": "mcp_developer_check", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "Checked"},
                # how agent._with_tool_images sends a screenshot
                {"role": "user", "content": [{"type": "text", "text": "Image returned by check:"}]}]
        before = ra.build_request({"model": "m", "messages": conv[:4], ra.EFFORT_KEY: "high"})["input"]
        after = ra.build_request({"model": "m", "messages": conv, ra.EFFORT_KEY: "high"})["input"]
        item = {"type": "configuration_update", "reasoning": {"effort": "high"}}
        self.assertEqual(before.index(item), after.index(item))
        self.assertEqual(after[after.index(item) + 1]["content"], "add horses")
        self.assertEqual(after[:len(before)], before, "the earlier request is a prefix of the later one")

    def test_freeform_patch(self):
        off = self.build()
        self.assertEqual([t["type"] for t in off["tools"]], ["function", "function"])
        req = self.build(**{ra.FREEFORM_KEY: True})
        patch = next(t for t in req["tools"] if t["name"] == "apply_patch")
        self.assertEqual(patch["type"], "custom")
        self.assertEqual(patch["format"]["syntax"], "lark")
        self.assertIn("begin_patch", patch["format"]["definition"])
        call = next(i for i in req["input"] if i.get("call_id") == "c1" and "output" not in i)
        self.assertEqual(call, {"type": "custom_tool_call", "call_id": "c1", "name": "apply_patch",
                                "input": "*** Begin Patch\n*** End Patch"})
        out = next(i for i in req["input"] if i.get("call_id") == "c1" and "output" in i)
        self.assertEqual(out["type"], "custom_tool_call_output")
        self.assertEqual(next(i for i in req["input"] if i.get("call_id") == "c2" and "output" in i)["type"],
                         "function_call_output")

    def test_freeform_with_allowed_tools(self):
        req = self.build(**{ra.FREEFORM_KEY: True, ra.TOOL_UNIVERSE_KEY: TOOLS + [fn("get_time", [])]})
        self.assertIn({"type": "custom", "name": "apply_patch"}, req["tool_choice"]["tools"])
        self.assertEqual(req[ra._NAMES_KEY], {"apply_patch": "mcp_developer_patch"})

    def test_shell_names(self):
        req = self.build(**{ra.SHELL_NAMES_KEY: True})
        run = next(t for t in req["tools"] if t["name"] == "mcp_developer_run")
        self.assertEqual(sorted(run["parameters"]["properties"]), ["cmd", "timeout", "workdir"])
        self.assertEqual(sorted(next(t for t in self.build()["tools"] if t["name"] == "mcp_developer_run")
                                ["parameters"]["properties"]), ["command", "path", "timeout"])


def ev(type_, **kw):
    return types.SimpleNamespace(type=type_, **kw)


class TestCreate(unittest.TestCase):
    def test_the_name_map_never_reaches_the_api(self):
        sent = {}
        client = types.SimpleNamespace(responses=types.SimpleNamespace(
            create=lambda **kw: sent.update(kw) or iter(())))
        list(ra.create(client, {"model": "m", "stream": True, "messages": CONV, "tools": TOOLS,
                                ra.FREEFORM_KEY: True}))
        self.assertNotIn(ra._NAMES_KEY, sent)
        self.assertIn("apply_patch", [t["name"] for t in sent["tools"]])


class TestStream(unittest.TestCase):
    def test_a_freeform_call_comes_back_as_json_arguments(self):
        events = [
            ev("response.output_item.added", output_index=1,
               item=types.SimpleNamespace(type="custom_tool_call", call_id="c9", name="apply_patch")),
            ev("response.custom_tool_call_input.delta", output_index=1, delta='*** Begin Patch\n+say "hi"\\'),
            ev("response.custom_tool_call_input.delta", output_index=1, delta="\n*** End Patch"),
            ev("response.output_item.done", output_index=1, item=types.SimpleNamespace(type="custom_tool_call")),
        ]
        args, name, cid = "", None, None
        for chunk in ra._translate(events, {}, {"apply_patch": "mcp_developer_patch"}):
            for tc in chunk.choices[0].delta.tool_calls or []:
                args += tc.function.arguments or ""
                name = tc.function.name or name
                cid = tc.id or cid
        self.assertEqual((name, cid), ("mcp_developer_patch", "c9"))   # FreeClaw's name, back
        self.assertEqual(json.loads(args), {"patch": '*** Begin Patch\n+say "hi"\\\n*** End Patch'})


class TestPlan(unittest.TestCase):
    def test_trained_shape_and_one_in_progress(self):
        params = agent.PLAN_TOOL["function"]["parameters"]
        self.assertEqual(params["required"], ["plan"])
        self.assertIn("explanation", params["properties"])
        sess = types.SimpleNamespace(turn_plan=[])
        out = agent._update_plan(sess, [{"step": "a", "status": "completed"},
                                        {"step": "b", "status": "in_progress"},
                                        {"step": "c", "status": "in_progress"}])
        self.assertEqual(out, "Plan updated: 1 of 3 completed.")
        self.assertEqual([p["status"] for p in sess.turn_plan], ["completed", "in_progress", "pending"])

    def test_dispatch_reads_plan(self):
        sess = agent._sess()
        agent._run_tool("update_plan", {"plan": [{"step": "x", "status": "pending"}], "explanation": "why"})
        self.assertEqual(sess.turn_plan, [{"step": "x", "status": "pending"}])


class TestAgentsMd(unittest.TestCase):
    def setUp(self):
        self.user = tempfile.mkdtemp()
        self.files = os.path.join(self.user, "files")
        os.makedirs(os.path.join(self.files, "projects", "app", "src"))
        with open(os.path.join(self.files, "projects", "app", "AGENTS.md"), "w") as f:
            f.write("Use tabs.")
        agent.set_static_dir(self.files)
        agent._sess().messages = []
        self.registry = {n: {"server": {"name": "developer", "workspace": True}, "tool": n.split("_", 2)[2]}
                         for n in ("mcp_developer_read", "mcp_developer_patch")}

    def tearDown(self):
        shutil.rmtree(self.user, ignore_errors=True)

    def test_once_per_conversation(self):
        with mock.patch.object(agent, "registry_for", lambda u: self.registry):
            block = agent._agents_md_for("mcp_developer_read", {"path": "projects/app/src/x.py"}, "text")
            self.assertTrue(block.startswith("\n\n# AGENTS.md instructions for projects/app\n\n<INSTRUCTIONS>\nUse tabs."))
            agent._sess().messages = [{"role": "tool", "content": "text" + block}]
            self.assertEqual(agent._agents_md_for("mcp_developer_read", {"path": "projects/app/a.py"}, "t"), "")
            self.assertEqual(agent._agents_md_for("get_time", {}, "t"), "")


if __name__ == "__main__":
    unittest.main()
