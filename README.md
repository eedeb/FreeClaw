# 🦅 FreeClaw

> **An AI agent that doesn't burn your money.**

FreeClaw is a cost-efficient, tool-using AI agent built on top of [Groq](https://groq.com/)'s fast inference API. It routes your queries to the smallest appropriate model and only calls expensive tools when truly needed — keeping token usage lean without sacrificing capability.

---

## Features

- **Smart intent classification** — uses a `Classy` classifier to determine what kind of request you're making (greeting, search, coding, math, etc.) and routes it to the right model and context window size
- **Adaptive model selection** — simple conversational messages use a lightweight `llama-3.1-8b-instant` model; complex tasks escalate to a larger model only when necessary
- **Minimal context windowing** — only the most relevant recent messages are sent to the API per turn, dramatically reducing token usage
- **Web search & scraping** — searches DuckDuckGo and scrapes the first viable result, cleaning and structuring it via a secondary LLM pass before returning it to the agent
- **File & page creation** — can write files and live HTML pages directly to a web server, returning a public URL
- **Smart home integration** — sends commands to Amazon Alexa and controls a smart TV (on/off, volume, YouTube playback)
- **Bash execution** — can run shell commands on the host machine and report results back
- **TTS-aware mode** — optional text-to-speech formatting for voice output contexts

---

## How It Works

```
User Input
    │
    ▼
Classy.classify()          ← determines intent & sets model/context strategy
    │
    ▼
Groq API call              ← uses eco_messages (trimmed history) + optional tools
    │
    ├── Tool call?  ────►  Execute tool (search, bash, Alexa, TV, file, etc.)
    │                          │
    │                          └──► Recursive agent() call with tool result
    │
    └── Text response? ──► Append to history, return to caller
```

The scraper pipeline works as follows:
1. DuckDuckGo search returns up to 10 URLs
2. Each URL is scraped with BeautifulSoup, skipping paywalled/unreliable sites
3. The first 3,000 characters of clean text are passed to `llama-3.3-70b-versatile` to produce a structured summary
4. That summary is fed back to the main agent as a tool result

---

## Files

| File | Description |
|---|---|
| `agent.py` | Core agent loop — intent classification, model routing, tool dispatch, context management |
| `scraper.py` | Web search (DuckDuckGo) + HTML scraping + text cleaning |
| `api.py` | FastAPI server that exposes the agent over HTTP |
| `alexa_integration.py` | Sends TTS commands to an Alexa device via Home Assistant |
| `smart_tv.py` | Controls a smart TV via Home Assistant (power, volume, YouTube) |
| `data.pth` | PyTorch data file used by the `Classy` intent classifier |

---

## Module Details

### `api.py` — HTTP Server

Wraps the agent in a [FastAPI](https://fastapi.tiangolo.com/) server so it can be called over the network. Configure your Groq key, location, and TTS preference at the top of the file, then run it with `uvicorn`.

**Endpoint:** `POST /chat`

```json
{ "message": "What's the weather in London?" }
```

Returns:

```json
{ "response": "..." }
```

Two special commands are also supported:
- `/reset` — clears the agent's conversation history
- `/shutdown` — kills the server process

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

---

### `alexa_integration.py` — Alexa Control

Sends arbitrary text as a spoken announcement to an Alexa device using the [Home Assistant](https://www.home-assistant.io/) REST API. Fill in your `TOKEN` (a long-lived Home Assistant access token) and `url` (your HA `media_player/play_media` endpoint).

```python
from alexa_integration import send_to_alexa
send_to_alexa("Dinner is ready.")
```

---

### `smart_tv.py` — Smart TV Control

Controls a smart TV and Google Cast device via Home Assistant. Fill in your `TOKEN` and the base `url` at the top of the file.

Available functions:

| Function | Description |
|---|---|
| `tv_on()` | Turns the TV on |
| `tv_off()` | Turns the TV off |
| `volume_up()` | Increases volume |
| `volume_down()` | Decreases volume |
| `play_youtube(media_id)` | Casts a YouTube video by its video ID |

```python
from smart_tv import play_youtube
play_youtube("dQw4w9WgXcQ")
```

---

## Dependencies

```bash
pip install groq ddgs requests beautifulsoup4 json-repair fastapi uvicorn
```

The `Classy` intent classifier (`data.pth`) is a local module included in the repo.

---

## Setup

### 1. Configure credentials

In `api.py`, set your Groq API key and location:

```python
groq_key = "your_groq_api_key"
location = "Your City, State"
tts = False  # set True for text-to-speech output formatting
```

In `alexa_integration.py` and `smart_tv.py`, fill in your Home Assistant `TOKEN` and endpoint `url`.

### 2. Run the server

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

### 3. Chat

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What is the latest news?"}'
```

### Alternatively — run directly in Python

```python
import agent

agent.reset(groq_key="your_key", location_innit="Your City, State")
print(agent.agent(user_input="Tell me a joke"))
```

Or interactively by uncommenting the loop at the bottom of `agent.py`:

```python
while True:
    output = agent(user_input=input(': '))
    print(output)
```

---

## Cost Philosophy

FreeClaw is designed around one principle: **use the cheapest model that can do the job.**

- Greetings and small talk → `llama-3.1-8b-instant` (no tools, minimal context)
- Search queries → full tools, trimmed to last 3 messages
- Coding, writing, logic → moderate context, larger model only if needed
- Web summarization → separate cheaper call isolated from main agent context

This keeps API costs near zero for everyday use.

---

## License

MIT — do whatever you want with it.
