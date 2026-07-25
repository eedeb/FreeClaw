"""Measuring proxy for the FreeClaw vs OpenClaw token benchmark.

Sits between an agent and its OpenAI-compatible provider, forwards every
request untouched, and records what the provider says it actually cost. Both
agents point at this instead of the real endpoint, so both get measured by the
same instrument — which is the whole point. Neither app needs a code change.

    agent  ->  this proxy  ->  real provider
                    |
                    +-- bench_log.csv (one row per request)

What it captures per request, straight from the provider's own `usage`:

    prompt_tokens                          -> "Input tokens"
    prompt_tokens_details.cached_tokens    -> "Cached input tokens"
    completion_tokens                      -> "Output tokens"
    one row                                -> one round trip
    wall time                              -> "Latency (s)"

Round trips are the reason this has to be a proxy rather than a patch: one
"task" is several HTTP requests (model -> tool call -> model -> tool call ->
final answer), each re-sending the whole context. Counting them is most of
the story on the multi-tool categories.

Usage:

    UPSTREAM=https://api.openai.com/v1 python3 bench/proxy.py

Point both agents at http://localhost:8900/v1 with their normal API key. The
key is forwarded from the incoming Authorization header and is never logged.

Then, around each trial:

    curl -s localhost:8900/trial -d 'T3 FreeClaw run1'   # before
    curl -s localhost:8900/summary                       # after -> paste row
"""

import csv
import http.server
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

UPSTREAM = os.getenv("UPSTREAM", "https://api.openai.com/v1").rstrip("/")
PORT = int(os.getenv("PORT", "8900"))
_HERE = os.path.dirname(os.path.abspath(__file__))
# Overridable so two instances (one per agent) can run side by side without
# fighting over the same file — see "Running both agents at once" in README.md.
LOG_PATH = os.getenv("LOG_PATH") or os.path.join(_HERE, "bench_log.csv")

LOG_FIELDS = [
    "timestamp", "trial", "round_trip", "model", "stream",
    "input_tokens", "cached_input_tokens", "output_tokens",
    "latency_s", "status",
]

_lock = threading.Lock()
_trial = "unlabelled"
_records = []  # every request this process has seen


def _log(rec):
    """Append one request to the CSV and keep it in memory for /summary."""
    with _lock:
        _records.append(rec)
        new = not os.path.exists(LOG_PATH)
        with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if new:
                w.writeheader()
            w.writerow(rec)


def _usage_fields(usage):
    """Pull the three token counts out of a provider `usage` object.

    cached_tokens lives in prompt_tokens_details on OpenAI-compatible APIs and
    is simply absent on providers that don't do prompt caching — absent is
    recorded as 0, which is the truth for those providers.
    """
    if not usage:
        return 0, 0, 0
    details = usage.get("prompt_tokens_details") or {}
    return (
        usage.get("prompt_tokens", 0),
        details.get("cached_tokens", 0),
        usage.get("completion_tokens", 0),
    )


class Handler(http.server.BaseHTTPRequestHandler):
    # HTTP/1.0 so the connection close frames the streaming body; adding
    # chunked framing by hand would buy nothing here.
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    # ── control endpoints ────────────────────────────────────────

    def _text(self, body, code=200):
        payload = body.encode()
        self.send_response(code)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _start_trial(self):
        global _trial
        n = int(self.headers.get("content-length", 0))
        label = self.rfile.read(n).decode().strip() if n else ""
        with _lock:
            _trial = label or "unlabelled"
        self._text(f"trial: {_trial}\n")

    def _summary(self):
        """Aggregate per trial, in the column order the sheet expects."""
        with _lock:
            recs = list(_records)
        by_trial = {}
        for r in recs:
            t = by_trial.setdefault(r["trial"], {
                "round_trips": 0, "input": 0, "cached": 0, "output": 0, "latency": 0.0,
            })
            t["round_trips"] += 1
            t["input"] += r["input_tokens"]
            t["cached"] += r["cached_input_tokens"]
            t["output"] += r["output_tokens"]
            t["latency"] += r["latency_s"]

        lines = [f"{'trial':<28}{'input':>8}{'cached':>8}{'output':>8}{'trips':>7}{'latency':>9}"]
        for name, t in by_trial.items():
            lines.append(
                f"{name:<28}{t['input']:>8}{t['cached']:>8}{t['output']:>8}"
                f"{t['round_trips']:>7}{t['latency']:>9.1f}"
            )
        return self._text("\n".join(lines) + "\n")

    def do_GET(self):
        if self.path.startswith("/summary"):
            return self._summary()
        if self.path.startswith("/v1/models"):
            return self._forward("/models", None)
        self._text("not found\n", 404)

    def do_POST(self):
        if self.path.startswith("/trial"):
            return self._start_trial()
        if self.path.startswith("/v1/chat/completions"):
            n = int(self.headers.get("content-length", 0))
            return self._forward("/chat/completions", self.rfile.read(n))
        self._text("not found\n", 404)

    # ── the actual proxying ──────────────────────────────────────

    def _forward(self, path, body):
        streaming = False

        if body:
            try:
                data = json.loads(body)
                streaming = bool(data.get("stream"))
                if streaming:
                    # Streaming responses omit `usage` unless it's asked for.
                    # FreeClaw skips chunks with no choices (agent.py:1151) and
                    # the openai SDK ignores them too, so this is invisible to
                    # the agent — it only adds a final usage-only chunk.
                    opts = data.get("stream_options") or {}
                    opts["include_usage"] = True
                    data["stream_options"] = opts
                    body = json.dumps(data).encode()
            except (ValueError, TypeError):
                pass  # not JSON we understand; pass it through untouched

        req = urllib.request.Request(UPSTREAM + path, data=body,
                                     method="POST" if body is not None else "GET")
        # The caller's real API key rides along to the provider and is never
        # written to the log.
        for h in ("authorization", "content-type"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])

        model, usage, status = "?", None, 0
        t0 = time.monotonic()
        try:
            resp = urllib.request.urlopen(req, timeout=600)
            status = resp.status
            ctype = resp.headers.get("content-type", "application/json")

            self.send_response(status)
            self.send_header("content-type", ctype)
            self.end_headers()

            if streaming:
                # Relay every SSE line the instant it arrives so the agent's
                # own streaming UI is unaffected, watching for the usage chunk.
                for raw in resp:
                    self.wfile.write(raw)
                    self.wfile.flush()
                    if raw.startswith(b"data: "):
                        chunk = raw[6:].strip()
                        if chunk and chunk != b"[DONE]":
                            try:
                                obj = json.loads(chunk)
                            except ValueError:
                                continue
                            model = obj.get("model") or model
                            if obj.get("usage"):
                                usage = obj["usage"]
            else:
                payload = resp.read()
                self.wfile.write(payload)
                try:
                    obj = json.loads(payload)
                    model = obj.get("model") or model
                    usage = obj.get("usage")
                except ValueError:
                    pass

        except urllib.error.HTTPError as e:
            # Relay the provider's own error so the agent sees the real reason
            # (rate limit, bad key) instead of a confusing proxy failure.
            status = e.code
            payload = e.read()
            self.send_response(status)
            self.send_header("content-type", e.headers.get("content-type", "application/json"))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:
            status = 502
            self._text(f"proxy error: {e}\n", 502)

        latency = time.monotonic() - t0
        inp, cached, out = _usage_fields(usage)
        with _lock:
            trip = sum(1 for r in _records if r["trial"] == _trial) + 1
            trial = _trial
        _log({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "trial": trial,
            "round_trip": trip,
            "model": model,
            "stream": streaming,
            "input_tokens": inp,
            "cached_input_tokens": cached,
            "output_tokens": out,
            "latency_s": round(latency, 3),
            "status": status,
        })
        print(f"[{trial}] trip {trip}  in={inp} cached={cached} out={out} "
              f"{latency:.1f}s status={status}", flush=True)


if __name__ == "__main__":
    # flush=True so these still appear immediately when stdout is redirected
    # to a log file, which block-buffers by default.
    print(f"measuring proxy on http://localhost:{PORT}/v1  ->  {UPSTREAM}", flush=True)
    print(f"logging to {LOG_PATH}", flush=True)
    try:
        http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
