"""browser_do: the fast loop. A goal in, Jev clicking toward it.

The main model drives the browser by screenshot, one 2-4 second request per
click. For the routine part of a web task — search, filter, sort, open a
menu, page through results — that is a large model deciding which of thirty
buttons to press. Here the main model hands over a short goal instead, and
each step is:

  1. the page as text: its controls on screen, numbered, with labels and
     where to click (browser_server fast_step — no screenshot);
  2. Jev picks the next control, or scroll / back / done / stop (~0.2s);
  3. the browser clicks it — or, for a text field, types what a small model
     says to type there.

until Jev says the goal is met, the loop is unsure, stuck or out of steps.
The main model then gets one screenshot and a list of what was done, and
carries on from there.

What it never does: press anything that spends money, sends a message or
deletes (jev.RISKY_CONTROL) — those controls are never offered — and keep
going when Jev isn't confident: an unsure step hands back rather than guesses.
"""
import json
import time
from urllib.parse import urlsplit

from src.logging_setup import get_logger
import src.jev as jev

logger = get_logger(__name__)

DEFAULT_STEPS = 8
_APPLYING_KINDS = {"link", "radio", "checkbox", "switch", "option", "menuitem",
                   "menuitemradio", "menuitemcheckbox", "tab"}
MAX_STEPS = 15
TIME_BUDGET_SECONDS = 120

# Why the loop stopped, as the main model reads it.
_ENDINGS = {
    "done": "Goal reached, by the fast loop's judgment — check the screenshot.",
    "stop": "Stopped: the page needs the user (sign-in, CAPTCHA, payment), the next step is a "
            "purchase, message or deletion, or the page is blocked.",
    "unsure": "Handed back: the fast loop wasn't confident what to do next. Carry on yourself.",
    "repeat": "Handed back: the same move wasn't changing anything.",
    "steps": "Handed back: out of steps.",
    "time": "Handed back: out of time.",
    "stopped": "Stopped by the user.",
    "check": "Stopped: the site is showing a human check (CAPTCHA). Hand it to the user with "
             "request_captcha_help.",
    "error": "Handed back after a browser error.",
    "nojev": "The fast loop needs Jev and couldn't reach it. Use the browser tools directly.",
}


def _describe(element):
    return f'{element.get("kind")} "{(element.get("text") or "")[:50]}"'


def run(goal, step, type_text, cancelled, max_steps=DEFAULT_STEPS):
    """Drive toward `goal`. `step(args)` runs fast_step and returns its parsed
    JSON (raising on a browser error); `type_text(goal, element, page)`
    returns what to type into a field, or None; `cancelled()` is the Stop
    button. Returns (summary text, log of steps)."""
    try:
        max_steps = max(1, min(int(max_steps or DEFAULT_STEPS), MAX_STEPS))
    except (TypeError, ValueError):
        max_steps = DEFAULT_STEPS
    started = time.monotonic()
    done_so_far, log = [], []
    ending = "steps"
    last_sig = None
    # Each move as (page path, move, control, option), in order: going back
    # to a move made two steps ago — sort by price, sort by reviews, sort by
    # price — is the loop undoing itself, and it hands back rather than
    # flip-flopping until the step budget runs out.
    moves = []
    try:
        page = step({"action": "observe"})
    except Exception as e:                                   # noqa: BLE001
        return f"{_ENDINGS['error']} ({e})", log
    for _n in range(max_steps):
        if cancelled():
            ending = "stopped"
            break
        if time.monotonic() - started > TIME_BUDGET_SECONDS:
            ending = "time"
            break
        if page.get("blocker"):
            ending = "check"
            break
        decision = jev.browser_step(goal, page, done_so_far)
        if decision is None:
            ending = "nojev" if not log else "unsure"
            break
        log.append({"url": page.get("url"), **{k: v for k, v in decision.items() if k not in ("element",)},
                    **({"control": _describe(decision["element"])} if decision.get("element") else {})})
        move = decision["move"]
        if move in ("done", "stop", "unsure"):
            ending = move
            break
        element = decision.get("element") or {}
        # A repeat is the same move on the same page *as it looked*: scrolling
        # twice to reach a button at the bottom is progress, because what's on
        # screen changed in between.
        seen = tuple((e.get("text"), e.get("y")) for e in page.get("elements") or ())
        sig = (page.get("url"), move, element.get("text"), element.get("x"), element.get("y"), seen)
        if sig == last_sig:
            ending = "repeat"
            break
        last_sig = sig
        made = (urlsplit(page.get("url") or "").path, move, element.get("text"), decision.get("option"))
        if move in ("click", "select", "type") and len(moves) >= 2 and made == moves[-2] != moves[-1]:
            ending = "repeat"
            break
        moves.append(made)
        args = {"action": move}
        if move in ("click", "type", "select"):
            args.update({"x": element.get("x"), "y": element.get("y"), "ref": element.get("ref") or 0,
                         "label": (element.get("text") or "")[:40]})
        # Controls that usually apply something — a link, a sort option, a
        # filter checkbox or switch — get the longer wait for the page to
        # start changing; a plain button often only opens a menu.
        if move == "click" and (element.get("kind") or "") in _APPLYING_KINDS:
            args["link"] = True
        if move == "select":
            args["text"] = decision.get("option") or ""
        if move == "type":
            text = type_text(goal, element, page)
            if text is None:
                args["action"] = move = "click"
            else:
                kind = (element.get("kind") or "") + " " + (element.get("text") or "")
                args.update({"text": text, "enter": "search" in kind.lower()})
        try:
            page = step(args)
        except Exception as e:                               # noqa: BLE001
            logger.warning("fast_step failed: %s", e)
            ending = "error"
            break
        if page.get("stale"):
            # Nothing was done: the control had gone. Decide again on the
            # page as it is now (this still counts against the step budget).
            last_sig = None
            continue
        done_so_far.append(
            {"click": f"clicked {_describe(element)}",
             "type": f"typed \"{args.get('text', '')}\" into {_describe(element)}"
                     + (" and pressed Enter" if args.get("enter") else ""),
             "select": f"chose \"{args.get('text', '')}\" in {_describe(element)}",
             "scroll": "scrolled down", "back": "went back"}[move])
    lines = [_ENDINGS[ending]]
    if done_so_far:
        lines.append("Did: " + "; ".join(done_so_far) + ".")
    lines.append(f"Now on: {page.get('title') or '(untitled)'} | {page.get('url') or ''}")
    logger.info("browser_do %r: %s after %d steps in %.1fs", goal[:80], ending, len(done_so_far),
                time.monotonic() - started)
    return "\n".join(lines), log
