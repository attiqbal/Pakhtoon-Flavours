"""One interface to the AI, so the assistant can run on Claude or on ChatGPT (or any OpenAI-compatible service).

Conversations are stored in Claude's block format. `complete` returns blocks in that same format whichever company
answers, so the rest of the site doesn't care which one is used."""
import json

import httpx

from . import config

_client = None      # a Claude client (tests swap in a fake)


def get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
    return _client


def set_client(c) -> None:
    global _client
    _client = c


def provider() -> str:
    from . import shop
    p = (shop.setting("ai_provider") or "").lower() or config.AI_PROVIDER
    return p if p in ("anthropic", "openai") else "anthropic"


def model() -> str:
    from . import shop
    m = shop.setting("ai_model")
    if m:
        return m
    return config.OPENAI_MODEL if provider() == "openai" else config.CLAUDE_MODEL


def configured() -> bool:
    if provider() == "openai":
        return bool(config.OPENAI_API_KEY)
    return bool(config.ANTHROPIC_API_KEY) or _client is not None


def complete(system: str, tools: list, messages: list, max_tokens: int = 2000) -> list[dict]:
    return _openai(system, tools, messages, max_tokens) if provider() == "openai" else _claude(system, tools, messages, max_tokens)


# ---------------------------------------------------------------- Claude

def _claude(system, tools, messages, max_tokens):
    extra = {"tools": tools} if tools else {}
    resp = get_client().messages.create(model=model(), max_tokens=max_tokens, system=system, messages=messages, **extra)
    out = []
    for b in resp.content:
        d = b.model_dump(mode="json", exclude_none=True) if hasattr(b, "model_dump") else dict(b)
        if d.get("type") in ("text", "tool_use"):
            out.append({k: d[k] for k in ("type", "text", "id", "name", "input") if k in d})
    return out


# ---------------------------------------------------------------- ChatGPT and compatible services

def _to_openai(system: str, messages: list) -> list[dict]:
    out = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            text = "\n".join(b["text"] for b in content if b.get("type") == "text" and b.get("text"))
            calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in content if b.get("type") == "tool_use"]
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:
            parts = []
            for b in content:
                if b.get("type") == "tool_result":
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": str(b.get("content", ""))})
                elif b.get("type") == "text":
                    parts.append({"type": "text", "text": b["text"]})
                elif b.get("type") == "image":
                    src = b["source"]
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
            if parts:
                out.append({"role": "user", "content": parts})
    return out


def _openai(system, tools, messages, max_tokens):
    body = {"model": model(), "messages": _to_openai(system, messages)}
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                            "parameters": t["input_schema"]}} for t in tools]
    r = httpx.post(f"{config.OPENAI_BASE_URL}/chat/completions", json=body, timeout=90,
                   headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"})
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    out = []
    if msg.get("content"):
        out.append({"type": "text", "text": msg["content"]})
    for c in msg.get("tool_calls") or []:
        try:
            args = json.loads(c["function"].get("arguments") or "{}")
        except ValueError:
            args = {}
        out.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": args})
    return out
