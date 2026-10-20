"""The Developer app's git (src/dev_git.py): GitHub https only, the token kept
to git, repo config that can't run commands, and the tools and gate around it.

    python tests/test_dev_git.py

No network: repos are local, with origin pointed at a github.com URL, and
anything that would reach GitHub goes through a stand-in git_process."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import src.dev_git as dev_git      # noqa: E402
import src.dev_server as dev_server  # noqa: E402

TOKEN = "ghp_testtoken1234567890"


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})


class GitTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fc-git-")
        self.repo = os.path.join(self.root, "proj")
        os.makedirs(self.repo)
        sh("git", "init", "-q", "-b", "main", cwd=self.repo)
        sh("git", "remote", "add", "origin", "https://github.com/someone/proj.git", cwd=self.repo)
        with open(os.path.join(self.repo, "a.py"), "w") as f:
            f.write("print(1)\n")
        env = mock.patch.dict(os.environ, {"FC_DEV_GITHUB_TOKEN": TOKEN, "FC_DEV_WORKSPACE": self.root})
        env.start()
        self.addCleanup(env.stop)
        dev_git._identity[TOKEN] = ("Test", "1+test@users.noreply.github.com")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_only_github_https(self):
        for bad in ("http://github.com/a/b", "https://gitlab.com/a/b", "git@github.com:a/b.git",
                    "file:///etc", "https://github.com/a", "https://github.com.evil.com/a/b",
                    "https://github.com/a/b/../../x"):
            with self.assertRaises(dev_git.GitError, msg=bad):
                dev_git.repo_url(bad)
        self.assertEqual(dev_git.repo_url("https://github.com/you/proj.git"), ("you", "proj"))

    def test_token_only_in_git_config_vars(self):
        env = dev_git.git_env()
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertNotIn(TOKEN, " ".join(k for k in env))
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "https")

    def test_token_never_reaches_a_run(self):
        out = dev_server.run(self.root, {"command": "env"})
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("FC_DEV_GITHUB_TOKEN", out)

    def test_repo_hooks_and_commands_dont_run(self):
        marker = os.path.join(self.root, "ran")
        hook = os.path.join(self.repo, ".git", "hooks", "pre-commit")
        with open(hook, "w") as f:
            f.write(f"#!/bin/sh\ntouch {marker}\n")
        os.chmod(hook, 0o755)
        sh("git", "config", "core.fsmonitor", f"touch {marker}", cwd=self.repo)
        sh("git", "config", "core.pager", f"touch {marker}", cwd=self.repo)
        dev_git.status(self.repo)
        dev_git.commit(self.repo, "first")
        self.assertFalse(os.path.exists(marker), "something from the repo's config ran")

    def test_status_diff_commit_branch(self):
        self.assertIn("a.py", dev_git.status(self.repo))
        self.assertIn("first", dev_git.commit(self.repo, "first"))
        with open(os.path.join(self.repo, "a.py"), "a") as f:
            f.write("print(2)\n")
        self.assertIn("+print(2)", dev_git.diff(self.repo))
        self.assertEqual(dev_git.branch(self.repo, "fix", switch=True), "On fix.")
        self.assertIn("fix", dev_git.branch(self.repo))
        with self.assertRaises(dev_git.GitError):
            dev_git.branch(self.repo, "--orphan")

    def test_push_goes_to_the_checked_origin(self):
        dev_git.commit(self.repo, "first")
        seen = []
        real = dev_git.git_process

        def fake(argv, cwd, env):
            if "push" in argv:
                seen.append(argv)
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return real(argv, cwd, env)
        with mock.patch.object(dev_git, "git_process", fake):
            self.assertIn("Pushed to main", dev_git.push(self.repo))
        self.assertIn("https://github.com/someone/proj.git", seen[0])
        self.assertIn("core.hooksPath=/dev/null", seen[0])
        sh("git", "remote", "set-url", "origin", "https://evil.example/x.git", cwd=self.repo)
        with mock.patch.object(dev_git, "git_process", fake):
            with self.assertRaises(dev_git.GitError):
                dev_git.push(self.repo)
        self.assertEqual(len(seen), 1, "pushed to a non-GitHub origin")

    def test_no_token_says_connect(self):
        with mock.patch.dict(os.environ, {"FC_DEV_GITHUB_TOKEN": ""}):
            with self.assertRaises(dev_git.GitError) as e:
                dev_git.push(self.repo)
        self.assertIn("Connect GitHub", str(e.exception))

    def test_token_scrubbed_from_output(self):
        def fake(argv, cwd, env):
            return subprocess.CompletedProcess(argv, 1, b"", f"fatal: {TOKEN} bad".encode())
        with mock.patch.object(dev_git, "git_process", fake):
            code, out = dev_git.git(["status"], self.repo)
        self.assertNotIn(TOKEN, out)

    def test_tools_listed_and_fenced(self):
        names = dev_server.TOOL_NAMES
        for t in ("git_clone", "git_status", "git_diff", "git_commit", "git_push", "git_pull", "git_branch"):
            self.assertIn(t, names)
        with self.assertRaises(dev_server.ToolError):
            dev_server.Tools().call("git_status", {"repo": "../outside"})
        self.assertIn("a.py", dev_server.Tools().call("git_status", {"repo": "proj"}))


class GateTests(unittest.TestCase):
    def test_push_is_gated_like_run(self):
        import src.agent as agent
        entry = {"server": {"builtin": True, "workspace": True}, "tool": "git_push"}
        self.assertTrue(agent._is_developer_entry(entry))
        self.assertFalse(agent._is_developer_entry({**entry, "tool": "git_status"}))


if __name__ == "__main__":
    unittest.main(verbosity=1)
