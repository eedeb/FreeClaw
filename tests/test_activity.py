"""Tests for Activity (src/activity.py, /activity and /api/activity in
Flask/main.py), the "Do this every morning?" offer in chat.html, and
scheduled tasks routed by Jev (src/agent.py).

Run from the repo root:  python -m unittest discover -s tests

Imports FreeClaw itself, so it needs its requirements installed; skipped with
a note where they aren't. The chat page's offer is checked with node when
it's installed.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class TestObserve(unittest.TestCase):
    """What a turn's entry says, from its events and its reply."""

    @classmethod
    def setUpClass(cls):
        try:
            import src.activity as activity
        except Exception as e:                               # pragma: no cover
            raise unittest.SkipTest(f"Couldn't import FreeClaw: {e}")
        cls.activity = activity

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "u", "activity.json")
        self.messages = [{"role": "assistant", "content": "an earlier reply"}]

    def run_turn(self, ask, events=(), reply=None, boom=None, servers=None):
        def stream():
            self.messages.append({"role": "user", "content": ask})
            for e in events:
                yield e
            if boom:
                raise boom
            if reply is not None:
                self.messages.append({"role": "assistant", "content": reply})
        for _ in self.activity.observe(stream(), self.path, "schedule", ask, lambda: self.messages,
                                       server_of=(servers or {}).get):
            pass
        return self.activity.read(self.path)[-1]

    def test_done_with_apps(self):
        e = self.run_turn("PING [due 2026-10-28 08:00, repeats daily]: Routine: Morning briefing. weather please",
                          [{"type": "tool_call", "name": "web_search", "arguments": {}},
                           {"type": "tool_call", "name": "mcp_browser_navigate", "arguments": {}},
                           {"type": "tool_call", "name": "mcp_cal_list", "arguments": {}},
                           {"type": "tool_call", "name": "web_search", "arguments": {}}],
                          reply="Sunny.", servers={"mcp_cal_list": "Calendar"})
        self.assertEqual(e["status"], "done")
        self.assertEqual(e["title"], "Morning briefing")
        self.assertEqual(e["ask"], "weather please")
        self.assertEqual(e["apps"], ["Web", "Browser", "Calendar"])
        self.assertEqual(e["steps"], 4)
        self.assertEqual(e["summary"], "Sunny.")

    def test_plain_ping(self):
        e = self.run_turn("PING [due 2026-10-28 08:00]: call the dentist", reply="Reminder: call the dentist.")
        self.assertEqual((e["title"], e["ask"], e["source"]), ("", "call the dentist", "schedule"))

    def test_needs_you(self):
        refused = ("The command was NOT run: it needs the user's approval, and this turn has nobody "
                   "to ask, so it was refused.")
        e = self.run_turn("PING [due x]: clean up", [{"type": "tool_result", "name": "bash", "result": refused}],
                          reply="I need your OK to run the cleanup.")
        self.assertEqual(e["status"], "needs_you")
        self.assertEqual(e["waiting"], ["run a command"])

    def test_host_patterns(self):
        """A host's own refusals and reply kinds (WAITING, REPLY_STATUS)."""
        a = self.activity
        with mock.patch.object(a, "WAITING", a.WAITING + [(re.compile(r"^Off \(([^)]*)\)"), "switched_off", "x")]), \
                mock.patch.object(a, "REPLY_STATUS", [(re.compile(r"^Out of credits"), "credits")]):
            e = self.run_turn("PING [due x]: a", [{"type": "tool_result", "result": "Off (delete a file)"}],
                              reply="It's off.")
            self.assertEqual((e["status"], e["waiting"]), ("switched_off", ["delete a file"]))
            self.assertEqual(self.run_turn("PING [due x]: b", reply="Out of credits for the month.")["status"],
                             "credits")

    def test_failed_turn_raises_and_says_so(self):
        with self.assertRaises(RuntimeError):
            self.run_turn("PING [due x]: break", boom=RuntimeError("down"))
        e = self.activity.read(self.path)[-1]
        self.assertEqual(e["status"], "failed")
        self.assertEqual(e["summary"], "", "an earlier turn's reply isn't this one's")

    def test_stopped(self):
        self.assertEqual(self.run_turn("PING [due x]: s", [{"type": "stopped"}], reply="Stopped.")["status"],
                         "stopped")

    def test_long_reply_and_limits(self):
        e = self.run_turn("PING [due x]: long", reply="x" * 5000)
        self.assertEqual(len(e["summary"]), self.activity.SUMMARY_MAX + 1)
        with mock.patch.object(self.activity, "KEEP", 3):
            for i in range(5):
                self.run_turn(f"PING [due x]: n{i}", reply=str(i))
            self.assertEqual([x["summary"] for x in self.activity.read(self.path)], ["2", "3", "4"])
        old = self.activity.read(self.path)
        old[0]["ts"] = time.time() - (self.activity.DAYS + 1) * 86400
        with open(self.path, "w") as f:
            json.dump(old, f)
        self.run_turn("PING [due x]: new", reply="new")
        self.assertTrue(all(x["ts"] > time.time() - self.activity.DAYS * 86400
                            for x in self.activity.read(self.path)))

    def test_composio_names_the_app(self):
        a = self.activity
        args = {"tools": [{"tool_slug": "GMAIL_FETCH_EMAILS"}, {"tool_slug": "GOOGLECALENDAR_EVENTS_LIST"}]}
        self.assertEqual(a.app_of("mcp_composio_COMPOSIO_MULTI_EXECUTE_TOOL", args, "composio"),
                         "Gmail, Google Calendar")
        self.assertIsNone(a.app_of("mcp_composio_COMPOSIO_SEARCH_TOOLS", {}, "composio"))


class TestActivityApi(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(ROOT, "Flask"))
        try:
            import src.activity as activity
            import src.agent as agent
            import main
        except Exception as e:                               # pragma: no cover
            raise unittest.SkipTest(f"Couldn't import FreeClaw: {e}")
        cls.activity, cls.agent, cls.main = activity, agent, main

    def setUp(self):
        self.user = "acttest-" + os.urandom(3).hex()
        os.makedirs(os.path.join(self.main.STATIC_DIR, self.user, "files"), exist_ok=True)
        self.addCleanup(shutil.rmtree, os.path.join(self.main.STATIC_DIR, self.user), True)
        self.client = self.main.app.test_client()
        with self.client.session_transaction() as s:
            s["authenticated"] = True
            s["current_user"] = self.user

    def test_one_tasks_window(self):
        """Activity, Scheduled and Routines are one window, Tasks."""
        r = self.client.get(f"/schedule?user={self.user}&tab=routines")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        for tab in ("activity", "scheduled", "routines"):
            self.assertIn(f'id="panel-{tab}"', page)
        self.assertIn('const FIRST_TAB = "routines";', page)
        self.assertIn(json.dumps(self.user), page)
        self.assertIn('"key": "morning"', page, "the routines are in it")
        self.assertIn('const FIRST_TAB = "";', self.client.get(f"/schedule?user={self.user}&tab=nope")
                      .get_data(as_text=True), "an unknown tab is ignored")
        chat = self.client.get(f"/chat?user={self.user}").get_data(as_text=True)
        self.assertEqual(chat.count('data-app="schedule"'), 2, "one button, one window")
        self.assertIn('<span class="app-label">Tasks</span>', chat)
        self.assertIn('id="activity-badge"', chat)
        self.assertNotIn('id="icon-activity"', chat)
        self.assertNotIn('id="win-activity"', chat)

    def test_old_activity_address(self):
        r = self.client.get(f"/activity?user={self.user}")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/schedule?", r.headers["Location"])
        self.assertIn("tab=activity", r.headers["Location"])
        self.assertIn(f"user={self.user}", r.headers["Location"])

    def test_entries_newest_first_and_since(self):
        path = self.activity.path_for(self.user)
        self.assertNotIn(os.sep + "files" + os.sep, path)
        now = time.time()
        with open(path, "w") as f:
            json.dump([{"ts": now - 100, "status": "done"}, {"ts": now - 10, "status": "needs_you"}], f)
        d = self.client.get(f"/api/activity?user={self.user}").get_json()
        self.assertEqual([e["status"] for e in d["entries"]], ["needs_you", "done"])
        d = self.client.get(f"/api/activity?user={self.user}&since={now - 50}").get_json()
        self.assertEqual(d["new_count"], 1)
        self.assertEqual(self.client.delete(f"/api/activity?user={self.user}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/activity?user={self.user}").get_json()["entries"], [])

    def test_unknown_user_and_signed_out(self):
        self.assertEqual(self.client.get("/api/activity?user=nobody-" + os.urandom(3).hex()).status_code, 404)
        self.assertEqual(self.main.app.test_client().get(f"/api/activity?user={self.user}").status_code, 401)

    def test_a_fired_ping_is_recorded(self):
        """The scheduler's own pass: the turn runs, and leaves an entry."""
        when = (datetime.now() - timedelta(minutes=1)).strftime(self.agent.PING_TIME_FORMAT)
        with open(self.main.user_ping_path(self.user), "w") as f:
            f.write(f"{when} - Routine: Morning briefing. weather\n")

        def fake_stream(user_input=None, ping=False, **kw):
            self.assertTrue(ping)
            self.agent.get_messages().append({"role": "user", "content": user_input})
            yield {"type": "tool_call", "name": "web_search", "arguments": {}}
            self.agent.get_messages().append({"role": "assistant", "content": "It's sunny."})

        with mock.patch.object(self.main, "list_users", return_value=[self.user]), \
                mock.patch.object(self.agent, "agent_stream", fake_stream):
            self.main._fire_due_pings()
        entries = self.activity.read(self.activity.path_for(self.user))
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["title"], entries[0]["status"], entries[0]["summary"], entries[0]["apps"]),
                         ("Morning briefing", "done", "It's sunny.", ["Web"]))


class TestPingRouting(unittest.TestCase):
    """A scheduled task takes Jev's route when there is one, and every tool
    when there isn't (or Jev only has its no-tools route for tiny messages)."""

    @classmethod
    def setUpClass(cls):
        try:
            import src.agent as agent
            import src.session as sessions
        except Exception as e:                               # pragma: no cover
            raise unittest.SkipTest(f"Couldn't import FreeClaw: {e}")
        cls.agent, cls.sessions = agent, sessions

    def first_request_tools(self, route):
        seen = {}

        class Stop(Exception):
            pass

        def fake_create(**kw):
            seen["tools"] = kw.get("tools")
            raise Stop()

        sess = self.sessions.Session(name="pingroute-" + os.urandom(3).hex(),
                                     static_dir=tempfile.mkdtemp() + os.sep)
        self.addCleanup(shutil.rmtree, sess.static_dir, True)
        with self.sessions.use(sess), \
                mock.patch.object(self.agent, "_jev_route", return_value=route), \
                mock.patch.object(self.agent, "_create_completion", side_effect=fake_create):
            try:
                for _ in self.agent.agent_stream(user_input="PING [due x]: check the weather", ping=True):
                    pass
            except Stop:
                pass
            except Exception:
                pass
        return seen.get("tools"), sess

    def test_no_jev_means_every_tool(self):
        tools, sess = self.first_request_tools(None)
        self.assertIsNotNone(tools)
        everything = self.agent.tools_for(None)
        self.assertEqual(len(tools), len(everything))
        self.assertEqual(sess.turn_tag, self.agent.PING_TAG)

    def test_jev_route_narrows_the_tools(self):
        import src.jev as jev
        groups, _servers = self.agent._jev_tool_groups(None)
        route = jev.Route("Research", "precise", {"web_search"}, [], [], 5, {})
        tools, sess = self.first_request_tools((route, groups, []))
        names = {self.agent._tool_name(t) for t in tools}
        self.assertIn("web_search", names)
        self.assertLess(len(tools), len(self.agent.tools_for(None)), "fewer than every tool")
        self.assertEqual(sess.turn_tag, self.agent.PING_TAG, "a ping keeps its own label")

    def test_tiny_local_route_is_ignored(self):
        local = mock.Mock(local=True)
        tools, _ = self.first_request_tools((local, [], []))
        self.assertEqual(len(tools), len(self.agent.tools_for(None)))


class TestRepeatOffer(unittest.TestCase):
    """chat.html's rule for offering to repeat a request (run with node)."""

    def setUp(self):
        if not shutil.which("node"):
            raise unittest.SkipTest("node isn't installed")
        with open(os.path.join(ROOT, "Flask", "templates", "chat.html"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("  const REPEATABLE = ")
        end = text.index("  const OFFERED_KEY")
        fn_start = text.index("  function repeatCadence")
        fn_end = text.index("  function offeredBefore")
        self.js = text[start:end] + text[fn_start:fn_end]

    def cadence(self, cases):
        prog = self.js + "\nconsole.log(JSON.stringify(" + json.dumps(cases) + ".map(c => repeatCadence(c[0], c[1]))));"
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_rules(self):
        cases = [
            ["Summarize my inbox", ["mcp_gmail_fetch"]],                    # daily
            ["What's the weather this week in Chicago?", ["web_search"]],   # weekly
            ["Check the price of the Sony XM6", ["mcp_browser_navigate"]],   # daily
            ["Summarize my inbox", []],                                      # nothing looked up
            ["Summarize my inbox every morning", ["web_search"]],            # already says when
            ["Check the weather", ["add_ping"]],                             # scheduled itself
            ["Write me a poem about cats", ["web_search"]],                  # not a check
            ["/reset", ["web_search"]],                                      # a command
        ]
        self.assertEqual(self.cadence(cases),
                         ["daily", "weekly", "daily", None, None, None, None, None])


if __name__ == "__main__":
    unittest.main()


class TestRoutines(unittest.TestCase):
    """The Routines tab's catalog (src/routines.py)."""

    @classmethod
    def setUpClass(cls):
        try:
            import src.activity as activity
            import src.routines as routines
        except Exception as e:                               # pragma: no cover
            raise unittest.SkipTest(f"Couldn't import FreeClaw: {e}")
        cls.routines, cls.activity = routines, activity

    def test_catalog(self):
        rs = self.routines.ROUTINES
        self.assertEqual(len({r["key"] for r in rs}), len(rs))
        self.assertEqual(len({r["title"] for r in rs}), len(rs))
        for r in rs:
            self.assertNotIn(".", r["title"], "a title marks the task, so no full stop")
            self.assertIn(r["repeat"], ("daily", "weekly"))
            self.assertRegex(r["time"], r"^\d\d:\d\d$")
            if r["repeat"] == "weekly":
                self.assertIn(r.get("day"), range(7))
            self.assertEqual("{input}" in r["prompt"], bool(r.get("input")), r["key"])
            self.assertLessEqual(len(self.routines.action(r, "x" * 500)), 2000)

    def test_action_names_the_run(self):
        r = self.routines.ROUTINES[0]
        action = self.routines.action(r)
        self.assertTrue(action.startswith(f"Routine: {r['title']}. "))
        title, _ask = self.activity.clean_ask("PING [due 2026-10-28 07:00, repeats daily]: " + action)
        self.assertEqual(title, r["title"])
        detail = next(x for x in self.routines.ROUTINES if x.get("input"))
        self.assertIn("a b", self.routines.action(detail, "a\n\n b"))

    def test_catalog_is_what_the_page_gets(self):
        self.assertEqual([c["key"] for c in self.routines.catalog()], [r["key"] for r in self.routines.ROUTINES])


class TestTasksPage(unittest.TestCase):
    """The Tasks page's routine scheduling, on the agent's clock (run with node)."""

    def test_next_run(self):
        if not shutil.which("node"):
            raise unittest.SkipTest("node isn't installed")
        with open(os.path.join(ROOT, "Flask", "templates", "schedule.html"), encoding="utf-8") as f:
            text = f.read()
        pick = lambda start, end: text[text.index(start):text.index(end)]
        js = (pick("  const parse = s =>", "  function dayLabel")
              + pick("  const at = (d, days", "  function fillChips")
              + pick("  function nextRun", "  function describe"))
        prog = ("let serverNow = null, readAt = 0;\n" + js +
                "serverNow = parse('2026-10-27 08:30'); readAt = Date.now();\n"   # a Tuesday
                "console.log(JSON.stringify([nextRun('07:00','daily'), nextRun('09:00','daily'),"
                " nextRun('18:00','weekly',6), nextRun('09:00','weekly',1), nextRun('08:00','weekly',1)]));")
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), ["2026-10-28 07:00", "2026-10-27 09:00", "2026-11-01 18:00",
                                                  "2026-10-27 09:00", "2026-11-03 08:00"])
