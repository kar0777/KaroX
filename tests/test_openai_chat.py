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


@pytest.mark.parametrize(
    ("output", "finish"), [(100, "length"), (99, "tool_calls"), (101, "tool_calls")]
)
def test_reply_at_the_token_cap_is_a_cut(output, finish):
    """A router reported tool_calls for 32768-token replies whose arguments were cut."""
    req = request()
    req.max_output_tokens = 100
    call = {"index": 0, "id": "c1", "function": {"name": "bash", "arguments": '{"comm'}}
    usage = {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": output}}
    result, _ = run_response(sse(chunk({"tool_calls": [call]}, "tool_calls"), usage), req=req)
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

        # A long 503 spell does not use up the quick retries of a later plain 500.
        class Mixed(Flaky):
            async def complete(self, request, on_delta=None):
                self.calls += 1
                if self.calls <= 8:
                    raise ProviderError("server", "unavailable", status=503)
                if self.calls == 9:
                    raise ProviderError("server", "oops", status=500)
                return ModelResponse(Message("assistant", "ok"), Usage(), "stop", None)

        provider = Mixed(0)
        assert (await complete_with_retry(provider, request(), policy=fast)).message.content == "ok"
        assert provider.calls == 10

    asyncio.run(go())


def _stream_then_reset(body: str):
    class Reset(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield body.encode()
            raise httpx.ReadError("connection reset")

    async def go():
        calls = []

        def handler(http_request):
            calls.append(1)
            return httpx.Response(200, stream=Reset())

        provider = OpenAIChatProvider("https://example.test", stream=True)
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await provider.complete(request(), None), len(calls)
        finally:
            await provider.aclose()

    return asyncio.run(go())


def test_reset_after_the_reply_finished_keeps_it():
    result, calls = _stream_then_reset(sse(chunk({"content": "ok"}, "stop")))
    assert result.message.content == "ok"
    assert calls == 1
    usage = {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1}}
    no_done = "".join(
        "data: " + json.dumps(c) + "\n\n" for c in (chunk({"content": "ok"}, "stop"), usage)
    )
    result, _ = _stream_then_reset(no_done)
    assert result.message.content == "ok"
    # Without usage the max_tokens check cannot run: retried, not kept.
    with pytest.raises(ProviderError):
        _stream_then_reset("data: " + json.dumps(chunk({"content": "ok"}, "stop")) + "\n\n")


def test_done_with_usage_but_no_finish_reason_is_kept():
    usage = {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1}}
    result, _ = run_response(sse(chunk({"content": "whole answer"}), usage))
    assert result.message.content == "whole answer"


def test_gateway_fault_mentioning_the_context_window_is_retried():
    with pytest.raises(ProviderError) as exc:
        run_response("failed to load context window configuration", status=503)
    assert exc.value.code == "server"


def test_reset_mid_reply_is_a_transport_error():
    with pytest.raises(ProviderError) as exc:
        _stream_then_reset("data: " + json.dumps(chunk({"content": "par"})) + "\n\n")
    assert exc.value.code == "transport"


def test_done_without_a_finish_reason_is_not_an_answer():
    with pytest.raises(ProviderError) as exc:
        run_response(sse(chunk({"content": "I will now edit"})))
    assert exc.value.code == "transport"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ({"code": "rate_limit_exceeded", "message": "slow down"}, "rate_limit"),
        ({"code": "context_length_exceeded", "message": "too long"}, "context_window"),
        ({"type": "server_error", "message": "boom"}, "server"),
    ],
)
def test_stream_error_with_a_string_code(error, code):
    with pytest.raises(ProviderError) as exc:
        run_response(sse({"error": error}))
    assert exc.value.code == code


def test_gateway_500_wrapping_a_context_overflow():
    with pytest.raises(ProviderError) as exc:
        run_response('{"error": "maximum context length is 128000 tokens"}', status=500)
    assert exc.value.code == "context_window"


def test_reasoning_only_reply_has_string_content_on_the_wire():
    from karox6.providers.openai_chat import to_wire

    assert to_wire(Message("assistant", ""))["content"] == ""
