"""Opt-in install telemetry.

Off unless FC_TELEMETRY=1 is set in .env, which only happens if the user says
yes to an explicitly default-no prompt during install. When it is on, FreeClaw
sends exactly one ping — the first time it starts after being enabled — with
four fields and nothing else:

    install_id      random UUID4, generated here and saved back to .env
    version         contents of the VERSION file at the repo root
    os              "linux", "darwin", ...
    install_method  "docker" or "native"

No messages, no prompts, no provider names or URLs, no API keys, no file
paths, no hostname. It answers "how many installs are there" and nothing more.
Users can read the exact payload in the log line below, see their install_id
sitting in .env, and turn the whole thing off from Settings.

The send is fire-and-forget on a daemon thread with a short timeout, and every
failure is swallowed: telemetry must never slow down, block, or break a
start-up. The install id is persisted only *after* a send succeeds, so a
machine that happens to be offline at first start still gets counted later
rather than being silently dropped.
"""

import os
import platform
import threading
import uuid

import requests

from src.logging_setup import get_logger

logger = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(os.path.dirname(BASE_DIR), '.env')
VERSION_PATH = os.path.join(os.path.dirname(BASE_DIR), 'VERSION')

DEFAULT_ENDPOINT = 'https://freeclaw.eedeb.dev/telemetry/install'

# Short enough that a hung or blackholed endpoint can't hold up a restart.
TIMEOUT_SECONDS = 5

_TRUTHY = ('1', 'true', 'yes', 'on')


def _enabled():
    """True only on an explicit opt-in. Anything else — unset, empty, '0',
    garbage — means off, so a malformed value can never turn it on."""
    return os.getenv('FC_TELEMETRY', '').strip().lower() in _TRUTHY


def _version():
    try:
        with open(VERSION_PATH, 'r', encoding='utf-8') as f:
            return f.read().strip() or 'unknown'
    except OSError:
        return 'unknown'


def _install_method():
    """Docker writes /.dockerenv into every container it builds."""
    return 'docker' if os.path.exists('/.dockerenv') else 'native'


def _persist_install_id(install_id):
    """Append FC_INSTALL_ID to .env, leaving every other line untouched.

    Deliberately append-only and no-op if the key already exists: this runs on
    a background thread and must not race the settings UI's own .env rewrite
    into clobbering a provider key.
    """
    try:
        existing = ''
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH, 'r', encoding='utf-8') as f:
                existing = f.read()
        if 'FC_INSTALL_ID=' in existing:
            return
        prefix = '' if existing.endswith('\n') or not existing else '\n'
        with open(ENV_PATH, 'a', encoding='utf-8') as f:
            f.write(f'{prefix}FC_INSTALL_ID={install_id}\n')
        os.environ['FC_INSTALL_ID'] = install_id
    except OSError:
        # Worst case we ping once more on the next start; the endpoint
        # dedupes on install_id, so this is not worth failing over.
        logger.debug("Could not persist FC_INSTALL_ID", exc_info=True)


def _send(payload, endpoint):
    try:
        requests.post(endpoint, json=payload, timeout=TIMEOUT_SECONDS).raise_for_status()
    except Exception:
        # Offline, DNS down, endpoint 500ing — all equally uninteresting.
        # No id is persisted, so the next start retries.
        logger.debug("Install ping failed", exc_info=True)
        return
    _persist_install_id(payload['install_id'])
    logger.info("Sent anonymous install ping (FC_TELEMETRY=1): %s", payload)


def maybe_send_install_ping():
    """Fire the one-time install ping if opted in and not already counted.

    Returns immediately in every case — the network call happens on a daemon
    thread so it can't delay start-up or keep the process alive at shutdown.
    """
    if not _enabled():
        return
    if os.getenv('FC_INSTALL_ID', '').strip():
        return  # already counted

    payload = {
        'install_id': str(uuid.uuid4()),
        'version': _version(),
        'os': platform.system().lower(),
        'install_method': _install_method(),
    }
    endpoint = os.getenv('FC_TELEMETRY_URL', '').strip() or DEFAULT_ENDPOINT

    threading.Thread(
        target=_send,
        args=(payload, endpoint),
        daemon=True,
        name='telemetry-install-ping',
    ).start()
