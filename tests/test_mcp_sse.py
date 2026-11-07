"""mcp_client reads an SSE reply as UTF-8, whatever requests guesses.

    python tests/test_mcp_sse.py
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import requests  # noqa: E402
from src import mcp_client  # noqa: E402


def response(body, ctype):
    r = requests.models.Response()
    r.status_code = 200
    r.headers["Content-Type"] = ctype
    r._content = body
    # What requests' adapter does with the headers: ISO-8859-1 for a text/*
    # type with no charset.
    r.encoding = requests.utils.get_encoding_from_headers(r.headers)
    return r


class TestSse(unittest.TestCase):
    MSG = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "shouldn’t · café · 📚"}]}}

    def test_sse_without_charset_is_utf8(self):
        body = ("event: message\ndata: " + json.dumps(self.MSG, ensure_ascii=False) + "\n\n").encode("utf-8")
        got = mcp_client._extract_message(response(body, "text/event-stream"))
        self.assertEqual(got["result"]["content"][0]["text"], "shouldn’t · café · 📚")

    def test_plain_json_still_works(self):
        body = json.dumps(self.MSG, ensure_ascii=False).encode("utf-8")
        got = mcp_client._extract_message(response(body, "application/json"))
        self.assertEqual(got["result"]["content"][0]["text"], "shouldn’t · café · 📚")


if __name__ == "__main__":
    unittest.main()
