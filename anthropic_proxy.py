"""Anthropic Messages API <-> OpenAI chat-completions translation.

Claude Code (and other Anthropic-SDK tools) speak POST /v1/messages, not the
OpenAI shape. This module translates a /v1/messages request into an OpenAI
chat-completions request the router serves, and translates the response back —
including tool use and streaming (both of which Claude Code requires). It is
pure translation; payment (ecash) is done by the caller in serve_ecash.
"""
from __future__ import annotations

import json

import re

_TIER_FALLBACK = {  # used only if the live model list is unavailable
    "opus": "anthropic/claude-opus-4.5",
    "sonnet": "anthropic/claude-sonnet-4.5",
    "haiku": "anthropic/claude-haiku-4.5",
}


def _ver(mid: str):
    m = re.search(r"(\d+)(?:\.(\d+))?", mid)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


def map_model(m: str, available: set | None = None) -> str:
    """Map an Anthropic model name (what Claude Code sends, e.g.
    'claude-sonnet-4-5-20250929') to a CURRENTLY-VALID router/OpenRouter id.
    Resolves against the live model list when given, since the catalog changes
    (claude-3.5-* is already retired); falls back to a per-tier default."""
    if not m:
        m = "claude-sonnet"
    if "/" in m:
        return m
    ml = m.lower()
    tier = "opus" if "opus" in ml else "haiku" if "haiku" in ml else "sonnet"
    cands = sorted((x for x in (available or ()) if not x.startswith("~")
                    and "claude" in x and tier in x), key=_ver)
    if cands:
        vm = re.search(r"(\d+)[-.](\d+)", ml)  # e.g. sonnet-4-5 / sonnet-4.5
        if vm:
            want = f"claude-{tier}-{vm.group(1)}.{vm.group(2)}"
            for c in cands:
                if c.endswith(want):
                    return c
        return cands[-1]  # newest of that tier
    return _TIER_FALLBACK[tier]


def error_event(msg: str) -> str:
    """An Anthropic SSE error stream (so a bad model/upstream surfaces to the
    client instead of an empty response)."""
    return (_sse("error", {"type": "error",
                           "error": {"type": "api_error", "message": msg}})
            + _sse("message_stop", {"type": "message_stop"}))


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def to_openai(req: dict) -> dict:
    """Anthropic Messages request -> OpenAI chat-completions request."""
    msgs = []
    system = req.get("system")
    if system:
        msgs.append({"role": "system", "content": _text_of(system)})

    for m in req.get("messages", []):
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        # content is a list of blocks
        text_parts, tool_calls, tool_results = [], [], []
        for b in content:
            t = b.get("type")
            if t == "text":
                text_parts.append(b.get("text", ""))
            elif t == "tool_use":  # assistant asked to call a tool
                tool_calls.append({
                    "id": b.get("id"), "type": "function",
                    "function": {"name": b.get("name"),
                                 "arguments": json.dumps(b.get("input", {}))},
                })
            elif t == "tool_result":  # user returns a tool's output
                tool_results.append({
                    "role": "tool", "tool_call_id": b.get("tool_use_id"),
                    "content": _text_of(b.get("content", "")),
                })
        if role == "assistant":
            a = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                a["tool_calls"] = tool_calls
            msgs.append(a)
        else:  # user: emit tool results as separate tool messages, then any text
            msgs.extend(tool_results)
            if text_parts:
                msgs.append({"role": "user", "content": "".join(text_parts)})

    out = {"model": map_model(req.get("model", "")), "messages": msgs,
           "max_tokens": req.get("max_tokens", 4096)}
    for k in ("temperature", "top_p", "stop_sequences", "stream"):
        if k in req:
            out["stop" if k == "stop_sequences" else k] = req[k]
    if req.get("tools"):
        out["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object"})}}
            for t in req["tools"]]
        tc = req.get("tool_choice")
        if isinstance(tc, dict):
            kind = tc.get("type")
            out["tool_choice"] = ({"type": "function", "function": {"name": tc.get("name")}}
                                  if kind == "tool" else
                                  "required" if kind == "any" else "auto")
    return out


_STOP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
         "content_filter": "end_turn", None: "end_turn"}


def to_anthropic(resp: dict, model: str) -> dict:
    """OpenAI chat-completions response -> Anthropic Messages response."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks = []
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except Exception:
            inp = {}
        blocks.append({"type": "tool_use", "id": tc.get("id"),
                       "name": fn.get("name"), "input": inp})
    usage = resp.get("usage") or {}
    return {
        "id": resp.get("id", "msg_0"), "type": "message", "role": "assistant",
        "model": model, "content": blocks or [{"type": "text", "text": ""}],
        "stop_reason": _STOP.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)},
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def stream_anthropic(openai_lines, model: str, msg_id: str = "msg_stream"):
    """Translate an OpenAI SSE line iterator into Anthropic SSE strings.

    Handles a single text content block plus tool_use blocks (input streamed as
    input_json_delta). Yields ready-to-write SSE strings.
    """
    yield _sse("message_start", {"type": "message_start", "message": {
        "id": msg_id, "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0}}})
    yield _sse("ping", {"type": "ping"})

    idx = -1                       # current anthropic content block index
    text_open = False
    tools: dict = {}               # openai tool_call index -> anthropic block index
    finish = "end_turn"
    out_tokens = 0

    for line in openai_lines:
        if not line.startswith("data: "):
            s = line.strip()
            if s.startswith("{") and '"error"' in s:  # upstream error, not SSE
                try:
                    e = json.loads(s).get("error", {})
                    msg = e.get("message", s) if isinstance(e, dict) else str(e)
                except Exception:
                    msg = s
                yield _sse("error", {"type": "error",
                                     "error": {"type": "api_error", "message": msg}})
                yield _sse("message_stop", {"type": "message_stop"})
                return
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except Exception:
            continue
        ch = (chunk.get("choices") or [{}])[0]
        delta = ch.get("delta") or {}
        if ch.get("finish_reason"):
            finish = _STOP.get(ch["finish_reason"], "end_turn")
        if (chunk.get("usage") or {}).get("completion_tokens"):
            out_tokens = chunk["usage"]["completion_tokens"]

        if delta.get("content"):
            if not text_open:
                idx += 1
                text_open = True
                yield _sse("content_block_start", {"type": "content_block_start",
                    "index": idx, "content_block": {"type": "text", "text": ""}})
            yield _sse("content_block_delta", {"type": "content_block_delta",
                "index": idx, "delta": {"type": "text_delta", "text": delta["content"]}})

        for tc in delta.get("tool_calls") or []:
            oi = tc.get("index", 0)
            if oi not in tools:
                if text_open:
                    yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
                    text_open = False
                idx += 1
                tools[oi] = idx
                fn = tc.get("function") or {}
                yield _sse("content_block_start", {"type": "content_block_start",
                    "index": idx, "content_block": {"type": "tool_use",
                        "id": tc.get("id", f"toolu_{idx}"), "name": fn.get("name", ""),
                        "input": {}}})
            args = (tc.get("function") or {}).get("arguments")
            if args:
                yield _sse("content_block_delta", {"type": "content_block_delta",
                    "index": tools[oi], "delta": {"type": "input_json_delta",
                                                  "partial_json": args}})

    if text_open or tools:
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
    yield _sse("message_delta", {"type": "message_delta",
        "delta": {"stop_reason": finish, "stop_sequence": None},
        "usage": {"output_tokens": out_tokens}})
    yield _sse("message_stop", {"type": "message_stop"})
