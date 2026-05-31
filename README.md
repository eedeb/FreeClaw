# 🦅 FreeClaw

> **An AI agent that doesn't burn your money.**

FreeClaw is a cost-efficient, tool-using AI agent that runs on your own machine. It comes with a slick dark-themed web UI where you can chat with it from any browser on your network. It searches the web, runs bash commands, controls your smart home, writes files — and does it all without blowing through your API budget.

---

## Installation

Install FreeClaw with a single command:

```bash
curl -fsSL https://freeclaw.eedeb.dev/install.sh | bash
```

The script will:
1. Clone the repo
2. Set up a Python virtual environment and install all dependencies
3. Ask for your **Groq API key**
4. Optionally walk you through **Home Assistant integration** (for Alexa and smart TV control)
5. Register FreeClaw as a systemd service so it starts automatically
6. Print the local URL to open in your browser

---

## Using FreeClaw

Once installed, open the URL printed by the installer in your browser — something like `http://192.168.x.x:6767`. You'll see the FreeClaw chat UI:

- Type a message and press **Enter** to send (Shift+Enter for a newline)
- Agent responses are rendered with full markdown — code blocks, lists, bold, links, etc.
- A live **token estimate** is shown in the top right so you can keep an eye on usage
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

- **Smart intent classification** — a local `Classy` classifier reads your message and routes it to the right model and context size before any API call is made
- **Adaptive model selection** — greetings and small talk use `llama-3.1-8b-instant`; complex tasks escalate to a larger model only when needed
- **Minimal context windowing** — only the most relevant recent messages are sent per turn, keeping token usage low
- **Web search & scraping** — searches DuckDuckGo, scrapes the top result, and summarizes it cleanly before passing it back to the agent
- **Bash execution** — can run shell commands on the host machine and return the output
- **File & page creation** — can write files and live HTML pages to a web server and return a public URL
- **Smart home control** — speaks through Alexa and controls a smart TV (power, volume, YouTube) via Home Assistant
- **TTS-aware mode** — optional formatting for text-to-speech output contexts

---

## How It Works

```
Browser (chat UI)
    │  POST /chat
    ▼
Flask server (Flask/main.py, port 6767)
    │
    ▼
Classy.classify()       ← local intent classifier using models/data.pth
    │                      determines model choice + context window size
    ▼
Groq API call           ← trimmed message history + optional tools
    │
    ├── Tool call? ───► Execute tool (search, bash, Alexa, TV, file…)
    │                       │
    │                       └──► Recursive agent() call with tool result
    │
    └── Text response? ──► Return to Flask → back to browser as JSON
```

The scraper pipeline:
1. DuckDuckGo returns up to 10 candidate URLs
2. Each is scraped with BeautifulSoup; paywalled/unreliable sites are skipped
3. The first 3,000 chars of clean text are summarized by `llama-3.3-70b-versatile` in a separate, isolated API call
4. The summary is injected back into the agent as a tool result

---

## Project Structure

```
FreeClaw/
├── Flask/
│   ├── main.py              # Flask server — routes, chat endpoint, command handling
│   └── templates/
│       └── index.html       # Chat UI (dark theme, markdown rendering, token counter)
├── src/
│   ├── agent.py             # Core agent loop — intent classification, model routing, tools
│   ├── scraper.py           # DuckDuckGo search + HTML scraping + text cleaning
│   ├── api.py               # Optional REST API (uvicorn, port 8080)
│   ├── alexa_integration.py # Alexa TTS via Home Assistant
│   └── smart_tv.py          # Smart TV control via Home Assistant
├── models/
│   └── data.pth             # Classy intent classifier weights
├── install.sh               # One-line installer
└── ha_setup.sh              # Home Assistant setup helper
```

---

## Smart Home Setup

During installation you'll be asked if you want to set up Home Assistant integration. If you say yes, `ha_setup.sh` will walk you through entering your Home Assistant IP and API token.

This enables:
- **Alexa** — FreeClaw can speak responses aloud through any Alexa device on your network
- **Smart TV** — FreeClaw can turn your TV on/off, adjust volume, and cast YouTube videos

You can also run the setup separately at any time:

```bash
./ha_setup.sh
```

---

## REST API (Optional)

A separate REST API is available on port 8080 for integrating FreeClaw with other apps or scripts. Start and stop it from the chat UI with `/startapi` and `/stopapi`, or manage it directly:

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

---

## Cost Philosophy

FreeClaw is built around one principle: **use the cheapest model that can do the job.**

- Greetings and small talk → `llama-3.1-8b-instant`, no tools, minimal context
- Search and smart home → full tools, trimmed to last few messages
- Coding, writing, logic → moderate context, larger model only if needed
- Web summarization → a separate, isolated cheap call that never inflates the main context

This keeps API costs near zero for everyday use.

---

## License

MIT — do whatever you want with it.
