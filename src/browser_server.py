"""FreeClaw's own browser, as an MCP server the agent drives by screenshot.

Run as `python -m src.browser_server` (src/mcp_client.py: BUILTIN_SERVERS). It
owns one Chromium per FreeClaw user and gives the model a computer-use style
toolset: every action replies with a screenshot of the page, and the model
points at what it wants by its x,y in that picture. Seeing the page as a
person does is what lets it cope with canvases, custom widgets and layouts an
element list describes badly — and what lets a person watch it work.

**Watching.** The Browser app (Flask/templates/browser.html) shows this
browser live, cursor and all. Nothing new listens for that: the frames ride
back to FreeClaw on the stdio pipe it already reads, as MCP notifications
(`notifications/freeclaw/*`), and FreeClaw relays them to the page behind its
own login (src/browser_live.py). The pipe is private to the two processes, so
there is no debugging port for anything else on the machine to drive. They
cost nothing unwatched: frames are only sent, and the cursor only glides
rather than jumps, while FreeClaw holds a watch lease on this process.

**Why not the `mcp` package.** The protocol this needs is four methods and a
notification each way, and the SDK's server hides the one thing that matters
here — custom notifications in both directions while a tool call is running.
So this speaks newline-delimited JSON-RPC itself, and has no dependency beyond
playwright.

Nothing here may write to stdout except protocol messages: that's the
JSON-RPC channel. Diagnostics go to stderr, which FreeClaw drains separately.
Playwright is only imported once a browser is actually needed, because Flask
imports this module for the constants below (src/browser_takeover.py).
"""

import asyncio
import base64
import json
import os
import re
import sys
import threading
import time

# The viewport the agent browses at, and the size of every screenshot it is
# shown — so an x,y the model reads off a screenshot is exactly the point it
# clicks. Shared with src/browser_takeover.py so the human's sign-in happens
# at the same size: a few sites serve a different DOM to a different viewport,
# and a login captured against the mobile layout can land the agent somewhere
# it can't navigate.
VIEWPORT = {"width": 1280, "height": 800}

LOCALE = "en-US"

# Playwright launches Chromium with --enable-automation, which is what sets
# navigator.webdriver = true — the first thing a sign-in page checks, and
# the reason Google answers "This browser or app may not be secure". Dropping
# the flag and the matching Blink feature makes it read false again. Shared by
# both browsers: the site should see the same one sign in and come back.
IGNORE_DEFAULT_ARGS = ["--enable-automation"]
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


# Run in every page of both browsers (context.add_init_script): pause looping
# animations while their element is off-screen, and play them again when it
# scrolls into view.
#
# A server has no GPU, so Chromium paints in software, and a page's looping
# animations cost CPU whether or not anybody can see them. walmart.com's home
# page runs 81 copies of an infinite loading shimmer, all off-screen; with them
# running, Chromium alone held one core at 63-82% while the page sat idle, and
# a click took seconds to show. Paused, 20-41%. Nothing visible changes: only
# infinite animations are touched, only ones this script paused are resumed,
# and an animation is only paused while nothing of its element is on screen.
OFFSCREEN_ANIMATION_PAUSE_JS = r"""
(() => {
  if (window.__fcAnimationPause || typeof document.getAnimations !== "function") return;
  window.__fcAnimationPause = true;
  const paused = new Set();
  const onScreen = (target) => {
    const el = target && (target.element || target);   // a pseudo-element's host
    if (!el || !el.isConnected || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0
        && r.top < innerHeight && r.left < innerWidth;
  };
  const sweep = () => {
    for (const a of document.getAnimations()) {
      const timing = a.effect && a.effect.getTiming ? a.effect.getTiming() : null;
      if (!timing || timing.iterations !== Infinity) continue;
      const visible = onScreen(a.effect.target);
      if (a.playState === "running" && !visible) { a.pause(); paused.add(a); }
      else if (paused.has(a) && visible) { a.play(); paused.delete(a); }
    }
    for (const a of paused) if (a.playState !== "paused") paused.delete(a);
  };
  let queued = false;
  const soon = () => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; sweep(); }); } };
  addEventListener("scroll", soon, { passive: true, capture: true });
  addEventListener("resize", soon, { passive: true });
  setInterval(sweep, 1000);
})();
"""


def launch_kwargs(headless, channel=None):
    """The `chromium.launch()` arguments both browsers share."""
    kwargs = {"headless": headless, "ignore_default_args": list(IGNORE_DEFAULT_ARGS),
              "args": list(LAUNCH_ARGS)}
    if channel:
        kwargs["channel"] = channel
    return kwargs


def chrome_user_agent(browser):
    """A plain-Chrome UA string matching `browser`'s actual version.

    Playwright's headless Chromium advertises itself as `HeadlessChrome`, which
    is exactly the token sign-in pages look at when they decide to refuse. The
    human signs in through a *headful* browser (src/browser_takeover.py) and
    the agent replays those cookies through a headless one, so if the two
    disagree about who they are, the site sees a session that changed browser
    mid-flight and re-challenges.

    Derived from `browser.version` rather than hardcoded so it ages with
    whatever Chromium playwright installed."""
    version = ""
    try:
        version = (browser.version or "").strip()
    except Exception:                             # noqa: BLE001 — cosmetic
        pass
    major = version.split(".")[0] if version else ""
    if not major.isdigit():
        # No version to work from: let playwright's default stand rather than
        # invent a number.
        return None
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def context_kwargs(browser, storage_state=None):
    """The `new_context()` arguments both browsers share.

    One definition used by the agent's context here and the human's context in
    browser_takeover, because the whole handoff rests on the two looking like
    the same browser to the site."""
    kwargs = {"locale": LOCALE, "viewport": dict(VIEWPORT)}
    user_agent = chrome_user_agent(browser)
    if user_agent:
        kwargs["user_agent"] = user_agent
    if storage_state:
        kwargs["storage_state"] = storage_state
    return kwargs


# ── protocol ─────────────────────────────────────────────────

SERVER_INFO = {"name": "freeclaw-browser", "version": "1.0"}
# Newest first; an initialize asking for one of these gets it back.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

# Everything FreeClaw-specific on the pipe is under this prefix, both ways. A
# client that isn't FreeClaw ignores notifications it doesn't know, as the
# spec has it, and never sends `watch`, so to it this is a plain MCP server.
LIVE_PREFIX = "notifications/freeclaw/"

# What the model is shown. JPEG because a screenshot rides along on every
# request until the history prunes it (agent.py: _MAX_HISTORY_IMAGES), and a
# text-heavy 1280x800 page is ~100KB this way against ~600KB as PNG.
SHOT_QUALITY = 70

# The live view. A watcher on a phone connection is the constraint, so these
# are cheaper than the model's screenshots and capped well under the
# compositor's 60fps.
LIVE_QUALITY = 55
LIVE_MIN_FRAME_GAP = 1 / 12
# How long the watched cursor takes to reach a target before the click lands,
# so the person sees where it's going rather than a click appearing from
# nowhere. Matched by the viewer's own animation.
CURSOR_GLIDE = 0.4
# FreeClaw renews the lease every few seconds while a viewer is open; this is
# the most it can ask for, so a FreeClaw that went away stops the frames soon.
MAX_WATCH_LEASE = 30.0

# read_text hands back this much at a time, then says where to carry on.
TEXT_CHUNK = 8000

# Typing paced for a watcher still has to finish: a long text types in chunks
# rather than taking minutes.
TYPE_DELAY_MS = 35
TYPE_BUDGET_MS = 2500

_SCHEMES = ("http://", "https://")


def _log(message):
    """Stderr, never stdout — see the module docstring."""
    print(f"[browser-server] {message}", file=sys.stderr, flush=True)


def _number(value, name):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a number.")


class ToolError(Exception):
    """A mistake the model can fix — reported to it, not logged as a crash."""


# Kept short on purpose: every description is resent with every request.
_XY = {"x": {"type": "number"}, "y": {"type": "number"}}

TOOLS = [
    {"name": "navigate",
     "description": (
         "Open a URL in your web browser: the user's real Chromium, with the logins they saved "
         "in the Browser app. Use it whenever they say \"browser\" or a task needs a website, "
         "not another service's cloud browser. Every browser tool shows you the page as a "
         "1280x800 screenshot; x,y are pixels in it. \"Access Denied\" means the site blocks "
         "automation: say so, don't retry."),
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}},
                     "required": ["url"]}},
    {"name": "click",
     "description": "Click at x,y. If it opens a new page it worked: don't repeat it.",
     "inputSchema": {"type": "object", "properties": {
         **_XY, "double": {"type": "boolean"},
         "button": {"type": "string", "enum": ["left", "right", "middle"]}},
         "required": ["x", "y"]}},
    {"name": "type",
     "description": "Type text where focus is: click the field first. enter=true presses Enter after.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string"}, "enter": {"type": "boolean"}}, "required": ["text"]}},
    {"name": "key",
     "description": "Press a key or chord: Enter, Escape, Tab, ArrowDown, Backspace, Control+A...",
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}},
                     "required": ["key"]}},
    {"name": "scroll",
     "description": "Scroll about a screen. x,y picks the panel to scroll (default: the page).",
     "inputSchema": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": ["down", "up", "left", "right"]}, **_XY},
         "required": ["direction"]}},
    {"name": "hover",
     "description": "Move the mouse to x,y, e.g. to open a menu.",
     "inputSchema": {"type": "object", "properties": dict(_XY), "required": ["x", "y"]}},
    {"name": "drag",
     "description": "Press at x,y, drag to to_x,to_y and let go (sliders, maps).",
     "inputSchema": {"type": "object", "properties": {
         **_XY, "to_x": {"type": "number"}, "to_y": {"type": "number"}},
         "required": ["x", "y", "to_x", "to_y"]}},
    {"name": "back",
     "description": "Go back a page, or close a tab that opened and return to the last.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "screenshot",
     "description": "Look at the page again, e.g. once it has finished loading.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "read_text",
     "description": "The page's text, no screenshot: for reading articles and results. "
                    "start continues a long page.",
     "inputSchema": {"type": "object", "properties": {"start": {"type": "integer"}}}},
    {"name": "find",
     "description": "Where elements matching text are, as x,y to click. For when the "
                    "screenshot leaves you unsure.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    # Internal: FreeClaw's fast loop (browser_do) only — the builtin entry in
    # src/mcp_client.py excludes it from the model's tool list. Does one plain
    # action and answers with the page's controls as JSON, no screenshot.
    {"name": "fast_step",
     "description": "Internal to browser_do.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["observe", "click", "type", "select", "scroll", "back"]},
         **_XY, "text": {"type": "string"}, "enter": {"type": "boolean"},
         "label": {"type": "string"}, "ref": {"type": "integer"}}}},
    {"name": "request_captcha_help",
     "description": "Hand the current page to the user to solve a CAPTCHA or human check. "
                    "End your turn after.\nreason: shown to them.",
     "inputSchema": {"type": "object", "properties": {"reason": {"type": "string"}}}},
]

TOOL_NAMES = frozenset(t["name"] for t in TOOLS)


# Run in the page by `find`. Interactive elements first, then any element
# whose own text matches, so "the Sign in button" and "the paragraph that says
# 'out of stock'" are both findable. Points are clamped into the element's
# visible part, so a wide element half off-screen still gets an on-screen x,y.
_FIND_JS = r"""
(q) => {
  q = String(q || "").toLowerCase().replace(/\s+/g, " ").trim();
  if (!q) return [];
  const W = innerWidth, H = innerHeight;
  const INTERACTIVE = 'a[href],button,input,select,textarea,summary,label,[role=button],' +
    '[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=radio],[role=option],' +
    '[role=combobox],[role=textbox],[role=searchbox],[role=switch],[onclick],[contenteditable=""],' +
    '[contenteditable=true]';
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const label = (el) => {
    const bits = [el.getAttribute("aria-label"), el.getAttribute("placeholder"),
                  el.getAttribute("title"), el.getAttribute("alt")];
    if (el.tagName === "INPUT" && /^(button|submit|reset)$/i.test(el.type)) bits.push(el.value);
    if (el.labels && el.labels.length) bits.push(el.labels[0].innerText);
    bits.push(el.innerText);
    return [...new Set(bits.map(clean).filter(Boolean))].join(" ");
  };
  const kind = (el) => {
    const role = el.getAttribute("role");
    if (role) return role;
    const tag = el.tagName.toLowerCase();
    if (tag === "a") return "link";
    if (tag === "input") return (el.type || "text") === "text" ? "text field" : el.type + " field";
    if (tag === "textarea") return "text field";
    if (tag === "select") return "dropdown";
    if (/^(button|label|summary)$/.test(tag)) return tag;
    return /^h[1-6]$/.test(tag) ? "heading" : "text";
  };
  const seen = new Set(), out = [];
  const add = (el, text, interactive) => {
    if (seen.has(el) || out.length >= 40) return;
    seen.add(el);
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    const st = getComputedStyle(el);
    if (st.visibility === "hidden" || st.display === "none" || +st.opacity === 0) return;
    const onScreen = r.bottom > 0 && r.right > 0 && r.top < H && r.left < W;
    const x = Math.round((Math.max(r.left, 0) + Math.min(r.right, W)) / 2);
    const y = Math.round((Math.max(r.top, 0) + Math.min(r.bottom, H)) / 2);
    out.push({ kind: kind(el), text: text.slice(0, 70), x, y, onScreen, interactive,
               where: r.bottom <= 0 ? "above" : r.top >= H ? "below" : "" });
  };
  for (const el of document.querySelectorAll(INTERACTIVE)) {
    const text = label(el);
    if (text.toLowerCase().includes(q)) add(el, text, true);
  }
  const walker = document.createTreeWalker(document.body || document.documentElement,
                                           NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n && out.length < 40; n = walker.nextNode()) {
    if (!n.nodeValue || !n.nodeValue.toLowerCase().includes(q)) continue;
    const el = n.parentElement;
    if (!el || /^(SCRIPT|STYLE|NOSCRIPT)$/.test(el.tagName)) continue;
    const owner = el.closest(INTERACTIVE);
    if (owner) add(owner, label(owner), true);
    else add(el, clean(el.innerText || n.nodeValue), false);
  }
  out.sort((a, b) => (b.onScreen - a.onScreen) || (b.interactive - a.interactive) || (a.y - b.y));
  return out.slice(0, 12);
}
"""


# What the fast loop (FreeClaw's browser_do, src/agent.py) reads instead of a
# screenshot: the page's headings, the start of its visible text, and every
# control on screen — kind, label, where to click, a field's current value —
# numbered. Same idea of a control as _FIND_JS, without a query. On-screen
# first, then a few just below the fold so "scroll down" has something to
# point at.
_ELEMENTS_JS = r"""
() => {
  const W = innerWidth, H = innerHeight;
  const INTERACTIVE = 'a[href],button,input:not([type=hidden]),select,textarea,summary,' +
    '[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=radio],' +
    '[role=option],[role=combobox],[role=textbox],[role=searchbox],[role=switch],[onclick],' +
    '[contenteditable=""],[contenteditable=true]';
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const label = (el) => {
    const bits = [el.getAttribute("aria-label"), el.getAttribute("placeholder"),
                  el.getAttribute("title"), el.getAttribute("alt")];
    if (el.tagName === "INPUT" && /^(button|submit|reset)$/i.test(el.type)) bits.push(el.value);
    if (el.labels && el.labels.length) bits.push(el.labels[0].innerText);
    bits.push(el.innerText);
    const img = el.querySelector && el.querySelector("img[alt]");
    if (img) bits.push(img.getAttribute("alt"));
    return [...new Set(bits.map(clean).filter(Boolean))].join(" ").slice(0, 90);
  };
  const kind = (el) => {
    const tag = el.tagName.toLowerCase();
    // The tag decides for form controls whatever their role says: a <select>
    // with role=combobox is still picked from, an <input> one still typed in.
    if (tag === "select") return "dropdown";
    const role = el.getAttribute("role");
    if (role && tag !== "input" && tag !== "textarea") return role;
    if (tag === "a") return "link";
    if (tag === "input") {
      const t = (el.type || "text").toLowerCase();
      return /^(text|search|email|tel|url|number|password)$/.test(t) ? (t === "search" ? "searchbox" : "text field")
           : t === "checkbox" || t === "radio" ? t : t + " button";
    }
    if (tag === "textarea") return "text field";
    if (tag === "select") return "dropdown";
    return tag;
  };
  // Which part of the page a control sits in — "Filters", "Sort", the nav —
  // from the nearest labelled ancestor or the heading above it in its
  // section. A bare "Price" means nothing until it's "Price (in: Filters)".
  const area = (el) => {
    let node = el.parentElement;
    for (let depth = 0; node && depth < 12; depth++, node = node.parentElement) {
      const by = node.getAttribute("aria-labelledby");
      const byEl = by && document.getElementById(by.split(" ")[0]);
      const named = node.getAttribute("aria-label") || (byEl && byEl.innerText) || (node.tagName === "FIELDSET" &&
        node.querySelector("legend") && node.querySelector("legend").innerText);
      if (named) return clean(named).slice(0, 40);
      const h = node.querySelector(":scope > h2, :scope > h3, :scope > h4, :scope > header");
      if (h && !h.contains(el)) return clean(h.innerText).slice(0, 40);
      if (/^(NAV|HEADER|FOOTER|ASIDE)$/.test(node.tagName)) return node.tagName.toLowerCase();
    }
    return "";
  };
  document.querySelectorAll("[data-fc-ref]").forEach(e => e.removeAttribute("data-fc-ref"));
  const out = [], below = [], seen = new Set();
  for (const el of document.querySelectorAll(INTERACTIVE)) {
    if (seen.has(el)) continue;
    seen.add(el);
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const st = getComputedStyle(el);
    if (st.visibility === "hidden" || st.display === "none" || +st.opacity === 0) continue;
    const text = label(el);
    const k = kind(el);
    if (!text && !/field|box|dropdown|combobox/.test(k)) continue;
    const x = Math.round((Math.max(r.left, 0) + Math.min(r.right, W)) / 2);
    const y = Math.round((Math.max(r.top, 0) + Math.min(r.bottom, H)) / 2);
    const item = { kind: k, text, x, y, ref: out.length + below.length + 1 };
    // Stamped so a later step can find this very element again — by
    // position alone a dropdown a shop has drawn its own box over is the
    // box, not the <select>.
    el.setAttribute("data-fc-ref", String(item.ref));
    const where = area(el);
    if (where && where.toLowerCase() !== text.toLowerCase()) item.area = where;
    if (el.tagName === "SELECT") {
      item.options = [...el.options].map(o => clean(o.text)).filter(Boolean).slice(0, 12);
      item.value = clean(el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : "");
    }
    if (el.value && el.tagName !== "SELECT" && /field|box|combobox/.test(k)) item.value = clean(el.value).slice(0, 60);
    if (el.checked) item.checked = true;
    if (el.disabled || el.getAttribute("aria-disabled") === "true") item.disabled = true;
    if (r.right <= 0 || r.left >= W) continue;
    if (r.bottom > 0 && r.top < H) { if (out.length < 70) out.push(item); }
    else if (r.top >= H && below.length < 10) { item.below = true; below.push(item); }
  }
  const headings = [...document.querySelectorAll("h1,h2,h3")].map(h => clean(h.innerText))
    .filter(Boolean).slice(0, 8);
  const text = clean(document.body ? document.body.innerText : "").slice(0, 700);
  // A human check over the page (PerimeterX, reCAPTCHA, hCaptcha,
  // Cloudflare): invisible in the controls, and nothing the loop can do.
  const check = document.querySelector(
    'iframe[id*="captcha" i], iframe[src*="captcha" i], iframe[title*="challenge" i], ' +
    'iframe[src*="challenges.cloudflare.com"], #px-captcha, .g-recaptcha, .h-captcha, #cf-challenge-running');
  const shown = check && (() => { const r = check.getBoundingClientRect();
    return r.width > 50 && r.height > 50 && getComputedStyle(check).visibility !== "hidden"; })();
  const human = /press (&|and) hold|verify (that )?you are (a )?human|are you a robot|unusual traffic/i
    .test(text);
  return { headings, text, elements: out.concat(below), blocker: (shown || human) ? "human check" : "" };
}
"""


# ── the browser ──────────────────────────────────────────────

class Browser:
    """One Chromium, its context, and the page the agent is on.

    Everything runs on the server's one asyncio loop; tool calls are
    serialised by the caller, while the live view runs beside them."""

    def __init__(self, emit):
        self._emit = emit                # (event, params) -> notification
        self.pw = self.browser = self.context = None
        self.page = None
        self.mouse = (VIEWPORT["width"] / 2, VIEWPORT["height"] / 2)
        self.new_tab = False             # a tab opened since the last report
        self._launch_lock = asyncio.Lock()

        # The live view.
        self.watch_until = 0.0
        self._watch_task = None
        self._cdp = None
        self._cast_page = None

    # ── lifecycle ──

    def watched(self):
        return time.monotonic() < self.watch_until

    def alive(self):
        try:
            return self.browser is not None and self.browser.is_connected()
        except Exception:                                    # noqa: BLE001
            return False

    async def ensure(self):
        """The page to act on, launching Chromium the first time and again if
        it went away (a crash, or the last tab closed itself)."""
        async with self._launch_lock:
            if not self.alive():
                await self._launch()
            page = self.current()
            if page is None:
                page = await self.context.new_page()
                self.page = page
                self.new_tab = False          # ours, not one the site opened
            return page

    async def _launch(self):
        from playwright.async_api import async_playwright

        await self.close()
        state = _state_path()
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(**launch_kwargs(headless=True))
        try:
            self.context = await self.browser.new_context(**context_kwargs(self.browser, state))
        except Exception as e:                               # noqa: BLE001 — degrade, don't die
            # A corrupt or truncated auth.json shouldn't cost the user their
            # browser entirely; it should cost them their logins, which they
            # can redo from the Browser app.
            if not state:
                raise
            _log(f"couldn't load saved logins from {state} ({e}); continuing signed out")
            self.context = await self.browser.new_context(**context_kwargs(self.browser))
            state = None
        await self.context.add_init_script(OFFSCREEN_ANIMATION_PAUSE_JS)
        self.context.on("page", self._on_page)
        self.page = await self.context.new_page()
        self.new_tab = False
        self.mouse = (VIEWPORT["width"] / 2, VIEWPORT["height"] / 2)
        _log(f"chromium ready ({'with saved logins' if state else 'signed out'})")

        # A user who just solved a check the agent handed them (see
        # src/browser_handoff.py) left off on some page; carry on from there.
        import src.browser_handoff as browser_handoff
        url = browser_handoff.take_resume(os.environ.get("FC_BROWSER_STORAGE_STATE"))
        if url:
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:                           # noqa: BLE001
                _log(f"couldn't reopen {url}: {e}")
        if self.watched():
            self._start_watching()

    async def close(self):
        await self._stop_cast()
        for target, method in ((self.context, "close"), (self.browser, "close"),
                               (self.pw, "stop")):
            if target is None:
                continue
            try:
                await getattr(target, method)()
            except Exception:                                # noqa: BLE001 — teardown
                pass
        self.pw = self.browser = self.context = self.page = None

    def _on_page(self, page):
        """Follow a tab the page opened: target=_blank links and sign-in
        popups land there, and it's nearly always where the agent needs to be."""
        self.page = page
        self.new_tab = True

    def current(self):
        """The page being driven, falling back to the newest open one when it
        closed under us (a popup that closes itself after signing in)."""
        if self.page is not None:
            try:
                if not self.page.is_closed():
                    return self.page
            except Exception:                                # noqa: BLE001
                pass
        pages = []
        if self.context is not None:
            try:
                pages = [p for p in self.context.pages if not p.is_closed()]
            except Exception:                                # noqa: BLE001
                pages = []
        self.page = pages[-1] if pages else None
        return self.page

    # ── looking ──

    async def settle(self, quick=False):
        """Give whatever the action set off a moment to land: a click starts
        its navigation a beat later, and a screenshot taken at once shows the
        page being left."""
        await asyncio.sleep(0.15 if quick else 0.4)
        page = self.current()
        if page is None:
            return
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:                                    # noqa: BLE001
            return
        if not quick:
            try:
                await page.wait_for_load_state("load", timeout=2500)
            except Exception:                                # noqa: BLE001
                pass

    async def shot(self):
        """The page as the model sees it, base64 JPEG. Retried once: in the
        middle of a navigation there's no renderer to capture."""
        for attempt in (0, 1):
            page = self.current()
            if page is None:
                return None
            try:
                data = await page.screenshot(type="jpeg", quality=SHOT_QUALITY,
                                             caret="initial", timeout=10000)
                return base64.b64encode(data).decode("ascii")
            except Exception as e:                           # noqa: BLE001
                if attempt:
                    _log(f"screenshot failed: {e}")
                    return None
                await self.settle(quick=True)
        return None

    async def where(self):
        page = self.current()
        if page is None:
            return {"url": "", "title": ""}
        try:
            title = await page.title()
        except Exception:                                    # noqa: BLE001 — mid-navigation
            title = ""
        return {"url": page.url, "title": title}

    # ── the mouse ──

    async def glide(self, x, y):
        """Move the mouse to x,y. Watched, it travels — the viewer animates the
        same path — so the person sees where it's going before it clicks."""
        page = self.current()
        if self.watched():
            self._emit("cursor", {"x": x, "y": y, "kind": "move", "ms": int(CURSOR_GLIDE * 1000)})
            await page.mouse.move(x, y, steps=12)
            await asyncio.sleep(CURSOR_GLIDE)
        else:
            await page.mouse.move(x, y)
        self.mouse = (x, y)

    def mark(self, kind):
        """A press or release, for the viewer to draw where the cursor is."""
        x, y = self.mouse
        self._emit("cursor", {"x": x, "y": y, "kind": kind})

    # ── the live view ──

    def watch(self, seconds):
        """FreeClaw has someone watching: send frames for the next `seconds`.
        Renewed every few seconds while they are; lapses on its own when they
        stop, or when FreeClaw does."""
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = 10.0
        seconds = min(max(seconds, 0.0), MAX_WATCH_LEASE)
        was = self.watched()
        self.watch_until = time.monotonic() + seconds
        if seconds and not was:
            self._start_watching()

    def _start_watching(self):
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.ensure_future(self._watch_loop())

    async def _watch_loop(self):
        """Keeps the screencast on whichever page is current while watched,
        and stops it once the lease lapses. Doesn't launch a browser: someone
        opening the viewer isn't a reason to start Chromium."""
        first = True
        try:
            while self.watched():
                if self.alive():
                    page = self.current()
                    if page is not None and page is not self._cast_page:
                        await self._cast(page)
                        first = True
                    if first and page is not None:
                        # A still page sends no screencast frame until it next
                        # repaints, so the viewer gets one to start from.
                        first = False
                        await self._send_still(page)
                        self._emit("live", await self.where())
                await asyncio.sleep(0.5)
        finally:
            await self._stop_cast()

    async def _send_still(self, page):
        try:
            data = await page.screenshot(type="jpeg", quality=LIVE_QUALITY, timeout=5000)
        except Exception:                                    # noqa: BLE001 — mid-navigation
            return
        self._emit("frame", {"data": base64.b64encode(data).decode("ascii")})

    async def _cast(self, page):
        await self._stop_cast()
        self._cast_page = page
        try:
            cdp = await page.context.new_cdp_session(page)
            # Bound to this session: frames still in flight from the page
            # being left mustn't be published over the new one.
            cdp.on("Page.screencastFrame", lambda params, c=cdp: self._on_frame(c, params))
            await cdp.send("Page.startScreencast", {
                "format": "jpeg", "quality": LIVE_QUALITY,
                "maxWidth": VIEWPORT["width"], "maxHeight": VIEWPORT["height"],
            })
            self._cdp = cdp
        except Exception as e:                               # noqa: BLE001 — a picture is optional
            _log(f"screencast unavailable: {e}")

    async def _stop_cast(self):
        cdp, self._cdp, self._cast_page = self._cdp, None, None
        if cdp is None:
            return
        for call in (lambda: cdp.send("Page.stopScreencast"), cdp.detach):
            try:
                await call()
            except Exception:                                # noqa: BLE001 — page already gone
                pass

    def _on_frame(self, cdp, params):
        if cdp is not self._cdp:
            return
        self._emit("frame", {"data": params.get("data") or ""})
        # Chromium sends the next frame only once this one is acked, so the
        # ack's delay is the frame-rate cap.
        asyncio.ensure_future(self._ack(cdp, params.get("sessionId")))

    async def _ack(self, cdp, session_id):
        await asyncio.sleep(LIVE_MIN_FRAME_GAP)
        if cdp is not self._cdp:
            return
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:                                    # noqa: BLE001 — page went away
            pass


def _state_path():
    """The storage_state this child should load, or None.

    Passed in the environment by src/mcp_client.py, which resolves it per
    FreeClaw user. Each user gets their own child (`for_user`), so two users
    never share a cookie jar."""
    path = (os.environ.get("FC_BROWSER_STORAGE_STATE") or "").strip()
    if not path or not os.path.exists(path):
        return None
    if os.path.getsize(path) == 0:
        _log(f"ignoring empty storage state at {path}")
        return None
    return path


def _web_url(raw):
    """What navigate should open for `raw`, or a ToolError. Only the web:
    file:// and chrome:// would turn the browser into a reader for the
    machine FreeClaw runs on."""
    url = (raw or "").strip()
    if not url:
        raise ToolError("url is required.")
    if "://" not in url and not url.lower().startswith(("about:", "data:", "javascript:")):
        url = "https://" + url
    if not url.lower().startswith(_SCHEMES):
        raise ToolError("Only http:// and https:// addresses can be opened.")
    return url


# ── the tools ────────────────────────────────────────────────

class Tools:
    """What each tool does. Returns MCP content blocks; raises ToolError for
    a mistake the model should see."""

    def __init__(self, browser, emit):
        self.b = browser
        self._emit = emit

    async def run(self, name, args):
        if name not in TOOL_NAMES:
            raise ToolError(f"Unknown tool {name!r}.")
        args = args if isinstance(args, dict) else {}
        return await getattr(self, "t_" + name)(**_known(name, args))

    def _status(self, action, busy=True, **extra):
        """What the viewer's status line says the agent is doing."""
        self._emit("live", {"action": action, "busy": busy, **extra})

    async def _reply(self, text="", image=True):
        """The page as it now is: where it is, any notes, and a screenshot."""
        await self.b.settle()
        where = await self.b.where()
        self._status("", busy=False, **where)
        lines = []
        if text:
            lines.append(text)
        if self.b.new_tab:
            self.b.new_tab = False
            lines.append("It opened in a new tab; you're on that tab now (back closes it).")
        title = where.get("title") or "(untitled)"
        lines.append(f"{title} | {where.get('url') or 'about:blank'}")
        content = [{"type": "text", "text": "\n".join(lines)}]
        if image:
            data = await self.b.shot()
            if data:
                content.append({"type": "image", "data": data, "mimeType": "image/jpeg"})
        return content

    def _xy(self, x, y, label="x,y"):
        x, y = _number(x, label.split(",")[0]), _number(y, label.split(",")[-1])
        if not (0 <= x < VIEWPORT["width"] and 0 <= y < VIEWPORT["height"]):
            raise ToolError(f"{label} must be inside the {VIEWPORT['width']}x{VIEWPORT['height']} "
                            "screenshot. Scroll to bring something else into view.")
        return x, y

    # ── navigation ──

    async def t_navigate(self, url=""):
        url = _web_url(url)
        page = await self.b.ensure()
        host = url.split("://", 1)[-1].split("/", 1)[0]
        self._status(f"Opening {host}")
        error = ""
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:                               # noqa: BLE001
            lines = str(e).strip().splitlines()
            error = lines[0] if lines else "The page wouldn't load."
        if error and "interrupted by another navigation" not in error:
            content = await self._reply(f"Couldn't load {url}: {error}")
            raise ToolError(content)
        return await self._reply()

    async def t_back(self):
        page = await self.b.ensure()
        self._status("Going back")
        try:
            moved = await page.go_back(wait_until="domcontentloaded", timeout=15000)
        except Exception:                                    # noqa: BLE001 — landed regardless
            moved = True
        if moved is None:
            # No history in this tab: a popup the page opened. Closing it is
            # the way back to where the agent came from.
            pages = [p for p in self.b.context.pages if not p.is_closed()]
            if len(pages) > 1:
                await page.close()
                self.b.page = None
                self.b.current()
                return await self._reply("Closed that tab.")
            return await self._reply("There's no previous page.")
        return await self._reply()

    # ── pointing ──

    async def t_click(self, x=None, y=None, double=False, button="left"):
        x, y = self._xy(x, y)
        button = button if button in ("left", "right", "middle") else "left"
        page = await self.b.ensure()
        self._status("Double-clicking" if double else "Clicking")
        await self.b.glide(x, y)
        self.b.mark("click")
        await page.mouse.click(x, y, button=button, click_count=2 if double else 1)
        return await self._reply()

    async def t_hover(self, x=None, y=None):
        x, y = self._xy(x, y)
        await self.b.ensure()
        self._status("Pointing")
        await self.b.glide(x, y)
        return await self._reply()

    async def t_drag(self, x=None, y=None, to_x=None, to_y=None):
        x, y = self._xy(x, y)
        to_x, to_y = self._xy(to_x, to_y, "to_x,to_y")
        page = await self.b.ensure()
        self._status("Dragging")
        await self.b.glide(x, y)
        self.b.mark("down")
        await page.mouse.down()
        try:
            await self.b.glide(to_x, to_y)
            # A slider or a map reads the drag from the moves in between, not
            # the endpoints; glide's steps are those moves.
            if not self.b.watched():
                await page.mouse.move(to_x, to_y, steps=15)
        finally:
            self.b.mark("up")
            await page.mouse.up()
        return await self._reply()

    async def t_scroll(self, direction="down", x=None, y=None):
        direction = direction if direction in ("down", "up", "left", "right") else "down"
        if x is None or y is None:
            x, y = VIEWPORT["width"] / 2, VIEWPORT["height"] / 2
        x, y = self._xy(x, y)
        page = await self.b.ensure()
        self._status("Scrolling " + direction)
        # The wheel scrolls whatever is under the mouse, which is how a
        # side panel or a dropdown list gets scrolled instead of the page.
        if (x, y) != self.b.mouse:
            await self.b.glide(x, y)
        step_y = VIEWPORT["height"] * 0.8
        step_x = VIEWPORT["width"] * 0.8
        dx = {"left": -step_x, "right": step_x}.get(direction, 0)
        dy = {"up": -step_y, "down": step_y}.get(direction, 0)
        await page.mouse.wheel(dx, dy)
        # Smooth scrolling animates; a screenshot at once catches it midway.
        await asyncio.sleep(0.35)
        return await self._reply()

    # ── the keyboard ──

    async def t_type(self, text="", enter=False):
        text = str(text or "")
        if not text and not enter:
            raise ToolError("text is required.")
        page = await self.b.ensure()
        self._status("Typing")
        delay = 0
        if self.b.watched() and text:
            delay = min(TYPE_DELAY_MS, TYPE_BUDGET_MS / len(text))
        await page.keyboard.type(text, delay=delay)
        if enter:
            await page.keyboard.press("Enter")
        return await self._reply()

    async def t_key(self, key=""):
        key = str(key or "").strip()
        if not key:
            raise ToolError("key is required.")
        page = await self.b.ensure()
        self._status("Pressing " + key)
        try:
            await page.keyboard.press(key)
        except Exception as e:                               # noqa: BLE001
            message = str(e).strip().splitlines()
            raise ToolError(f"Couldn't press {key!r}: {message[0] if message else e}")
        return await self._reply()

    # ── reading ──

    async def t_screenshot(self):
        await self.b.ensure()
        self._status("Looking at the page")
        return await self._reply()

    async def t_read_text(self, start=0):
        try:
            start = max(int(start or 0), 0)
        except (TypeError, ValueError):
            start = 0
        page = await self.b.ensure()
        self._status("Reading the page")
        await self.b.settle(quick=True)
        try:
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception as e:                               # noqa: BLE001 — mid-navigation
            raise ToolError(f"Couldn't read the page yet ({str(e).splitlines()[0]}). Try again.")
        text = re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", str(text or ""))).strip()
        chunk = text[start:start + TEXT_CHUNK]
        rest = len(text) - start - len(chunk)
        note = f"\n[{rest} more characters: read_text start={start + len(chunk)}]" if rest > 0 else ""
        content = await self._reply(image=False)
        content[0]["text"] += "\n\n" + (chunk or "(no text)") + note
        return content

    async def t_find(self, text=""):
        text = str(text or "").strip()
        if not text:
            raise ToolError("text is required.")
        page = await self.b.ensure()
        self._status(f"Looking for “{text[:40]}”")
        await self.b.settle(quick=True)
        try:
            hits = await page.evaluate(_FIND_JS, text)
        except Exception as e:                               # noqa: BLE001 — mid-navigation
            raise ToolError(f"Couldn't search the page yet ({str(e).splitlines()[0]}). Try again.")
        lines = []
        for h in hits or []:
            label = f"{h.get('kind')} \"{h.get('text')}\""
            if h.get("onScreen"):
                lines.append(f"{label} at ({h.get('x')}, {h.get('y')})")
            else:
                lines.append(f"{label}: off-screen, scroll "
                             f"{'up' if h.get('where') == 'above' else 'down'}")
        found = "\n".join(lines) if lines else f"Nothing matching \"{text}\" on this page."
        content = await self._reply(image=False)
        content[0]["text"] = found + "\n" + content[0]["text"]
        return content

    # ── the fast loop's eyes and hands ──

    async def _await_change(self, page, url_before, budget):
        """After a fast_step action: wait (up to `budget` seconds) for the URL
        to change — a search submitted with Enter can take a second to start
        navigating, and settle() alone would read the page being left — then
        for the page's content to stop changing, so results that render in
        after load (most shops) are there to be read."""
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            page = self.b.current() or page
            if self.b.new_tab or page.url != url_before:
                break
            await asyncio.sleep(0.15)
        await self.b.settle()
        page = self.b.current() or page
        last = None
        for _ in range(8):
            try:
                size = await page.evaluate(
                    "() => (document.body ? document.body.innerText.length : 0) + ':' + "
                    "document.querySelectorAll('a,button,input').length")
            except Exception:                                # noqa: BLE001 — mid-navigation
                size = None
            if size is not None and size == last:
                break
            last = size
            await asyncio.sleep(0.3)
        return page

    async def t_fast_step(self, action="observe", x=None, y=None, text="", enter=False, label="",
                          ref=0):
        page = await self.b.ensure()
        url_before = page.url
        what = str(label or "")[:40]
        if action in ("click", "type"):
            x, y = self._xy(x, y)
            self._status((f"Clicking “{what}”" if action == "click" else f"Typing into “{what}”")
                         if what else "Clicking")
            await self.b.glide(x, y)
            self.b.mark("click")
            await page.mouse.click(x, y)
            if action == "type":
                await page.keyboard.press("Control+A")
                await page.keyboard.type(str(text or ""), delay=0)
                if enter:
                    await page.keyboard.press("Enter")
        elif action == "select":
            # A <select> at x,y, set to the option whose text is `text`, the
            # way a person picking from it would: change and input fire.
            x, y = self._xy(x, y)
            self._status(f"Choosing “{str(text)[:40]}”")
            await self.b.glide(x, y)
            ok = await page.evaluate("""([x, y, want, ref]) => {
                const hit = document.querySelector(`[data-fc-ref="${ref}"]`) || document.elementFromPoint(x, y);
                const sel = hit && (hit.tagName === "SELECT" ? hit : hit.closest("select") ||
                                    (hit.tagName === "LABEL" && hit.control));
                if (!sel || sel.tagName !== "SELECT") return false;
                const norm = s => String(s || "").replace(/\\s+/g, " ").trim().toLowerCase();
                const opt = [...sel.options].find(o => norm(o.text) === norm(want))
                         || [...sel.options].find(o => norm(o.text).includes(norm(want)));
                if (!opt) return false;
                sel.value = opt.value;
                sel.dispatchEvent(new Event("input", { bubbles: true }));
                sel.dispatchEvent(new Event("change", { bubbles: true }));
                return true;
            }""", [x, y, str(text or ""), int(ref or 0)])
            if not ok:
                raise ToolError(f"No dropdown option “{text}” at {x},{y}.")
        elif action == "scroll":
            self._status("Scrolling down")
            await page.mouse.wheel(0, VIEWPORT["height"] * 0.8)
            await asyncio.sleep(0.35)
        elif action == "back":
            self._status("Going back")
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=15000)
            except Exception:                                # noqa: BLE001 — landed regardless
                pass
        budget = 4.0 if (action == "type" and enter) else 1.5 if action in ("click", "select") else 0.0
        page = await self._await_change(page, url_before, budget)
        try:
            state = await page.evaluate(_ELEMENTS_JS)
        except Exception as e:                               # noqa: BLE001 — mid-navigation
            await self.b.settle()
            try:
                state = await page.evaluate(_ELEMENTS_JS)
            except Exception:                                # noqa: BLE001
                raise ToolError(f"Couldn't read the page yet ({str(e).splitlines()[0]}).")
        where = await self.b.where()
        self._status("", busy=False, **where)
        state.update({"url": where.get("url") or "", "title": where.get("title") or ""})
        if self.b.new_tab:
            self.b.new_tab = False
            state["new_tab"] = True
        return [{"type": "text", "text": json.dumps(state)}]

    # ── handing over ──

    async def t_request_captcha_help(self, reason=""):
        """Dumps the context's cookies to a private temp file and says where;
        FreeClaw moves it into the user's profile and draws a button
        (src/browser_handoff.py). Cookies never ride in the result itself."""
        if not self.b.alive():
            raise ToolError("Nothing to hand over: open the page with the check first.")
        page = await self.b.ensure()
        url = page.url
        if not url.lower().startswith(_SCHEMES):
            raise ToolError("Nothing to hand over: open the page with the check first.")
        self._status("Asking you to solve a check", busy=False)
        import src.browser_handoff as browser_handoff
        state = await self.b.context.storage_state()
        try:
            title = await page.title()
        except Exception:                                    # noqa: BLE001 — cosmetic
            title = ""
        path = browser_handoff.write_state_file(state)
        return [{"type": "text", "text": json.dumps({"url": url, "title": title,
                                                     "state_file": path})}]


def _known(name, args):
    """`args` limited to what the tool takes, so a model inventing a parameter
    gets the tool's own behaviour rather than a TypeError."""
    schema = next(t["inputSchema"] for t in TOOLS if t["name"] == name)
    allowed = set(schema.get("properties") or {})
    return {k: v for k, v in args.items() if k in allowed}


# ── the server ───────────────────────────────────────────────

class Server:
    def __init__(self):
        self._out = sys.stdout.buffer
        self.browser = Browser(self.event)
        self.tools = Tools(self.browser, self.event)
        self._tool_lock = asyncio.Lock()

    # ── writing ──

    def write(self, message):
        # ensure_ascii keeps every byte on the pipe ASCII, whatever the console
        # code page; the client decodes UTF-8 either way.
        line = json.dumps(message, separators=(",", ":")) + "\n"
        try:
            self._out.write(line.encode("ascii"))
            self._out.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass                                             # FreeClaw went away; EOF follows

    def event(self, name, params):
        self.write({"jsonrpc": "2.0", "method": LIVE_PREFIX + name, "params": params})

    def reply(self, request_id, result=None, error=None):
        message = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result
        self.write(message)

    # ── reading ──

    async def request(self, message):
        request_id, method = message.get("id"), message.get("method")
        params = message.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            self.reply(request_id, {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })
        elif method == "ping":
            self.reply(request_id, {})
        elif method == "tools/list":
            self.reply(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            self.reply(request_id, await self.call(params.get("name"), params.get("arguments")))
        else:
            self.reply(request_id, error={"code": -32601, "message": f"Method not found: {method}"})

    async def call(self, name, args):
        # One browser, one action at a time: two clicks interleaving would
        # each screenshot the other's result.
        async with self._tool_lock:
            try:
                return {"content": await self.tools.run(name, args)}
            except ToolError as e:
                self._stopped()
                detail = e.args[0] if e.args else "Error"
                if isinstance(detail, list):
                    return {"content": detail, "isError": True}
                return {"content": [{"type": "text", "text": str(detail)}], "isError": True}
            except Exception as e:                           # noqa: BLE001 — reported, not fatal
                self._stopped()
                _log(f"{name} failed: {type(e).__name__}: {e}")
                lines = str(e).strip().splitlines()
                text = lines[0] if lines else type(e).__name__
                if not self.browser.alive():
                    text += " (the browser closed; the next call starts it again)"
                return {"content": [{"type": "text", "text": f"{name} failed: {text}"}],
                        "isError": True}

    def _stopped(self):
        """A tool failed: whatever the viewer was told the agent is doing,
        it has stopped doing it."""
        self.event("live", {"action": "", "busy": False})

    def notification(self, message):
        method = message.get("method") or ""
        if method == LIVE_PREFIX + "watch":
            self.browser.watch((message.get("params") or {}).get("seconds"))
        elif method == "notifications/initialized":
            # Tells FreeClaw this user's browser exists, so a viewer that was
            # already open starts its watch from the agent's first action.
            self.event("live", {"ready": True})

    async def serve(self):
        loop = asyncio.get_running_loop()
        inbox = asyncio.Queue()

        def reader():
            # A thread rather than an asyncio pipe reader: the latter isn't
            # available for stdin on Windows' proactor loop.
            try:
                for raw in sys.stdin.buffer:
                    loop.call_soon_threadsafe(inbox.put_nowait, raw)
            finally:
                loop.call_soon_threadsafe(inbox.put_nowait, None)

        threading.Thread(target=reader, daemon=True, name="stdin").start()
        tasks = set()
        while True:
            raw = await inbox.get()
            if raw is None:
                break
            try:
                message = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("method") and "id" in message:
                # A task, so the watch lease and pings are still read while a
                # tool call is in the middle of a page load.
                task = asyncio.ensure_future(self.request(message))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            elif message.get("method"):
                self.notification(message)
        await self.browser.close()


# Playwright drives Chromium through a Node.js driver, and each user's browser
# server starts its own: measured at 144MB PSS, nearly as much as the Chromium
# beside it (~190MB). Its JavaScript is plumbing — relaying protocol messages
# and screenshots — so a small heap and no JIT cost it nothing measurable:
# 104MB with these, the same steps in the same time, every tool and the live
# view's frames still working. That's ~40MB a user back on a small machine.
# (--lite-mode went further and broke the driver outright.) Only a default:
# whatever NODE_OPTIONS the environment already has is left alone.
DRIVER_NODE_OPTIONS = "--max-old-space-size=64 --max-semi-space-size=1 --jitless"


def main():
    # Before anything starts the driver: it reads NODE_OPTIONS when it launches.
    os.environ.setdefault("NODE_OPTIONS", DRIVER_NODE_OPTIONS)
    if sys.platform == "win32":
        # Playwright drives Chromium through a subprocess, which only the
        # proactor loop supports on Windows. The default since 3.8; pinned so
        # an embedding that changed the policy doesn't break the browser.
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(Server().serve())


if __name__ == "__main__":
    main()
