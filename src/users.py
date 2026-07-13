"""User / conversation storage, shared by the Flask app (Flask/main.py) and
the CLI (src/cli.py) so both entry points read and write the exact same
on-disk layout under Flask/static/<user>/."""

import os
import re
import json
import shutil
import time

import src.agent as agent

BASE_DIR = os.path.dirname(os.path.realpath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "..", "Flask", "static")
os.makedirs(STATIC_DIR, exist_ok=True)

CONVERSATIONS_SUBDIR = "conversations"
RESERVED_NAMES = {"conversations", "uploads"}


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


def conversation_path(name):
    return os.path.join(user_dir(name), "conversation.json")


def conv_files_dir(name):
    """Folder where this user's created/uploaded files live, e.g.
    static/<user>/files/ — also where their context.md (long-term memory)
    lives, so the agent's normal file tools can read/edit it directly.
    Kept separate from the conversation's metadata JSON file."""
    path = os.path.join(user_dir(name), "files")
    os.makedirs(path, exist_ok=True)
    return path


def _migrate_legacy_conversations(name):
    """Older versions of FreeClaw gave each user many chats, stored as
    static/<user>/conversations/<id>.json (each with its own files/
    subfolder). Now every user has exactly one conversation, so collapse
    that down: keep the most recently updated chat's history and files as
    this user's single conversation, and drop the rest."""
    legacy_dir = os.path.join(user_dir(name), CONVERSATIONS_SUBDIR)
    if not os.path.isdir(legacy_dir):
        return
    latest_id, latest_data, latest_ts = None, None, -1
    for fname in os.listdir(legacy_dir):
        if not fname.endswith(".json"):
            continue
        conv_id = fname[:-5]
        try:
            with open(os.path.join(legacy_dir, fname), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        ts = data.get("updated_at", 0)
        if ts > latest_ts:
            latest_id, latest_data, latest_ts = conv_id, data, ts
    if latest_data is not None and not os.path.exists(conversation_path(name)):
        with open(conversation_path(name), "w", encoding="utf-8") as f:
            json.dump(latest_data, f)
        legacy_files_dir = os.path.join(legacy_dir, latest_id)
        if os.path.isdir(legacy_files_dir):
            dest = conv_files_dir(name)
            for item in os.listdir(legacy_files_dir):
                shutil.move(os.path.join(legacy_files_dir, item), os.path.join(dest, item))
    shutil.rmtree(legacy_dir, ignore_errors=True)


def user_context_path(name):
    """Path to this user's context.md — inside their files folder (not
    user_dir directly) so it's reachable by the agent's normal read_file/
    edit_file/create_file tools instead of needing a dedicated tool.
    Calling this creates static/<user>/files/ as a side effect, same as
    conv_files_dir()."""
    return os.path.join(conv_files_dir(name), "context.md")


def list_users():
    if not os.path.isdir(STATIC_DIR):
        return []
    users_found = []
    for entry in sorted(os.listdir(STATIC_DIR)):
        if entry.lower() in RESERVED_NAMES or entry.startswith('.'):
            continue
        full = os.path.join(STATIC_DIR, entry)
        try:
            if os.path.isdir(full):
                users_found.append(entry)
        except OSError:
            continue
    return users_found


def user_exists(name):
    return name in list_users()


def create_user(name):
    ctx_path = user_context_path(name)  # creates static/<user>/files/ too
    if not os.path.exists(ctx_path):
        with open(ctx_path, "w", encoding="utf-8") as f:
            f.write("")
    ensure_conversation(name)


def load_conversation(name):
    path = conversation_path(name)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_conversation(name, messages, title=None):
    path = conversation_path(name)
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


def ensure_conversation(name):
    """Make sure `name` has a conversation.json, migrating an older
    multi-chat layout if one is found, or creating a fresh conversation
    (via agent.reset(), scoped to this user's own files folder, which also
    holds their long-term context.md) if there's nothing to migrate."""
    _migrate_legacy_conversations(name)
    if not os.path.exists(conversation_path(name)):
        agent.set_static_dir(conv_files_dir(name))
        agent.reset()
        save_conversation(name, agent.get_messages(), title="New chat")


def activate_session(name):
    """Point the agent module's globals at this user's file folder (which
    holds their context.md alongside created/uploaded files), and load
    their saved conversation messages so the next agent_stream() call
    continues the right thread."""
    ensure_conversation(name)
    agent.set_static_dir(conv_files_dir(name))
    data = load_conversation(name)
    agent.set_messages(data.get("messages", []))
