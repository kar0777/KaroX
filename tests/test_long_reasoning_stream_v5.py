from __future__ import annotations

import httpx

from _support import SRC  # noqa: F401
from karox.provider_adapters import _StreamingAdapter
from karox.providers import ModelEventKind, OpenAIChatCompletionsProvider


def test_reasoning_heavy_chat_stream_can_exceed_old_10k_event_ceiling() -> None:
    neutral = 'data: {"choices":[]}\n\n'
    body = neutral * 10_001 + "data: [DONE]\n\n"
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://provider.example/v1/chat/completions"),
        headers={"Content-Type": "text/event-stream"},
        content=body.encode("utf-8"),
    )

    events = list(
        OpenAIChatCompletionsProvider._stream_events(
            response, attempts=1, deadline=float("inf")
        )
    )

    assert OpenAIChatCompletionsProvider.MAX_SSE_EVENTS >= 100_000
    assert _StreamingAdapter.MAX_SSE_EVENTS >= 100_000
    assert events[-1].kind is ModelEventKind.COMPLETION
