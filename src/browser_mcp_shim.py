"""Entry point FreeClaw runs instead of `shadow_web.mcp.server` directly.

shadow-web's MCP server builds its browser in one place — `_ensure_browser()` —
and hardcodes it:

    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(locale="en-US")

An in-memory context, no `storage_state`, no `user_data_dir`, and no
environment variable anywhere in the MCP path that changes it (the `--session`
profile and `--headed` flags belong to shadow-web's separate *agent* CLI, not
this server). So every login the agent performs dies with the process, and
sites behind a sign-in wall are permanently out of reach.

This module swaps that one function for a version that loads the current
FreeClaw user's saved cookies, then hands control straight back to shadow-web.
The ~770 lines of snapshot/compress/query work — the part FreeClaw actually
wants from the package — run untouched.

**Why a shim and not a patch.** Editing the installed package would be reverted
by the next `pip install -r requirements.txt`, silently, and the breakage would
show up as "the agent is logged out again" weeks later. Nothing here writes to
site-packages, so the pin in requirements.txt keeps meaning what it says.

**What it costs.** `_session` and `_ensure_browser` are private names. Nothing
stops a shadow-web release renaming them, and the pin exists partly for this
reason — see the version policy in requirements.txt. `_assert_compatible()`
turns that into a loud failure at startup, in this file, rather than a quiet
one where the shim's replacement is ignored and a second cookie-less browser
launches instead.

Nothing here may write to stdout: that's the JSON-RPC channel to FreeClaw's MCP
client. Diagnostics go to stderr, which `_StdioServer` drains separately.
"""

import os
import sys

# The viewport the agent browses at. Shared with src/browser_takeover.py so the
# human's sign-in happens at the same size the agent later sees — a few sites
# serve a different DOM to a different viewport, and a login captured against
# the mobile layout can land the agent somewhere it can't navigate.
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


def _log(message):
    """Stderr, never stdout — see the module docstring."""
    print(f"[browser-mcp-shim] {message}", file=sys.stderr, flush=True)


def chrome_user_agent(browser):
    """A plain-Chrome UA string matching `browser`'s actual version.

    Playwright's headless Chromium advertises itself as `HeadlessChrome`, which
    is exactly the token sign-in pages look at when they decide to refuse. The
    human signs in through a *headful* browser (src/browser_takeover.py) and
    the agent replays those cookies through a headless one, so if the two
    disagree about who they are, the site sees a session that changed browser
    mid-flight and re-challenges.

    Derived from `browser.version` rather than hardcoded so it ages with
    whatever Chromium playwright installed, instead of pinning a version string
    that will be years stale by the time anyone notices."""
    version = ""
    try:
        version = (browser.version or "").strip()
    except Exception:                             # noqa: BLE001 — cosmetic
        pass
    major = version.split(".")[0] if version else ""
    if not major.isdigit():
        # No version to work from: let playwright's default stand rather than
        # invent a number. Worst case is the HeadlessChrome token, which is
        # what we had before this shim existed.
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


def _state_path():
    """The storage_state this child should load, or None.

    Passed in the environment by src/mcp_client.py, which resolves it per
    FreeClaw user. `_sig()` there includes the env in the key identifying a
    child process, so two users asking for different paths get two separate
    children and never share a cookie jar."""
    path = (os.environ.get("FC_BROWSER_STORAGE_STATE") or "").strip()
    if not path:
        return None
    if not os.path.exists(path):
        # Normal on a user who has never signed into anything.
        return None
    if os.path.getsize(path) == 0:
        _log(f"ignoring empty storage state at {path}")
        return None
    return path


def _assert_compatible(sw):
    """Fail now, loudly, if the internals this shim steers have moved."""
    problems = []
    if not isinstance(getattr(sw, "_session", None), dict):
        problems.append("shadow_web.mcp.server._session is missing or not a dict")
    if not callable(getattr(sw, "_ensure_browser", None)):
        problems.append("shadow_web.mcp.server._ensure_browser is missing")
    if not callable(getattr(sw, "create_mcp_server", None)):
        problems.append("shadow_web.mcp.server.create_mcp_server is missing")
    if problems:
        raise RuntimeError(
            "This FreeClaw build drives shadow-web's browser through internals that "
            "have changed in the installed version: " + "; ".join(problems) + ". "
            "The pinned version in requirements.txt (shadow-web==0.4.1) is the one "
            "this was written against."
        )


def _install_browser_hook(sw):
    """Replace `_ensure_browser` with the storage_state-aware version.

    Replacing the *function* rather than pre-filling `_session`: playwright's
    async objects belong to the event loop that created them, and FastMCP
    starts its own loop inside `mcp.run()`. A context built eagerly here would
    be bound to a loop that's dead by the time the first tool call arrives.
    Swapping the function keeps creation lazy and inside the right loop, which
    is where upstream did it too."""

    async def _ensure_browser():
        session = sw._session

        # Upstream's recreate-on-crash check, kept verbatim in effect: a page
        # that closed under us (renderer crash, site called window.close)
        # should rebuild rather than raise on every later tool call.
        if "page" in session:
            try:
                if session["page"].is_closed():
                    _log("page closed, recreating the browser session")
                    session.clear()
            except Exception:                     # noqa: BLE001 — upstream does the same
                _log("page check failed, recreating the browser session")
                session.clear()

        if "playwright" in session:
            return

        from playwright.async_api import async_playwright

        state = _state_path()
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(**launch_kwargs(headless=True))
        try:
            context = await browser.new_context(**context_kwargs(browser, state))
        except Exception as e:                    # noqa: BLE001 — degrade, don't die
            # A corrupt or truncated auth.json shouldn't cost the user their
            # browser tools entirely; it should cost them their logins, which
            # they can redo from the sign-in page.
            if not state:
                raise
            _log(f"couldn't load saved logins from {state} ({e}); continuing signed out")
            context = await browser.new_context(**context_kwargs(browser))
            state = None

        await context.add_init_script(OFFSCREEN_ANIMATION_PAUSE_JS)
        page = await context.new_page()

        # Every key upstream's own _ensure_browser sets. browser_info() and the
        # tools read these, so a missing one is a crash in a tool that looks
        # unrelated to this file.
        session.update({
            "playwright_driver": pw,
            "playwright": browser,
            "browser": browser,
            "context": context,
            "page": page,
            "browser_type": "chromium",
            "requested_browser": "chromium",
            "browser_fallback": False,
        })
        _log(f"chromium ready ({'with saved logins' if state else 'signed out'})")

    sw._ensure_browser = _ensure_browser


def main():
    # Chromium, not camoufox. FreeClaw already pins this in BUILTIN_SERVERS and
    # the hook above only knows how to build Chromium, so set it here too:
    # a stale .env that unsets it must not leave the two disagreeing.
    os.environ["SHADOW_WEB_BROWSER"] = "chromium"

    from shadow_web.mcp import server as sw
    import src.browser_handoff as browser_handoff
    import src.browser_mcp_tools as browser_tools

    _assert_compatible(sw)
    _install_browser_hook(sw)
    # sw.main() is create_mcp_server() then run(); built here instead so the
    # tools can be presented as a browser the model will reach for and can
    # drive (src/browser_mcp_tools.py) before the server starts answering.
    mcp = sw.create_mcp_server()
    problems = browser_tools.check(sw, mcp)
    if problems:
        # Not fatal: the stock tools still work, they're only harder to use.
        _log("browser tool tuning skipped — " + "; ".join(problems))
    else:
        browser_tools.install(sw, mcp, log=_log)
    # After the tuning, so the snapshot it wraps keeps the tuned description.
    browser_handoff.register(mcp, sw)
    mcp.run()


if __name__ == "__main__":
    main()
