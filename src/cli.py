"""FreeClaw CLI — talks to the same agent.agent() used by the web app."""

import sys
import os
import json
from contextlib import contextmanager

from dotenv import load_dotenv
# .env lives at the project root, one level above src/
load_dotenv(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", ".env"))

# Flag file that enables the OpenAI-compatible API (see api_is_enabled in
# Flask/main.py) — toggled here by /startapi and /stopapi.
API_FLAG = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "Flask", ".api_enabled")

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
    print(clr("  /reset  — clear conversation      /exit — quit", GREY))
    print()

def render_tool_call(tc, tool_results):
    fn   = tc.get("function", {})
    name = fn.get("name", tc.get("id", "unknown"))
    args = fn.get("arguments", "")
    result_obj  = tool_results.get(tc.get("id", ""))
    result_text = result_obj.get("content", "(no result)") if result_obj else "(no result)"
    is_err      = isinstance(result_text, str) and result_text.lower().startswith("error")

    try:
        args_pretty = json.dumps(json.loads(args), indent=2)
    except Exception:
        args_pretty = args or ""

    print(clr(f"  ┌─ tool: {name}", CYAN))
    for line in args_pretty.splitlines():
        print(clr(f"  │  {line}", GREY))
    result_color = RED if is_err else GREY
    for line in str(result_text).splitlines():
        print(clr(f"  └─ {line}", result_color))
    print()

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

def render_tool_blocks(new_messages):
    """Render only the tool-call/result blocks contained in a slice of
    freshly-added conversation messages. Assistant *text* is skipped here
    since it has already been streamed live to the terminal."""
    tool_results = collect_tool_results(new_messages)
    for msg in new_messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                render_tool_call(tc, tool_results)

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
        # them explicitly here to make sure they're always available.
        try:
            agent.refresh_tools()
        except Exception:
            pass
        users.activate_session(username)

    print(clr(f"  logged in as {username}\n", GREEN))

    had_title = users.load_conversation(username).get("title") not in (None, "", "New chat")

    # A fresh reset() leaves just the system message; more than that means
    # there's a real conversation to resume — show its scrollback.
    conversation = agent.agent_messages
    if len(conversation) > 1:
        print(clr("  — resuming previous conversation —\n", GREY))
        render_conversation(conversation)
    last_rendered_index = len(conversation)

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
            last_rendered_index = 0
            print(clr("  conversation reset\n", GREY))
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

        agent_label_printed = False
        buffer = ""

        try:
            for event in stream_silent(agent.agent_stream(user_input=user_input)):
                etype = event.get("type")

                if etype == "token":
                    if not agent_label_printed:
                        sys.stdout.write(clr("agent › ", GREEN, BOLD))
                        agent_label_printed = True
                    sys.stdout.write(event.get("text", ""))
                    sys.stdout.flush()
                    buffer += event.get("text", "")

                elif etype == "tool_call":
                    if agent_label_printed:
                        print()  # close out any partial streamed line
                        agent_label_printed = False
                        buffer = ""
                    name = event.get("name", "unknown")
                    print(clr(f"  … calling tool: {name}", GREY))

                elif etype == "tool_result":
                    name = event.get("name", "unknown")
                    print(clr(f"  ✓ {name} done", GREY))

        except Exception as exc:
            if agent_label_printed:
                print()
            print(clr(f"  error: {exc}\n", RED))
            continue

        if agent_label_printed:
            print()
            print()

        conversation = agent.agent_messages
        render_tool_blocks(conversation[last_rendered_index:])
        last_rendered_index = len(conversation)

        title = None if had_title else users.derive_title(conversation)
        users.save_conversation(username, conversation, title=title)
        if title:
            had_title = True

if __name__ == "__main__":
    main()