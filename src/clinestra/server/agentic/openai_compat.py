"""
OpenAI-compatible providers: any endpoint that speaks the OpenAI Chat Completions API (OpenAI, Azure OpenAI's
/openai/v1, OpenRouter, Together, Groq, DeepSeek, Mistral, vLLM, Ollama, LM Studio, LiteLLM, ...). Needs only a
base URL and a key.

The agent loop keeps its thread in Messages-API shape (content blocks, ``tool_use`` / ``tool_result``). This module
translates that thread to Chat Completions on the way out and the reply back into the same shape on the way in, so
agents, tools, audit and stored threads work unchanged whichever platform a model runs on.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_BASE_URL = "https://api.openai.com/v1"
_STOP = {"tool_calls": "tool_use", "function_call": "tool_use", "length": "max_tokens", "content_filter": "refusal"}


class OpenAICompatError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status_code, self.message = status, message


class _Usage:
    def __init__(self, u: dict):
        self.input_tokens = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        self.output_tokens = int(u.get("completion_tokens") or u.get("output_tokens") or 0)


class _Block:
    def __init__(self, d: dict):
        self._d = d
        self.type = d["type"]
        self.text = d.get("text", "")


class Response:
    """Just enough of the Messages API response for the callers: content, stop_reason, usage, model, to_dict()."""

    def __init__(self, content: List[dict], stop_reason: str, usage: dict, model: str):
        self._content = content
        self.content = [_Block(b) for b in content]
        self.stop_reason, self.usage, self.model = stop_reason, _Usage(usage or {}), model

    def to_dict(self) -> dict:
        return {"content": self._content, "stop_reason": self.stop_reason, "model": self.model}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def to_chat_messages(system: str, messages: list) -> List[dict]:
    """Messages-API thread -> Chat Completions messages (tool_use -> tool_calls, tool_result -> role "tool")."""
    out: List[dict] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        content = m.get("content")
        if m["role"] == "assistant":
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in blocks if b.get("type") == "tool_use"]
            msg: Dict[str, Any] = {"role": "assistant", "content": _text(blocks) or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        if isinstance(content, list):
            for b in content:
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": c if isinstance(c, str) else _text(c) or json.dumps(c)})
            text = _text(content)
            if text:
                out.append({"role": "user", "content": text})
        else:
            out.append({"role": "user", "content": content or ""})
    return out


def to_chat_tools(tools: Optional[list]) -> Optional[list]:
    if not tools:
        return None
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", "")[:1024],
                                              "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}} for t in tools]


def from_chat(data: dict) -> Response:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content: List[dict] = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        content.append({"type": "tool_use", "id": c.get("id") or f"call_{len(content)}", "name": fn.get("name", ""), "input": args})
    stop = "tool_use" if msg.get("tool_calls") else _STOP.get(choice.get("finish_reason") or "", "end_turn")
    if msg.get("refusal"):
        stop = "refusal"
        content.append({"type": "text", "text": msg["refusal"]})
    return Response(content, stop, data.get("usage") or {}, data.get("model") or "")


class Client:
    """Minimal Chat Completions client (no SDK dependency)."""

    def __init__(self, base_url: Optional[str], api_key: Optional[str], headers: Optional[dict] = None, timeout: float = 120):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        h = {"Content-Type": "application/json", **(headers or {})}
        if api_key:
            h["Authorization"] = f"Bearer {api_key}"
            h.setdefault("api-key", api_key)  # Azure OpenAI accepts this header
        self._http = httpx.Client(headers=h, timeout=timeout)

    def _post(self, body: dict) -> dict:
        try:
            r = self._http.post(f"{self.base_url}/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise OpenAICompatError(0, f"Could not reach the endpoint: {e}") from e
        if r.status_code >= 400:
            try:
                err = r.json().get("error") or {}
                message = err.get("message") if isinstance(err, dict) else str(err)
            except ValueError:
                message = r.text[:300]
            raise OpenAICompatError(r.status_code, message or r.reason_phrase)
        return r.json()

    def create(self, model: str, system: str, messages: list, tools: Optional[list] = None, max_tokens: Optional[int] = None) -> Response:
        body: Dict[str, Any] = {"model": model, "messages": to_chat_messages(system, messages)}
        if tools:
            body["tools"] = to_chat_tools(tools)
        if max_tokens:
            body["max_tokens"] = max_tokens
        # Adapt to what the endpoint says it wants, at most once per kind of complaint:
        #  - newer OpenAI models take max_completion_tokens instead of max_tokens;
        #  - reasoning models refuse function tools on Chat Completions unless reasoning_effort is "none".
        for _ in range(3):
            try:
                return from_chat(self._post(body))
            except OpenAICompatError as e:
                msg = e.message or ""
                if e.status_code == 400 and "max_tokens" in body and "max_completion_tokens" in msg:
                    body["max_completion_tokens"] = body.pop("max_tokens")
                elif e.status_code == 400 and "reasoning_effort" in msg and body.get("reasoning_effort") != "none":
                    body["reasoning_effort"] = "none"
                else:
                    raise
        return from_chat(self._post(body))


def test(client: Client, model: str) -> dict:
    try:
        r = client.create(model, "", [{"role": "user", "content": "Reply with the single word: ok"}], max_tokens=16)
        return {"ok": True, "model": r.model or model, "reply": "".join(b.text for b in r.content if b.type == "text").strip()[:40]}
    except OpenAICompatError as e:
        hint = {401: "Authentication failed: the key was rejected.", 403: "The key does not have access to this model.",
                404: "Model or endpoint not found. Check the base URL (it usually ends in /v1) and the model id.",
                429: "Rate limited by the provider. The key works; try again shortly."}.get(e.status_code)
        return {"ok": False, "error": hint or (f"Provider error {e.status_code}: {e.message}" if e.status_code else e.message)}
