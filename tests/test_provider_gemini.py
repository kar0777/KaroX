import asyncio
import json

import httpx
import pytest

from karox6.core.messages import Message, ToolCall
from karox6.providers.base import ModelRequest, ProviderError, ToolSpec
from karox6.providers.gemini import GeminiProvider


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
        provider = GeminiProvider("https://example.test", "key")
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


END = [{"candidates": [{"finishReason": "STOP"}]}]
TEXT = [{"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}]
LENGTH = [{"candidates": [{"finishReason": "MAX_TOKENS"}]}]
TOOL = [
    {
        "candidates": [
            {"content": {"parts": [{"functionCall": {"name": "lookup", "args": {"x": 1}}}]}}
        ]
    }
]
TOOL_END = END
REASONING = [{"candidates": [{"content": {"parts": [{"text": "think", "thought": True}]}}]}]
USAGE_END = [
    {
        "usageMetadata": {
            "promptTokenCount": 10,
            "cachedContentTokenCount": 2,
            "candidatesTokenCount": 4,
            "thoughtsTokenCount": 1,
        },
        "candidates": [{"finishReason": "STOP"}],
    }
]


def test_signatures_roundtrip():
    event = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "hello", "thoughtSignature": "textsig"},
                        {
                            "functionCall": {"name": "lookup", "args": {"x": 1}},
                            "thoughtSignature": "callsig",
                        },
                    ]
                }
            }
        ]
    }
    result, _ = run([event, *END])
    request = ModelRequest(
        "model",
        "s",
        [result.message, Message("tool", "ok", tool_call_id=result.message.tool_calls[0].id)],
    )
    _, captured = run(TEXT + END, request=request)
    contents = captured["body"]["contents"]
    assert contents[0]["parts"][0]["thoughtSignature"] == "textsig"
    assert contents[0]["parts"][1]["thoughtSignature"] == "callsig"
    assert contents[1]["parts"][0]["functionResponse"]["name"] == "lookup"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            {
                "code": 429,
                "message": "Resource has been exhausted.",
                "status": "RESOURCE_EXHAUSTED",
            },
            "rate_limit",
        ),
        ({"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}, "server"),
        (
            {"code": 500, "message": "An internal error has occurred.", "status": "INTERNAL"},
            "server",
        ),
        (
            {
                "code": 400,
                "message": "The input token count (1200000) exceeds the maximum number of tokens "
                "allowed (1048576).",
                "status": "INVALID_ARGUMENT",
            },
            "context_window",
        ),
        (
            {
                "code": 400,
                "message": "Invalid JSON payload received.",
                "status": "INVALID_ARGUMENT",
            },
            "bad_request",
        ),
        ({"code": 403, "message": "Permission denied.", "status": "PERMISSION_DENIED"}, "auth"),
        ({"code": 504, "message": "Deadline expired.", "status": "DEADLINE_EXCEEDED"}, "timeout"),
    ],
)
def test_an_error_inside_the_stream_keeps_its_kind_and_message(error, code):
    with pytest.raises(ProviderError) as exc:
        run([*TEXT, {"error": error}])
    assert exc.value.code == code
    assert error["message"] in str(exc.value)


def test_too_many_input_tokens_is_a_context_overflow():
    message = (
        "The input token count (1200000) exceeds the maximum number of tokens allowed (1048576)."
    )
    error = {"error": {"code": 400, "message": message, "status": "INVALID_ARGUMENT"}}
    with pytest.raises(ProviderError) as exc:
        run([], 400, body=json.dumps(error))
    assert exc.value.code == "context_window"


def test_an_empty_safety_stop_reports_the_filter_instead_of_retrying_as_empty():
    result, _ = run([{"candidates": [{"finishReason": "SAFETY"}]}])
    assert result.finish_reason == "content_filter"
