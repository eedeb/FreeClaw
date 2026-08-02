"""OpenAI Responses API, translated to and from chat completions.

FreeClaw speaks one dialect internally: OpenAI chat completions. The provider
chain, the saved conversations, the tool loop, and the frontend all assume it,
and every provider in Settings → Providers accepts it.

OpenAI is the exception, in one specific way: it refuses function tools
combined with reasoning on /v1/chat/completions. Tools or thinking, not both —
unless the request goes to /v1/responses instead. Since FreeClaw sends tools on
every call, that would mean running a reasoning model with its reasoning turned
off, which is the worst point on the effort curve. Hence this module.

A provider with `api: "responses"` gets its request translated on the way out
and its stream translated back on the way in. Nothing upstream of
_create_completion learns the difference — same kwargs in, same chunk shapes
out, same exceptions on failure, so the fallback chain, the cooldown tracking
and the token accounting all keep working unchanged. In particular:

  * The conversation stays ours. `store=False` and no `previous_response_id`,
    because FreeClaw windows its own history, repairs orphaned tool pairings,
    and can hand the very next request to a different provider — all of which
    are edits to a history OpenAI would otherwise think it owned.
  * The tools stay ours. Only the function tools FreeClaw already declares get
    sent, never the server-side hosted ones (web search, code interpreter,
    file search), so every call still goes through the approval layer.
  * The thinking rides along. Reasoning comes back encrypted, is parked on the
    assistant message, and is replayed on the next request of the same turn.
    Without that the model re-derives its plan on every tool hop, and a tool
    loop is mostly tool hops.
"""

from types import SimpleNamespace

from src.logging_setup import get_logger

logger = get_logger(__name__)


# How hard a `responses` provider is asked to think. "low" rather than the
# API's default because nearly all of the benefit is in the first step: on
# published effort curves none→low is worth tens of points and low→medium a
# handful, while cost and latency keep climbing. Raise it here if a model is
# being asked to do something genuinely hard — it's a constant rather than a
# per-provider setting because the answer is the same for every model that
# reaches this path.
REASONING_EFFORT = "low"

# Ask for a readable summary of the thinking alongside the encrypted blob. The
# blob is opaque by design — it exists to be handed back, not read — so without
# this the reasoning block in the UI would have nothing to put in it.
REASONING_SUMMARY = "auto"


def _content_text(content):
    """A message's content as plain text.

    The chain's messages are text-only: images go to the separate vision model
    in agent.py, which describes them into text well before anything reaches
    here. That's why dropping a non-text block is safe."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("text"):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def _tools_to_responses(tools):
    """Chat-completions tool schemas flattened into Responses' shape.

    Chat completions nests the definition under a "function" key; Responses
    puts the same fields at the top level. strict=False is explicit because
    Responses otherwise defaults function tools to strict mode, which demands
    every schema set additionalProperties:false and mark every property
    required. FreeClaw's schemas don't, and the mismatch is a 400."""
    out = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if not fn:
            continue
        out.append({
            "type": "function",
            "name": fn.get("name"),
            "description": fn.get("description") or "",
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            "strict": False,
        })
    return out


def _messages_to_input(messages):
    """The conversation as a flat Responses `input` list.

    Responses has no `messages` array and no `tool` role. A turn is a sequence
    of items where a tool call and its result are two separate entries linked
    by call_id, and the reasoning that led to them is a third. Order matters:
    the reasoning has to precede the calls it produced, or the model gets its
    own conclusions without the thinking that reached them."""
    items = []
    for m in messages:
        role = m.get("role")

        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id"),
                "output": m.get("content") or "",
            })
            continue

        if role == "assistant":
            # Encrypted reasoning from the request that produced this message,
            # replayed exactly as it was issued. Opaque to us on purpose.
            for item in m.get("reasoning_items") or []:
                items.append(item)
            text = _content_text(m.get("content"))
            if text:
                items.append({"role": "assistant", "content": text})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": tc.get("id"),
                    "name": fn.get("name"),
                    "arguments": fn.get("arguments") or "{}",
                })
            continue

        text = _content_text(m.get("content"))
        if text:
            items.append({
                "role": "system" if role == "system" else "user",
                "content": text,
            })
    return items


def build_request(call_kwargs):
    """A chat-completions request rebuilt as a Responses one.

    Assembled key by key from what we recognise rather than copied and patched,
    so nothing chat-only can leak through — not stream_options, and not the
    internal bookkeeping keys (provider, usage, reasoning) that hang off our
    own messages and that no provider should ever see."""
    request = {
        "model": call_kwargs.get("model"),
        "input": _messages_to_input(call_kwargs.get("messages") or []),
        "stream": True,
        # Ours to keep — see the module docstring.
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": REASONING_EFFORT, "summary": REASONING_SUMMARY},
    }
    tools = _tools_to_responses(call_kwargs.get("tools"))
    if tools:
        request["tools"] = tools
    if call_kwargs.get("max_tokens"):
        request["max_output_tokens"] = call_kwargs["max_tokens"]
    # temperature and top_p are dropped deliberately: reasoning models reject
    # them outright, and the chain only ever sends them at their defaults.
    return request


def _chunk(content=None, reasoning=None, tool_calls=None):
    """One chat-completions-shaped stream chunk.

    Only the fields agent_stream actually reads off a chunk are present; it
    reaches everything through getattr with a default, so there's no need to
    reproduce the SDK's full model."""
    delta = SimpleNamespace(content=content, reasoning=reasoning,
                            tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


def _tool_call_delta(index, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index, id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _usage_dict(usage):
    """Responses usage under the chat-completions spelling.

    prompt/completion are already read under both names upstream; the cache
    figure is the one that would otherwise be lost, since Responses nests it
    under input_tokens_details rather than prompt_tokens_details."""
    if usage is None:
        return None
    details = getattr(usage, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", None)
    if cached is None and isinstance(details, dict):
        cached = details.get("cached_tokens")
    return {
        "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
        "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
        "prompt_tokens_details": {"cached_tokens": cached or 0},
    }


def _reasoning_items(response):
    """The encrypted reasoning items from a finished response, as plain dicts
    ready to be replayed in a later request's input.

    Kept whole rather than picked apart: the encrypted payload means nothing to
    us and the API wants the item back exactly as it was issued. An item with
    nothing encrypted on it is only the summary already streamed to the UI, so
    replaying it would buy nothing and cost tokens."""
    items = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "reasoning":
            continue
        as_dict = (item.model_dump(exclude_none=True)
                   if hasattr(item, "model_dump") else dict(item))
        if as_dict.get("encrypted_content"):
            items.append(as_dict)
    return items


def translate_stream(events):
    """Responses stream events rewritten as chat-completions chunks."""
    # Responses numbers every output item — reasoning, text, each function
    # call — in one sequence, while the chat side indexes tool calls from zero
    # among themselves. Map between the two rather than passing output_index
    # straight through, or the first tool call of a response that opened with
    # reasoning arrives as index 1 and desyncs the accumulator.
    tool_index = {}

    for event in events:
        etype = getattr(event, "type", "")

        if etype == "response.output_text.delta":
            yield _chunk(content=getattr(event, "delta", "") or "")

        elif etype == "response.reasoning_summary_text.delta":
            yield _chunk(reasoning=getattr(event, "delta", "") or "")

        elif etype == "response.output_item.added":
            item = getattr(event, "item", None)
            if getattr(item, "type", None) == "function_call":
                index = len(tool_index)
                tool_index[getattr(event, "output_index", index)] = index
                # Name and id arrive once, here; the arguments stream in
                # separately below and are appended by the caller.
                yield _chunk(tool_calls=[_tool_call_delta(
                    index,
                    call_id=getattr(item, "call_id", None),
                    name=getattr(item, "name", None),
                )])

        elif etype == "response.function_call_arguments.delta":
            index = tool_index.get(getattr(event, "output_index", None))
            if index is not None:
                yield _chunk(tool_calls=[_tool_call_delta(
                    index, arguments=getattr(event, "delta", "") or "")])

        elif etype in ("response.completed", "response.incomplete"):
            # Both carry a finished response object. `incomplete` means the
            # output was cut short (usually max_output_tokens) — whatever
            # streamed before it is still real, so it's collected the same way
            # rather than discarded.
            response = getattr(event, "response", None)
            # No choices, so the caller's `if not chunk.choices: continue`
            # skips it after reading usage — exactly how a chat-completions
            # usage chunk behaves. reasoning_items rides along on the same
            # chunk because there's nowhere else for it to go.
            yield SimpleNamespace(
                choices=[],
                usage=_usage_dict(getattr(response, "usage", None)),
                reasoning_items=_reasoning_items(response),
            )

        elif etype == "response.failed":
            response = getattr(event, "response", None)
            error = getattr(response, "error", None)
            raise RuntimeError(
                f"Responses stream failed: {getattr(error, 'message', None) or error}")


def create(client, call_kwargs):
    """Run a chat-completions-shaped request against the Responses API and
    return something the caller can consume as if it were a chat-completions
    stream.

    Deliberately not a generator: the request has to be issued now, inside
    _create_completion's try, so a failure is caught there and the next
    provider gets tried. A generator would defer it until the first chunk was
    pulled — long after the fallback chain had moved on."""
    if not call_kwargs.get("stream"):
        # The chain only ever streams (see agent_stream); a non-streaming
        # caller would need a different translation and doesn't exist.
        raise ValueError("The Responses translation supports streaming calls only.")
    request = build_request(call_kwargs)
    logger.info(
        "Responses request: model=%s items=%d tools=%d effort=%s",
        request.get("model"), len(request["input"]),
        len(request.get("tools") or []), REASONING_EFFORT,
    )
    return translate_stream(client.responses.create(**request))
