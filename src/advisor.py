"""The advisor: a second model that checks the agent's work.

The agent's own model is chosen to be cheap enough to run every step. Now and
then it goes wrong in a way it can't see from the inside: it loops on a tool,
says it did something a tool refused, saves the same note over and over, or
writes a mistake into its memory. The advisor is a stronger model, called only
when something looks off, that reads a short account of the turn and says what
to fix. It never acts. The agent reads the advice and does the fixing with its
own tools, under the same approvals as everything else it does.

It is called:

  * **in trouble, mid-turn** (`trouble()`): a call held back as a loop, a
    third failed tool result, or the turn passing STEP_TRIGGER tool calls.
    The advice rides on the tool result the agent reads next.
  * **after the reply** (`after_reply()`): when the reply check (src/jev.py
    follow_through) says a reviewer would likely find a problem or is sure the
    reply gives up ("can't"), or, with no Jev, when the turn had trouble the
    advisor didn't already see. Advice that
    needs acting on comes back as a note and the turn carries on, the way a
    follow-through nudge does.
  * **when the agent asks** (the ask_advisor tool), once a turn.
  * **for a memory check-up** (`checkup_due()` / `checkup()`): at most once a
    day, on a day context.md changed, it reads the whole file and lists
    duplicates, contradictions and stale notes. The list reaches the agent as
    a background turn of its own (like a sub-agent's result), so the cleanup
    happens with the agent's own edit_file.

What it sees is a digest of the turn, not the conversation: the request, each
tool call with its arguments and result cut short, the reply, and the tools the
agent had (a line each), so it can see a way the agent missed. Its tools only
read, and only inside the agent's own folder: the memory file, a file, past
conversations, the schedule, one of the agent's tools in full. At most one round of them, then it answers.

At most TURN_MAX calls a turn (one more when a loop was caught). Off unless
FC_ADVISOR is set; FC_ADVISOR_MODEL names its model (blank: each provider's
own). A host can gate it per user (`allowed`) and hear about each call
(`spent`), e.g. to cap what it costs.
"""
import json
import os
import re
import threading
import time

from src.logging_setup import get_logger

logger = get_logger(__name__)

ENV = "FC_ADVISOR"
MODEL_ENV = "FC_ADVISOR_MODEL"
# On a request's kwargs: this call is the advisor's. _prepare_kwargs swaps in
# the advisor's model and drops the key before anything goes over the wire.
ADVISOR_KEY = "_fc_advisor"
TOOL_NAME = "ask_advisor"

TURN_MAX = 1            # advisor calls a turn
LOOP_EXTRA = 1          # one more when the trouble was a loop
FAIL_TRIGGER = 3        # failed tool results in a turn that call it in
STEP_TRIGGER = 25       # tool calls in a turn that call it in
CANT_THRESHOLD = 0.7    # the reply check's certainty a reply gives up ("cant") that calls it in
CHECKUP_EVERY = 20 * 3600
STATE_FILE = ".advisor.json"

REQUEST_CLIP = 1500
REPLY_CLIP = 2000
ARG_CLIP = 240
RESULT_CLIP = 300
ACTIONS_SHOWN = 40
MEMORY_CLIP = 24000
TOOL_RESULT_CLIP = 6000
QUESTION_CLIP = 1000
TOOL_LINE_CLIP = 110    # each of the agent's tools in the digest: its name and this much of its description
TOOLS_SHOWN = 80

NOTE_PREFIX = "[Advisor check, not typed by the user]"
CHECKUP_PREFIX = "[Advisor memory check-up, not typed by the user]"
CHECKUP_KIND = "advisor_checkup"
MID_TURN_PREFIX = "[Advisor, a second model checking this turn — not the user]"


def enabled():
    return os.environ.get(ENV, "").strip().lower() in ("1", "on", "true", "yes")


def model():
    return os.environ.get(MODEL_ENV, "").strip() or None


# ── host hooks ──────────────────────────────────────────────

def allowed(user):
    """Whether `user`'s agent may call the advisor now. A host replaces this
    (an account's switch, a monthly cap)."""
    return True


def spent(user, kind, usage):
    """Told after each advisor request: `kind` is why it was called, `usage`
    the provider's token counts (or None). A host may replace it."""


# ── the prompt ──────────────────────────────────────────────

SYSTEM_PROMPT = """You are the advisor: a careful second model that checks the work of an AI agent which acts for one person (their assistant). The agent uses a cheaper model. You see a digest of what it just did and say what, if anything, it should fix. You never act yourself and never talk to the person; the agent reads your answer.

Check the work against what was asked and what the tools actually returned:
- Did it do what was asked, or stop partway with work it could still do?
- Does the reply say anything the tool results don't support: something done that failed, was refused or never ran, or facts that aren't in what it read?
- Is it repeating itself: the same call, or the same note saved again in other words?
- Did it save something wrong, duplicated or contradictory to memory, or schedule something wrong or twice?
- Did it send, post, buy or change anything the person didn't ask for?
- When it is stuck or says it can't: is there a different way to do it with the tools it has (listed in the digest), or should it stop and tell the person?

Be brief and concrete. Name the problem and the smallest fix the agent can make with its own tools (for example: edit_file on context.md to remove two duplicate lines, cancel_ping on the duplicate, tell the person the email was not sent). If the work is fine, say so and stop: most work is fine, and inventing problems costs the person money. Don't restate the work, don't add new tasks, and don't ask for anything the person didn't want.

Only call something a problem when the digest or your checks show it. The agent knows things you don't (its own name and addresses, what the person told it before), so a claim you can't check is not a mistake: check it with a tool, or let it stand. Judge the work by what this request needed, not by how much more could be done.

You have read-only tools for checking: the agent's memory file, its files, past conversations, its schedule, and the full description of any of its tools. Use one only when the digest isn't enough to judge, at most one round.

Answer with JSON only:
{"verdict": "fine" | "fix", "advice": "what the agent should do, one to five short lines; empty when fine"}"""

CHECKUP_PROMPT = """You are the advisor: a careful second model that looks after an AI agent's memory. Below is the agent's memory file (context.md), the notes it keeps about the one person it works for. It is sent with every message, so every wasted line costs money and every wrong one misleads the agent.

List what should change:
- the same fact saved more than once (keep the clearest one);
- notes that contradict each other (keep the later or better-supported one, and say which);
- notes about the agent's own process rather than about the person (plans, reminders to itself, "not done yet"), once they no longer apply;
- clearly stale notes that a later note supersedes.

Don't rewrite notes that are fine, don't add facts, and don't remove anything the person told it that still holds. Quote the lines to change exactly as they appear so the agent can edit them with edit_file.

Answer with JSON only:
{"verdict": "fine" | "fix", "advice": "the changes, one per line; empty when fine"}"""

TOOLS = [
    {"type": "function", "function": {
        "name": "read_memory",
        "description": "The agent's whole memory file (context.md).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Reads one of the agent's files (not context.md: use read_memory).",
        "parameters": {"type": "object", "properties": {"filename": {"type": "string"}},
                       "required": ["filename"]}}},
    {"type": "function", "function": {
        "name": "search_history",
        "description": "Searches the agent's past conversations for keywords.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "list_scheduled",
        "description": "The agent's scheduled tasks and reminders.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "describe_tool",
        "description": "One of the agent's tools in full: its whole description and its parameters.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}},
                       "required": ["name"]}}},
]


def ask_tool():
    """The agent's own way in."""
    return {"type": "function", "function": {
        "name": TOOL_NAME,
        "description": ("Asks the advisor, a stronger model, to check your work so far this turn and "
                        "suggest a fix. Once a turn. For when you're stuck, a tool keeps failing, or "
                        "you're unsure the result is right; not for routine work."),
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "What you want checked or are stuck on"}},
            "required": ["question"]}}}


# ── failure signals ─────────────────────────────────────────

# A tool result that says the step failed. Generic signals only, never a
# particular site or task's.
_FAILED = re.compile(r'^(?:\[[^\]]*\]\s*)?(?:Error\b|Tool reported an error|This was NOT done)'
                     r'|\bexit [1-9]\d* ·|\btimed out after \d|"successful":\s*false|\bwas NOT run\b')


def failed(result):
    """Whether a tool result says its step failed. One held for the person's
    approval, or refused by them, isn't the model's failure."""
    text = str(result or "")[:3000]
    if "the user said no" in text[:400] or ("approval" in text[:400] and "NOT done" in text[:400]):
        return False
    return bool(_FAILED.search(text))


# ── per-turn state (on the Session, reset with the turn's tool record) ──

def _turn(sess):
    record = getattr(sess, "turn_tool_names", None)
    held = getattr(sess, "_advisor_turn", None)
    if held is None or held[0] is not record:
        held = (record, {"calls": 0, "fails": 0, "loop": False, "steps": 0, "held": 0,
                         "trouble_seen": False, "results": []})
        sess._advisor_turn = held
    return held[1]


def _budget(state):
    return TURN_MAX + (LOOP_EXTRA if state["loop"] else 0)


def can_call(sess, user):
    if not enabled() or getattr(sess, "depth", 0):
        return False
    if _turn(sess)["calls"] >= _budget(_turn(sess)):
        return False
    try:
        return bool(allowed(user))
    except Exception:
        logger.exception("advisor.allowed failed")
        return False


def note_tool(sess, name, result, held_back=None):
    """Count one tool result toward the turn's trouble. Returns the trouble
    to call the advisor for, or None. `held_back` is why the repeat guard held
    the call back, if it did: the same call again ("identical_repeat") is a
    loop at once; a run of one tool is common in honest work (three files read
    in a row), so it's a loop the second time this turn."""
    state = _turn(sess)
    state["steps"] += 1
    if held_back:
        state["held"] += 1
        if held_back == "identical_repeat" or state["held"] >= 2:
            if state["loop"]:
                return None
            state["loop"] = True
            return "the agent kept repeating a tool call after being held back"
        return None
    if name != TOOL_NAME and failed(result):
        state["fails"] += 1
        if state["fails"] == FAIL_TRIGGER:
            return f"{FAIL_TRIGGER} tool steps failed this turn"
    if state["steps"] == STEP_TRIGGER:
        return f"the turn passed {STEP_TRIGGER} tool calls"
    return None


def had_trouble(sess):
    state = _turn(sess)
    return (state["loop"] or state["fails"] >= FAIL_TRIGGER or state["steps"] >= STEP_TRIGGER) \
        and not state["trouble_seen"]


# ── the digest ──────────────────────────────────────────────

def _clip(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _content(m):
    c = m.get("content")
    if isinstance(c, list):
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return str(c or "")


def _agent_tools(sess):
    """[(tool, loadable)] the agent had this turn: what was sent, then what a
    routed turn withheld and the agent could still load with load_tools."""
    prefix = getattr(sess, "turn_prefix", None) or {}
    out = [(t, False) for t in prefix.get("tools") or ()]
    for entry in (getattr(sess, "turn_withheld", None) or {}).values():
        try:
            out += [(t, True) for t in entry[1]]
        except (IndexError, TypeError):
            pass
    for tools in (getattr(sess, "turn_trimmed", None) or {}).values():
        out += [(t, True) for t in tools]
    return [(t, loadable) for t, loadable in out
            if isinstance(t, dict) and _tool_fn(t).get("name") not in (TOOL_NAME, "load_tools")]


def _tool_fn(t):
    return t.get("function") if isinstance(t.get("function"), dict) else t


def tool_lines(sess):
    """The agent's tools as the digest lists them, a line each."""
    sent, loadable = [], []
    for t, can_load in _agent_tools(sess)[:TOOLS_SHOWN]:
        fn = _tool_fn(t)
        line = f"- {fn.get('name')}: {_clip(fn.get('description'), TOOL_LINE_CLIP)}"
        (loadable if can_load else sent).append(line)
    if loadable:
        sent.append("Not sent this turn, but it could load them with load_tools:")
        sent += loadable
    return sent


def describe_tool(sess, name):
    for t, can_load in _agent_tools(sess):
        fn = _tool_fn(t)
        if fn.get("name") == name:
            params = json.dumps((fn.get("parameters") or {}).get("properties") or {})
            return (f"{name}{' (loadable with load_tools)' if can_load else ''}\n"
                    f"{fn.get('description') or ''}\nParameters: {params}")[:TOOL_RESULT_CLIP]
    return f"The agent had no tool named {name} this turn."


def digest(messages, reply=None, trouble=None, question=None, tools=None):
    """The turn as the advisor reads it. `tools`: tool_lines(), the tools the
    agent had."""
    start = next((i for i in range(len(messages) - 1, 0, -1)
                  if messages[i].get("role") == "user" and not messages[i].get("auto")), 0)
    request = _content(messages[start]) if start else ""
    results = {m.get("tool_call_id"): _content(m) for m in messages[start:] if m.get("role") == "tool"}
    lines = []
    for m in messages[start + 1:]:
        if m.get("role") == "user" and m.get("auto"):
            lines.append("(note to the agent: " + _clip(_content(m), 200) + ")")
        for call in m.get("tool_calls") or ():
            fn = call.get("function") or {}
            lines.append(f"{fn.get('name')}({_clip(fn.get('arguments'), ARG_CLIP)})\n"
                         f"  -> {_clip(results.get(call.get('id'), '(no result yet)'), RESULT_CLIP)}")
    if len(lines) > ACTIONS_SHOWN:
        lines = [f"({len(lines) - ACTIONS_SHOWN} earlier steps not shown)"] + lines[-ACTIONS_SHOWN:]
    parts = ["REQUEST:\n" + (_clip(request, REQUEST_CLIP) or "(none)"),
             "WHAT IT DID THIS TURN:\n" + ("\n".join(lines) or "(no tool calls)")]
    if reply is not None:
        parts.append("ITS REPLY:\n" + (str(reply)[-REPLY_CLIP:] or "(empty)"))
    if tools:
        parts.append("TOOLS IT HAD:\n" + "\n".join(tools))
    if trouble:
        parts.append("WHY YOU WERE CALLED: " + trouble)
    if question:
        parts.append("THE AGENT ASKS: " + _clip(question, QUESTION_CLIP))
    return "\n\n".join(parts)


# ── one advisor call ────────────────────────────────────────

def _run_advisor_tool(name, args):
    import src.agent as agent
    sess = agent._sess()
    if name == "read_memory":
        text = agent._read_context()
        return text[:MEMORY_CLIP] or "(empty)"
    if name == "list_scheduled":
        try:
            with open(sess.static_dir + "ping.md", "r", encoding="utf-8") as f:
                return f.read()[:TOOL_RESULT_CLIP] or "(nothing scheduled)"
        except OSError:
            return "(nothing scheduled)"
    if name == "describe_tool":
        return describe_tool(sess, str((args or {}).get("name") or "") if isinstance(args, dict) else "")
    if name in ("read_file", "search_history"):
        return str(agent._run_tool(name, args if isinstance(args, dict) else {}))[:TOOL_RESULT_CLIP]
    return f"No tool named {name}."


def _request(messages, tools):
    """One streamed request on the agent's provider chain, as the advisor.
    Returns (text, tool_calls, reasoning_items, usage)."""
    import src.agent as agent
    stream, _provider = agent._create_completion(
        model=model() or "advisor", messages=messages, temperature=0, top_p=1,
        tools=tools, stream=True, **{ADVISOR_KEY: True})
    text, calls, items, usage = "", {}, None, None
    for chunk in stream:
        u = getattr(chunk, "usage", None)
        if u:
            usage = u if isinstance(u, dict) else getattr(u, "__dict__", None) or usage
        got = getattr(chunk, "reasoning_items", None)
        if got:
            items = got
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        if getattr(delta, "content", None):
            text += delta.content
        for tc in getattr(delta, "tool_calls", None) or ():
            slot = calls.setdefault(tc.index, {"id": None, "type": "function",
                                               "function": {"name": "", "arguments": ""}})
            if tc.id:
                slot["id"] = tc.id
            if tc.function is not None:
                if tc.function.name:
                    slot["function"]["name"] += tc.function.name
                if tc.function.arguments:
                    slot["function"]["arguments"] += tc.function.arguments
    return text, [calls[k] for k in sorted(calls)], items, usage


def parse(text):
    """{"verdict": "fine"|"fix", "advice": str} from the advisor's answer,
    whatever shape it came in."""
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            verdict = str(data.get("verdict") or "").lower()
            advice = str(data.get("advice") or "").strip()
            if verdict in ("fine", "fix"):
                return {"verdict": "fix" if verdict == "fix" and advice else "fine", "advice": advice}
        except (ValueError, AttributeError):
            pass
    if not text or re.match(r"(?i)\W*(fine|looks fine|ok|no (problems|issues))\b", text):
        return {"verdict": "fine", "advice": ""}
    return {"verdict": "fix", "advice": text[:2000]}


def call(user, kind, content, system=SYSTEM_PROMPT, tools=True):
    """Ask the advisor about `content`. Returns {"verdict", "advice", "ms"}
    or None when it couldn't answer. Never raises."""
    started = time.monotonic()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    try:
        text, calls, items, usage = _request(messages, TOOLS if tools else None)
        _spent(user, kind, usage)
        if calls:
            assistant = {"role": "assistant", "content": text or None, "tool_calls": calls}
            if items:
                assistant["reasoning_items"] = items
            messages.append(assistant)
            for c in calls:
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                except ValueError:
                    args = {}
                try:
                    out = _run_advisor_tool(c["function"]["name"], args)
                except Exception as e:                   # noqa: BLE001
                    out = f"Error: {e}"
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": out})
            text, _calls, _items, usage = _request(messages, None)
            _spent(user, kind, usage)
    except Exception:
        logger.exception("The advisor couldn't answer")
        return None
    out = parse(text)
    out["ms"] = int((time.monotonic() - started) * 1000)
    out["kind"] = kind
    return out


def _spent(user, kind, usage):
    try:
        spent(user, kind, usage)
    except Exception:
        logger.exception("advisor.spent failed")


# ── the three ways in, for agent_stream ─────────────────────

def _review(sess, user, kind, messages, reply=None, trouble=None, question=None):
    state = _turn(sess)
    state["calls"] += 1
    if trouble:
        state["trouble_seen"] = True
    out = call(user, kind, digest(messages, reply=reply, trouble=trouble, question=question,
                                  tools=tool_lines(sess)))
    if out:
        state["results"].append(out)
        logger.info("Advisor (%s): %s in %sms", kind, out["verdict"], out["ms"])
    return out


def mid_turn(sess, user, messages, trouble):
    """Advice to put on the tool result the agent reads next, or None."""
    if not can_call(sess, user):
        return None
    out = _review(sess, user, "trouble", messages, trouble=trouble)
    if not out or out["verdict"] != "fix":
        return None
    return out


def ask(sess, user, messages, question):
    """The ask_advisor tool's result."""
    if not enabled():
        return "The advisor isn't on here."
    if not can_call(sess, user):
        try:
            if not allowed(user):
                return "The advisor is off for this account right now. Carry on without it."
        except Exception:                                # noqa: BLE001
            pass
        return "The advisor has already been asked this turn. Carry on with what it said."
    out = _review(sess, user, "asked", messages, question=question)
    if not out:
        return "The advisor couldn't answer just now. Carry on without it."
    if out["verdict"] == "fine":
        return "The advisor looked and sees nothing wrong" + (f": {out['advice']}" if out["advice"] else ".")
    return "The advisor says:\n" + out["advice"]


def after_reply(sess, user, messages, reply, follow):
    """After the reply: the advisor's verdict when it should look, else None.
    `follow` is the reply check's verdict (src/jev.py), which may carry
    "advisor", Jev's estimate that a reviewer would find a problem."""
    if not can_call(sess, user):
        return None
    why = None
    if follow and follow.get("advisor"):
        why = "the reply check flagged this turn for review"
    elif gave_up(follow):
        why = ("the reply says it can't do this; check whether a tool it had (or could load) "
               "would have done it")
    elif had_trouble(sess):
        why = "this turn had trouble: " + ", ".join(
            w for w, on in (("a loop was held back", _turn(sess)["loop"]),
                            ("tool steps failed", _turn(sess)["fails"] >= FAIL_TRIGGER),
                            ("many tool calls", _turn(sess)["steps"] >= STEP_TRIGGER)) if on)
    if not why:
        return None
    return _review(sess, user, "reply", messages, reply=reply, trouble=why)


def gave_up(follow):
    """The reply check is sure the reply gives up on the request."""
    if not follow or follow.get("status") != "cant":
        return False
    try:
        return float((follow.get("shares") or {}).get("cant", 0.0)) >= CANT_THRESHOLD
    except (TypeError, ValueError):
        return False


def results(sess):
    """This turn's advisor verdicts, for the route panel."""
    return [{k: r.get(k) for k in ("kind", "verdict", "advice", "ms")} for r in _turn(sess)["results"]]


# ── the memory check-up ─────────────────────────────────────

def _state_path(static_dir):
    """Beside conversation.json, not in the files folder, so the agent's own
    file tools don't see it."""
    return os.path.join(os.path.dirname(os.path.normpath(static_dir)), STATE_FILE)


def _read_state(static_dir):
    try:
        with open(_state_path(static_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(static_dir, data):
    path = _state_path(static_dir)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        logger.exception("Couldn't save the advisor's state")


_checkups = set()
_checkups_lock = threading.Lock()


def checkup_due(user, static_dir, now=None):
    """Whether `user`'s memory is due a check-up: it changed since the last
    one, and that was CHECKUP_EVERY ago or more."""
    if not enabled() or not user or not static_dir:
        return False
    try:
        mtime = os.path.getmtime(os.path.join(static_dir, "context.md"))
    except OSError:
        return False
    if not os.path.isdir(os.path.dirname(_state_path(static_dir))):
        return False
    state = _read_state(static_dir)
    now = time.time() if now is None else now
    if mtime <= float(state.get("memory_mtime") or 0):
        return False
    return now - float(state.get("checkup_at") or 0) >= CHECKUP_EVERY


def checkup(user, static_dir, memory):
    """Run the check-up on `memory` (context.md's text). Records it whatever
    the answer, so a failure isn't retried every turn. The verdict or None."""
    with _checkups_lock:
        if user in _checkups:
            return None
        _checkups.add(user)
    try:
        try:
            mtime = os.path.getmtime(os.path.join(static_dir, "context.md"))
        except OSError:
            mtime = time.time()
        _write_state(static_dir, {**_read_state(static_dir), "checkup_at": time.time(),
                                  "memory_mtime": mtime})
        if not memory.strip():
            return None
        out = call(user, "checkup", "MEMORY FILE (context.md):\n" + memory[:MEMORY_CLIP],
                   system=CHECKUP_PROMPT, tools=False)
        if out:
            logger.info("Advisor memory check-up: %s in %sms", out["verdict"], out["ms"])
        return out
    finally:
        with _checkups_lock:
            _checkups.discard(user)


def mark_checkup(messages, message):
    """Mark the check-up's message, once its turn is over, as FreeClaw's own
    (auto, auto_kind "advisor_checkup"), so the chat shows a note rather than
    the person's bubble. Not before: during the turn it is the request the
    reply is checked against."""
    for m in reversed(messages):
        if m.get("role") == "user" and m.get("content") == message:
            m["auto"] = True
            m["auto_kind"] = CHECKUP_KIND
            return True
    return False


def start_checkup(user, static_dir):
    """Run the memory check-up on a thread of its own, once `user`'s current
    turn has finished (it waits for the Session's lock, as a sub-agent's
    result does), and when it finds something, hand it to the agent as a turn
    of its own so the agent fixes its memory with its own tools."""
    threading.Thread(target=_checkup_turn, args=(user, static_dir),
                     name=f"advisor-checkup:{user}", daemon=True).start()


def _checkup_turn(user, static_dir):
    import src.agent as agent
    import src.cancellation as cancellation
    import src.live_turns as live_turns
    import src.session as sessions
    from src.users import activate_session, save_conversation, user_exists
    if not user_exists(user):
        return
    sess = sessions.for_user(user)
    with sess.lock, sessions.use(sess):
        try:
            activate_session(user)
            if not allowed(user) or not checkup_due(user, static_dir):
                return
            out = checkup(user, static_dir, agent._read_context())
            if not out or out["verdict"] != "fix":
                return
            message = (f"{CHECKUP_PREFIX} The advisor read your memory file and suggests these "
                       f"changes. Make the ones that are right with edit_file on context.md, then "
                       f"reply in one short line saying what you changed.\n\n{out['advice']}")
            cancellation.begin_turn(sess)
            try:
                with live_turns.turn(user, message, "advisor") as live:
                    for _event in live.watch(agent.agent_stream(user_input=message, ping=True,
                                                                auto_kind=CHECKUP_KIND)):
                        pass
                    mark_checkup(agent.get_messages(), message)
                    save_conversation(user, agent.get_messages())
            finally:
                cancellation.end_turn(sess)
            # Its own edits aren't a reason for another check-up tomorrow.
            try:
                mtime = os.path.getmtime(os.path.join(static_dir, "context.md"))
                _write_state(static_dir, {**_read_state(static_dir), "memory_mtime": mtime})
            except OSError:
                pass
            logger.info("Advisor memory check-up delivered to %s", user)
        except Exception:
            logger.exception("The advisor's memory check-up for %s failed", user)
