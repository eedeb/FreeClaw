from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for, session, Response, stream_with_context
import src.agent as agent
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
STATIC_DIR    = os.path.join(os.path.dirname(__file__), 'static')
os.makedirs(STATIC_DIR, exist_ok=True)

CONVERSATIONS_SUBDIR = "conversations"
RESERVED_NAMES = {"conversations", "uploads"}

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
# initialized lazily, scoped correctly, the first time new_conversation()
# or activate_session() runs (both call set_static_dir/set_context_path
# before reset()).


def logged_in():
    return session.get("authenticated") is True


# ── User / conversation storage helpers ─────────────────────

def safe_username(name):
    """Restrict usernames to something that's safe to use as a folder name
    and can't escape the static/ directory or collide with reserved paths."""
    if not name:
        return None
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\- ]{1,40}", name):
        return None
    if name.lower() in RESERVED_NAMES:
        return None
    return name


def user_dir(name):
    return os.path.join(STATIC_DIR, name) + os.sep


def conversations_dir(name):
    path = os.path.join(user_dir(name), CONVERSATIONS_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def conversation_path(name, conv_id):
    return os.path.join(conversations_dir(name), conv_id + ".json")


def conv_files_dir(name, conv_id):
    """Folder where this specific chat's created/uploaded files live, e.g.
    static/<user>/conversations/<conv_id>/. Kept separate from the
    conversation's metadata JSON file."""
    path = os.path.join(conversations_dir(name), conv_id)
    os.makedirs(path, exist_ok=True)
    return path


def user_context_path(name):
    return os.path.join(user_dir(name), "context.md")


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


def list_users():
    if not os.path.isdir(STATIC_DIR):
        return []
    users = []
    for entry in sorted(os.listdir(STATIC_DIR)):
        if entry.lower() in RESERVED_NAMES or entry.startswith('.'):
            continue
        full = os.path.join(STATIC_DIR, entry)
        try:
            if os.path.isdir(full):
                users.append(entry)
        except OSError:
            continue
    return users


def user_exists(name):
    return name in list_users()


def create_user(name):
    os.makedirs(user_dir(name), exist_ok=True)
    ctx_path = os.path.join(user_dir(name), "context.md")
    if not os.path.exists(ctx_path):
        with open(ctx_path, "w", encoding="utf-8") as f:
            f.write("")
    os.makedirs(conversations_dir(name), exist_ok=True)


def list_conversations(name):
    convs = []
    cdir = conversations_dir(name)
    for fname in os.listdir(cdir):
        if not fname.endswith(".json"):
            continue
        conv_id = fname[:-5]
        try:
            with open(os.path.join(cdir, fname), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        convs.append({
            "id": conv_id,
            "title": data.get("title") or "New chat",
            "updated_at": data.get("updated_at", 0)
        })
    convs.sort(key=lambda c: c["updated_at"], reverse=True)
    return convs


def load_conversation(name, conv_id):
    path = conversation_path(name, conv_id)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_conversation(name, conv_id, messages, title=None):
    path = conversation_path(name, conv_id)
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}
    if title is not None:
        data["title"] = title
    elif "title" not in data:
        data["title"] = "New chat"
    data["messages"] = messages
    data["updated_at"] = time.time()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def derive_title(messages):
    for m in messages:
        if m.get("role") == "user":
            text = m.get("content")
            if isinstance(text, list):
                text = " ".join(b.get("text", "") for b in text if isinstance(b, dict))
            text = (text or "").strip()
            if text:
                return text[:60]
    return "New chat"


def new_conversation(name):
    """Creates a fresh conversation for `name`, builds its system-prompt
    messages via agent.reset() (file tools scoped to this chat's own
    folder, long-term memory scoped to the user's context.md), and
    persists it to disk. Returns the new conversation id."""
    conv_id = uuid.uuid4().hex[:12]
    agent.set_static_dir(conv_files_dir(name, conv_id))
    agent.set_context_path(user_context_path(name))
    agent.reset()
    save_conversation(name, conv_id, agent.get_messages(), title="New chat")
    return conv_id


def activate_session(name, conv_id):
    """Point the agent module's globals at this chat's file folder and
    this user's long-term context.md, and load this conversation's saved
    messages so the next agent_stream() call continues the right thread
    of conversation."""
    agent.set_static_dir(conv_files_dir(name, conv_id))
    agent.set_context_path(user_context_path(name))
    data = load_conversation(name, conv_id)
    agent.set_messages(data.get("messages", []))


def current_user():
    name = session.get("current_user")
    if name and user_exists(name):
        return name
    return None


def current_conv():
    name = current_user()
    conv_id = session.get("current_conv")
    if name and conv_id and os.path.exists(conversation_path(name, conv_id)):
        return conv_id
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
        users = []
        for name in list_users():
            users.append({'name': name, 'chat_count': len(list_conversations(name))})
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
            session.pop('current_conv', None)
    return jsonify({'ok': True})


@app.route('/api/users/<name>/conversations', methods=['GET'])
def api_list_conversations(name):
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    if not user_exists(name):
        return jsonify({'error': 'No such user'}), 404
    return jsonify({'conversations': list_conversations(name)})


@app.route('/api/users/<name>/conversations', methods=['POST'])
def api_create_conversation(name):
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    if not user_exists(name):
        return jsonify({'error': 'No such user'}), 404
    with agent_lock:
        conv_id = new_conversation(name)
    return jsonify({'id': conv_id})


@app.route('/api/users/<name>/conversations/<conv_id>', methods=['DELETE'])
def api_delete_conversation(name, conv_id):
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    if not user_exists(name):
        return jsonify({'error': 'No such user'}), 404
    path = conversation_path(name, conv_id)
    if not os.path.exists(path):
        return jsonify({'error': 'No such chat'}), 404
    with agent_lock:
        # Remove the metadata file and this chat's own files/ folder.
        os.remove(path)
        shutil.rmtree(conv_files_dir(name, conv_id), ignore_errors=True)
        if session.get('current_user') == name and session.get('current_conv') == conv_id:
            session.pop('current_conv', None)
    return jsonify({'ok': True})


@app.route('/api/conversation', methods=['GET'])
def api_get_conversation():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    name, conv_id = current_user(), current_conv()
    if not name or not conv_id:
        return jsonify({'error': 'No active conversation'}), 400
    data = load_conversation(name, conv_id)
    return jsonify({
        'user': name,
        'id': conv_id,
        'title': data.get('title'),
        'messages': data.get('messages', [])
    })


# ── CHAT ENTRY POINT ─────────────────────────────────────────

@app.route('/chat', methods=['GET'])
def open_chat():
    """ip:6767/chat?user=Elliot&conv=<id> — selects which agent/conversation
    subsequent requests in this browser session talk to, then serves the
    chat UI."""
    if not logged_in():
        return redirect(url_for('login'))

    name = safe_username(request.args.get('user', ''))
    conv_id = request.args.get('conv', '')

    if name and user_exists(name) and conv_id and os.path.exists(conversation_path(name, conv_id)):
        session['current_user'] = name
        session['current_conv'] = conv_id
    elif not current_user() or not current_conv():
        return redirect(url_for('index'))

    return render_template('chat.html')


@app.route('/chat', methods=['POST'])
def chat():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401

    name, conv_id = current_user(), current_conv()
    if not name or not conv_id:
        return jsonify({'error': 'No active conversation selected'}), 400

    data = request.get_json()
    user_input = data.get('message', '').strip()
    if not user_input:
        return jsonify({'error': 'Empty message'}), 400

    # Slash-commands stay as quick, plain JSON responses — no need to stream these.
    if user_input.lower() == '/reset':
        with agent_lock:
            activate_session(name, conv_id)
            agent.reset()
            save_conversation(name, conv_id, agent.get_messages())
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
            activate_session(name, conv_id)
            had_title = False
            try:
                with open(conversation_path(name, conv_id), "r", encoding="utf-8") as f:
                    had_title = bool(json.load(f).get("title", "") not in (None, "", "New chat"))
            except (OSError, json.JSONDecodeError):
                had_title = False
            try:
                for event in agent.agent_stream(user_input=user_input):
                    yield f"data: {json.dumps(event)}\n\n"
                messages = agent.get_messages()
                title = None if had_title else derive_title(messages)
                save_conversation(name, conv_id, messages, title=title)
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
    name, conv_id = current_user(), current_conv()
    if name and conv_id:
        with agent_lock:
            activate_session(name, conv_id)
            agent.reset()
            save_conversation(name, conv_id, agent.get_messages())
    if request.method == 'POST':
        return jsonify({'response': 'Agent reset successfully'})
    return redirect(url_for('index'))


@app.route('/upload', methods=['POST'])
def upload():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401

    name = current_user()
    conv_id = current_conv()
    if not name or not conv_id:
        return jsonify({'error': 'No active conversation selected'}), 400

    file = request.files.get('file')
    if not file or file.filename == '':
        return jsonify({'error': 'No file provided'}), 400

    # Save into this chat's own files folder, preserving extension, with a
    # uuid prefix to avoid collisions.
    ext = os.path.splitext(file.filename)[1]
    safe_name = uuid.uuid4().hex + ext
    dest = os.path.join(conv_files_dir(name, conv_id), safe_name)
    file.save(dest)

    # Return the path the agent can reference (relative to app root)
    rel_path = os.path.join('static', name, CONVERSATIONS_SUBDIR, conv_id, safe_name)
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
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

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
    ("HA_TOKEN",         "Home Assistant Token",    True),
    ("HA_URL",           "Home Assistant URL",      False),
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=6767, debug=True)