from __future__ import annotations

import json
from typing import Iterator
from unittest.mock import patch

import httpx

from _support import SRC  # noqa: F401
from karox.providers import (
    ModelMessage,
    ModelRequest,
    OpenAIChatCompletionsProvider,
    ToolCall,
)


class _StreamContext:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    def __enter__(self) -> httpx.Response:
        return self.response

    def __exit__(self, *args: object) -> None:
        self.response.close()
        return None


class _Client:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def stream(self, *_args: object, **_kwargs: object) -> _StreamContext:
        return _StreamContext(self.response)


def _response(events: list[object]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    body += "data: [DONE]\n\n"
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    return httpx.Response(
        200,
        request=request,
        content=body.encode("utf-8"),
        headers={"Content-Type": "text/event-stream"},
    )


def test_openrouter_style_null_tool_fragments_are_valid() -> None:
    events = [
        {
            "id": "response-ox",
            "choices": [{
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call-ox",
                    "type": "function",
                    "function": {"name": "repo_read_file", "arguments": '{"path":'},
                }]},
                "finish_reason": None,
            }],
        },
        {
            "id": "response-ox",
            "choices": [{
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": None,
                    "type": None,
                    "function": {"name": None, "arguments": '"README.md"}'},
                }]},
                "finish_reason": None,
            }],
        },
        {
            "id": "response-ox",
            "choices": [{"delta": {"tool_calls": None}, "finish_reason": None}],
        },
        {
            "id": "response-ox",
            "choices": [{"delta": None, "finish_reason": "tool_calls"}],
        },
    ]
    provider = OpenAIChatCompletionsProvider("https://provider.example/v1")
    request = ModelRequest(
        model="stealth/ox-alpha",
        messages=(ModelMessage("user", "inspect README"),),
    )
    with patch("karox.providers.httpx.Client", return_value=_Client(_response(events))):
        result = provider.complete(request)

    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == (
        ToolCall("call-ox", "repo_read_file", '{"path":"README.md"}'),
    )
