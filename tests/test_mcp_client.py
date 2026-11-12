"""mcp_client against a real (local) HTTP server and a real stdio child:
an SSE reply is taken as soon as the response arrives, a tool call isn't
sent twice after a server error, and a stdio child isn't called before its
handshake is done.

    python tests/test_mcp_client.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src import mcp_client  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, status, obj, headers=()):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        srv = self.server
        msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        method = msg.get("method")
        srv.seen.append((method, self.headers.get("Mcp-Session-Id")))
        if "id" not in msg:                                  # a notification
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "initialize":
            srv.sessions += 1
            return self._json(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {}},
                              [("Mcp-Session-Id", f"s{srv.sessions}")])
        if method == "tools/call":
            srv.calls += 1
            mode = srv.mode
            if mode == "broken":
                return self._json(502, {"error": "bad gateway"})
            if mode == "stale" and self.headers.get("Mcp-Session-Id") == "s1":
                return self._json(404, {"error": "unknown session"})
            if mode == "open-stream":
                # The answer, then the stream held open well past the test.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                answer = {"jsonrpc": "2.0", "id": msg["id"],
                          "result": {"content": [{"type": "text", "text": "done"}]}}
                for part in (b": hello\n\n", b"data: " + json.dumps(answer).encode() + b"\n\n"):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                self.wfile.flush()
                srv.release.wait(20)
                return
            return self._json(200, {"jsonrpc": "2.0", "id": msg["id"],
                                    "result": {"content": [{"type": "text", "text": "ok"}]}})
        return self._json(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": []}})


class TestHttp(unittest.TestCase):

    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.seen, self.httpd.calls, self.httpd.sessions = [], 0, 0
        self.httpd.mode = "ok"
        self.httpd.release = threading.Event()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.server = {"name": "t", "transport": "http", "token": "",
                       "url": f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"}
        mcp_client.release(self.server)

    def tearDown(self):
        self.httpd.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        mcp_client.release(self.server)

    def test_plain_call(self):
        self.assertEqual(str(mcp_client.call_tool(self.server, "x", {})), "ok")

    def test_sse_answer_is_taken_while_the_stream_stays_open(self):
        self.httpd.mode = "open-stream"
        started = time.monotonic()
        self.assertEqual(str(mcp_client.call_tool(self.server, "x", {})), "done")
        self.assertLess(time.monotonic() - started, 5)

    def test_a_server_error_does_not_send_the_call_again(self):
        self.httpd.mode = "broken"
        with self.assertRaises(Exception):
            mcp_client.call_tool(self.server, "send_email", {})
        self.assertEqual(self.httpd.calls, 1)

    def test_a_stale_session_is_reopened_and_the_call_retried(self):
        self.assertEqual(str(mcp_client.call_tool(self.server, "x", {})), "ok")   # opens s1
        self.httpd.mode = "stale"
        self.assertEqual(str(mcp_client.call_tool(self.server, "x", {})), "ok")
        self.assertEqual(self.httpd.sessions, 2)


_SLOW_CHILD = r'''
import json, sys, time
ready = False
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        time.sleep(0.6)
        ready = True
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {}}), flush=True)
    elif "id" in msg:
        result = {"content": [{"type": "text", "text": "ready" if ready else "TOO EARLY"}]}
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
'''


class TestStdioStart(unittest.TestCase):

    def test_second_caller_waits_for_the_handshake(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "child.py")
            with open(script, "w") as f:
                f.write(_SLOW_CHILD)
            server = {"name": "slow", "transport": "stdio",
                      "command": f'"{sys.executable}" "{script}"'}
            self.addCleanup(mcp_client.release, server)
            results = []

            def call():
                results.append(str(mcp_client.call_tool(server, "x", {})))

            first = threading.Thread(target=call)
            first.start()
            time.sleep(0.2)                  # the child is up, its handshake isn't
            second = threading.Thread(target=call)
            second.start()
            first.join(15)
            second.join(15)
            self.assertEqual(results, ["ready", "ready"])


if __name__ == "__main__":
    unittest.main()
