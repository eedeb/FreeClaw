"""Handing the agent's browser back from the chat (chat.html's sign-in card)
and a renewal that must not take it again (Flask/main.py
/api/browser/agent/control, browser.html renewControl).

    python -m unittest tests.test_browser_handback
"""
import json
import os
import shutil
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestControl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(ROOT, "Flask"))
        try:
            import main
        except Exception as e:                               # pragma: no cover
            raise unittest.SkipTest(f"Couldn't import FreeClaw: {e}")
        cls.main = main

    def setUp(self):
        self.user = "handback-" + os.urandom(3).hex()
        home = os.path.join(self.main.STATIC_DIR, self.user)
        os.makedirs(os.path.join(home, "files"), exist_ok=True)
        with open(os.path.join(home, "conversation.json"), "w") as f:
            json.dump({"messages": [{"role": "system", "content": "s"}]}, f)
        self.addCleanup(shutil.rmtree, home, True)
        self.client = self.main.app.test_client()
        with self.client.session_transaction() as s:
            s["authenticated"] = True
            s["current_user"] = self.user
        self.held = {"v": True}
        live = self.main.browser_live
        for name, fn in (("holding", lambda u: self.held["v"]), ("take", lambda u, away=False: True),
                         ("release", lambda u: self.held.update(v=False))):
            p = mock.patch.object(live, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def post(self, body):
        return self.client.post(f"/api/browser/agent/control?user={self.user}", json=body)

    def test_a_renewal_keeps_control_it_still_has(self):
        self.assertEqual(self.post({"take": True, "renew": True}).status_code, 200)

    def test_handed_back_from_the_chat_a_renewal_doesnt_take_it_again(self):
        self.assertEqual(self.post({"take": False}).status_code, 200)
        r = self.post({"take": True, "renew": True})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["control"], False)

    def test_taking_control_on_purpose_still_works(self):
        self.held["v"] = False
        self.assertEqual(self.post({"take": True}).status_code, 200)


class TestPages(unittest.TestCase):
    def test_the_sign_in_card_hands_back_before_it_sends(self):
        with open(os.path.join(ROOT, "Flask", "templates", "chat.html"), encoding="utf-8") as f:
            text = f.read()
        i = text.index("done.addEventListener('click', async () => {")
        block = text[i:text.index("actions.appendChild(done);", i)]
        self.assertLess(block.index("take: false"), block.index("sendMessage()"))

    def test_the_browser_page_renews_without_retaking(self):
        with open(os.path.join(ROOT, "Flask", "templates", "browser.html"), encoding="utf-8") as f:
            text = f.read()
        i = text.index("async function renewControl()")
        self.assertIn("renew: true", text[i:i + 800])


if __name__ == "__main__":
    unittest.main()
