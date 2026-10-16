"""An agent's name and its person's name, kept apart.

Each FreeClaw user (profile) is an agent, and its name is the agent's own
("Jarvis"). The person it works for is someone else, with a name of their own.
Mixing the two up is how an agent ended up called "Elliot" for Elliot, who then
couldn't tell it from himself, and greeted as "Good morning, Jarvis".

So:
* the person's name lives in the agent's context.md, under About-user, as
  "Their name is Sam. You, their agent, are called Jarvis." It's what the agent
  itself reads, and if the person asks to be called something else and the
  agent updates that line, everything that shows the name follows;
* person_in() finds it there, however the agent's own memory has worded it;
* clash() refuses an agent name that is the person's own (or their first
  name), or the name of anyone else an agent here works for.
"""
import re

# The line names_line() writes, then the looser ways an agent's own memory puts
# it, looked for only under "## About-user".
_PATTERNS = [
    re.compile(r"\bTheir name is ([^.\n]{1,60})\.\s*You, their agent, are called"),
    re.compile(r"(?im)^\s*[-*]?\s*(?:their|the user'?s|user'?s|my owner'?s)\s+name\s+is\s+([^.\n,;]{1,60})"),
    re.compile(r"(?im)^\s*[-*]?\s*name\s*:\s*([^.\n,;(]{1,60})"),
    # "Jarvis's human is named Elliot", "The user is called Dana", "assistant to Priya"
    re.compile(r"(?i)\b(?:human|user|owner|person)\s+is\s+(?:named|called)\s+([A-Z][^.\n,;()]{0,59})"),
    re.compile(r"(?i)\b(?:their|the user'?s|user'?s|my owner'?s|my human'?s)\s+name\s+is\s+([A-Z][^.\n,;()]{0,59})"),
    re.compile(r"(?i)\b(?:works for|working for|assistant to|agent for)\s+([A-Z][a-z]+(?: [A-Z][a-z]+)?)\b"),
]
_NAME_END = re.compile(r"\s+(?:and|who|from|lives|living|in|at|is|was|but|of|with)\b|\s*\(|\s+[\-–—]")
_NOT_A_NAME = {"unknown", "not given", "not known", "none", "n/a", "na", "tbd", "unspecified", "?", "-"}

SAME_NAME = ("{name} is your name. Give your agent a name of its own, like Jarvis or Friday, "
             "so you can tell the two of you apart.")
SAME_AS_OTHER = ("{name} is the name of someone using FreeClaw here. Give the agent a name of "
                 "its own, like Jarvis or Friday, so it isn't mistaken for them.")


def _about(context):
    """The "## About-user" section of a context.md."""
    m = re.search(r"(?ms)^## About-user[^\n]*\n(.*?)(?=^## |\Z)", context or "")
    return m.group(1) if m else ""


def _clean(raw):
    """The name at the start of `raw` ("Elliot and lives in…" is Elliot), or ""."""
    name = _NAME_END.split(raw.strip(), 1)[0].strip().strip("*_`\"'").strip()
    if not name or name.casefold() in _NOT_A_NAME or name.casefold().startswith(("not ", "unknown")):
        return ""
    return " ".join(name.split()[:4])


def person_in(context):
    """The person's name an agent's context.md gives, or ""."""
    about = _about(context)
    for i, pattern in enumerate(_PATTERNS):
        m = pattern.search(context or "" if i == 0 else about)
        name = _clean(m.group(1)) if m else ""
        if name:
            return name
    return ""


def names_line(agent, person):
    """What an agent's context.md says about the two names."""
    if person:
        return f"Their name is {person}. You, their agent, are called {agent}."
    return f"You, their agent, are called {agent}."


def with_names(context, agent, person):
    """`context` with the names line at the top of About-user."""
    line = names_line(agent, person)
    if "## About-user\n" in context:
        return context.replace("## About-user\n", "## About-user\n" + line + "\n", 1)
    return "## About-user\n" + line + "\n" + context


def renamed(context, old, new):
    """`context` telling the agent it's now called `new`."""
    said = f"You, their agent, are called {old}."
    if said in context:
        return context.replace(said, f"You, their agent, are called {new}.")
    return with_names(context, new, "")


def clash(agent, person, people):
    """Why `agent` can't be an agent's name, or "": it's the person's own name
    or first name (`person`), or the name of anyone else an agent here works
    for (`people`)."""
    a = (agent or "").strip().casefold()
    if not a:
        return ""
    if person and a in (person.strip().casefold(), person.strip().split(" ")[0].casefold()):
        return SAME_NAME.format(name=agent.strip())
    if any(a in ((p or "").strip().casefold(), (p or "").strip().split(" ")[0].casefold())
           for p in people if p):
        return SAME_AS_OTHER.format(name=agent.strip())
    return ""
