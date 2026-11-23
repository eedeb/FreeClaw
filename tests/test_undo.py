"""Undo a turn's file changes (src/undo.py, agent._undo_capture).

    python -m unittest tests.test_undo
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.agent as agent  # noqa: E402
import src.undo as undo  # noqa: E402


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def read(path):
    with open(path) as f:
        return f.read()


class TestUndo(unittest.TestCase):
    def setUp(self):
        self.user = tempfile.mkdtemp()
        self.files = os.path.join(self.user, "files")
        put(os.path.join(self.files, "notes.txt"), "v1")
        put(os.path.join(self.files, "context.md"), "memory")

    def tearDown(self):
        shutil.rmtree(self.user, ignore_errors=True)

    def p(self, rel):
        return os.path.join(self.files, rel)

    def test_one_turn(self):
        undo.capture(self.user, self.files, "t1", "fix my notes", [self.p("notes.txt"), self.p("new.txt"),
                                                                   self.p("context.md")])
        undo.capture(self.user, self.files, "t1", "fix my notes", [self.p("notes.txt")])   # kept once
        put(self.p("notes.txt"), "v2")
        put(self.p("new.txt"), "made")
        put(self.p("context.md"), "memory changed")
        recs = undo.records(self.user)
        self.assertEqual([r["label"] for r in recs], ["fix my notes"])
        self.assertEqual(sorted(recs[0]["files"]), ["new.txt", "notes.txt"])
        restored, deleted, failed = undo.undo(self.user, self.files, "t1")
        self.assertEqual((restored, deleted, failed), (["notes.txt"], ["new.txt"], []))
        self.assertEqual(read(self.p("notes.txt")), "v1")
        self.assertFalse(os.path.exists(self.p("new.txt")))
        self.assertEqual(read(self.p("context.md")), "memory changed")    # memory isn't undone
        self.assertEqual(undo.records(self.user), [])
        with self.assertRaises(ValueError):
            undo.undo(self.user, self.files, "t1")

    def test_an_older_turn_undoes_the_later_ones_too(self):
        undo.capture(self.user, self.files, "t1", "one", [self.p("notes.txt")])
        put(self.p("notes.txt"), "v2")
        time.sleep(0.01)
        undo.capture(self.user, self.files, "t2", "two", [self.p("notes.txt"), self.p("b.txt")])
        put(self.p("notes.txt"), "v3")
        put(self.p("b.txt"), "b")
        restored, deleted, _ = undo.undo(self.user, self.files, "t1")
        self.assertEqual(read(self.p("notes.txt")), "v1")
        self.assertEqual((restored, deleted), (["notes.txt"], ["b.txt"]))

    def test_only_the_newest_kept(self):
        for n in range(undo.KEEP + 3):
            undo.capture(self.user, self.files, f"t{n:02}", str(n), [self.p("notes.txt")])
            time.sleep(0.002)
        self.assertEqual(len(undo.records(self.user)), undo.KEEP)

    def test_outside_the_files_is_never_kept(self):
        undo.capture(self.user, self.files, "t1", "x", [os.path.join(self.user, "conversation.json"),
                                                        "/etc/passwd"])
        self.assertEqual(undo.records(self.user), [])

    def test_the_note(self):
        text = undo.note(["a.py"], ["b.py"], [], "fix it")
        self.assertIn('"fix it"', text)
        self.assertIn("a.py", text)
        self.assertIn("weren't undone", text)


class TestCapture(unittest.TestCase):
    def setUp(self):
        self.user = tempfile.mkdtemp()
        self.files = os.path.join(self.user, "files")
        put(os.path.join(self.files, "projects", "app", "main.py"), "x = 1\n")
        put(os.path.join(self.files, "draft.md"), "hi")
        agent.set_static_dir(self.files)
        agent._sess().reset_tool_run()
        agent._sess().messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "make it 2"}]
        dev = {"server": {"name": "developer", "workspace": True}}
        self.registry = {"mcp_developer_patch": dict(dev, tool="patch"),
                         "mcp_developer_write": dict(dev, tool="write"),
                         "mcp_developer_read": dict(dev, tool="read")}

    def tearDown(self):
        shutil.rmtree(self.user, ignore_errors=True)

    def test_file_tools_and_the_developer_app(self):
        with mock.patch.object(agent, "registry_for", lambda u: self.registry):
            agent._undo_capture("edit_file", {"filename": "draft.md", "old_str": "hi", "new_str": "yo"})
            agent._undo_capture("mcp_developer_patch", {"patch": (
                "*** Begin Patch\n*** Update File: projects/app/main.py\n*** Move to: projects/app/app.py\n"
                "-x = 1\n+x = 2\n*** End Patch")})
            agent._undo_capture("mcp_developer_read", {"path": "projects/app/main.py"})
            agent._undo_capture("mcp_developer_write", {"path": "../../etc/x"})   # refused: nothing kept
        recs = undo.records(self.user)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["label"], "make it 2")
        self.assertEqual(sorted(recs[0]["files"]), ["draft.md", "projects/app/app.py", "projects/app/main.py"])
        self.assertIsNone(recs[0]["files"]["projects/app/app.py"])


if __name__ == "__main__":
    unittest.main()


class TestRoutes(unittest.TestCase):
    """GET and POST /api/undo (Flask/main.py)."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Flask"))
        try:
            import main
        except Exception as e:                               # pragma: no cover
            raise unittest.SkipTest(f"Couldn't import FreeClaw: {e}")
        cls.main = main

    def setUp(self):
        self.user = "undotest-" + os.urandom(3).hex()
        self.home = os.path.join(self.main.STATIC_DIR, self.user)
        self.files = os.path.join(self.home, "files")
        put(os.path.join(self.files, "a.txt"), "before")
        with open(os.path.join(self.home, "conversation.json"), "w") as f:
            json.dump({"messages": [{"role": "system", "content": "s"}]}, f)
        self.addCleanup(shutil.rmtree, self.home, True)
        self.client = self.main.app.test_client()
        with self.client.session_transaction() as s:
            s["authenticated"] = True
            s["current_user"] = self.user

    def test_list_undo_and_the_note(self):
        undo.capture(self.home, self.files, "t1", "change a", [os.path.join(self.files, "a.txt")])
        put(os.path.join(self.files, "a.txt"), "after")
        turns = self.client.get("/api/undo").get_json()["turns"]
        self.assertEqual([(t["id"], t["label"], t["files"]) for t in turns], [("t1", "change a", ["a.txt"])])
        r = self.client.post("/api/undo", json={"id": "t1"})
        self.assertEqual(r.get_json(), {"restored": ["a.txt"], "deleted": [], "failed": []})
        self.assertEqual(read(os.path.join(self.files, "a.txt")), "before")
        with open(os.path.join(self.home, "conversation.json")) as f:
            last = json.load(f)["messages"][-1]
        self.assertEqual(last["auto_kind"], "undo")
        self.assertIn("a.txt", last["content"])
        self.assertEqual(self.client.post("/api/undo", json={"id": "t1"}).status_code, 409)

    def test_signed_out(self):
        self.assertEqual(self.main.app.test_client().get("/api/undo").status_code, 401)
