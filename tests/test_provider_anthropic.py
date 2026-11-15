import asyncio
import json

import httpx
import pytest

from karox6.core.messages import Message, ToolCall
from karox6.providers.anthropic import AnthropicProvider
from karox6.providers.base import ModelRequest, ProviderError, ToolSpec


def run(events, status=200, headers=None, request=None):
    captured = {}

    def handler(req):
        captured["body"] = json.loads(req.content)
        captured["url"] = str(req.url)
        if status != 200:
            return httpx.Response(
                status, text="prompt is too long" if status == 400 else "error", headers=headers
            )
        data = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
        return httpx.Response(200, text=data, headers={"content-type": "text/event-stream"})

    async def invoke():
        provider = AnthropicProvider("https://example.test", "key")
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await provider.complete(
                request or ModelRequest("model", "system", [Message("user", "hello")])
            )
        finally:
            await provider.aclose()

    return asyncio.run(invoke()), captured


@pytest.mark.parametrize(
    ("status", "code"),
    [(429, "rate_limit"), (500, "server"), (400, "context_window"), (401, "auth")],
)
def test_errors(status, code):
    with pytest.raises(ProviderError) as exc:
        run([], status, {"retry-after": "3"})
    assert exc.value.code == code
    if status == 429:
        assert exc.value.retry_after == 3


def test_empty():
    with pytest.raises(ProviderError) as exc:
        run(END)
    assert exc.value.code == "empty_response"


def test_truncated():
    with pytest.raises(ProviderError) as exc:
        run([])
    assert exc.value.code == "transport"


def test_text():
    result, captured = run(TEXT + END)
    assert result.message.content == "hello"
    assert result.finish_reason == "stop"
    assert captured["body"]


def test_length():
    result, _ = run(LENGTH)
    assert result.finish_reason == "length"


def test_tools():
    result, _ = run(TOOL + TOOL_END)
    assert result.message.tool_calls[0].name == "lookup"
    assert json.loads(result.message.tool_calls[0].arguments) == {"x": 1}
    assert result.finish_reason == "tool_calls"


def test_reasoning():
    result, _ = run(REASONING + TEXT + END)
    assert result.message.reasoning == "think"


def test_usage():
    result, _ = run(TEXT + USAGE_END)
    assert result.usage.input_tokens == 10
    assert result.usage.cached_tokens == 2
    assert result.usage.output_tokens == 5


def test_body():
    request = ModelRequest(
        "model",
        "system",
        [
            Message("user", "hello"),
            Message("assistant", "", tool_calls=[ToolCall("id", "lookup", '{"x":1}')]),
            Message("tool", "ok", tool_call_id="id", name="lookup"),
        ],
        tools=[ToolSpec("lookup", "find", {"type": "object"})],
        reasoning_effort="low",
    )
    _, captured = run(TEXT + END, request=request)
    body = captured["body"]
    assert "tools" in body
    assert "system" in json.dumps(body)
    assert "lookup" in json.dumps(body)
    assert "ok" in json.dumps(body)


END = [{"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"}]
TEXT = [
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
]
LENGTH = [
    {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
    {"type": "message_stop"},
]
TOOL = [
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "tool_use", "id": "id", "name": "lookup", "input": {}},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '{"x":'},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": "1}"},
    },
]
TOOL_END = [
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
    {"type": "message_stop"},
]
REASONING = [
    {
        "type": "content_block_start",
        "index": 1,
        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
    },
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "thinking_delta", "thinking": "think"},
    },
]
USAGE_END = [
    {
        "type": "message_start",
        "message": {"usage": {"input_tokens": 8, "cache_read_input_tokens": 2}},
    },
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
    {"type": "message_stop"},
]


def test_thinking_roundtrip_and_merge():
    events = [
        *REASONING,
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "signature_delta", "signature": "sig"},
        },
        *TEXT,
        *END,
    ]
    result, _ = run(events)
    request = ModelRequest(
        "model",
        "s",
        [
            Message("user", "one"),
            Message("user", "two"),
            result.message,
            Message("tool", "ok", tool_call_id="id"),
        ],
    )
    _, captured = run(TEXT + END, request=request)
    messages = captured["body"]["messages"]
    assert len(messages) == 3
    assert len(messages[0]["content"]) == 2
    assert messages[1]["content"][0]["signature"] == "sig"
    assert messages[2]["content"][0]["type"] == "tool_result"
