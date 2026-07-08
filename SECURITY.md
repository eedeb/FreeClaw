# Security Policy

## Supported Versions

FreeClaw doesn't use versioned releases. Only the latest code on `main` is actively maintained.

---

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities. Instead, reach out directly on Discord so it can be handled privately before any fix is made public.

**Contact:** [your Discord link or username here]

Describe what you found, how to reproduce it, and what the potential impact might be. You'll get a response as soon as possible.

---

## What Counts as a Security Issue

Given the nature of this project, the most relevant areas of concern are:

- **Credential exposure** — anything that could leak your Groq API key, MCP server tokens, or other secrets stored in the codebase or `.env`
- **Bash execution abuse** — the `run_bash_command` tool runs shell commands directly on the host machine; any prompt injection or bypass that causes unintended commands to run is a serious issue
- **Scraper exploitation** — malicious web content that manipulates the agent's behavior through scraped text
- **MCP server trust** — a malicious or compromised MCP server can return content that manipulates the agent, or attempt to misuse the token it's given

---

## Best Practices for Running FreeClaw

To keep your own instance secure:

- **Never commit credentials** — keep your API keys and tokens out of the repo; use environment variables or a `.env` file instead
- **Only connect trusted MCP servers** — FreeClaw sends your configured token to each MCP server and runs the tools it advertises, so only add servers you trust
- **Be careful with bash** — the `run_bash_command` tool is powerful; only run FreeClaw in an environment you're comfortable with it having shell access to
