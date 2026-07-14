import json
import re
import Classy
from openai import OpenAI, APIConnectionError
import subprocess
import shlex
import src.scraper as scraper
from datetime import datetime
from json_repair import repair_json
import src.mcp_client as mcp_client
from src.logging_setup import get_logger
import os

logger = get_logger(__name__)


import base64
import mimetypes
import httpx

 


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

html_dir=BASE_DIR+'/../Flask/static/'
static_dir=BASE_DIR+'/../Flask/static/'
location = BASE_DIR + "/../models/data.pth"

# Root that Flask's /static/<path:filename> route serves from. Each user now
# gets their own subfolder under here (set via set_static_dir), so links back
# to a created file need to include that subfolder, not just the filename.
STATIC_ROOT = os.path.normpath(BASE_DIR + '/../Flask/static')


def _static_url(directory, filename):
    """Build the public /static/... URL for `filename`, which was written to
    `directory` (static_dir or html_dir) — accounting for the per-user
    subfolder those may point at."""
    rel = os.path.relpath(os.path.normpath(directory), STATIC_ROOT)
    if rel in ('.', '') or rel.startswith('..'):
        rel = ''
    else:
        rel = rel.replace(os.sep, '/') + '/'
    return url + "/static/" + rel + filename

from dotenv import load_dotenv

load_dotenv()




custom_domain = os.getenv("CUSTOM_DOMAIN")

if custom_domain is None:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        # Doesn't actually send data
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()

    url='http://'+ip+':6767'
else:
    url=custom_domain





agent_messages=[]
tools=[]

# LLM provider fallback chain: (name, env_var, base_url, model_override,
# extra_body), tried in order. The key is looked up from the environment
# fresh on every call (not captured here) so a key added or changed via
# /api/settings takes effect immediately, without restarting the process.
# A provider with no key configured is skipped.
#
# model_override, when set, replaces whatever model the caller asked for
# when talking to that specific provider — needed here since Google and
# Cerebras don't share a model namespace. Google gets gemini-3.5-flash
# explicitly; Cerebras gets no override, so the caller's own default
# (openai/gpt-oss-120b) passes straight through — that's exactly what
# Cerebras hosts under that name.
#
# gemini-3.5-flash is a reasoning model with *no way to turn thinking
# off* — confirmed directly against Google's own docs: reasoning cannot
# be disabled for Gemini 3.x models (the thinking_budget=0 trick that
# works on this exact family of bug only applies to Gemini 2.5
# Flash/Flash-Lite). That's a live, undismissed risk: this is the same
# reasoning-leaks-into-content failure family that hit qwen3.5, and that
# NVIDIA's Nemotron 3 Super needed extra_body={"chat_template_kwargs":
# {"enable_thinking": False}} to avoid — except here there's no
# equivalent switch to flip. Cerebras is the fallback if Google's tool
# calling misbehaves.
#
# NVIDIA (nemotron-3-super-120b-a12b, which needed that enable_thinking
# flag to behave — see git history for the exact config) was removed
# from the chain when switching primary/fallback to Google + Cerebras.
# If a reasoning model goes back into this list later, check whether it
# exposes an equivalent off-switch before trusting its tool calls — don't
# re-learn that lesson the hard way a third time.
#
# Leave model_override/extra_body as None for a provider that should just
# use whatever the caller passed in.
#
# Google is primary, Cerebras is the fallback for when Google is
# rate-limited or fails.
_PROVIDER_CONF = [
    ("google", "GOOGLE_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-3.5-flash", None),
    ("cerebras", "CEREBRAS_KEY", "https://api.cerebras.ai/v1", None, None),
]

# Short connect/read timeouts + no SDK-level retries so a dead provider
# fails in seconds instead of the SDK's 600s default (compounded by its own
# exponential-backoff retries) before we fail over to the next one. A long
# timeout only makes the user wait longer for a reply that was never
# coming; fast failure at least surfaces a clear error quickly (or, for a
# provider further down the chain, actually gets tried in time).
#
# Not yet verified: gemini-3.5-flash can't skip its own mandatory
# reasoning pass (see _PROVIDER_CONF above), and if that phase produces no
# streamed bytes at all before the real answer starts, a 30s *read*
# timeout (time between individual chunks, not total response time) could
# fire on a legitimately-thinking-but-silent connection, not just a dead
# one. Watch for this specifically if Google starts timing out more than
# Cerebras ever did as primary.
_PROVIDER_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

# One OpenAI client per provider, built once and reused so switching
# providers doesn't pay a fresh TCP/TLS handshake every time.
_provider_clients = {}

# The provider that answered last — tracked only so _create_completion can
# log when a call actually switches providers. Try order itself always
# follows _PROVIDER_CONF's listed order (first entry tried first, every
# call), not whichever provider happened to work last.
_last_provider = "google"


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
    # bad (e.g. a malformed conversation history). Distinct from "unknown"
    # so the final error message points at the request, not the network.
    if status in (400, 404, 413, 422):
        return "bad_request"
    # The openai SDK never lets a raw httpx timeout/connect error escape —
    # it always wraps it as APIConnectionError (APITimeoutError is a
    # subclass of it) before it reaches us, so checking for the raw httpx
    # types alone never matched a hung/unreachable provider and silently
    # fell through to "unknown". Check both.
    if isinstance(e, APIConnectionError) or isinstance(e, (httpx.TimeoutException, httpx.ConnectError)):
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
    """Try each configured provider in the order listed in _PROVIDER_CONF —
    first entry first, every call — and return (response_or_stream,
    provider_name) from the first that works. Raises AllProvidersFailedError
    if none do."""
    global _last_provider
    failures = []
    for name, env_var, base_url, model_override, extra_body in _PROVIDER_CONF:
        key = os.getenv(env_var)
        if not key or key == "None":
            continue
        call_kwargs = kwargs if model_override is None else {**kwargs, "model": model_override}
        if extra_body:
            call_kwargs = {**call_kwargs, "extra_body": extra_body}
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
        return "No LLM provider is configured. Add an API key in Settings."
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
    """Point the agent's file tools (read_file, list_files, create_file,
    create_page, get_image_description, etc.) at a specific folder — e.g.
    static/<username>/files/ for that user's files. context.md (the agent's
    long-term memory, read/updated via the same file tools) lives in this
    same folder, so pointing static_dir at a user's folder is all that's
    needed to scope both. Creates the folder if it doesn't exist yet."""
    global static_dir, html_dir
    if not path.endswith(os.sep):
        path = path + os.sep
    os.makedirs(path, exist_ok=True)
    static_dir = path
    html_dir = path
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

    Some providers' chat templates reject the whole request the moment a
    system message shows up anywhere but the very front — confirmed on
    NVIDIA's qwen3.5: "Failed to apply prompt template: invalid operation:
    System message must be at the beginning." An older FreeClaw build saved
    two (instructions, then a separate one for context.md); merge them back
    into one here so conversations saved before that fix keep loading and
    working instead of failing this way on every turn from now on."""
    system_parts = [m.get("content", "") for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    if not system_parts:
        return rest
    merged = {"role": "system", "content": "\n\n".join(p for p in system_parts if p)}
    return [merged] + rest


def _heal_history(messages):
    """Repair a conversation so every provider will accept it again.

    OpenAI-compatible APIs reject the entire request if any assistant
    tool_calls entry lacks a matching `tool` response (or a `tool` message
    answers an id nobody declared). A conversation saved by an older
    FreeClaw build — which only ever answered the first of several parallel
    tool calls — is permanently stuck that way: every new turn resends the
    broken history and 400s. Healing on load makes those chats usable
    again: missing tool responses get a placeholder, orphaned ones are
    dropped, and null call ids are backfilled. Also collapses multiple
    system messages into one (see _merge_system_messages)."""
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


def reset(location_innit=location, tts=False):
    global location
    location=location_innit



    global agent_messages
    global tools
    ctx_path = static_dir + "context.md"
    if not os.path.exists(ctx_path):
        with open(ctx_path, "w", encoding="utf-8") as f:
            f.write("")
    with open(ctx_path, "r", encoding="utf-8") as f:
        content = f.read()
    # A single system message, always exactly one and always at index 0:
    # some providers' chat templates (confirmed on NVIDIA's qwen3.5) reject
    # the request outright — "System message must be at the beginning." —
    # the moment a second system-role message shows up anywhere else in the
    # list, which a separate "here's context.md" message used to be. Every
    # eco_messages slice below (and the tool_input branch further down)
    # assumes this is the only header message; if this ever goes back to
    # more than one, all of those need updating too.
    if tts:
        agent_messages=[
            {
                "role": "system",
                "content": f"""
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

            You will be connected to a text-to-speech system, so your responses should be optimized for clear and natural speech.

            Long-term context about the user is stored in context.md, alongside their other files — read/edit it with the normal file tools. Here are its current contents: {content}
            """
            }
        ]
    else:
        agent_messages=[
            {
                "role": "system",
                "content": f"""
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

            Long-term context about the user is stored in context.md, alongside their other files — read/edit it with the normal file tools. Here are its current contents: {content}
            """
            }
        ]

    refresh_tools()


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
                "description": "Deletes a file from /static.",
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
                "name": "edit_file",
                "description": "Edits an existing /static file by replacing one exact string with another. Use instead of create_file for modifying existing content. Use this to edit the context.md file.",
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
    return [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Fetches up-to-date info for real-time queries. Max 2 calls per task — proceed once you have enough, or report back to the user if you still don't after 2 searches.",
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
    parameter = args_dict.get('query') or args_dict.get('site') or args_dict.get('url') or args_dict.get('command') or args_dict.get('filename') or args_dict.get('contents') or args_dict.get('media_id') or None
    print('Agent called tool: '+command_name)
    print('Agent parameter: '+str(parameter) if parameter else ' ')

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
        site = args_dict.get('site') or None
        print('Site: '+site if site else None)
        if site is not None:
            return scraper.get_result(parameter+' - '+site)
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
        filename = os.path.basename(args_dict.get('filename'))
        file_location = static_dir+filename
        try:
            # Read and encode the image to base64
            with open(file_location, "rb") as image_file:
                image_data = base64.b64encode(image_file.read()).decode("utf-8")
        except FileNotFoundError:
            return "File not found."

        # Detect MIME type from file extension
        ext = filename.rsplit(".", 1)[-1].lower()
        mime_types = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
        mime_type = mime_types.get(ext, "image/jpeg")

        vision_client = _client_for("nvidia", os.getenv("NVIDIA_KEY"), "https://integrate.api.nvidia.com/v1")
        completion = vision_client.chat.completions.create(
            model="qwen/qwen3.5-397b-a17b",
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
        return completion.choices[0].message.content or "No description returned."

    if command_name == 'list_files':
        return "Files in static directory: "+", ".join(os.listdir(static_dir))

    if command_name == 'get_date':
        return "Today's date is "+datetime.now().strftime('%B %d, %Y')

    if command_name == 'read_web':
        return scraper.scrape(parameter)

    if command_name == 'create_file':
        filename = args_dict.get('filename')
        if "/" in filename or "\\" in filename:
            return "Invalid filename."
        with open(static_dir+filename, "w", encoding="utf-8") as f:
            f.write(args_dict.get('contents') or '')
        return "Your file is accessable at "+_static_url(static_dir, filename)

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
        updated = contents.replace(old_str, new_str, 1)  # replace only first occurrence
        with open(static_dir + filename, "w", encoding="utf-8") as f:
            f.write(updated)
        return "File edited successfully."

    if command_name == 'create_page':
        filename = args_dict.get('filename')
        if "/" in filename or "\\" in filename:
            return "Invalid filename."
        with open(html_dir+filename, "w", encoding="utf-8") as f:
            f.write(args_dict.get('contents') or '')
        return "Your site is live at "+_static_url(html_dir, filename)

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


def agent_stream(user_input=None, system_input=None,tool_input=None,tool_id=None,tool_name=None):
    """Generator version of the agent loop. Yields small dict events as the
    model produces output, so callers (e.g. the Flask route) can stream
    them to the browser in real time:
      {"type": "token", "text": "..."}            - a chunk of assistant text
      {"type": "tool_call", "name": "...", "arguments": {...}} - tool invocation started
      {"type": "tool_result", "name": "...", "result": "..."}  - tool finished
    The full, final conversation is still available afterwards via
    agent_messages (module-level), same as before.
    """
    global agent_messages
    global scrape
    global tags
    global messages
    global reset
    global tools
    model="openai/gpt-oss-120b"
    temp=1
    check_tools=tools
    if user_input and system_input:
        raise Exception("You cannot have both user input and system input at the same time.")
    elif user_input:

        if user_input.lower() == 'reset':
            reset()
            yield {"type": "token", "text": "Agent reset."}
            return


        intent, certainty = Classy.classify(user_input,location)
        print(intent)
        tag=intent[0]
        



        agent_messages.append(
            {
            "role": "user",
            "content": user_input
        }
        )
        agent_input=user_input

#####################################################################################################################################
        print(tag)
        if tag == 'Greeting/goodbye':
            if len(agent_messages) > 5:
                eco_messages=[agent_messages[0], *agent_messages[-3:]]
            else:
                eco_messages=agent_messages
            model="openai/gpt-oss-20b"
            check_tools=None
        elif tag == 'Personal-question' or  tag == 'Banter' or tag == 'About-user':
            if len(agent_messages) > 7:
                eco_messages=[agent_messages[0], *agent_messages[-5:]]
            else:
                eco_messages=agent_messages
            model="openai/gpt-oss-20b"
            check_tools=None
        elif tag == 'Search':
            temp=0.4
            if len(agent_messages) > 7:
                eco_messages=[agent_messages[0], *agent_messages[-5:]]
            else:
                eco_messages=agent_messages
            check_tools=build_search_tools()

        elif tag == 'Context' or tag == 'Edit':
            if len(agent_messages) > 11:
                eco_messages=[agent_messages[0], *agent_messages[-9:]]
            else:
                eco_messages=agent_messages


        elif tag == 'Coding' or tag == 'Writing' or tag == 'List' or tag == 'Suggest':
            if len(agent_messages) > 9:
                eco_messages=[agent_messages[0], *agent_messages[-7:]]
            else:
                eco_messages=agent_messages


        elif tag == 'Logic' or tag == 'Math' or tag == 'Explain':
            temp=0.2
            if len(agent_messages) > 9:
                eco_messages=[agent_messages[0], *agent_messages[-7:]]
            else:
                eco_messages=agent_messages
        elif tag == 'Utility':
            if len(agent_messages) > 9:
                eco_messages=[agent_messages[0], *agent_messages[-7:]]
            else:
                eco_messages=agent_messages
        else:
            if len(agent_messages) > 9:
                eco_messages=[agent_messages[0], *agent_messages[-7:]]
            else:
                eco_messages=agent_messages

######################################################################################################################################
    elif system_input:
        # No caller in this codebase currently passes system_input (it's
        # kept for direct/external callers of agent()/agent_stream()) — if
        # one starts to, note that appending a second system-role message
        # here breaks the single-leading-system-message invariant reset()
        # now relies on (see the comment there) and will fail the same way
        # on providers that enforce it.
        agent_messages.append(
            {
            "role": "system",
            "content": system_input
        }
        )
        agent_input=system_input
        eco_messages=agent_messages
    # `is not None` (not truthiness): a tool can legitimately return "" —
    # e.g. reading an empty file — and that still has to be recorded as the
    # call's response and continue the turn, not fall through to the
    # "no input" error below with the tool_call left dangling.
    elif tool_input is not None:
        temp=0.2
        yield {"type": "tool_result", "name": tool_name, "result": tool_input}
        agent_messages.append(
            {
            "role": "tool",
            "tool_call_id": tool_id,
            "name": tool_name,
            "content": tool_input
        }
        )
        agent_input=tool_input
        # Find all user message indices
        user_indices = [i for i, m in enumerate(agent_messages) if m['role'] == 'user']
        # Start from 2 user messages ago, or the first user message if there
        # aren't 2. A system-initiated conversation may have no user turns at
        # all — keep everything after the one system message then.
        if len(user_indices) >= 2:
            start_index = user_indices[-2]
        elif user_indices:
            start_index = user_indices[0]
        else:
            start_index = 1
        eco_messages = [agent_messages[0]] + agent_messages[start_index:]
    else:
        raise Exception("You must have either user input or system input.")
    '''
    print('##########################################################################')
    print('\n')
    print(eco_messages)
    print('\n')
    print('##########################################################################')
    '''
    print('Reveived: '+agent_input)
    try:
        stream, provider = _create_completion(
            model=model,
            messages=eco_messages,
            temperature=temp,
            tools=check_tools,
            top_p=1,
            stream=True,
            stop=None
        )
    except AllProvidersFailedError as e:
        # Each provider's full traceback was already logged individually
        # inside _create_completion — this ties them together as one
        # incident so they're easy to find by searching the log.
        logger.error("All providers failed for this turn: %s", e.failures)
        raise Exception(_user_facing_error(e.failures))

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
    print('Agent: '+buffer if buffer else ' ')









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
    elif buffer is not None:
        agent_messages.append(
            {
            "role": "assistant",
            "content": buffer
            }
        )
        print('\n')
    print(agent_messages)
    print('\n')
    return


def agent(user_input=None, system_input=None, tool_input=None, tool_id=None, tool_name=None):
    """Backward-compatible, non-streaming entry point. Drains the
    agent_stream() generator and returns the full conversation, exactly
    like the old synchronous agent() used to."""
    for _ in agent_stream(user_input=user_input, system_input=system_input,
                           tool_input=tool_input, tool_id=tool_id, tool_name=tool_name):
        pass
    return agent_messages


def api_complete(messages, model=None, stream=False, temperature=1.0, max_tokens=None):
    """Stateless LLM call for the OpenAI-compatible API endpoint.
    Does not touch agent_messages. Tries providers in order (starting with
    whichever last succeeded), via the same cached, fast-fail clients as
    agent_stream."""
    kwargs = dict(
        model=model or "openai/gpt-oss-120b",
        messages=messages,
        temperature=temperature,
        top_p=1,
        stream=stream,
        stop=None,
    )
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    result, _ = _create_completion(**kwargs)
    return result


'''
while True:
    output=agent(user_input=input(': '))
    print(output)
    '''