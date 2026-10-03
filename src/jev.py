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
