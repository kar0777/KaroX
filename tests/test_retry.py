"""Retry policy: how long to wait, what a server's Retry-After means, when to give up."""

import asyncio
import time
from email.utils import format_datetime

import pytest

from karox6.core.messages import Message, Usage
from karox6.providers import base
from karox6.providers.base import (
    ModelRequest,
    ModelResponse,
    ProviderError,
    RetryPolicy,
    complete_with_retry,
    retry_after_seconds,
)
from karox6.providers.openai_chat import _http_error


def request():
    return ModelRequest("model", "system", [Message("user", "hello")])


class Flaky:
    name = "flaky"

    def __init__(self, errors):
        self.errors, self.calls = list(errors), 0

    async def complete(self, request, on_delta=None):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return ModelResponse(Message("assistant", "ok"), Usage(), "stop", None)

    async def aclose(self):
        pass


def run_with_recorded_sleeps(provider, policy, monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(base.asyncio, "sleep", fake_sleep)

    async def go():
        return await complete_with_retry(provider, request(), policy=policy)

    try:
        return asyncio.run(go()), slept
    except ProviderError as exc:
        return exc, slept


@pytest.mark.parametrize(
    ("headers", "text", "seconds"),
    [
        ({"retry-after": "2"}, "", 2.0),
        ({"retry-after": "1.5"}, "", 1.5),
        ({"retry-after-ms": "250"}, "", 0.25),
        # retry-after-ms is the more exact one when both are sent (OpenAI, Anthropic SDKs)
        ({"retry-after-ms": "1200", "retry-after": "2"}, "", 1.2),
        # OpenAI: the limit that is used up says when to come back
        (
            {
                "x-ratelimit-remaining-requests": "0",
                "x-ratelimit-reset-requests": "6m0s",
                "x-ratelimit-remaining-tokens": "9000",
                "x-ratelimit-reset-tokens": "250ms",
            },
            "",
            360.0,
        ),
        ({"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "1h2m3s"}, "", 3723.0),
        ({"x-ratelimit-reset": "17"}, "", 17.0),
        ({}, "Rate limit reached. Please try again in 1.242s.", 1.242),
        ({}, "Rate limit reached. Please try again in 350ms.", 0.35),
        ({}, "Too many requests, retry after 20 seconds", 20.0),
        ({}, "busy: try again in 3 minutes", 180.0),
        ({}, '{"error": {"details": [{"@type": "RetryInfo", "retryDelay": "23s"}]}}', 23.0),
        ({}, '{"error": "slow down", "retry_after": 30}', 30.0),
    ],
)
def test_the_wait_a_server_asks_for(headers, text, seconds):
    assert retry_after_seconds(headers, text) == pytest.approx(seconds)


@pytest.mark.parametrize(
    ("headers", "text"),
    [
        ({}, ""),
        ({"retry-after": "soon"}, ""),
        ({"retry-after": "0"}, ""),
        ({"retry-after": "-5"}, ""),
        ({"retry-after": "9999999"}, ""),  # months: not advice to follow
        ({"x-ratelimit-reset-requests": "6m0s", "x-ratelimit-remaining-requests": "12"}, ""),
        ({"x-ratelimit-reset-requests": "later"}, ""),
        ({}, "The request failed; contact support."),
        (None, "no headers object at all"),
    ],
)
def test_no_usable_wait(headers, text):
    assert retry_after_seconds(headers, text) is None


def test_retry_after_as_an_http_date_uses_the_servers_clock():
    from datetime import UTC, datetime, timedelta

    sent = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)  # the server's clock, not ours
    headers = {
        "date": format_datetime(sent, usegmt=True),
        "retry-after": format_datetime(sent + timedelta(seconds=45), usegmt=True),
    }
    assert retry_after_seconds(headers) == pytest.approx(45.0)
    # without a Date header the local clock is the reference
    later = datetime.now(UTC) + timedelta(seconds=30)
    seconds = retry_after_seconds({"retry-after": format_datetime(later, usegmt=True)})
    assert seconds is not None
    assert 25 <= seconds <= 31


def test_reset_given_as_an_epoch():
    soon = time.time() + 12
    assert retry_after_seconds({"x-ratelimit-reset": str(int(soon))}) == pytest.approx(12, abs=2)
    millis = str(int(soon * 1000))  # OpenRouter sends milliseconds
    assert retry_after_seconds({"x-ratelimit-reset": millis}) == pytest.approx(12, abs=2)


def test_a_server_wait_is_a_floor_never_shortened():
    policy = RetryPolicy()
    for attempt in range(1, 8):
        for asked in (0.2, 3.0, 45.0):
            delay = policy.delay(attempt, asked)
            assert asked <= delay <= asked * 1.1 + 1e-9
    # a rate limit is waited out as long as the server says (the budget decides later)
    assert 90.0 <= policy.delay(1, 90.0, rate_limit=True) <= 92.0
    # for other failures an over-long wait is not followed: the usual backoff applies
    assert policy.delay(1, 90.0) < 1.0


def test_rate_limit_waits_as_long_as_the_server_asks(monkeypatch):
    busy = ProviderError("rate_limit", "busy", status=429, retry_after=90.0)
    provider = Flaky([busy, busy])
    result, slept = run_with_recorded_sleeps(provider, RetryPolicy(), monkeypatch)
    assert isinstance(result, ModelResponse)
    assert provider.calls == 3
    assert len(slept) == 2
    assert all(90.0 <= s <= 92.0 for s in slept)  # never earlier than asked


def test_a_wait_longer_than_the_budget_ends_the_retries_at_once(monkeypatch):
    hour = ProviderError("rate_limit", "come back in an hour", status=429, retry_after=3600.0)
    provider = Flaky([hour] * 5)
    result, slept = run_with_recorded_sleeps(provider, RetryPolicy(), monkeypatch)
    assert isinstance(result, ProviderError)
    assert result.code == "rate_limit"
    assert provider.calls == 1
    assert slept == []  # sleeping through part of it would not have helped


def test_outage_with_retry_after_is_waited_out_on_its_budget(monkeypatch):
    down = ProviderError("server", "unavailable", status=503, retry_after=120.0)
    provider = Flaky([down] * 3)
    result, slept = run_with_recorded_sleeps(provider, RetryPolicy(), monkeypatch)
    assert isinstance(result, ModelResponse)
    assert len(slept) == 3
    assert all(120.0 <= s <= 122.0 for s in slept)
    # the waits stop where the next one would pass the 600 s budget
    provider = Flaky([down] * 9)
    result, slept = run_with_recorded_sleeps(provider, RetryPolicy(), monkeypatch)
    assert isinstance(result, ProviderError)
    assert 4 <= len(slept) <= 5
    assert sum(slept) <= 600.0


def test_an_empty_reply_is_asked_for_again_only_once(monkeypatch):
    empty = ProviderError("empty_response", "the model returned no text and no tool calls")
    provider = Flaky([empty] * 10)
    result, _ = run_with_recorded_sleeps(provider, RetryPolicy(), monkeypatch)
    assert isinstance(result, ProviderError)
    assert result.code == "empty_response"
    assert provider.calls == 2  # the reply and one identical resend, not six
    # a single empty reply is still recovered by that resend
    provider = Flaky([empty])
    result, _ = run_with_recorded_sleeps(provider, RetryPolicy(), monkeypatch)
    assert isinstance(result, ModelResponse)
    # ...and it does not use up the retries of a later dropped connection
    reset = ProviderError("transport", "connection reset")
    provider = Flaky([empty, reset, reset, reset])
    result, _ = run_with_recorded_sleeps(provider, RetryPolicy(), monkeypatch)
    assert isinstance(result, ModelResponse)
    assert provider.calls == 5


@pytest.mark.parametrize(
    "body",
    [
        "Исчерпан дневной лимит 5000 запросов твоего ранга (общий на все ключи аккаунта). "
        "Сброс в 00:00 МСК.",
        '{"error": {"message": "Rate limit exceeded: free-models-per-day. Add credits"}}',
        '{"error": {"message": "Daily limit of 200 requests reached"}}',
        '{"error": {"message": "You have used your daily quota"}}',
        '{"error": {"message": "limit of 1000 requests per day exceeded"}}',
    ],
)
def test_a_daily_allowance_is_quota_not_a_rate_limit(body):
    # minutes of waiting cannot bring back a per-day allowance
    assert _http_error(429, body, {}).code == "quota"


def test_quota_wording_with_a_short_wait_is_a_rate_limit():
    # Gemini's free tier refuses a per-minute window in quota words and says when to retry
    body = (
        '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "You exceeded '
        "your current quota, please check your plan and billing details. Please retry in "
        '23.5s.", "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", '
        '"retryDelay": "23s"}]}}'
    )
    error = _http_error(429, body, {})
    assert error.code == "rate_limit"
    assert error.retry_after == pytest.approx(23.5)
    # the same words with no wait, or a long one, mean the allowance is gone
    assert _http_error(429, "You exceeded your current quota, check billing", {}).code == "quota"
    long_wait = {"retry-after": "7200"}
    assert _http_error(429, "You exceeded your current quota", long_wait).code == "quota"
    # a payment problem is never waited out
    assert _http_error(402, "Insufficient balance. Please retry in 5s", {}).code == "quota"


def test_minute_and_concurrency_limits_stay_rate_limits():
    for body in (
        "Превышен лимит 40 запросов в минуту для ключа. Дождитесь освобождения минутного окна.",
        "Превышен лимит параллельных запросов для ключа (4 одновременных потоков).",
        '{"error": {"message": "This model is at concurrency capacity."}}',
        '{"error": {"message": "Rate limit reached for requests per minute"}}',
    ):
        assert _http_error(429, body, {}).code == "rate_limit"


# -- a stream that goes silent -------------------------------------------------------


def _sse(*chunks):
    import json

    return "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)


def _chunk(delta, finish=None, usage=None):
    out = {"choices": [{"delta": delta, "finish_reason": finish}]}
    if usage:
        out["usage"] = usage
    return out


def _streaming_provider(parts, idle_s=0.05):
    """An OpenAI-compatible provider whose reply arrives as ``parts``: strings are sent,
    numbers are pauses in seconds. Returns (provider, number of requests seen)."""
    import httpx

    from karox6.providers.openai_chat import OpenAIChatProvider

    class Paced(httpx.AsyncByteStream):
        async def __aiter__(self):
            for part in parts:
                if isinstance(part, str):
                    yield part.encode()
                else:
                    await asyncio.sleep(part)

    seen = []

    def handler(http_request):
        seen.append(1)
        return httpx.Response(200, stream=Paced())

    provider = OpenAIChatProvider("https://example.test", stream=True, stream_idle_s=idle_s)
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider, seen


def _complete(provider):
    async def go():
        try:
            return await asyncio.wait_for(provider.complete(request(), None), 5)
        finally:
            await provider.aclose()

    return asyncio.run(go())


def test_a_stream_that_stalls_mid_answer_is_a_timeout_not_a_long_hang():
    provider, _ = _streaming_provider([_sse(_chunk({"content": "half an ans"})), 30.0])
    started = time.monotonic()
    with pytest.raises(ProviderError) as caught:
        _complete(provider)
    assert caught.value.code == "timeout"  # so the retry policy asks again
    assert "stalled" in str(caught.value)
    assert time.monotonic() - started < 3


def test_silence_while_the_model_is_still_thinking_is_not_a_stall():
    usage = {"prompt_tokens": 5, "completion_tokens": 3}
    provider, _ = _streaming_provider(
        [
            _sse(_chunk({"reasoning_content": "let me think"})),
            0.3,  # six times the idle limit, but no answer token has arrived yet
            _sse(_chunk({"content": "done"}, "stop", usage)) + "data: [DONE]\n\n",
        ]
    )
    reply = _complete(provider)
    assert reply.message.content == "done"
    assert reply.message.reasoning == "let me think"


def test_a_stall_after_the_reply_finished_keeps_the_reply():
    usage = {"prompt_tokens": 5, "completion_tokens": 3}
    provider, seen = _streaming_provider([_sse(_chunk({"content": "done"}, "stop", usage)), 30.0])
    reply = _complete(provider)  # only [DONE] is missing: not worth paying for again
    assert reply.message.content == "done"
    assert len(seen) == 1


def test_keep_alive_pings_do_not_hide_a_stalled_answer():
    pings = []
    for _ in range(40):  # 40 x 20 ms of pings: far past the 50 ms idle limit
        pings += [': ping\n\nevent: ping\ndata: {"type": "ping"}\n\n', 0.02]
    provider, _ = _streaming_provider([_sse(_chunk({"content": "half an ans"})), *pings, 30.0])
    started = time.monotonic()
    with pytest.raises(ProviderError) as caught:
        _complete(provider)
    assert caught.value.code == "timeout"
    assert time.monotonic() - started < 0.6
