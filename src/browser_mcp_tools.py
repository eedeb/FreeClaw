"""How the agent's browser presents itself to the model.

shadow-web's MCP server is a capable browser and a poor one to be handed cold.
Driving it as the agent does, it failed mostly on presentation:

  * Its tools never said they *were* a browser — navigate is "Open URL in
    Playwright and build grouped XML Action Map snapshot" — so asked to "use
    the web browser", a model with another server's cloud browser connected
    picks that one, which has none of the logins saved in the Browser app.
  * The element list a snapshot returns is the first 15 elements in page
    order, nearly always links. GitHub's sign-in page listed the username box
    but not the password box, and Bing's listed no search box at all; a field
    that isn't listed has no id, and fill can't reach it.
  * click and fill take an element's "id", but the list shows a "bind_id"
    beside it, and passing that one fails after a timeout.
  * A click that navigates — a submit, "Add to cart" — raises once the page it
    ran in is gone, so a click that worked comes back as an error and the
    model clicks again.
  * Nothing presses a key, and Enter is how most search boxes submit.
  * After an action lands on another page, the element list and the content
    outline still describe the page it started on.
  * Nine tools take raw HTML pasted into the call rather than reading the
    page; the model has none to give them, and each has a twin that reads the
    page directly. They are resent with every request for nothing.

Each is handled here, on the server shadow-web builds, without editing the
package — the same reasoning as src/browser_mcp_shim.py, and the same cost:
the names steered below are shadow-web's private ones, and `check()` fails
loudly at startup if a release moves them.
"""

import asyncio

# Tools that only work on raw HTML passed in the call, never on the page the
# browser is on. Each has a *_session or content_* twin that reads the page.
HIDDEN_TOOLS = ("compress_html", "shadow_grep_html", "compress_html_to_xml",
                "schema_table", "schema_form", "schema_list", "schema_page",
                "schema_json", "schema_csv")

# How many elements a terse snapshot lists, form controls first.
TERSE_LIMIT = 30

_ID_NOTE = ('Elements are identified by their "id" (e.g. "12") — pass that, '
            'not the "bind_id", to click, fill and press_key.')

DESCRIPTIONS = {
    "navigate": (
        "Open a web page in your web browser: a real Chromium you drive, carrying "
        "the logins the user saved in the Browser app. This is the browser to use "
        "whenever the user says \"the browser\" or \"web browser\", or a task needs "
        "a website — not another service's cloud browser, which has none of their "
        "logins.\n\n"
        "Returns the page's interactive elements, form fields and buttons first, "
        "then links. " + _ID_NOTE + " If what you need isn't listed, find it with "
        "shadow_query (e.g. \"type:input\", \"label~/search/i\"). To read the page's "
        "text, call content_outline, then content_blocks.\n\n"
        "Some sites refuse automated browsers outright (\"Access Denied\"). Say so; "
        "don't retry or work around it.\n\n"
        "detail: terse (default) | minimal | xml | full"),
    "snapshot": (
        "Re-read the current page after it changed: after a click, after typing, "
        "or when a page loads its content late. Same element list as navigate. "
        + _ID_NOTE + " diff=true lists only what appeared, changed or "
        "disappeared since the last snapshot.\n\n"
        "detail: terse (default) | minimal | xml | full"),
    "shadow_query": (
        "Find elements on the current page that the element list left out. "
        "Query examples:\n"
        "  type:input           every text field\n"
        "  label~/search/i      anything labelled like search\n"
        "  type:button intent:login\n"
        "  group:Login Form\n"
        "  id:1,3,5\n"
        "format: terse (default) | json | xml"),
}

CLICK_DESCRIPTION = (
    "Click an element on the current page. " + _ID_NOTE + " The reply says which "
    "page the browser is on afterwards. A click that opens another page (a "
    "submit, \"Add to cart\", a link) is reported as done with the new URL — it "
    "worked, so don't click it again; call snapshot to see the new page.")

FILL_DESCRIPTION = (
    "Type a value into a form field, replacing what's in it. " + _ID_NOTE + " To "
    "submit, click the form's button, or press_key \"Enter\" on the field (how "
    "most search boxes submit).")

PRESS_KEY_DESCRIPTION = (
    "Press a key in the browser, e.g. \"Enter\" to submit a search box, or "
    "\"Escape\", \"Tab\", \"ArrowDown\", \"PageDown\". Give an element's \"id\" to "
    "press it on that element (focusing it first); leave it out to press on "
    "whatever has focus. The reply says which page the browser is on afterwards.")

# What a navigation does to an action running in the page it left.
_NAV_ERRORS = ("execution context was destroyed", "navigation",
               "target closed", "frame was detached")

# The element types a terse list keeps ahead of links.
_CONTROL_TYPES = ("input", "textarea", "select", "button")


def check(sw, mcp):
    """The shadow-web internals this module steers, or a list of what moved."""
    problems = []
    for name in ("_format_mcp_response", "_get_shadow_page"):
        if not callable(getattr(sw, name, None)):
            problems.append(f"shadow_web.mcp.server.{name} is missing")
    manager = getattr(mcp, "_tool_manager", None)
    if not callable(getattr(manager, "get_tool", None)):
        problems.append("FastMCP._tool_manager.get_tool is missing")
    for name in ("navigate", "snapshot", "click", "fill", "shadow_query"):
        if manager is not None and callable(getattr(manager, "get_tool", None)) \
                and manager.get_tool(name) is None:
            problems.append(f"shadow-web no longer has a {name} tool")
    return problems


def plain_sid(sid):
    """`sw-12` -> `12`. The element list shows both; click and fill only
    accept the plain id, and the other one failed after a timeout."""
    sid = str(sid or "").strip()
    return sid[3:] if sid.startswith("sw-") else sid


def _navigated(error):
    text = str(error).lower()
    return any(marker in text for marker in _NAV_ERRORS)


def prioritised(actions, limit=TERSE_LIMIT):
    """A terse list: form controls and buttons in page order, then links and
    the rest, up to `limit`. Hidden inputs never earn a place."""
    def kind(a):
        return str(a.get("type") or "").lower()
    controls = [a for a in actions
                if kind(a).startswith(_CONTROL_TYPES) and "hidden" not in kind(a)]
    seen = {id(a) for a in controls}
    rest = [a for a in actions if id(a) not in seen and "hidden" not in kind(a)]
    return (controls + rest)[:limit]


def _focus_script():
    """shadow-web's own interaction script with a "focus" action added.

    Elements are found by the DOM path shadow-web recorded when it listed them
    (the data-sid attribute is often re-rendered away by then), so focusing one
    goes through the same path lookup click and fill use. None if the script
    has changed shape and the addition didn't take."""
    try:
        from shadow_web.dom_capture import _INTERACT_SCRIPT as script
    except ImportError:
        return None
    anchor = 'if (action === "click") {'
    if anchor not in script:
        return None
    return script.replace(anchor, 'if (action === "focus") { node.focus(); return { ok: true }; }\n  '
                          + anchor, 1)


_FOCUS_SCRIPT = _focus_script()


def install(sw, mcp, log=lambda message: None):
    """Apply everything above to `mcp`, the server sw.create_mcp_server() built."""

    # ── the element list ──
    upstream_format = sw._format_mcp_response

    def _format_mcp_response(shadow, *args, **kwargs):
        res = upstream_format(shadow, *args, **kwargs)
        if isinstance(res, dict) and "action_map" in res:
            actions = list(getattr(shadow, "action_map", None) or [])
            shown = prioritised(actions)
            res["action_map"] = shown
            if len(actions) > len(shown):
                res["not_shown"] = len(actions) - len(shown)
                res["hint"] = ("More elements than are listed here (mostly links). "
                               "Find one with shadow_query, e.g. "
                               "\"label~/checkout/i\" or \"type:a\".")
        return res

    sw._format_mcp_response = _format_mcp_response

    # ── descriptions, and the raw-HTML tools ──
    manager = mcp._tool_manager
    for name, text in DESCRIPTIONS.items():
        tool = manager.get_tool(name)
        if tool is not None:
            tool.description = text
    for name in HIDDEN_TOOLS:
        if manager.get_tool(name) is not None:
            mcp.remove_tool(name)

    # ── click, fill and press_key: where the page ended up ──
    def _page():
        page = sw._session.get("page")
        if page is None:
            raise RuntimeError("No browser session. Call navigate(url) first.")
        return page

    async def _where_now(page):
        try:
            # A click or Enter starts its navigation a beat later; asked at
            # once, the load state is still the old page's.
            await asyncio.sleep(0.4)
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:                                    # noqa: BLE001
            pass
        try:
            return {"url": page.url, "title": await page.title()}
        except Exception:                                    # noqa: BLE001
            return {"url": getattr(page, "url", "")}

    async def _acted(tool, page, before, run):
        """Run `run()`, then report where the page is, re-capturing it when the
        action left it on another page so the element list and the outline
        describe the new one."""
        navigated = False
        try:
            await run()
        except Exception as e:                               # noqa: BLE001
            if not _navigated(e):
                raise
            navigated = True
        after = await _where_now(page)
        if navigated or (after.get("url") and after.get("url") != before):
            try:
                await mcp.call_tool("snapshot", {"detail": "minimal"})
            except Exception as e:                           # noqa: BLE001
                log(f"snapshot after {tool} skipped: {type(e).__name__}: {e}")
        out = {"ok": True, "tool": tool, **after}
        if navigated:
            out["note"] = ("That opened another page — the action worked; don't "
                           "repeat it. Call snapshot to see the new page.")
        return out

    async def click(sid: str) -> dict:
        shadow, page, sid = sw._get_shadow_page(), _page(), plain_sid(sid)
        return await _acted("click", page, page.url, lambda: shadow.click(sid))

    async def fill(sid: str, value: str) -> dict:
        shadow, page, sid = sw._get_shadow_page(), _page(), plain_sid(sid)
        return await _acted("fill", page, page.url, lambda: shadow.fill(sid, value))

    async def press_key(key: str, sid: str = "") -> dict:
        page, sid = _page(), plain_sid(sid)

        async def run():
            if sid:
                await _focus(sid)
            await page.keyboard.press(key)

        out = await _acted("press_key", page, page.url, run)
        out["key"] = key
        return out

    async def _focus(sid):
        page = _page()
        shadow = sw._session.get("shadow_page")
        binding = None
        try:
            binding = shadow._get_binding_for_sid(sid) if shadow is not None else None
        except Exception:                                    # noqa: BLE001
            binding = None
        if binding and binding.get("path") and binding.get("source") != "a11y" and _FOCUS_SCRIPT:
            result = await page.evaluate(_FOCUS_SCRIPT, {"path": binding["path"],
                                                         "action": "focus", "value": None})
            if isinstance(result, dict) and result.get("ok"):
                return
        await page.focus(f'[data-sid="{sid}"]', timeout=3000)

    for name, fn, description in (("click", click, CLICK_DESCRIPTION),
                                  ("fill", fill, FILL_DESCRIPTION),
                                  ("press_key", press_key, PRESS_KEY_DESCRIPTION)):
        if manager.get_tool(name) is not None:
            mcp.remove_tool(name)
        mcp.add_tool(fn, name=name, description=description)

