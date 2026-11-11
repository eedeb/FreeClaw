"""Background jobs, the toolbox, and connected apps from code (the Developer app).

    python tests/test_dev_jobs_tools.py
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

import src.dev_server as dev_server  # noqa: E402
import src.workbench as workbench  # noqa: E402


def wait_for(cond, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


class TestJobs(unittest.TestCase):

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="fc-jobs-")
        self.top = os.path.join(self.ws, "files")
        os.makedirs(os.path.join(self.top, "projects"))
        self.root = os.path.join(self.top, "projects")
        self.addCleanup(shutil.rmtree, self.ws, True)
        self.tools = dev_server.Tools()

    def test_start_check_finish(self):
        out = self.tools.t_start_job(self.root, {"command": "for i in 1 2 3; do echo tick $i; sleep 0.2; done"})
        self.assertIn("Started job j1", out)
        report = self.tools.t_check_job(self.root, {"id": "j1", "wait": 10})
        self.assertIn("tick 3", report)
        self.assertIn("exit 0", report)

    def test_list_and_stop(self):
        self.tools.t_start_job(self.root, {"command": "sleep 30"})
        self.assertIn("running", self.tools.t_check_job(self.root, {}))
        report = self.tools.t_stop_job(self.root, {"id": "j1"})
        self.assertIn("stopped on request", report)
        self.assertFalse(self.tools.jobs.get("j1").running())

    def test_unknown_job(self):
        with self.assertRaises(dev_server.ToolError):
            self.tools.t_check_job(self.root, {"id": "j9"})

    def test_idle_jobs_are_stopped(self):
        jobs = dev_server.Jobs(idle=0.5)
        jobs.start("sleep 30", lambda: dev_server.spawn(["sleep", "30"], self.top))
        job = jobs.get("j1")
        self.assertTrue(wait_for(lambda: not job.running(), 10))
        self.assertIn("nobody checked on it", job.report())

    def test_too_long_jobs_are_stopped(self):
        jobs = dev_server.Jobs(idle=60, longest=0.5)
        jobs.start("sleep 30", lambda: dev_server.spawn(["sleep", "30"], self.top))
        self.assertTrue(wait_for(lambda: not jobs.get("j1").running(), 10))

    def test_at_most_so_many(self):
        jobs = dev_server.Jobs()
        for _ in range(dev_server.JOB_MAX_RUNNING):
            jobs.start("sleep 30", lambda: dev_server.spawn(["sleep", "30"], self.top))
        with self.assertRaises(dev_server.ToolError):
            jobs.start("sleep 30", lambda: dev_server.spawn(["sleep", "30"], self.top))
        jobs.stop_all("done")

    def test_tools_listed(self):
        names = {t["name"] for t in dev_server.TOOLS}
        self.assertTrue({"start_job", "check_job", "stop_job"} <= names)
        self.assertTrue({"start_job", "check_job", "stop_job"} <= dev_server.TOOL_NAMES)


class TestGate(unittest.TestCase):

    def test_start_job_asks_first_like_run(self):
        import src.agent as agent
        self.assertIn("start_job", agent.DEVELOPER_GATED)
        self.assertIn("run", agent.DEVELOPER_GATED)


class TestToolbox(unittest.TestCase):

    def test_line(self):
        import src.agent as agent
        ws = tempfile.mkdtemp(prefix="fc-box-")
        self.addCleanup(shutil.rmtree, ws, True)
        self.assertIn("empty", agent._toolbox_line(ws))
        os.makedirs(os.path.join(ws, "toolbox"))
        with open(os.path.join(ws, "toolbox", "heic_to_pdf.py"), "w") as f:
            f.write("#!/usr/bin/env python3\n# Convert HEIC photos to one PDF.\n")
        with open(os.path.join(ws, "toolbox", ".hidden"), "w") as f:
            f.write("x")
        line = agent._toolbox_line(ws)
        self.assertIn("heic_to_pdf.py (Convert HEIC photos to one PDF.)", line)
        self.assertNotIn(".hidden", line)
        self.assertEqual(agent._toolbox_line(os.path.join(ws, "missing")), "")


class TestConnectedAppsFromCode(unittest.TestCase):
    """A run calls the agent's connected apps; the request carries the run's
    own user, whatever it claims; the install's checks apply."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fc-wb-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.seen = []
        self.real_handle = workbench.handle
        self.addCleanup(setattr, workbench, "handle", self.real_handle)
        workbench.handle = lambda user, request: (self.seen.append((user, request)),
                                                  {"ok": True, "result": {"user": user}})[1]
        path = os.path.join(self.dir, "wb", "wb.sock")
        server = socket.socket(socket.AF_UNIX)
        os.makedirs(os.path.dirname(path))
        server.bind(path)
        server.listen(4)
        self.addCleanup(server.close)

        def accept():
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                threading.Thread(target=workbench._serve_one, args=(conn,), daemon=True).start()
        threading.Thread(target=accept, daemon=True).start()
        self.env = {"FC_DEV_USER": "alice", "FC_DEV_TOOLS_SOCKET": path}
        self.old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def run_code(self, code):
        path = os.path.join(self.dir, "t.py")
        with open(path, "w") as f:
            f.write(code)
        proc = dev_server.spawn([sys.executable, path], self.dir)
        out = proc.stdout.read().decode()
        proc.wait()
        return out

    def test_a_run_calls_as_its_own_user(self):
        out = self.run_code("import freeclaw_tools as ft, json\n"
                            "print(json.dumps(ft.call('GMAIL_FETCH_EMAILS', {'n': 1})))\n")
        self.assertEqual(json.loads(out.strip().splitlines()[-1]), {"user": "alice"})
        user, request = self.seen[-1]
        self.assertEqual((user, request["op"], request["tool"]), ("alice", "call", "GMAIL_FETCH_EMAILS"))

    def test_a_run_cant_claim_another_user(self):
        self.run_code("import json, os, socket\n"
                      "s = socket.socket(socket.AF_UNIX); s.connect(os.environ['FREECLAW_TOOLS'])\n"
                      "s.sendall(json.dumps({'op': 'tools', 'user': 'bob'}).encode() + b'\\n')\n"
                      "print(s.recv(4096))\n")
        self.assertEqual(self.seen[-1][0], "alice")

    def test_the_program_never_sees_the_workbench_itself(self):
        out = self.run_code("import os\nprint(os.environ.get('FC_DEV_TOOLS_SOCKET'), os.environ.get('FC_DEV_USER'))\n")
        self.assertEqual(out.strip(), "None None")

    def test_checks_refuse(self):
        workbench.handle = self.real_handle
        entry = {"server": {"name": "Gmail"}, "tool": "send"}
        workbench.CHECKS.append(lambda user, e, args: "Not done: sends need approval." if e["tool"] == "send" else None)
        self.addCleanup(workbench.CHECKS.clear)
        import src.users as users
        import src.agent as agent
        real_exists, real_registry = users.user_exists, agent.registry_for
        users.user_exists = lambda name: name == "alice"
        agent.registry_for = lambda user: {"mcp_Gmail_send": entry,
                                           "mcp_developer_run": {"server": {"builtin": True}, "tool": "run"}}
        try:
            r = workbench.handle("alice", {"op": "call", "tool": "send", "arguments": {}})
            self.assertEqual(r, {"ok": False, "error": "Not done: sends need approval."})
            tools = workbench.handle("alice", {"op": "tools"})["result"]
            self.assertEqual([t["name"] for t in tools], ["mcp_Gmail_send"], "built-ins aren't reachable")
            self.assertFalse(workbench.handle("alice", {"op": "call", "tool": "run"})["ok"])
            self.assertFalse(workbench.handle("mallory", {"op": "tools"})["ok"])
        finally:
            users.user_exists, agent.registry_for = real_exists, real_registry


if __name__ == "__main__":
    unittest.main()
