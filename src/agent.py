import base64
import json
import os
import re
import socket
import subprocess
import time
from datetime import datetime

import Classy
import httpx
from dotenv import dotenv_values, load_dotenv
from json_repair import repair_json
from openai import OpenAI, APIConnectionError

import src.approvals as approvals
import src.mcp_client as mcp_client
import src.scraper as scraper
from src.logging_setup import get_logger

load_dotenv()

logger = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Weights for the local Classy intent classifier.
CLASSIFIER_PATH = BASE_DIR + "/../models/data.pth"

# Root that Flask's /static/<path:filename> route serves from. Each user
# gets their own subfolder under here (set via set_static_dir), so links back
# to a created file need to include that subfolder, not just the filename.
STATIC_ROOT = os.path.normpath(BASE_DIR + '/../Flask/static')

# Folder the agent's file tools operate in — repointed at the active user's
# own files folder via set_static_dir().
static_dir = BASE_DIR + '/../Flask/static/'


def _server_base_url():
    """Public base URL for links to files the agent creates: CUSTOM_DOMAIN if
    set, otherwise this machine's LAN IP on the app's port."""
    custom_domain = os.getenv("CUSTOM_DOMAIN")
    if custom_domain:
        return custom_domain
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # doesn't actually send data
        ip = s.getsockname()[0]
    finally:
        s.close()
    return 'http://' + ip + ':6767'


url = _server_base_url()


static_token_signer = None


def set_static_token_signer(fn):
    """Registers the function that signs a static-file path into a
    short-lived access token (main.py wires this up at startup, same pattern
    as set_user_creator — agent.py can't import main.py directly since main.py
    already imports agent.py). fn must accept the file's path relative to
    Flask's static root and return a token string.

    Needed because links the agent hands back (e.g. a generated .ics) get
    opened by the client via the OS — Safari/Calendar on iOS, not the app's
    own authenticated session — so they can't rely on the login cookie."""
    global static_token_signer
    static_token_signer = fn


def _static_url(directory, filename):
    """Build the public /static/... URL for `filename`, which was written to
    `directory` — accounting for the per-user subfolder it may point at.
    Includes a signed access token so the link still works when opened
    outside the logged-in session (e.g. handed to Calendar/Safari on a
    phone), without requiring /static to be open to anyone who guesses a
    path."""
    rel = os.path.relpath(os.path.normpath(directory), STATIC_ROOT)
    if rel in ('.', '') or rel.startswith('..'):
        rel = ''
    else:
        rel = rel.replace(os.sep, '/') + '/'
    rel_path = rel + filename
    link = url + "/static/" + rel_path
    if static_token_signer is not None:
        link += "?token=" + static_token_signer(rel_path)
    return link


agent_messages = []
tools = []

# LLM providers are user-defined in Settings → Providers and persist in .env
# as five parallel JSON lists — the same storage shape MCP servers use (see
# src/mcp_client.py) — read fresh on every call so an add/remove/toggle takes
# effect without a restart. There is no built-in fallback: an empty list
# means the agent has nothing to call, which is reported to the user plainly
# (see _user_facing_error) rather than silently degrading to a default.
#
# Worth checking before trusting a new provider: reasoning models (qwen3.5,
# Gemini 3.x, NVIDIA Nemotron) tend to leak their thinking into plain content
# or misbehave on tool calls unless they have an explicit thinking
# off-switch, and Gemini 3.x 400s multi-turn tool calls through its
# OpenAI-compatible endpoint ("Function call is missing a thought_signature").
_ENV_PATH = os.path.join(os.path.dirname(BASE_DIR), ".env")
_PROVIDER_NAMES_KEY = "PROVIDER_NAMES"
_PROVIDER_URLS_KEY = "PROVIDER_URLS"
_PROVIDER_KEYS_KEY = "PROVIDER_KEYS"
_PROVIDER_MODELS_KEY = "PROVIDER_MODELS"
_PROVIDER_ENABLED_KEY = "PROVIDER_ENABLED"

# Which configured provider (by name) get_image_description uses — chosen in
# Settings → Vision Model, stored as a single scalar env var (unlike the
# parallel-list providers above, since there's only ever one selection).
_VISION_PROVIDER_KEY = "VISION_PROVIDER"


def _parse_env_list(raw):
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return val if isinstance(val, list) else []


# Models that need an explicit cache_control breakpoint to cache anything.
# Everyone else (OpenAI, DeepSeek, Groq, Cerebras, xAI) caches a repeated
# prefix on their own and needs no request-side opt-in — only a stable prefix,
# which _VOLATILE_HEADER provides. Matched on the model id rather than asked
# of the user: the models that need it are the ones whose names say so, and a
# wrong guess is self-correcting (see the bad_request retry in
# _create_completion). A provider behind an opaque model id just misses out.
_WANTS_CACHE_BREAKPOINT = re.compile(r"claude|anthropic|gemini|qwen", re.I)


def read_providers():
    """Return the user-defined providers as a list of
    {"name","url","key","model","enabled"} dicts, read fresh from .env on
    every call so runtime edits are picked up without a restart. Empty when
    the user hasn't configured any — see _active_providers, which has no
    fallback for that case."""
    if not os.path.exists(_ENV_PATH):
        return []
    env = dotenv_values(_ENV_PATH)
    names = _parse_env_list(env.get(_PROVIDER_NAMES_KEY))
    urls = _parse_env_list(env.get(_PROVIDER_URLS_KEY))
    keys = _parse_env_list(env.get(_PROVIDER_KEYS_KEY))
    models = _parse_env_list(env.get(_PROVIDER_MODELS_KEY))
    enabled = _parse_env_list(env.get(_PROVIDER_ENABLED_KEY))
    out = []
    for i, url in enumerate(urls):
        if not url:
            continue
        out.append({
            "name": names[i] if i < len(names) and names[i] else f"provider{i + 1}",
            "url": url,
            "key": keys[i] if i < len(keys) else "",
            "model": models[i] if i < len(models) else "",
            "enabled": bool(enabled[i]) if i < len(enabled) else True,
        })
    return out


def providers_to_env(providers):
    """Turn a list of provider dicts into the {ENV_KEY: value} mapping to
    persist. Values are single-quote-wrapped JSON so brackets and the inner
    double quotes survive python-dotenv untouched (callers must reject
    single quotes / newlines in the fields, same as MCP does)."""
    return {
        _PROVIDER_NAMES_KEY: "'" + json.dumps([p.get("name", "") for p in providers]) + "'",
        _PROVIDER_URLS_KEY: "'" + json.dumps([p.get("url", "") for p in providers]) + "'",
        _PROVIDER_KEYS_KEY: "'" + json.dumps([p.get("key", "") for p in providers]) + "'",
        _PROVIDER_MODELS_KEY: "'" + json.dumps([p.get("model", "") for p in providers]) + "'",
        _PROVIDER_ENABLED_KEY: "'" + json.dumps([bool(p.get("enabled", True)) for p in providers]) + "'",
    }


def read_vision_provider():
    """Name of the provider selected in Settings → Vision Model, or None if
    unset — read fresh from .env on every call, same as read_providers()."""
    if not os.path.exists(_ENV_PATH):
        return None
    return dotenv_values(_ENV_PATH).get(_VISION_PROVIDER_KEY) or None


def _active_providers():
    """The provider chain _create_completion actually tries, in order.
    Each item is (name, base_url, api_key, model_override, extra_body,
    cache_mode).

    Purely the enabled entries from Settings → Providers, in the order the
    user listed them — no built-in fallback. Empty if the user hasn't
    configured any yet. A provider with a blank model sends no override
    (the endpoint's own default model is used)."""
    user = [p for p in read_providers() if p.get("enabled", True) and p.get("url")]
    return [(p["name"], p["url"], p.get("key", ""), (p.get("model") or None), None)
            for p in user]

# Short timeouts + no SDK-level retries so a dead provider fails in seconds
# (not the SDK's 600s default compounded by exponential-backoff retries) and
# the next provider in the chain actually gets tried. A slow self-hosted or
# reasoning-model endpoint may need this raised.
_PROVIDER_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

# One OpenAI client per provider, built once and reused so switching
# providers doesn't pay a fresh TCP/TLS handshake every time.
_provider_clients = {}

# The provider that answered last — tracked only so _create_completion can
# log when a call actually switches providers. Try order itself always
# follows _active_providers()' order (first entry tried first, every call),
# not whichever provider happened to work last.
_last_provider = None


def _client_for(name, key, base_url):
    # Cache keyed on (name, key) so a key rotated at runtime (e.g. via
    # /api/settings) gets a fresh client instead of reusing one built with
    # the old (or missing) key.
    cached = _provider_clients.get(name)
    if cached is None or cached[0] != key:
        _provider_clients[name] = (key, OpenAI(
            api_key=key, base_url=base_url,
            timeout=_PROVIDER_TIMEOUT, max_retries=0,
        ))
    return _provider_clients[name][1]


def _classify_error(e):
    """Best-effort classification of a provider failure, used to build an
    accurate message if every provider in the chain fails."""
    status = getattr(e, "status_code", None)
    text = str(e).lower()
    # Both spellings: providers write this as prose ("Rate limit reached") and
    # as an error code ("rate_limit_exceeded"), and Groq answers a request
    # bigger than the whole per-minute budget with 413 + rate_limit_exceeded —
    # a rate limit wearing a 4xx that would otherwise read as "bad request".
    if (status == 429 or "rate limit" in text or "rate_limit" in text
            or "quota" in text):
        return "rate_limited"
    if status in (401, 403) or "invalid api key" in text or "unauthorized" in text:
        return "auth_error"
    # Other 4xx: the provider understood us and said the request itself is
    # bad (e.g. a malformed conversation history), not the network.
    if status in (400, 404, 413, 422):
        return "bad_request"
    # The openai SDK wraps raw httpx timeout/connect errors in
    # APIConnectionError before they reach us — check both to be safe.
    if isinstance(e, (APIConnectionError, httpx.TimeoutException, httpx.ConnectError)):
        return "network_error"
    if status and status >= 500:
        return "provider_error"
    return "unknown"


# Wording that marks a failure as "this one request is too big", as opposed to
# "you've used up this minute's budget". The distinction matters: the first is
# not fixed by waiting, only by a smaller conversation.
_OVERSIZE_MARKERS = ("too large", "context length", "context_length_exceeded",
                     "maximum context", "reduce your message size")

_DURATION_RE = re.compile(r"^\s*([0-9.]+)\s*(ms|s|m)?\s*$", re.I)
_RETRY_AFTER_RE = re.compile(r"try again in\s+([0-9.]+\s*(?:ms|s|m)?)", re.I)

# Never sideline a provider for longer than this, however long it asked for —
# a misparsed or absurd delay must not cost us a provider for the session.
_MAX_COOLDOWN = 300.0
# Applied when a provider says it's rate-limited but doesn't say for how long
# (Cerebras, among others). TPM windows are a minute, so this backs off enough
# to stop the hammering without sitting out a whole window.
_DEFAULT_COOLDOWN = 20.0

# What the providers' own errors have told us about their limits.
#   _provider_cooldown[name]      — monotonic clock deadline to wait until
#   _provider_size_ceiling[name]  — payload size (see _payload_size) this
#                                   provider already refused outright
# Both are cleared the moment the provider answers successfully, and by
# forget_provider_capabilities() when the provider list is edited.
_provider_cooldown = {}
_provider_size_ceiling = {}


def _parse_duration(raw):
    """Seconds from a duration as providers write them — "41", "40.8525s",
    "1500ms", "2m" — or None if it isn't one."""
    if raw is None:
        return None
    m = _DURATION_RE.match(str(raw))
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "s").lower()
    if unit == "ms":
        return value / 1000
    return value * 60 if unit == "m" else value


def _retry_after_seconds(e):
    """How long the provider asked us to wait, or None if it didn't say.
    Prefers the Retry-After header and falls back to the wording of the error,
    since several OpenAI-compatible endpoints state the delay only in prose
    ("Please try again in 40.8525s")."""
    headers = getattr(getattr(e, "response", None), "headers", None)
    if headers:
        for header in ("retry-after", "x-ratelimit-reset-tokens",
                       "x-ratelimit-reset-requests"):
            seconds = _parse_duration(headers.get(header))
            if seconds is not None:
                return seconds
    m = _RETRY_AFTER_RE.search(str(e))
    return _parse_duration(m.group(1)) if m else None


def _is_oversized_error(e):
    """True when the provider is saying this single request can't fit, whatever
    we do about timing — 413, or any of the usual phrasings."""
    if getattr(e, "status_code", None) == 413:
        return True
    text = str(e).lower()
    return any(marker in text for marker in _OVERSIZE_MARKERS)


def _payload_size(call_kwargs):
    """Cheap proxy for how big a request is. Characters, not tokens: the
    absolute figure is never needed, only whether a request is smaller than one
    a provider has already refused, and character count answers that without
    guessing at somebody else's tokenizer."""
    try:
        return (len(json.dumps(call_kwargs.get("messages") or [], default=str))
                + len(json.dumps(call_kwargs.get("tools") or [], default=str)))
    except (TypeError, ValueError):
        return 0


def _cooldown_reason(name, call_kwargs):
    """Why this provider shouldn't be tried right now, or None to go ahead.
    Expired cooldowns and ceilings a shrunken conversation has dropped back
    under are forgotten here, so a provider is always given another chance
    rather than being written off for the session."""
    until = _provider_cooldown.get(name)
    if until is not None:
        remaining = until - time.monotonic()
        if remaining > 0:
            return f"rate-limited, {remaining:.0f}s left of the delay it asked for"
        del _provider_cooldown[name]
    ceiling = _provider_size_ceiling.get(name)
    if ceiling is not None:
        if _payload_size(call_kwargs) >= ceiling:
            return "already refused a request this size as too large"
        del _provider_size_ceiling[name]
    return None


def _note_provider_limit(name, e, call_kwargs):
    """Record what a provider just told us about its own limits, so the next
    call can skip it instead of re-sending something it has already refused."""
    if _is_oversized_error(e):
        _provider_size_ceiling[name] = _payload_size(call_kwargs)
        logger.info("Provider '%s' refused a request of this size — skipping it "
                    "until the conversation shrinks", name)
        return
    if _classify_error(e) == "rate_limited":
        delay = min(_retry_after_seconds(e) or _DEFAULT_COOLDOWN, _MAX_COOLDOWN)
        _provider_cooldown[name] = time.monotonic() + delay
        logger.info("Provider '%s' rate-limited — not retrying it for %.0fs", name, delay)


class AllProvidersFailedError(RuntimeError):
    """Raised when every configured LLM provider failed for one call.
    Carries the per-provider (name, reason, detail) failures so callers can
    build a message that reflects what actually went wrong instead of a
    generic string."""

    def __init__(self, failures):
        self.failures = failures
        super().__init__(
            "All providers failed: " + "; ".join(f"{n}: {d}" for n, _, d in failures)
        )


def _cache_breakpoint_messages(messages):
    """`messages` with the system message split at _VOLATILE_HEADER into two
    content blocks: the stable instructions marked `cache_control: ephemeral`,
    and the live tail left unmarked. A provider honouring this caches
    everything up to the marker and re-reads only the short tail.

    Returns the input unchanged when there's no useful split — no system
    message, no marker, or nothing stable above it (a breakpoint over text that
    changes every turn could never be reused). Builds a new list, so
    agent_messages keeps its plain string content and nothing is persisted."""
    if not messages or messages[0].get("role") != "system":
        return messages
    head = messages[0]
    content = head.get("content")
    if not isinstance(content, str):
        return messages  # already blocks, or an unexpected shape — leave it
    stable, sep, volatile = content.partition(_VOLATILE_HEADER)
    if not sep or not stable.strip():
        return messages
    return [{**head, "content": [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _VOLATILE_HEADER + volatile},
    ]}, *messages[1:]]


# Keys we hang on our own message dicts for the UI's benefit and that no
# provider should ever see. Stripped in _create_completion.
_INTERNAL_MESSAGE_KEYS = ("provider", "usage")

# Optional request extras that most OpenAI-compatible endpoints accept and some
# reject outright:
#   "usage" — stream_options.include_usage, so a streamed response reports its
#             token counts (it carries none otherwise).
#   "cache" — a cache_control breakpoint on the system message, for the models
#             that need one (_WANTS_CACHE_BREAKPOINT).
# Both are sent optimistically. A provider that 400s with them on is retried
# once without, and then never asked again — so an endpoint that doesn't
# understand them costs one wasted request ever, nothing needs configuring, and
# no working call is lost to a field the user didn't know about.
_unsupported_extras = set()   # {provider_name, ...} — see forget_provider_capabilities


def forget_provider_capabilities():
    """Forget what we've learned about providers' quirks and limits. Called
    when the provider list is edited, so re-adding a name doesn't carry over a
    verdict reached against whatever endpoint used to hold it."""
    _unsupported_extras.clear()
    _provider_cooldown.clear()
    _provider_size_ceiling.clear()


def _apply_optional_extras(call_kwargs, model):
    """Add the optional extras this request can use, or return it unchanged if
    none apply. Returns (kwargs, applied) so the caller knows whether there's
    anything to retry without."""
    applied = []
    out = call_kwargs
    if call_kwargs.get("stream"):
        out = {**out, "stream_options": {"include_usage": True}}
        applied.append("usage")
    if model and _WANTS_CACHE_BREAKPOINT.search(str(model)) and "messages" in out:
        blocked = _cache_breakpoint_messages(out["messages"])
        if blocked is not out["messages"]:
            out = {**out, "messages": blocked}
            applied.append("cache")
    return out, applied


# Running token totals for the turn in flight. One turn can make several LLM
# requests — every tool round-trip recurses into agent_stream and issues
# another — so per-turn cost is only visible by summing them. Module-level
# because the recursion means locals wouldn't aggregate; safe for the same
# reason agent_messages is, in that agent_lock serialises whole turns.
# `requests` counts every call made, `reported` only those that came back with
# numbers — so "zero tokens" stays distinguishable from "this provider doesn't
# say".
_turn_usage = {}


def _reset_turn_usage():
    global _turn_usage
    _turn_usage = {"prompt_tokens": 0, "completion_tokens": 0,
                   "cached_tokens": 0, "requests": 0, "reported": 0}


_reset_turn_usage()


def get_turn_usage():
    """Token totals for the turn that just ran. Read by the /v1 endpoint to
    fill in its `usage` block — the figures are the providers' own, so they're
    exact wherever the provider reports them and zero where it doesn't."""
    return dict(_turn_usage)


def _num(obj, *names):
    """First of `names` present on `obj` as a number, else None. Tolerates both
    attributes and dict keys, since a provider's usage block may arrive as
    either an SDK model or a plain dict."""
    for n in names:
        val = getattr(obj, n, None)
        if val is None and isinstance(obj, dict):
            val = obj.get(n)
        if isinstance(val, (int, float)):
            return int(val)
    return None


def _usage_summary(usage):
    """Prompt/cached/completion token counts from a provider's usage block, or
    None if it holds nothing useful. The cache-hit figure is nested under
    prompt_tokens_details by OpenAI and OpenRouter, and reported flat by
    Anthropic-flavoured shims — both spellings are checked."""
    if usage is None:
        return None
    details = _num(getattr(usage, "prompt_tokens_details", None)
                   or (usage.get("prompt_tokens_details") if isinstance(usage, dict) else None)
                   or {}, "cached_tokens")
    cached = details if details is not None else _num(usage, "cache_read_input_tokens")
    prompt = _num(usage, "prompt_tokens", "input_tokens")
    completion = _num(usage, "completion_tokens", "output_tokens")
    if prompt is None and completion is None and cached is None:
        return None
    return {"prompt_tokens": prompt or 0, "cached_tokens": cached or 0,
            "completion_tokens": completion or 0}


def _prepare_kwargs(kwargs, model_override, extra_body):
    """The request as this provider needs to receive it, before any of the
    optional extras are layered on."""
    call_kwargs = kwargs if model_override is None else {**kwargs, "model": model_override}
    if extra_body:
        call_kwargs = {**call_kwargs, "extra_body": extra_body}
    # Strip params passed as None (stop, tools, ...). Per OpenAI
    # semantics null means the same as omitting the key, but Google's
    # Gemini shim 400s on any optional field sent as JSON null.
    call_kwargs = {k: v for k, v in call_kwargs.items() if v is not None}
    if "messages" in call_kwargs:
        # Our assistant messages carry non-standard bookkeeping keys (which
        # provider answered, what it reported for token usage) — strip them
        # before they go over the wire; some providers 400 on unrecognized
        # message fields.
        call_kwargs = {**call_kwargs, "messages": [
            {k: v for k, v in m.items() if k not in _INTERNAL_MESSAGE_KEYS}
            for m in call_kwargs["messages"]
        ]}
    return call_kwargs


def _create_completion(**kwargs):
    """Try each configured provider in the order _active_providers() returns
    them — first entry first, every call — and return (response_or_stream,
    provider_name) from the first that works. Raises AllProvidersFailedError
    if none do.

    Providers that told us on a previous call to back off (a retry-after, or a
    refusal to accept a request this size) are skipped without a round-trip
    until that has expired."""
    global _last_provider
    failures = []
    prepared = [
        (name, base_url, key, _prepare_kwargs(kwargs, model_override, extra_body))
        for name, base_url, key, model_override, extra_body in _active_providers()
        if key and key != "None"
    ]

    # Honour what the providers asked for — unless that would sit out every
    # one of them, in which case try anyway rather than fail a turn on the
    # strength of a limit we're only predicting.
    waiting = {name: reason for name, _, _, ck in prepared
               if (reason := _cooldown_reason(name, ck))}
    if prepared and len(waiting) == len(prepared):
        logger.info("Every provider is in cooldown — trying them anyway")
        waiting = {}

    for name, base_url, key, plain_kwargs in prepared:
        if name in waiting:
            logger.info("Skipping provider '%s': %s", name, waiting[name])
            failures.append((name, "rate_limited", waiting[name]))
            continue

        call_kwargs = plain_kwargs
        applied = []
        if name not in _unsupported_extras:
            call_kwargs, applied = _apply_optional_extras(call_kwargs, call_kwargs.get("model"))

        try:
            c = _client_for(name, key, base_url)
            try:
                result = c.chat.completions.create(**call_kwargs)
            except Exception as e:
                # Retry without the optional extras if that's plausibly what it
                # objected to. A provider must never lose a working call over a
                # field we added for our own benefit.
                #
                # "Plausibly" has to exclude size and rate complaints: an
                # oversized request fails identically without the extras, so
                # retrying only doubles the round-trips, and a success there
                # would wrongly convict the provider of not supporting a field
                # it never mentioned.
                if (not applied or _classify_error(e) != "bad_request"
                        or _is_oversized_error(e)):
                    raise
                logger.info("Provider '%s' rejected a request with %s applied — "
                            "retrying without", name, "/".join(applied))
                result = c.chat.completions.create(**plain_kwargs)
                # Only now is it established that the extras were the problem.
                _unsupported_extras.add(name)
                logger.info("Provider '%s' accepted it without %s — not sending "
                            "them again", name, "/".join(applied))
            if name != _last_provider:
                print(f"LLM provider switched: {_last_provider} -> {name}")
            _last_provider = name
            # It answered, so anything we'd recorded about its limits is stale.
            _provider_cooldown.pop(name, None)
            _provider_size_ceiling.pop(name, None)
            return result, name
        except Exception as e:
            reason = _classify_error(e)
            failures.append((name, reason, str(e)))
            _note_provider_limit(name, e, plain_kwargs)
            # Full traceback + request shape (never message content, which
            # may hold user data) — the short strings above are all that
            # ever reach the frontend or the model, so this is the only
            # place the real cause of a "provider error" is recoverable.
            logger.exception(
                "Provider '%s' failed (%s): model=%s tools=%s messages=%d",
                name, reason, call_kwargs.get("model"),
                bool(call_kwargs.get("tools")), len(call_kwargs.get("messages") or []),
            )
    raise AllProvidersFailedError(failures)


def _user_facing_error(failures):
    """Build a short, accurate frontend message from the per-provider
    failures collected by _create_completion."""
    if not failures:
        return "No LLM provider is configured. Add one in Settings → Providers."
    reasons = {r for _, r, _ in failures}
    if reasons == {"rate_limited"}:
        # "Try again shortly" is wrong — and endlessly frustrating — when the
        # problem is that the conversation no longer fits in anyone's limit.
        if all(n in _provider_size_ceiling for n, _, _ in failures):
            names = ", ".join(n for n, _, _ in failures)
            return (f"This conversation has grown too large for {names} to accept in "
                    "one request. Start a new conversation, or add a provider with a "
                    "higher token limit.")
        return "All configured providers are rate-limited or out of usage right now. Try again shortly."
    if reasons == {"auth_error"}:
        return "All configured providers rejected the request — check your API keys in Settings."
    if reasons == {"network_error"}:
        return "Couldn't reach any LLM provider — check your network connection."
    if reasons == {"rate_limited", "network_error"}:
        limited = [n for n, r, _ in failures if r == "rate_limited"]
        unreachable = [n for n, r, _ in failures if r == "network_error"]
        return (
            f"{', '.join(limited)} {'is' if len(limited) == 1 else 'are'} rate-limited, and "
            f"{', '.join(unreachable)} {'is' if len(unreachable) == 1 else 'are'} unreachable right now. Try again shortly."
        )
    return "All providers failed: " + ", ".join(f"{name} ({reason})" for name, reason, _ in failures)

# Maps the sanitized function name we expose to the model (e.g.
# "mcp_github_create_issue") back to the (server, real tool name) needed to
# actually invoke it. Rebuilt by load_mcp_tools().
mcp_tool_registry = {}


def set_static_dir(path):
    """Point the agent's file tools (read_file, create_file, create_page,
    etc.) at a specific folder — e.g. static/<username>/files/. context.md
    (the agent's long-term memory, read/updated via the same file tools)
    lives in this same folder, so this scopes both. Creates the folder if it
    doesn't exist yet."""
    global static_dir
    if not path.endswith(os.sep):
        path = path + os.sep
    os.makedirs(path, exist_ok=True)
    static_dir = path
    return static_dir


user_creator = None


def set_user_creator(fn):
    """Registers the function the create_user tool calls to actually create
    a new FreeClaw user. main.py wires this up at startup (rather than
    agent.py importing main.py directly, which would be circular) — fn must
    accept (name, context=None) and return the created user's name, raising
    an exception (with a clear message) on failure."""
    global user_creator
    user_creator = fn


def get_messages():
    return agent_messages


def _merge_system_messages(messages):
    """Collapse every system-role message into a single one at index 0.

    Some providers (confirmed on NVIDIA's qwen3.5) reject the whole request
    when a system message appears anywhere but the very front. Older saved
    conversations have two (instructions + context.md), so merge on load."""
    system_parts = [m.get("content", "") for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    if not system_parts:
        return rest
    merged = {"role": "system", "content": "\n\n".join(p for p in system_parts if p)}
    return [merged] + rest


def _heal_history(messages):
    """Repair a loaded conversation so every provider will accept it again.

    OpenAI-compatible APIs reject the entire request if any assistant
    tool_calls entry lacks a matching `tool` response (or a `tool` message
    answers an id nobody declared) — and since the full history is resent
    every turn, a conversation saved in that state (older builds, mid-turn
    crashes) stays broken forever without this. Missing tool responses get
    a placeholder, orphaned ones are dropped, null call ids are backfilled,
    and multiple system messages are merged (_merge_system_messages)."""
    messages = _merge_system_messages(messages)
    healed = []
    pending = {}  # id -> function name, awaiting a tool response

    def flush_pending():
        for call_id, fn_name in pending.items():
            healed.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": fn_name,
                "content": "(tool response missing from saved conversation — treat this call as failed)",
            })
        pending.clear()

    for n, m in enumerate(messages):
        role = m.get("role")
        if role == "tool":
            call_id = m.get("tool_call_id")
            if call_id in pending:
                del pending[call_id]
                healed.append(m)
            # else: orphaned/duplicate tool response — drop it
            continue
        flush_pending()
        healed.append(m)
        if role == "assistant" and m.get("tool_calls"):
            for i, tc in enumerate(m["tool_calls"]):
                if not tc.get("id"):
                    tc["id"] = f"healed_{n}_{i}"
                pending[tc["id"]] = (tc.get("function") or {}).get("name", "unknown")
    flush_pending()
    return healed


def set_messages(messages):
    """Load a previously-saved conversation (a plain list of OpenAI-style
    message dicts) as the active conversation for subsequent agent_stream
    calls. Healed on the way in so a conversation corrupted by an older
    build (or a mid-turn crash) can't keep failing every provider call."""
    global agent_messages
    agent_messages = _heal_history(messages)


# reset() builds the system message once; a conversation can then run for days
# (a ping delivered next week reuses the same one) without another reset(), so a
# date baked in at reset() would go stale. It's refreshed on every turn instead
# (see _refresh_volatile), which is what stops add_ping resolving "today" /
# "tomorrow" against a guess.
#
# The date only — no time of day. A clock down to the minute changed the prompt
# every minute and could never be cached; the date is stable for a whole day, so
# the entire system message now is too. Anything needing the actual time calls
# the get_time tool, which is the rare case and shouldn't be billed for on every
# request.
_NOW_LINE_PREFIX = "Current date: "

# What the line looked like when it carried a time as well. Only used to spot
# and strip it when migrating a conversation saved by an older build — matching
# on the current prefix alone would leave a stale clock frozen in the cached
# part of the prompt forever.
_LEGACY_NOW_PREFIXES = (_NOW_LINE_PREFIX, "Current date/time: ")


def _now_line():
    return (f"{_NOW_LINE_PREFIX}{datetime.now().strftime('%Y-%m-%d %A')} "
            f"— resolve relative dates against this, never guess. "
            f"Call get_time for the time of day.")


# Marks where the injected context.md copy starts.
_CTX_HEADER = "\ncontext.md:\n"

# What a brand-new context.md is seeded with. Headings only: they give the
# model somewhere obvious to file a new fact, which keeps memory grouped
# instead of becoming one flat list — and grouped is what makes it possible to
# send the model a table of contents instead of the whole file (see
# _context_block). Used by users.create_user() and by reset() when the file has
# gone missing, so both produce the same shape.
CONTEXT_TEMPLATE = """## About
## Preferences
## People
## Work
## Projects
## Commands
"""

# Everything after this marker is rebuilt every turn; everything before it —
# the instructions *and* the context.md snapshot — stays byte-identical for the
# whole conversation. That split is what makes prompt caching possible:
# providers cache a byte-identical *prefix*, and this text used to open with
# the live timestamp, so every turn differed from byte 0 and nothing could ever
# be reused. _cache_breakpoint_messages() marks this exact boundary for the
# models that need an explicit one.
#
# Only the clock lives down here now. context.md used to as well, re-read every
# turn, which meant memory was both the fastest-growing part of the prompt and
# the one part that could never be cached. It is now snapshotted once by
# reset() and left alone until the next reset — see _refresh_volatile.
_VOLATILE_HEADER = "\n\n--- live context (refreshed every turn) ---\n"


# The sections always sent in full. Who the model is talking to and how they
# want to be talked to both apply to every turn, unlike the rest of memory —
# and preferences the model has to go and fetch are preferences it ignores.
_CTX_ALWAYS = ("About", "Preferences")


# Sections created since this conversation started. The table of contents in
# the system prompt is snapshotted by reset() and deliberately never re-read
# (it lives in the cached prefix), so without this a section the model files
# something under mid-conversation stays invisible to it: it can't call
# search_context for a name it was never told about, and a question that
# section answers goes to web_search instead. Rendered into the volatile tail,
# which is rewritten every turn anyway, so the cached prefix is untouched.
_new_sections = []


def _note_new_section(name):
    """Remember a section created mid-conversation so the next turn's prompt
    mentions it. No-op for one already listed."""
    if name and name not in _new_sections:
        _new_sections.append(name)


def _new_sections_line():
    if not _new_sections:
        return ""
    return ("\nSections added this conversation (read with search_context): "
            + ", ".join(_new_sections))


def _context_path():
    return static_dir + "context.md"


def _read_context():
    try:
        with open(_context_path(), "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _write_context(content):
    with open(_context_path(), "w", encoding="utf-8") as f:
        f.write(content)


def _split_context(content):
    """Split a context.md into (preamble, [(header, body_lines), ...]).

    `preamble` is everything above the first "## " line. Normally empty — but
    the Setup Wizard's context.md is a free-form script with no headings at
    all, and that has to keep reaching the prompt whole rather than being
    summarised down to nothing."""
    preamble = []
    sections = []
    for line in content.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return "\n".join(preamble), sections


def _norm_header(name):
    """Fold a heading down to letters and digits, so the model's "## work" and
    "Work:" both land on the section actually called Work."""
    return re.sub(r'[^a-z0-9]', '', (name or "").lower())


def _clean_header(name):
    """A heading as it gets written to the file: no '#', one line, trimmed."""
    cleaned = (name or "").replace("#", " ").strip()
    return cleaned.splitlines()[0].strip() if cleaned else ""


def _find_header(sections, header, fuzzy=True):
    """Index of the section `header` names, or -1.

    An exact (normalised) match always wins. With `fuzzy`, containment either
    way is then accepted, so a model asking for "Projects" finds "Current
    Projects" instead of silently starting a duplicate — and the *longest*
    such match is taken, so "mental health" prefers "Mental Health" over
    "Health" rather than whichever happens to sit earlier in the file.

    Pass fuzzy=False when the caller's whole purpose is to create something
    new. Containment is right for looking a section up and wrong for deciding
    one already exists: "Health" contains-matches "Mental Health", which would
    otherwise make every header a permanent block on any longer name built
    from it."""
    target = _norm_header(header)
    if not target:
        return -1
    normalised = [_norm_header(name) for name, _ in sections]
    if target in normalised:
        return normalised.index(target)
    if not fuzzy:
        return -1
    best, best_len = -1, 0
    for i, name in enumerate(normalised):
        if name and (target in name or name in target) and len(name) > best_len:
            best, best_len = i, len(name)
    return best


def _section_text(body):
    return "\n".join(body).strip()


def _render_context(preamble, sections):
    """Rebuild the file from _split_context()'s pieces."""
    parts = [preamble.rstrip()] if preamble.strip() else []
    for name, body in sections:
        text = _section_text(body)
        parts.append(f"## {name}\n{text}" if text else f"## {name}")
    return "\n".join(parts) + "\n"


def _context_block():
    """The part of context.md that goes into the system message: the About and
    Preferences sections in full, plus the *names* of every other section.

    Memory used to be injected whole, which made it the fastest-growing thing
    in the prompt — every fact ever saved was paid for on every turn, nearly
    all of it irrelevant to what was just asked. The model now gets who it's
    talking to and a table of contents, and pulls a section in with
    search_context when the conversation actually calls for one.

    Anything above the first heading is kept verbatim: an unheaded context.md
    has no table of contents to offer and would otherwise arrive empty.

    Only called when a conversation starts (reset), never per turn."""
    content = _read_context()
    if not content.strip():
        return _CTX_HEADER + "(empty — use add_context to start it)\n"

    preamble, sections = _split_context(content)
    # Kept in _CTX_ALWAYS order, not file order, so the block reads the same
    # way for every user however their context.md happens to be arranged.
    inlined = []
    for header in _CTX_ALWAYS:
        idx = _find_header(sections, header)
        if idx != -1 and idx not in inlined:
            inlined.append(idx)
    parts = []
    if preamble.strip():
        parts.append(preamble.strip())
    for idx in inlined:
        name, body = sections[idx]
        parts.append(f"## {name}\n{_section_text(body) or '(empty)'}")
    others = [name for i, (name, _) in enumerate(sections) if i not in inlined]
    if others:
        parts.append("Other sections (read with search_context): " + ", ".join(others))
    return _CTX_HEADER + "\n".join(parts) + "\n"


def _stable_prefix(content):
    """The cacheable part of a system message: the instructions plus the
    context.md snapshot, i.e. everything before the volatile marker.

    Also migrates the two earlier layouts, both of which kept context.md in the
    part that got rewritten every turn: the original had the clock as its first
    line and context.md glued to the end with no marker at all, and the one
    after it put clock + context.md together below the marker. Either way the
    snapshot is lifted up here, where it now belongs, instead of being dropped
    and leaving an in-flight conversation with no memory until its next
    reset."""
    head, sep, _ = content.partition(_VOLATILE_HEADER)
    if not sep:
        # Oldest layout: drop the leading clock line.
        first_line, _, rest = head.partition("\n")
        if first_line.startswith(_LEGACY_NOW_PREFIXES):
            head = rest
    if _CTX_HEADER in head:
        return head  # already in the current shape — returned verbatim so the
                     # bytes a provider cached last turn are the bytes it sees
    return head.partition(_CTX_HEADER)[0].rstrip() + _context_block()


def _refresh_volatile():
    """Rewrite the volatile tail at the top of every turn. That is now only the
    clock, which has to stay live or add_ping resolves "tomorrow" against a
    stale date.

    context.md is deliberately *not* re-read here. It's snapshotted by reset()
    and stays fixed for the conversation, which is what lets it sit in the
    cached prefix. The cost is that a fact the model saves mid-conversation
    won't appear in its own prompt until the next reset — but the *names* of
    any sections it created do (see _new_sections), because a section the model
    doesn't know exists is one it can never call search_context for."""
    if not agent_messages or agent_messages[0].get("role") != "system":
        return
    stable = _stable_prefix(agent_messages[0].get("content", ""))
    agent_messages[0]["content"] = (stable + _VOLATILE_HEADER + _now_line()
                                    + _new_sections_line() + "\n")


def reset(tts=False):
    """Start a fresh conversation for the current static_dir, seeded with
    that user's context.md.

    The result is a single system message, always exactly one and always at
    index 0: some providers' chat templates (confirmed on NVIDIA's qwen3.5)
    reject the request the moment a second system-role message shows up
    anywhere else in the list. The eco_messages slicing in agent_stream
    assumes this is the only header message.

    Its content is ordered stable-instructions-first, volatile-tail-last (see
    _VOLATILE_HEADER) so the bulk of it can be cached by the provider."""
    global agent_messages
    # The fresh snapshot below lists every section, so the running tally of
    # ones added mid-conversation starts over with it.
    _new_sections.clear()
    # Create the file if it's missing so the model's first edit_file/create_file
    # lands somewhere; _context_block() reads it back below. Uses the same
    # headed template new users get, so a context.md that was deleted (or
    # predates the template) comes back with the same structure rather than
    # blank.
    ctx_path = _context_path()
    if not os.path.exists(ctx_path):
        with open(ctx_path, "w", encoding="utf-8") as f:
            f.write(CONTEXT_TEMPLATE)

    # Every line here is resent on every request, so it stays terse — but each
    # one is load-bearing. Three separate instructions used to say "be direct"
    # and three more said "match depth to the question"; those are merged, not
    # dropped.
    prompt = """
You are a capable AI assistant.

Answer directly: no preamble, no filler, no restating the question. Match depth to the request —
simple questions get simple answers. Use your tools to act rather than describing what could be
done, and verify anything important before relying on it.

context.md is your long-term memory, filed under headers. Below are its About and Preferences
sections plus the names of the other sections — never read the file itself with a tool. Call
search_context to open a section whenever one looks relevant.
Save only what would change how you help this user weeks from now: who they are, standing
preferences, ongoing projects and work, the people in their life, decisions, corrections you
were given. When something qualifies, add_context immediately and silently. Do not save chit-chat,
one-off requests, the details or results of the task you are doing, anything you can look up
again, or anything already in memory — a cluttered file buries the facts that matter.

Scheduled events live in ping.md.
"""
    if tts:
        prompt += "\nYou are speaking through text-to-speech — write for clear, natural speech.\n"

    # Instructions + this one snapshot of context.md above the marker, live
    # clock below it. _refresh_volatile() rewrites everything from the marker
    # down on every turn and leaves the text above it byte-identical, which is
    # what a provider's prompt cache needs in order to hit. This call is the
    # only place context.md is read into the prompt, so a reset is what picks
    # up edits made to it.
    agent_messages = [{"role": "system", "content":
                       prompt.rstrip() + _context_block()
                       + _VOLATILE_HEADER + _now_line() + "\n"}]
    refresh_tools()


# Canonical timestamp the add_ping tool asks the model for. Both the add_ping
# sort and the ping scheduler parse times through parse_ping_time() rather than
# a single strict format, so a timestamp that's slightly off-format (seconds,
# a 'T' separator, AM/PM, ISO offset) still fires instead of sitting unnoticed
# in ping.md forever — that silent-skip was why scheduled pings weren't running.
PING_TIME_FORMAT = "%Y-%m-%d %H:%M"
_PING_TIME_FALLBACK_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y.%m.%d %H:%M",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y %I:%M %p",
    "%Y-%m-%d %I:%M %p",
    "%Y-%m-%d %I:%M%p",
)


def parse_ping_time(stamp):
    """Parse a ping timestamp into a naive local datetime, tolerating the
    common shapes a model emits instead of the exact PING_TIME_FORMAT. Returns
    None if nothing matches. A tz-aware value (e.g. an ISO string with an
    offset) is converted to local time and made naive so it compares cleanly
    against datetime.now()."""
    if not stamp:
        return None
    stamp = stamp.strip()
    parsed = None
    try:
        parsed = datetime.fromisoformat(stamp)  # tolerant on 3.11+ (space/T, secs)
    except ValueError:
        for fmt in (PING_TIME_FORMAT, *_PING_TIME_FALLBACK_FORMATS):
            try:
                parsed = datetime.strptime(stamp, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def build_context_tools():
    """Memory, one section at a time. Your system prompt carries only the About
    and Preferences sections and the list of header names, so these are how the
    rest of context.md is read and written."""
    return [
        {
            "type": "function",
            "function": {
                "name": "search_context",
                "description": "Reads one section of your memory of this user — their details, preferences, projects, people, and anything they've told you before. Your prompt lists section names only, never their contents, so call this whenever a name looks relevant. This, not web_search, is where anything about this user comes from.",
                "parameters": {
                    "type": "object",
                    "properties": { "header": { "type": "string" } },
                    "required": ["header"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "add_context",
                "description": "Saves one durable fact under a header in context.md — this is how you remember anything. Only for what still matters weeks from now, never task details or one-offs. Creates the header if it doesn't exist.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "header": { "type": "string" },
                        "string": { "type": "string", "description": "What to remember" }
                    },
                    "required": ["header","string"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "add_header",
                "description": "Adds an empty header to context.md. Only for a topic no existing header covers — add_context alone can save under one.",
                "parameters": {
                    "type": "object",
                    "properties": { "header": { "type": "string" } },
                    "required": ["header"]
                }
            }
        },
    ]


def build_file_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "create_user",
                "description": "Creates a new FreeClaw user with their own chats and memory. Only when explicitly asked to add a user — never to switch context here.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": { "type": "string", "description": "Used as a folder name: letters, numbers, spaces, - or _" },
                        "context": { "type": "string", "description": "Optional starting content for their context.md" }
                    },
                    "required": ["name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Reads a file from /static — ping.md, and created or uploaded files.",
                "parameters": {
                    "type": "object",
                    "properties": { "filename": { "type": "string" } },
                    "required": ["filename"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "Lists the files in /static.",
                "parameters": { "type": "object", "properties": {} }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "create_page",
                "description": "Creates an HTML page for the user to see.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": { "type": "string", "description": "e.g. page.html" },
                        "contents": { "type": "string", "description": "HTML" }
                    },
                    "required": ["filename","contents"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "create_file",
                "description": "Creates an output file — document, data export, script, config — for the user or other tools.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": { "type": "string", "description": "e.g. notes.md" },
                        "contents": { "type": "string", "description": "May be blank" }
                    },
                    "required": ["filename","contents"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "delete_file",
                "description": "Deletes a file from /static. Never delete context.md or ping.md.",
                "parameters": {
                    "type": "object",
                    "properties": { "filename": { "type": "string" } },
                    "required": ["filename"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "add_ping",
                "description": "Schedules a reminder or future action; the action text reaches the user as a prompt when it fires.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date_time": { "type": "string", "description": "Absolute, as 'YYYY-MM-DD HH:MM'. Never guess: today's date is in your prompt, but any time of day ('in 20 minutes', '6pm', 'tonight') needs get_time first." },
                        "action": { "type": "string", "description": "An instruction to yourself, e.g. 'Remind them to take their medication.'" },
                    },
                    "required": ["date_time", "action"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "Replaces one exact string in an existing /static file. Use instead of create_file to change existing content — ping.md, or correcting or removing a context.md line (add_context adds one).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": { "type": "string" },
                        "old_str": { "type": "string", "description": "Exact string to replace" },
                        "new_str": { "type": "string", "description": "Replacement" }
                    },
                    "required": ["filename", "old_str", "new_str"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_image_description",
                "description": "Returns a detailed description of an image in /static.",
                "parameters": {
                    "type": "object",
                    "properties": { "filename": { "type": "string" } },
                    "required": ["filename"]
                }
            }
        }
    ]


def build_time_tools():
    """The clock, as a tool. Its own builder rather than part of the file or
    utility sets because it's the one tool every intent needs to be able to
    reach: the system prompt carries the date but not the time, so asking "what
    time is it" in a mode that didn't offer this would send the model to a web
    search for something the host machine already knows."""
    return [
        {
            "type": "function",
            "function": {
                "name": "get_time",
                "description": "Current local date and time. For the time of day, or before scheduling anything relative ('in 20 minutes'). Today's date is already in your prompt — don't call this just for that.",
                "parameters": { "type": "object", "properties": {} }
            }
        }
    ]


def build_search_tools():
    # Sites worth steering the model toward for common query types. Rendered
    # as "weather → a, b; news → c" rather than str(dict), which spent tokens
    # on Python's quotes and brackets to say the same thing.
    best_sites = {
        "weather": ["localconditions.com"],
        "news": ["bbc.com", "atoztimes.com"],
    }
    site_guide = "; ".join(f"{k} → {', '.join(v)}" for k, v in best_sites.items())
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Searches the public internet for facts you don't have. Max 2 calls per task, then answer with what you have or say you couldn't find it. Best sites: " + site_guide,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": { "type": "string", "description": "Natural-language query" },
                        "site": { "type": "string", "description": "Restrict to one site; omit for a general search" }
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_web",
                "description": "First 3000 characters of a webpage. Only when the user names a specific URL.",
                "parameters": {
                    "type": "object",
                    "properties": { "url": { "type": "string" } },
                    "required": ["url"]
                }
            }
        }
    ]


def build_utility_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "open_url",
                "description": "Opens a URL or URI on the user's device — webpages, or apps via custom URI (texting, calling).",
                "parameters": {
                    "type": "object",
                    "properties": { "url": { "type": "string" } },
                    "required": ["url"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                # The longest description here, and deliberately so: every
                # clause is a rule the model breaks without it. Kept whole,
                # only tightened.
                "name": "run_bash_command",
                "description": "Runs a shell command on this machine. Run it when asked; don't chain several without reporting back. FreeClaw handles permission outside this conversation — never ask whether you may, never describe a command instead of running it, never treat your own judgement as approval. Just call this: the user is prompted automatically and you'll be told if they refuse.",
                "parameters": {
                    "type": "object",
                    "properties": { "command": { "type": "string" } },
                    "required": ["command"]
                }
            }
        }
    ]


def _sanitize_tool_name(name):
    """OpenAI function names must match ^[A-Za-z0-9_-]+$ and stay short, so
    scrub anything else out of the MCP-derived name."""
    cleaned = re.sub(r'[^0-9A-Za-z_-]', '_', name).strip('_') or 'tool'
    return cleaned[:60]


def load_mcp_tools():
    """Connect to each MCP server configured in .env, fetch its tools, and
    return them as OpenAI-style function definitions. Also (re)builds
    mcp_tool_registry, which maps the function name exposed to the model back
    to the (server, real tool name) needed to actually call it.

    A single unreachable server is logged and skipped rather than taking down
    the whole tool list."""
    global mcp_tool_registry
    registry = {}
    out = []
    for server in mcp_client.read_servers():
        if not server.get("enabled", True):
            continue
        try:
            server_tools = mcp_client.list_tools(server)
        except Exception as e:
            print(f"[mcp] '{server.get('name')}' unavailable: {e}")
            logger.exception("MCP server '%s' (%s) unavailable",
                             server.get('name'), mcp_client.describe(server))
            continue
        for t in server_tools:
            real_name = t.get("name")
            if not real_name:
                continue
            fn_name = _sanitize_tool_name(f"mcp_{server.get('name', '')}_{real_name}")
            # Guarantee uniqueness across servers/tools.
            base = fn_name
            n = 1
            while fn_name in registry:
                suffix = f"_{n}"
                fn_name = base[:60 - len(suffix)] + suffix
                n += 1
            params = t.get("inputSchema") or {"type": "object", "properties": {}}
            description = t.get("description") or f"{real_name} (via '{server.get('name')}' MCP server)"
            out.append({
                "type": "function",
                "function": {
                    "name": fn_name,
                    "description": description[:1024],
                    "parameters": params,
                },
            })
            registry[fn_name] = {"server": server, "tool": real_name}
    mcp_tool_registry = registry
    return out


def refresh_tools():
    """Rebuild the full tool list = built-in tools + any MCP server tools.
    Safe to call anytime (e.g. after the MCP server list changes); does not
    touch the conversation or the LLM client."""
    global tools
    tools = (build_file_tools() + build_context_tools() + build_search_tools()
             + build_utility_tools() + build_time_tools() + load_mcp_tools())
    return tools


def _run_tool(command_name, args_dict, bash_approved=False):
    """Execute a single tool call and return its result as a string.

    Pure dispatch: appending the tool-response message and making the
    follow-up LLM turn are the caller's job, so this can run once per call
    when the model requests several tools in one turn. Exceptions may
    escape freely — the caller converts them into an error result, because
    whatever happens, every tool_call id the assistant message declared
    must end up with a response or the whole conversation is rejected by
    the provider on the next turn.

    `bash_approved` has to be passed explicitly by the caller that ran the
    approval gate (see agent_stream). It defaults to False so there is no code
    path — not a future caller, not a mistake — that reaches the shell without
    a decision having been made first."""
    parameter = (args_dict.get('query') or args_dict.get('site') or args_dict.get('url')
                 or args_dict.get('command') or args_dict.get('filename')
                 or args_dict.get('header') or args_dict.get('contents') or None)
    print(f"Agent called tool: {command_name}" + (f" — {parameter}" if parameter else ""))

    if command_name == 'create_user':
        new_name = args_dict.get('name')
        new_context = args_dict.get('context')
        if not new_name or not str(new_name).strip():
            return "Error: a name is required to create a user."
        if user_creator is None:
            return "Error: user creation isn't available in this context."
        try:
            created_name = user_creator(new_name, new_context)
        except Exception as e:
            logger.exception("create_user tool failed for name=%r", new_name)
            return f"Error creating user: {e}"
        result = f"User '{created_name}' created successfully."
        if new_context:
            result += " Their context.md was set with the provided content."
        return result

    # 'search' was this tool's name until it was renamed for being the bare
    # verb the model reached for when it meant search_context; still accepted
    # so a conversation saved before the rename can be resumed.
    if command_name in ('web_search', 'search'):
        site = args_dict.get('site')
        if site:
            return scraper.get_result(parameter + ' - ' + site)
        return scraper.get_result(parameter)

    if command_name == 'read_file':
        # Uploaded files are referenced by their full "static/..." path
        # in the chat tag, not a bare filename; take just the basename
        # so both forms resolve against this session's static_dir.
        filename = os.path.basename(args_dict.get('filename'))
        try:
            with open(static_dir+filename, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return "File not found."

    if command_name == 'search_context':
        header = args_dict.get('header') or ''
        _, sections = _split_context(_read_context())
        idx = _find_header(sections, header)
        if idx == -1:
            # Name what does exist rather than a bare miss: the model picked
            # this header off the list in its prompt, so a near-miss is far
            # likelier than a section that genuinely isn't there.
            names = ", ".join(name for name, _ in sections)
            return (f"No '{header}' section in context.md. "
                    + (f"Sections: {names}." if names else "It has no sections yet."))
        name, body = sections[idx]
        text = _section_text(body)
        return f"## {name}\n{text}" if text else f"## {name}\n(this section is empty)"

    if command_name == 'add_context':
        entry = (args_dict.get('string') or '').strip()
        if not entry:
            return "Error: nothing to save — 'string' was empty."
        preamble, sections = _split_context(_read_context())
        idx = _find_header(sections, args_dict.get('header'))
        created = False
        if idx == -1:
            # Better a header nobody asked for than a fact quietly dropped
            # because the model guessed the name wrong.
            name = _clean_header(args_dict.get('header'))
            if not name:
                return "Error: a header is required."
            sections.append((name, []))
            idx = len(sections) - 1
            created = True
            _note_new_section(name)
        name, body = sections[idx]
        lines = list(body)
        while lines and not lines[-1].strip():
            lines.pop()
        if any(line.strip().lstrip('-*').strip() == entry.lstrip('-*').strip()
               for line in lines if line.strip()):
            return f"Already saved under '{name}' — nothing added."
        # One-liners become list items so a section stays readable as it grows;
        # anything multi-line is left exactly as the model wrote it.
        if "\n" not in entry and not entry.startswith(("-", "*", "#")):
            entry = "- " + entry
        lines.append(entry)
        sections[idx] = (name, lines)
        _write_context(_render_context(preamble, sections))
        return f"Saved under '{name}'." + (" (new section)" if created else "")

    if command_name == 'add_header':
        name = _clean_header(args_dict.get('header'))
        if not name:
            return "Error: a header is required."
        preamble, sections = _split_context(_read_context())
        # Exact match only: this tool exists to create a section, so a merely
        # similar name ("Health" when asked for "Mental Health") must not count
        # as one already existing.
        idx = _find_header(sections, name, fuzzy=False)
        if idx != -1:
            return f"'{sections[idx][0]}' already exists in context.md — add to it with add_context."
        sections.append((name, []))
        _write_context(_render_context(preamble, sections))
        _note_new_section(name)
        return f"Added the '{name}' section to context.md."

    if command_name == 'get_image_description':
        vision_provider_name = read_vision_provider()
        if not vision_provider_name:
            return "Image description isn't configured — pick a provider in Settings → Vision Model."
        provider = next((p for p in read_providers() if p.get("name") == vision_provider_name), None)
        if provider is None or not provider.get("url"):
            return f"The vision provider '{vision_provider_name}' no longer exists — pick another in Settings → Vision Model."
        if not provider.get("key"):
            return f"Provider '{vision_provider_name}' has no API key set — add one in Settings → Providers."
        if not provider.get("model"):
            return f"Provider '{vision_provider_name}' has no model set — add one in Settings → Providers to use it for vision."

        filename = os.path.basename(args_dict.get('filename'))
        file_location = static_dir+filename
        try:
            with open(file_location, "rb") as image_file:
                image_data = base64.b64encode(image_file.read()).decode("utf-8")
        except FileNotFoundError:
            return "File not found."

        ext = filename.rsplit(".", 1)[-1].lower()
        mime_types = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
        mime_type = mime_types.get(ext, "image/jpeg")

        vision_client = _client_for(provider["name"], provider["key"], provider["url"])
        try:
            completion = vision_client.chat.completions.create(
                model=provider["model"],
                messages=[
                    {
                        "role": "system",
                        "content": "Describe images that the user sends in extreme detail"
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{image_data}"
                                }
                            },
                            {
                                "type": "text",
                                "text": "Please describe this image in extreme detail."
                            }
                        ]
                    }
                ],
                temperature=1,
                top_p=1,
            )
        except Exception as e:
            logger.exception("Vision provider '%s' failed", vision_provider_name)
            return f"Image description failed — vision provider '{vision_provider_name}' returned an error: {e}"
        return completion.choices[0].message.content or "No description returned."

    if command_name == 'get_time':
        # Deliberately exactly PING_TIME_FORMAT, with no weekday appended: the
        # model's whole reason for calling this is usually to hand the result
        # to add_ping, and parse_ping_time() rejects a trailing weekday — which
        # would leave the ping sitting in ping.md forever instead of firing.
        # The weekday is in the system prompt already if it's wanted.
        return datetime.now().strftime(PING_TIME_FORMAT)

    if command_name == 'list_files':
        return "Files in static directory: "+", ".join(os.listdir(static_dir))

    if command_name == 'read_web':
        return scraper.scrape(parameter)

    if command_name == 'create_file':
        filename = args_dict.get('filename')
        if "/" in filename or "\\" in filename:
            return "Invalid filename."
        contents = args_dict.get('contents') or ''
        path = static_dir + filename
        # "w" truncates, which on context.md would silently destroy every fact
        # learned so far — and create_file is exactly what a model reaches for
        # when it wants to "save" something. Append there instead, so memory
        # can only ever grow; edit_file remains the way to change or remove.
        if filename == "context.md" and os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n" + contents.strip() + "\n")
            return "Appended to context.md. Existing memory was kept — use edit_file to change or remove a line."
        with open(path, "w", encoding="utf-8") as f:
            f.write(contents)
        return "Your file is accessible at "+_static_url(static_dir, filename)

    if command_name == 'delete_file':
        filename = args_dict.get('filename')
        if "/" in filename or "\\" in filename:
            return "Invalid filename."
        file_path = static_dir + filename
        if os.path.exists(file_path):
            os.remove(file_path)
            return "File deleted."
        return "File not found."

    if command_name == 'edit_file':
        filename = args_dict.get('filename')
        if "/" in filename or "\\" in filename:
            return "Invalid filename."
        old_str = args_dict.get('old_str')
        new_str = args_dict.get('new_str')
        try:
            with open(static_dir + filename, "r", encoding="utf-8") as f:
                contents = f.read()
        except FileNotFoundError:
            return "File not found."
        # An empty file can never match old_str, so say what will work rather
        # than letting the model retry the same failing call.
        if not contents.strip():
            return "File is empty — use create_file to write its first contents."
        if old_str not in contents:
            return "String not found in file."
        updated = contents.replace(old_str, new_str, 1)
        with open(static_dir + filename, "w", encoding="utf-8") as f:
            f.write(updated)
        return "File edited successfully."
    if command_name == 'add_ping':
        filename = "ping.md"
        date_time = args_dict.get('date_time')
        action = args_dict.get('action')
        with open(static_dir+filename, "a", encoding="utf-8") as f:
            f.write(f"{date_time} - {action}\n")

        # Re-sort ping.md on every update so the next scheduled event is
        # always the first line and the furthest-out event is the last.
        # Each entry is "YYYY-MM-DD HH:MM - <action>"; sort by the parsed
        # timestamp. Any line whose timestamp doesn't parse is kept in its
        # existing order at the bottom rather than dropped.
        with open(static_dir+filename, "r", encoding="utf-8") as f:
            entries = [line for line in f.read().splitlines() if line.strip()]

        def _ping_sort_key(line):
            parsed = parse_ping_time(line.split(" - ", 1)[0])
            return (1, datetime.max) if parsed is None else (0, parsed)

        entries.sort(key=_ping_sort_key)
        with open(static_dir+filename, "w", encoding="utf-8") as f:
            f.write("\n".join(entries) + "\n" if entries else "")
        return "Ping added successfully."
    if command_name == 'create_page':
        filename = args_dict.get('filename')
        if "/" in filename or "\\" in filename:
            return "Invalid filename."
        with open(static_dir+filename, "w", encoding="utf-8") as f:
            f.write(args_dict.get('contents') or '')
        return "Your site is live at "+_static_url(static_dir, filename)

    if command_name == 'open_url':
        # Actually opening the tab happens client-side — the frontend
        # listens for the "tool_call" SSE event (which already carries
        # this url in evt.arguments) and calls window.open() on it.
        return "URL opened: "+args_dict.get('url', '')

    if command_name == 'run_bash_command':
        # Read `command` directly rather than using the shared `parameter`
        # above: that falls back through query/site/url/…, so a call carrying a
        # stray `url` key alongside `command` would have run the url. Harmless
        # noise before the approval gate existed — a hole in it now, since the
        # gate checks args_dict['command'] and this is what actually executes.
        # Same key on both sides means the string approved is the string run.
        command = args_dict.get('command') or ''
        # Second line of defence. agent_stream has already run the gate and
        # passed the outcome in; this makes it impossible for a caller that
        # skipped it to execute anything regardless.
        if not bash_approved:
            logger.warning("Blocked an unapproved bash command: %.300r", command)
            return approvals.denial_message(approvals.DECISION_DENY)
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=True
        )
        stdout, stderr = proc.communicate()
        print(stdout,stderr)
        output = (stdout + "\n" + stderr).strip()
        if not output:
            output = 'Command was run successfully, Report back to the user.'
        return output

    if command_name in mcp_tool_registry:
        entry = mcp_tool_registry[command_name]
        server = entry["server"]
        try:
            return mcp_client.call_tool(server, entry["tool"], args_dict)
        except Exception as e:
            logger.exception("MCP tool '%s' on '%s' failed", entry['tool'], server.get('name'))
            return f"Error calling MCP tool '{entry['tool']}' on '{server.get('name')}': {e}"

    # Unknown tool (e.g. an MCP server that was removed after the model
    # decided to call it) — answer it anyway so the tool_call isn't left
    # dangling, which would break the next turn.
    return "Unknown tool: " + command_name


# Per-intent settings for a turn: (recent messages to send, temperature,
# tools offered). Simple conversational intents get a small context window
# and no tools; precision-flavored intents run colder. The system message at
# index 0 is always sent on top of the recent slice.
_TAG_SETTINGS = {
    'Greeting/goodbye':  (3, 1.0, 'file'),
    'Personal-question': (5, 1.0, 'file'),
    'Banter':            (5, 1.0, 'file'),
    'About-user':        (5, 1.0, 'file'),
    'Search':            (5, 0.4, 'search'),
    'Context':           (9, 1.0, 'all'),
    'Edit':              (9, 1.0, 'all'),
    'Logic':             (7, 0.2, 'all'),
    'Math':              (7, 0.2, 'all'),
    'Explain':           (7, 0.2, 'all'),
}
_DEFAULT_TAG_SETTINGS = (7, 1.0, 'all')  # Coding, Writing, List, Suggest, Utility, ...


def _window_start(messages, recent):
    """Index the recent-history slice should start at, aiming for `recent`
    messages but widening to keep a tool call whole.

    A tool result is only valid directly after the assistant message whose
    tool_calls it answers — providers 400 on a 'tool' role that opens the
    conversation ("must be a response to a preceeding message with
    'tool_calls'"). The fixed-size tail has no notion of that pairing, so a
    turn ending in assistant{tool_calls} → tool → assistant gets cut straight
    through the middle. Walk back past any tool results the cut orphaned so
    their assistant comes along: a message or two over budget, but what the
    tool returned stays in context instead of being dropped.

    Never crosses index 0 — that's the system message, and everything after it
    that isn't a tool result is a valid place to start."""
    start = max(1, len(messages) - recent)
    while start > 1 and messages[start].get("role") == "tool":
        start -= 1
    # Only reachable if the history itself is malformed (a tool result with no
    # assistant ahead of it); skipping them beats sending a request that 400s.
    while start < len(messages) and messages[start].get("role") == "tool":
        start += 1
    return start


def agent_stream(user_input=None, system_input=None, tool_input=None, tool_id=None, tool_name=None):
    """Generator version of the agent loop. Yields small dict events as the
    model produces output, so callers (e.g. the Flask route) can stream
    them to the browser in real time:
      {"type": "token", "text": "..."}            - a chunk of assistant text
      {"type": "tool_call", "name": "...", "arguments": {...}} - tool invocation started
      {"type": "tool_result", "name": "...", "result": "..."}  - tool finished
    The full, final conversation is available afterwards via agent_messages.
    """
    _refresh_volatile()
    # Default model id — any provider with its own model set overrides it.
    model = "openai/gpt-oss-120b"
    temp = 1
    check_tools = tools
    if user_input and system_input:
        raise Exception("You cannot have both user input and system input at the same time.")
    elif user_input:
        if user_input.lower() == 'reset':
            reset()
            yield {"type": "token", "text": "Agent reset."}
            return

        # A fresh turn — start its token tally from zero. Tool continuations
        # (the tool_input branch) deliberately don't reset, so their requests
        # add to the same turn's total.
        _reset_turn_usage()

        intent, _ = Classy.classify(user_input, CLASSIFIER_PATH)
        tag = intent[0]
        print('Intent: ' + tag)

        agent_messages.append({"role": "user", "content": user_input})
        agent_input = user_input

        recent, temp, tool_mode = _TAG_SETTINGS.get(tag, _DEFAULT_TAG_SETTINGS)
        if len(agent_messages) > recent + 2:
            eco_messages = [agent_messages[0], *agent_messages[_window_start(agent_messages, recent):]]
        else:
            eco_messages = agent_messages
        if tool_mode == 'none':
            check_tools = None
        elif tool_mode == 'search':
            # Memory tools are here as well as in 'file' mode: a search turn
            # both needs to read memory (looking something up "near home" or
            # "for my sister" depends on a section the prompt only names) and
            # to write it, since the system prompt tells the model to save what
            # it learns on every turn and this was the one mode with no tool to
            # do it with. get_time rides along with every restricted set — see
            # build_time_tools() for why it can't be left out of one.
            check_tools = (build_search_tools() + build_context_tools()
                           + build_time_tools())
        elif tool_mode == 'file':
            # Memory tools ride along here too: these are the tags (About-user,
            # Personal-question) most likely to turn up something worth
            # remembering, and the system prompt tells the model to save it.
            check_tools = build_file_tools() + build_context_tools() + build_time_tools()
    elif system_input:
        # Kept for direct/external callers only — note that appending a
        # second system-role message breaks the single-leading-system-message
        # invariant reset() relies on, and some providers reject that.
        _reset_turn_usage()
        agent_messages.append({"role": "system", "content": system_input})
        agent_input = system_input
        eco_messages = agent_messages
    # `is not None` (not truthiness): a tool can legitimately return "" —
    # e.g. reading an empty file — and that still has to be recorded as the
    # call's response and continue the turn, not fall through to the
    # "no input" error below with the tool_call left dangling.
    elif tool_input is not None:
        temp = 0.2
        yield {"type": "tool_result", "name": tool_name, "result": tool_input}
        agent_messages.append({
            "role": "tool",
            "tool_call_id": tool_id,
            "name": tool_name,
            "content": tool_input,
        })
        agent_input = tool_input
        # Resume from 2 user messages ago, or the first user message if
        # there aren't 2. A system-initiated conversation may have no user
        # turns at all — keep everything after the one system message then.
        user_indices = [i for i, m in enumerate(agent_messages) if m['role'] == 'user']
        if len(user_indices) >= 2:
            start_index = user_indices[-2]
        elif user_indices:
            start_index = user_indices[0]
        else:
            start_index = 1
        eco_messages = [agent_messages[0]] + agent_messages[start_index:]
    else:
        raise Exception("You must have either user input or system input.")
    print('Received: ' + agent_input)
    try:
        stream, provider = _create_completion(
            model=model,
            messages=eco_messages,
            temperature=temp,
            tools=check_tools,
            top_p=1,
            stream=True,
        )
    except AllProvidersFailedError as e:
        # Each provider's full traceback was already logged individually
        # inside _create_completion — this ties them together as one
        # incident so they're easy to find by searching the log.
        logger.error("All providers failed for this turn: %s", e.failures)
        raise Exception(_user_facing_error(e.failures))

    # Tell the frontend which provider is about to answer. This fires once
    # per _create_completion call, and every tool-call continuation is its
    # own recursive agent_stream() -> _create_completion() call (see the
    # `yield from agent_stream(...)` below), so a fallback mid-conversation
    # (or even mid a single tool round-trip) surfaces here too, not just at
    # the very start of the turn.
    _turn_usage["requests"] += 1
    yield {"type": "provider", "name": provider}

    # Consume the stream, forwarding text chunks to the caller in real
    # time and reassembling any tool calls (which always arrive as
    # incremental argument-string fragments when streamed).
    buffer = ""
    tool_calls_acc = {}
    usage_seen = None
    try:
        for chunk in stream:
            # The usage block arrives on its own final chunk (only when
            # stream_options.include_usage was sent — see _create_completion),
            # and that chunk has an empty choices list, so it has to be read
            # before the choices guard below skips it.
            chunk_usage = _usage_summary(getattr(chunk, "usage", None))
            if chunk_usage:
                usage_seen = chunk_usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if getattr(delta, "content", None):
                buffer += delta.content
                yield {"type": "token", "text": delta.content}
            if getattr(delta, "tool_calls", None):
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}
                    if tc_delta.id:
                        tool_calls_acc[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_calls_acc[idx]["function"]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_calls_acc[idx]["function"]["arguments"] += tc_delta.function.arguments
    except Exception as e:
        # A provider's own stream can break mid-response — confirmed on
        # NVIDIA: a malformed SSE event with no JSON body, which the
        # openai SDK doesn't guard against and raises JSONDecodeError
        # straight out of chunk iteration. This is a different failure
        # mode from the one _create_completion guards: that only covers
        # the initial request, not the body actually arriving afterward.
        # Log it fully, but keep whatever partial content/tool-call we
        # already have instead of losing it outright — that matters most
        # when a tool call already completed and only the follow-up reply
        # got cut off.
        logger.exception(
            "Stream from provider '%s' broke mid-response: %d chars buffered, %d tool call(s) in progress",
            provider, len(buffer), len(tool_calls_acc),
        )
        if not buffer and not tool_calls_acc:
            buffer = f"(No response — the connection to {provider} was interrupted before anything came back. Please try again.)"

    if usage_seen:
        # The provider's own count of what this request cost — the only exact
        # figure available, and the one place a prompt-cache hit is observable.
        # Logged always (so a provider that isn't caching, or isn't reporting,
        # is diagnosable from the log alone) and forwarded for display.
        for key in ("prompt_tokens", "completion_tokens", "cached_tokens"):
            _turn_usage[key] += usage_seen[key]
        _turn_usage["reported"] += 1
        logger.info(
            "Usage for provider '%s': prompt=%d cached=%d completion=%d "
            "(turn so far: %d requests, %d prompt, %d completion)",
            provider, usage_seen["prompt_tokens"], usage_seen["cached_tokens"],
            usage_seen["completion_tokens"], _turn_usage["requests"],
            _turn_usage["prompt_tokens"], _turn_usage["completion_tokens"],
        )
        # `context_tokens` is this request's prompt size — what the model
        # actually read this time, which is the number worth putting in front
        # of the user. It is NOT the whole conversation: history windowing
        # means only a slice of it is sent.
        yield {"type": "usage", "provider": provider,
               "context_tokens": usage_seen["prompt_tokens"],
               **usage_seen, "turn": dict(_turn_usage)}

    tool_calls_list = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())] if tool_calls_acc else None
    if buffer:
        print('Agent: ' + buffer)

    if tool_calls_list:
        # A provider that streams a tool call without an id would leave
        # id=None on both the assistant message and its tool response, and
        # OpenAI-compatible APIs reject null ids. Synthesize stable ones so
        # the two sides always match.
        for i, tc in enumerate(tool_calls_list):
            if not tc["id"]:
                tc["id"] = f"call_{i}"

        assistant_msg = {
            "role": "assistant",
            "provider": provider,
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": tc["type"],
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"]
                    }
                }
                for tc in tool_calls_list
            ]
        }
        # Keep any text the model streamed before its tool calls — dropping
        # it would desync the saved conversation from what the user saw.
        if buffer:
            assistant_msg["content"] = buffer
        # Saved with the conversation so the real counts are still there after
        # a reload instead of reverting to the client's estimate. Only set when
        # the provider actually reported, and stripped before any request goes
        # out (_INTERNAL_MESSAGE_KEYS).
        if usage_seen:
            assistant_msg["usage"] = usage_seen
        agent_messages.append(assistant_msg)

        # The assistant message above declares every requested call, and an
        # OpenAI-compatible API rejects any history where a tool_calls id
        # has no matching tool response — which would permanently break the
        # conversation, since the full history is resent every turn. So run
        # every call (models often request several at once), turn any
        # failure into an error result instead of letting it escape, and
        # hand the last result to the recursive turn that asks the model to
        # continue.
        last = len(tool_calls_list) - 1
        for i, tc in enumerate(tool_calls_list):
            command_name = tc["function"]["name"]
            args_dict = None
            result = None
            try:
                args_dict = json.loads(repair_json(tc["function"]["arguments"]))
                # MCP (and malformed) tool calls can arrive with a
                # non-object payload; keep _run_tool, which assumes a dict,
                # crash-free.
                if not isinstance(args_dict, dict):
                    args_dict = {}
            except Exception as e:
                result = f"Error: couldn't parse arguments for '{command_name}': {e}"
                logger.warning(
                    "Tool call args unparseable for '%s': %.500r",
                    command_name, tc["function"]["arguments"], exc_info=True,
                )
            if args_dict is not None:
                yield {"type": "tool_call", "name": command_name, "arguments": args_dict}
                bash_approved = False
                if command_name == 'run_bash_command':
                    # The approval gate. Deliberately here rather than inside
                    # _run_tool: asking the user means emitting an event and
                    # then blocking, and only the generator can do the first
                    # half. The model has no say in any of this and isn't
                    # consulted — see src/approvals.py.
                    verdict = approvals.check(args_dict.get('command'))
                    if verdict == approvals.ALLOWED:
                        bash_approved = True
                    elif verdict == approvals.NO_COMMAND:
                        result = approvals.denial_message(approvals.NO_COMMAND)
                    elif not approvals.is_interactive():
                        result = approvals.denial_message("not_interactive")
                    else:
                        req = approvals.open_request(args_dict.get('command'))
                        yield {"type": "approval_request", **req.as_event()}
                        # Blocks this turn until the user answers, the request
                        # times out, or it's abandoned. In the web UI the answer
                        # arrives on another thread via POST /api/approval; in
                        # the CLI the consumer answers before resuming us, so
                        # the wait returns immediately.
                        decision = approvals.wait(req)
                        bash_approved = approvals.was_approved(decision)
                        yield {"type": "approval_resolved", "id": req.id,
                               "decision": decision, "approved": bash_approved}
                        if not bash_approved:
                            result = approvals.denial_message(decision)
                if result is None:
                    try:
                        result = _run_tool(command_name, args_dict, bash_approved=bash_approved)
                    except Exception as e:
                        logger.exception("Tool '%s' raised with args=%.500r", command_name, args_dict)
                        result = f"Error running tool '{command_name}': {e}"
            if not isinstance(result, str):
                result = "" if result is None else str(result)
            if i < last:
                yield {"type": "tool_result", "name": command_name, "result": result}
                agent_messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": command_name,
                    "content": result
                })
            else:
                yield from agent_stream(tool_input=result, tool_id=tc["id"], tool_name=command_name)
        return

    final_msg = {
        "role": "assistant",
        "provider": provider,
        "content": buffer,
    }
    if usage_seen:
        final_msg["usage"] = usage_seen
    agent_messages.append(final_msg)


def agent(user_input=None, system_input=None, tool_input=None, tool_id=None, tool_name=None):
    """Non-streaming entry point: drains agent_stream() and returns the
    full conversation."""
    for _ in agent_stream(user_input=user_input, system_input=system_input,
                          tool_input=tool_input, tool_id=tool_id, tool_name=tool_name):
        pass
    return agent_messages


# api_complete() used to live here: a stateless passthrough that forwarded the
# caller's whole message array straight to the provider chain. The /v1 endpoint
# now runs a real agent turn against a stored per-user conversation instead
# (see v1_chat_completions in Flask/main.py), so nothing needed it any more.