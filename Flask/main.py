from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for, session, Response, stream_with_context
from werkzeug.exceptions import HTTPException
import src.agent as agent
import src.approvals as approvals
import src.browser_profiles as browser_profiles
import src.browser_setup as browser_setup
# Neither of these imports playwright at module scope — it arrives only when a
# sign-in browser is actually opened, which keeps the web process the same size
# it was for everyone who never uses one (the reasoning in browser_setup.py).
import src.browser_takeover as browser_takeover
import src.cancellation as cancellation
import src.mcp_client as mcp_client
import src.session as sessions
import src.telemetry as telemetry
from src.users import (
    STATIC_DIR, safe_username, user_dir, conv_files_dir,
    user_ping_path, list_users, user_exists, create_user,
    load_conversation, save_conversation, derive_title, ensure_conversation,
    activate_session,
)
from src.logging_setup import get_logger
import atexit
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


# A handful of users may legitimately hit /chat at the same moment. The agent's
# "active conversation" now lives on a per-user Session (src/session.py) rather
# than in module globals, so those requests no longer contend for one lock:
# each turn takes its own conversation's lock, and two users run side by side.
#
# What still needs serialising is the conversation itself — a second turn must
# not start in a conversation whose first turn hasn't finished and been
# persisted — which is exactly the scope of Session.lock.
def _session_lock(name):
    """The lock serialising turns in `name`'s conversation."""
    return sessions.for_user(name).lock


# Global settings, on the other hand, are still global: the provider and MCP
# routes below do a read-modify-write of .env, and two of those interleaving
# would lose an entry. That's what this guards — config, never conversations.
config_lock = threading.Lock()

# NOTE: we deliberately do NOT call agent.reset() here at startup. reset()
# reads/creates a context.md inside whatever folder agent.static_dir
# currently points to — calling it before a user/chat has been selected
# would create a stray context.md directly in static/ instead of inside a
# user's files folder. The tool list gets initialized lazily, scoped
# correctly, the first time ensure_conversation() or activate_session()
# runs (both call set_static_dir before reset()).


def logged_in():
    return session.get("authenticated") is True


# Build the agent's tool list now (built-ins + any MCP servers configured in
# .env) so tools are ready even for the very first request against an existing
# conversation — which doesn't otherwise trigger agent.reset(). A flaky MCP
# server must never stop the app from booting.
try:
    agent.refresh_tools()
except Exception as e:
    print(f"[freeclaw] Warning: couldn't load tools at startup ({e}).")
    logger.exception("Couldn't load tools at startup")


# A sign-in browser holds an X display and a Chromium; Settings -> Restart
# exits the process, so without this one would be left running with nothing
# able to reach it.
atexit.register(browser_takeover.shutdown_all)


def current_user():
    name = session.get("current_user")
    if name and user_exists(name):
        return name
    return None


def _reset_conversation(name):
    """Start `name`'s conversation over. Shared by the /reset route and the
    /reset slash-command so the two can't drift."""
    with _session_lock(name):
        activate_session(name)
        agent.reset()
        save_conversation(name, agent.get_messages(), title="New chat")


def _has_title(name):
    """Whether this conversation already has a real (non-default) title —
    checked before a turn so only the first exchange derives one."""
    try:
        return load_conversation(name).get("title") not in (None, "", "New chat")
    except (OSError, json.JSONDecodeError):
        return False


# ── AUTH ROUTES ──────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = False
    if request.method == 'POST':
        # Read at request time (not cached at import) so a password changed in
        # Settings — which _write_env pushes into os.environ — takes effect on
        # the very next login without a restart.
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
    with _session_lock(name):
        shutil.rmtree(user_dir(name), ignore_errors=True)
        # Drop the in-memory conversation too, so a user recreated under the
        # same name starts clean instead of inheriting the deleted one's
        # messages from the registry.
        sessions.discard(name)
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
        _reset_conversation(name)
        return jsonify({'response': 'Agent reset successfully'})
    elif user_input.lower() == '/startapi':
        _set_api_enabled(True)
        return jsonify({'response': 'API enabled. Use your FreeClaw password as the Bearer token at /v1/chat/completions'})
    elif user_input.lower() == '/stopapi':
        _set_api_enabled(False)
        return jsonify({'response': 'API disabled'})

    def generate():
        # This user's conversation only — another user's turn runs alongside
        # it rather than queueing behind it.
        with _session_lock(name):
            # One try/except around the whole turn (not just agent_stream)
            # so a failure in activate_session() or the title check — not
            # just in the agent loop itself — still gets logged and turned
            # into a proper SSE error event instead of a silently broken
            # stream.
            session_active = False
            try:
                # interactive=True: there's a browser on the other end of this
                # stream that can answer a bash approval prompt (see
                # /api/approval below).
                sess = activate_session(name, interactive=True)
                session_active = True
                # Arm the Stop button for this turn. Clears any stop left over
                # from the previous one, so a press that raced the end of its
                # own turn can't kill this one.
                cancellation.begin_turn(sess)
                had_title = _has_title(name)
                for event in agent.agent_stream(user_input=user_input):
                    yield f"data: {json.dumps(event)}\n\n"
                messages = agent.get_messages()
                title = None if had_title else derive_title(messages)
                updated_at = save_conversation(name, messages, title=title)
                # Hand back the stamp we just wrote so the page can retire its
                # own turn without the ping poller then seeing updated_at move
                # and re-fetching/re-rendering the whole thread a few seconds
                # later.
                yield f"data: {json.dumps({'type': 'done', 'conversation': messages, 'updated_at': updated_at})}\n\n"
            except Exception as e:
                logger.exception("Chat request failed for user=%s", name)
                # A turn that blew up mid-flight may have left an approval
                # prompt on screen with nothing behind it; release any waiter
                # instead of letting it sit out the full timeout. Scoped to
                # this user, so another user's prompt isn't refused by a
                # failure in a conversation that has nothing to do with it.
                approvals.abandon_all(name)
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
            finally:
                # Finished, failed, stopped, or the client hung up mid-stream
                # (which closes the generator and runs this too) — either way
                # the flag must not outlive the turn that owns it.
                cancellation.end_turn(sessions.for_user(name))

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


@app.route('/api/stop', methods=['POST'])
def api_stop():
    """Ask this user's in-flight turn to wind down.

    Like /api/approval below, this deliberately does NOT take the conversation's
    lock: the turn being stopped is holding it, so waiting for it here would
    deadlock the two against each other. Setting a flag the generator polls is
    the whole mechanism.

    The Session has to be named rather than inferred — this request runs on a
    different thread from the turn, so it has no binding of its own to read.
    That also keeps Stop pointed at the presser's own conversation instead of
    whichever turn happens to be running.

    Any pending bash approval is abandoned too — a turn blocked on a prompt is
    exactly the case where Stop is most wanted, and it can't reach a
    cancellation checkpoint while it sits in approvals.wait()."""
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    name = current_user()
    if not name:
        return jsonify({'error': 'No active conversation'}), 400
    sess = sessions.existing(name)
    if sess is None or not cancellation.request_stop(sess):
        # 409 rather than 400, matching /api/approval: the request was fine,
        # there just isn't a turn to stop (it finished as the click landed).
        return jsonify({'error': 'No turn is currently running.'}), 409
    approvals.abandon_all(name)
    return jsonify({'ok': True})


# ── BASH APPROVALS ───────────────────────────────────────────
#
# When a command isn't covered by a saved rule, the agent turn emits an
# `approval_request` SSE event and blocks; the browser shows the command and
# posts the answer back here. These routes deliberately do NOT take the
# conversation's lock — the turn that's waiting is holding it. That's the whole
# mechanism: a blocked generator on one thread, released by a request on
# another. Prompts are keyed by request id, which is already unique per prompt,
# so this keeps working unchanged now that several turns can be waiting at once.

@app.route('/api/approval', methods=['POST'])
def api_resolve_approval():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    request_id = str(data.get('id', '')).strip()
    decision = str(data.get('decision', '')).strip()
    if not request_id:
        return jsonify({'error': "Body must include 'id'."}), 400
    if decision not in approvals.DECISIONS:
        return jsonify({'error': f"decision must be one of: {', '.join(approvals.DECISIONS)}."}), 400
    ok, error = approvals.resolve(request_id, decision)
    if not ok:
        # 409, not 400: the request was well-formed, the prompt just isn't
        # waiting any more (answered already, or timed out).
        return jsonify({'error': error}), 409
    return jsonify({'ok': True})


@app.route('/api/bash-approvals', methods=['GET'])
def api_list_bash_approvals():
    """The always-allow rules saved for the session's current user."""
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    name = current_user()
    if not name:
        return jsonify({'error': 'No active user selected'}), 400
    return jsonify({'user': name, **approvals.list_rules(name)})


@app.route('/api/bash-approvals', methods=['DELETE'])
def api_delete_bash_approval():
    """Revoke one saved rule, or all of them with {"all": true}. Anything
    revoked here goes back to prompting on the next attempt."""
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    name = current_user()
    if not name:
        return jsonify({'error': 'No active user selected'}), 400
    data = request.get_json(silent=True) or {}
    if data.get('all'):
        approvals.clear_rules(name)
    else:
        kind = str(data.get('kind', '')).strip()
        value = data.get('value')
        if kind not in ('exact', 'programs') or not isinstance(value, str):
            return jsonify({'error': "Body must include kind ('exact' or 'programs') and value, "
                                     "or {\"all\": true}."}), 400
        if not approvals.remove_rule(name, kind, value):
            return jsonify({'error': 'No such rule'}), 404
    return jsonify({'ok': True, **approvals.list_rules(name)})


# ── BROWSER SIGN-IN ──────────────────────────────────────────
#
# The agent's browser starts signed out of everything, so any site behind a
# login is a wall it can't get past. These routes drive a second, headful
# Chromium (src/browser_takeover.py) that the user operates through the web UI
# — it renders as a stream of screenshots and forwards clicks and keystrokes.
# On finish, its cookies are saved per user (src/browser_profiles.py) and the
# agent's next browser call loads them.
#
# Every route is behind logged_in() and scoped to current_user(). There is no
# token-based escape hatch like serve_static's: a frame of this browser may
# have someone's inbox in it, and a saved session is a live credential.

# Schemes the sign-in browser will open. Anything else — file://, chrome://,
# view-source: — turns a page meant for logging into websites into a reader for
# the server's own filesystem, rendered back over HTTP as a screenshot.
_BROWSER_SCHEMES = ('http://', 'https://')


def _takeover_user():
    """(user, error_response). The browser profile is per FreeClaw user, so
    there has to be one selected before any of this means anything."""
    name = current_user()
    if not name:
        return None, (jsonify({'error': 'Open a chat first — saved logins belong to a '
                                        'specific FreeClaw user.'}), 400)
    return name, None


@app.route('/browser-login')
def browser_login_page():
    if not logged_in():
        return redirect(url_for('login'))
    return render_template('browser_login.html', user=current_user())


@app.route('/api/browser-login/start', methods=['POST'])
def api_browser_login_start():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    name, error = _takeover_user()
    if error:
        return error
    url = str((request.get_json(silent=True) or {}).get('url', '')).strip()
    if not url:
        return jsonify({'error': 'Body must include a url.'}), 400
    if '://' not in url:
        url = 'https://' + url
    if not url.lower().startswith(_BROWSER_SCHEMES):
        return jsonify({'error': 'Only http:// and https:// addresses can be opened here.'}), 400
    if not browser_setup.chromium_present():
        return jsonify({'error': 'Chromium isn\'t installed yet. Switch the browser server on '
                                 'in Settings first — that\'s what downloads it.'}), 409
    try:
        browser_takeover.start(name, url)
    except Exception as e:
        logger.exception('Couldn\'t start the sign-in browser')
        return jsonify({'error': f'Couldn\'t start the browser: {e}'}), 500
    return jsonify({'ok': True, **browser_takeover.status(name)})


@app.route('/api/browser-login/status', methods=['GET'])
def api_browser_login_status():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    name = current_user()
    if not name:
        return jsonify({'running': False, 'saved_domains': []})
    return jsonify(browser_takeover.status(name))


@app.route('/api/browser-login/frame', methods=['GET'])
def api_browser_login_frame():
    """The latest screenshot. The page requests the next one as soon as this
    finishes loading, so the frame rate self-clocks to whatever the connection
    and the browser can actually manage instead of a fixed interval that's
    wrong on both a LAN and a slow VPS link."""
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    name, error = _takeover_user()
    if error:
        return error
    sess = browser_takeover.get(name)
    if sess is None or not sess.alive():
        return jsonify({'error': 'No sign-in browser is open.'}), 404
    sess.touch()
    frame = sess.frame()
    if frame is None:
        # Still loading the first page — not an error, just nothing to draw.
        return jsonify({'error': 'No frame yet.'}), 204
    response = app.response_class(frame, mimetype='image/jpeg')
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/browser-login/input', methods=['POST'])
def api_browser_login_input():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    name, error = _takeover_user()
    if error:
        return error
    sess = browser_takeover.get(name)
    if sess is None or not sess.alive():
        return jsonify({'error': 'No sign-in browser is open.'}), 404
    data = request.get_json(silent=True) or {}
    kind = str(data.get('kind', '')).strip()
    if kind not in ('click', 'text', 'key', 'scroll', 'nav', 'back', 'reload'):
        return jsonify({'error': f'Unknown input kind {kind!r}.'}), 400
    if kind == 'nav':
        url = str(data.get('url', '')).strip()
        if '://' not in url:
            url = 'https://' + url
        if not url.lower().startswith(_BROWSER_SCHEMES):
            return jsonify({'error': 'Only http:// and https:// addresses can be opened here.'}), 400
        data = {**data, 'url': url}
    sess.send({**data, 'kind': kind})
    return jsonify({'ok': True})


@app.route('/api/browser-login/finish', methods=['POST'])
def api_browser_login_finish():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    name, error = _takeover_user()
    if error:
        return error
    sess = browser_takeover.get(name)
    if sess is None or not sess.alive():
        return jsonify({'error': 'No sign-in browser is open.'}), 404
    saved = sess.finish()
    if not saved:
        return jsonify({'error': sess.error or 'Couldn\'t save the logins.'}), 500
    # The agent's browser child was spawned with the old profile (or none), and
    # `_sig` keys it on that. Drop the cached children so the next tool call
    # spawns one that loads what was just saved.
    mcp_client.clear_cache()
    agent.refresh_tools()
    return jsonify({'ok': True, 'saved_domains': browser_profiles.domains(name)})


@app.route('/api/browser-login/cancel', methods=['POST'])
def api_browser_login_cancel():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    name, error = _takeover_user()
    if error:
        return error
    sess = browser_takeover.get(name)
    if sess is not None:
        sess.cancel()
    return jsonify({'ok': True})


@app.route('/api/browser-login/saved', methods=['DELETE'])
def api_browser_login_clear():
    """Forget every site this user's agent is signed into."""
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    name, error = _takeover_user()
    if error:
        return error
    browser_profiles.clear(name)
    mcp_client.clear_cache()
    agent.refresh_tools()
    return jsonify({'ok': True, 'saved_domains': []})


@app.route('/reset', methods=['GET', 'POST'])
def reset():
    if not logged_in():
        return redirect(url_for('login'))
    name = current_user()
    if name:
        _reset_conversation(name)
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


def _set_api_enabled(enable):
    """Flip the flag file that enables /v1. Shared by the /startapi and
    /stopapi slash-commands and the /api/api-status toggle."""
    if enable:
        open(_API_FLAG, 'w').close()
    elif os.path.exists(_API_FLAG):
        os.remove(_API_FLAG)


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
    _set_api_enabled(data.get('enabled', not api_is_enabled()))
    return jsonify({'enabled': api_is_enabled()})


@app.route('/v1/models', methods=['GET'])
@_require_api_auth
def v1_models():
    """Your FreeClaw users, in the shape an OpenAI client expects a model list.

    Picking a "model" is picking whose conversation you're continuing — so the
    model list is the user list, and an off-the-shelf client's model dropdown
    becomes a user picker with no extra work."""
    return jsonify({
        "object": "list",
        "data": [{"id": name, "object": "model", "created": 0, "owned_by": "freeclaw"}
                 for name in list_users()],
    })


def _api_error(message, status, err_type="invalid_request_error", code=None):
    body = {"message": message, "type": err_type}
    if code:
        body["code"] = code
    return jsonify({"error": body}), status


def _last_user_message(messages):
    """The prompt to act on: the last user-role message in the request.

    History lives on the server, so everything before it is ignored. Clients
    that resend the whole transcript every call (most do) therefore work
    unchanged — their earlier turns are simply redundant, not duplicated into
    the stored conversation."""
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, list):
            # Multimodal shape: keep the text parts, drop the rest.
            content = "\n".join(b.get("text", "") for b in content
                                if isinstance(b, dict) and b.get("type") == "text")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


@app.route('/v1/chat/completions', methods=['POST'])
@_require_api_auth
def v1_chat_completions():
    """A stateful chat turn, wearing the OpenAI chat-completions shape.

    Two departures from a plain completions endpoint, both deliberate:

      * **`model` names a FreeClaw user**, not a model. The provider chain is
        FreeClaw's own business (Settings → Providers); what the caller is
        choosing is whose memory, whose conversation and whose approval rules
        this turn runs against.
      * **History is the server's.** Only the last user message in the request
        is acted on; the stored conversation supplies everything before it, and
        the turn is appended to it. So the same thread is shared with the web
        UI and the CLI — send a message here and it's there when you open the
        chat page.

    It's a full agent turn, not a raw model call: tools run, memory is read and
    written, and the reply comes back after all of that. Bash runs only if it
    matches a saved always-allow rule for that user — there's nobody on an API
    call to answer a prompt, so anything else is refused rather than hanging.
    """
    data = request.get_json(silent=True)
    if not data:
        return _api_error("Invalid JSON body", 400)

    messages = data.get('messages')
    if not messages or not isinstance(messages, list):
        return _api_error("messages field is required", 400)

    user_input = _last_user_message(messages)
    if not user_input:
        return _api_error("messages must contain a user message with text content", 400)

    req_model = str(data.get('model') or '').strip()
    if not req_model:
        return _api_error(
            "model is required — set it to the FreeClaw user you want to talk to. "
            "GET /v1/models lists them.", 400, code="model_not_found")
    name = safe_username(req_model)
    if not name or not user_exists(name):
        known = ", ".join(list_users()) or "none yet"
        return _api_error(
            f"No FreeClaw user named '{req_model}'. The model field selects the user "
            f"to talk to; available: {known}.", 404, code="model_not_found")

    stream = bool(data.get('stream', False))
    completion_id = 'chatcmpl-' + uuid.uuid4().hex[:12]
    created = int(time.time())

    def run_turn():
        """One agent turn for `name`, yielding its events as they happen and
        persisting the conversation at the end. Mirrors the /chat route: the
        same conversation lock held across the whole turn, the same title
        behaviour, and a save even when the turn raises part-way so completed
        tool work isn't lost.

        Taking this user's lock (not a global one) is also what stops an API
        call and a browser turn interleaving inside the same conversation while
        leaving a different user's turn free to run alongside."""
        with _session_lock(name):
            session_active = False
            try:
                # interactive=False: an API caller can't answer a bash approval
                # prompt, so unapproved commands are refused, not queued.
                activate_session(name, interactive=False)
                session_active = True
                had_title = _has_title(name)
                yield from agent.agent_stream(user_input=user_input)
                msgs = agent.get_messages()
                save_conversation(name, msgs, title=None if had_title else derive_title(msgs))
            except Exception:
                if session_active:
                    try:
                        save_conversation(name, agent.get_messages())
                    except Exception:
                        logger.exception("Also failed to save partial conversation for user=%s", name)
                raise

    def usage_block():
        u = agent.get_turn_usage()
        return {"prompt_tokens": u["prompt_tokens"],
                "completion_tokens": u["completion_tokens"],
                "total_tokens": u["prompt_tokens"] + u["completion_tokens"]}

    if stream:
        def generate():
            try:
                for event in run_turn():
                    # Tools run transparently — only assistant text goes out,
                    # so a caller sees the same reply the chat UI renders.
                    if event.get("type") != "token":
                        continue
                    yield "data: " + json.dumps({
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": name,
                        "choices": [{"index": 0,
                                     "delta": {"content": event.get("text", "")},
                                     "finish_reason": None}],
                    }) + "\n\n"
                yield "data: " + json.dumps({
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": usage_block(),
                }) + "\n\n"
                yield "data: [DONE]\n\n"
            except agent.AllProvidersFailedError as e:
                logger.error("v1 stream: all providers failed: %s", e.failures)
                yield f"data: {json.dumps({'error': {'message': agent._user_facing_error(e.failures), 'type': 'server_error'}})}\n\n"
            except Exception as e:
                logger.exception("v1_chat_completions stream failed for user=%s", name)
                yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'server_error'}})}\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
        )

    parts = []
    try:
        for event in run_turn():
            if event.get("type") == "token":
                parts.append(event.get("text", ""))
    except agent.AllProvidersFailedError as e:
        # Each provider's full traceback is already logged inside
        # _create_completion; this just ties them to the /v1 request.
        logger.error("v1_chat_completions: all providers failed: %s", e.failures)
        reasons = {r for _, r, _ in e.failures}
        status = 429 if reasons == {"rate_limited"} else 500
        err_type = "rate_limit_error" if reasons == {"rate_limited"} else "server_error"
        return _api_error(agent._user_facing_error(e.failures), status, err_type)
    except Exception as e:
        logger.exception("v1_chat_completions failed for user=%s", name)
        return _api_error(str(e), 500, "server_error")

    return jsonify({
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": name,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "".join(parts)},
            "finish_reason": "stop",
        }],
        "usage": usage_block(),
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
    ("FC_TELEMETRY",     "Anonymous Install Ping (1 = on, 0 = off)", False),
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
    # _write_env writes values verbatim, one KEY=value per line — a newline in
    # a value would spill onto its own line and be read back as a different
    # (or brand-new) key.
    for key, value in updates.items():
        if '\n' in value or '\r' in value:
            return jsonify({'error': f'{key} cannot contain newlines.'}), 400
    try:
        _write_env(updates)
    except Exception as e:
        return _log_and_error(e, message=str(e))
    return jsonify({'ok': True})


# ── MCP SERVERS (env-backed parallel lists) ──────────────────

# Characters that would break the single-quote-wrapped JSON we store in .env,
# or python-dotenv's parsing. Rejected on input so the round-trip is safe.
_MCP_BAD_CHARS = ("'", '"', '\n', '\r')

# A stdio command legitimately needs quoting for paths with spaces, and a
# double quote survives our .env encoding intact (json.dumps escapes it, and
# python-dotenv doesn't reinterpret escapes inside a single-quoted value). A
# single quote would terminate that value, so it's still out.
_MCP_BAD_COMMAND_CHARS = ("'", '\n', '\r')


def _mcp_server_public(s):
    """Shape a stored server for the client. The token is write-only — we only
    report whether one is set, never echo it back. The command is not a
    secret (it's what the user typed) so it round-trips, which is what lets
    the UI show what a stdio server actually runs.

    A builtin carries two extras: a description, since the user never typed a
    command line to recognise it by, and — for one that drives a browser — how
    far along the one-time Chromium download is, which is what the card shows
    in place of the command."""
    out = {
        'name': s.get('name', ''),
        'url': s.get('url', ''),
        'has_token': bool((s.get('token') or '').strip()),
        'enabled': s.get('enabled', True),
        'transport': s.get('transport') or mcp_client.HTTP,
        'command': s.get('command', ''),
    }
    if s.get('builtin'):
        out['builtin'] = True
        out['description'] = s.get('description', '')
        # The command is an absolute interpreter path we generated, not
        # something the user would recognise or should edit. Don't show it.
        out['command'] = ''
    if s.get('needs_browser'):
        out['browser'] = browser_setup.state()
    return out


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
    transport = str(data.get('transport', '') or mcp_client.HTTP).strip().lower()
    command = str(data.get('command', '')).strip()

    if transport not in mcp_client.TRANSPORTS:
        return jsonify({'error': f"Transport must be one of: {', '.join(mcp_client.TRANSPORTS)}."}), 400
    if not name:
        return jsonify({'error': 'A name is required.'}), 400

    if transport == mcp_client.STDIO:
        # A stdio server has no URL or bearer token — it's a local process, so
        # anything it needs to authenticate comes from the environment (see
        # the module docstring in src/mcp_client.py).
        url, token = '', ''
        if not command:
            return jsonify({'error': 'A command is required for a stdio server.'}), 400
        if any(c in command for c in _MCP_BAD_COMMAND_CHARS):
            return jsonify({'error': 'The command cannot contain single quotes or newlines. '
                                     'Use double quotes to wrap a path with spaces.'}), 400
    else:
        command = ''
        if not url:
            return jsonify({'error': 'A URL is required for an HTTP server.'}), 400
        if not re.match(r'^https?://', url, re.IGNORECASE):
            return jsonify({'error': 'URL must start with http:// or https://.'}), 400

    for field, val in (('name', name), ('URL', url), ('token', token)):
        if any(c in val for c in _MCP_BAD_CHARS):
            return jsonify({'error': f'The {field} contains unsupported characters (quotes or newlines).'}), 400

    entry = {'name': name, 'url': url, 'token': token, 'enabled': True,
             'transport': transport, 'command': command}

    with config_lock:
        servers = mcp_client.read_servers()
        if any(s.get('name') == name for s in servers):
            return jsonify({'error': f"An MCP server named '{name}' already exists."}), 409
        servers.append(entry)
        try:
            _write_env(mcp_client.servers_to_env(servers))
        except Exception as e:
            return _log_and_error(e, message=f'Could not save: {e}')

        # Verify the server is reachable and pick up its tool count now, so
        # the user gets immediate feedback instead of a silent no-op. For
        # stdio this is also what spawns the process for the first time, so a
        # bad command line surfaces here rather than mid-conversation.
        mcp_client.clear_cache()
        error = None
        tool_count = 0
        try:
            tool_count = len(mcp_client.list_tools(entry))
        except Exception as e:
            error = str(e)
            logger.exception("New MCP server '%s' (%s) unreachable at add time",
                             name, mcp_client.describe(entry))
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
    with config_lock:
        servers = mcp_client.read_servers()
        match = next((s for s in servers if s.get('name') == name), None)
        if match is None:
            return jsonify({'error': 'No such MCP server'}), 404
        match['enabled'] = enabled
        try:
            _write_env(mcp_client.servers_to_env(servers))
        except Exception as e:
            return _log_and_error(e, message=f'Could not save: {e}')
        # Disabling a stdio server has to actually stop its child process, not
        # just hide its tools — clear_cache() is what shuts those down.
        mcp_client.clear_cache()
        # Enabling a browser-backed server is what triggers its one-time
        # Chromium download, which is why FreeClaw's own install stays small.
        # start() returns straight away and the work continues on a background
        # thread; the card polls /api/mcp for the result. Its tools stay out of
        # the agent's list until that lands (see load_mcp_tools).
        browser = None
        if enabled and match.get('needs_browser'):
            browser = browser_setup.start()
        agent.refresh_tools()
    resp = {'ok': True, 'servers': [_mcp_server_public(s) for s in servers]}
    if browser and browser.get('status') == browser_setup.INSTALLING:
        resp['warning'] = browser.get('message')
    return jsonify(resp)


@app.route('/api/mcp/<name>', methods=['DELETE'])
def api_delete_mcp(name):
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    # A server FreeClaw ships with isn't the user's to remove — read_servers()
    # would put it straight back on the next page load, so deleting it would
    # look broken rather than forbidden. Switching it off is the way to be rid
    # of it, and that already stops its process and hides its tools.
    if mcp_client.is_builtin(name):
        return jsonify({'error': f"'{name}' ships with FreeClaw and can't be removed. "
                                 "Switch it off instead."}), 400
    with config_lock:
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
        'api': p.get('api', 'chat'),
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
    # Which wire protocol to speak to this endpoint. Anything unrecognised
    # falls back to "chat", the dialect every OpenAI-compatible endpoint takes.
    api = str(data.get('api', 'chat')).strip()
    if api not in agent.PROVIDER_APIS:
        return jsonify({'error': f"Unknown api '{api}'."}), 400

    if not name or not url or not key:
        return jsonify({'error': 'Name, URL, and API key are all required.'}), 400
    if not re.match(r'^https?://', url, re.IGNORECASE):
        return jsonify({'error': 'URL must start with http:// or https://.'}), 400
    for field, val in (('name', name), ('URL', url), ('API key', key), ('model', model)):
        if any(c in val for c in _MCP_BAD_CHARS):  # same quote/newline rejects as MCP
            return jsonify({'error': f'The {field} contains unsupported characters (quotes or newlines).'}), 400

    with config_lock:
        providers = agent.read_providers()
        if any(p.get('name') == name for p in providers):
            return jsonify({'error': f"A provider named '{name}' already exists."}), 409
        providers.append({'name': name, 'url': url, 'key': key, 'model': model,
                          'enabled': True, 'api': api})
        try:
            _write_env(agent.providers_to_env(providers))
        except Exception as e:
            return _log_and_error(e, message=f'Could not save: {e}')
        agent.forget_provider_capabilities()
    return jsonify({'ok': True, 'providers': [_provider_public(p) for p in providers]})


@app.route('/api/providers/<name>', methods=['PATCH'])
def api_toggle_provider(name):
    """Flip a provider's 'enabled' or 'api' without dropping its saved
    url/key/model. Either field alone is a valid body."""
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    if 'enabled' not in data and 'api' not in data:
        return jsonify({'error': "Body must include 'enabled' or 'api'."}), 400
    if 'api' in data and data.get('api') not in agent.PROVIDER_APIS:
        return jsonify({'error': f"Unknown api '{data.get('api')}'."}), 400
    with config_lock:
        providers = agent.read_providers()
        match = next((p for p in providers if p.get('name') == name), None)
        if match is None:
            return jsonify({'error': 'No such provider'}), 404
        if 'enabled' in data:
            match['enabled'] = bool(data.get('enabled'))
        if 'api' in data:
            match['api'] = data['api']
        try:
            _write_env(agent.providers_to_env(providers))
        except Exception as e:
            return _log_and_error(e, message=f'Could not save: {e}')
        agent.forget_provider_capabilities()
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
    with config_lock:
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
        agent.forget_provider_capabilities()
    return jsonify({'ok': True, 'providers': [_provider_public(p) for p in reordered]})


@app.route('/api/providers/<name>', methods=['DELETE'])
def api_delete_provider(name):
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    with config_lock:
        providers = agent.read_providers()
        remaining = [p for p in providers if p.get('name') != name]
        if len(remaining) == len(providers):
            return jsonify({'error': 'No such provider'}), 404
        try:
            _write_env(agent.providers_to_env(remaining))
        except Exception as e:
            return _log_and_error(e, message=f'Could not save: {e}')
        agent.forget_provider_capabilities()
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
    caller holds this user's conversation lock, so this can't race add_ping
    rewriting the same file from a turn of theirs."""
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
        # Hold this user's conversation lock across pop+deliver: it serialises
        # the scheduler against their live chat turns and against add_ping
        # writing the same ping.md. Only theirs, so a burst of pings for one
        # user no longer blocks everybody else's chat — and the lock is still
        # released between users.
        #
        # `sessions.use()` rather than a bare activate_session(): this is a
        # long-lived background thread that walks every user in turn, so the
        # binding has to be scoped to each delivery instead of being left
        # behind on the thread for the next user to inherit.
        sess = sessions.for_user(name)
        with sess.lock, sessions.use(sess):
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
    # FC_DEBUG=0 turns off the reloader and the interactive debugger. Defaults
    # to on, so a native install behaves exactly as before; the Docker image
    # sets it to 0.
    debug = os.getenv("FC_DEBUG", "1").strip().lower() not in ("0", "false", "no", "off")

    # debug=True runs Werkzeug's reloader, which re-execs this module in a
    # child process; only that child has WERKZEUG_RUN_MAIN set. Start the
    # scheduler there so pings aren't fired twice (once per process). With the
    # reloader off there is only ever one process and WERKZEUG_RUN_MAIN is
    # never set, so start it directly or pings would never fire at all.
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_ping_scheduler()
        # Same one-process-only guard: under the reloader this would otherwise
        # run in both the parent and the child. No-op unless FC_TELEMETRY=1.
        telemetry.maybe_send_install_ping()

    app.run(host='0.0.0.0', port=6767, debug=debug)