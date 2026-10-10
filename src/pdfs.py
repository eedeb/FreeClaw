"""Reading PDFs for read_file: the text page by page, and scanned pages as images.

read_file opens a file as UTF-8 text, which a PDF isn't — so before this an
uploaded PDF came back as a decode error and the agent couldn't see a word of
it. agent._run_tool sends a file that starts with %PDF- here instead.

  * **Text** comes from pdfium (pypdfium2), with a "--- Page N of M ---" line
    before each page, up to MAX_CHARS per read. A longer document ends by
    saying where it stopped and which `pages` to ask for next.
  * **A scanned page** — little or no text, and an image on it — is rendered
    and returned as an image on the result (mcp_client.ToolText, the same way a
    browser screenshot reaches the model), at most MAX_IMAGES a read and
    MAX_SIDE pixels on its long side. A model without vision gets the usual
    note that an image couldn't be shown (agent._without_tool_images).
  * **The PDF is opened in a child process**, with a memory ceiling and a time
    limit where the platform has them. pdfium is C, and a malformed or hostile
    file must not be able to take the app — every user's conversation — down
    with it.

A password-protected PDF is reported as such. The agent is told to ask for an
unprotected copy, never for the password: the chat is no place to type one.
"""
import base64
import io
import json
import os
import re
import subprocess
import sys

MAX_CHARS = 40_000          # ~10k tokens a read
MAX_PAGES = 60              # pages looked at in one read
MAX_IMAGES = 4              # scanned pages sent as images in one read
MAX_SIDE = 1600             # px, long side of a rendered page
MIN_TEXT = 25               # fewer characters than this, and an image: a scan
CHILD_MEMORY = 768 * 1024 * 1024
CHILD_SECONDS = 45

_RANGE_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$")


def is_pdf(path):
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def parse_pages(spec):
    """"21-40" → (21, 40), "7" → (7, 7), nothing → (1, None). None for a
    range that can't be read."""
    if spec is None or not str(spec).strip():
        return 1, None
    m = _RANGE_RE.match(str(spec))
    if not m:
        return None
    first = max(1, int(m.group(1)))
    last = int(m.group(2)) if m.group(2) else first
    return first, max(first, last)


# ── in the child process ────────────────────────────────────────

def extract(path, first, last):
    """What the child prints: the page count, the pages read (number, text,
    whether it's a scan, its image as a data URL), and where it stopped."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c
    try:
        pdf = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as e:
        if "password" in str(e).lower():
            return {"error": "password"}
        return {"error": f"unreadable: {e}"}
    total = len(pdf)
    last = min(total, last or total, first + MAX_PAGES - 1)
    out, chars, images, stopped = [], 0, 0, None
    for n in range(first, last + 1):
        page = pdf[n - 1]
        text = page.get_textpage().get_text_range().replace("\r\n", "\n").strip()
        # A scan is a page that is a picture: little or no text, and an image
        # on it. A short page of real text, or a blank one, isn't.
        scanned = len(text) < MIN_TEXT and any(
            True for _ in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=2))
        image = None
        if scanned and images < MAX_IMAGES:
            w, h = page.get_size()
            scale = min(2.0, MAX_SIDE / max(w, h, 1))
            pil = page.render(scale=scale).to_pil().convert("RGB")
            buf = io.BytesIO()
            pil.save(buf, "JPEG", quality=80)
            image = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
            images += 1
        if out and chars + len(text) > MAX_CHARS:
            stopped = n
            break
        out.append({"n": n, "text": text[:MAX_CHARS], "scanned": scanned, "image": image})
        chars += len(text)
    if stopped is None and last < total:
        stopped = last + 1
    return {"total": total, "pages": out, "next": stopped}


def _limit_child():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (CHILD_MEMORY, CHILD_MEMORY))
    except (ImportError, ValueError, OSError):
        pass    # no ceiling on this platform; the time limit still holds


def _run_child(path, first, last):
    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__), path, str(first), str(last or 0)],
        capture_output=True, timeout=CHILD_SECONDS,
        preexec_fn=_limit_child if os.name == "posix" else None)
    if proc.returncode != 0:
        if b"No module named 'pypdfium2'" in proc.stderr:
            return {"error": "missing"}
        return {"error": "crashed"}
    return json.loads(proc.stdout or b"{}")


# ── in the app ──────────────────────────────────────────────────

def _render(name, data):
    """read_file's answer for one PDF, as (text, [image data URLs])."""
    err = data.get("error")
    if err == "password":
        return (f"{name} is password-protected, so it can't be opened. Ask them for a copy "
                "without the password — never to type the password into the chat."), []
    if err == "missing":
        return ("PDFs can't be read on this install yet: it needs the pypdfium2 package. "
                "Tell the user to run ./update.sh."), []
    if err:
        return f"{name} couldn't be read as a PDF ({err}).", []
    total, pages = data["total"], data["pages"]
    if not pages:
        return f"{name} has {total} page(s); there is no page in the range asked for.", []
    parts, images = [f"{name} — PDF, {total} page(s)."], []
    for p in pages:
        parts.append(f"\n--- Page {p['n']} of {total} ---")
        if p["image"]:
            images.append(p["image"])
            parts.append("(No text layer — a scanned page. Its image is attached; read it from that."
                         + (f" Text found: {p['text']}" if p["text"] else "") + ")")
        elif p["scanned"]:
            parts.append(f"(A scanned page, past this read's image limit — read_file with "
                         f"pages=\"{p['n']}\" to see it.)")
        elif not p["text"]:
            parts.append("(blank page)")
        else:
            parts.append(p["text"])
    if data.get("next"):
        parts.append(f"\n[Stopped before page {data['next']} of {total}. For more, read_file "
                     f"\"{name}\" with pages=\"{data['next']}-{min(total, data['next'] + 19)}\".]")
    return "\n".join(parts), images


def read(path, name, pages=None):
    """read_file for the PDF at `path` (shown to the model as `name`)."""
    # Imported here, not at the top: the child runs this file as a script,
    # where the src package isn't on the path.
    from src.logging_setup import get_logger
    from src.mcp_client import ToolText
    logger = get_logger("src.pdfs")
    span = parse_pages(pages)
    if span is None:
        return f"Error: pages should look like \"5\" or \"21-40\", not {pages!r}."
    try:
        data = _run_child(path, *span)
    except subprocess.TimeoutExpired:
        data = {"error": "it took too long to open"}
    except Exception as e:                               # noqa: BLE001 — answered, not raised
        logger.exception("Couldn't read PDF %r", name)
        data = {"error": type(e).__name__}
    text, images = _render(name, data)
    # Counts only: what the document says is the user's.
    logger.info("Read a PDF: %s page(s), %d read, %d as images%s", data.get("total", "?"),
                len(data.get("pages") or []), len(images),
                f", error {data['error']}" if data.get("error") else "")
    return ToolText(text, images)


if __name__ == "__main__":
    _path, _first, _last = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]) or None
    sys.stdout.write(json.dumps(extract(_path, _first, _last)))
