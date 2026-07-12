from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for, session, Response, stream_with_context
import src.agent as agent
import src.mcp_client as mcp_client
from src.users import (
    STATIC_DIR, safe_username, user_dir, conversation_path, conv_files_dir,
    user_context_path, list_users, user_exists, create_user,
    load_conversation, save_conversation, derive_title, ensure_conversation,
    activate_session,
)
import uuid
import json
import re
import time
import threading
import shutil
import functools

from dotenv import load_dotenv, dotenv_values
import os
load_dotenv()
password  = os.getenv("FC_PASSWORD")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
location = BASE_DIR + "/../models/data.pth"

app = Flask(__name__, static_folder=None)

# Persist the session-signing secret across restarts (in a gitignored local
# file) instead of regenerating a random one every run — otherwise every
# restart invalidates existing session cookies, logging everyone out and
# clearing their "current user / current chat" selection, which looks like
# data loss even though nothing on disk actually changed.
_secret_key = os.getenv("SECRET_KEY")
if not _secret_key:
    _secret_key_path = os.path.join(BASE_DIR, ".secret_key")
    try:
        if os.path.exists(_secret_key_path):
            with open(_secret_key_path, "r") as f:
                _secret_key = f.read().strip()
        if not _secret_key:
            _secret_key = os.urandom(24).hex()
            with open(_secret_key_path, "w") as f:
                f.write(_secret_key)
    except OSError as e:
        # Read-only filesystem, permissions issue, etc. — fall back to an
        # in-memory key rather than crashing the whole app at import time.
        print(f"[freeclaw] Warning: couldn't persist secret key to {_secret_key_path} ({e}); "
              f"sessions won't survive a restart. Set SECRET_KEY to fix this.")
        _secret_key = os.urandom(24).hex()
app.secret_key = _secret_key

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), 'templates')
# User/conversation storage (STATIC_DIR, safe_username, list_users, etc. —
# imported above) lives in src/users.py, shared with the CLI, so both entry
# points read and write the exact same on-disk layout.

# A handful of users may legitimately hit /chat at the same moment, and the
# agent module keeps its "active conversation" as module-level globals
# (static_dir, agent_messages, ...) rather than per-request state. This lock
# makes sure one request's agent turn fully finishes (and is persisted to
# disk) before another request is allowed to swap those globals out from
# under it.
agent_lock = threading.Lock()

# NOTE: we deliberately do NOT call agent.reset() here at startup. reset()
# both builds the OpenAI client AND reads/creates a context.md at whatever
# path agent.context_path currently points to — calling it before a user/
# chat has been selected would create a stray context.md directly in
# static/ instead of inside a user's folder. The client + tool list get
# initialized lazily, scoped correctly, the first time ensure_conversation()
# or activate_session() runs (both call set_static_dir/set_context_path
# before reset()).


def logged_in():
    return session.get("authenticated") is True


def create_user_with_context(name, context=None):
    """Used by the agent's create_user tool (registered below via
    agent.set_user_creator) so the assistant itself can spin up new
    FreeClaw users. Raises ValueError with a user-facing message on bad
    input; the agent surfaces that message back to whoever asked."""
    safe_name = safe_username(name)
    if not safe_name:
        raise ValueError("Invalid name — use 1-40 letters, numbers, spaces, - or _.")
    if user_exists(safe_name):
        raise ValueError(f"A user named '{safe_name}' already exists.")
    create_user(safe_name)
    if context and str(context).strip():
        with open(user_context_path(safe_name), "w", encoding="utf-8") as f:
            f.write(str(context).strip() + "\n")
    return safe_name


agent.set_user_creator(create_user_with_context)

# Build the agent's tool list now (built-ins + any MCP servers configured in
# .env) so tools are ready even for the very first request against an existing
# conversation — which doesn't otherwise trigger agent.reset(). A flaky MCP
# server must never stop the app from booting.
try:
    agent.refresh_tools()
except Exception as e:
    print(f"[freeclaw] Warning: couldn't load tools at startup ({e}).")


def current_user():
    name = session.get("current_user")
    if name and user_exists(name):
        return name
    return None


# ── AUTH ROUTES ──────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = False
    if request.method == 'POST':
        if request.form.get('password') == password:
            session.permanent = True
            session['authenticated'] = True
            return redirect(url_for('index'))
        else:
            error = True
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── LANDING PAGE ─────────────────────────────────────────────

@app.route('/')
def index():
    if not logged_in():
        return redirect(url_for('login'))
    return render_template('index.html')


# ── USER / CONVERSATION API ──────────────────────────────────

@app.route('/api/users', methods=['GET'])
def api_list_users():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        users = [{'name': name} for name in list_users()]
        return jsonify({'users': users, 'static_dir': STATIC_DIR})
    except Exception as e:
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500


@app.route('/api/users', methods=['POST'])
def api_create_user():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    name = safe_username(data.get('name', ''))
    if not name:
        return jsonify({'error': 'Invalid name. Use 1-40 letters, numbers, spaces, - or _.'}), 400
    if user_exists(name):
        return jsonify({'error': 'A user with that name already exists.'}), 409
    try:
        create_user(name)
    except Exception as e:
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return jsonify({'name': name})


@app.route('/api/users/<name>', methods=['DELETE'])
def api_delete_user(name):
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    if not user_exists(name):
        return jsonify({'error': 'No such user'}), 404
    with agent_lock:
        shutil.rmtree(user_dir(name), ignore_errors=True)
        # If the deleted user was active in this browser session, clear it
        # so we don't keep pointing at a now-missing conversation.
        if session.get('current_user') == name:
            session.pop('current_user', None)
    return jsonify({'ok': True})


@app.route('/api/conversation', methods=['GET'])
def api_get_conversation():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    name = current_user()
    if not name:
        return jsonify({'error': 'No active conversation'}), 400
    ensure_conversation(name)
    data = load_conversation(name)
    return jsonify({
        'user': name,
        'title': data.get('title'),
        'messages': data.get('messages', [])
    })


# ── CHAT ENTRY POINT ─────────────────────────────────────────

@app.route('/chat', methods=['GET'])
def open_chat():
    """ip:6767/chat?user=Elliot — selects which user's (single) conversation
    subsequent requests in this browser session talk to, then serves the
    chat UI."""
    if not logged_in():
        return redirect(url_for('login'))

    name = safe_username(request.args.get('user', ''))

    if name and user_exists(name):
        session['current_user'] = name
    elif not current_user():
        return redirect(url_for('index'))

    return render_template('chat.html')


@app.route('/chat', methods=['POST'])
def chat():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401

    name = current_user()
    if not name:
        return jsonify({'error': 'No active conversation selected'}), 400

    data = request.get_json()
    user_input = data.get('message', '').strip()
    if not user_input:
        return jsonify({'error': 'Empty message'}), 400

    # Slash-commands stay as quick, plain JSON responses — no need to stream these.
    if user_input.lower() == '/reset':
        with agent_lock:
            activate_session(name)
            agent.reset()
            save_conversation(name, agent.get_messages())
        return jsonify({'response': 'Agent reset successfully'})
    elif user_input.lower() == '/startapi':
        open(_API_FLAG, 'w').close()
        return jsonify({'response': 'API enabled. Use your FreeClaw password as the Bearer token at /v1/chat/completions'})
    elif user_input.lower() == '/stopapi':
        if os.path.exists(_API_FLAG):
            os.remove(_API_FLAG)
        return jsonify({'response': 'API disabled'})

    def generate():
        with agent_lock:
            activate_session(name)
            had_title = False
            try:
                with open(conversation_path(name), "r", encoding="utf-8") as f:
                    had_title = bool(json.load(f).get("title", "") not in (None, "", "New chat"))
            except (OSError, json.JSONDecodeError):
                had_title = False
            try:
                for event in agent.agent_stream(user_input=user_input):
                    yield f"data: {json.dumps(event)}\n\n"
                messages = agent.get_messages()
                title = None if had_title else derive_title(messages)
                save_conversation(name, messages, title=title)
                yield f"data: {json.dumps({'type': 'done', 'conversation': messages})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


@app.route('/reset', methods=['GET', 'POST'])
def reset():
    name = current_user()
    if name:
        with agent_lock:
            activate_session(name)
            agent.reset()
            save_conversation(name, agent.get_messages())
    if request.method == 'POST':
        return jsonify({'response': 'Agent reset successfully'})
    return redirect(url_for('index'))


@app.route('/upload', methods=['POST'])
def upload():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401

    name = current_user()
    if not name:
        return jsonify({'error': 'No active conversation selected'}), 400

    file = request.files.get('file')
    if not file or file.filename == '':
        return jsonify({'error': 'No file provided'}), 400

    # Save into this user's own files folder, preserving extension, with a
    # uuid prefix to avoid collisions.
    ext = os.path.splitext(file.filename)[1]
    safe_name = uuid.uuid4().hex + ext
    dest = os.path.join(conv_files_dir(name), safe_name)
    file.save(dest)

    # Return the path the agent can reference (relative to app root). Use
    # forward slashes explicitly since this is a URL, not an OS file path
    # (os.path.join would emit backslashes on Windows, breaking <img src>).
    rel_path = '/'.join(['static', name, 'files', safe_name])
    return jsonify({'path': rel_path, 'filename': file.filename})


@app.route('/agent/<path:text>')
def serve_template(text):
    if not logged_in():
        return redirect(url_for('login'))
    return render_template(f"{text}.html")


@app.route('/static/<path:filename>')
def serve_static(filename):
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    return send_from_directory(STATIC_DIR, filename)


# ── OPENAI-COMPATIBLE API ────────────────────────────────────

_API_FLAG = os.path.join(BASE_DIR, '.api_enabled')


def api_is_enabled():
    return os.path.exists(_API_FLAG)


def _require_api_auth(f):
    """Decorator: checks Bearer token == FC_PASSWORD and that the API is enabled."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not api_is_enabled():
            return jsonify({"error": {"message": "API is disabled", "type": "api_disabled", "code": 503}}), 503
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return jsonify({"error": {"message": "Missing Bearer token", "type": "invalid_request_error", "code": 401}}), 401
        token = auth[len('Bearer '):]
        if token != password:
            return jsonify({"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": 401}}), 401
        return f(*args, **kwargs)
    return wrapper


@app.route('/api/api-status', methods=['GET'])
def api_get_api_status():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'enabled': api_is_enabled()})


@app.route('/api/api-status', methods=['POST'])
def api_toggle_api():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    enable = data.get('enabled', not api_is_enabled())
    if enable:
        open(_API_FLAG, 'w').close()
    elif os.path.exists(_API_FLAG):
        os.remove(_API_FLAG)
    return jsonify({'enabled': api_is_enabled()})


@app.route('/v1/models', methods=['GET'])
@_require_api_auth
def v1_models():
    return jsonify({
        "object": "list",
        "data": [
            {"id": "freeclaw", "object": "model", "created": 0, "owned_by": "freeclaw"},
            {"id": "openai/gpt-oss-120b", "object": "model", "created": 0, "owned_by": "freeclaw"},
            {"id": "openai/gpt-oss-20b", "object": "model", "created": 0, "owned_by": "freeclaw"},
        ]
    })


@app.route('/v1/chat/completions', methods=['POST'])
@_require_api_auth
def v1_chat_completions():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}), 400

    messages = data.get('messages')
    if not messages or not isinstance(messages, list):
        return jsonify({"error": {"message": "messages field is required", "type": "invalid_request_error"}}), 400

    req_model = data.get('model', 'openai/gpt-oss-120b')
    stream = bool(data.get('stream', False))
    temperature = float(data.get('temperature', 1.0))
    max_tokens = data.get('max_tokens')
    completion_id = 'chatcmpl-' + uuid.uuid4().hex[:12]
    created = int(time.time())

    try:
        result = agent.api_complete(
            messages=messages,
            model=req_model,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except agent.AllProvidersFailedError as e:
        reasons = {r for _, r, _ in e.failures}
        status = 429 if reasons == {"rate_limited"} else 500
        err_type = "rate_limit_error" if reasons == {"rate_limited"} else "server_error"
        return jsonify({"error": {"message": agent._user_facing_error(e.failures), "type": err_type}}), status
    except Exception as e:
        return jsonify({"error": {"message": str(e), "type": "server_error"}}), 500

    if stream:
        def generate():
            try:
                for chunk in result:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    finish = chunk.choices[0].finish_reason
                    payload = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": req_model,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": delta.content or ""} if getattr(delta, "content", None) else {},
                            "finish_reason": finish,
                        }]
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                yield "data: [DONE]\n\n"
            except agent.AllProvidersFailedError as e:
                yield f"data: {json.dumps({'type': 'error', 'error': agent._user_facing_error(e.failures)})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
        )
    else:
        choice = result.choices[0]
        content = choice.message.content or ""
        usage = result.usage
        return jsonify({
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": req_model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": choice.finish_reason or "stop",
            }],
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                "completion_tokens": getattr(usage, "completion_tokens", 0),
                "total_tokens": getattr(usage, "total_tokens", 0),
            } if usage else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })


# ── SETTINGS (env file) ──────────────────────────────────────

# Known .env keys shown in the settings UI, in display order.
SETTINGS_KEYS = [
    ("FC_PASSWORD",      "Login Password",          False),
    ("SECRET_KEY",       "Session Secret Key",      False),
    ("API_KEY",          "Groq API Key",            True),
    ("NVIDIA_KEY",       "NVIDIA API Key",          True),
    ("OPENROUTER_KEY",   "OpenRouter API Key",      True),
    ("CUSTOM_DOMAIN",    "Custom Domain",           False),
]
KNOWN_KEYS = {k for k, _, _ in SETTINGS_KEYS}

def _env_path():
    """Return path to .env file two directories up from Flask/."""
    return os.path.join(os.path.dirname(BASE_DIR), '.env')


def _read_env():
    """Read the .env file and return a dict of key→value."""
    path = _env_path()
    if not os.path.exists(path):
        return {}
    return dict(dotenv_values(path))


def _write_env(updates: dict):
    """Write only the known keys back into the .env file, preserving unknown lines."""
    path = _env_path()
    # Read existing lines
    lines = []
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

    written = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('#') or '=' not in stripped:
            new_lines.append(line)
            continue
        key = stripped.split('=', 1)[0].strip()
        if key in updates:
            new_lines.append(f'{key}={updates[key]}\n')
            written.add(key)
        else:
            new_lines.append(line)

    # Append any new keys not already in the file
    for key, value in updates.items():
        if key not in written:
            new_lines.append(f'{key}={value}\n')

    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

    # Also update the live process environment so a key added/changed here
    # (e.g. NVIDIA_KEY) is picked up by the LLM provider fallback on the
    # very next request, without restarting the app.
    for key, value in updates.items():
        os.environ[key] = value


@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    current = _read_env()
    result = []
    for key, label, is_secret in SETTINGS_KEYS:
        result.append({
            'key': key,
            'label': label,
            'value': current.get(key, ''),
            'secret': is_secret,
        })
    return jsonify({'settings': result})


@app.route('/api/settings', methods=['POST'])
def api_update_settings():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    updates = {k: str(v) for k, v in data.items() if k in KNOWN_KEYS}
    if not updates:
        return jsonify({'error': 'No valid keys provided'}), 400
    try:
        _write_env(updates)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


# ── MCP SERVERS (env-backed parallel lists) ──────────────────

# Characters that would break the single-quote-wrapped JSON we store in .env,
# or python-dotenv's parsing. Rejected on input so the round-trip is safe.
_MCP_BAD_CHARS = ("'", '"', '\n', '\r')


def _mcp_server_public(s):
    """Shape a stored server for the client. The token is write-only — we only
    report whether one is set, never echo it back."""
    return {
        'name': s.get('name', ''),
        'url': s.get('url', ''),
        'has_token': bool((s.get('token') or '').strip()),
    }


@app.route('/api/mcp', methods=['GET'])
def api_list_mcp():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        servers = mcp_client.read_servers()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'servers': [_mcp_server_public(s) for s in servers]})


@app.route('/api/mcp', methods=['POST'])
def api_add_mcp():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip()
    url = str(data.get('url', '')).strip()
    token = str(data.get('token', '')).strip()

    if not name or not url:
        return jsonify({'error': 'A name and URL are both required.'}), 400
    if not re.match(r'^https?://', url, re.IGNORECASE):
        return jsonify({'error': 'URL must start with http:// or https://.'}), 400
    for field, val in (('name', name), ('URL', url), ('token', token)):
        if any(c in val for c in _MCP_BAD_CHARS):
            return jsonify({'error': f'The {field} contains unsupported characters (quotes or newlines).'}), 400

    with agent_lock:
        servers = mcp_client.read_servers()
        if any(s.get('name') == name for s in servers):
            return jsonify({'error': f"An MCP server named '{name}' already exists."}), 409
        servers.append({'name': name, 'url': url, 'token': token})
        try:
            _write_env(mcp_client.servers_to_env(servers))
        except Exception as e:
            return jsonify({'error': f'Could not save: {e}'}), 500

        # Verify the server is reachable and pick up its tool count now, so
        # the user gets immediate feedback instead of a silent no-op.
        mcp_client.clear_cache()
        error = None
        tool_count = 0
        try:
            tool_count = len(mcp_client.list_tools({'name': name, 'url': url, 'token': token}))
        except Exception as e:
            error = str(e)
        agent.refresh_tools()

    resp = {'ok': True, 'servers': [_mcp_server_public(s) for s in servers], 'tool_count': tool_count}
    if error:
        resp['warning'] = f"Saved, but couldn't reach the server yet: {error}"
    return jsonify(resp)


@app.route('/api/mcp/<name>', methods=['DELETE'])
def api_delete_mcp(name):
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    with agent_lock:
        servers = mcp_client.read_servers()
        remaining = [s for s in servers if s.get('name') != name]
        if len(remaining) == len(servers):
            return jsonify({'error': 'No such MCP server'}), 404
        try:
            _write_env(mcp_client.servers_to_env(remaining))
        except Exception as e:
            return jsonify({'error': f'Could not save: {e}'}), 500
        mcp_client.clear_cache()
        agent.refresh_tools()
    return jsonify({'ok': True, 'servers': [_mcp_server_public(s) for s in remaining]})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=6767, debug=True)