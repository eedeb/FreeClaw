"""The Developer tools' `check`: a page from the workspace in a headless
browser, and a short report of what went wrong (src/dev_server.py).

The agent writes a web page or a game and, without this, hands over the link
having never seen it run: a ReferenceError on load, a player that falls
through the floor, a 404 for a script — the person finds out, not the agent.
`check` loads it, optionally presses keys, clicks or waits, optionally
evaluates a JS expression (the game's state, say), and returns uncaught
errors (with the file, line and the code around the column, since generated
pages are often one long line), console output, failed requests, the title
and the start of the visible text. A screenshot only when asked: it costs far
more than the text.

**The page's own origin.** Files are served straight from the folder by
request interception at https://app.check/ — no server, no port. The page
can only reach that origin and the public web: requests to localhost or a
private address are refused, so a page can't poke at services on this
machine. Hosting replaces `launch_options` to go out through the account's
proxy instead.

Playwright is imported only when a check runs.
"""
import ipaddress
import json
import mimetypes
import os
import re
import subprocess
import time
import urllib.parse

ORIGIN = "https://app.check"
WAIT_DEFAULT_MS = 1500
WAIT_MAX_MS = 10_000
STEPS_MAX = 30
STEPS_MAX_MS = 20_000           # all the steps' holds and waits together
VIEWPORT = {"width": 960, "height": 640}
CONSOLE_KEEP = 15               # distinct console lines kept
LINE_CHARS = 240
EVAL_CHARS = 2000
TEXT_CHARS = 300
SNIPPET = 60                    # characters either side of an error's column

STEPS_HELP = ('"press Space", "hold ArrowRight 800" (ms), "click 120 300" (x y), '
              '"click Start" (text), "type hello", "wait 500"')


class CheckError(Exception):
    pass


def launch_options():
    """chromium.launch() arguments. Hosting adds its sandbox and proxy."""
    return {"headless": True}


def _private(host):
    host = (host or "").strip("[]").lower()
    if host in ("localhost", "") or host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not ip.is_global


def _serve(folder, url):
    """(status, body, type) of a file in `folder` for a URL on ORIGIN."""
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path).lstrip("/")
    if not path or path.endswith("/"):
        path += "index.html"
    parts = [p for p in re.split(r"[\\/]", path) if p not in ("", ".")]
    if ".." in parts:
        return 403, b"", "text/plain"
    full = os.path.realpath(os.path.join(folder, *parts))
    if not (full == folder or full.startswith(folder.rstrip(os.sep) + os.sep)) or not os.path.isfile(full):
        return 404, b"Not found", "text/plain"
    with open(full, "rb") as f:
        body = f.read()
    kind = mimetypes.guess_type(full)[0] or "application/octet-stream"
    if kind.startswith("text/") or kind in ("application/javascript", "application/json"):
        kind += "; charset=utf-8"
    return 200, body, kind


def _cut(text, n=LINE_CHARS):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n] + "…"


def _where(stack, folder, root_rel):
    """'path:line:col — near: `code`' for the first frame of `stack` that's
    one of the folder's files, or ''."""
    for m in re.finditer(re.escape(ORIGIN) + r"/([^\s:()]*):(\d+):(\d+)", stack or ""):
        rel, line, col = urllib.parse.unquote(m.group(1)) or "index.html", int(m.group(2)), int(m.group(3))
        shown = (root_rel.rstrip("/") + "/" + rel) if root_rel not in ("", ".") else rel
        out = f"{shown}:{line}:{col}"
        try:
            with open(os.path.join(folder, rel), encoding="utf-8", errors="replace") as f:
                text = f.read().splitlines()
            code = text[line - 1] if 0 < line <= len(text) else ""
        except OSError:
            code = ""
        if code:
            a = max(0, col - 1 - SNIPPET)
            near = code[a:col - 1 + SNIPPET].strip()
            out += f" — near: `{near}`"
        return out
    return ""


def _step(page, step):
    words = str(step).split()
    if not words:
        return 0
    verb, rest = words[0].lower(), words[1:]
    try:
        if verb == "press" and rest:
            page.keyboard.press(rest[0])
        elif verb == "hold" and rest:
            ms = int(rest[1]) if len(rest) > 1 else 500
            page.keyboard.down(rest[0])
            page.wait_for_timeout(ms)
            page.keyboard.up(rest[0])
            return ms
        elif verb == "click" and len(rest) == 2 and all(re.fullmatch(r"-?\d+(\.\d+)?", w) for w in rest):
            page.mouse.click(float(rest[0]), float(rest[1]))
        elif verb == "click" and rest:
            label = str(step).split(None, 1)[1]
            try:
                page.get_by_text(label).first.click(timeout=3000)
            except Exception:                            # noqa: BLE001 — playwright's timeout
                raise CheckError(f"Step {step!r}: nothing visible says {label!r}.")
        elif verb == "type" and rest:
            page.keyboard.type(str(step).split(None, 1)[1])
        elif verb == "wait" and rest:
            ms = int(rest[0])
            page.wait_for_timeout(ms)
            return ms
        else:
            raise CheckError(f"Unknown step {step!r}. Steps look like {STEPS_HELP}.")
    except ValueError:
        raise CheckError(f"Bad number in step {step!r}.")
    return 0


SIZE_MAX = 4096                 # pixels either way, for a saved image


def check(folder, page_rel, root_rel, wait_ms=WAIT_DEFAULT_MS, steps=(), expression=None,
          screenshot=False, size=None, save=None):
    """Load `page_rel` (inside `folder`, the site's root), run `steps`, and
    return (report text, png bytes or None).

    `size` (width, height) sets the viewport. `save`, an absolute path ending
    .png, .jpg or .jpeg, writes the viewport there as an image at exactly that
    size — a page drawn as a picture (a post, a card, a diagram) — and only a
    line about it comes back, not the image."""
    from playwright.sync_api import sync_playwright, Error as PlaywrightError

    if len(steps) > STEPS_MAX:
        raise CheckError(f"At most {STEPS_MAX} steps.")
    folder = os.path.realpath(folder)
    errors, console, failed, counts, refused = [], [], [], {}, set()

    def note_console(msg):
        kind = msg.type
        if kind not in ("log", "info", "warning", "error", "debug", "trace", "assert"):
            return
        if msg.text.startswith("Failed to load resource"):
            return                                       # it's under Failed requests
        line = _cut(msg.text)
        key = (kind, line)
        if key in counts:
            counts[key] += 1
            return
        counts[key] = 1
        if len(console) < CONSOLE_KEEP:
            console.append(key)

    def note_error(err):
        stack = getattr(err, "stack", "") or ""
        message = getattr(err, "message", "") or str(err)
        name = getattr(err, "name", "") or "Error"
        where = _where(stack, folder, root_rel)
        text = _cut(f"{name}: {message}" if not message.startswith(name) else message)
        entry = text + (f"\n    at {where}" if where else "")
        if entry not in errors and len(errors) < 10:
            errors.append(entry)

    def route(r):
        url = r.request.url
        if url.startswith(ORIGIN + "/") or url == ORIGIN:
            status, body, kind = _serve(folder, url)
            if status != 200:
                failed.append(f"{status} {url[len(ORIGIN):]}")
            return r.fulfill(status=status, body=body, content_type=kind)
        host = urllib.parse.urlsplit(url).hostname
        if url.startswith(("data:", "blob:")):
            return r.continue_()
        if _private(host) or not url.startswith(("http://", "https://")):
            refused.add(url)
            failed.append(f"blocked {_cut(url, 120)} (only the page's own files and the public web)")
            return r.abort()
        return r.continue_()

    started = time.monotonic()
    shot = None
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(**launch_options())
        except PlaywrightError as e:
            raise CheckError(f"Couldn't start the browser: {_cut(str(e).splitlines()[0], 160)}")
        try:
            viewport = {"width": size[0], "height": size[1]} if size else VIEWPORT
            context = browser.new_context(viewport=viewport, device_scale_factor=1,
                                          service_workers="block", accept_downloads=False)
            page = context.new_page()
            page.on("console", note_console)
            page.on("pageerror", note_error)
            page.on("requestfailed", lambda req: None if req.url.startswith(ORIGIN) or req.url in refused else
                    failed.append(f"{_cut(req.url, 120)} ({req.failure or 'failed'})"))
            page.on("dialog", lambda d: (console.append(("dialog", _cut(f"{d.type}: {d.message}"))), d.dismiss()))
            context.route("**/*", route)
            url = ORIGIN + "/" + urllib.parse.quote(page_rel)
            try:
                response = page.goto(url, wait_until="load", timeout=15_000)
            except PlaywrightError as e:
                raise CheckError(f"The page didn't load: {_cut(str(e).splitlines()[0])}")
            if response is not None and response.status != 200:
                raise CheckError(f"{page_rel}: {response.status}.")
            page.wait_for_timeout(wait_ms)
            try:
                page.mouse.move(VIEWPORT["width"] / 2, VIEWPORT["height"] / 2)
            except PlaywrightError:
                pass
            spent = 0
            for step in steps:
                spent += _step(page, step)
                if spent > STEPS_MAX_MS:
                    raise CheckError(f"Steps hold and wait {STEPS_MAX_MS // 1000}s at most in all.")
            if steps:
                page.wait_for_timeout(200)
            value = None
            if expression:
                try:
                    value = page.evaluate(
                        "(src) => { const v = (0, eval)(src); "
                        "try { return typeof v === 'string' ? v : JSON.stringify(v); } "
                        "catch (e) { return String(v); } }", expression)
                    value = "undefined" if value is None else value
                except PlaywrightError as e:
                    value = "threw " + _cut(re.sub(r"^\w+\.evaluate: ", "", str(e).splitlines()[0]))
            title = page.title()
            try:
                text = page.inner_text("body", timeout=2000)
            except PlaywrightError:
                text = ""
            if screenshot:
                shot = page.screenshot(type="png")
            if save:
                jpeg = save.lower().endswith((".jpg", ".jpeg"))
                os.makedirs(os.path.dirname(save), exist_ok=True)
                page.screenshot(path=save, type="jpeg" if jpeg else "png",
                                **({"quality": 92} if jpeg else {}))
        finally:
            browser.close()

    seconds = time.monotonic() - started
    head = f"Checked {(root_rel.rstrip('/') + '/' if root_rel not in ('', '.') else '')}{page_rel}"
    head += f" ({seconds:.1f}s"
    if steps:
        head += f", {len(steps)} step{'s' if len(steps) != 1 else ''}"
    head += ")"
    head += f": {len(errors)} error{'s' if len(errors) != 1 else ''}" if errors else ": no errors"
    out = [head]
    if errors:
        out.append("Errors:")
        out += ["  " + e for e in errors]
    if console:
        out.append("Console:")
        for kind, line in console:
            n = counts.get((kind, line), 1)
            out.append(f"  [{kind}] {line}" + (f" (×{n})" if n > 1 else ""))
        hidden = sum(counts.values()) - sum(counts.get(k, 1) for k in console)
        if hidden > 0:
            out.append(f"  [… {hidden} more lines]")
    if failed:
        out.append("Failed requests:")
        out += ["  " + f for f in dict.fromkeys(failed)][:10]
    out.append(f"Title: {_cut(title, 120) or '(none)'}")
    visible = _cut(text, TEXT_CHARS)
    out.append(f"Text: {visible}" if visible else "Text: (nothing visible; a canvas, or blank)")
    if save:
        w, h = (size or (VIEWPORT["width"], VIEWPORT["height"]))
        out.append(f"Saved {os.path.basename(save)} ({w}×{h}, {max(1, os.path.getsize(save) // 1024)} KB).")
    if expression:
        value = str(value)
        if len(value) > EVAL_CHARS:
            value = value[:EVAL_CHARS] + f" [… {len(value) - EVAL_CHARS} chars]"
        out.append(f"{_cut(expression, 80)} → {value}")
    return "\n".join(out), shot


# ── syntax, on write and edit ────────────────────────────────

_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)


def _node_check(source, suffix):
    """Node's parse error for `source` as a line, or None (also when there's
    no node). Parses only: `node --check` never runs the code."""
    import shutil
    import tempfile
    node = shutil.which("node")
    if not node:
        return None
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(source)
        proc = subprocess.run([node, "--check", tmp], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if proc.returncode == 0:
        return None
    err = proc.stderr
    m = re.search(re.escape(tmp) + r":(\d+)", err)
    msg = next((l for l in err.splitlines() if re.match(r"^\w*Error\b", l)), "SyntaxError")
    return f"{msg} at line {m.group(1)}" if m else msg


def syntax(path, text):
    """A one-line syntax problem in `text` (the file at `path`), or None."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".py":
            try:
                compile(text, path, "exec", dont_inherit=True)
            except SyntaxError as e:
                return f"SyntaxError: {e.msg} at line {e.lineno}"
        elif ext == ".json":
            try:
                json.loads(text)
            except ValueError as e:
                return f"Invalid JSON: {e}"
        elif ext in (".js", ".mjs", ".cjs"):
            return _node_check(text, ext)
        elif ext in (".html", ".htm"):
            for m in _SCRIPT.finditer(text):
                attrs, body = m.group(1).lower(), m.group(2)
                if "src=" in attrs or not body.strip():
                    continue
                kind = re.search(r"type\s*=\s*[\"']?([\w/+-]+)", attrs)
                kind = kind.group(1) if kind else ""
                if kind not in ("", "module", "text/javascript", "application/javascript"):
                    continue
                # Padded so node's line numbers are the page's.
                pad = "\n" * text[:m.start(2)].count("\n")
                problem = _node_check(pad + body, ".mjs" if kind == "module" else ".js")
                if problem:
                    return problem
    except Exception:                                    # noqa: BLE001 — a hint, never a failure
        return None
    return None
