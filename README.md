# 🦅 FreeClaw

> **An AI agent that doesn't burn your money.**

<p align="center">
  <img src="https://freeclaw.eedeb.dev/demo.gif" alt="FreeClaw answering a question by searching the web, in its dark-themed chat UI" width="720">
</p>

FreeClaw is a cost-efficient, tool-using AI agent that runs on your own machine. It comes with a password-protected, dark-themed web UI you can chat with from any browser on your network. It remembers things about you, searches the web, runs bash commands, connects to external tools through MCP servers, reads images, and reads/writes files — and does it all while routing as much traffic as possible to small, cheap models.

---

## Installation

### Linux

Runs natively, supervised by systemd. Needs `git`, `python3`, and `sudo`.

```bash
curl -fsSL https://freeclaw.eedeb.dev/install.sh | bash
```

The script will:
1. Clone the repo and set up a Python virtual environment with all dependencies
2. Ask you to set a **password** for the web UI (no API keys collected here)
3. Register FreeClaw as a systemd service (`FreeClaw.service`) so it starts automatically, and install the `freeclaw` terminal client
4. Point you to the web UI, where **Settings → Providers** is where you add your AI provider(s) — FreeClaw can't answer until at least one is configured
5. Print the local URL to open in your browser

### macOS

macOS has no systemd, so FreeClaw runs in a container instead. Needs `git` and [Docker Desktop](https://www.docker.com/products/docker-desktop/) (running).

```bash
curl -fsSL https://freeclaw.eedeb.dev/install-mac.sh | bash
```

Same flow — clone, set a password, start — but step 1 builds a Docker image rather than a virtualenv, and step 3 runs the container with `restart: unless-stopped` in place of a systemd unit. The `freeclaw` CLI is installed as a wrapper around `docker compose exec`. Once it's up, open **http://localhost:6767**.

The first build downloads PyTorch and takes a few minutes; the resulting image is on the order of a gigabyte. Later builds are cached.

> **Apple Silicon:** if Docker Desktop fails to start with `Failed to install Rosetta`, choose **Disable Rosetta** in that dialog. Rosetta is only needed to run x86/amd64 images; FreeClaw builds natively for arm64, so turning it off costs you nothing.

Your chats, uploads, `context.md`, logs, and `.env` are bind-mounted from the install directory, so they survive rebuilds and updates.

One difference from the Linux install: the OpenAI-compatible API's on/off state is stored inside the container, so it resets to **off** after `./update-mac.sh` recreates it. Turn it back on with `/startapi` or the API chip on the homepage.

> **Note:** each installer checks out only the files for its own platform — a Linux install has no `docker/` directory or `*-mac.sh` scripts, and a macOS install has no systemd scripts. Run the wrong one and it will tell you and point at the other.

---

## Using FreeClaw

Once installed, open the URL printed by the installer — something like `http://192.168.x.x:6767`. You'll be asked for the password you set during install, then dropped into the FreeClaw chat UI:

- Type a message and press **Enter** to send (Shift+Enter for a newline)
- Agent responses are rendered with full markdown — code blocks, lists, bold, links, etc.
- A live **token count** is shown in the top right so you can keep an eye on usage — the provider's own exact figure where available, hover it for the breakdown (see [Token Counts](#token-counts))
- Use the **attach button** to upload a file — FreeClaw can read it back, including describing images in detail
- Hit **Reset** to clear the conversation and start fresh

### Chat commands

You can type these directly into the chat box:

| Command | What it does |
|---|---|
| `/reset` | Clears the conversation history |
| `/startapi` | Enables the OpenAI-compatible API at `/v1/chat/completions` |
| `/stopapi` | Disables the API |

---

## Features

- **Smart intent classification** — a local `Classy` classifier reads your message and tags its intent (greeting, search, coding, logic, banter, etc.) before any API call is made
- **Adaptive turns** — the intent tag decides how much chat history is sent, the sampling temperature, and which tools are offered: small talk gets a tiny context window and no tools, precision work runs colder with the full toolset
- **Minimal context windowing** — the number of past messages sent per turn scales with how complex the intent tag is, keeping token usage low for simple exchanges
- **Multi-provider fallback** — add any OpenAI-compatible endpoint from Settings → Providers (URL, API key, model); the agent tries them in the order you list them, falling through to the next if one fails or is rate-limited
- **Persistent memory, paged in** — the agent keeps durable facts about you in `context.md`, filed under `##` headers alongside your other files. Its prompt carries only the **About** section plus the *names* of the others, so memory can grow for years without the prompt growing with it; it pulls a section in with `search_context` when the conversation calls for one, and saves with `add_context`
- **Web search & scraping** — queries DuckDuckGo for instant answers, news, and snippets, then scrapes and cleans the top non-JS-heavy result pages, all stitched into one capped, structured block of context for the model — no extra LLM call required
- **Bash execution, gated on your approval** — can run shell commands on the host machine, but only ones you've okayed. The prompt is raised by FreeClaw itself, not by the model: see [Bash Approvals](#bash-approvals)
- **File, page & image tools** — can create, read, edit (find/replace), delete, and list files in its sandboxed static folder; can publish a live HTML page at a public URL; can describe an uploaded image in detail using a vision model
- **MCP servers, remote or local** — connect external [Model Context Protocol](https://modelcontextprotocol.io) servers from **Settings → MCP Servers**, over HTTP *or* as a local process on stdio (which is how most published MCP servers ship). Their tools are merged into the agent's toolset automatically, no restart required
- **Prompt caching** — the system prompt is laid out stable-part-first so providers can cache it, cutting the cost of the repeated prefix every turn resends. See [Prompt Caching](#prompt-caching)
- **Password-protected UI** — the web chat sits behind a login screen so it's safe to expose on your local network
- **OpenAI-compatible API** — toggle `/v1/chat/completions` on the same port for use from other apps and scripts, authenticated with your FreeClaw password

---

## How It Works

```
Browser (chat UI, behind /login)
    │  POST /chat
    ▼
Flask server (Flask/main.py, port 6767)
    │
    ▼
Classy.classify()       ← local intent classifier using models/data.pth
    │                      picks temperature, tools + how much history to send
    ▼
Configured provider API call ← trimmed message history + tools
    │  (falls back to the next provider in Settings → Providers on failure)
    │
    ├── Tool call? ───► bash? ──► approval gate (src/approvals.py)
    │                   │           │  saved rule → run · else ask the user and wait
    │                   │           │  refused/timed out → never runs
    │                   └──► Execute tool (search, file ops, MCP servers, vision…)
    │                           │
    │                           └──► Recursive agent turn with the tool result
    │
    └── Text response? ──► Streamed back to the browser as server-sent events
```

The search pipeline (`src/scraper.py`):
1. DuckDuckGo (via `ddgs`) supplies instant answers, news results (for news-flavored queries), and web snippets
2. Time-sensitive queries (weather, prices, scores, etc.) have stale results filtered out by date
3. The top few non-JS-heavy result pages are scraped directly with BeautifulSoup and cleaned of nav/ad/boilerplate noise
4. Everything is combined into one structured, character-capped block and handed straight to the agent as a tool result — there's no separate summarization call

---

## Project Structure

```
FreeClaw/
├── Flask/
│   ├── main.py               # Flask server — login, chat SSE endpoint, settings/provider/MCP APIs, /v1 API
│   ├── static/               # Created at first run; each user gets static/<user>/files/ holding context.md, uploads, and agent-created files,
│   │                         # plus static/<user>/.bash_approvals.json — their always-allow rules, kept out of files/ so the agent can't edit it
│   └── templates/
│       ├── index.html        # Home page — pick a user, toggle the API
│       ├── chat.html         # Chat UI (dark theme, markdown rendering, token counter, file upload)
│       ├── settings.html     # Settings — providers, MCP servers, .env, restart
│       └── login.html        # Password login screen
├── src/
│   ├── agent.py              # Core agent loop — intent classification, provider fallback, tool dispatch
│   ├── approvals.py          # Bash approval gate — per-user allow rules, blocking prompts (no LLM involvement)
│   ├── cli.py                # Terminal chat client (the `freeclaw` command)
│   ├── users.py              # User/conversation storage, shared by the web app and CLI
│   ├── scraper.py            # DuckDuckGo search + page scraping + text cleaning
│   ├── mcp_client.py         # MCP client — external MCP servers over HTTP or local stdio processes
│   └── logging_setup.py      # Central logger — full tracebacks go to logs/freeclaw.log
├── models/
│   └── data.pth              # Classy intent classifier weights
├── logs/
│   └── freeclaw.log          # Created at first run; full error detail, see Debugging below
├── docker/                   # macOS install only
│   ├── Dockerfile            # CPU-only PyTorch + the agent, mirroring what install.sh does natively
│   └── docker-compose.yml    # Port 6767, restart policy, bind mounts for .env / static / logs
├── install.sh                # One-line installer      (Linux)
├── update.sh                 # Pull and apply updates  (Linux)
├── uninstall.sh              # Remove service + files  (Linux)
├── install-mac.sh            # One-line installer      (macOS, Docker)
├── update-mac.sh             # Pull, rebuild, restart  (macOS, Docker)
├── uninstall-mac.sh          # Remove container + files (macOS, Docker)
├── requirements.txt          # Python dependencies (web/agent libs)
└── .env                      # Password, providers, MCP servers, and other config (created during install)
```

Only one platform's scripts are checked out at install time, so you'll see either the Linux set or the macOS set — not both.

---

## MCP Servers

FreeClaw can connect to external [Model Context Protocol](https://modelcontextprotocol.io) (MCP) servers to gain new tools — think GitHub, web search, databases, or your own custom server. Add one from **Settings → MCP Servers**, in either of two flavours.

### Remote — HTTP

Enter the server's URL and (optionally) an auth token. FreeClaw connects over the Streamable HTTP transport, fetches the server's tools, and makes them available immediately — no restart required.

### Local — stdio

Enter a command instead. FreeClaw runs it as a child process and speaks JSON-RPC over its stdin/stdout. This is the transport most published MCP servers actually use, so it's what makes the wider ecosystem reachable:

```
npx -y @modelcontextprotocol/server-filesystem /srv/shared
```

Things worth knowing:

- **The runtime has to exist where FreeClaw runs.** `npx` needs Node on that machine — the host on a Linux install, but **inside the container** on the macOS/Docker one, whose image ships Python only. So `npx`-based servers need `nodejs`/`npm` added to [`docker/Dockerfile`](docker/Dockerfile) and a rebuild. FreeClaw says exactly this rather than failing vaguely when the binary is missing.
- **Secrets go in `.env`.** The child inherits FreeClaw's environment, so a server wanting `GITHUB_TOKEN` gets it by adding that key under **Settings → Environment**. There's no separate credential field for stdio servers.
- **Paths with spaces need double quotes** (`… "/My Files/notes"`). Single quotes would break the `.env` encoding and are rejected.
- **One process per server**, started on first use and kept alive, shut down when you disable or remove the server, and respawned automatically if it dies mid-session.

Either kind can be toggled off without losing its saved config. Connections live in your `.env` as the parallel `MCP_NAMES`, `MCP_URLS`, `MCP_TOKENS`, `MCP_ENABLED`, `MCP_TRANSPORTS`, and `MCP_COMMANDS` lists, so you can review or edit them by hand. An entry with no transport recorded is treated as HTTP, so configs written by older versions keep working untouched.

---

## Bash Approvals

The agent can run shell commands — but not on its own initiative. Every `run_bash_command` call goes through a gate outside the conversation entirely:

- **The model is never asked and can't answer.** It can't request permission, can't grant itself any, and can't talk its way past the gate. Its tool description tells it not to ask you either; approval isn't its business.
- **You see the exact command** in the chat (or terminal) with **allow once**, **always allow**, and **deny**. The agent's turn is genuinely paused until you answer.
- **Nothing runs on a non-answer.** Ignoring the prompt, closing the tab, or Ctrl-C at the CLI all count as refusals, as does the five-minute timeout.

### Always-allow rules

"Always allow" saves a rule so the same thing isn't asked twice, **per FreeClaw user** — approving something as Elliot grants nothing to anyone else. An **exact** rule matches the command byte for byte; a **program** rule (*always allow all "ls" commands*) matches any simple command with that leading token.

The program option is only offered for a *simple* command: anything containing `;`, `&&`, `|`, `` ` ``, `$(…)`, `>` or `<` can neither create such a rule nor be matched by one, since `ls; rm -rf ~` would otherwise satisfy a rule that says `ls`.

Review and revoke under **Settings → Bash Approvals**. There's no field to type a rule in by hand, on purpose: a rule can only come from a command you were shown.

### Background runs, and what this isn't

A ping firing at 3am has nobody to ask. Those turns still honour saved rules — that's what always-allow is for — but anything needing a prompt is refused rather than left hanging.

The gate stops commands running unasked. It is **not a sandbox**: an approved command has whatever access the FreeClaw process does, including rewriting the rule file, so approving one arbitrary command is in practice approving all of them. Running FreeClaw as a user that can't touch anything you care about is still the real containment story. Rules live at `Flask/static/<user>/.bash_approvals.json`, outside the `files/` folder the agent's own file tools can reach.

---

## Prompt Caching

Every turn resends the system prompt, so it's the single biggest repeated cost in a conversation. Providers will serve a repeated *prefix* from cache at a large discount — but only if it's byte-identical each time.

FreeClaw's used not to be. The system message opened with a live timestamp, which meant every turn differed from byte 0 and **nothing could ever be cached**. The layout is now stable-part-first:

```
<instructions>                          ← fixed for the whole conversation, cacheable
context.md: About section + header names ← snapshotted once, at reset
--- live context (refreshed every turn) ---
Current date: …                         ← rewritten every turn
```

That alone is enough for providers that cache automatically (OpenAI, DeepSeek, Groq, Cerebras, xAI) — they need no request-side opt-in, just a stable prefix.

Anthropic, Gemini and Qwen models instead need an explicit breakpoint, so FreeClaw marks the boundary above with `cache_control: ephemeral` when the model id looks like one of those. There's nothing to configure: the models that need it are the ones whose names say so, and an endpoint that objects is handled by the same retry that covers token counts (see below). A provider behind an opaque model id just misses out.

Whether it's working is visible in the token counts below — cache reads are reported alongside them, for any provider that reports usage at all.

---

## Token Counts

FreeClaw asks every provider for the exact token usage of each request and shows you what comes back. Where a provider reports nothing, it falls back to a length-based estimate — and says which you're looking at:

| Display | Meaning |
|---|---|
| `2,041 tokens` (accent colour) | Exact, from the provider |
| `~362 tokens` (grey, leading `~`) | Estimated from message length |

Hover the counter for the breakdown: tokens sent, tokens received, how many came from cache, and how many requests the turn took. The CLI prints the same on its per-turn summary line:

```
2 requests · 1.4s · groq · 3,224 in / 118 out (1,024 cached)
```

**The headline number is the last request's prompt size** — what the model actually read, which is deliberately *not* the size of your whole conversation: history windowing sends only a slice. Counts are written to `logs/freeclaw.log` and ride along with the saved conversation, so they survive a reload.

Expect the exact number to disagree with the estimate: the estimate walks the saved conversation, so it counts the **whole history** rather than the windowed slice, and it can't see the **tool definitions**, which aren't part of the conversation but are resent with every request.

### How it gets the numbers

A streamed response carries no usage block unless the request asks for it, via `stream_options: {"include_usage": true}` — and not every OpenAI-compatible endpoint accepts that field. Same story for the `cache_control` breakpoint above. Since this is a fallback chain where one rejected request would cost you a provider, both are sent optimistically and share one safety net: if a provider `400`s with them attached, FreeClaw retries the identical call without them and stops sending them to that provider. An endpoint that supports neither costs one wasted request, ever — nothing to configure, and no working call lost to a field you didn't know about. The verdict resets when you edit the provider list, so reusing a name for a different endpoint doesn't inherit the old one's quirk.

---

## OpenAI-Compatible API (Optional)

FreeClaw can expose an OpenAI-compatible API on the same port as the web UI, so anything that speaks the OpenAI chat format can use your provider chain. Toggle it with the **API** chip on the homepage, or with `/startapi` / `/stopapi` in chat. Authenticate with your FreeClaw password as the Bearer token:

```bash
curl http://localhost:6767/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_FC_PASSWORD" \
  -d '{"model": "openai/gpt-oss-120b", "messages": [{"role": "user", "content": "Hello!"}]}'
```

`GET /v1/models` and streaming (`"stream": true`) are supported. Requests are stateless — they go straight to your configured providers in the same fallback order as the chat UI, without touching any user's conversation.

---

## Configuration

Settings live in a `.env` file in the project root, created for you during install:

| Variable | Required | Purpose |
|---|---|---|
| `FC_PASSWORD` | Yes | Password for the web UI login screen |
| `SECRET_KEY` | Yes | Flask session secret (auto-generated by the installer) |
| `PROVIDER_NAMES` / `PROVIDER_URLS` / `PROVIDER_KEYS` / `PROVIDER_MODELS` / `PROVIDER_ENABLED` | Yes | Your LLM provider(s) — managed entirely from **Settings → Providers**; the agent has nothing to call until at least one exists here |
| `VISION_PROVIDER` | No | Name of the configured provider (from **Settings → Providers**) used to describe uploaded images — pick it in **Settings → Vision Model** |
| `MCP_NAMES` / `MCP_URLS` / `MCP_TOKENS` / `MCP_ENABLED` / `MCP_TRANSPORTS` / `MCP_COMMANDS` | No | Connected MCP servers — managed from **Settings → MCP Servers**. `MCP_TRANSPORTS` is `http` or `stdio` per entry (missing = `http`); `MCP_COMMANDS` holds the command line for stdio ones |
| `CUSTOM_DOMAIN` | No | Overrides the auto-detected local IP for file/page links the agent returns. Set to `http://localhost:6767` by the macOS installer, since a container can't see the host's LAN address |
| `FC_DEBUG` | No | `0` turns off Werkzeug's reloader and interactive debugger. Defaults to on for native installs; the Docker image sets it to `0` |
| `FC_TELEMETRY` | No | `1` sends the one-off anonymous install ping described under [Telemetry](#telemetry). Off unless you opted in during install |
| `FC_INSTALL_ID` | No | Random UUID written here after that ping is sent, so it's only ever sent once. Delete the line to reset |

---

## Telemetry

**Off by default.** The installer asks once, the default answer is no, and if you say no nothing is ever sent.

If you say yes, FreeClaw sends **one** HTTP request, the first time it starts, containing exactly four fields:

```json
{
  "install_id": "f81d4fae-7dec-11d0-a765-00a0c91e6bf6",
  "version": "0.1.0",
  "os": "darwin",
  "install_method": "docker"
}
```

That's the entire payload. Not sent, ever: your messages, prompts, provider names, URLs, API keys, file contents, file paths, hostname, or username. The receiving end ([`telemetry/`](telemetry/)) stores those four fields and a timestamp — its schema has no column for anything else — and does not log IP addresses.

It exists to answer one question: how many people actually installed this. That's it.

- **Sent once**, not on a schedule. After a successful send the `install_id` is written to your `.env` as `FC_INSTALL_ID` and never sent again.
- **Turn it off** any time in **Settings → Anonymous Install Ping**, or set `FC_TELEMETRY=0` in `.env`.
- **Turn it on** later the same way, if you didn't at install time.
- **Read the code** — it's ~100 lines in [`src/telemetry.py`](src/telemetry.py), and the log line it writes shows you the exact payload it sent.

If the endpoint is unreachable the failure is swallowed silently and start-up is unaffected — the ping runs on a background thread with a 5-second timeout, and no `install_id` is saved, so it simply tries again next time.

---

## Updating

From your FreeClaw install directory — on Linux:

```bash
./update.sh
```

On macOS:

```bash
./update-mac.sh
```

Both pull the latest `src/`, `Flask/templates/`, and `Flask/main.py` from `origin/main` and leave your `Flask/static/` data (context, uploads, generated pages) untouched. The Linux script syncs the virtualenv and restarts the systemd service; the macOS one rebuilds the image (the source is baked in at build time, so a rebuild is what makes new code take effect) and restarts the container.

---

## Debugging

Every unexpected failure — a provider erroring out, a tool crashing, an MCP server going unreachable, an unhandled exception in a route — gets logged with its full traceback to `logs/freeclaw.log` at the repo root, rotated at 5MB (5 backups kept). This is separate from what you see in the chat UI or API response, which stays short on purpose; the log file is where the real cause lives.

```bash
tail -f logs/freeclaw.log
```

Warnings and errors are also mirrored to the console. On Linux that means `journalctl -u FreeClaw.service -f`; on macOS, `docker compose -f docker/docker-compose.yml logs -f`. Either way `logs/freeclaw.log` holds the same detail — on macOS the directory is bind-mounted out of the container, so you can tail it from the host exactly as above.

`logs/` is never served by the app (unlike `Flask/static/`), so it's safe to keep tracebacks there even though they can include file paths and request shapes.

---

## Cost Philosophy

FreeClaw is built around one principle: **use the cheapest model that can do the job.**

- Greetings, small talk, and personal questions → no tools, minimal context
- Search, coding, logic, and everything else → tools included, context trimmed to a handful of recent messages
- Long-term facts → saved once to `context.md` under a header, and only that header's *name* is re-sent each turn until the agent actually needs what's under it
- A free, no-LLM scraping pipeline does the heavy lifting for search instead of spending a model call on it
- The part of the prompt that never changes sits where a provider can cache it, so the repeated prefix is discounted instead of paid for in full every turn ([Prompt Caching](#prompt-caching))

This keeps API costs near zero for everyday use.

---

## License

MIT — do whatever you want with it.