"""Central logging for FreeClaw.

Every module that wants full error detail (exception type, message, and
traceback) — not just the short string that gets shown to a user or fed
back to the model — goes through get_logger() here, so it all lands in one
place: logs/freeclaw.log at the repo root.

That location matters: Flask/static/ is served over HTTP to any logged-in
user via the /static/<path:filename> route, so logs must live outside it —
a traceback can easily contain file paths, stack frames, or other detail
you don't want reachable from the browser.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "logs"))
LOG_FILE = os.path.join(LOG_DIR, "freeclaw.log")

_root = logging.getLogger("freeclaw")


def _configure():
    if _root.handlers:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    _root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s")

    # Full detail, rotated so a busy failure loop can't grow this forever.
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    _root.addHandler(file_handler)

    # Mirror warnings/errors to the console too, so they're visible live
    # under `python main.py` or `journalctl -u FreeClaw.service -f` without
    # having to go tail the file separately.
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(fmt)
    _root.addHandler(console_handler)

    # Don't also propagate to the root logger — avoids double-printing if
    # something else (e.g. Flask/Werkzeug) configures logging.basicConfig.
    _root.propagate = False


def get_logger(name):
    """Return a logger under the shared "freeclaw" namespace that writes to
    logs/freeclaw.log (rotated at 5MB x 5 backups) plus the console for
    warnings and above. Use logger.exception(...) inside an except block
    (or logger.error(..., exc_info=True) outside one) to capture the full
    traceback, regardless of how short the message shown to the user or
    the model ends up being."""
    _configure()
    return _root.getChild(name)
