# 🦅 FreeClaw

> **An AI agent that doesn't burn your money.**

FreeClaw is a cost-efficient, tool-using AI agent that runs on your own machine. It comes with a password-protected, dark-themed web UI you can chat with from any browser on your network. It remembers things about you, searches the web, runs bash commands, connects to external tools through MCP servers, reads images, and reads/writes files — and does it all while routing as much traffic as possible to small, cheap models.

---

## Installation

Install FreeClaw with a single command:

```bash
curl -fsSL https://freeclaw.eedeb.dev/install.sh | bash
```

The script will:
1. Clone the repo and set up a Python virtual environment with all dependencies
2. Ask for your **Google AI API key** (free at [aistudio.google.com](https://aistudio.google.com))
3. Optionally ask for a **Cerebras API key** (fallback provider) and an **NVIDIA NIM API key** (used only for describing uploaded images)
4. Ask you to set a **password** for the web UI
5. Register FreeClaw as a systemd service (`FreeClaw.service`) so it starts automatically, plus a disabled-by-default API service (`FreeClawAPI.service`)
6. Point you to the web UI, where you can connect **MCP servers** for extra tools
7. Print the local URL to open in your browser

---

## Using FreeClaw

Once installed, open the URL printed by the installer — something like `http://192.168.x.x:6767`. You'll be asked for the password you set during install, then dropped into the FreeClaw chat UI:

- Type a message and press **Enter** to send (Shift+Enter for a newline)
- Agent responses are rendered with full markdown — code blocks, lists, bold, links, etc.
- A live **token estimate** is shown in the top right so you can keep an eye on usage
- Use the **attach button** to upload a file — FreeClaw can read it back, including describing images in detail
- Hit **Reset** to clear the conversation and start fresh

### Chat commands

You can type these directly into the chat box:

| Command | What it does |
|---|---|
| `/reset` | Clears the conversation history |
| `/startapi` | Starts the FreeClaw REST API on port 8080 |
| `/stopapi` | Stops the REST API |

---

## Features

- **Smart intent classification** — a local `Classy` classifier reads your message and tags its intent (greeting, search, coding, logic, banter, etc.) before any API call is made
- **Adaptive model routing** — small talk, greetings, and personal questions are handled by `openai/gpt-oss-20b`; everything else (search, coding, logic, file work) uses the larger `openai/gpt-oss-120b`
- **Minimal context windowing** — the number of past messages sent per turn scales with how complex the intent tag is, keeping token usage low for simple exchanges
- **Multi-provider fallback** — runs on Google (Gemini) by default; if a call fails, it automatically retries on Cerebras if you've configured a key. Add your own OpenAI-compatible endpoints from Settings → Providers to fully control the chain.
- **Persistent memory** — the agent keeps durable facts about you in `context.md`, stored alongside your other files and read/updated with the same file tools it uses for everything else, without that history bloating the active context window
- **Web search & scraping** — queries DuckDuckGo for instant answers, news, and snippets, then scrapes and cleans the top non-JS-heavy result pages, all stitched into one capped, structured block of context for the model — no extra LLM call required
- **Bash execution** — can run shell commands on the host machine and return the output
- **File, page & image tools** — can create, read, edit (find/replace), delete, and list files in its sandboxed static folder; can publish a live HTML page at a public URL; can describe an uploaded image in detail using a vision model
- **MCP servers** — connect external [Model Context Protocol](https://modelcontextprotocol.io) servers straight from the web UI (a **+ MCP** button opens a sidebar for the server URL and token); their tools are fetched over the Streamable HTTP transport and merged into the agent's toolset automatically, no restart required
- **Password-protected UI** — the web chat sits behind a login screen so it's safe to expose on your local network
- **TTS-aware mode** — optional response formatting tuned for text-to-speech output (used automatically by the REST API)

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
    │                      picks the model + how much chat history to send
    ▼
Google (Gemini) API call ← trimmed message history + tools
    │  (falls back to the next configured provider on failure)
    │
    ├── Tool call? ───► Execute tool (search, bash, file ops, MCP servers, vision…)
    │                       │
    │                       └──► Recursive agent() call with tool result
    │
    └── Text response? ──► Return to Flask → back to browser as JSON
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
│   ├── main.py               # Flask server — login, chat endpoint, file upload, command handling
│   ├── static/                # Created at install; each user gets static/<user>/files/ holding context.md, uploads, and agent-created files
│   └── templates/
│       ├── index.html         # Chat UI (dark theme, markdown rendering, token counter, file upload)
│       ├── login.html         # Password login screen
│       └── agent/             # HTML pages created by the agent's create_page tool
├── src/
│   ├── agent.py               # Core agent loop — intent classification, model routing, tool dispatch
│   ├── scraper.py             # DuckDuckGo search + page scraping + text cleaning
│   ├── api.py                 # Optional REST API (FastAPI/uvicorn, port 8080)
│   ├── mcp_client.py          # MCP client — connects to external MCP servers over HTTP
│   └── logging_setup.py       # Central logger — full tracebacks go to logs/freeclaw.log
├── models/
│   └── data.pth                # Classy intent classifier weights
├── logs/
│   └── freeclaw.log            # Created at first run; full error detail, see Debugging below
├── install.sh                  # One-line installer
├── update.sh                   # Pulls and applies the latest changes from GitHub
└── .env                         # API keys, password, MCP servers, and other config (created during install)
```

---

## MCP Servers

FreeClaw can connect to external [Model Context Protocol](https://modelcontextprotocol.io) (MCP) servers to gain new tools — think GitHub, web search, databases, or your own custom server.

Add one from the chat UI: click the **+ MCP** button in the header to open the servers sidebar, then enter the server's URL and (optionally) an auth token. FreeClaw connects over the Streamable HTTP transport, fetches the server's tools, and makes them available to the agent immediately — no restart required.

Connections are stored in your `.env` file as the parallel `MCP_NAMES`, `MCP_URLS`, and `MCP_TOKENS` lists, so you can also review or edit them by hand.

---

## REST API (Optional)

A separate REST API is available on port 8080 for integrating FreeClaw with other apps or scripts (it runs with TTS-aware formatting on by default). Start and stop it from the chat UI with `/startapi` and `/stopapi`, or manage it directly:

```bash
sudo systemctl start FreeClawAPI.service
sudo systemctl stop FreeClawAPI.service
```

**Endpoint:** `POST /chat`

```bash
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What is the weather like today?"}'
```

Sending `{"message": "/reset"}` resets the agent's conversation state.

---

## Configuration

Settings live in a `.env` file in the project root, created for you during install:

| Variable | Required | Purpose |
|---|---|---|
| `GOOGLE_KEY` | Yes | Google AI (Gemini) API key — the primary LLM provider |
| `FC_PASSWORD` | Yes | Password for the web UI login screen |
| `SECRET_KEY` | Yes | Flask session secret (auto-generated by the installer) |
| `CEREBRAS_KEY` | No | Cerebras API key, used as a fallback if Google fails |
| `NVIDIA_KEY` | No | NVIDIA NIM API key — only used to describe uploaded images, not a chat fallback |
| `MCP_NAMES` / `MCP_URLS` / `MCP_TOKENS` | No | Connected MCP servers — managed from the web UI's **+ MCP** sidebar |
| `CUSTOM_DOMAIN` | No | Overrides the auto-detected local IP for file/page links the agent returns |

---

## Updating

From your FreeClaw install directory:

```bash
./update.sh
```

This pulls the latest `src/`, `Flask/templates/`, and `Flask/main.py` from `origin/main`, leaves your `Flask/static/` data (context, uploads, generated pages) untouched, syncs dependencies, and restarts the service.

---

## Debugging

Every unexpected failure — a provider erroring out, a tool crashing, an MCP server going unreachable, an unhandled exception in a route — gets logged with its full traceback to `logs/freeclaw.log` at the repo root, rotated at 5MB (5 backups kept). This is separate from what you see in the chat UI or API response, which stays short on purpose; the log file is where the real cause lives.

```bash
tail -f logs/freeclaw.log
```

Warnings and errors are also mirrored to the console, so they show up live under `journalctl -u FreeClaw.service -f` too if you're running as a systemd service. `logs/` is never served by the app (unlike `Flask/static/`), so it's safe to keep tracebacks there even though they can include file paths and request shapes.

---

## Cost Philosophy

FreeClaw is built around one principle: **use the cheapest model that can do the job.**

- Greetings, small talk, and personal questions → `openai/gpt-oss-20b`, no tools, minimal context
- Search, coding, logic, and everything else → `openai/gpt-oss-120b`, full tools, context trimmed to a handful of recent messages
- Long-term facts → saved once to `context.md` instead of being re-sent every turn
- A free, no-LLM scraping pipeline does the heavy lifting for search instead of spending a model call on it

This keeps API costs near zero for everyday use.

---

## License

MIT — do whatever you want with it.