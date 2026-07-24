import base64
import json
import os
import re
import socket
import subprocess
from datetime import datetime

import Classy
import httpx
from dotenv import dotenv_values, load_dotenv
from json_repair import repair_json
from openai import OpenAI, APIConnectionError

import src.mcp_client as mcp_client
import src.scraper as scraper
from src.logging_setup import get_logger

load_dotenv()

logger = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Weights for the local Classy intent classifier.
CLASSIFIER_PATH = BASE_DIR + "/../models/data.pth"

# Root that Flask's /static/<path:filename> route serves from. Each user
# gets their own subfolder under here (set via set_static_dir), so links back
# to a created file need to include that subfolder, not just the filename.
STATIC_ROOT = os.path.normpath(BASE_DIR + '/../Flask/static')

# Folder the agent's file tools operate in — repointed at the active user's
# own files folder via set_static_dir().
static_dir = BASE_DIR + '/../Flask/static/'


def _server_base_url():
    """Public base URL for links to files the agent creates: CUSTOM_DOMAIN if
    set, otherwise this machine's LAN IP on the app's port."""
    custom_domain = os.getenv("CUSTOM_DOMAIN")
    if custom_domain:
        return custom_domain
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # doesn't actually send data
        ip = s.getsockname()[0]
    finally:
        s.close()
    return 'http://' + ip + ':6767'


url = _server_base_url()


static_token_signer = None


def set_static_token_signer(fn):
    """Registers the function that signs a static-file path into a
    short-lived access token (main.py wires this up at startup, same pattern
    as set_user_creator — agent.py can't import main.py directly since main.py
    already imports agent.py). fn must accept the file's path relative to
    Flask's static root and return a token string.

    Needed because links the agent hands back (e.g. a generated .ics) get
    opened by the client via the OS — Safari/Calendar on iOS, not the app's
    own authenticated session — so they can't rely on the login cookie."""
    global static_token_signer
    static_token_signer = fn


def _static_url(directory, filename):
    """Build the public /static/... URL for `filename`, which was written to
    `directory` — accounting for the per-user subfolder it may point at.
    Includes a signed access token so the link still works when opened
    outside the logged-in session (e.g. handed to Calendar/Safari on a
    phone), without requiring /static to be open to anyone who guesses a
    path."""
    rel = os.path.relpath(os.path.normpath(directory), STATIC_ROOT)
    if rel in ('.', '') or rel.startswith('..'):
        rel = ''
    else:
        rel = rel.replace(os.sep, '/') + '/'
    rel_path = rel + filename
    link = url + "/static/" + rel_path
    if static_token_signer is not None:
        link += "?token=" + static_token_signer(rel_path)
    return link


agent_messages = []
tools = []

# LLM providers are user-defined in Settings → Providers and persist in .env
# as five parallel JSON lists — the same storage shape MCP servers use (see
# src/mcp_client.py) — read fresh on every call so an add/remove/toggle takes
# effect without a restart. There is no built-in fallback: an empty list
# means the agent has nothing to call, which is reported to the user plainly
# (see _user_facing_error) rather than silently degrading to a default.
#
# Worth checking before trusting a new provider: reasoning models (qwen3.5,
# Gemini 3.x, NVIDIA Nemotron) tend to leak their thinking into plain content
# or misbehave on tool calls unless they have an explicit thinking
# off-switch, and Gemini 3.x 400s multi-turn tool calls through its
# OpenAI-compatible endpoint ("Function call is missing a thought_signature").
_ENV_PATH = os.path.join(os.path.dirname(BASE_DIR), ".env")
_PROVIDER_NAMES_KEY = "PROVIDER_NAMES"
_PROVIDER_URLS_KEY = "PROVIDER_URLS"
_PROVIDER_KEYS_KEY = "PROVIDER_KEYS"
_PROVIDER_MODELS_KEY = "PROVIDER_MODELS"
_PROVIDER_ENABLED_KEY = "PROVIDER_ENABLED"

# Which configured provider (by name) get_image_description uses — chosen in
# Settings → Vision Model, stored as a single scalar env var (unlike the
# parallel-list providers above, since there's only ever one selection).
_VISION_PROVIDER_KEY = "VISION_PROVIDER"


def _parse_env_list(raw):
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return val if isinstance(val, list) else []


def read_providers():
    """Return the user-defined providers as a list of
    {"name","url","key","model","enabled"} dicts, read fresh from .env on
    every call so runtime edits are picked up without a restart. Empty when
    the user hasn't configured any — see _active_providers, which has no
    fallback for that case."""
    if not os.path.exists(_ENV_PATH):
        return []
    env = dotenv_values(_ENV_PATH)
    names = _parse_env_list(env.get(_PROVIDER_NAMES_KEY))
    urls = _parse_env_list(env.get(_PROVIDER_URLS_KEY))
    keys = _parse_env_list(env.get(_PROVIDER_KEYS_KEY))
    models = _parse_env_list(env.get(_PROVIDER_MODELS_KEY))
    enabled = _parse_env_list(env.get(_PROVIDER_ENABLED_KEY))
    out = []
    for i, url in enumerate(urls):
        if not url:
            continue
        out.append({
            "name": names[i] if i < len(names) and names[i] else f"provider{i + 1}",
            "url": url,
            "key": keys[i] if i < len(keys) else "",
            "model": models[i] if i < len(models) else "",
            "enabled": bool(enabled[i]) if i < len(enabled) else True,
        })
    return out


def providers_to_env(providers):
    """Turn a list of provider dicts into the {ENV_KEY: value} mapping to
    persist. Values are single-quote-wrapped JSON so brackets and the inner
    double quotes survive python-dotenv untouched (callers must reject
    single quotes / newlines in the fields, same as MCP does)."""
    return {
        _PROVIDER_NAMES_KEY: "'" + json.dumps([p.get("name", "") for p in providers]) + "'",
        _PROVIDER_URLS_KEY: "'" + json.dumps([p.get("url", "") for p in providers]) + "'",
        _PROVIDER_KEYS_KEY: "'" + json.dumps([p.get("key", "") for p in providers]) + "'",
        _PROVIDER_MODELS_KEY: "'" + json.dumps([p.get("model", "") for p in providers]) + "'",
        _PROVIDER_ENABLED_KEY: "'" + json.dumps([bool(p.get("enabled", True)) for p in providers]) + "'",
    }


def read_vision_provider():
    """Name of the provider selected in Settings → Vision Model, or None if
    unset — read fresh from .env on every call, same as read_providers()."""
    if not os.path.exists(_ENV_PATH):
        return None
    return dotenv_values(_ENV_PATH).get(_VISION_PROVIDER_KEY) or None


def _active_providers():
    """The provider chain _create_completion actually tries, in order.
    Each item is (name, base_url, api_key, model_override, extra_body).

    Purely the enabled entries from Settings → Providers, in the order the
    user listed them — no built-in fallback. Empty if the user hasn't
    configured any yet. A provider with a blank model sends no override
    (the endpoint's own default model is used)."""
    user = [p for p in read_providers() if p.get("enabled", True) and p.get("url")]
    return [(p["name"], p["url"], p.get("key", ""), (p.get("model") or None), None) for p in user]

# Short timeouts + no SDK-level retries so a dead provider fails in seconds
# (not the SDK's 600s default compounded by exponential-backoff retries) and
# the next provider in the chain actually gets tried. A slow self-hosted or
# reasoning-model endpoint may need this raised.
_PROVIDER_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

# One OpenAI client per provider, built once and reused so switching
# providers doesn't pay a fresh TCP/TLS handshake every time.
_provider_clients = {}

# The provider that answered last — tracked only so _create_completion can
# log when a call actually switches providers. Try order itself always
# follows _active_providers()' order (first entry tried first, every call),
# not whichever provider happened to work last.
_last_provider = None


def _client_for(name, key, base_url):
    # Cache keyed on (name, key) so a key rotated at runtime (e.g. via
    # /api/settings) gets a fresh client instead of reusing one built with
    # the old (or missing) key.
    cached = _provider_clients.get(name)
    if cached is None or cached[0] != key:
        _provider_clients[name] = (key, OpenAI(
            api_key=key, base_url=base_url,
            timeout=_PROVIDER_TIMEOUT, max_retries=0,
        ))
    return _provider_clients[name][1]


def _classify_error(e):
    """Best-effort classification of a provider failure, used to build an
    accurate message if every provider in the chain fails."""
    status = getattr(e, "status_code", None)
    text = str(e).lower()
    if status == 429 or "rate limit" in text or "quota" in text:
        return "rate_limited"
    if status in (401, 403) or "invalid api key" in text or "unauthorized" in text:
        return "auth_error"
    # Other 4xx: the provider understood us and said the request itself is
    # bad (e.g. a malformed conversation history), not the network.
    if status in (400, 404, 413, 422):
        return "bad_request"
    # The openai SDK wraps raw httpx timeout/connect errors in
    # APIConnectionError before they reach us — check both to be safe.
    if isinstance(e, (APIConnectionError, httpx.TimeoutException, httpx.ConnectError)):
        return "network_error"
    if status and status >= 500:
        return "provider_error"
    return "unknown"


class AllProvidersFailedError(RuntimeError):
    """Raised when every configured LLM provider failed for one call.
    Carries the per-provider (name, reason, detail) failures so callers can
    build a message that reflects what actually went wrong instead of a
    generic string."""

    def __init__(self, failures):
        self.failures = failures
        super().__init__(
            "All providers failed: " + "; ".join(f"{n}: {d}" for n, _, d in failures)
        )


def _create_completion(**kwargs):
    """Try each configured provider in the order _active_providers() returns
    them — first entry first, every call — and return (response_or_stream,
    provider_name) from the first that works. Raises AllProvidersFailedError
    if none do."""
    global _last_provider
    failures = []
    for name, base_url, key, model_override, extra_body in _active_providers():
        if not key or key == "None":
            continue
        call_kwargs = kwargs if model_override is None else {**kwargs, "model": model_override}
        if extra_body:
            call_kwargs = {**call_kwargs, "extra_body": extra_body}
        # Strip params passed as None (stop, tools, ...). Per OpenAI
        # semantics null means the same as omitting the key, but Google's
        # Gemini shim 400s on any optional field sent as JSON null.
        call_kwargs = {k: v for k, v in call_kwargs.items() if v is not None}
        if "messages" in call_kwargs:
            # Our assistant messages carry a non-standard "provider" key (so
            # the UI can show which provider answered) — strip it before it
            # goes over the wire; some providers 400 on unrecognized fields.
            call_kwargs = {**call_kwargs, "messages": [
                {k: v for k, v in m.items() if k != "provider"} for m in call_kwargs["messages"]
            ]}
        try:
            c = _client_for(name, key, base_url)
            result = c.chat.completions.create(**call_kwargs)
            if name != _last_provider:
                print(f"LLM provider switched: {_last_provider} -> {name}")
            _last_provider = name
            return result, name
        except Exception as e:
            reason = _classify_error(e)
            failures.append((name, reason, str(e)))
            # Full traceback + request shape (never message content, which
            # may hold user data) — the short strings above are all that
            # ever reach the frontend or the model, so this is the only
            # place the real cause of a "provider error" is recoverable.
            logger.exception(
                "Provider '%s' failed (%s): model=%s tools=%s messages=%d",
                name, reason, call_kwargs.get("model"),
                bool(call_kwargs.get("tools")), len(call_kwargs.get("messages") or []),
            )
    raise AllProvidersFailedError(failures)


def _user_facing_error(failures):
    """Build a short, accurate frontend message from the per-provider
    failures collected by _create_completion."""
    if not failures:
        return "No LLM provider is configured. Add one in Settings → Providers."
    reasons = {r for _, r, _ in failures}
    if reasons == {"rate_limited"}:
        return "All configured providers are rate-limited or out of usage right now. Try again shortly."
    if reasons == {"auth_error"}:
        return "All configured providers rejected the request — check your API keys in Settings."
    if reasons == {"network_error"}:
        return "Couldn't reach any LLM provider — check your network connection."
    if reasons == {"rate_limited", "network_error"}:
        limited = [n for n, r, _ in failures if r == "rate_limited"]
        unreachable = [n for n, r, _ in failures if r == "network_error"]
        return (
            f"{', '.join(limited)} {'is' if len(limited) == 1 else 'are'} rate-limited, and "
            f"{', '.join(unreachable)} {'is' if len(unreachable) == 1 else 'are'} unreachable right now. Try again shortly."
        )
    return "All providers failed: " + ", ".join(f"{name} ({reason})" for name, reason, _ in failures)

# Maps the sanitized function name we expose to the model (e.g.
# "mcp_github_create_issue") back to the (server, real tool name) needed to
# actually invoke it. Rebuilt by load_mcp_tools().
mcp_tool_registry = {}


def set_static_dir(path):
    """Point the agent's file tools (read_file, create_file, create_page,
    etc.) at a specific folder — e.g. static/<username>/files/. context.md
    (the agent's long-term memory, read/updated via the same file tools)
    lives in this same folder, so this scopes both. Creates the folder if it
    doesn't exist yet."""
    global static_dir
    if not path.endswith(os.sep):
        path = path + os.sep
    os.makedirs(path, exist_ok=True)
    static_dir = path
    return static_dir


user_creator = None


def set_user_creator(fn):
    """Registers the function the create_user tool calls to actually create
    a new FreeClaw user. main.py wires this up at startup (rather than
    agent.py importing main.py directly, which would be circular) — fn must
    accept (name, context=None) and return the created user's name, raising
    an exception (with a clear message) on failure."""
    global user_creator
    user_creator = fn


def get_messages():
    return agent_messages


def _merge_system_messages(messages):
    """Collapse every system-role message into a single one at index 0.

    Some providers (confirmed on NVIDIA's qwen3.5) reject the whole request
    when a system message appears anywhere but the very front. Older saved
    conversations have two (instructions + context.md), so merge on load."""
    system_parts = [m.get("content", "") for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    if not system_parts:
        return rest
    merged = {"role": "system", "content": "\n\n".join(p for p in system_parts if p)}
    return [merged] + rest


def _heal_history(messages):
    """Repair a loaded conversation so every provider will accept it again.

    OpenAI-compatible APIs reject the entire request if any assistant
    tool_calls entry lacks a matching `tool` response (or a `tool` message
    answers an id nobody declared) — and since the full history is resent
    every turn, a conversation saved in that state (older builds, mid-turn
    crashes) stays broken forever without this. Missing tool responses get
    a placeholder, orphaned ones are dropped, null call ids are backfilled,
    and multiple system messages are merged (_merge_system_messages)."""
    messages = _merge_system_messages(messages)
    healed = []
    pending = {}  # id -> function name, awaiting a tool response

    def flush_pending():
        for call_id, fn_name in pending.items():
            healed.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": fn_name,
                "content": "(tool response missing from saved conversation — treat this call as failed)",
            })
        pending.clear()

    for n, m in enumerate(messages):
        role = m.get("role")
        if role == "tool":
            call_id = m.get("tool_call_id")
            if call_id in pending:
                del pending[call_id]
                healed.append(m)
            # else: orphaned/duplicate tool response — drop it
            continue
        flush_pending()
        healed.append(m)
        if role == "assistant" and m.get("tool_calls"):
            for i, tc in enumerate(m["tool_calls"]):
                if not tc.get("id"):
                    tc["id"] = f"healed_{n}_{i}"
                pending[tc["id"]] = (tc.get("function") or {}).get("name", "unknown")
    flush_pending()
    return healed


def set_messages(messages):
    """Load a previously-saved conversation (a plain list of OpenAI-style
    message dicts) as the active conversation for subsequent agent_stream
    calls. Healed on the way in so a conversation corrupted by an older
    build (or a mid-turn crash) can't keep failing every provider call."""
    global agent_messages
    agent_messages = _heal_history(messages)


def reset(tts=False):
    """Start a fresh conversation for the current static_dir, seeded with
    that user's context.md.

    The result is a single system message, always exactly one and always at
    index 0: some providers' chat templates (confirmed on NVIDIA's qwen3.5)
    reject the request the moment a second system-role message shows up
    anywhere else in the list. The eco_messages slicing in agent_stream
    assumes this is the only header message."""
    global agent_messages
    ctx_path = static_dir + "context.md"
    if not os.path.exists(ctx_path):
        with open(ctx_path, "w", encoding="utf-8") as f:
            f.write("")
    with open(ctx_path, "r", encoding="utf-8") as f:
        content = f.read()

    prompt = """
You are a capable AI assistant.

Answer the user's request directly.

If the request requires actions, perform them using available tools instead of describing how they could be done.

Adapt the depth of your response to the user's request.
Simple questions deserve simple answers.
Complex questions deserve thorough answers.

Use tools only when they are necessary.
Verify important information before responding.

Do not add unnecessary explanations, introductions, or conclusions.
Focus on solving the user's problem.

Keep context.md up to date with important information you may need later — use edit_file or create_file on it, the same as any other file.

Scheduled events are stored in the ping.md file
"""
    if tts:
        prompt += "\nYou will be connected to a text-to-speech system, so your responses should be optimized for clear and natural speech.\n"
    prompt += f"\nLong-term context about the user is stored in context.md, alongside their other files — read/edit it with the normal file tools. Here are its current contents: {content}\n"

    agent_messages = [{"role": "system", "content": prompt}]
    refresh_tools()


# Canonical timestamp the add_ping tool asks the model for. Both the add_ping
# sort and the ping scheduler parse times through parse_ping_time() rather than
# a single strict format, so a timestamp that's slightly off-format (seconds,
# a 'T' separator, AM/PM, ISO offset) still fires instead of sitting unnoticed
# in ping.md forever — that silent-skip was why scheduled pings weren't running.
PING_TIME_FORMAT = "%Y-%m-%d %H:%M"
_PING_TIME_FALLBACK_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y.%m.%d %H:%M",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y %I:%M %p",
    "%Y-%m-%d %I:%M %p",
    "%Y-%m-%d %I:%M%p",
)


def parse_ping_time(stamp):
    """Parse a ping timestamp into a naive local datetime, tolerating the
    common shapes a model emits instead of the exact PING_TIME_FORMAT. Returns
    None if nothing matches. A tz-aware value (e.g. an ISO string with an
    offset) is converted to local time and made naive so it compares cleanly
    against datetime.now()."""
    if not stamp:
        return None
    stamp = stamp.strip()
    parsed = None
    try:
        parsed = datetime.fromisoformat(stamp)  # tolerant on 3.11+ (space/T, secs)
    except ValueError:
        for fmt in (PING_TIME_FORMAT, *_PING_TIME_FALLBACK_FORMATS):
            try:
                parsed = datetime.strptime(stamp, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def build_file_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "create_user",
                "description": "Creates a new FreeClaw user with their own chats and memory. Only when explicitly asked to add a user — never to switch context in this conversation.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": { "type": "string", "description": "New user's name — used as a folder name, so keep it short (letters, numbers, spaces, - or _)." },
                        "context": { "type": "string", "description": "Optional starting content for the new user's context.md. Omit for blank." }
                    },
                    "required": ["name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Reads a file's contents from /static. Use this to view the context.md file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": { "type": "string", "description": "Name of the file" }
                    },
                    "required": ["filename"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "Lists the files in /static.",
                "parameters": { "type": "object", "properties": {} }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "create_page",
                "description": "Creates an HTML page for the user to see.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": { "type": "string", "description": "name_of_your_webpage.html" },
                        "contents": { "type": "string", "description": "HTML code" }
                    },
                    "required": ["filename","contents"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "create_file",
                "description": "Creates an output file (document, data export, script, config, etc.) for the user or other tools to use.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": { "type": "string", "description": "name_of_your_file.something" },
                        "contents": { "type": "string", "description": "File contents, can leave blank" }
                    },
                    "required": ["filename","contents"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "delete_file",
                "description": "Deletes a file from /static. Never delte context.md or ping.md.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": { "type": "string", "description": "name_of_your_file.something" },
                    },
                    "required": ["filename"]
                }
            }
        },
    {
            "type": "function",
            "function": {
                "name": "add_ping",
                "description": "Schedules a reminder or future action. The action text is delivered to the user as a prompt when its time arrives.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date_time": { "type": "string", "description": "Absolute fire time, format 'YYYY-MM-DD HH:MM' (e.g. '2026-07-23 14:30'). Resolve relative times yourself." },
                        "action": { "type": "string", "description": "What to do when it fires, as an instruction to yourself, e.g. 'Remind the user to take their medication.'" },
                    },
                    "required": ["date_time", "action"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "Edits an existing /static file by replacing one exact string with another. Use instead of create_file for modifying existing content. Use this to edit the context.md and ping.md files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": { "type": "string", "description": "Name of the file to edit" },
                        "old_str": { "type": "string", "description": "Exact string to find and replace" },
                        "new_str": { "type": "string", "description": "String to replace it with" }
                    },
                    "required": ["filename", "old_str", "new_str"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_image_description",
                "description": "Returns a detailed description of an image in /static.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": { "type": "string", "description": "name_of_your_image.something" }
                    },
                    "required": ["filename"]
                }
            }
        }
    ]


def build_search_tools():
    # Sites worth steering the model toward for common query types.
    best_sites = {
        "weather": ["localconditions.com"],
        "news": ["bbc.com", "atoztimes.com"],
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Fetches up-to-date info for real-time queries. Max 2 calls per task — proceed once you have enough, or report back to the user if you still don't after 2 searches. Here is a website guide: "+str(best_sites),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": { "type": "string", "description": "Natural language query to answer" },
                        "site": { "type": "string", "description": "Site to search. Omit to search generally." }
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_web",
                "description": "Returns the first 3000 characters of a webpage. Use only when the user asks to look at a specific URL.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": { "type": "string", "description": "URL of the webpage to read" }
                    },
                    "required": ["url"]
                }
            }
        }
    ]


def build_utility_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "open_url",
                "description": "Opens a URL or URI on the user's device — webpages, apps via custom URI (e.g. texting, calling).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": { "type": "string", "description": "url to open" }
                    },
                    "required": ["url"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "run_bash_command",
                "description": "Runs a shell command on this machine. Execute immediately when asked — don't chain multiple commands without reporting back first.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": { "type": "string", "description": "BASH command" }
                    },
                    "required": ["command"]
                }
            }
        }
    ]


def _sanitize_tool_name(name):
    """OpenAI function names must match ^[A-Za-z0-9_-]+$ and stay short, so
    scrub anything else out of the MCP-derived name."""
    cleaned = re.sub(r'[^0-9A-Za-z_-]', '_', name).strip('_') or 'tool'
    return cleaned[:60]


def load_mcp_tools():
    """Connect to each MCP server configured in .env, fetch its tools, and
    return them as OpenAI-style function definitions. Also (re)builds
    mcp_tool_registry, which maps the function name exposed to the model back
    to the (server, real tool name) needed to actually call it.

    A single unreachable server is logged and skipped rather than taking down
    the whole tool list."""
    global mcp_tool_registry
    registry = {}
    out = []
    for server in mcp_client.read_servers():
        if not server.get("enabled", True):
            continue
        try:
            server_tools = mcp_client.list_tools(server)
        except Exception as e:
            print(f"[mcp] '{server.get('name')}' unavailable: {e}")
            logger.exception("MCP server '%s' (%s) unavailable", server.get('name'), server.get('url'))
            continue
        for t in server_tools:
            real_name = t.get("name")
            if not real_name:
                continue
            fn_name = _sanitize_tool_name(f"mcp_{server.get('name', '')}_{real_name}")
            # Guarantee uniqueness across servers/tools.
            base = fn_name
            n = 1
            while fn_name in registry:
                suffix = f"_{n}"
                fn_name = base[:60 - len(suffix)] + suffix
                n += 1
            params = t.get("inputSchema") or {"type": "object", "properties": {}}
            description = t.get("description") or f"{real_name} (via '{server.get('name')}' MCP server)"
            out.append({
                "type": "function",
                "function": {
                    "name": fn_name,
                    "description": description[:1024],
                    "parameters": params,
                },
            })
            registry[fn_name] = {"server": server, "tool": real_name}
    mcp_tool_registry = registry
    return out


def refresh_tools():
    """Rebuild the full tool list = built-in tools + any MCP server tools.
    Safe to call anytime (e.g. after the MCP server list changes); does not
    touch the conversation or the LLM client."""
    global tools
    tools = build_file_tools() + build_search_tools() + build_utility_tools() + load_mcp_tools()
    return tools


def _run_tool(command_name, args_dict):
    """Execute a single tool call and return its result as a string.

    Pure dispatch: appending the tool-response message and making the
    follow-up LLM turn are the caller's job, so this can run once per call
    when the model requests several tools in one turn. Exceptions may
    escape freely — the caller converts them into an error result, because
    whatever happens, every tool_call id the assistant message declared
    must end up with a response or the whole conversation is rejected by
    the provider on the next turn."""
    parameter = (args_dict.get('query') or args_dict.get('site') or args_dict.get('url')
                 or args_dict.get('command') or args_dict.get('filename')
                 or args_dict.get('contents') or None)
    print(f"Agent called tool: {command_name}" + (f" — {parameter}" if parameter else ""))

    if command_name == 'create_user':
        new_name = args_dict.get('name')
        new_context = args_dict.get('context')
        if not new_name or not str(new_name).strip():
            return "Error: a name is required to create a user."
        if user_creator is None:
            return "Error: user creation isn't available in this context."
        try:
            created_name = user_creator(new_name, new_context)
        except Exception as e:
            logger.exception("create_user tool failed for name=%r", new_name)
            return f"Error creating user: {e}"
        result = f"User '{created_name}' created successfully."
        if new_context:
            result += " Their context.md was set with the provided content."
        return result

    if command_name == 'search':
        site = args_dict.get('site')
        if site:
            return scraper.get_result(parameter + ' - ' + site)
        return scraper.get_result(parameter)

    if command_name == 'read_file':
        # Uploaded files are referenced by their full "static/..." path
        # in the chat tag, not a bare filename; take just the basename
        # so both forms resolve against this session's static_dir.
        filename = os.path.basename(args_dict.get('filename'))
        try:
            with open(static_dir+filename, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return "File not found."

    if command_name == 'get_image_description':
        vision_provider_name = read_vision_provider()
        if not vision_provider_name:
            return "Image description isn't configured — pick a provider in Settings → Vision Model."
        provider = next((p for p in read_providers() if p.get("name") == vision_provider_name), None)
        if provider is None or not provider.get("url"):
            return f"The vision provider '{vision_provider_name}' no longer exists — pick another in Settings → Vision Model."
        if not provider.get("key"):
            return f"Provider '{vision_provider_name}' has no API key set — add one in Settings → Providers."
        if not provider.get("model"):
            return f"Provider '{vision_provider_name}' has no model set — add one in Settings → Providers to use it for vision."

        filename = os.path.basename(args_dict.get('filename'))
        file_location = static_dir+filename
        try:
            with open(file_location, "rb") as image_file:
                image_data = base64.b64encode(image_file.read()).decode("utf-8")
        except FileNotFoundError:
            return "File not found."

        ext = filename.rsplit(".", 1)[-1].lower()
        mime_types = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
        mime_type = mime_types.get(ext, "image/jpeg")

        vision_client = _client_for(provider["name"], provider["key"], provider["url"])
        try:
            completion = vision_client.chat.completions.create(
                model=provider["model"],
                messages=[
                    {
                        "role": "system",
                        "content": "Describe images that the user sends in extreme detail"
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{image_data}"
                                }
                            },
                            {
                                "type": "text",
                                "text": "Please describe this image in extreme detail."
                            }
                        ]
                    }
                ],
                temperature=1,
                top_p=1,
            )
        except Exception as e:
            logger.exception("Vision provider '%s' failed", vision_provider_name)
            return f"Image description failed — vision provider '{vision_provider_name}' returned an error: {e}"
        return completion.choices[0].message.content or "No description returned."

    if command_name == 'list_files':
        return "Files in static directory: "+", ".join(os.listdir(static_dir))

    if command_name == 'read_web':
        return scraper.scrape(parameter)

    if command_name == 'create_file':
        filename = args_dict.get('filename')
        if "/" in filename or "\\" in filename:
            return "Invalid filename."
        with open(static_dir+filename, "w", encoding="utf-8") as f:
            f.write(args_dict.get('contents') or '')
        return "Your file is accessible at "+_static_url(static_dir, filename)

    if command_name == 'delete_file':
        filename = args_dict.get('filename')
        if "/" in filename or "\\" in filename:
            return "Invalid filename."
        file_path = static_dir + filename
        if os.path.exists(file_path):
            os.remove(file_path)
            return "File deleted."
        return "File not found."

    if command_name == 'edit_file':
        filename = args_dict.get('filename')
        if "/" in filename or "\\" in filename:
            return "Invalid filename."
        old_str = args_dict.get('old_str')
        new_str = args_dict.get('new_str')
        try:
            with open(static_dir + filename, "r", encoding="utf-8") as f:
                contents = f.read()
        except FileNotFoundError:
            return "File not found."
        if old_str not in contents:
            return "String not found in file."
        updated = contents.replace(old_str, new_str, 1)
        with open(static_dir + filename, "w", encoding="utf-8") as f:
            f.write(updated)
        return "File edited successfully."
    if command_name == 'add_ping':
        filename = "ping.md"
        date_time = args_dict.get('date_time')
        action = args_dict.get('action')
        with open(static_dir+filename, "a", encoding="utf-8") as f:
            f.write(f"{date_time} - {action}\n")

        # Re-sort ping.md on every update so the next scheduled event is
        # always the first line and the furthest-out event is the last.
        # Each entry is "YYYY-MM-DD HH:MM - <action>"; sort by the parsed
        # timestamp. Any line whose timestamp doesn't parse is kept in its
        # existing order at the bottom rather than dropped.
        with open(static_dir+filename, "r", encoding="utf-8") as f:
            entries = [line for line in f.read().splitlines() if line.strip()]

        def _ping_sort_key(line):
            parsed = parse_ping_time(line.split(" - ", 1)[0])
            return (1, datetime.max) if parsed is None else (0, parsed)

        entries.sort(key=_ping_sort_key)
        with open(static_dir+filename, "w", encoding="utf-8") as f:
            f.write("\n".join(entries) + "\n" if entries else "")
        return "Ping added successfully."
    if command_name == 'create_page':
        filename = args_dict.get('filename')
        if "/" in filename or "\\" in filename:
            return "Invalid filename."
        with open(static_dir+filename, "w", encoding="utf-8") as f:
            f.write(args_dict.get('contents') or '')
        return "Your site is live at "+_static_url(static_dir, filename)

    if command_name == 'open_url':
        # Actually opening the tab happens client-side — the frontend
        # listens for the "tool_call" SSE event (which already carries
        # this url in evt.arguments) and calls window.open() on it.
        return "URL opened: "+args_dict.get('url', '')

    if command_name == 'run_bash_command':
        proc = subprocess.Popen(
            f'{parameter}',
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=True
        )
        stdout, stderr = proc.communicate()
        print(stdout,stderr)
        output = (stdout + "\n" + stderr).strip()
        if not output:
            output = 'Command was run successfully, Report back to the user.'
        return output

    if command_name in mcp_tool_registry:
        entry = mcp_tool_registry[command_name]
        server = entry["server"]
        try:
            return mcp_client.call_tool(server, entry["tool"], args_dict)
        except Exception as e:
            logger.exception("MCP tool '%s' on '%s' failed", entry['tool'], server.get('name'))
            return f"Error calling MCP tool '{entry['tool']}' on '{server.get('name')}': {e}"

    # Unknown tool (e.g. an MCP server that was removed after the model
    # decided to call it) — answer it anyway so the tool_call isn't left
    # dangling, which would break the next turn.
    return "Unknown tool: " + command_name


# Per-intent settings for a turn: (recent messages to send, temperature,
# tools offered). Simple conversational intents get a small context window
# and no tools; precision-flavored intents run colder. The system message at
# index 0 is always sent on top of the recent slice.
_TAG_SETTINGS = {
    'Greeting/goodbye':  (3, 1.0, 'none'),
    'Personal-question': (5, 1.0, 'none'),
    'Banter':            (5, 1.0, 'none'),
    'About-user':        (5, 1.0, 'none'),
    'Search':            (5, 0.4, 'search'),
    'Context':           (9, 1.0, 'all'),
    'Edit':              (9, 1.0, 'all'),
    'Logic':             (7, 0.2, 'all'),
    'Math':              (7, 0.2, 'all'),
    'Explain':           (7, 0.2, 'all'),
}
_DEFAULT_TAG_SETTINGS = (7, 1.0, 'all')  # Coding, Writing, List, Suggest, Utility, ...


def agent_stream(user_input=None, system_input=None, tool_input=None, tool_id=None, tool_name=None):
    """Generator version of the agent loop. Yields small dict events as the
    model produces output, so callers (e.g. the Flask route) can stream
    them to the browser in real time:
      {"type": "token", "text": "..."}            - a chunk of assistant text
      {"type": "tool_call", "name": "...", "arguments": {...}} - tool invocation started
      {"type": "tool_result", "name": "...", "result": "..."}  - tool finished
    The full, final conversation is available afterwards via agent_messages.
    """
    # Default model id — any provider with its own model set overrides it.
    model = "openai/gpt-oss-120b"
    temp = 1
    check_tools = tools
    if user_input and system_input:
        raise Exception("You cannot have both user input and system input at the same time.")
    elif user_input:
        if user_input.lower() == 'reset':
            reset()
            yield {"type": "token", "text": "Agent reset."}
            return

        intent, _ = Classy.classify(user_input, CLASSIFIER_PATH)
        tag = intent[0]
        print('Intent: ' + tag)

        agent_messages.append({"role": "user", "content": user_input})
        agent_input = user_input

        recent, temp, tool_mode = _TAG_SETTINGS.get(tag, _DEFAULT_TAG_SETTINGS)
        if len(agent_messages) > recent + 2:
            eco_messages = [agent_messages[0], *agent_messages[-recent:]]
        else:
            eco_messages = agent_messages
        if tool_mode == 'none':
            check_tools = None
        elif tool_mode == 'search':
            check_tools = build_search_tools()
    elif system_input:
        # Kept for direct/external callers only — note that appending a
        # second system-role message breaks the single-leading-system-message
        # invariant reset() relies on, and some providers reject that.
        agent_messages.append({"role": "system", "content": system_input})
        agent_input = system_input
        eco_messages = agent_messages
    # `is not None` (not truthiness): a tool can legitimately return "" —
    # e.g. reading an empty file — and that still has to be recorded as the
    # call's response and continue the turn, not fall through to the
    # "no input" error below with the tool_call left dangling.
    elif tool_input is not None:
        temp = 0.2
        yield {"type": "tool_result", "name": tool_name, "result": tool_input}
        agent_messages.append({
            "role": "tool",
            "tool_call_id": tool_id,
            "name": tool_name,
            "content": tool_input,
        })
        agent_input = tool_input
        # Resume from 2 user messages ago, or the first user message if
        # there aren't 2. A system-initiated conversation may have no user
        # turns at all — keep everything after the one system message then.
        user_indices = [i for i, m in enumerate(agent_messages) if m['role'] == 'user']
        if len(user_indices) >= 2:
            start_index = user_indices[-2]
        elif user_indices:
            start_index = user_indices[0]
        else:
            start_index = 1
        eco_messages = [agent_messages[0]] + agent_messages[start_index:]
    else:
        raise Exception("You must have either user input or system input.")
    print('Received: ' + agent_input)
    try:
        stream, provider = _create_completion(
            model=model,
            messages=eco_messages,
            temperature=temp,
            tools=check_tools,
            top_p=1,
            stream=True,
        )
    except AllProvidersFailedError as e:
        # Each provider's full traceback was already logged individually
        # inside _create_completion — this ties them together as one
        # incident so they're easy to find by searching the log.
        logger.error("All providers failed for this turn: %s", e.failures)
        raise Exception(_user_facing_error(e.failures))

    # Tell the frontend which provider is about to answer. This fires once
    # per _create_completion call, and every tool-call continuation is its
    # own recursive agent_stream() -> _create_completion() call (see the
    # `yield from agent_stream(...)` below), so a fallback mid-conversation
    # (or even mid a single tool round-trip) surfaces here too, not just at
    # the very start of the turn.
    yield {"type": "provider", "name": provider}

    # Consume the stream, forwarding text chunks to the caller in real
    # time and reassembling any tool calls (which always arrive as
    # incremental argument-string fragments when streamed).
    buffer = ""
    tool_calls_acc = {}
    try:
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if getattr(delta, "content", None):
                buffer += delta.content
                yield {"type": "token", "text": delta.content}
            if getattr(delta, "tool_calls", None):
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}
                    if tc_delta.id:
                        tool_calls_acc[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_calls_acc[idx]["function"]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_calls_acc[idx]["function"]["arguments"] += tc_delta.function.arguments
    except Exception as e:
        # A provider's own stream can break mid-response — confirmed on
        # NVIDIA: a malformed SSE event with no JSON body, which the
        # openai SDK doesn't guard against and raises JSONDecodeError
        # straight out of chunk iteration. This is a different failure
        # mode from the one _create_completion guards: that only covers
        # the initial request, not the body actually arriving afterward.
        # Log it fully, but keep whatever partial content/tool-call we
        # already have instead of losing it outright — that matters most
        # when a tool call already completed and only the follow-up reply
        # got cut off.
        logger.exception(
            "Stream from provider '%s' broke mid-response: %d chars buffered, %d tool call(s) in progress",
            provider, len(buffer), len(tool_calls_acc),
        )
        if not buffer and not tool_calls_acc:
            buffer = f"(No response — the connection to {provider} was interrupted before anything came back. Please try again.)"

    tool_calls_list = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())] if tool_calls_acc else None
    if buffer:
        print('Agent: ' + buffer)

    if tool_calls_list:
        # A provider that streams a tool call without an id would leave
        # id=None on both the assistant message and its tool response, and
        # OpenAI-compatible APIs reject null ids. Synthesize stable ones so
        # the two sides always match.
        for i, tc in enumerate(tool_calls_list):
            if not tc["id"]:
                tc["id"] = f"call_{i}"

        assistant_msg = {
            "role": "assistant",
            "provider": provider,
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": tc["type"],
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"]
                    }
                }
                for tc in tool_calls_list
            ]
        }
        # Keep any text the model streamed before its tool calls — dropping
        # it would desync the saved conversation from what the user saw.
        if buffer:
            assistant_msg["content"] = buffer
        agent_messages.append(assistant_msg)

        # The assistant message above declares every requested call, and an
        # OpenAI-compatible API rejects any history where a tool_calls id
        # has no matching tool response — which would permanently break the
        # conversation, since the full history is resent every turn. So run
        # every call (models often request several at once), turn any
        # failure into an error result instead of letting it escape, and
        # hand the last result to the recursive turn that asks the model to
        # continue.
        last = len(tool_calls_list) - 1
        for i, tc in enumerate(tool_calls_list):
            command_name = tc["function"]["name"]
            args_dict = None
            result = None
            try:
                args_dict = json.loads(repair_json(tc["function"]["arguments"]))
                # MCP (and malformed) tool calls can arrive with a
                # non-object payload; keep _run_tool, which assumes a dict,
                # crash-free.
                if not isinstance(args_dict, dict):
                    args_dict = {}
            except Exception as e:
                result = f"Error: couldn't parse arguments for '{command_name}': {e}"
                logger.warning(
                    "Tool call args unparseable for '%s': %.500r",
                    command_name, tc["function"]["arguments"], exc_info=True,
                )
            if args_dict is not None:
                yield {"type": "tool_call", "name": command_name, "arguments": args_dict}
                try:
                    result = _run_tool(command_name, args_dict)
                except Exception as e:
                    logger.exception("Tool '%s' raised with args=%.500r", command_name, args_dict)
                    result = f"Error running tool '{command_name}': {e}"
            if not isinstance(result, str):
                result = "" if result is None else str(result)
            if i < last:
                yield {"type": "tool_result", "name": command_name, "result": result}
                agent_messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": command_name,
                    "content": result
                })
            else:
                yield from agent_stream(tool_input=result, tool_id=tc["id"], tool_name=command_name)
        return

    agent_messages.append({
        "role": "assistant",
        "provider": provider,
        "content": buffer,
    })


def agent(user_input=None, system_input=None, tool_input=None, tool_id=None, tool_name=None):
    """Non-streaming entry point: drains agent_stream() and returns the
    full conversation."""
    for _ in agent_stream(user_input=user_input, system_input=system_input,
                          tool_input=tool_input, tool_id=tool_id, tool_name=tool_name):
        pass
    return agent_messages


def api_complete(messages, model=None, stream=False, temperature=1.0, max_tokens=None):
    """Stateless LLM call for the OpenAI-compatible API endpoint. Does not
    touch agent_messages. Tries the configured providers in order, via the
    same cached, fast-fail clients as agent_stream."""
    kwargs = dict(
        model=model or "openai/gpt-oss-120b",
        messages=messages,
        temperature=temperature,
        top_p=1,
        stream=stream,
    )
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    result, _ = _create_completion(**kwargs)
    return result