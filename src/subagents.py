"""Sub-agents that work beside the main agent instead of inside its turn.

spawn_subagent used to run the child to completion inside the tool call: the
main agent sat blocked until it finished, and nobody could see what it was
doing. Now the call starts the child and returns at once, and the child is a
real FreeClaw user of its own for as long as it runs:

  * It shows on the homepage, marked as a sub-agent, and opening it is the
    chat page following its turn live (src/live_turns.py) — its thinking, its
    tool calls, its browser.
  * It starts from the main agent's conversation and memory: the parent's
    messages are copied in, its file tools point at the parent's files folder
    (context.md included, so whatever it writes there stays), and it has a
    copy of the parent's browser logins and app choices. Its own browser,
    though: two agents driving one page would fight over it.
  * When it finishes, its last reply goes back to the main agent as a new
    message (a turn of its own, like a scheduled ping, that waits for any turn
    already running there), and the sub-agent is deleted.

One level deep: a sub-agent can't start its own (agent.MAX_SUBAGENT_DEPTH).

Installed by Flask/main.py (agent.set_subagent_launcher). Without it — the
CLI — spawn_subagent runs the child inline the way it always did.
"""

import json
import os
import shutil
import threading
import time

import src.agent as agent
import src.approvals as approvals
import src.browser_profiles as browser_profiles
import src.cancellation as cancellation
import src.live_turns as live_turns
import src.mcp_client as mcp_client
import src.session as sessions
from src.logging_setup import get_logger
from src.users import (activate_session, conv_files_dir, create_user, list_users,
                       save_conversation, user_dir, user_exists)

logger = get_logger(__name__)

# Marks a user folder as a sub-agent's: who it works for and on what. In the
# user folder, not files/, so the agent's own file tools can't reach it.
MARKER = ".subagent.json"

# The per-user settings a sub-agent inherits: which apps are switched on and
# the saved bash rules. Both live beside conversation.json in the user folder.
INHERITED_FILES = (mcp_client.PREFS_FILENAME, approvals.RULES_FILENAME)

# How many may run at once for one main agent. Each can have a browser of its
# own, and those are the expensive part of a small server.
MAX_RUNNING = max(1, int(os.environ.get("FC_MAX_SUBAGENTS") or 3))

# The message that brings a result back to the main agent starts with this,
# and the spawn tool's description tells the model so.
RESULT_PREFIX = "SUB-AGENT RESULT"

# How a user's name is shown, to the person and to the agents. A hosted
# install replaces it to leave out the account prefix.
display_name = lambda name: name  # noqa: E731

# Users whose names are taken by a child being set up, so two spawns at once
# can't pick the same one.
_claim_lock = threading.Lock()
_claimed = set()


def info(name):
    """{parent, task, started} if `name` is a sub-agent, else None."""
    try:
        with open(os.path.join(user_dir(name), MARKER), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("parent") else None


def children(parent):
    """The sub-agents working for `parent` right now."""
    return [n for n in list_users() if (info(n) or {}).get("parent") == parent]


def _child_name(parent):
    """"<parent>-subagent-N", the first N free, cut to fit the 40-character
    name limit from the parent's end so a hosted account prefix survives. No
    spaces of its own: browser_profiles only keeps logins for a plain name."""
    for n in range(1, 100):
        suffix = f"-subagent-{n}"
        name = parent[:40 - len(suffix)].rstrip() + suffix
        if name not in _claimed and not user_exists(name):
            return name
    return None


def launch(parent_sess, task):
    """Start a sub-agent on `task` for the main agent in `parent_sess` and
    return what the tool call answers with. Runs in the parent's turn, so it
    only sets the child up; the work happens on a thread of its own."""
    parent = parent_sess.name
    if not parent or not user_exists(parent):
        return "Error: sub-agents can only be started from a saved conversation."
    running = children(parent)
    if len(running) >= MAX_RUNNING:
        return (f"Error: {len(running)} sub-agents are already working for you "
                f"({', '.join(map(display_name, running))}), the most at once. Wait for one to report back, "
                "or do this yourself.")
    with _claim_lock:
        child = _child_name(parent)
        if child is None:
            return "Error: no free name for another sub-agent."
        _claimed.add(child)
    try:
        _set_up(parent_sess, child, task)
    except Exception as e:
        logger.exception("Couldn't set up a sub-agent for %s", parent)
        _delete(child)
        return f"Error: the sub-agent couldn't be started: {e}"
    finally:
        with _claim_lock:
            _claimed.discard(child)
    history = _settled(parent_sess.messages)
    threading.Thread(target=_run, args=(parent, child, task, history, parent_sess.static_dir),
                     name=f"subagent:{child}", daemon=True).start()
    logger.info("Started sub-agent %s for %s", child, parent)
    return (f"Started sub-agent \"{display_name(child)}\". It's working in parallel and the user can watch it "
            f"on the homepage. Its result will come back to you as a new message starting "
            f"\"{RESULT_PREFIX}\". Don't wait for it or do its task yourself: carry on with "
            f"anything else, or end your turn and tell the user it's on it.")


def _settled(messages):
    """The parent's conversation up to its last complete step. The call that
    started this sub-agent is still waiting on its answer, and a child shown
    that call half-done would read it as one that failed."""
    history = [m for m in messages if m.get("role") != "system"]
    for i in range(len(history) - 1, -1, -1):
        calls = history[i].get("tool_calls") if history[i].get("role") == "assistant" else None
        if not calls:
            continue
        answered = {m.get("tool_call_id") for m in history[i + 1:] if m.get("role") == "tool"}
        if any(c.get("id") not in answered for c in calls):
            return history[:i]
        break
    return history


def _set_up(parent_sess, child, task):
    create_user(child)
    parent = parent_sess.name
    for fname in INHERITED_FILES:
        src = os.path.join(user_dir(parent), fname)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(user_dir(child), fname))
    # Signed in wherever the main agent is. A copy of the file, not the file:
    # each browser saves its own logins back, and the child's go with it.
    saved = browser_profiles.state_path(parent)
    if saved and os.path.exists(saved) and browser_profiles.ensure_dir(child):
        try:
            with open(saved, "r", encoding="utf-8") as f:
                browser_profiles.write_state(child, json.load(f))
        except (OSError, ValueError):
            logger.exception("Couldn't copy %s's browser logins to its sub-agent", parent)
    with open(os.path.join(user_dir(child), MARKER), "w", encoding="utf-8") as f:
        json.dump({"parent": parent, "task": task, "started": time.time()}, f)
    # Set before anyone can open its chat, so the first look at it already
    # can't start another level down.
    sessions.for_user(child).depth = parent_sess.depth + 1


def _run(parent, child, task, history, workspace):
    """The sub-agent's whole life: its turn, then its result to the parent,
    then gone."""
    sess = sessions.for_user(child)
    reply, outcome = "", "finished"
    try:
        with sess.lock, sessions.use(sess):
            # The parent's folder for files and memory, not the child's own:
            # what it makes outlives it there.
            # Its own name for tools: its own browser, holding the copied
            # logins. Nobody can answer an approval prompt mid-run.
            approvals.begin_turn(child, False)
            agent.set_static_dir(workspace)
            agent.reset(subagent=True)
            agent.set_messages(agent.get_messages()[:1] + history)
            cancellation.begin_turn(sess)
            message = f"[Task from the main agent] {task}"
            try:
                # ping=True: every tool, no per-message routing. Routed like a
                # message, a task got a tool set picked for its wording, which
                # could leave out the browser it needed, and a sub-agent has
                # nobody to ask for the rest.
                with live_turns.turn(child, message, "task") as live:
                    for _event in live.watch(agent.agent_stream(user_input=message, ping=True)):
                        pass
                    save_conversation(child, agent.get_messages(), title=task[:60])
                if cancellation.is_stopped(sess):
                    outcome = "was stopped by the user"
            finally:
                cancellation.end_turn(sess)
            reply = next((m.get("content") for m in reversed(sess.messages)
                          if m.get("role") == "assistant" and isinstance(m.get("content"), str)
                          and m.get("content").strip()), "")
    except Exception as e:
        logger.exception("Sub-agent %s failed", child)
        outcome = f"failed ({e})"
    finally:
        _delete(child)
    _deliver(parent, child, task, outcome, reply)


def _deliver(parent, child, task, outcome, reply):
    """Hand the sub-agent's result to the main agent as a turn of its own,
    once whatever turn it's in now has finished."""
    if not user_exists(parent):
        logger.info("Sub-agent %s finished, but %s is gone", child, parent)
        return
    reply = (reply or "").strip()
    if len(reply) > agent.SUBAGENT_RESULT_LIMIT:
        reply = reply[:agent.SUBAGENT_RESULT_LIMIT] + "\n\n(truncated)"
    if not reply:
        reply = "(It reported nothing. Treat the task as not done.)"
    message = f"{RESULT_PREFIX} from \"{display_name(child)}\", which {outcome}. Its task: {task}\n\n{reply}"
    sess = sessions.for_user(parent)
    with sess.lock, sessions.use(sess):
        try:
            activate_session(parent)
            cancellation.begin_turn(sess)
            with live_turns.turn(parent, message, "subagent") as live:
                for _event in live.watch(agent.agent_stream(user_input=message, ping=True)):
                    pass
                save_conversation(parent, agent.get_messages())
            logger.info("Delivered sub-agent %s's result to %s", child, parent)
        except Exception:
            logger.exception("Couldn't deliver sub-agent %s's result to %s", child, parent)
        finally:
            cancellation.end_turn(sess)


def _delete(child):
    """Everything of the sub-agent's: its folder, its browser and saved logins,
    its in-memory conversation."""
    try:
        mcp_client.release_user_browser(child)
    except Exception:
        logger.exception("Couldn't stop sub-agent %s's browser", child)
    saved = browser_profiles.state_path(child)
    if saved:
        shutil.rmtree(os.path.dirname(saved), ignore_errors=True)
    shutil.rmtree(user_dir(child), ignore_errors=True)
    sessions.discard(child)


def recover():
    """Sub-agents a restart cut off: each is reported to its main agent as
    interrupted, and deleted. Call once at startup."""
    stale = [(n, info(n)) for n in list_users()]
    stale = [(n, i) for n, i in stale if i]
    for child, meta in stale:
        def report(child=child, meta=meta):
            _delete(child)
            _deliver(meta["parent"], child, meta.get("task") or "", "was cut off by a server restart",
                     "")
        threading.Thread(target=report, name=f"subagent-recover:{child}", daemon=True).start()
    return len(stale)
