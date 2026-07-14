# Contributing to FreeClaw

Thanks for wanting to help out! Whether you're fixing a bug, adding a feature, or just improving the docs — all contributions are welcome. This is a small, friendly project, so don't stress about getting everything perfect.

---

## Getting Started

1. **Clone the repo**
   ```bash
   git clone https://github.com/eedeb/FreeClaw.git
   cd FreeClaw
   ```

2. **Install dependencies**
   ```bash
   pip install openai ddgs requests beautifulsoup4 json-repair fastapi uvicorn
   ```

3. **Set up your credentials** — run `./install.sh`, or create a `.env` file in the project root with at least your Google AI API key (`GOOGLE_KEY`) and a web UI password (`FC_PASSWORD`)

---

## Making Changes

1. **Create a branch** for your change — name it something descriptive:
   ```bash
   git checkout -b my-feature-name
   ```

2. **Make your changes** and test them locally before submitting

3. **Commit with a clear message** describing what you did:
   ```bash
   git commit -m "Add support for X" 
   ```

4. **Push your branch** and open a pull request:
   ```bash
   git push origin my-feature-name
   ```

5. In your pull request, briefly describe **what** you changed and **why** — a sentence or two is fine

---

## What Can I Contribute?

Anything! Some ideas:

- **Bug fixes** — if something's broken, fix it
- **New tools** — add a new tool to the agent (e.g. calendar, timers, notifications)
- **MCP integrations** — connectors for new MCP servers, or improvements to the MCP client
- **Scraper improvements** — better site handling, more reliable parsing
- **Performance & cost** — ways to reduce token usage even further
- **Documentation** — clearer explanations, more examples

If you're not sure whether an idea fits, just ask on Discord before spending time on it.

---

## Guidelines

- Keep changes focused — one thing per pull request makes reviewing much easier
- Don't commit API keys, tokens, or any credentials — leave those fields blank or use a `.env` file
- If you're adding a new feature, a quick note in the README is appreciated
- Be kind — see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)

---

## Questions?

Jump into the Discord server and ask. No question is too small.
