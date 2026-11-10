"""The Conversations app: the current conversation and the archived ones,
each continued or deleted; the Files app rooted at the files folder.

    python tests/test_conversations.py
"""
import json
import os
import shutil
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)


class TestConversations(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(ROOT, "Flask"))
        try:
            import src.agent as agent
            import main
        except Exception as e:                               # pragma: no cover
            raise unittest.SkipTest(f"Couldn't import FreeClaw: {e}")
        cls.agent, cls.main = agent, main

    def setUp(self):
        self.user = "convtest-" + os.urandom(3).hex()
        self.home = os.path.join(self.main.STATIC_DIR, self.user)
        os.makedirs(os.path.join(self.home, "files"), exist_ok=True)
        os.makedirs(os.path.join(self.home, "history"), exist_ok=True)
        self.addCleanup(shutil.rmtree, self.home, True)
        self.client = self.main.app.test_client()
        with self.client.session_transaction() as s:
            s["authenticated"] = True
            s["current_user"] = self.user
        self.api = f"/api/users/{self.user}/conversations"

    def archive(self, stamp, said, title=None):
        record = {"archived_at": "2026-11-01 10:00",
                  "messages": [{"role": "user", "content": said},
                               {"role": "assistant", "content": "Sure: " + said}]}
        if title:
            record["title"] = title
        with open(os.path.join(self.home, "history", stamp + ".json"), "w", encoding="utf-8") as f:
            json.dump(record, f)

    def current(self, said):
        self.main.save_conversation(self.user, [{"role": "system", "content": "SYS"},
                                                {"role": "user", "content": said},
                                                {"role": "assistant", "content": "ok"}], title="Now chat")

    def test_list(self):
        self.current("what's on today")
        self.archive("2026-11-01_100000", "plan my trip to Lisbon", title="Lisbon trip")
        self.archive("2026-11-05_090000", "write a cover letter")
        data = self.client.get(self.api).get_json()
        self.assertEqual(data["current"]["title"], "Now chat")
        self.assertEqual(data["current"]["messages"], 2)
        self.assertEqual(data["current"]["last"], "what's on today")
        self.assertEqual([p["id"] for p in data["past"]], ["2026-11-05_090000", "2026-11-01_100000"],
                         "newest first")
        self.assertEqual(data["past"][0]["title"], "write a cover letter", "untitled: its first message")
        self.assertEqual(data["past"][1]["title"], "Lisbon trip")
        self.assertEqual(data["past"][1]["label"], "2026-11-01 10:00")

    def test_continue(self):
        self.current("what's on today")
        self.archive("2026-11-01_100000", "plan my trip to Lisbon", title="Lisbon trip")
        r = self.client.post(self.api + "/2026-11-01_100000/continue")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        conv = self.client.get("/api/conversation").get_json()
        said = [m["content"] for m in conv["messages"] if m["role"] == "user"]
        self.assertEqual(said, ["plan my trip to Lisbon"])
        self.assertEqual(conv["title"], "Lisbon trip")
        self.assertEqual(conv["messages"][0]["role"], "system", "a fresh system message")
        self.assertNotEqual(conv["messages"][0]["content"], "SYS")
        past = self.client.get(self.api).get_json()["past"]
        self.assertNotIn("2026-11-01_100000", [p["id"] for p in past], "it's the current one now")
        self.assertEqual([p["title"] for p in past], ["Now chat"], "the one it replaced is kept, title and all")

    def test_continue_refused_mid_turn(self):
        self.archive("2026-11-01_100000", "plan my trip")
        real = self.main.live_turns.current
        self.main.live_turns.current = lambda name: object()
        try:
            r = self.client.post(self.api + "/2026-11-01_100000/continue")
        finally:
            self.main.live_turns.current = real
        self.assertEqual(r.status_code, 409)
        self.assertTrue(os.path.exists(os.path.join(self.home, "history", "2026-11-01_100000.json")))

    def test_delete(self):
        self.archive("2026-11-01_100000", "plan my trip")
        self.assertEqual(self.client.delete(self.api + "/2026-11-01_100000").status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(self.home, "history", "2026-11-01_100000.json")))
        self.assertEqual(self.client.delete(self.api + "/2026-11-01_100000").status_code, 404)

    def test_only_archives(self):
        for bad in ("..%2Fconversation", "conversation", "2026-11-01_100000.json", "x"):
            self.assertIn(self.client.delete(f"{self.api}/{bad}").status_code, (400, 404), bad)
            self.assertIn(self.client.post(f"{self.api}/{bad}/continue").status_code, (400, 404), bad)

    def test_logged_out(self):
        anon = self.main.app.test_client()
        self.assertEqual(anon.get(self.api).status_code, 401)
        self.assertEqual(anon.delete(self.api + "/2026-11-01_100000").status_code, 401)

    def test_page(self):
        chat = self.client.get(f"/chat?user={self.user}").get_data(as_text=True)
        self.assertIn('id="win-convos"', chat)
        self.assertIn('<span class="app-label">Conversations</span>', chat)
        self.assertIn("const FILES_ROOT = 'files';", chat, "Files opens on the files folder")


if __name__ == "__main__":
    unittest.main()
