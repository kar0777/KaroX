"""Each API's own shapes reach the engine and go back to the API intact.

Found by the 2026-11-15 audit of the model-call layer: every model family but
the Chat Completions one said "done" or "cut off" in words the engine did not
read, and Anthropic/Gemini replays sent shapes those APIs reject with a 400.
"""

from __future__ import annotations

from typing import Iterator

import httpx
import pytest

from karox.provider_adapter_base import drain_after_end
from karox.provider_adapters import AnthropicMessagesProvider, GeminiGenerateContentProvider
from karox.provider_types import normalized_finish_reason
from karox.providers import (
    ModelMessage,
    ModelRequest,
    ProviderError,
    ProviderErrorKind,
    ReasoningBlock,
    ToolCall,
)


def test_every_api_says_stop_and_length_in_the_engine_words() -> None:
    for reason in ("stop", "end_turn", "stop_sequence", "completed", "STOP"):
        assert normalized_finish_reason(reason) == "stop", reason
    for reason in ("length", "max_tokens", "max_output_tokens", "MAX_TOKENS"):
        assert normalized_finish_reason(reason) == "length", reason
    assert normalized_finish_reason("tool_calls") == "tool_calls"
    assert normalized_finish_reason("SAFETY") == "SAFETY"
    assert normalized_finish_reason(None) is None


def _follow_up() -> ModelRequest:
    call = ToolCall("call-1", "repo_git_status", "")
    return ModelRequest(
        model="m",
        messages=(
            ModelMessage("user", "look"),
            ModelMessage(
                "assistant",
                "",
                tool_calls=(call,),
                reasoning_blocks=(ReasoningBlock("redacted_thinking", data="ENCRYPTED"),),
            ),
            ModelMessage("tool", "{}", tool_call_id="call-1"),
        ),
        deadline_seconds=2,
    )


def test_anthropic_replay_has_no_empty_text_and_keeps_redacted_thinking() -> None:
    payload = AnthropicMessagesProvider("https://provider.example/v1")._request_payload(_follow_up())
    assistant = payload["messages"][1]["content"]
    kinds = [block["type"] for block in assistant]
    assert kinds == ["redacted_thinking", "tool_use"], assistant
    assert assistant[0]["data"] == "ENCRYPTED"
    assert assistant[1]["input"] == {}


def test_gemini_replays_a_parameterless_call_as_empty_arguments() -> None:
    payload = GeminiGenerateContentProvider("https://provider.example/v1beta")._request_payload(_follow_up())
    parts = [part for content in payload["contents"] for part in content.get("parts", ())]
    calls = [part["functionCall"] for part in parts if "functionCall" in part]
    assert calls == [{"name": "repo_git_status", "args": {}}]


def _records(fail_with: BaseException) -> Iterator[int]:
    yield 1
    yield 2
    raise fail_with


@pytest.mark.parametrize(
    "error",
    [httpx.ReadError("reset"), ProviderError(ProviderErrorKind.TRANSPORT, "deadline expired while reading stream")],
)
def test_a_broken_tail_after_the_end_is_not_a_failure(error: BaseException) -> None:
    assert list(drain_after_end(_records(error), lambda: True)) == [1, 2]
    with pytest.raises(type(error)):
        list(drain_after_end(_records(error), lambda: False))


def test_a_malformed_tail_still_fails_after_the_end() -> None:
    malformed = ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "bad frame")
    with pytest.raises(ProviderError):
        list(drain_after_end(_records(malformed), lambda: True))
