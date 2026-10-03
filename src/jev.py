"""Turn routing with Jev, TypeSafe's decision model.

Optional. With a key in Settings → Jev (JEV_API_KEY in .env), each message the
user types is read by Jev before the main model sees it, and Jev decides what
that one turn is sent:

  * which tools — each one on its own, built-in and MCP alike, so a turn that
    only needs to read a file gets read_file and not the other eight file
    tools, and a browser turn gets navigate and click but not drag;
  * which MCP server is the way to do it, as one choice between them — see
    PATH_SHARE;
  * which earlier messages, picked individually — message 2 and message 4 and
    nothing in between is a valid answer — and for each picked reply, whether
    the tool results behind it come too;
  * which saved memory sections (context.md) are opened in full;
  * how exact the answer has to be, which sets the temperature;
  * a one-word label for the turn, shown in the chat.

None of it is tied to the Classy tag table: the label is only a label, and the
tools, history and temperature are each decided on their own.

Jev doesn't write text. It is handed a "state" and a set of typed questions,
and answers every question in one pass with a probability attached, in a few
hundred milliseconds. The state is counted once and each question adds ~20
tokens, so asking about a hundred tools and messages costs about what asking
about one does.

Without a key, or when Jev is slow or down, nothing here runs and the turn goes
through the local Classy classifier exactly as it always has: `route()` returns
None and agent_stream falls back. Jev can narrow a turn; it can never be the
reason one fails.

Leaning wide is deliberate everywhere below. A message or a tool the turn
didn't need costs a few hundred tokens; one it needed and didn't get costs a
wrong answer. So the thresholds sit well under 0.5, and the main model can
still load a withheld tool group mid-turn (load_tools, in agent.py).
"""
import os
import re
import time

import httpx

import src.mcp_client as mcp_client
from src.logging_setup import get_logger

logger = get_logger(__name__)

ENV_KEY = "JEV_API_KEY"
# Overridable for a proxy or a gateway that speaks the same API (OpenRouter's
# is https://openrouter.ai/api/alpha/decisions with an OpenRouter key).
JEV_URL = os.environ.get("JEV_URL", "https://api.typesafe.ai/v1/systemone")
# Pinned, not jev-latest: the thresholds below were set against this version,
# and a moving alias would change what 0.3 means without anyone noticing.
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-1.13.0")

# Jev answers in 150–450ms from a normal connection. Past this the turn stops
# waiting and routes the old way — a slow router is worse than a coarse one.
TIMEOUT_SECONDS = 3.0

# How far back Jev may reach, in messages that carry text (user messages and
# assistant replies; pure tool traffic rides with the reply it led to).
MAX_CANDIDATES = 80
# Characters of each earlier message shown to Jev. Enough to tell what it was
# about; the model gets the real thing if it's picked.
STATE_CLIP = 300
# Memory sections offered to Jev, and how much of each it sees.
MAX_SECTIONS = 40
SECTION_PREVIEW = 220
# How much of a tool's description Jev sees.
TOOL_DESC_CLIP = 150
# A server with more tools than this is asked about as a whole, not tool by
# tool: past it the list is a catalogue (Composio's can run to hundreds), and
# a question per entry buys nothing but a longer state.
MAX_TOOLS_PER_SERVER = 30

# Probability at or above which something is included. Low on purpose — see
# the module docstring.
TOOL_THRESHOLD = 0.3
MESSAGE_THRESHOLD = 0.3
RAW_THRESHOLD = 0.5
SECTION_THRESHOLD = 0.3

# Which MCP server the request goes through is asked once, as a choice between
# them, not server by server. Asked one at a time, "could the browser do it?"
# and "could Composio do it?" are both fair yeses for "what are my recent
# emails" — Gmail is a website too — so both went out, and they are the two
# most expensive tool sets there are. A choice has to put its weight
# somewhere. The winner always goes; a runner-up only with at least this
# share, for requests that genuinely need two ("send me those prices on
# Discord"), or where Jev is torn.
PATH_SHARE = 0.2
PATH_NONE = "none"

# The one-word label shown on the reply. Nothing else reads it.
TAG_CRITERIA = {
    "Followup": "Reacting to or continuing the previous reply or task: corrections, "
                "'yes', 'next', 'the second one', 'I logged in', 'keep going'.",
    "Code": "Writing, reading, or debugging code, queries, or scripts.",
    "Reason": "Explaining how or why something works, maths, logic, or comparing ideas.",
    "Compose": "Writing or brainstorming text: messages, posts, poems, lists, plans, ideas.",
    "Imagine": "Making a picture or other image.",
    "Websearch": "Needs current facts from the web: prices, news, hours, reviews, availability.",
    "Files": "The user's files, notes, documents, reminders, or schedule.",
    "Memory": "Telling the assistant something to remember, or asking what it remembers about them.",
    "System": "Operating the computer or an app: installs, shell commands, devices, sending messages.",
    "Control": "Steering the conversation: stop, drop that, switch topic, start over.",
    "Smalltalk": "Greetings, chit-chat, or talk about the assistant itself; no task.",
}

# How exact the answer has to be, and the temperature each means.
STYLE_CRITERIA = {
    "exact": "Facts, numbers, code, commands, instructions, steps, schedules, or a "
             "recommendation between options — one right answer.",
    "balanced": "Everyday help and conversation: summaries, explanations, advice.",
    "creative": "Stories, poems, jokes, names, brainstorming, banter — variety is wanted.",
}
STYLE_TEMPERATURE = {"exact": 0.2, "balanced": 0.5, "creative": 1.0}


def api_key():
    """The configured key, or "". Read fresh from .env each time (as providers
    are) so saving one in Settings takes effect on the next message; falls back
    to the process environment for installs that set it there."""
    try:
        env = mcp_client.read_env_values(mcp_client.ENV_PATH)
    except Exception:
        env = {}
    return (env.get(ENV_KEY) or os.environ.get(ENV_KEY) or "").strip()


def enabled():
    return bool(api_key())


def ask(state, questions, key=None, timeout=TIMEOUT_SECONDS):
    """Send one state and its questions. Returns the `answers` dict, or None on
    any failure (logged, never raised)."""
    key = key or api_key()
    if not key:
        return None
    try:
        r = httpx.post(JEV_URL, timeout=timeout,
                       headers={"Authorization": f"Bearer {key}"},
                       json={"model": JEV_MODEL, "state": state, "questions": questions})
        if r.status_code != 200:
            logger.warning("Jev returned %s: %s", r.status_code, r.text[:300])
            return None
        return r.json().get("answers") or None
    except Exception as e:
        logger.warning("Jev unavailable (%s); routing falls back to the classifier", e)
        return None


def check_key(key):
    """(ok, message) for Settings: one tiny call with the key being saved."""
    try:
        r = httpx.post(JEV_URL, timeout=10, headers={"Authorization": f"Bearer {key}"},
                       json={"model": JEV_MODEL, "state": "Hello there!",
                             "questions": {"greeting": {"type": "noul",
                                                        "instructions": "Is this a greeting?"}}})
    except Exception as e:
        return False, f"Couldn't reach Jev: {e}"
    if r.status_code in (401, 403):
        return False, "Jev rejected that key."
    if r.status_code != 200:
        return False, f"Jev returned {r.status_code}: {r.text[:200]}"
    return True, "Key works."


# ── what the router is shown ─────────────────────────────────

def one_line(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def candidates(messages, end):
    """The earlier messages Jev may pick from: (index, role, text, tools_used)
    for every user message and every assistant reply with text before `end`,
    newest MAX_CANDIDATES of them, oldest first. `tools_used` lists the tools
    called between the user message and the reply — what "raw" would bring."""
    out = []
    tools_used = []
    for i in range(1, end):
        m = messages[i]
        role = m.get("role")
        if role == "user" and m.get("auto"):
            continue  # a follow-through nudge, not something the user said
        if role == "user" and isinstance(m.get("content"), str):
            tools_used = []
            out.append((i, "user", m["content"], ()))
        elif role == "assistant":
            for call in m.get("tool_calls") or ():
                name = ((call or {}).get("function") or {}).get("name")
                if name:
                    tools_used.append(name)
            if isinstance(m.get("content"), str) and m["content"].strip():
                out.append((i, "assistant", m["content"], tuple(tools_used)))
                tools_used = []
    return out[-MAX_CANDIDATES:]


def _qid(text):
    """Question ids must be plain identifiers; tool and server names may not be."""
    return re.sub(r"[^A-Za-z0-9_]", "_", text)[:60]


def _units(tools):
    """What Jev is asked about for tools: one unit per tool, except that a
    server with more than MAX_TOOLS_PER_SERVER is one unit for all of them.
    [(qid, label for the state, [tool names])], in the order given."""
    per_group = {}
    for name, group, _desc in tools:
        per_group.setdefault(group, []).append(name)
    units, seen = [], set()
    for name, group, desc in tools:
        if len(per_group[group]) > MAX_TOOLS_PER_SERVER:
            if group not in seen:
                seen.add(group)
                units.append(("g_" + _qid(group), f"{group} (all {len(per_group[group])} tools)",
                              per_group[group]))
        else:
            units.append(("t_" + _qid(name), f"{name}: {one_line(desc, TOOL_DESC_CLIP)}", [name]))
    return units


def _state(user_input, cands, units, sections, servers=()):
    parts = []
    if servers:
        parts.append("MCP SERVERS:")
        parts += [f"[{_qid(gid)}] {about}" for gid, about in servers]
        parts.append("")
    if sections:
        parts.append("SAVED MEMORY SECTIONS:")
        parts += [f"[s{n}] {name}: {one_line(body, SECTION_PREVIEW)}"
                  for n, (name, body) in enumerate(sections)]
        parts.append("")
    parts.append("TOOLS:")
    parts += [f"[{qid}] {label}" for qid, label, _names in units]
    parts.append("")
    if cands:
        parts.append("EARLIER MESSAGES, oldest first:")
        for i, role, text, used in cands:
            line = f"[m{i}] {role}: {one_line(text, STATE_CLIP)}"
            if used:
                line += f"  (used tools: {', '.join(dict.fromkeys(used))})"
            parts.append(line)
        parts.append("")
    parts.append("CURRENT REQUEST:")
    parts.append(user_input)
    return "\n".join(parts)


def _questions(cands, units, sections, servers=()):
    qs = {
        "tag": {"type": "choice",
                "instructions": "What kind of request is the CURRENT REQUEST?",
                "criteria": dict(TAG_CRITERIA)},
        "style": {"type": "choice",
                  "instructions": "How exact does the answer to the CURRENT REQUEST have to be?",
                  "criteria": dict(STYLE_CRITERIA)},
    }
    if servers:
        criteria = {_qid(gid): about for gid, about in servers}
        criteria[PATH_NONE] = ("None of the MCP servers: built-in tools only (files, memory, "
                               "web search, links) or no tools at all.")
        qs["path"] = {"type": "choice",
                      "instructions": "Which MCP server is the way to handle the CURRENT REQUEST?",
                      "criteria": criteria}
    for qid, _label, _names in units:
        qs[qid] = {"type": "noul",
                   "instructions": f"Could fully handling the CURRENT REQUEST need tool [{qid}]?"}
    for i, role, _text, used in cands:
        qs[f"msg_{i}"] = {
            "type": "noul",
            "instructions": f"Is earlier message [m{i}] needed to understand or answer "
                            f"the CURRENT REQUEST?"}
        if role == "assistant" and used:
            qs[f"raw_{i}"] = {
                "type": "noul",
                "instructions": f"Does the CURRENT REQUEST need the raw tool results behind "
                                f"[m{i}] (page contents, file text, data), not just what [m{i}] said?"}
    for n, (name, _body) in enumerate(sections):
        qs[f"sec_{n}"] = {
            "type": "noul",
            "instructions": f"Does the CURRENT REQUEST need the saved memory section [s{n}] ({name})?"}
    return qs


def _p(answers, qid):
    a = answers.get(qid) or {}
    try:
        return float(a.get("noul"))
    except (TypeError, ValueError):
        return None


class Route:
    """What one turn is sent. `tools` is the picked tool names; `messages` is
    [(index, "text"|"raw")] oldest first; `sections` memory section names."""

    def __init__(self, tag, style, tools, messages, sections, ms, questions, servers=None):
        self.tag, self.style = tag, style
        # {server group: share of the path choice} for the servers that went.
        self.servers = servers or {}
        self.tools, self.messages, self.sections = tools, messages, sections
        self.ms, self.questions = ms, questions

    @property
    def temperature(self):
        return STYLE_TEMPERATURE.get(self.style, 0.4)

    def summary(self):
        picked = [str(i) + ("+raw" if k == "raw" else "") for i, k in self.messages]
        return (f"tag={self.tag} style={self.style} servers={self.servers} tools={sorted(self.tools)} "
                f"messages={picked} sections={self.sections} "
                f"({len(self.questions)} questions, {self.ms}ms)")


def route(user_input, messages, end, tools, sections, servers=()):
    """Ask Jev what this turn needs.

    `messages[:end]` is the conversation before the current request, system
    message first. `tools` is [(name, group, description)] for every tool Jev
    may choose; `sections` is [(name, body)] of the memory sections that
    aren't always sent; `servers` is [(group, what it's for)] of the MCP
    servers, whose tools are only sent if the server wins the path choice.
    Returns a Route, or None when there's no key or Jev didn't answer in time
    — the caller's cue to route the old way."""
    key = api_key()
    if not key:
        return None
    cands = candidates(messages, end)
    sections = sections[:MAX_SECTIONS]
    units = _units(tools)
    servers = list(servers)
    questions = _questions(cands, units, sections, servers)
    started = time.monotonic()
    answers = ask(_state(user_input, cands, units, sections, servers), questions, key=key)
    ms = int((time.monotonic() - started) * 1000)
    if not answers:
        return None
    tag = (answers.get("tag") or {}).get("choice")
    style = (answers.get("style") or {}).get("choice")
    if tag not in TAG_CRITERIA:
        return None
    if style not in STYLE_CRITERIA:
        style = "balanced"

    # The servers that go. Unanswered, every one does: leaning wide.
    chosen = {gid: None for gid, _about in servers}
    path = answers.get("path") or {}
    shares = path.get("probabilities") or {}
    if servers and shares:
        chosen = {gid: round(float(shares.get(_qid(gid)) or 0), 2) for gid, _about in servers
                  if _qid(gid) == path.get("choice")
                  or float(shares.get(_qid(gid)) or 0) >= PATH_SHARE}
    server_groups = {gid for gid, _about in servers}

    picked_tools = set()
    for qid, _label, names in units:
        group = next((g for n, g, _d in tools if n == names[0]), None)
        if group in server_groups and group not in chosen:
            continue
        p = _p(answers, qid)
        # An unanswered question counts as "yes": leaning wide.
        if p is None or p >= TOOL_THRESHOLD:
            picked_tools.update(names)

    picked = []
    for i, role, _text, used in cands:
        p = _p(answers, f"msg_{i}")
        raw = role == "assistant" and used and (_p(answers, f"raw_{i}") or 0) >= RAW_THRESHOLD
        if raw or (p is not None and p >= MESSAGE_THRESHOLD):
            picked.append((i, "raw" if raw else "text"))

    chosen_sections = [name for n, (name, _b) in enumerate(sections)
                       if (_p(answers, f"sec_{n}") or 0) >= SECTION_THRESHOLD]
    return Route(tag, style, picked_tools, picked, chosen_sections, ms, questions, chosen)


# ── follow-through: did the reply actually finish the job? ───

# How sure Jev must be that a reply stopped with work it could still do before
# the agent is nudged to carry on. High on purpose: this is a nudge toward
# being proactive, never a push into something the agent can't do — any
# real chance that it's waiting on the user or has hit a wall, and nothing
# happens.
PARTWAY_THRESHOLD = 0.7
PROMISE_THRESHOLD = 0.85
BLOCKED_CEILING = 0.2
REPLY_CLIP = 1500
ACTIONS_SHOWN = 25

FOLLOW_CRITERIA = {
    "done": "The reply finishes what was asked: it answers, or reports the work as done.",
    "partway": "Work the assistant could still do itself is left undone: steps or items "
               "skipped, only part of the questions answered, or a next step announced "
               "and not taken.",
    "needs_user": "It is waiting on the user: a question, a choice, a sign-in, an approval, "
                  "or information only they have.",
    "cant": "It can't be done with the tools it has, or it hit a wall (blocked site, error, "
            "missing access) and said so.",
}


def follow_through(request, actions, reply):
    """Did `reply` finish `request`? Returns {"status", "shares", "promise",
    "nudge", "ms"} — `nudge` True only when Jev is confident the reply
    stopped partway on work the agent can do, and sees no sign it is waiting
    on the user or blocked — or None when Jev isn't available."""
    if not enabled():
        return None
    state = ("REQUEST:\n" + one_line(request, 1200)
             + "\n\nACTIONS TAKEN THIS TURN:\n" + ("\n".join(actions[-ACTIONS_SHOWN:]) or "(none)")
             + "\n\nREPLY:\n" + str(reply or "")[-REPLY_CLIP:])
    questions = {
        "status": {"type": "choice",
                   "instructions": "Where does the REPLY leave the REQUEST?",
                   "criteria": dict(FOLLOW_CRITERIA)},
        "promise": {"type": "noul",
                    "instructions": "Does the REPLY say it will do something next (\"I'll check…\", "
                                    "\"Next, I'll…\", \"Let me…\") that it has not done?"},
    }
    started = time.monotonic()
    answers = ask(state, questions)
    ms = int((time.monotonic() - started) * 1000)
    if not answers:
        return None
    status = (answers.get("status") or {}).get("choice")
    shares = {k: float(v) for k, v in
              ((answers.get("status") or {}).get("probabilities") or {}).items()}
    promise = _p(answers, "promise") or 0.0
    blocked = shares.get("needs_user", 0.0) + shares.get("cant", 0.0)
    nudge = ((status == "partway" and shares.get("partway", 0.0) >= PARTWAY_THRESHOLD)
             or (promise >= PROMISE_THRESHOLD and blocked < BLOCKED_CEILING)) \
        and blocked < BLOCKED_CEILING
    return {"status": status, "shares": {k: round(v, 2) for k, v in shares.items()},
            "promise": round(promise, 2), "nudge": bool(nudge), "ms": ms}


# ── browser steps: the fast loop's next move (src/fast_browser.py) ──

# Controls the fast loop never touches, whatever Jev picks: money, messages,
# deletion. A goal that needs one hands the page back to the main model, which
# answers to the user and the approval rules.
RISKY_CONTROL = re.compile(
    r"\b(place (your )?order|buy now|pay( now)?|checkout|check ?out|purchase|"
    r"complete (your )?(order|purchase)|submit (order|payment)|confirm (order|purchase|payment)|"
    r"delete|remove account|close account|send|unsubscribe|subscribe|transfer|donate|"
    r"book now|reserve now|sign out|log ?out)\b", re.I)

STEP_DONE_THRESHOLD = 0.7
STEP_MIN_CONFIDENCE = 0.35
STEP_DONE_PICK = 0.6


# Where on a 1280x800 page a control is, in words. A shop's "Price" facet in
# the left column and its "Sort by" at the top right read alike as labels;
# the position is what tells a filter list from the sort control.
def _position(e):
    x, y = e.get("x") or 0, e.get("y") or 0
    col = "left" if x < 320 else "right" if x > 960 else "middle"
    row = "top" if y < 200 else "bottom" if y > 600 else "middle"
    return "below the screen" if e.get("below") else f"{row} {col}"


def _control_line(n, e):
    line = f'[e{n}] {e.get("kind")} "{e.get("text") or ""}" [{_position(e)}]'
    if e.get("area"):
        line += f' (in: {e["area"]})'
    if e.get("value"):
        line += f' (now: "{e["value"]}")'
    if e.get("checked"):
        line += " (checked)"
    if e.get("disabled"):
        line += " (disabled)"
    return line


def browser_step(goal, page, done_so_far):
    """The fast loop's next move on `page` (fast_step's JSON) toward `goal`.

    Returns {"move": "click"|"type"|"scroll"|"back"|"done"|"stop"|"unsure",
    "element": the control for click/type, "p": Jev's share for the pick,
    "done": P(goal already met), "ms"} — or None when Jev didn't answer.
    Only on-screen, enabled, non-risky controls are offered; "unsure" is a
    pick Jev itself gave less than STEP_MIN_CONFIDENCE."""
    elements = page.get("elements") or []
    options = {}
    for n, e in enumerate(elements, 1):
        # Below-the-screen controls are offered too — the browser scrolls to
        # the one picked. Offering only what's on screen made a "4 stars &
        # up" filter further down the column invisible, and Jev settled for
        # "sort by reviews", the nearest thing it could see.
        if e.get("disabled") or RISKY_CONTROL.search(e.get("text") or ""):
            continue
        if e.get("options"):
            # A dropdown is a choice of its options, each its own move.
            for k, opt in enumerate(e["options"]):
                if opt != e.get("value"):
                    options[f"e{n}o{k}"] = (f'choose "{opt}" in dropdown "{e.get("text") or ""}"'
                                            + (f' (in: {e["area"]})' if e.get("area") else ""))
            continue
        options[f"e{n}"] = _control_line(n, e)[len(f"[e{n}] "):]
    options["scroll"] = "What the GOAL needs isn't among any of these controls: scroll down."
    options["back"] = "This page is a wrong turn for the GOAL: go back."
    options["done"] = "The GOAL is already achieved on this page."
    options["stop"] = ("Can't go on without the user (sign-in, CAPTCHA, payment, a choice only "
                       "they can make), the next step is a purchase, message or deletion, or the "
                       "page is blocked or broken.")
    state = "\n".join([
        "GOAL: " + one_line(goal, 400),
        f"PAGE: {one_line(page.get('title'), 120)} | {one_line(page.get('url'), 160)}",
        "HEADINGS: " + "; ".join(page.get("headings") or []),
        "VISIBLE TEXT: " + one_line(page.get("text"), 700),
        "DONE SO FAR: " + ("; ".join(done_so_far[-8:]) or "nothing yet"),
        "CONTROLS:",
        *(_control_line(n, e) + (" (not allowed)" if RISKY_CONTROL.search(e.get("text") or "") else "")
          for n, e in enumerate(elements, 1)),
    ])
    questions = {
        "next": {"type": "choice",
                 "instructions": "Which ONE move gets closest to the GOAL from this page? Match "
                                 "what the GOAL asks to do — search, sort, filter, open, go to a "
                                 "page — to a control that does that, not just one that shares a "
                                 "word with it: sorting is the sort control, a filter only narrows "
                                 "the results. A text field or search box means typing into it.",
                 "criteria": options},
        "done": {"type": "noul",
                 "instructions": "Is the GOAL already achieved on this page?"},
    }
    started = time.monotonic()
    answers = ask(state, questions)
    ms = int((time.monotonic() - started) * 1000)
    if not answers:
        return None
    nxt = answers.get("next") or {}
    pick = nxt.get("choice")
    p = float((nxt.get("probabilities") or {}).get(pick) or 0)
    done = _p(answers, "done") or 0.0
    out = {"p": round(p, 2), "done": round(done, 2), "ms": ms}
    # "Done" has to be meant: a hesitant one (Walmart, a price *filter*
    # clicked for "sort by price", called done at 0.46) hands back instead.
    if done >= STEP_DONE_THRESHOLD or (pick == "done" and p >= STEP_DONE_PICK):
        return {**out, "move": "done"}
    if pick == "done":
        return {**out, "move": "unsure"}
    if pick in ("stop", "scroll", "back"):
        return {**out, "move": pick}
    if not pick or p < STEP_MIN_CONFIDENCE or pick not in options:
        return {**out, "move": "unsure"}
    if "o" in pick[1:]:
        n, k = pick[1:].split("o")
        element = elements[int(n) - 1]
        return {**out, "move": "select", "element": element, "option": element["options"][int(k)]}
    element = elements[int(pick[1:]) - 1]
    typing = any(k in (element.get("kind") or "") for k in ("field", "box", "combobox", "textbox"))
    return {**out, "move": "type" if typing else "click", "element": element}
