"""Model-call resilience and thinking-model replay on the engine bridge.

Three arena runs ended as ``provider_error:transport`` and one as
``provider_error:provider_internal`` with most of their budget left: the
direct ``--model/--base-url`` route had no retry for a 429, a 5xx or a cut
stream. A thinking model on the Chat Completions wire also never saw its own
earlier reasoning, which DeepSeek requires back on the turns of a tool chain.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from karox.engine.agents.default import DefaultAgent
from karox.engine_provider_bridge import (
    EngineProviderModel,
    provider_retry_delay,
)
from karox.provider_adapters import AnthropicMessagesProvider
from karox.providers import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    OpenAIChatCompletionsProvider,
    ProviderError,
    ProviderErrorKind,
    ToolCall,
    _without_optional_tuning,
)


def _finish(call_id: str = "f1") -> ToolCall:
    return ToolCall(call_id, "finish", json.dumps({"answer": "done"}))


class FlakyProvider:
    """Raises the scripted errors first, then answers."""

    provider_name = "flaky"

    def __init__(self, errors: List[BaseException], response: ModelResponse) -> None:
        self._errors = list(errors)
        self._response = response
        self.requests: List[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._errors:
            raise self._errors.pop(0)
        return self._response

    def close(self) -> None:
        return None


def _model(provider: Any, *, deadline: Any = 300.0, events: Optional[list] = None):
    slept: List[float] = []

    def event(kind: str, **fields: Any) -> None:
        if events is not None:
            events.append((kind, fields))

    model = EngineProviderModel(
        provider=provider,
        model_id="m",
        tools=(),
        deadline_seconds=deadline,
        event=event,
        sleep=slept.append,
        jitter=lambda: 0.0,
    )
    return model, slept


def _conversation() -> List[Dict[str, Any]]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do it"},
    ]


def test_rate_limit_and_5xx_are_retried_then_the_answer_is_used() -> None:
    provider = FlakyProvider(
        [
            ProviderError(ProviderErrorKind.RATE_LIMIT, "429", status_code=429),
            ProviderError(ProviderErrorKind.PROVIDER_INTERNAL, "502", status_code=502),
            ProviderError(ProviderErrorKind.TRANSPORT, "stream cut"),
        ],
        ModelResponse(content=None, tool_calls=(_finish(),), finish_reason="tool_calls", usage={}),
    )
    events: list = []
    model, slept = _model(provider, events=events)

    message = model.query(_conversation())

    assert message["tool_calls"][0]["name"] == "finish"
    assert len(provider.requests) == 4
    assert slept == [2.0, 4.0, 8.0]
    assert [item["kind"] for item in model.provider_retries] == [
        "rate_limit",
        "provider_internal",
        "transport",
    ]
    warnings = [fields for kind, fields in events if kind == "warning"]
    assert [fields["reason"] for fields in warnings] == ["provider_retry"] * 3
    assert "HTTP 429" in warnings[0]["detail"]


@pytest.mark.parametrize(
    "kind",
    [
        ProviderErrorKind.INVALID_REQUEST,
        ProviderErrorKind.AUTHENTICATION,
        ProviderErrorKind.MODEL_UNAVAILABLE,
        ProviderErrorKind.CANCELLED,
        ProviderErrorKind.BUDGET_EXCEEDED,
    ],
)
def test_failures_that_repeat_identically_are_not_retried(kind) -> None:
    provider = FlakyProvider(
        [ProviderError(kind, "no")],
        ModelResponse(content="unused", tool_calls=(), finish_reason="stop", usage={}),
    )
    model, slept = _model(provider)

    with pytest.raises(ProviderError) as caught:
        model.query(_conversation())

    assert caught.value.kind is kind
    assert len(provider.requests) == 1
    assert slept == []


def test_retries_stop_after_the_attempt_limit() -> None:
    errors = [
        ProviderError(ProviderErrorKind.RATE_LIMIT, "429", status_code=429)
        for _ in range(20)
    ]
    provider = FlakyProvider(errors, ModelResponse(content="x", tool_calls=(), finish_reason="stop", usage={}))
    model, slept = _model(provider, deadline=3600.0)

    with pytest.raises(ProviderError):
        model.query(_conversation())

    assert len(slept) == 6
    assert len(provider.requests) == 7
    assert max(slept) <= 30.0


def test_no_retry_when_the_call_window_is_nearly_spent() -> None:
    provider = FlakyProvider(
        [ProviderError(ProviderErrorKind.RATE_LIMIT, "429", status_code=429)],
        ModelResponse(content="x", tool_calls=(), finish_reason="stop", usage={}),
    )
    model, slept = _model(provider, deadline=15.0)

    with pytest.raises(ProviderError):
        model.query(_conversation())

    assert slept == []


def test_retry_after_is_honoured_and_capped() -> None:
    asked = ProviderError(
        ProviderErrorKind.RATE_LIMIT, "429", status_code=429, retry_after=7.0
    )
    assert provider_retry_delay(asked, 1, 300.0) == 7.0
    huge = ProviderError(
        ProviderErrorKind.RATE_LIMIT, "429", status_code=429, retry_after=3600.0
    )
    assert provider_retry_delay(huge, 1, 300.0) == 30.0
    # Jitter only ever lengthens the wait, by at most a quarter.
    assert provider_retry_delay(asked, 1, 300.0, jitter=1.0) == pytest.approx(8.75)
    assert provider_retry_delay(ValueError("x"), 1, 300.0) is None


def test_each_call_reads_the_deadline_when_it_starts() -> None:
    remaining = [200.0]
    provider = FlakyProvider(
        [ProviderError(ProviderErrorKind.TRANSPORT, "cut")],
        ModelResponse(content=None, tool_calls=(_finish(),), finish_reason="tool_calls", usage={}),
    )
    model, _slept = _model(provider, deadline=lambda: remaining[0])

    def shrink(_delay: float) -> None:
        remaining[0] = 90.0

    model._sleep = shrink
    model.query(_conversation())

    assert [request.deadline_seconds for request in provider.requests] == [200.0, 90.0]


# -- thinking-model replay ----------------------------------------------------


def test_flat_reasoning_is_kept_and_replayed_on_the_current_tool_chain() -> None:
    first = ModelResponse(
        content=None,
        tool_calls=(ToolCall("c1", "finish", json.dumps({"answer": "a"})),),
        finish_reason="tool_calls",
        usage={},
        reasoning="plan: write the file, then finish",
    )
    provider = FlakyProvider([], first)
    model, _slept = _model(provider)

    message = model.query(_conversation())
    assert message["reasoning_content"] == "plan: write the file, then finish"

    history = _conversation() + [
        message,
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    model.query(history)
    replayed = provider.requests[-1].messages
    assistant = [item for item in replayed if item.role == "assistant"]
    assert assistant[0].reasoning_text == "plan: write the file, then finish"

    payload = OpenAIChatCompletionsProvider._request_payload(provider.requests[-1])
    sent = [item for item in payload["messages"] if item["role"] == "assistant"]
    assert sent[0]["reasoning_content"] == "plan: write the file, then finish"
    assert "reasoning_content" not in json.dumps(
        [item for item in payload["messages"] if item["role"] != "assistant"]
    )


def test_reasoning_before_the_latest_user_turn_is_not_sent() -> None:
    request = ModelRequest(
        model="deepseek-ai/DeepSeek-V4.1-Flash",
        messages=(
            ModelMessage("user", "task"),
            ModelMessage(
                "assistant",
                None,
                tool_calls=(ToolCall("c1", "finish", "{}"),),
                reasoning_text="old plan",
            ),
            ModelMessage("tool", "verification failed", tool_call_id="c1"),
            ModelMessage("user", "repair the failing test"),
            ModelMessage(
                "assistant",
                None,
                tool_calls=(ToolCall("c2", "finish", "{}"),),
                reasoning_text="new plan",
            ),
            ModelMessage("tool", "ok", tool_call_id="c2"),
        ),
    )
    payload = OpenAIChatCompletionsProvider._request_payload(request)
    assistants = [item for item in payload["messages"] if item["role"] == "assistant"]

    assert "reasoning_content" not in assistants[0]
    assert assistants[1]["reasoning_content"] == "new plan"


def test_a_gateway_that_refuses_replay_gets_the_reduced_request() -> None:
    payload = {
        "model": "m",
        "messages": [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": None, "reasoning_content": "plan"},
        ],
        "reasoning_effort": "max",
    }
    reduced = _without_optional_tuning(payload)

    assert "reasoning_effort" not in reduced
    assert all("reasoning_content" not in item for item in reduced["messages"])
    # The caller's payload is not edited in place.
    assert payload["messages"][1]["reasoning_content"] == "plan"


def test_only_assistant_messages_may_carry_reasoning_text() -> None:
    with pytest.raises(ValueError):
        ModelMessage("user", "x", reasoning_text="no")


def test_engine_context_counts_and_sheds_replayed_reasoning() -> None:
    agent = DefaultAgent.__new__(DefaultAgent)
    agent.messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "reasoning_content": "r" * 4000},
    ]
    assert agent._estimate_tokens() >= 1000
    assert agent._elide_tool_arguments(agent.messages[2]) is True
    assert "reasoning_content" not in agent.messages[2]
    assert agent._estimate_tokens() < 10


def test_a_long_reasoning_reply_fits_the_stream_limits() -> None:
    # 16 MiB of SSE framing is roughly 65k streamed tokens on a gateway that
    # sends one token per event; a max-effort reply can exceed that.
    for provider in (OpenAIChatCompletionsProvider, AnthropicMessagesProvider):
        assert provider.MAX_SSE_TOTAL_CHARS >= 128 * 1024 * 1024
        assert provider.MAX_SSE_EVENTS >= 500_000


# -- cached bytecode ------------------------------------------------------------


def test_a_same_second_same_size_rewrite_invalidates_cached_bytecode(tmp_path) -> None:
    import os
    import subprocess
    import sys

    from karox.atomic_io import atomic_write

    wrong = b"def add(a, b):\n    return 0*a\n"
    right = b"def add(a, b):\n    return a+b\n"
    assert len(wrong) == len(right)
    module = tmp_path / "calc.py"
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    env.pop("PYTHONDONTWRITEBYTECODE", None)

    def run() -> str:
        completed = subprocess.run(
            [sys.executable, "-c", "import calc; print(calc.add(2, 2))"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return completed.stdout.strip()

    second = 1_000_000_000
    module.write_bytes(wrong)
    pinned = (module.stat().st_mtime_ns // second) * second
    os.utime(module, ns=(pinned, pinned))
    assert run() == "0"  # compiled; the cache records this second and size

    # The hazard itself: same second, same size, different code -> stale import.
    module.write_bytes(right)
    os.utime(module, ns=(pinned, pinned + second // 2))
    assert run() == "0"

    # A KaroX write always lands in a later second than the version it
    # replaces -- even when the clock has not reached that second yet -- so a
    # cached form of the old version is stale and the new code is imported.
    import time

    ahead = (time.time_ns() // second + 5) * second
    os.utime(module, ns=(ahead, ahead))
    atomic_write(module, right)
    assert module.stat().st_mtime_ns // second == ahead // second + 1
    assert run() == "4"


def test_non_python_files_keep_their_natural_mtime(tmp_path) -> None:
    from karox.atomic_io import atomic_write

    target = tmp_path / "notes.txt"
    atomic_write(target, b"one")
    before = target.stat().st_mtime_ns
    atomic_write(target, b"two")
    after = target.stat().st_mtime_ns
    assert after - before < 1_000_000_000
