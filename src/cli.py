"""FreeClaw CLI — talks to the same agent.agent() used by the web app."""

import sys
import os
import json
import time
from contextlib import contextmanager

from dotenv import load_dotenv
# .env lives at the project root, one level above src/
load_dotenv(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", ".env"))

# Flag file that enables the OpenAI-compatible API (see api_is_enabled in
# Flask/main.py) — toggled here by /startapi and /stopapi.
API_FLAG = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "Flask", ".api_enabled")


def _prepare_windows_console():
    """Make a Windows console able to render what this CLI prints.

    Two separate problems, both silent-looking and both fatal to readability:

    * ANSI. Every colour below is an escape sequence, and a console only
      interprets those once ENABLE_VIRTUAL_TERMINAL_PROCESSING is on. Windows
      Terminal sets it; conhost — still what you get from `cmd` in a lot of
      places — does not, and prints `←[38;5;154m` in front of every line
      instead. The flag is per-handle and sticks for the process.
    * Encoding. The banner is box-drawing characters and the prompts use
      `…`/`→`. Attached to a console Python handles those, but redirected to a
      pipe or a file it falls back to the locale codepage, where they raise
      UnicodeEncodeError and take the CLI down mid-print.

    Best-effort throughout: an older console that refuses the flag should lose
    the colours, not the CLI.
    """
    if os.name != "nt":
        return

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            if handle in (0, -1):
                continue
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue  # redirected, not a console — nothing to switch on
            kernel32.SetConsoleMode(
                handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        pass


_prepare_windows_console()

print("\033[38;5;242m  loading FreeClaw…\033[0m", flush=True)


@contextmanager
def silence():
    """Suppress stdout for the duration — used to mute agent debug prints."""
    with open(os.devnull, 'w') as devnull:
        old = sys.stdout
        sys.stdout = devnull
        try:
            yield
        finally:
            sys.stdout = old


with silence():
    import src.agent as agent
    import src.approvals as approvals
    import src.users as users

# ── ANSI colours ────────────────────────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[38;5;154m"
CYAN   = "\033[38;5;81m"
YELLOW = "\033[38;5;220m"
RED    = "\033[38;5;203m"
GREY   = "\033[38;5;242m"

def clr(text, *codes):
    return "".join(codes) + str(text) + RESET

def print_banner():
    print()
    print(clr("  ███████╗██████╗ ███████╗███████╗ ██████╗██╗      █████╗ ██╗    ██╗", GREEN, BOLD))
    print(clr("  ██╔════╝██╔══██╗██╔════╝██╔════╝██╔════╝██║     ██╔══██╗██║    ██║", GREEN, BOLD))
    print(clr("  █████╗  ██████╔╝█████╗  █████╗  ██║     ██║     ███████║██║ █╗ ██║", GREEN, BOLD))
    print(clr("  ██╔══╝  ██╔══██╗██╔══╝  ██╔══╝  ██║     ██║     ██╔══██║██║███╗██║", GREEN, BOLD))
    print(clr("  ██║     ██║  ██║███████╗███████╗╚██████╗███████╗██║  ██║╚███╔███╔╝", GREEN, BOLD))
    print(clr("  ╚═╝     ╚═╝  ╚═╝╚══════╝╚══════╝ ╚═════╝╚══════╝╚═╝  ╚═╝ ╚══╝╚══╝ ", GREEN, BOLD))
    print()
    print(clr("  search the web · run bash · connect MCP tools · write files", GREY))
    print(clr("  /reset — clear conversation   /verbose — full tool output   /exit — quit", GREY))
    print()

# Full tool output is off by default — a single search result is thousands of
# lines of scraped page text, which buries everything else. Toggle with
# /verbose when you actually need to inspect what a tool returned.
VERBOSE = False

# How much of a tool result to show when verbose is on, so even then one
# scrape can't run away with the scrollback.
VERBOSE_MAX_LINES = 40


def shorten(value, limit):
    """Collapse whitespace onto one line and cap the length."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def fmt_args(args):
    """Tool arguments as one short line: query="weather"  site=bbc.com"""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return shorten(args, 72)
    if not isinstance(args, dict) or not args:
        return ""
    # Optional arguments the model left blank are noise, not information.
    return "  ".join(
        f"{k}={shorten(v if isinstance(v, str) else json.dumps(v), 44)}"
        for k, v in args.items()
        if v not in (None, "", [], {})
    )


def fmt_size(value):
    n = len(str(value))
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


def is_error(result):
    return isinstance(result, str) and result.strip().lower().startswith("error")


def print_tool_result(name, result, seconds=None):
    """One line per result: what ran, how long, how much came back.

    Errors print in full regardless of VERBOSE — a failed tool call is the one
    thing you always need to see during a run.
    """
    err = is_error(result)
    timing = f"{seconds:.1f}s · " if seconds is not None else ""
    mark, color = ("✗", RED) if err else ("✓", GREY)
    print(clr(f"  {mark} {name}  {timing}{fmt_size(result)}", color))
    if err or VERBOSE:
        lines = str(result).splitlines()
        for line in lines[:VERBOSE_MAX_LINES]:
            print(clr(f"      {line}", GREY))
        if len(lines) > VERBOSE_MAX_LINES:
            print(clr(f"      … {len(lines) - VERBOSE_MAX_LINES} more lines", GREY))


def ask_approval(event):
    """Ask on the terminal whether a bash command may run, and record the
    answer.

    Called from the event loop, which means the agent generator is suspended at
    its `yield` and hasn't started waiting yet — so by the time it resumes,
    approvals.resolve() has already stored the decision and its wait returns
    at once. Same code path the web UI drives from another thread; the CLI just
    happens to answer before the wait begins.

    An interrupt or EOF at this prompt counts as a refusal — the safe reading
    of someone hitting Ctrl-C at "run this command?"."""
    print()
    print(clr("  ⚠ the agent wants to run a shell command", RED, BOLD))
    print(clr(f"    {event.get('command', '')}", YELLOW))
    program = event.get("program")
    options = [("1", "once", "run it this once"),
               ("2", "always_exact", "always allow this exact command")]
    if program:
        options.append(("3", "always_program", f'always allow all "{program}" commands'))
    options.append(("n", "deny", "don't run it"))
    for key, _, label in options:
        print(clr(f"    {key}) {label}", CYAN))
    by_key = {key: decision for key, decision, _ in options}

    decision = "deny"
    while True:
        try:
            answer = input(clr("  choose › ", YELLOW, BOLD)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not answer:
            continue
        if answer in by_key:
            decision = by_key[answer]
            break
        print(clr("    not a valid option.", RED))

    ok, error = approvals.resolve(event.get("id"), decision)
    if not ok:
        print(clr(f"    couldn't record that: {error}", RED))
    elif decision == "deny":
        print(clr("    denied — not running it", GREY))
    elif decision == "always_exact":
        print(clr("    approved, and saved so it won't ask again", GREY))
    elif decision == "always_program":
        print(clr(f'    approved, and saved: all "{program}" commands', GREY))
    else:
        print(clr("    approved for this one call", GREY))
    print()


def render_tool_call(tc, tool_results):
    """Compact replay of a stored tool call — used for resumed scrollback."""
    fn = tc.get("function", {})
    name = fn.get("name", tc.get("id", "unknown"))
    result_obj = tool_results.get(tc.get("id", ""))
    result = result_obj.get("content", "(no result)") if result_obj else "(no result)"

    args = fmt_args(fn.get("arguments", ""))
    print(clr(f"  ⟶ {name}", CYAN) + (clr(f"  {args}", GREY) if args else ""))
    print_tool_result(name, result)

def stream_silent(gen):
    """Advance a generator one step at a time, suppressing stdout only
    while the generator itself is *running* (i.e. while agent.py's debug
    prints would fire). stdout is restored before each event is handed
    back to the caller, so the caller's own prints (streamed tokens, tool
    indicators, etc.) show up normally."""
    while True:
        with silence():
            try:
                event = next(gen)
            except StopIteration:
                return
        yield event

def collect_tool_results(messages):
    """Index tool-result messages by tool_call_id for rendering."""
    results = {}
    for msg in messages:
        if msg.get("role") == "tool":
            results[msg.get("tool_call_id", "")] = {
                "content": msg.get("content", ""),
                "name":    msg.get("name", ""),
            }
    return results

def render_conversation(conversation):
    tool_results = collect_tool_results(conversation)
    for msg in conversation:
        role = msg.get("role")
        if role in ("system", "tool", "user"):
            continue

        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                render_tool_call(tc, tool_results)

            content = msg.get("content") or ""
            if isinstance(content, list):
                content = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
            if content and content.strip():
                print(clr("agent › ", GREEN, BOLD) + content.strip())
                print()

def prompt_new_user():
    """Ask for a valid, not-yet-taken username and create it."""
    while True:
        name = input(clr("  name for new user › ", YELLOW, BOLD)).strip()
        safe = users.safe_username(name)
        if not safe:
            print(clr("  invalid name — use 1-40 letters, numbers, spaces, - or _.", RED))
            continue
        if users.user_exists(safe):
            print(clr(f"  a user named '{safe}' already exists.", RED))
            continue
        break
    with silence():
        users.create_user(safe)
    print(clr(f"  created user '{safe}'\n", GREEN))
    return safe


def resolve_user(requested):
    """Figure out which user to chat as: the name passed on the command
    line if given (creating it after confirmation if it doesn't exist
    yet), otherwise an interactive picker over existing users."""
    if requested:
        name = users.safe_username(requested)
        if not name:
            print(clr(f"  '{requested}' isn't a valid username — use 1-40 letters, numbers, spaces, - or _.\n", RED))
            sys.exit(1)
        if users.user_exists(name):
            return name
        answer = input(clr(f"  no user named '{name}' — create them? [y/N] ", YELLOW)).strip().lower()
        if answer != "y":
            print(clr("\n  bye\n", GREY))
            sys.exit(0)
        with silence():
            users.create_user(name)
        print(clr(f"  created user '{name}'\n", GREEN))
        return name

    existing = users.list_users()
    if not existing:
        print(clr("  no users yet — let's create one.", GREY))
        return prompt_new_user()

    print(clr("  who's chatting?\n", GREY))
    for i, u in enumerate(existing, 1):
        print(clr(f"  {i}) {u}", CYAN))
    print(clr(f"  {len(existing) + 1}) + add a new user\n", CYAN))

    while True:
        choice = input(clr("  choose a number or type a name › ", YELLOW, BOLD)).strip()
        if not choice:
            continue
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(existing):
                return existing[idx - 1]
            if idx == len(existing) + 1:
                return prompt_new_user()
            print(clr("  not a valid option.", RED))
            continue
        safe = users.safe_username(choice)
        if not safe:
            print(clr("  invalid name — use 1-40 letters, numbers, spaces, - or _.", RED))
            continue
        if safe in existing:
            return safe
        answer = input(clr(f"  no user named '{safe}' — create them? [y/N] ", YELLOW)).strip().lower()
        if answer == "y":
            with silence():
                users.create_user(safe)
            print(clr(f"  created user '{safe}'\n", GREEN))
            return safe


def main():
    try:
        print_banner()
    except Exception:
        print("FreeClaw CLI\n")

    requested = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else None
    username = resolve_user(requested)

    with silence():
        # Tools are only (re)loaded by agent.reset(), which activate_session()
        # skips for a returning user with an existing conversation — so load
        # them explicitly here to make sure they're always available. For this
        # user by name: which MCP servers are switched on is their choice, so
        # the catalogue to warm is theirs and not the install's.
        try:
            agent.refresh_tools(username)
        except Exception:
            pass
        # interactive=True: there's a terminal here that can answer a bash
        # approval prompt (see ask_approval).
        users.activate_session(username, interactive=True)

    print(clr(f"  logged in as {username}\n", GREEN))

    had_title = users.load_conversation(username).get("title") not in (None, "", "New chat")

    # A fresh reset() leaves just the system message; more than that means
    # there's a real conversation to resume — show its scrollback.
    conversation = agent.agent_messages
    if len(conversation) > 1:
        print(clr("  — resuming previous conversation —\n", GREY))
        render_conversation(conversation)

    while True:
        try:
            user_input = input(clr("you › ", YELLOW, BOLD)).strip()
        except (EOFError, KeyboardInterrupt):
            print(clr("\n  bye\n", GREY))
            sys.exit(0)

        if not user_input:
            continue

        if user_input.lower() == "/exit":
            print(clr("\n  bye\n", GREY))
            sys.exit(0)

        if user_input.lower() == "/reset":
            with silence():
                agent.reset()
                users.save_conversation(username, agent.get_messages(), title="New chat")
            had_title = False
            print(clr("  conversation reset\n", GREY))
            continue

        if user_input.lower() == "/verbose":
            global VERBOSE
            VERBOSE = not VERBOSE
            state = "on — tool results printed in full" if VERBOSE else "off — tool results summarised"
            print(clr(f"  verbose {state}\n", GREY))
            continue

        if user_input.lower() == "/startapi":
            open(API_FLAG, 'w').close()
            print(clr("  API enabled at /v1/chat/completions (use your FreeClaw password as the Bearer token)\n", GREY))
            continue

        if user_input.lower() == "/stopapi":
            if os.path.exists(API_FLAG):
                os.remove(API_FLAG)
            print(clr("  API disabled\n", GREY))
            continue

        streaming = False   # mid-way through an "agent › ..." line

        def close_line():
            """Finish a partially streamed line before printing anything else."""
            nonlocal streaming
            if streaming:
                print()
                streaming = False

        # agent_stream recurses once per tool round trip and emits "provider"
        # at the top of each, so counting them gives the round-trip total —
        # the number to cross-check against the proxy's.
        request_n = 0
        last_provider = None
        started_at = {}
        turn_usage = {}
        turn_start = time.monotonic()

        try:
            for event in stream_silent(agent.agent_stream(user_input=user_input)):
                etype = event.get("type")

                if etype == "provider":
                    request_n += 1
                    name = event.get("name", "?")
                    # Only worth a line when it *changes* — a silent fallback
                    # mid-turn otherwise looks identical to a normal run while
                    # quietly changing the token numbers.
                    if last_provider is not None and name != last_provider:
                        close_line()
                        print(clr(f"  ⚠ provider fell back → {name}", YELLOW))
                    last_provider = name

                elif etype == "token":
                    if not streaming:
                        sys.stdout.write(clr("agent › ", GREEN, BOLD))
                        streaming = True
                    sys.stdout.write(event.get("text", ""))
                    sys.stdout.flush()

                elif etype == "tool_call":
                    close_line()
                    name = event.get("name", "unknown")
                    started_at[name] = time.monotonic()
                    args = fmt_args(event.get("arguments"))
                    print(clr(f"  ⟶ {name}", CYAN) + (clr(f"  {args}", GREY) if args else ""))

                elif etype == "approval_request":
                    close_line()
                    ask_approval(event)

                elif etype == "usage":
                    # Exact counts from the provider (those that report them).
                    # event["turn"] already holds the running totals for every
                    # request in this turn, so take them rather than summing.
                    turn_usage = event.get("turn") or {}

                elif etype == "tool_result":
                    name = event.get("name", "unknown")
                    began = started_at.pop(name, None)
                    print_tool_result(
                        name,
                        event.get("result", ""),
                        None if began is None else time.monotonic() - began,
                    )

        except Exception as exc:
            close_line()
            print(clr(f"  error: {exc}\n", RED))
            continue

        close_line()

        # Tool activity was shown live above, so replaying the stored messages
        # here would print all of it a second time.
        conversation = agent.agent_messages

        plural = "" if request_n == 1 else "s"
        summary = (f"\n  {request_n} request{plural} · {time.monotonic() - turn_start:.1f}s"
                   + (f" · {last_provider}" if last_provider else ""))
        if turn_usage.get("reported"):
            # Exact, from the provider. Only shown when it actually reported —
            # printing "0 tokens" for an endpoint that stays silent would read
            # as a free turn rather than an unknown one.
            summary += (f" · {turn_usage['prompt_tokens']:,} in"
                        f" / {turn_usage['completion_tokens']:,} out")
            if turn_usage.get("cached_tokens"):
                summary += f" ({turn_usage['cached_tokens']:,} cached)"
        print(clr(summary, GREY))
        print()

        title = None if had_title else users.derive_title(conversation)
        users.save_conversation(username, conversation, title=title)
        if title:
            had_title = True

if __name__ == "__main__":
    main()