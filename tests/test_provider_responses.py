import asyncio
import json

import httpx
import pytest

from karox6.core.messages import Message, ToolCall
from karox6.providers.base import ModelRequest, ProviderError, ToolSpec
from karox6.providers.openai_responses import OpenAIResponsesProvider


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
        provider = OpenAIResponsesProvider("https://example.test", "key")
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


END = [{"type": "response.completed", "response": {}}]
TEXT = [{"type": "response.output_text.delta", "delta": "hello"}]
LENGTH = [
    {
        "type": "response.incomplete",
        "response": {"incomplete_details": {"reason": "max_output_tokens"}},
    }
]
TOOL = [
    {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {"type": "function_call", "call_id": "id", "name": "lookup"},
    },
    {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"x":'},
    {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": "1}"},
]
TOOL_END = END
REASONING = [{"type": "response.reasoning_summary_text.delta", "delta": "think"}]
USAGE_END = [
    {
        "type": "response.completed",
        "response": {
            "usage": {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 2},
                "output_tokens": 5,
            }
        },
    }
]


def test_encrypted_reasoning_roundtrip():
    item = {"type": "reasoning", "id": "r", "encrypted_content": "encrypted"}
    result, _ = run(
        [{"type": "response.output_item.done", "output_index": 1, "item": item}, *TOOL, *END]
    )
    _, captured = run(TEXT + END, request=ModelRequest("model", "s", [result.message]))
    assert item in captured["body"]["input"]
    assert captured["body"]["store"] is False
    assert captured["body"]["include"] == ["reasoning.encrypted_content"]


def failed(code, message):
    error = {"code": code, "message": message}
    return {"type": "response.failed", "response": {"status": "failed", "error": error}}


@pytest.mark.parametrize(
    ("event", "code", "says"),
    [
        # the API's own `error` event: code and message at the top level, no "error" object
        (
            {
                "type": "error",
                "code": "rate_limit_exceeded",
                "message": "Rate limit reached for requests. Please try again in 1.5s.",
                "param": None,
                "sequence_number": 2,
            },
            "rate_limit",
            "Rate limit reached",
        ),
        (
            {"type": "error", "code": None, "message": "The server had an error.", "param": None},
            "server",
            "The server had an error.",
        ),
        (
            failed(
                "context_length_exceeded", "Your input exceeds the context window of this model."
            ),
            "context_window",
            "exceeds the context window",
        ),
        (
            failed("insufficient_quota", "You exceeded your current quota, check your plan."),
            "quota",
            "current quota",
        ),
        (
            failed("server_error", "The model failed to generate a response."),
            "server",
            "failed to generate",
        ),
    ],
)
def test_an_error_inside_the_stream_keeps_its_kind_and_message(event, code, says):
    with pytest.raises(ProviderError) as exc:
        run([*TEXT, event])
    assert exc.value.code == code
    assert says in str(exc.value)
    if code == "rate_limit":
        assert exc.value.retry_after == 1.5


def test_a_filtered_reply_is_not_the_final_answer():
    stopped = {
        "type": "response.incomplete",
        "response": {"incomplete_details": {"reason": "content_filter"}},
    }
    result, _ = run([*TEXT, stopped])
    assert result.finish_reason == "content_filter"
