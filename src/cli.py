"""FreeClaw CLI — talks to the same agent.agent() used by the web app."""

import sys
import os
import json
from contextlib import contextmanager

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.realpath(__file__)), ".env"))

print("\033[38;5;242m  loading FreeClaw…\033[0m", flush=True)

# Silence agent.py's debug prints during import
with open(os.devnull, 'w') as devnull:
    _real_stdout = sys.stdout
    sys.stdout = devnull
    import src.agent as agent
    sys.stdout = _real_stdout

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

def print_banner():
    print()
    print(clr("  ███████╗██████╗ ███████╗███████╗ ██████╗██╗      █████╗ ██╗    ██╗", GREEN, BOLD))
    print(clr("  ██╔════╝██╔══██╗██╔════╝██╔════╝██╔════╝██║     ██╔══██╗██║    ██║", GREEN, BOLD))
    print(clr("  █████╗  ██████╔╝█████╗  █████╗  ██║     ██║     ███████║██║ █╗ ██║", GREEN, BOLD))
    print(clr("  ██╔══╝  ██╔══██╗██╔══╝  ██╔══╝  ██║     ██║     ██╔══██║██║███╗██║", GREEN, BOLD))
    print(clr("  ██║     ██║  ██║███████╗███████╗╚██████╗███████╗██║  ██║╚███╔███╔╝", GREEN, BOLD))
    print(clr("  ╚═╝     ╚═╝  ╚═╝╚══════╝╚══════╝ ╚═════╝╚══════╝╚═╝  ╚═╝ ╚══╝╚══╝ ", GREEN, BOLD))
    print()
    print(clr("  search the web · run bash · control smart home · write files", GREY))
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

def render_tool_blocks(new_messages):
    """Render only the tool-call/result blocks contained in a slice of
    freshly-added conversation messages. Assistant *text* is skipped here
    since it has already been streamed live to the terminal."""
    tool_results = {}
    for msg in new_messages:
        if msg.get("role") == "tool":
            tool_results[msg.get("tool_call_id", "")] = {
                "content": msg.get("content", ""),
                "name":    msg.get("name", ""),
            }

    for msg in new_messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                render_tool_call(tc, tool_results)

def render_conversation(conversation):
    tool_results = {}
    for msg in conversation:
        if msg.get("role") == "tool":
            tool_results[msg.get("tool_call_id", "")] = {
                "content": msg.get("content", ""),
                "name":    msg.get("name", ""),
            }

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

def main():
    try:
        print_banner()
    except Exception:
        print("FreeClaw CLI\n")

    with silence():
        agent.reset()

    last_rendered_index = 0

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
            last_rendered_index = 0
            print(clr("  conversation reset\n", GREY))
            continue

        if user_input.lower() == "/startapi":
            os.system("sudo systemctl start FreeClawAPI.service")
            print(clr("  API started on port 8080\n", GREY))
            continue

        if user_input.lower() == "/stopapi":
            os.system("sudo systemctl stop FreeClawAPI.service")
            print(clr("  API stopped\n", GREY))
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

if __name__ == "__main__":
    main()