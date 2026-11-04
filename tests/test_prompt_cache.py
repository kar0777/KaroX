"""Prompt-prefix caching: the run's cache key and why a request missed."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx

from karox.engine_provider_bridge import EngineProviderModel
from karox.prompt_cache_telemetry import PromptPrefixTracker, summarize
from karox.provider_adapters import AnthropicMessagesProvider, OpenAIResponsesProvider
from karox.providers import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    OpenAIChatCompletionsProvider,
    ProviderTool,
    ToolCall,
)


class _Recording:
    provider_name = "recording"

    def __init__(self, usages: List[Dict[str, int]]) -> None:
        self.requests: List[ModelRequest] = []
        self._usages = list(usages)

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        usage = self._usages.pop(0) if self._usages else {}
        return ModelResponse(
            content=None,
            tool_calls=(ToolCall(f"f{len(self.requests)}", "finish", json.dumps({"answer": "ok"})),),
            finish_reason="tool_calls",
            usage=usage,
        )

    def close(self) -> None:
        return None


_TOOLS = (ProviderTool("git_status", "status", {"type": "object", "properties": {}}),)


def _model(provider: Any, cache_key: Any = "karox-abc") -> EngineProviderModel:
    return EngineProviderModel(
        provider=provider,
        model_id="m",
        tools=_TOOLS,
        deadline_seconds=60.0,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
        cache_key=cache_key,
    )


def _history(turns: int) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do it"},
    ]
    for index in range(turns):
        messages.append({"role": "assistant", "content": f"step {index}"})
        messages.append({"role": "user", "content": f"observation {index}"})
    return messages


def test_every_request_of_a_run_carries_the_same_cache_key() -> None:
    provider = _Recording([{}, {}])
    model = _model(provider)
    model.query(_history(0))
    model.query(_history(1))
    keys = [request.cache_key for request in provider.requests]
    assert keys[0] and keys[0] == keys[1]
    assert keys[0].startswith("karox-abc-")


def test_a_different_system_prompt_gets_a_different_key() -> None:
    provider = _Recording([{}, {}])
    model = _model(provider)
    model.query(_history(0))
    changed = _history(0)
    changed[0] = {"role": "system", "content": "another system prompt"}
    model.query(changed)
    assert provider.requests[0].cache_key != provider.requests[1].cache_key


def test_without_a_key_no_cache_key_is_sent() -> None:
    provider = _Recording([{}])
    _model(provider, cache_key=None).query(_history(0))
    assert provider.requests[0].cache_key is None


def test_the_engine_message_reports_why_a_large_prompt_missed() -> None:
    provider = _Recording(
        [
            {"prompt_tokens": 5000, "cache_read_tokens": 0},
            {"prompt_tokens": 6000, "cache_read_tokens": 4800},
            {"prompt_tokens": 7000, "cache_read_tokens": 0},
        ]
    )
    model = _model(provider)
    first = model.query(_history(0))
    second = model.query(_history(1))
    third = model.query(_history(2))
    assert first["extra"]["prompt_cache"]["cause"] == "first_request"
    assert second["extra"]["prompt_cache"]["full_miss"] is False
    # Append-only history and still nothing cached: the provider dropped it.
    assert third["extra"]["prompt_cache"]["cause"] == "provider_miss"


def test_a_rewritten_message_is_named_as_the_cause() -> None:
    tracker = PromptPrefixTracker()
    one = [ModelMessage("system", "s"), ModelMessage("user", "u"), ModelMessage("assistant", "a")]
    tracker.classify(tracker.observe(one, _TOOLS), {"prompt_tokens": 4000, "cache_read_tokens": 100})
    two = [ModelMessage("system", "s"), ModelMessage("user", "u (clipped)"), ModelMessage("assistant", "a"), ModelMessage("user", "next")]
    result = tracker.classify(tracker.observe(two, _TOOLS), {"prompt_tokens": 4000, "cache_read_tokens": 0})
    assert result["cause"] == "history_rewrite"
    assert result["rewritten_at"] == 1


def test_a_changed_tool_table_is_named_as_the_cause() -> None:
    tracker = PromptPrefixTracker()
    messages = [ModelMessage("system", "s"), ModelMessage("user", "u")]
    tracker.classify(tracker.observe(messages, _TOOLS), {"prompt_tokens": 4000, "cache_read_tokens": 10})
    other_tools = _TOOLS + (ProviderTool("git_diff", "diff", {"type": "object", "properties": {}}),)
    result = tracker.classify(
        tracker.observe(messages + [ModelMessage("user", "x")], other_tools),
        {"prompt_tokens": 4000, "cache_read_tokens": 0},
    )
    assert result["cause"] == "tools_changed"


def test_a_route_that_never_reports_cached_tokens_is_not_counted_as_missing() -> None:
    tracker = PromptPrefixTracker()
    messages = [ModelMessage("system", "s"), ModelMessage("user", "u")]
    tracker.classify(tracker.observe(messages, _TOOLS), {"prompt_tokens": 4000})
    result = tracker.classify(
        tracker.observe(messages + [ModelMessage("user", "x")], _TOOLS), {"prompt_tokens": 4100}
    )
    assert result["full_miss"] is None
    assert result["cause"] == "unreported"


def test_small_prompts_are_never_misses() -> None:
    tracker = PromptPrefixTracker()
    result = tracker.classify(
        tracker.observe([ModelMessage("user", "u")], _TOOLS), {"prompt_tokens": 300}
    )
    assert result["full_miss"] is False


def test_summary_counts_full_misses_by_cause() -> None:
    history = [
        {"role": "assistant", "prompt_cache": {"prompt_tokens": 5000, "cached_tokens": 0, "full_miss": True, "cause": "first_request"}},
        {"role": "tool", "content": "x"},
        {"role": "assistant", "prompt_cache": {"prompt_tokens": 6000, "cached_tokens": 5000, "full_miss": False}},
        {"role": "assistant", "prompt_cache": {"prompt_tokens": 7000, "cached_tokens": 0, "full_miss": True, "cause": "history_rewrite"}},
    ]
    summary = summarize(history)
    assert summary["requests"] == 3
    assert summary["full_misses"] == 2
    assert summary["causes"] == {"first_request": 1, "history_rewrite": 1}
    assert summary["cached_share"] == round(5000 / 18000, 4)


class _StreamContext:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    def __enter__(self) -> httpx.Response:
        return self.response

    def __exit__(self, *args: object) -> None:
        self.response.close()


class _Client:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def stream(self, method: str, url: str, **kwargs: Any) -> _StreamContext:
        self.calls.append({"method": method, "url": url, **kwargs})
        body = (
            "data: "
            + json.dumps({"id": "r", "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
            + "\n\ndata: [DONE]\n\n"
        )
        return _StreamContext(
            httpx.Response(
                200,
                request=httpx.Request("POST", url),
                content=body.encode("utf-8"),
                headers={"Content-Type": "text/event-stream"},
            )
        )


def _sent_payload(endpoint: str) -> Dict[str, Any]:
    from unittest.mock import patch

    client = _Client()
    provider = OpenAIChatCompletionsProvider(endpoint)
    request = ModelRequest(
        model="m",
        messages=(ModelMessage("user", "hi"),),
        deadline_seconds=5,
        cache_key="karox-abc-123",
    )
    with patch("karox.providers.httpx.Client", return_value=client):
        provider.complete(request)
    return client.calls[0]["json"]


def test_chat_completions_sends_the_key_only_to_openai() -> None:
    assert _sent_payload("https://api.openai.com/v1")["prompt_cache_key"] == "karox-abc-123"
    assert "prompt_cache_key" not in _sent_payload("https://api.deepseek.com/v1")


def test_responses_sends_the_key_only_to_openai() -> None:
    request = ModelRequest(
        model="m", messages=(ModelMessage("user", "hi"),), deadline_seconds=5, cache_key="karox-k"
    )
    official = OpenAIResponsesProvider("https://api.openai.com/v1")._request_payload(request)
    gateway = OpenAIResponsesProvider("https://gateway.example/v1")._request_payload(request)
    assert official["prompt_cache_key"] == "karox-k"
    assert "prompt_cache_key" not in gateway


def test_anthropic_gets_cache_breakpoints_from_the_engine_key() -> None:
    request = ModelRequest(
        model="claude-x",
        messages=(ModelMessage("system", "sys"), ModelMessage("user", "hi")),
        deadline_seconds=5,
        cache_key="karox-k",
    )
    payload = AnthropicMessagesProvider("https://api.anthropic.com")._request_payload(request)
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
