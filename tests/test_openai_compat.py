"""OpenAI-compatible provider: thread translation both ways, tool calls, and the max_completion_tokens retry."""
import json

import httpx

from clinestra.server.agentic import openai_compat as o


def test_openai_compat_round_trip():
    seen = []

    def handler(req):
        b = json.loads(req.content)
        seen.append(b)
        if len(seen) == 1:
            return httpx.Response(200, json={"model": "m", "usage": {"prompt_tokens": 5, "completion_tokens": 2}, "choices": [{"finish_reason": "tool_calls", "message": {
                "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "summarize_patient", "arguments": "{\"x\": 1}"}}]}}]})
        if "max_tokens" in b:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."}})
        return httpx.Response(200, json={"model": "m", "usage": {"prompt_tokens": 9, "completion_tokens": 1}, "choices": [{"finish_reason": "stop", "message": {"content": "done"}}]})

    c = o.Client("https://example.test/v1", "k")
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    tools = [{"name": "summarize_patient", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    thread = [{"role": "user", "content": "hi"}]

    r = c.create("m", "sys", thread, tools, 100)
    assert r.stop_reason == "tool_use"
    content = r.to_dict()["content"]
    assert content[0] == {"type": "tool_use", "id": "c1", "name": "summarize_patient", "input": {"x": 1}}

    thread += [{"role": "assistant", "content": content}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}]}]
    r = c.create("m", "sys", thread, tools, 100)
    assert r.stop_reason == "end_turn" and r.content[0].text == "done" and r.usage.input_tokens == 9

    sent = seen[1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool"]
    assert sent[2]["tool_calls"][0]["id"] == "c1" and sent[3]["tool_call_id"] == "c1"
    assert seen[1]["tools"][0]["function"]["name"] == "summarize_patient"
    assert "max_completion_tokens" in seen[2] and "max_tokens" not in seen[2]
