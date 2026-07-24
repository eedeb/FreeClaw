from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for, session, Response, stream_with_context
from werkzeug.exceptions import HTTPException
import src.agent as agent
import src.mcp_client as mcp_client
from src.users import (
    STATIC_DIR, safe_username, user_dir, conv_files_dir,
    user_context_path, user_ping_path, list_users, user_exists, create_user,
    load_conversation, save_conversation, derive_title, ensure_conversation,
    activate_session,
)
from src.logging_setup import get_logger
import uuid
import json
import re
import time
import threading
import shutil
import functools
from datetime import datetime

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from dotenv import load_dotenv, dotenv_values
import os
load_dotenv()

logger = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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
        logger.exception("Couldn't persist secret key to %s", _secret_key_path)
        _secret_key = os.urandom(24).hex()
app.secret_key = _secret_key

# Signs short-lived tokens for /static/<path> links that get opened outside
# the logged-in browser session — e.g. a generated .ics handed to the iOS
# app's open_url tool, which launches Safari/Calendar in a separate process
# that doesn't carry our session cookie. The token authorizes only the exact
# file path it was signed for, and expires, so it can't be used to browse
# other users' files or stay valid indefinitely if a link leaks.
STATIC_TOKEN_MAX_AGE = 24 * 60 * 60  # 24 hours
_static_token_serializer = URLSafeTimedSerializer(_secret_key, salt="static-file-access")


def _make_static_token(rel_path):
    return _static_token_serializer.dumps(rel_path)


def _verify_static_token(rel_path, token):
    try:
        signed_path = _static_token_serializer.loads(token, max_age=STATIC_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return signed_path == rel_path


agent.set_static_token_signer(_make_static_token)


def _log_and_error(e, message=None, status=500):
    """Log the full exception (with traceback) to logs/freeclaw.log, then
    build the short JSON error body the frontend actually sees — same
    shape every route already returned, just no longer throwing the real
    cause away. Call from inside the except block so exc_info is live."""
    logger.exception("Request failed: %s %s", request.method, request.path)
    return jsonify({'error': message or f'{type(e).__name__}: {e}'}), status


@app.errorhandler(Exception)
def _handle_uncaught(e):
    """Safety net for anything that escapes a route's own try/except (or a
    route with none) — logs the full traceback so a failure is never just
    a blank 500 with no record of what happened. Werkzeug's own routing
    exceptions (404, 405, ...) are real, intended responses, not bugs, so
    they pass through unchanged instead of being logged as errors."""
    if isinstance(e, HTTPException):
        return e
    logger.exception("Unhandled exception on %s %s", request.method, request.path)
    return jsonify({'error': 'Internal server error — see logs/freeclaw.log for details.'}), 500


# A handful of users may legitimately hit /chat at the same moment, and the
# agent module keeps its "active conversation" as module-level globals
# (static_dir, agent_messages, ...) rather than per-request state. This lock
# makes sure one request's agent turn fully finishes (and is persisted to
# disk) before another request is allowed to swap those globals out from
# under it.
agent_lock = threading.Lock()

# NOTE: we deliberately do NOT call agent.reset() here at startup. reset()
# reads/creates a context.md inside whatever folder agent.static_dir
# currently points to — calling it before a user/chat has been selected
# would create a stray context.md directly in static/ instead of inside a
# user's files folder. The tool list gets initialized lazily, scoped
# correctly, the first time ensure_conversation() or activate_session()
# runs (both call set_static_dir before reset()).


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
    logger.exception("Couldn't load tools at startup")


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
        # Read fresh (not the module-load `password` global) so a password
        # changed in Settings — which _write_env pushes into os.environ —
        # takes effect on the very next login without a restart.
        if request.form.get('password') == os.getenv("FC_PASSWORD"):
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


@app.route('/settings')
def settings_page():
    if not logged_in():
        return redirect(url_for('login'))
    return render_template('settings.html')


# ── USER / CONVERSATION API ──────────────────────────────────

@app.route('/api/users', methods=['GET'])
def api_list_users():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        users = [{'name': name} for name in list_users()]
        return jsonify({'users': users, 'static_dir': STATIC_DIR})
    except Exception as e:
        return _log_and_error(e)


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
        return _log_and_error(e)
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
        'messages': data.get('messages', []),
        'updated_at': data.get('updated_at')
    })


@app.route('/api/conversation/meta', methods=['GET'])
def api_get_conversation_meta():
    """Cheap poll target: just the conversation's updated_at, not the full
    message history. The chat page polls this every few seconds so a ping
    delivered in the background (see PING SCHEDULER below) shows up without
    a manual page refresh — only fetching /api/conversation in full once
    this value actually changes."""
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    name = current_user()
    if not name:
        return jsonify({'error': 'No active conversation'}), 400
    ensure_conversation(name)
    data = load_conversation(name)
    return jsonify({'updated_at': data.get('updated_at')})


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
            save_conversation(name, agent.get_messages(), title="New chat")
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
            # One try/except around the whole turn (not just agent_stream)
            # so a failure in activate_session() or the title check — not
            # just in the agent loop itself — still gets logged and turned
            # into a proper SSE error event instead of a silently broken
            # stream.
            session_active = False
            try:
                activate_session(name)
                session_active = True
                try:
                    had_title = load_conversation(name).get("title") not in (None, "", "New chat")
                except (OSError, json.JSONDecodeError):
                    had_title = False
                for event in agent.agent_stream(user_input=user_input):
                    yield f"data: {json.dumps(event)}\n\n"
                messages = agent.get_messages()
                title = None if had_title else derive_title(messages)
                save_conversation(name, messages, title=title)
                yield f"data: {json.dumps({'type': 'done', 'conversation': messages})}\n\n"
            except Exception as e:
                logger.exception("Chat request failed for user=%s", name)
                if session_active:
                    # agent_stream can append several messages (e.g. a
                    # completed tool call) before failing on a later step —
                    # without this, the next turn reloads the pre-turn
                    # conversation from disk and that work silently
                    # vanishes. Only safe once activate_session() has
                    # actually run: before that, agent.get_messages()
                    # could still be a *different* user's leftover state.
                    try:
                        save_conversation(name, agent.get_messages())
                    except Exception:
                        logger.exception("Also failed to save partial conversation for user=%s", name)
                yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


@app.route('/reset', methods=['GET', 'POST'])
def reset():
    if not logged_in():
        return redirect(url_for('login'))
    name = current_user()
    if name:
        with agent_lock:
            activate_session(name)
            agent.reset()
            save_conversation(name, agent.get_messages(), title="New chat")
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


@app.route('/static/<path:filename>')
def serve_static(filename):
    token = request.args.get('token')
    if not logged_in() and not (token and _verify_static_token(filename, token)):
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
        if token != os.getenv("FC_PASSWORD"):  # fresh read — see login()
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
        # Each provider's full traceback is already logged inside
        # _create_completion; this just ties them to the /v1 request.
        logger.error("v1_chat_completions: all providers failed: %s", e.failures)
        reasons = {r for _, r, _ in e.failures}
        status = 429 if reasons == {"rate_limited"} else 500
        err_type = "rate_limit_error" if reasons == {"rate_limited"} else "server_error"
        return jsonify({"error": {"message": agent._user_facing_error(e.failures), "type": err_type}}), status
    except Exception as e:
        logger.exception("v1_chat_completions failed")
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
                logger.exception("v1_chat_completions stream failed")
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

# Known .env keys shown in the settings UI, in display order. LLM provider
# credentials (name/url/key/model) live under Settings → Providers instead
# (see agent.read_providers / providers_to_env) — this list is only for
# config that isn't a provider: login, session, and misc.
SETTINGS_KEYS = [
    ("FC_PASSWORD",      "Login Password",          False),
    ("SECRET_KEY",       "Session Secret Key",      False),
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
    # (e.g. a PROVIDER_KEYS entry) is picked up on the very next request,
    # without restarting the app.
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
        return _log_and_error(e, message=str(e))
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
        'enabled': s.get('enabled', True),
    }


@app.route('/api/mcp', methods=['GET'])
def api_list_mcp():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        servers = mcp_client.read_servers()
    except Exception as e:
        return _log_and_error(e, message=str(e))
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
        servers.append({'name': name, 'url': url, 'token': token, 'enabled': True})
        try:
            _write_env(mcp_client.servers_to_env(servers))
        except Exception as e:
            return _log_and_error(e, message=f'Could not save: {e}')

        # Verify the server is reachable and pick up its tool count now, so
        # the user gets immediate feedback instead of a silent no-op.
        mcp_client.clear_cache()
        error = None
        tool_count = 0
        try:
            tool_count = len(mcp_client.list_tools({'name': name, 'url': url, 'token': token}))
        except Exception as e:
            error = str(e)
            logger.exception("New MCP server '%s' (%s) unreachable at add time", name, url)
        agent.refresh_tools()

    resp = {'ok': True, 'servers': [_mcp_server_public(s) for s in servers], 'tool_count': tool_count}
    if error:
        resp['warning'] = f"Saved, but couldn't reach the server yet: {error}"
    return jsonify(resp)


@app.route('/api/mcp/<name>', methods=['PATCH'])
def api_toggle_mcp(name):
    """Enable/disable a server without touching its saved URL/token — a
    disabled server's tools are left out of the agent's tool list (see
    load_mcp_tools in agent.py) but its config stays in .env untouched, so
    re-enabling it later needs no re-entering of credentials."""
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    if 'enabled' not in data:
        return jsonify({'error': "Body must include 'enabled'."}), 400
    enabled = bool(data.get('enabled'))
    with agent_lock:
        servers = mcp_client.read_servers()
        match = next((s for s in servers if s.get('name') == name), None)
        if match is None:
            return jsonify({'error': 'No such MCP server'}), 404
        match['enabled'] = enabled
        try:
            _write_env(mcp_client.servers_to_env(servers))
        except Exception as e:
            return _log_and_error(e, message=f'Could not save: {e}')
        agent.refresh_tools()
    return jsonify({'ok': True, 'servers': [_mcp_server_public(s) for s in servers]})


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
            return _log_and_error(e, message=f'Could not save: {e}')
        mcp_client.clear_cache()
        agent.refresh_tools()
    return jsonify({'ok': True, 'servers': [_mcp_server_public(s) for s in remaining]})


# ── LLM PROVIDERS (env-backed parallel lists) ────────────────
#
# User-defined OpenAI-compatible endpoints. Stored + read by agent.py
# (read_providers / providers_to_env), persisted the same single-quoted-JSON
# way MCP servers are. Reject the same characters MCP does so the round-trip
# through .env is safe (the api key is the risky field here).

def _provider_public(p):
    """Shape a stored provider for the client. The key is write-only — we
    only report whether one is set, never echo it back."""
    return {
        'name': p.get('name', ''),
        'url': p.get('url', ''),
        'model': p.get('model', ''),
        'has_key': bool((p.get('key') or '').strip()),
        'enabled': p.get('enabled', True),
    }


@app.route('/api/providers', methods=['GET'])
def api_list_providers():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        providers = agent.read_providers()
    except Exception as e:
        return _log_and_error(e, message=str(e))
    return jsonify({'providers': [_provider_public(p) for p in providers]})


@app.route('/api/providers', methods=['POST'])
def api_add_provider():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip()
    url = str(data.get('url', '')).strip()
    key = str(data.get('key', '')).strip()
    model = str(data.get('model', '')).strip()

    if not name or not url or not key:
        return jsonify({'error': 'Name, URL, and API key are all required.'}), 400
    if not re.match(r'^https?://', url, re.IGNORECASE):
        return jsonify({'error': 'URL must start with http:// or https://.'}), 400
    for field, val in (('name', name), ('URL', url), ('API key', key), ('model', model)):
        if any(c in val for c in _MCP_BAD_CHARS):  # same quote/newline rejects as MCP
            return jsonify({'error': f'The {field} contains unsupported characters (quotes or newlines).'}), 400

    with agent_lock:
        providers = agent.read_providers()
        if any(p.get('name') == name for p in providers):
            return jsonify({'error': f"A provider named '{name}' already exists."}), 409
        providers.append({'name': name, 'url': url, 'key': key, 'model': model, 'enabled': True})
        try:
            _write_env(agent.providers_to_env(providers))
        except Exception as e:
            return _log_and_error(e, message=f'Could not save: {e}')
    return jsonify({'ok': True, 'providers': [_provider_public(p) for p in providers]})


@app.route('/api/providers/<name>', methods=['PATCH'])
def api_toggle_provider(name):
    """Enable/disable a provider without dropping its saved url/key/model."""
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    if 'enabled' not in data:
        return jsonify({'error': "Body must include 'enabled'."}), 400
    enabled = bool(data.get('enabled'))
    with agent_lock:
        providers = agent.read_providers()
        match = next((p for p in providers if p.get('name') == name), None)
        if match is None:
            return jsonify({'error': 'No such provider'}), 404
        match['enabled'] = enabled
        try:
            _write_env(agent.providers_to_env(providers))
        except Exception as e:
            return _log_and_error(e, message=f'Could not save: {e}')
    return jsonify({'ok': True, 'providers': [_provider_public(p) for p in providers]})


@app.route('/api/providers/reorder', methods=['POST'])
def api_reorder_providers():
    """Persist a new top-to-bottom order for the provider chain — this is
    the order _active_providers() (and so _create_completion's fallback
    chain) tries them in, so dragging a provider to the top makes it the
    one used first."""
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    order = data.get('order')
    if not isinstance(order, list) or not all(isinstance(n, str) for n in order):
        return jsonify({'error': "Body must include 'order' as a list of provider names."}), 400
    with agent_lock:
        providers = agent.read_providers()
        by_name = {p.get('name'): p for p in providers}
        # Providers named in `order` come first, in that order; anything not
        # named (shouldn't normally happen — the client always sends every
        # name it has) is appended after, in its existing order, so a stale
        # request can't silently drop a provider from the chain.
        reordered = [by_name[n] for n in order if n in by_name]
        seen = set(order)
        reordered += [p for p in providers if p.get('name') not in seen]
        try:
            _write_env(agent.providers_to_env(reordered))
        except Exception as e:
            return _log_and_error(e, message=f'Could not save: {e}')
    return jsonify({'ok': True, 'providers': [_provider_public(p) for p in reordered]})


@app.route('/api/providers/<name>', methods=['DELETE'])
def api_delete_provider(name):
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    with agent_lock:
        providers = agent.read_providers()
        remaining = [p for p in providers if p.get('name') != name]
        if len(remaining) == len(providers):
            return jsonify({'error': 'No such provider'}), 404
        try:
            _write_env(agent.providers_to_env(remaining))
        except Exception as e:
            return _log_and_error(e, message=f'Could not save: {e}')
    return jsonify({'ok': True, 'providers': [_provider_public(p) for p in remaining]})


# ── VISION MODEL (single scalar, references a provider by name) ──

@app.route('/api/vision-model', methods=['GET'])
def api_get_vision_model():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'provider': _read_env().get('VISION_PROVIDER', '')})


@app.route('/api/vision-model', methods=['POST'])
def api_set_vision_model():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    name = str(data.get('provider', '')).strip()
    if name and not any(p.get('name') == name for p in agent.read_providers()):
        return jsonify({'error': f"No such provider: '{name}'."}), 400
    try:
        _write_env({'VISION_PROVIDER': name})
    except Exception as e:
        return _log_and_error(e, message=str(e))
    return jsonify({'ok': True, 'provider': name})


# ── SERVER RESTART ───────────────────────────────────────────

@app.route('/api/restart', methods=['POST'])
def api_restart():
    """Restart the server so config that isn't picked up live (SECRET_KEY,
    a newly-installed dependency, code pulled by update.sh) takes effect.

    Mechanism: the process simply exits, and systemd — which runs FreeClaw
    with Restart=always / RestartSec=5 (see install.sh) — brings it back up
    within a few seconds. No sudo, no shelling out to systemctl. The
    frontend polls until the server answers again, then reloads. If FreeClaw
    is being run WITHOUT the systemd unit (e.g. a bare `python -m
    Flask.main` during development), nothing restarts it and the process
    just stops — the poll will time out with a clear message rather than
    silently hang."""
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401

    def _exit_soon():
        # Give the HTTP response time to flush to the browser before the
        # worker dies; os._exit skips atexit/cleanup so systemd sees a
        # clean process gone and restarts it immediately.
        time.sleep(0.7)
        logger.info("Restart requested via /api/restart — exiting for systemd to respawn")
        os._exit(0)

    threading.Thread(target=_exit_soon, daemon=True).start()
    return jsonify({'ok': True})


# ── PING SCHEDULER ───────────────────────────────────────────
#
# One daemon thread wakes every PING_POLL_SECONDS and delivers any pings
# whose time has arrived. Each user's pings live in their own ping.md (written
# by the agent's add_ping tool), one per line as "YYYY-MM-DD HH:MM - <action>",
# kept sorted soonest-first. Delivering a ping runs a normal agent turn for
# that user with the action text as the prompt, then saves the conversation —
# so the exchange is already there the next time they open their chat.

PING_POLL_SECONDS = 30
_ping_scheduler_started = False
_ping_scheduler_start_lock = threading.Lock()


def _pop_due_pings(name, now):
    """Read this user's ping.md, remove every entry whose time is <= now, and
    return those due entries as (timestamp, action) pairs. Future entries —
    and any line whose timestamp genuinely can't be parsed — are written back
    untouched.

    We compare with <= (not ==) so a ping still fires if the exact minute's
    poll was missed (server busy, asleep, or only just restarted): any overdue
    ping runs on the next pass and is then removed. Timestamps are parsed with
    agent.parse_ping_time(), which accepts the off-format shapes models emit —
    a strict single-format parse here was silently skipping real pings. The
    caller holds agent_lock, so this can't race add_ping rewriting the file."""
    path = user_ping_path(name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except FileNotFoundError:
        return []
    due, remaining = [], []
    for ln in lines:
        stamp, _, action = ln.partition(" - ")
        when = agent.parse_ping_time(stamp)
        if when is None:
            # Keep it (we can't know when it's meant to fire) but make the
            # skip visible — this is exactly the failure that hid before.
            logger.warning("Skipping unparseable ping for user=%s: %r", name, ln)
            remaining.append(ln)
        elif when <= now:
            due.append((stamp.strip(), action.strip()))
        else:
            remaining.append(ln)
    if due:  # only rewrite when we actually removed something
        with open(path, "w", encoding="utf-8") as f:
            f.write(("\n".join(remaining) + "\n") if remaining else "")
    return due


def _fire_due_pings():
    """One scheduler pass: deliver every due ping for every user."""
    now = datetime.now()
    for name in list_users():
        # Hold agent_lock across pop+deliver for this user: it serialises the
        # scheduler against live chat turns (which share the agent module's
        # globals) and against add_ping writing the same ping.md. The lock is
        # released between users so a burst of pings can't starve the web UI.
        with agent_lock:
            try:
                due = _pop_due_pings(name, now)
            except Exception:
                logger.exception("Couldn't read pings for user=%s", name)
                continue
            for stamp, action in due:
                if not action:
                    continue
                try:
                    activate_session(name)
                    # Injected as a normal user turn ("physically entered"),
                    # so the model acts on it and the bubble shows in the UI.
                    agent.agent(user_input=action)
                    save_conversation(name, agent.get_messages())
                    logger.info("Delivered ping for user=%s scheduled=%s action=%r", name, stamp, action)
                except Exception:
                    # A failed turn must not wedge the scheduler or replay the
                    # same ping forever — it's already been removed from
                    # ping.md, so log it and move on.
                    logger.exception("Ping delivery failed for user=%s scheduled=%s", name, stamp)


def _ping_scheduler_loop():
    while True:
        try:
            _fire_due_pings()
        except Exception:
            logger.exception("Ping scheduler pass crashed")
        time.sleep(PING_POLL_SECONDS)


def start_ping_scheduler():
    """Start the background ping thread exactly once per process."""
    global _ping_scheduler_started
    with _ping_scheduler_start_lock:
        if _ping_scheduler_started:
            return
        _ping_scheduler_started = True
    threading.Thread(target=_ping_scheduler_loop, daemon=True, name="ping-scheduler").start()
    logger.info("Ping scheduler started (polling every %ss)", PING_POLL_SECONDS)


if __name__ == '__main__':
    # debug=True runs Werkzeug's reloader, which re-execs this module in a
    # child process; only that child has WERKZEUG_RUN_MAIN set. Start the
    # scheduler there so pings aren't fired twice (once per process).
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_ping_scheduler()
    app.run(host='0.0.0.0', port=6767, debug=True)