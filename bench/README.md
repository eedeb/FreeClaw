# Benchmark measuring proxy

Neither FreeClaw nor any other agent UI shows real token usage — the `~N tokens`
in FreeClaw's header is a client-side estimate (roughly chars ÷ 4), and nothing
in `src/agent.py` reads the `usage` field off a provider response. This proxy
gets the real numbers, from the provider itself, identically for every agent
under test.

```
FreeClaw  ─┐
           ├─►  proxy (localhost:8900)  ─►  real provider
OpenClaw  ─┘            │
                        └─► bench_log.csv
```

## Run it

```bash
UPSTREAM=https://api.openai.com/v1 python3 bench/proxy.py
```

Stdlib only — no venv, no dependencies. Set `UPSTREAM` to whichever provider
you're testing against and `PORT` if 8900 is taken.

## Point the agents at it

**FreeClaw** — Settings → Providers, change the URL to the proxy. Keep the API
key exactly as it is; the proxy forwards your `Authorization` header upstream
and never logs it.

| Install | Provider URL |
|---|---|
| Native (Linux) | `http://localhost:8900/v1` |
| Docker (macOS) | `http://host.docker.internal:8900/v1` |

The Docker row matters: inside the container `localhost` is the container, not
your Mac, so the native URL fails with a connection error. The proxy binds
`0.0.0.0` so the container can reach it.

**OpenClaw** — set its base URL to the same address.

## Per trial

Label the trial, run the task in the agent, then read the row:

```bash
curl -s localhost:8900/trial -d 'T3 FreeClaw run1'
```

```bash
curl -s localhost:8900/summary
```

```
trial                          input  cached  output  trips  latency
T3 FreeClaw run1                1700     900      85      2      6.4
T3 OpenClaw run1                4820       0     140      4     11.2
```

Those columns map straight onto the spreadsheet: **input** → Input tokens,
**cached** → Cached input tokens, **output** → Output tokens, **trips** →
Round trips, **latency** → Latency (s).

`/summary` covers every trial since the proxy started, so leave it running for
a whole session and read it once at the end. `bench_log.csv` keeps the raw
per-request rows for anything you want to dig into afterwards — it's gitignored,
since it's your data rather than the repo's.

## Running both agents at once

The trial label is a single global, so one proxy assumes **one trial at a
time**: label it, run that task in that agent, read the row, label the next.
Run both agents against one proxy simultaneously and their requests interleave
under whichever label happens to be set — the totals silently become nonsense.

Sequential is the right methodology anyway (web results move, and you want one
variable at a time). If you do want them side by side, run one proxy per agent:

```bash
PORT=8900 LOG_PATH=bench/freeclaw.csv UPSTREAM=https://api.openai.com/v1 python3 bench/proxy.py
```

```bash
PORT=8901 LOG_PATH=bench/openclaw.csv UPSTREAM=https://api.openai.com/v1 python3 bench/proxy.py
```

Each keeps its own trial label and its own log, and you read `/summary` from
each port separately.

## Reading the numbers

**Round trips are the headline on the multi-tool categories.** One task is
several HTTP requests — model, tool call, model again, tool call again, final
answer — and each one re-sends the entire context. Four round trips at 5k
tokens is 20k input tokens for a single question. This is the number that
explains the others.

**Reset the conversation between trials** (`/reset` in FreeClaw). Otherwise
turn 2 of trial 5 carries turn 1 of trial 4, and the input counts drift upward
for reasons that have nothing to do with the tool being measured.

**`cached` will be 0 on providers without prompt caching**, which is accurate
rather than missing. Where it's non-zero, report it separately — cached input
is usually billed at a large discount, so folding it into the input total
overstates the difference between the two tools.

Latency includes a local proxy hop, which is sub-millisecond next to any real
API call, but it's a shared overhead both tools pay equally either way.
