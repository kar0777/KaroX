import asyncio
import json

import httpx
import pytest

from karox6.core.messages import Message, ToolCall
from karox6.providers.anthropic import AnthropicProvider
from karox6.providers.base import ModelRequest, ProviderError, ToolSpec


def run(events, status=200, headers=None, request=None, body=None):
    captured = {}

    def handler(req):
        captured["body"] = json.loads(req.content)
        captured["url"] = str(req.url)
        if status != 200:
            text = body or ("prompt is too long" if status == 400 else "error")
            return httpx.Response(status, text=text, headers=headers)
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


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ({"type": "overloaded_error", "message": "Overloaded"}, "server"),
        ({"type": "api_error", "message": "Internal server error"}, "server"),
        (
            {"type": "rate_limit_error", "message": "This request would exceed the rate limit"},
            "rate_limit",
        ),
        ({"type": "authentication_error", "message": "invalid x-api-key"}, "auth"),
        ({"type": "permission_error", "message": "Your API key may not use this model"}, "auth"),
        ({"type": "not_found_error", "message": "model: claude-nope"}, "bad_request"),
        (
            {
                "type": "invalid_request_error",
                "message": "prompt is too long: 213000 tokens > 200000",
            },
            "context_window",
        ),
        (
            {"type": "invalid_request_error", "message": "messages.0: content must be non-empty"},
            "bad_request",
        ),
    ],
)
def test_an_error_inside_the_stream_keeps_its_kind_and_message(error, code):
    with pytest.raises(ProviderError) as exc:
        run([*TEXT, {"type": "error", "error": error}])
    assert exc.value.code == code
    assert error["message"] in str(exc.value)


def test_input_plus_max_tokens_over_the_window_is_a_context_overflow():
    # The refusal an agent meets first: the conversation still fits, the reply's room does not.
    message = (
        "input length and `max_tokens` exceed context limit: 196280 + 21333 > 200000, "
        "decrease input length or `max_tokens` and try again"
    )
    error = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    with pytest.raises(ProviderError) as exc:
        run([], 400, body=json.dumps(error))
    assert exc.value.code == "context_window"


def test_a_refusal_is_not_the_final_answer():
    stop = {"type": "message_delta", "delta": {"stop_reason": "refusal"}}
    result, _ = run([*TEXT, stop, {"type": "message_stop"}])
    assert result.finish_reason == "content_filter"


def test_a_reply_stopped_by_the_context_window_asks_for_a_smaller_conversation():
    stop = {"type": "message_delta", "delta": {"stop_reason": "model_context_window_exceeded"}}
    with pytest.raises(ProviderError) as exc:
        run([*TEXT, stop, {"type": "message_stop"}])
    assert exc.value.code == "context_window"


def test_an_empty_refusal_reports_the_refusal_instead_of_retrying_as_empty():
    stop = {"type": "message_delta", "delta": {"stop_reason": "refusal"}}
    result, _ = run([stop, {"type": "message_stop"}])
    assert result.finish_reason == "content_filter"


def test_an_assistant_turn_without_content_is_not_sent():
    request = ModelRequest(
        "model",
        "system",
        [Message("user", "hello"), Message("assistant", ""), Message("user", "again")],
    )
    _, captured = run([*TEXT, *END], request=request)
    roles = [m["role"] for m in captured["body"]["messages"]]
    assert roles == ["user"]  # both user turns merged; no empty assistant message
    assert all(m["content"] for m in captured["body"]["messages"])


def test_reading_stops_at_done_even_if_the_connection_stays_open():
    class OpenAfterDone(httpx.AsyncByteStream):
        async def __aiter__(self):
            body = "".join("data: " + json.dumps(e) + "\n\n" for e in [*TEXT, *END])
            yield (body + "data: [DONE]\n\n").encode()
            await asyncio.sleep(30)

    async def invoke():
        provider = AnthropicProvider("https://example.test", "key")
        provider._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _req: httpx.Response(200, stream=OpenAfterDone()))
        )
        try:
            return await asyncio.wait_for(
                provider.complete(ModelRequest("model", "system", [Message("user", "hi")])), 5
            )
        finally:
            await provider.aclose()

    assert asyncio.run(invoke()).message.content
