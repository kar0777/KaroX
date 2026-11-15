import asyncio
import json

import httpx
import pytest

from karox6.core.messages import Message, ToolCall
from karox6.providers.base import (
    ModelRequest,
    ProviderError,
    RetryPolicy,
    ToolSpec,
    complete_with_retry,
)
from karox6.providers.openai_chat import OpenAIChatProvider


def request():
    return ModelRequest("model", "system", [Message("user", "hello")])


def sse(*chunks):
    return "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"


def chunk(delta, finish=None):
    return {"choices": [{"delta": delta, "finish_reason": finish}]}


def run_response(body, *, status=200, headers=None, stream=True, req=None, callback=None):
    async def go():
        captured = []

        def handler(http_request):
            captured.append(json.loads(http_request.content))
            return httpx.Response(status, text=body, headers=headers)

        provider = OpenAIChatProvider("https://example.test", stream=stream)
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await provider.complete(req or request(), callback)
            return result, captured[0]
        finally:
            await provider.aclose()

    return asyncio.run(go())


def test_body_shape():
    req = request()
    req.tools = [ToolSpec("read", "read file", {"type": "object"})]
    req.max_output_tokens = 42
    req.reasoning_effort = "high"
    req.extra_body = {"custom": 1}
    _, body = run_response(sse(chunk({"content": "ok"}, "stop")), req=req)
    assert body["messages"][0] == {"role": "system", "content": "system"}
    assert body["tools"][0]["function"]["name"] == "read"
    assert body["max_tokens"] == 42
    assert body["reasoning_effort"] == "high"
    assert body["stream_options"] == {"include_usage": True}
    assert body["custom"] == 1


def test_wire_tool_calls_and_reasoning():
    req = request()
    req.messages += [
        Message(
            "assistant",
            tool_calls=[
                ToolCall("id", "read", "", {"extra_content": {"thought_signature": "sig"}})
            ],
            reasoning="thought",
        ),
        Message("tool", "result", tool_call_id="id"),
    ]
    _, body = run_response(sse(chunk({"content": "ok"}, "stop")), req=req)
    assistant, tool = body["messages"][-2:]
    assert assistant["content"] is None
    assert assistant["reasoning_content"] == "thought"
    assert assistant["tool_calls"][0]["function"]["arguments"] == "{}"
    assert assistant["tool_calls"][0]["extra_content"]["thought_signature"] == "sig"
    assert tool["tool_call_id"] == "id"


def test_text_deltas():
    deltas = []
    result, _ = run_response(
        sse(chunk({"content": "a"}), chunk({"content": "b"}, "stop")), callback=deltas.append
    )
    assert result.message.content == "ab"
    assert [d.text for d in deltas] == ["a", "b"]


def test_split_parallel_calls_and_signature():
    parts = [
        {
            "index": 0,
            "id": "a",
            "function": {"name": "read", "arguments": '{"p":'},
            "extra_content": {"thought_signature": "sig"},
        },
        {"index": 1, "id": "b", "function": {"name": "grep", "arguments": "{"}},
    ]
    end = [
        {"index": 1, "function": {"arguments": "}"}},
        {"index": 0, "function": {"arguments": "1}"}},
    ]
    result, _ = run_response(
        sse(chunk({"tool_calls": parts}), chunk({"tool_calls": end}, "tool_calls"))
    )
    assert [(c.id, c.name, c.arguments) for c in result.message.tool_calls] == [
        ("a", "read", '{"p":1}'),
        ("b", "grep", "{}"),
    ]
    assert result.message.tool_calls[0].extra["extra_content"]["thought_signature"] == "sig"
    assert result.finish_reason == "tool_calls"


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
def test_reasoning_delta(field):
    deltas = []
    result, _ = run_response(
        sse(chunk({field: "think", "content": "ok"}, "stop")), callback=deltas.append
    )
    assert result.message.reasoning == "think"
    assert any(d.kind == "reasoning" and d.text == "think" for d in deltas)


@pytest.mark.parametrize(
    "usage", [{"prompt_tokens_details": {"cached_tokens": 4}}, {"prompt_cache_hit_tokens": 4}]
)
def test_usage(usage):
    usage.update(
        {
            "prompt_tokens": 10,
            "completion_tokens": 6,
            "completion_tokens_details": {"reasoning_tokens": 2},
        }
    )
    result, _ = run_response(sse(chunk({"content": "ok"}, "stop"), {"choices": [], "usage": usage}))
    assert (
        result.usage.input_tokens,
        result.usage.cached_tokens,
        result.usage.output_tokens,
        result.usage.reasoning_tokens,
    ) == (10, 4, 6, 2)


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_finish_reason(finish):
    result, _ = run_response(sse(chunk({"content": "ok"}, finish)))
    assert result.finish_reason == finish


def test_missing_call_id():
    result, _ = run_response(
        sse(chunk({"tool_calls": [{"index": 0, "function": {"name": "read"}}]}, "tool_calls"))
    )
    assert result.message.tool_calls[0].id == "call_0"


def test_sse_comments_and_blanks():
    result, _ = run_response(": comment\n\n" + sse(chunk({"content": "ok"}, "stop")))
    assert result.message.content == "ok"


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (429, "busy", "rate_limit"),
        (500, "oops", "server"),
        (400, "maximum context length exceeded", "context_window"),
        (401, "no", "auth"),
    ],
)
def test_http_errors(status, body, code):
    with pytest.raises(ProviderError) as exc:
        run_response(body, status=status, headers={"retry-after": "2"})
    assert exc.value.code == code
    if status == 429:
        assert exc.value.retry_after == 2.0


def test_incomplete_stream():
    with pytest.raises(ProviderError) as exc:
        run_response("data: " + json.dumps(chunk({"content": "partial"})) + "\n\n")
    assert exc.value.code == "transport"


def test_stream_error():
    with pytest.raises(ProviderError) as exc:
        run_response(sse({"error": {"code": 429, "message": "busy"}}))
    assert exc.value.code == "rate_limit"


def test_empty_response():
    with pytest.raises(ProviderError) as exc:
        run_response(sse(chunk({}, "stop")))
    assert exc.value.code == "empty_response"


def test_nonstream():
    result, body = run_response(
        json.dumps({"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}),
        stream=False,
    )
    assert result.message.content == "done"
    assert "stream" not in body


@pytest.mark.parametrize(("status", "succeeds", "count"), [(429, True, 2), (401, False, 1)])
def test_retry(status, succeeds, count):
    async def go():
        calls = []

        def handler(req):
            calls.append(req)
            if len(calls) == 1:
                return httpx.Response(status, text="failure")
            return httpx.Response(200, text=sse(chunk({"content": "ok"}, "stop")))

        provider = OpenAIChatProvider("https://example.test")
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            if succeeds:
                assert (
                    await complete_with_retry(
                        provider, request(), policy=RetryPolicy(initial_delay_s=0.001)
                    )
                ).message.content == "ok"
            else:
                with pytest.raises(ProviderError):
                    await complete_with_retry(
                        provider, request(), policy=RetryPolicy(initial_delay_s=0.001)
                    )
            assert len(calls) == count
        finally:
            await provider.aclose()

    asyncio.run(go())


def test_rate_limit_retries_until_budget():
    from karox6.core.messages import Message, Usage
    from karox6.providers.base import ModelResponse

    class Flaky:
        name = "flaky"

        def __init__(self, failures, code="rate_limit", status=None):
            self.failures, self.code, self.status, self.calls = failures, code, status, 0

        async def complete(self, request, on_delta=None):
            self.calls += 1
            if self.calls <= self.failures:
                raise ProviderError(self.code, "busy", status=self.status)
            return ModelResponse(Message("assistant", "ok"), Usage(), "stop", None)

        async def aclose(self):
            pass

    async def go():
        fast = RetryPolicy(initial_delay_s=0.001, rate_limit_max_delay_s=0.002)
        # Far more than max_retries rate limits are waited out within the budget.
        provider = Flaky(12)
        assert (await complete_with_retry(provider, request(), policy=fast)).message.content == "ok"
        assert provider.calls == 13
        # Other retryable errors still stop after max_retries.
        provider = Flaky(12, code="server")
        with pytest.raises(ProviderError):
            await complete_with_retry(provider, request(), policy=fast)
        assert provider.calls == fast.max_retries + 1
        # The waiting budget bounds rate limits.
        tight = RetryPolicy(
            initial_delay_s=0.01, rate_limit_max_delay_s=0.01, jitter=0.0, rate_limit_budget_s=0.035
        )
        provider = Flaky(100)
        with pytest.raises(ProviderError):
            await complete_with_retry(provider, request(), policy=tight)
        assert provider.calls == 4
        # "Service unavailable" outages are waited out like rate limits...
        for status in (502, 503, 529):
            provider = Flaky(12, code="server", status=status)
            reply = await complete_with_retry(provider, request(), policy=fast)
            assert reply.message.content == "ok"
            assert provider.calls == 13
        # ...on their own budget, while a plain 500 keeps the short policy.
        provider = Flaky(100, code="server", status=503)
        short = RetryPolicy(
            initial_delay_s=0.01,
            rate_limit_max_delay_s=0.01,
            jitter=0.0,
            unavailable_budget_s=0.035,
        )
        with pytest.raises(ProviderError):
            await complete_with_retry(provider, request(), policy=short)
        assert provider.calls == 4
        provider = Flaky(12, code="server", status=500)
        with pytest.raises(ProviderError):
            await complete_with_retry(provider, request(), policy=fast)
        assert provider.calls == fast.max_retries + 1

    asyncio.run(go())
