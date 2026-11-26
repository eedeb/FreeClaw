"""request_captcha_help looks for a real human check before handing the
browser to the person: a page with nothing to solve is reported back to the
agent, and asking again for the same page hands it over anyway.

    python tests/test_captcha_check.py
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import src.browser_server as bs  # noqa: E402


class _Frame:
    def __init__(self, url):
        self.url = url


class _Page:
    def __init__(self, url, shows, frames=()):
        self.url, self._shows = url, shows
        self.frames = [_Frame(url)] + [_Frame(f) for f in frames]

    async def evaluate(self, js):
        if isinstance(self._shows, Exception):
            raise self._shows
        return self._shows

    async def title(self):
        return "Shop"


class _Context:
    async def storage_state(self):
        return {"cookies": [], "origins": []}


class _Browser:
    def __init__(self, page):
        self.page, self.context = page, _Context()

    def alive(self):
        return True

    async def ensure(self):
        return self.page


def call(tools):
    with mock.patch("src.browser_handoff.write_state_file", return_value="/x/state.json"):
        return asyncio.run(tools.t_request_captcha_help("check"))[0]["text"]


def tools_for(page):
    t = bs.Tools(_Browser(page), lambda *a, **k: None)
    t._status = lambda *a, **k: None
    return t


class CaptchaCheck(unittest.TestCase):
    def test_no_check_is_reported_not_handed_over(self):
        out = call(tools_for(_Page("https://shop.example/", False)))
        self.assertIn("No CAPTCHA or human check was found", out)
        self.assertNotIn("state_file", out)

    def test_asking_again_hands_it_over(self):
        t = tools_for(_Page("https://shop.example/", False))
        call(t)
        self.assertIn("state_file", call(t))

    def test_a_check_is_handed_over_at_once(self):
        self.assertIn("state_file", call(tools_for(_Page("https://shop.example/", True))))

    def test_a_challenge_frame_counts(self):
        page = _Page("https://shop.example/", False, frames=["https://www.google.com/recaptcha/api2/anchor"])
        self.assertIn("state_file", call(tools_for(page)))

    def test_unsure_hands_it_over(self):
        self.assertIn("state_file", call(tools_for(_Page("https://shop.example/", RuntimeError("gone")))))

    def test_another_page_is_looked_at_again(self):
        t = tools_for(_Page("https://shop.example/", False))
        call(t)
        t.b.page = _Page("https://shop.example/cart", False)
        self.assertIn("No CAPTCHA", call(t))


if __name__ == "__main__":
    unittest.main(verbosity=1)
