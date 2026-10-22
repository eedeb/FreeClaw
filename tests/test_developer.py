"""Tests for the Developer app: the tool server (src/dev_server.py), the
approval gate around its run tool (src/agent.py), and the off switch.

Run from the repo root:  python -m unittest discover -s tests

The tool-server tests need nothing but the standard library. The gate and
off-switch tests import FreeClaw itself, so they need its requirements
installed, and are skipped with a note where they aren't.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import src.dev_server as dev  # noqa: E402


class Workspace(unittest.TestCase):
    """A fresh workspace per test, with FC_DEV_WORKSPACE pointing at it."""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.root = os.path.realpath(os.path.join(self.base, "projects"))
        env = mock.patch.dict(os.environ, {"FC_DEV_WORKSPACE": self.root})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.base, True)
        self.tools = dev.Tools()
        self.addCleanup(self.tools.preview.stop)

    def put(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def call(self, name, **args):
        return self.tools.call(name, args)

    def error(self, name, **args):
        with self.assertRaises(dev.ToolError) as ctx:
            self.call(name, **args)
        return str(ctx.exception)


class TestPaths(Workspace):
    def test_workspace_created(self):
        self.call("ls")
        self.assertTrue(os.path.isdir(self.root))

    def test_no_workspace(self):
        with mock.patch.dict(os.environ, {"FC_DEV_WORKSPACE": ""}):
            self.assertIn("No workspace", self.error("ls"))

    def test_refuses_escapes(self):
        os.makedirs(self.root, exist_ok=True)
        outside = os.path.join(self.base, "secret.txt")
        with open(outside, "w") as f:
            f.write("secret")
        for bad in (outside, "/etc/passwd", "../secret.txt", "a/../../secret.txt", "C:\\x", "a\x00b"):
            with self.subTest(path=bad):
                self.error("read", path=bad)
        self.error("write", path="../evil.py", content="x")
        self.assertFalse(os.path.exists(os.path.join(self.base, "evil.py")))

    def test_refuses_symlink_out(self):
        os.makedirs(self.root, exist_ok=True)
        with open(os.path.join(self.base, "secret.txt"), "w") as f:
            f.write("secret")
        os.symlink(self.base, os.path.join(self.root, "link"))
        self.assertIn("outside", self.error("read", path="link/secret.txt"))
        self.assertIn("outside", self.error("write", path="link/new.txt", content="x"))

    def test_inside_is_fine(self):
        self.put("a/b.txt", "hi\n")
        self.assertIn("hi", self.call("read", path="a/b.txt"))
        self.assertIn("hi", self.call("read", path="./a/b.txt"))


class TestLs(Workspace):
    def test_tree_and_skips(self):
        self.put("app.py", "")
        self.put("src/lib.py", "")
        self.put("node_modules/x/index.js", "")
        self.put(".git/HEAD", "")
        out = self.call("ls")
        self.assertIn("src/", out)
        self.assertIn("  lib.py", out)
        self.assertIn("node_modules/ (skipped)", out)
        self.assertNotIn("index.js", out)
        self.assertNotIn("HEAD", out)

    def test_entry_cap(self):
        for i in range(dev.LS_MAX_ENTRIES + 25):
            self.put(f"f{i:04}.txt", "")
        out = self.call("ls")
        self.assertEqual(len(out.splitlines()), dev.LS_MAX_ENTRIES + 1)
        self.assertIn("25+ more entries", out)


class TestRead(Workspace):
    def setUp(self):
        super().setUp()
        self.put("big.py", "".join(f"line {n}\n" for n in range(1, 1001)))

    def test_default_slice_states_total(self):
        out = self.call("read", path="big.py")
        head, *lines = out.splitlines()
        self.assertEqual(head, f"big.py · lines 1-{dev.READ_DEFAULT_LINES} of 1000 "
                               f"(next: start={dev.READ_DEFAULT_LINES + 1})")
        self.assertEqual(len(lines), dev.READ_DEFAULT_LINES)
        self.assertEqual(lines[0], "  1\tline 1")

    def test_range_and_numbers(self):
        out = self.call("read", path="big.py", start=995, lines=10)
        head, *lines = out.splitlines()
        self.assertEqual(head, "big.py · lines 995-1000 of 1000")
        self.assertEqual(lines[0], " 995\tline 995")   # numbers right-aligned to the widest
        self.assertEqual(lines[-1], "1000\tline 1000")

    def test_hard_cap(self):
        out = self.call("read", path="big.py", lines=5000)
        self.assertEqual(len(out.splitlines()) - 1, dev.READ_MAX_LINES)

    def test_past_the_end(self):
        self.assertIn("1000 lines", self.error("read", path="big.py", start=2000))

    def test_long_line_cut(self):
        self.put("min.js", "x" * 5000)
        out = self.call("read", path="min.js")
        self.assertLess(len(out), dev.READ_LINE_CHARS + 100)
        self.assertIn("chars]", out)

    def test_binary_refused(self):
        with open(os.path.join(self.root, "b.bin"), "wb") as f:
            f.write(b"\x00\x01\x02")
        self.assertIn("not a text file", self.error("read", path="b.bin"))


class TestWriteEdit(Workspace):
    def test_write_creates_folders_and_reports_briefly(self):
        out = self.call("write", path="pkg/mod.py", content="a\nb\n")
        self.assertEqual(out, "Created pkg/mod.py (2 lines)")
        self.assertEqual(self.call("write", path="pkg/mod.py", content="c"), "Overwrote pkg/mod.py (1 lines)")

    def test_write_size_cap(self):
        self.assertIn("Too big", self.error("write", path="x", content="a" * (dev.WRITE_MAX_BYTES + 1)))

    def test_edit_unique(self):
        path = self.put("m.py", "a = 1\nb = 2\n")
        out = self.call("edit", path="m.py", old="b = 2", new="b = 3")
        self.assertEqual(out, "Edited m.py: 1 replacement, first at line 2")
        self.assertEqual(open(path).read(), "a = 1\nb = 3\n")

    def test_edit_not_found(self):
        self.put("m.py", "a = 1\n")
        self.assertIn("old not found", self.error("edit", path="m.py", old="zzz", new=""))

    def test_edit_whitespace_hint(self):
        self.put("m.py", "    a = 1\n")
        self.assertIn("whitespace", self.error("edit", path="m.py", old="  a = 1\n  ", new=""))

    def test_edit_ambiguous_needs_all(self):
        path = self.put("m.py", "x\nx\nx\n")
        msg = self.error("edit", path="m.py", old="x", new="y")
        self.assertIn("matches 3 times", msg)
        self.assertIn("all: true", msg)
        self.assertEqual(open(path).read(), "x\nx\nx\n")
        self.assertIn("3 replacements", self.call("edit", path="m.py", old="x", new="y", all=True))
        self.assertEqual(open(path).read(), "y\ny\ny\n")

    def test_edit_empty_old(self):
        self.put("m.py", "x")
        self.assertIn("use write", self.error("edit", path="m.py", old="", new="y"))

    def test_edit_keeps_crlf(self):
        path = self.put("w.txt", "")
        with open(path, "wb") as f:
            f.write(b"one\r\ntwo\r\n")
        self.call("edit", path="w.txt", old="two", new="2")
        self.assertEqual(open(path, "rb").read(), b"one\r\n2\r\n")


class TestSearch(Workspace):
    def test_hits_format(self):
        self.put("a.py", "import os\nx = 1\n")
        self.put("b.txt", "import os\n")
        self.assertEqual(self.call("search", pattern=r"^import"), "a.py:1: import os\nb.txt:1: import os")
        self.assertEqual(self.call("search", pattern="import", glob="*.py"), "a.py:1: import os")
        self.assertEqual(self.call("search", pattern="IMPORT", ignore_case=True, path="b.txt"),
                         "b.txt:1: import os")

    def test_cap(self):
        self.put("many.txt", "hit\n" * (dev.SEARCH_MAX_HITS + 30))
        out = self.call("search", pattern="hit").splitlines()
        self.assertEqual(len(out), dev.SEARCH_MAX_HITS + 1)
        self.assertIn("30 more hits", out[-1])

    def test_skips_dependencies_and_bad_regex(self):
        self.put("node_modules/x.js", "needle")
        self.assertEqual(self.call("search", pattern="needle"), "No matches.")
        self.assertIn("Bad regex", self.error("search", pattern="("))


class TestRun(Workspace):
    def test_command_line(self):
        self.assertEqual(dev.command_line({"file": "app.py", "args": ["a b", "c"]}),
                         ("python app.py 'a b' c", None))
        self.assertEqual(dev.command_line({"file": "go.sh"}), ("bash go.sh", None))
        self.assertEqual(dev.command_line({"command": " ls -la "}), ("ls -la", None))
        for bad in ({}, {"command": "ls", "file": "a.py"}, {"file": "a.rb"},
                    {"file": "a.py", "args": "x"}, {"command": "ls", "args": ["x"]},
                    {"file": "a.py", "env": {"X": "1"}}):
            with self.subTest(args=bad):
                self.assertIsNone(dev.command_line(bad)[0])
        # A folder to run in is allowed only as part of the shown command, so
        # what's approved (and what a saved rule matches) is what runs.
        self.assertEqual(dev.command_line({"file": "a.py", "cwd": "other"}), ("cd other && python a.py", None))

    def test_python_file_with_args(self):
        self.put("hello.py", "import sys\nprint('hi', *sys.argv[1:])\n")
        out = self.call("run", file="hello.py", args=["there"])
        self.assertTrue(out.startswith("exit 0 · "), out)
        self.assertIn("hi there", out)

    def test_bash_command_runs_in_workspace(self):
        self.put("marker.txt", "")
        out = self.call("run", command="ls; echo oops >&2; exit 3")
        self.assertTrue(out.startswith("exit 3"), out)
        self.assertIn("marker.txt", out)
        self.assertIn("oops", out)

    def test_output_trimmed(self):
        self.put("loud.py", "for i in range(20000): print('line', i)\n")
        out = self.call("run", file="loud.py")
        self.assertLess(len(out), dev.OUTPUT_HEAD + dev.OUTPUT_TAIL + 200)
        self.assertIn("line 0\n", out)
        self.assertIn("line 19999", out)
        self.assertRegex(out, r"\[… [\d,]+ characters cut …\]")

    def test_timeout(self):
        out = self.call("run", command="sleep 5", timeout=1)
        self.assertTrue(out.startswith("timed out after 1s"), out)

    def test_run_file_must_be_inside(self):
        self.error("run", file="../x.py")

    def test_trimmer_streaming_matches_text(self):
        t = dev.Trimmer(head=5, tail=4)
        live = "".join(t.feed(c) for c in ("abc", "defgh", "ijklmnop"))
        self.assertEqual(live, "abcde")
        self.assertEqual(t.text(), "abcde\n[… 7 characters cut …]\nmnop")
        short = dev.Trimmer(head=10, tail=10)
        short.feed("hello")
        self.assertEqual(short.text(), "hello")


class TestArgumentNames(Workspace):
    """The names a model reaches for when it doesn't recall the schema."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.root, "app"), exist_ok=True)
        with open(os.path.join(self.root, "app", "x.py"), "w") as f:
            f.write("print('ok')\n" + "\n".join(f"# {i}" for i in range(300)) + "\n")

    def test_edit_and_read_aliases(self):
        self.call("edit", filename="app/x.py", old_string="print('ok')", new_string="print('yes')")
        out = self.call("read", file_path="app/x.py", offset=1, limit=1)
        self.assertIn("print('yes')", out)

    def test_read_defaults_to_120_lines(self):
        out = self.call("read", path="app/x.py")
        self.assertIn("120", out)
        self.assertNotIn("# 150", out)

    def test_run_in_a_folder(self):
        self.assertEqual(dev.command_line({"command": "python3 x.py", "path": "app"}),
                         ("cd app && python3 x.py", None))
        self.assertEqual(dev.command_line({"file": "x.py", "cwd": "app/"}), ("cd app && python x.py", None))
        self.assertIn("ok", self.call("run", command="python3 x.py", path="app"))

    def test_run_folder_stays_inside(self):
        for bad in ("../", "/etc", "a/../../b"):
            with self.subTest(where=bad):
                shown, err = dev.command_line({"command": "ls", "path": bad})
                self.assertIsNone(shown)
                self.assertIn("inside the workspace", err)

    def test_git_tools_take_path_for_repo(self):
        self.assertEqual(dev.normalize("git_status", {"path": "app"}), {"repo": "app"})

    def test_unknown_still_refused(self):
        self.assertIn("Unknown argument", dev.command_line({"command": "ls", "bogus": 1})[1])


class TestProjectsKnown(unittest.TestCase):
    """The agent (and Jev) are told which projects exist, so "my minecraft
    game" means the code in the workspace, not the real game."""

    def setUp(self):
        import src.mcp_client as mc
        self.mc = mc
        self.static = tempfile.mkdtemp(prefix="fc-proj-")
        ws = os.path.join(self.static, "alice", "files", "projects")
        for d in ("minecraft", "Home-web", ".hidden"):
            os.makedirs(os.path.join(ws, d))
        open(os.path.join(ws, "notes.txt"), "w").close()
        p = mock.patch.object(mc, "STATIC_ROOT", self.static)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.static, ignore_errors=True)

    def test_project_list(self):
        self.assertEqual(self.mc.developer_projects("alice"), ["Home-web", "minecraft"])
        self.assertEqual(self.mc.developer_projects("nobody"), [])
        self.assertEqual(self.mc.developer_projects("../x"), [])

    def test_in_instructions_and_routing(self):
        import src.agent as agent
        text = agent._instructions(coding=True, projects=["Home-web", "minecraft"])
        self.assertIn("Their projects in the workspace: Home-web, minecraft.", text)
        self.assertNotIn("Their projects", agent._instructions(coding=False, projects=["x"]))
        with mock.patch.object(agent, "_tools_user", lambda: "alice"):
            self.assertIn("[their projects: Home-web, minecraft]", agent._projects_hint("mcp:developer"))
            self.assertEqual(agent._projects_hint("mcp:browser"), "")
            line = agent._jev_server_about({"name": "developer", "workspace": True, "description": "Code."},
                                           [], None)
            self.assertIn("The user's own projects here: Home-web, minecraft.", line)


class TestPreview(Workspace):
    def test_serves_workspace_only(self):
        self.put("site/index.html", "<h1>hi</h1>")
        with open(os.path.join(self.base, "secret.txt"), "w") as f:
            f.write("secret")
        os.symlink(os.path.join(self.base, "secret.txt"), os.path.join(self.root, "site", "s.txt"))
        out = self.call("preview", path="site")
        url = out.split(" at ")[1].split()[0].rstrip(".")
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertIn(b"<h1>hi</h1>", urllib.request.urlopen(url, timeout=5).read())
        with self.assertRaises(Exception):
            urllib.request.urlopen(url + "s.txt", timeout=5)
        # Same folder again: the same server.
        self.assertIn(url, self.call("preview", path="site"))


class TestProtocol(Workspace):
    def test_list_and_call(self):
        self.put("a.txt", "x\n")
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            "not json",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "read", "arguments": {"path": "a.txt"}}}),
            json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "read", "arguments": {"path": "../a"}}}),
        ]
        out = io.BytesIO()
        dev.Server(out=out).serve(stdin=[(l + "\n").encode() for l in lines])
        replies = [json.loads(l) for l in out.getvalue().decode().splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        names = [t["name"] for t in replies[1]["result"]["tools"]]
        self.assertEqual(names, ["ls", "read", "write", "edit", "overview", "outline", "find", "search", "run", "git_clone", "git_status", "git_diff", "git_commit", "git_push", "git_fetch", "git_pull", "git_branch", "preview"])
        self.assertIn("1\tx", replies[2]["result"]["content"][0]["text"])
        self.assertTrue(replies[3]["result"]["isError"])

    def test_descriptions_stay_short(self):
        for tool in dev.TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertLessEqual(len(tool["description"]), 140)


class TestOffSwitch(unittest.TestCase):
    def test_disabled(self):
        with mock.patch.dict(os.environ, {"FC_DISABLE_DEVELOPER": "", "FC_DISABLE_BASH": ""}):
            self.assertFalse(dev.disabled())
        with mock.patch.dict(os.environ, {"FC_DISABLE_DEVELOPER": "1"}):
            self.assertTrue(dev.disabled())
        with mock.patch.dict(os.environ, {"FC_DISABLE_DEVELOPER": "", "FC_DISABLE_BASH": "true"}):
            self.assertTrue(dev.disabled())


def _import_freeclaw():
    """FreeClaw's own modules, or a reason to skip."""
    try:
        import src.mcp_client as mcp_client
        import src.agent as agent
        return mcp_client, agent
    except ImportError as e:                                 # pragma: no cover
        raise unittest.SkipTest(f"FreeClaw's requirements aren't installed: {e}")


class TestServerList(unittest.TestCase):
    def setUp(self):
        self.mcp_client, _ = _import_freeclaw()

    def test_on_by_default_and_off_with_switch(self):
        with mock.patch.dict(os.environ, {"FC_DISABLE_DEVELOPER": "", "FC_DISABLE_BASH": ""}):
            dev_entry = [s for s in self.mcp_client._merge_builtins([]) if s["name"] == "developer"]
            self.assertEqual(len(dev_entry), 1)
            self.assertTrue(dev_entry[0]["enabled"])
            browser = [s for s in self.mcp_client._merge_builtins([]) if s["name"] == "browser"][0]
            self.assertFalse(browser["enabled"])
        with mock.patch.dict(os.environ, {"FC_DISABLE_DEVELOPER": "1"}):
            names = [s["name"] for s in self.mcp_client._merge_builtins([])]
            self.assertNotIn("developer", names)
            self.assertIn("browser", names)
            self.assertTrue(self.mcp_client.is_builtin("developer"))

    def test_for_user_binds_workspace(self):
        spec = next(s for s in self.mcp_client.BUILTIN_SERVERS if s["name"] == "developer")
        alice = self.mcp_client.for_user(spec, "alice")
        bob = self.mcp_client.for_user(spec, "bob")
        self.assertTrue(alice["env"]["FC_DEV_WORKSPACE"].endswith(
            os.path.join("Flask", "static", "alice", "files", "projects")))
        self.assertNotEqual(self.mcp_client._sig(alice), self.mcp_client._sig(bob))
        self.assertNotIn("env", self.mcp_client.for_user(spec, "../x"))


class TestApprovalGate(unittest.TestCase):
    """mcp_developer_run must go through the same gate as run_bash_command."""

    def setUp(self):
        self.mcp_client, self.agent = _import_freeclaw()
        spec = next(s for s in self.mcp_client.BUILTIN_SERVERS if s["name"] == "developer")
        self.registry = {
            "mcp_developer_run": {"server": spec, "tool": "run"},
            "mcp_developer_read": {"server": spec, "tool": "read"},
        }
        patcher = mock.patch.object(self.agent, "registry_for", return_value=self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_run_is_gated_other_tools_are_not(self):
        self.assertTrue(self.agent._is_developer_run("mcp_developer_run"))
        self.assertFalse(self.agent._is_developer_run("mcp_developer_read"))
        self.assertFalse(self.agent._is_developer_run("run_bash_command"))

    def test_gate_branch_names_developer_run(self):
        import inspect
        src = inspect.getsource(self.agent.agent_stream)
        self.assertIn("command_name == 'run_bash_command' or _is_developer_run(command_name)", src)

    def test_dispatch_refuses_unapproved_run(self):
        with mock.patch.object(self.mcp_client, "call_tool", return_value="ran") as call:
            out = self.agent._run_tool("mcp_developer_run", {"command": "ls"}, bash_approved=False)
            self.assertIn("NOT run", out)
            call.assert_not_called()
            self.assertEqual(self.agent._run_tool("mcp_developer_run", {"command": "ls"},
                                                  bash_approved=True), "ran")
            # Reading needs no approval.
            self.assertEqual(self.agent._run_tool("mcp_developer_read", {"path": "a"}), "ran")

    def test_instructions_mention_developer_only_when_on(self):
        self.assertIn("edit (exact replacement)", self.agent._instructions(coding=True))
        self.assertNotIn("Developer workspace", self.agent._instructions())


class TestRoutes(unittest.TestCase):
    """The Flask side of the off switch: the page, the routes and the sidebar
    entry all go away together."""

    @classmethod
    def setUpClass(cls):
        _import_freeclaw()
        sys.path.insert(0, os.path.join(ROOT, "Flask"))
        try:
            import main
        except Exception as e:                               # pragma: no cover
            raise unittest.SkipTest(f"Couldn't import Flask/main.py: {e}")
        cls.main = main

    def setUp(self):
        self.user = "devtest-" + os.urandom(3).hex()
        os.makedirs(os.path.join(self.main.STATIC_DIR, self.user, "files"), exist_ok=True)
        self.addCleanup(shutil.rmtree, os.path.join(self.main.STATIC_DIR, self.user), True)
        self.client = self.main.app.test_client()
        with self.client.session_transaction() as s:
            s["authenticated"] = True
            s["current_user"] = self.user

    def test_on(self):
        with mock.patch.dict(os.environ, {"FC_DISABLE_DEVELOPER": "", "FC_DISABLE_BASH": ""}):
            r = self.client.put("/api/dev/file", json={"user": self.user, "path": "a/hi.py",
                                                       "content": "print('hi')\n", "create": True})
            self.assertEqual(r.status_code, 200, r.get_json())
            tree = self.client.get(f"/api/dev/tree?user={self.user}").get_json()
            self.assertEqual([f["path"] for f in tree["files"]], ["a/hi.py"])
            self.assertEqual(self.client.put("/api/dev/file", json={
                "user": self.user, "path": "a/hi.py", "content": "", "create": True}).status_code, 409)
            self.assertEqual(self.client.get(f"/api/dev/file?user={self.user}&path=../../x").status_code, 400)
            body = self.client.post("/api/dev/run", json={"user": self.user, "path": "a/hi.py"}).get_data(as_text=True)
            events = [json.loads(l[6:]) for l in body.split("\n\n") if l.startswith("data: ")]
            self.assertEqual(events[0], {"type": "start", "command": "python a/hi.py"})
            self.assertEqual(events[-1]["type"], "end")
            self.assertEqual(events[-1]["code"], 0)
            self.assertIn("hi", "".join(e.get("text", "") for e in events))
            r = self.client.post("/api/dev/rename", json={"user": self.user, "from": "a/hi.py", "to": "b.py"})
            self.assertEqual(r.get_json()["path"], "b.py")
            self.assertEqual(self.client.delete(f"/api/dev/file?user={self.user}&path=b.py").status_code, 200)
            self.assertIn(b'id="icon-developer"', self.client.get("/chat").data)
            self.assertEqual(self.client.get("/developer").status_code, 200)

    def test_delete_folder(self):
        with mock.patch.dict(os.environ, {"FC_DISABLE_DEVELOPER": "", "FC_DISABLE_BASH": ""}):
            for path in ("proj/a.py", "proj/sub/b.txt"):
                self.client.put("/api/dev/file", json={"user": self.user, "path": path, "content": "x",
                                                       "create": True})
            outside = os.path.join(self.main.STATIC_DIR, self.user, "files", "keep.txt")
            with open(outside, "w") as f:
                f.write("keep")
            root = self.main.mcp_client.developer_workspace(self.user)
            os.symlink(outside, os.path.join(root, "proj", "link.txt"))
            r = self.client.delete(f"/api/dev/file?user={self.user}&path=proj")
            self.assertEqual(r.status_code, 400, "not without recursive")
            r = self.client.delete(f"/api/dev/file?user={self.user}&path=proj&recursive=1")
            self.assertEqual(r.status_code, 200, r.get_json())
            self.assertFalse(os.path.exists(os.path.join(root, "proj")))
            self.assertTrue(os.path.exists(outside), "a link inside is removed, not followed")
            r = self.client.delete(f"/api/dev/file?user={self.user}&path=.&recursive=1")
            self.assertNotEqual(r.status_code, 200, "never the workspace itself")
            self.assertTrue(os.path.isdir(root))

    def test_off(self):
        with mock.patch.dict(os.environ, {"FC_DISABLE_DEVELOPER": "1"}):
            self.assertEqual(self.client.get("/developer").status_code, 404)
            for method, url in (("get", f"/api/dev/tree?user={self.user}"),
                                ("get", f"/api/dev/file?user={self.user}&path=a.py"),
                                ("put", "/api/dev/file"), ("post", "/api/dev/run"),
                                ("post", "/api/dev/rename"), ("post", "/api/dev/stop")):
                with self.subTest(url=url):
                    r = getattr(self.client, method)(url, json={"user": self.user, "path": "a.py"})
                    self.assertEqual(r.status_code, 404)
            chat = self.client.get("/chat").data
            self.assertNotIn(b'id="icon-developer"', chat)
            self.assertNotIn(b'id="win-developer"', chat)


if __name__ == "__main__":
    unittest.main()
