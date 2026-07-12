import json
import re
import types
import Classy
from openai import OpenAI
import subprocess
import shlex
import src.scraper as scraper
from datetime import datetime
from json_repair import repair_json
import src.mcp_client as mcp_client
import os


import base64
import mimetypes
import httpx

 


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

html_dir=BASE_DIR+'/../Flask/static/'
static_dir=BASE_DIR+'/../Flask/static/'
context_path=BASE_DIR+'/../Flask/static/context.md'
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

# LLM provider fallback chain: (name, env_var, base_url), tried in order.
# The key is looked up from the environment fresh on every call (not
# captured here) so a key added or changed via /api/settings takes effect
# immediately, without restarting the process. A provider with no key
# configured is skipped.
_PROVIDER_CONF = [
    ("groq", "API_KEY", "https://api.groq.com/openai/v1"),
    ("nvidia", "NVIDIA_KEY", "https://integrate.api.nvidia.com/v1"),
    ("openrouter", "OPENROUTER_KEY", "https://openrouter.ai/api/v1"),
]

# Short connect/read timeouts + no SDK-level retries so a dead provider
# fails in seconds instead of the SDK's 600s default (compounded by its own
# exponential-backoff retries) before we fail over to the next one.
_PROVIDER_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

# One OpenAI client per provider, built once and reused so switching
# providers doesn't pay a fresh TCP/TLS handshake every time.
_provider_clients = {}

# The provider that answered last, tried first on the next call so a
# working provider doesn't get re-probed (and potentially fail over) on
# every single turn.
_last_provider = "groq"


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
    if isinstance(e, (httpx.TimeoutException, httpx.ConnectError)):
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
    """Try each configured provider in turn — starting with whichever one
    last succeeded — and return (response_or_stream, provider_name) from the
    first that works. Raises AllProvidersFailedError if none do."""
    global _last_provider
    names = [p[0] for p in _PROVIDER_CONF]
    order = [_last_provider] + [n for n in names if n != _last_provider]
    conf = {n: (env_var, base_url) for n, env_var, base_url in _PROVIDER_CONF}
    failures = []
    for name in order:
        env_var, base_url = conf[name]
        key = os.getenv(env_var)
        if not key or key == "None":
            continue
        try:
            c = _client_for(name, key, base_url)
            result = c.chat.completions.create(**kwargs)
            if name != _last_provider:
                print(f"LLM provider switched: {_last_provider} -> {name}")
            _last_provider = name
            return result, name
        except Exception as e:
            failures.append((name, _classify_error(e), str(e)))
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
    return "All providers failed: " + ", ".join(f"{name} ({reason})" for name, reason, _ in failures)

# Maps the sanitized function name we expose to the model (e.g.
# "mcp_github_create_issue") back to the (server, real tool name) needed to
# actually invoke it. Rebuilt by load_mcp_tools().
mcp_tool_registry = {}


def set_static_dir(path):
    """Point the agent's file tools (read_file, list_files, create_file,
    create_page, get_image_description, etc.) at a specific folder — e.g.
    static/<username>/conversations/<conv_id>/ for a single chat's files.
    Creates the folder if it doesn't exist yet."""
    global static_dir, html_dir
    if not path.endswith(os.sep):
        path = path + os.sep
    os.makedirs(path, exist_ok=True)
    static_dir = path
    html_dir = path
    return static_dir


def set_context_path(path):
    """Point save_context/read_context (the agent's long-term, cross-chat
    memory) at a specific context.md file — independent of static_dir, so
    the same user's memory persists across all of their separate chats.
    Creates the file (and its parent folder) if missing."""
    global context_path
    context_path = path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
    return context_path


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


def set_messages(messages):
    """Load a previously-saved conversation (a plain list of OpenAI-style
    message dicts) as the active conversation for subsequent agent_stream
    calls."""
    global agent_messages
    agent_messages = messages


def reset(location_innit=location, tts=False):
    global location
    location=location_innit



    global agent_messages
    global tools
    if not os.path.exists(context_path):
        os.makedirs(os.path.dirname(context_path), exist_ok=True)
        with open(context_path, "w", encoding="utf-8") as f:
            f.write("")
    with open(context_path, "r", encoding="utf-8") as f:
        content = f.read()
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

            Update your context file with important information that you may need later.

            You will be connected to a text-to-speech system, so your responses should be optimized for clear and natural speech.
            """
            },
            {
            "role": "system",
            "content": f"Context about the user is stored in context.md. Here are the contents of that file: {content}"
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

            Update your context file with important information that you may need later.
            """
            },
            {
            "role": "system",
            "content": f"Context about the user is stored in context.md. Here are the contents of that file: {content}"
            }
        ]

    refresh_tools()


def build_base_tools():
    """Build the list of built-in tool definitions (OpenAI function-calling
    schema). Kept separate from reset() so the full tool list can be rebuilt
    at any time — e.g. after the MCP server list changes — without touching
    the conversation or the LLM client."""
    best_sites = [
        {
            "weather": ["localconditions.com"],
            "news": ["bbc.com", "atoztimes.com"]
        }
    ]

    return [
        {
            "type": "function",
            "function": {
                "name": "save_context",
                "description": "Stores long-term user information such as identity details, preferences, habits, and persistent instructions for future sessions. Use only for durable facts the user expects the assistant to remember across conversations. Do not store temporary context, or one-time information.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contents": { "type": "string", "description": "Appends a new entry for context.md" }
                    },
                    "required": ["contents"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_context",
                "description": "Shows contents of the context.md file.",
                "parameters": { "type": "object", "properties": {} }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "create_user",
                "description": "Creates a brand new FreeClaw user with their own folder, chats, and long-term memory. Only use this when the person explicitly asks to add/create a new user — never to switch context for the current conversation.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": { "type": "string", "description": "The new user's name. Used as their folder name, so keep it short and simple (letters, numbers, spaces, - or _)." },
                        "context": { "type": ["string", "null"], "description": "Optional starting content for the new user's context.md (long-term memory) — e.g. known preferences or background info. Leave null/omit for a blank context.md." }
                    },
                    "required": ["name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Fetches validated, up-to-date information for real-time queries. Only call this tool a maximum of 2 times per task — if you have sufficient data after 1-2 searches, proceed to the next step immediately. If you don't have the sufficient data, report back to the user after a maximium of 2 searches. Here is a website guide: "+str(best_sites),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": { "type": "string", "description": "The natural language query to answer" },
                        "site": { "type": ["string","null"], "description": "The site to be searched or None" }
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "See the contents of a file that is in the /static directory.",
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
                "description": "Lists the files in the /static directory.",
                "parameters": { "type": "object", "properties": {} }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_date",
                "description": "Returns the current date.",
                "parameters": { "type": "object", "properties": {} }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "create_page",
                "description": "Creates an HTML page for the user to see",
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
                "description": "Creates interoperable outputs for other applications and systems. Use for documents, data exports, scripts, configurations, automation artifacts, and other task-completing file outputs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": { "type": "string", "description": "name_of_your_file.something" },
                        "contents": { "type": "string", "description": "Contents of file, can leave blank" }
                    },
                    "required": ["filename","contents"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "delete_file",
                "description": "Deletes a file in the /static directory. Use this command to delete files that are no longer needed, or if the user asks you to delete a file.",
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
                "description": "Edits an existing file in the /static directory by replacing a specific string with a new one. Use this instead of create_file when modifying existing content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": { "type": "string", "description": "Name of the file to edit" },
                        "old_str": { "type": "string", "description": "The exact string to find and replace" },
                        "new_str": { "type": "string", "description": "The string to replace it with" }
                    },
                    "required": ["filename", "old_str", "new_str"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_image_description",
                "description": "Returns a very detailed description of an image in the static folder.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": { "type": "string", "description": "name_of_your_image.something" }
                    },
                    "required": ["filename"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_web",
                "description": "Returns the first 3000 English characters of a webpage. Only use this if the user tells you to specifically look at a webpage.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": { "type": "string", "description": "URL of the webpage to read" }
                    },
                    "required": ["url"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "open_url",
                "description": "Allows you to open a URL or URI on the user's machine. Use this to open webpages or apps with a URI. Also use it for other URI tools such as texting or calling.",
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
                "description": "Allows you to run commands directly on this machine. If a user asks you to do something, immediately create a command and run it. Don't run multiple commands without reporting back to the user.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": { "type": "string", "description": "BASH Command" }
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
        try:
            server_tools = mcp_client.list_tools(server)
        except Exception as e:
            print(f"[mcp] '{server.get('name')}' unavailable: {e}")
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
    tools = build_base_tools() + load_mcp_tools()
    return tools


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
                eco_messages=[agent_messages[0], agent_messages[1], *agent_messages[-3:]]
            else:
                eco_messages=agent_messages
            model="openai/gpt-oss-20b"
            #check_tools=None
        elif tag == 'Personal-question' or  tag == 'Banter' or tag == 'About-user':
            if len(agent_messages) > 7:
                eco_messages=[agent_messages[0], agent_messages[1], *agent_messages[-5:]]
            else:
                eco_messages=agent_messages
            model="openai/gpt-oss-20b"
            #check_tools=None
        elif tag == 'Search':
            temp=0.4
            if len(agent_messages) > 7:
                eco_messages=[agent_messages[0], agent_messages[1], *agent_messages[-5:]]
            else:
                eco_messages=agent_messages


        elif tag == 'Context' or tag == 'Edit':
            if len(agent_messages) > 11:
                eco_messages=[agent_messages[0], agent_messages[1], *agent_messages[-9:]]
            else:
                eco_messages=agent_messages


        elif tag == 'Coding' or tag == 'Writing' or tag == 'List' or tag == 'Suggest':
            if len(agent_messages) > 9:
                eco_messages=[agent_messages[0], agent_messages[1], *agent_messages[-7:]]
            else:
                eco_messages=agent_messages


        elif tag == 'Logic' or tag == 'Math' or tag == 'Explain':
            temp=0.2
            if len(agent_messages) > 9:
                eco_messages=[agent_messages[0], agent_messages[1], *agent_messages[-7:]]
            else:
                eco_messages=agent_messages
        elif tag == 'Utility':
            if len(agent_messages) > 9:
                eco_messages=[agent_messages[0], agent_messages[1], *agent_messages[-7:]]
            else:
                eco_messages=agent_messages
        else:
            if len(agent_messages) > 9:
                eco_messages=[agent_messages[0], agent_messages[1], *agent_messages[-7:]]
            else:
                eco_messages=agent_messages

######################################################################################################################################
    elif system_input:
        agent_messages.append(
            {
            "role": "system",
            "content": system_input
        }
        )
        agent_input=system_input
        eco_messages=agent_messages
    elif tool_input:
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
        # Start from 2 user messages ago, or the first user message if there aren't 2
        start_index = user_indices[-2] if len(user_indices) >= 2 else user_indices[0]
        eco_messages = [agent_messages[0], agent_messages[1]] + agent_messages[start_index:]
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
        raise Exception(_user_facing_error(e.failures))

    # Consume the stream, forwarding text chunks to the caller in real
    # time and reassembling any tool calls (which always arrive as
    # incremental argument-string fragments when streamed).
    buffer = ""
    tool_calls_acc = {}
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

    tool_calls_list = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())] if tool_calls_acc else None
    print('Agent: '+buffer if buffer else ' ')









    if tool_calls_list:

        agent_messages.append(
            {
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
        )
    

        
        tool_call = types.SimpleNamespace(id=tool_calls_list[0]["id"])
        command_name = tool_calls_list[0]["function"]["name"]
        tool_args = tool_calls_list[0]["function"]["arguments"]  # JSON string

        fixed_tool_args = repair_json(tool_args)

        args_dict = json.loads(fixed_tool_args)
        # MCP (and malformed) tool calls can arrive with a non-object payload;
        # keep the rest of the dispatch, which assumes a dict, crash-free.
        if not isinstance(args_dict, dict):
            args_dict = {}
        parameter = args_dict.get('query') or args_dict.get('site') or args_dict.get('url') or args_dict.get('command') or args_dict.get('filename') or args_dict.get('contents') or args_dict.get('media_id') or None
        print('Agent called tool: '+command_name)
        print('Agent parameter: '+str(parameter) if parameter else ' ')
        yield {"type": "tool_call", "name": command_name, "arguments": args_dict}
        if command_name == 'save_context':
            contents=args_dict.get('contents')
            with open(context_path, "a", encoding="utf-8") as f:
                f.write(contents.strip()+'\n')
            yield from agent_stream(tool_input="Context saved.", tool_id=tool_call.id,tool_name=command_name)
        elif command_name == 'read_context':
            with open(context_path, "r", encoding="utf-8") as f:
                content = f.read()
            yield from agent_stream(tool_input=content, tool_id=tool_call.id,tool_name=command_name)
        elif command_name == 'create_user':
            new_name = args_dict.get('name')
            new_context = args_dict.get('context')
            if not new_name or not str(new_name).strip():
                result = "Error: a name is required to create a user."
            elif user_creator is None:
                result = "Error: user creation isn't available in this context."
            else:
                try:
                    created_name = user_creator(new_name, new_context)
                    result = f"User '{created_name}' created successfully."
                    if new_context:
                        result += " Their context.md was set with the provided content."
                except Exception as e:
                    result = f"Error creating user: {e}"
            yield from agent_stream(tool_input=result, tool_id=tool_call.id,tool_name=command_name)
        elif command_name == 'search':


            query=args_dict.get('query')

            site=args_dict.get('site') or None
            print('Site: '+site if site else None)
            if site is not None:
                web_data=scraper.get_result(parameter+' - '+site)
            else:
                web_data=scraper.get_result(parameter)
            #print(web_data)


            '''
            s_messages=[{"role": "system", "content": "Query: "+parameter+".The following data has been scraped from a website, and your job is to clean up and structure the following data, answering the query. Only include information closely related to the query. Respond in full sentences."+web_data[0]}]


            stream = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=s_messages,
                stream=True
            )
            report=''
            for chunk in stream:
                report+=chunk.choices[0].delta.content or ""

            '''
            
            #yield from agent_stream(tool_input=report+" - "+web_data[1], tool_id=tool_call.id,tool_name=command_name)
            yield from agent_stream(tool_input=web_data, tool_id=tool_call.id,tool_name=command_name)
        elif command_name == 'read_file':
            filename=args_dict.get('filename')
            # Uploaded files are referenced by their full "static/..." path
            # in the chat tag, not a bare filename; take just the basename
            # so both forms resolve against this session's static_dir.
            filename=os.path.basename(filename)
            try:
                with open(static_dir+filename, "r", encoding="utf-8") as f:
                    file_contents = f.read()
                yield from agent_stream(tool_input=file_contents, tool_id=tool_call.id,tool_name=command_name)
            except FileNotFoundError:
                yield from agent_stream(tool_input="File not found.", tool_id=tool_call.id,tool_name=command_name)
            return

        elif command_name == 'get_image_description':
            filename=args_dict.get('filename')
            filename=os.path.basename(filename)
            file_location=static_dir+filename

            try:
                # Read and encode the image to base64
                with open(file_location, "rb") as image_file:
                    image_data = base64.b64encode(image_file.read()).decode("utf-8")
            except FileNotFoundError:
                yield from agent_stream(tool_input="File not found.", tool_id=tool_call.id,tool_name=command_name)
                return


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

            description=completion.choices[0].message.content
            yield from agent_stream(tool_input=description, tool_id=tool_call.id,tool_name=command_name)

        elif command_name == 'list_files':
            files = os.listdir(static_dir)
            yield from agent_stream(tool_input="Files in static directory: "+", ".join(files), tool_id=tool_call.id,tool_name=command_name)
        elif command_name == 'get_date':
            current_date=datetime.now().strftime('%B %d, %Y')
            yield from agent_stream(tool_input="Today's date is "+current_date, tool_id=tool_call.id,tool_name=command_name)
        elif command_name == 'read_web':
            site_data=scraper.scrape(parameter)
            yield from agent_stream(tool_input=site_data, tool_id=tool_call.id,tool_name=command_name)

        elif command_name == 'create_file':

            filename=args_dict.get('filename')
            if "/" in filename or "\\" in filename:
                yield from agent_stream(tool_input="Invalid filename.", tool_id=tool_call.id,tool_name=command_name)
            contents=args_dict.get('contents')
            with open(static_dir+filename, "w", encoding="utf-8") as f:
                f.write(contents)
            yield from agent_stream(tool_input="Your file is accessable at "+_static_url(static_dir, filename), tool_id=tool_call.id,tool_name=command_name)
        

        elif command_name == 'delete_file':
            filename=args_dict.get('filename')
            if "/" in filename or "\\" in filename:
                yield from agent_stream(tool_input="Invalid filename.", tool_id=tool_call.id,tool_name=command_name)
            file_path = static_dir + filename
            if os.path.exists(file_path):
                os.remove(file_path)
                yield from agent_stream(tool_input="File deleted.", tool_id=tool_call.id,tool_name=command_name)
            else:
                yield from agent_stream(tool_input="File not found.", tool_id=tool_call.id,tool_name=command_name)
        elif command_name == 'edit_file':
            filename = args_dict.get('filename')
            if "/" in filename or "\\" in filename:
                yield from agent_stream(tool_input="Invalid filename.", tool_id=tool_call.id, tool_name=command_name)
            old_str = args_dict.get('old_str')
            new_str = args_dict.get('new_str')
            try:
                with open(static_dir + filename, "r", encoding="utf-8") as f:
                    contents = f.read()
                if old_str not in contents:
                    yield from agent_stream(tool_input="String not found in file.", tool_id=tool_call.id, tool_name=command_name)
                updated = contents.replace(old_str, new_str, 1)  # replace only first occurrence
                with open(static_dir + filename, "w", encoding="utf-8") as f:
                    f.write(updated)
                yield from agent_stream(tool_input="File edited successfully.", tool_id=tool_call.id, tool_name=command_name)
            except FileNotFoundError:
                yield from agent_stream(tool_input="File not found.", tool_id=tool_call.id, tool_name=command_name)
            
        elif command_name == 'create_page':

            filename=args_dict.get('filename')
            if "/" in filename or "\\" in filename:
                yield from agent_stream(tool_input="Invalid filename.", tool_id=tool_call.id,tool_name=command_name)
            contents=args_dict.get('contents')
            with open(html_dir+filename, "w", encoding="utf-8") as f:
                f.write(contents)
            yield from agent_stream(tool_input="Your site is live at "+_static_url(html_dir, filename), tool_id=tool_call.id,tool_name=command_name)
        
#        elif command_name == 'List_Home_Assistant_Devices':
#            devices=home_assistant.get_entities()
#            yield from agent_stream(tool_input=str(devices), tool_id=tool_call.id,tool_name=command_name)
#        
#        elif command_name == 'Home_Assistant':
#            domain=args_dict.get('domain')
#            service=args_dict.get('service')
#            data=args_dict.get('data')
#            output=home_assistant.execute_action(domain, service, data)
#            yield from agent_stream(tool_input=output, tool_id=tool_call.id,tool_name=command_name)
        elif command_name == 'open_url':
            # Actually opening the tab happens client-side — the frontend
            # listens for the "tool_call" SSE event (which already carries
            # this url in evt.arguments) and calls window.open() on it.
            yield from agent_stream(tool_input="URL opened: "+args_dict.get('url', ''), tool_id=tool_call.id,tool_name=command_name)
        elif command_name == 'run_bash_command':
            print(parameter)
            run_command='y'
            if run_command.lower() == 'y':
                proc = subprocess.Popen(
                    f'{parameter}',
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    shell=True
                )
                stdout, stderr = proc.communicate()
                print(stdout,stderr)
                output=(stdout + "\n" + stderr).strip()
            else:
                output='User denied command'
            if output is None or output == '':
                output='Command was run successfully, Report back to the user.'
            print(output)
            yield from agent_stream(tool_input=output, tool_id=tool_call.id,tool_name=command_name)
        elif command_name in mcp_tool_registry:
            entry = mcp_tool_registry[command_name]
            server = entry["server"]
            try:
                result = mcp_client.call_tool(server, entry["tool"], args_dict)
            except Exception as e:
                result = f"Error calling MCP tool '{entry['tool']}' on '{server.get('name')}': {e}"
            yield from agent_stream(tool_input=result, tool_id=tool_call.id, tool_name=command_name)
        else:
            # Unknown tool (e.g. an MCP server that was removed after the
            # model decided to call it) — reply so the tool_call isn't left
            # dangling, which would break the next turn.
            yield from agent_stream(tool_input="Unknown tool: " + command_name, tool_id=tool_call.id, tool_name=command_name)
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