"""Provider transport contracts: SSE, retries, redaction, usage, adapters.

Ported from the v5 suite.  Every transport here is a fake client: no test makes
a real model call or touches a public network.
"""

from __future__ import annotations

import json
import math
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Iterator
from unittest.mock import patch

import httpx

from karox.provider_adapters import (
    AnthropicMessagesProvider,
    GeminiGenerateContentProvider,
    OpenAIResponsesProvider,
    stream_error_kind,
)
from karox.providers import (
    ModelEventKind,
    ModelMessage,
    ModelRequest,
    OpenAIChatCompletionsProvider,
    ProviderError,
    ProviderErrorKind,
    ProviderTool,
    ReasoningBlock,
    ToolCall,
)


def sse_response(
    events: list[object],
    *,
    done: bool = True,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    records = []
    for event in events:
        data = event if isinstance(event, str) else json.dumps(event)
        records.append(f"data: {data}\n\n")
    if done:
        records.append("data: [DONE]\n\n")
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    response_headers = {"Content-Type": "text/event-stream", **(headers or {})}
    return httpx.Response(
        status,
        request=request,
        content="".join(records).encode("utf-8"),
        headers=response_headers,
    )

def error_response(status: int, body: str, **headers: str) -> httpx.Response:
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    return httpx.Response(
        status,
        request=request,
        text=body,
        headers=headers,
    )

class FakeStreamContext:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome

    def __enter__(self) -> httpx.Response:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        assert isinstance(self.outcome, httpx.Response)
        return self.outcome

    def __exit__(self, *args: object) -> None:
        if isinstance(self.outcome, httpx.Response):
            self.outcome.close()
        return None

class FakeClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, object]] = []

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs: object) -> FakeStreamContext:
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeStreamContext(self.outcomes.pop(0))

class InterruptedStream(httpx.SyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self.request = request

    def __iter__(self) -> Iterator[bytes]:
        yield b'data: {"id":"partial","choices":[{"delta":{"content":"part"}}]}\n\n'
        raise httpx.ReadError("stream broke", request=self.request)

class EarlyReadErrorStream(httpx.SyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self.request = request

    def __iter__(self) -> Iterator[bytes]:
        yield from ()
        raise httpx.ReadError("stream broke before first event", request=self.request)

class DrainTrackingStream(httpx.SyncByteStream):
    def __init__(self) -> None:
        self.exhausted = False

    def __iter__(self) -> Iterator[bytes]:
        yield (
            b'data: {"id":"response-1","choices":[{"delta":{"content":"ok"},'
            b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        )
        yield b": post-terminal heartbeat\n\n"
        self.exhausted = True

class OpenAIChatCompletionsProviderTests(unittest.TestCase):
    def request(self, *, deadline_seconds: float = 2.0) -> ModelRequest:
        return ModelRequest(
            model="test-model",
            messages=(
                ModelMessage("system", "bounded"),
                ModelMessage(
                    "assistant",
                    None,
                    (ToolCall("old-call", "repo_read_file", '{"path":"a.txt"}'),),
                ),
                ModelMessage("tool", '{"ok":true}', tool_call_id="old-call"),
            ),
            tools=(
                ProviderTool(
                    "repo_read_file",
                    "Read a file",
                    {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                ),
            ),
            temperature=0.2,
            max_output_tokens=123,
            deadline_seconds=deadline_seconds,
        )

    @staticmethod
    def fragmented_events() -> list[object]:
        return [
            {
                "id": "response-1",
                "choices": [{"delta": {"content": "work"}, "finish_reason": None}],
            },
            {
                "id": "response-1",
                "choices": [
                    {
                        "delta": {
                            "content": "ing",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-",
                                    "type": "function",
                                    "function": {
                                        "name": "repo_write_",
                                        "arguments": '{"path":"a.txt",',
                                    },
                                },
                                {
                                    "index": 1,
                                    "id": "call-2",
                                    "type": "function",
                                    "function": {
                                        "name": "git_",
                                        "arguments": "{",
                                    },
                                },
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "response-1",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "1",
                                    "function": {
                                        "name": "file",
                                        "arguments": '"content":"after"}',
                                    },
                                },
                                {
                                    "index": 1,
                                    "function": {
                                        "name": "status",
                                        "arguments": "}",
                                    },
                                },
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "response-1",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
            {
                "id": "response-1",
                "choices": [],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "ignored_boolean": True,
                    "ignored_negative": -1,
                },
            },
        ]

    def test_rejected_reasoning_effort_retries_once_without_optional_tuning(self) -> None:
        """A gateway with a different effort vocabulary gets one reduced retry.

        W&B DeepSeek rejects ``reasoning_effort=medium`` (it accepts
        low/high/xhigh/max/none), so the default effort made every live run
        fail with HTTP 400. The retry drops only optional tuning fields; the
        messages, tools and output cap are unchanged, and the error body is
        never read.
        """

        client = FakeClient(
            [
                error_response(400, '{"message":"reasoning_effort must be low or high"}'),
                sse_response(
                    [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]
                ),
            ]
        )
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1", max_transport_retries=0
        )
        request = replace(self.request(), reasoning_effort="medium")
        with patch("karox.providers.httpx.Client", return_value=client):
            result = provider.complete(request)

        self.assertEqual(result.content, "ok")
        self.assertEqual(len(client.calls), 2)
        first = client.calls[0]["json"]
        second = client.calls[1]["json"]
        self.assertEqual(first["reasoning_effort"], "medium")
        self.assertIn("temperature", first)
        self.assertNotIn("reasoning_effort", second)
        self.assertNotIn("temperature", second)
        self.assertEqual(second["tools"][0]["function"]["name"], "repo_read_file")
        self.assertEqual(second["max_tokens"], 123)

    def test_tuning_fallback_is_remembered_for_later_requests(self) -> None:
        client = FakeClient(
            [
                error_response(400, "bad reasoning_effort"),
                sse_response(
                    [{"choices": [{"delta": {"content": "one"}, "finish_reason": "stop"}]}]
                ),
                sse_response(
                    [{"choices": [{"delta": {"content": "two"}, "finish_reason": "stop"}]}]
                ),
            ]
        )
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1", max_transport_retries=0
        )
        request = replace(self.request(), reasoning_effort="medium")
        with patch("karox.providers.httpx.Client", return_value=client):
            first = provider.complete(request)
            second = provider.complete(request)

        self.assertEqual((first.content, second.content), ("one", "two"))
        self.assertEqual(len(client.calls), 3)
        self.assertIn("reasoning_effort", client.calls[0]["json"])
        self.assertNotIn("reasoning_effort", client.calls[1]["json"])
        # The second request starts without the field: no failed attempt paid.
        self.assertNotIn("reasoning_effort", client.calls[2]["json"])

    def test_rejected_level_above_high_retries_at_high_and_remembers_it(self) -> None:
        client = FakeClient(
            [
                error_response(400, "bad reasoning_effort"),
                sse_response(
                    [{"choices": [{"delta": {"content": "one"}, "finish_reason": "stop"}]}]
                ),
                sse_response(
                    [{"choices": [{"delta": {"content": "two"}, "finish_reason": "stop"}]}]
                ),
            ]
        )
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1", max_transport_retries=0
        )
        request = replace(self.request(), reasoning_effort="max")
        with patch("karox.providers.httpx.Client", return_value=client):
            first = provider.complete(request)
            second = provider.complete(request)

        self.assertEqual((first.content, second.content), ("one", "two"))
        efforts = [call["json"].get("reasoning_effort") for call in client.calls]
        # One rejected attempt at max, then high; the next request starts at high.
        self.assertEqual(efforts, ["max", "high", "high"])
        # Only the level changed: the other tuning fields are still sent.
        self.assertIn("temperature", client.calls[1]["json"])
        self.assertEqual(client.calls[1]["json"]["max_tokens"], 123)

    def test_level_above_high_drops_tuning_only_after_high_is_rejected_too(self) -> None:
        client = FakeClient(
            [
                error_response(400, "bad reasoning_effort"),
                error_response(400, "bad reasoning_effort"),
                sse_response(
                    [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]
                ),
            ]
        )
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1", max_transport_retries=0
        )
        request = replace(self.request(), reasoning_effort="max")
        with patch("karox.providers.httpx.Client", return_value=client):
            result = provider.complete(request)

        self.assertEqual(result.content, "ok")
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(client.calls[0]["json"]["reasoning_effort"], "max")
        self.assertEqual(client.calls[1]["json"]["reasoning_effort"], "high")
        self.assertNotIn("reasoning_effort", client.calls[2]["json"])

    def test_reduced_retry_failing_again_reports_the_first_error(self) -> None:
        client = FakeClient(
            [
                error_response(400, "bad reasoning_effort"),
                error_response(400, "bad tool schema"),
            ]
        )
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1", max_transport_retries=0
        )
        request = replace(self.request(), reasoning_effort="medium")
        with (
            patch("karox.providers.httpx.Client", return_value=client),
            self.assertRaises(ProviderError) as raised,
        ):
            provider.complete(request)

        self.assertEqual(raised.exception.kind, ProviderErrorKind.INVALID_REQUEST)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("HTTP 400", raised.exception.safe_message)

    def test_only_invalid_request_earns_the_reduced_retry(self) -> None:
        client = FakeClient([error_response(401, "no")])
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1", max_transport_retries=0
        )
        request = replace(self.request(), reasoning_effort="medium")
        with (
            patch("karox.providers.httpx.Client", return_value=client),
            self.assertRaises(ProviderError) as raised,
        ):
            provider.complete(request)

        self.assertEqual(raised.exception.kind, ProviderErrorKind.AUTHENTICATION)
        self.assertEqual(len(client.calls), 1)

    def test_stream_options_rejection_gets_a_reduced_retry(self) -> None:
        """Older gateways reject stream_options; the retry must drop it."""

        client = FakeClient(
            [
                error_response(400, "unknown field stream_options"),
                sse_response(
                    [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]
                ),
            ]
        )
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1", max_transport_retries=0
        )
        with patch("karox.providers.httpx.Client", return_value=client):
            result = provider.complete(self.request())

        self.assertEqual(result.content, "ok")
        self.assertEqual(len(client.calls), 2)
        self.assertIn("stream_options", client.calls[0]["json"])
        self.assertNotIn("stream_options", client.calls[1]["json"])
        self.assertNotIn("temperature", client.calls[1]["json"])

    def test_reduced_retry_is_remembered_per_model(self) -> None:
        client = FakeClient(
            [
                error_response(400, "bad reasoning_effort"),
                sse_response(
                    [{"choices": [{"delta": {"content": "one"}, "finish_reason": "stop"}]}]
                ),
                sse_response(
                    [{"choices": [{"delta": {"content": "two"}, "finish_reason": "stop"}]}]
                ),
            ]
        )
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1", max_transport_retries=0
        )
        first_request = replace(
            self.request(), model="model-a", reasoning_effort="medium"
        )
        second_request = replace(
            self.request(), model="model-b", reasoning_effort="high"
        )
        with patch("karox.providers.httpx.Client", return_value=client):
            first = provider.complete(first_request)
            second = provider.complete(second_request)

        self.assertEqual((first.content, second.content), ("one", "two"))
        self.assertEqual(len(client.calls), 3)
        # The rejected model starts reduced; the other model keeps its dial.
        self.assertNotIn("reasoning_effort", client.calls[1]["json"])
        self.assertEqual(client.calls[2]["json"]["reasoning_effort"], "high")
        self.assertIn("stream_options", client.calls[2]["json"])

    def test_reduced_failure_is_not_retried_again(self) -> None:
        client = FakeClient(
            [
                error_response(400, "bad reasoning_effort"),
                error_response(400, "still bad"),
                error_response(400, "still bad"),
            ]
        )
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1", max_transport_retries=0
        )
        request = replace(self.request(), reasoning_effort="medium")
        with (
            patch("karox.providers.httpx.Client", return_value=client),
            self.assertRaises(ProviderError),
        ):
            provider.complete(request)
        with (
            patch("karox.providers.httpx.Client", return_value=client),
            self.assertRaises(ProviderError),
        ):
            provider.complete(request)

        # First call: original + reduced. Second call: one attempt, because the
        # reduced shape already failed for this model and is not worth paying
        # for again.
        self.assertEqual(len(client.calls), 3)

    def test_usage_null_does_not_discard_a_complete_answer(self) -> None:
        client = FakeClient(
            [
                sse_response(
                    [
                        {
                            "choices": [
                                {"delta": {"content": "ok"}, "finish_reason": "stop"}
                            ]
                        },
                        {
                            "choices": [],
                            "usage": {
                                "prompt_tokens": 10.0,
                                "completion_tokens": None,
                                "total_tokens": 12,
                            },
                        },
                    ]
                )
            ]
        )
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1", max_transport_retries=0
        )
        with patch("karox.providers.httpx.Client", return_value=client):
            result = provider.complete(self.request())

        self.assertEqual(result.content, "ok")
        self.assertEqual(result.usage, {"prompt_tokens": 10, "total_tokens": 12})

    def test_deepseek_cache_alias_is_counted(self) -> None:
        client = FakeClient(
            [
                sse_response(
                    [
                        {
                            "choices": [
                                {"delta": {"content": "ok"}, "finish_reason": "stop"}
                            ]
                        },
                        {
                            "choices": [],
                            "usage": {
                                "prompt_tokens": 100,
                                "completion_tokens": 5,
                                "total_tokens": 105,
                                "prompt_cache_hit_tokens": 80,
                            },
                        },
                    ]
                )
            ]
        )
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1", max_transport_retries=0
        )
        with patch("karox.providers.httpx.Client", return_value=client):
            result = provider.complete(self.request())

        self.assertEqual(result.usage["cache_read_tokens"], 80)

    def test_usage_chunk_without_choices_is_accepted(self) -> None:
        client = FakeClient(
            [
                sse_response(
                    [
                        {
                            "choices": [
                                {"delta": {"content": "ok"}, "finish_reason": "stop"}
                            ]
                        },
                        {"usage": {"prompt_tokens": 3, "completion_tokens": 2}},
                    ]
                )
            ]
        )
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1", max_transport_retries=0
        )
        with patch("karox.providers.httpx.Client", return_value=client):
            result = provider.complete(self.request())

        self.assertEqual(result.content, "ok")
        self.assertEqual(result.usage, {"prompt_tokens": 3, "completion_tokens": 2})

    def test_repeated_tool_identity_fragments_are_not_concatenated(self) -> None:
        client = FakeClient(
            [
                sse_response(
                    [
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "call_1",
                                                "type": "function",
                                                "function": {
                                                    "name": "read_file",
                                                    "arguments": "",
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ]
                        },
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "call_1",
                                                "type": "function",
                                                "function": {
                                                    "name": "read_file",
                                                    "arguments": '{"path":"a"}',
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": "tool_calls",
                                }
                            ]
                        },
                    ]
                )
            ]
        )
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1", max_transport_retries=0
        )
        with patch("karox.providers.httpx.Client", return_value=client):
            result = provider.complete(self.request())

        self.assertEqual(len(result.tool_calls), 1)
        call = result.tool_calls[0]
        self.assertEqual(call.call_id, "call_1")
        self.assertEqual(call.name, "read_file")
        self.assertEqual(call.raw_arguments, '{"path":"a"}')

    def test_normalizes_streamed_request_tools_content_calls_and_usage(self) -> None:
        client = FakeClient([sse_response(self.fragmented_events())])
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1/",
            credential=lambda: "harmless-test-key",
            headers={"X-Tenant": "local"},
            query={"api-version": "2026-01-01"},
        )
        with patch("karox.providers.httpx.Client", return_value=client):
            result = provider.complete(self.request())

        self.assertEqual(
            client.calls[0]["url"],
            "https://provider.example/v1/chat/completions?api-version=2026-01-01",
        )
        self.assertEqual(client.calls[0]["method"], "POST")
        headers = client.calls[0]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer harmless-test-key")
        self.assertEqual(headers["Accept"], "text/event-stream")
        payload = client.calls[0]["json"]
        self.assertIs(payload["stream"], True)
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertEqual(payload["messages"][1]["tool_calls"][0]["id"], "old-call")
        self.assertEqual(payload["messages"][2]["tool_call_id"], "old-call")
        self.assertEqual(payload["tools"][0]["function"]["name"], "repo_read_file")
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(payload["max_tokens"], 123)
        self.assertEqual(result.content, "working")
        self.assertEqual(result.response_id, "response-1")
        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual(result.transport_attempts, 1)
        self.assertEqual(result.usage, {"prompt_tokens": 10, "completion_tokens": 4})
        self.assertEqual(
            result.tool_calls,
            (
                ToolCall(
                    "call-1",
                    "repo_write_file",
                    '{"path":"a.txt","content":"after"}',
                ),
                ToolCall("call-2", "git_status", "{}"),
            ),
        )

    def test_stream_exposes_normalized_delta_and_completion_events(self) -> None:
        client = FakeClient([sse_response(self.fragmented_events())])
        provider = OpenAIChatCompletionsProvider("https://provider.example/v1")
        with patch("karox.providers.httpx.Client", return_value=client):
            events = list(provider.stream(self.request()))

        self.assertEqual(events[0].kind, ModelEventKind.TEXT_DELTA)
        deltas = [
            item.tool_call_delta
            for item in events
            if item.kind == ModelEventKind.TOOL_CALL_DELTA
        ]
        self.assertEqual([item.index for item in deltas if item is not None], [0, 1, 0, 1])
        self.assertEqual(events[-2].kind, ModelEventKind.USAGE)
        self.assertEqual(events[-1].kind, ModelEventKind.COMPLETION)
        self.assertEqual(events[-1].finish_reason, "tool_calls")

    def test_provider_reuses_one_http_client_across_calls(self) -> None:
        client = FakeClient(
            [
                sse_response(self.fragmented_events()),
                sse_response(self.fragmented_events()),
            ]
        )
        provider = OpenAIChatCompletionsProvider("https://provider.example/v1")
        try:
            with patch("karox.providers.httpx.Client", return_value=client) as constructor:
                first = provider.complete(self.request())
                second = provider.complete(self.request())
            self.assertEqual(constructor.call_count, 1)
            self.assertEqual(len(client.calls), 2)
            for result in (first, second):
                metrics = result.transport_metrics
                self.assertLessEqual(
                    metrics["response_headers_ms"], metrics["first_event_ms"]
                )
                self.assertLessEqual(metrics["first_event_ms"], metrics["total_ms"])
        finally:
            provider.close()

    def test_completion_drains_http_body_after_done_for_connection_reuse(self) -> None:
        stream = DrainTrackingStream()
        raw = httpx.Response(
            200,
            request=httpx.Request(
                "POST", "https://provider.example/v1/chat/completions"
            ),
            stream=stream,
            headers={"Content-Type": "text/event-stream"},
        )
        client = FakeClient([raw])
        provider = OpenAIChatCompletionsProvider("https://provider.example/v1")
        try:
            with patch("karox.providers.httpx.Client", return_value=client):
                result = provider.complete(self.request())
            self.assertEqual(result.content, "ok")
            self.assertTrue(stream.exhausted)
        finally:
            provider.close()

    def test_openrouter_reasoning_summary_is_public_but_raw_detail_is_not(self) -> None:
        events = [
            {
                "id": "summary-1",
                "choices": [
                    {
                        "delta": {
                            "reasoning_details": [
                                {
                                    "type": "reasoning.summary",
                                    "summary": "Inspecting the ",
                                    "id": "s1",
                                    "format": "openai-responses-v1",
                                },
                                {
                                    "type": "reasoning.text",
                                    "text": "private deliberation must stay private",
                                    "id": "r1",
                                    "format": "openai-responses-v1",
                                },
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "summary-1",
                "choices": [
                    {
                        "delta": {
                            "reasoning_details": [
                                {
                                    "type": "reasoning.summary",
                                    "summary": "relevant code.",
                                    "id": "s1",
                                    "format": "openai-responses-v1",
                                }
                            ]
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        ]
        provider = OpenAIChatCompletionsProvider("https://provider.example/v1")
        client = FakeClient([sse_response(events), sse_response(events)])
        with patch("karox.providers.httpx.Client", return_value=client):
            streamed = list(provider.stream(self.request()))
            completed = provider.complete(self.request())

        summaries = [
            event.reasoning_summary_delta
            for event in streamed
            if event.kind == ModelEventKind.REASONING_SUMMARY_DELTA
        ]
        self.assertEqual(summaries, ["Inspecting the ", "relevant code."])
        self.assertEqual(completed.reasoning_summary, "Inspecting the relevant code.")
        self.assertNotIn(
            "private deliberation",
            "".join(item or "" for item in summaries),
        )

    def test_retries_only_transport_errors_before_response(self) -> None:
        request = httpx.Request("POST", "https://provider.example")
        client = FakeClient(
            [
                httpx.ConnectError("temporary", request=request),
                sse_response(
                    [
                        {
                            "choices": [
                                {"delta": {"content": "done"}, "finish_reason": "stop"}
                            ]
                        }
                    ]
                ),
            ]
        )
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1",
            max_transport_retries=1,
            retry_backoff_seconds=0,
        )
        with patch("karox.providers.httpx.Client", return_value=client):
            result = provider.complete(self.request())
        self.assertEqual(result.content, "done")
        self.assertEqual(result.transport_attempts, 2)
        self.assertEqual(len(client.calls), 2)

    def test_retries_read_error_after_headers_before_first_event(self) -> None:
        request = httpx.Request(
            "POST", "https://provider.example/v1/chat/completions"
        )
        interrupted = httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "text/event-stream"},
            stream=EarlyReadErrorStream(request),
        )
        client = FakeClient(
            [
                interrupted,
                sse_response(
                    [
                        {
                            "choices": [
                                {
                                    "delta": {"content": "recovered"},
                                    "finish_reason": "stop",
                                }
                            ]
                        }
                    ]
                ),
            ]
        )
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1",
            max_transport_retries=1,
            retry_backoff_seconds=0,
        )

        with patch("karox.providers.httpx.Client", return_value=client):
            result = provider.complete(self.request())

        self.assertEqual(result.content, "recovered")
        self.assertEqual(result.transport_attempts, 2)
        self.assertEqual(len(client.calls), 2)

    def test_retry_exhaustion_does_not_expose_exception_text(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        request = httpx.Request("POST", "https://provider.example")
        client = FakeClient(
            [
                httpx.ConnectError(f"first failure {secret}", request=request),
                httpx.ConnectError(f"second failure {secret}", request=request),
            ]
        )
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1",
            max_transport_retries=1,
            retry_backoff_seconds=0,
        )
        with (
            patch("karox.providers.httpx.Client", return_value=client),
            self.assertRaises(ProviderError) as raised,
        ):
            provider.complete(self.request())

        self.assertEqual(raised.exception.kind, ProviderErrorKind.TRANSPORT)
        self.assertIn("ConnectError", raised.exception.safe_message)
        self.assertNotIn(secret, raised.exception.safe_message)
        self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(raised.exception.transport_attempts, 2)
        self.assertEqual(len(client.calls), 2)

    def test_retry_respects_request_deadline(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        request = httpx.Request("POST", "https://provider.example")
        client = FakeClient([httpx.ConnectError(f"failed {secret}", request=request)])
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1",
            max_transport_retries=2,
            retry_backoff_seconds=1,
        )
        with (
            patch("karox.providers.httpx.Client", return_value=client),
            patch("karox.providers.time.monotonic", return_value=100.0),
            self.assertRaises(ProviderError) as raised,
        ):
            provider.complete(self.request(deadline_seconds=0.05))
        self.assertEqual(raised.exception.kind, ProviderErrorKind.TRANSPORT)
        self.assertIn("deadline", raised.exception.safe_message)
        self.assertNotIn(secret, str(raised.exception))

    def test_classifies_http_errors_without_exposing_response_body(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        cases = {
            401: ProviderErrorKind.AUTHENTICATION,
            402: ProviderErrorKind.BUDGET_EXCEEDED,
            403: ProviderErrorKind.PERMISSION,
            404: ProviderErrorKind.MODEL_UNAVAILABLE,
            422: ProviderErrorKind.INVALID_REQUEST,
            429: ProviderErrorKind.RATE_LIMIT,
            503: ProviderErrorKind.PROVIDER_INTERNAL,
        }
        for status, kind in cases.items():
            with self.subTest(status=status):
                # A 422 is an INVALID_REQUEST: the transport now spends one
                # reduced retry before reporting, so the fake endpoint answers
                # twice. The first error is still the one surfaced.
                outcomes = [
                    error_response(
                        status,
                        json.dumps({"error": secret}),
                        **{"Retry-After": "2.5"},
                    )
                    for _ in range(
                        2 if kind is ProviderErrorKind.INVALID_REQUEST else 1
                    )
                ]
                client = FakeClient(outcomes)
                provider = OpenAIChatCompletionsProvider("https://provider.example/v1")
                with (
                    patch("karox.providers.httpx.Client", return_value=client),
                    self.assertRaises(ProviderError) as raised,
                ):
                    provider.complete(self.request())
                self.assertEqual(raised.exception.kind, kind)
                self.assertEqual(raised.exception.status_code, status)
                self.assertNotIn(secret, str(raised.exception))
                if status == 429:
                    self.assertEqual(raised.exception.retry_after, 2.5)

    def test_http_error_names_the_vendor_code_but_not_the_message(self) -> None:
        echoed = "your prompt said: please keep this private"
        bodies = {
            "openai": (
                {
                    "error": {
                        "message": echoed,
                        "type": "invalid_request_error",
                        "param": "max_tokens",
                        "code": "context_length_exceeded",
                    }
                },
                ("context_length_exceeded", "invalid_request_error", "param=max_tokens"),
            ),
            "vllm": (
                {
                    "object": "error",
                    "message": echoed,
                    "type": "BadRequestError",
                    "param": None,
                    "code": 400,
                },
                ("BadRequestError",),
            ),
            "secret-shaped": (
                {"error": {"code": "sk-abcdefghijklmnopqrstuvwxyz123456", "message": echoed}},
                (),
            ),
            "not-json": ("<html>" + echoed + "</html>", ()),
        }
        for name, (body, expected) in bodies.items():
            with self.subTest(body=name):
                text = body if isinstance(body, str) else json.dumps(body)
                client = FakeClient([error_response(400, text) for _ in range(2)])
                provider = OpenAIChatCompletionsProvider("https://provider.example/v1")
                with (
                    patch("karox.providers.httpx.Client", return_value=client),
                    self.assertRaises(ProviderError) as raised,
                ):
                    provider.complete(self.request())
                message = raised.exception.safe_message
                self.assertEqual(raised.exception.kind, ProviderErrorKind.INVALID_REQUEST)
                self.assertIn("HTTP 400: Bad Request", message)
                for code in expected:
                    self.assertIn(code, message)
                if not expected:
                    self.assertTrue(message.endswith("Bad Request"), message)
                self.assertNotIn("private", message)
                self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", message)

    def test_an_empty_type_is_a_fragment_not_a_foreign_tool_call(self) -> None:
        # Measured on StepFun's Step Plan endpoint: one tool call streams as a
        # first chunk carrying id/type/name and later chunks carrying
        # {"id": "", "type": ""} with only the argument text appended. Rejecting
        # that shape made every tool call on that endpoint fail.
        fragments = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "chatcmpl-tool-1",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": "{"},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "",
                                    "type": "",
                                    "function": {"name": "", "arguments": '"path": '},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "",
                                    "type": "",
                                    "function": {"name": "", "arguments": '"sample.txt"}'},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        client = FakeClient([sse_response(fragments)])
        provider = OpenAIChatCompletionsProvider("https://provider.example/v1")

        with patch("karox.providers.httpx.Client", return_value=client):
            result = provider.complete(self.request())

        self.assertEqual(
            result.tool_calls,
            (ToolCall("chatcmpl-tool-1", "read_file", '{"path": "sample.txt"}'),),
        )
        self.assertEqual(result.finish_reason, "tool_calls")

    def test_a_foreign_tool_call_type_is_still_rejected(self) -> None:
        # The empty-string tolerance above must not become a general allowance:
        # a call that names a different kind of tool is still not executable.
        event = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "web_search",
                                "function": {"name": "search", "arguments": "{}"},
                            }
                        ]
                    }
                }
            ]
        }
        client = FakeClient([sse_response([event])])
        provider = OpenAIChatCompletionsProvider("https://provider.example/v1")

        with (
            patch("karox.providers.httpx.Client", return_value=client),
            self.assertRaises(ProviderError) as raised,
        ):
            provider.complete(self.request())

        self.assertEqual(raised.exception.kind, ProviderErrorKind.MALFORMED_RESPONSE)

    def test_rejects_malformed_sse_json_and_event_schema(self) -> None:
        malformed = (
            "not-json",
            [],
            {},
            {"choices": "invalid"},
            {"choices": [{"delta": {"content": 42}}]},
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": True, "function": {}}]}}
                ]
            },
            {"choices": [], "usage": []},
        )
        for event in malformed:
            with self.subTest(event=event):
                client = FakeClient([sse_response([event])])
                provider = OpenAIChatCompletionsProvider("https://provider.example/v1")
                with (
                    patch("karox.providers.httpx.Client", return_value=client),
                    self.assertRaises(ProviderError) as raised,
                ):
                    provider.complete(self.request())
                self.assertEqual(
                    raised.exception.kind, ProviderErrorKind.MALFORMED_RESPONSE
                )

    def test_sse_reader_enforces_resource_bounds(self) -> None:
        neutral = {"choices": [], "usage": {}}
        cases = (
            (
                "MAX_SSE_LINE_CHARS",
                8,
                sse_response([neutral]),
                "line exceeds",
            ),
            (
                "MAX_SSE_EVENTS",
                1,
                sse_response([neutral]),
                "too many events",
            ),
            (
                "MAX_SSE_TOTAL_CHARS",
                20,
                sse_response([neutral]),
                "stream exceeds",
            ),
        )
        for attribute, limit, item, message in cases:
            with (
                self.subTest(attribute=attribute),
                patch.object(OpenAIChatCompletionsProvider, attribute, limit),
                self.assertRaisesRegex(ProviderError, message) as raised,
            ):
                list(
                    OpenAIChatCompletionsProvider._stream_events(
                        item,
                        1,
                        float("inf"),
                    )
                )
            self.assertEqual(
                raised.exception.kind,
                ProviderErrorKind.MALFORMED_RESPONSE,
            )

    def test_rejects_invalid_known_usage_values(self) -> None:
        for value in (True, -1, 1.5, "1"):
            with self.subTest(value=value):
                client = FakeClient(
                    [
                        sse_response(
                            [
                                {
                                    "choices": [],
                                    "usage": {"prompt_tokens": value},
                                }
                            ]
                        )
                    ]
                )
                provider = OpenAIChatCompletionsProvider(
                    "https://provider.example/v1"
                )
                with (
                    patch("karox.providers.httpx.Client", return_value=client),
                    self.assertRaises(ProviderError) as raised,
                ):
                    provider.complete(self.request())
                self.assertEqual(
                    raised.exception.kind,
                    ProviderErrorKind.MALFORMED_RESPONSE,
                )

    def test_missing_done_is_interrupted_transport_and_is_not_retried(self) -> None:
        client = FakeClient(
            [
                sse_response(
                    [{"choices": [{"delta": {}, "finish_reason": "stop"}]}],
                    done=False,
                )
            ]
        )
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1", max_transport_retries=3
        )
        with (
            patch("karox.providers.httpx.Client", return_value=client),
            self.assertRaises(ProviderError) as raised,
        ):
            provider.complete(self.request())
        self.assertEqual(raised.exception.kind, ProviderErrorKind.TRANSPORT)
        self.assertEqual(len(client.calls), 1)

    def test_read_error_after_first_event_is_not_retried(self) -> None:
        request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
        interrupted = httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "text/event-stream"},
            stream=InterruptedStream(request),
        )
        client = FakeClient([interrupted])
        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1", max_transport_retries=3
        )
        with (
            patch("karox.providers.httpx.Client", return_value=client),
            self.assertRaises(ProviderError) as raised,
        ):
            provider.complete(self.request())
        self.assertEqual(raised.exception.kind, ProviderErrorKind.TRANSPORT)
        self.assertIn("interrupted", raised.exception.safe_message)
        self.assertEqual(len(client.calls), 1)

    def test_ignores_non_finite_retry_after(self) -> None:
        for raw_value in ("nan", "inf", "-inf"):
            with self.subTest(raw_value=raw_value):
                item = error_response(429, "rate limited", **{"Retry-After": raw_value})
                error = OpenAIChatCompletionsProvider._http_error(item)
                self.assertIsNone(error.retry_after)
                self.assertFalse(
                    error.retry_after is not None and math.isfinite(error.retry_after)
                )

    def test_reads_both_retry_after_forms(self) -> None:
        def retry_after(raw_value: str) -> float | None:
            item = error_response(429, "rate limited", **{"Retry-After": raw_value})
            return OpenAIChatCompletionsProvider._http_error(item).retry_after

        now = datetime.now(timezone.utc)
        # RFC 9110 allows a delay or an HTTP date, and gateways send both; the
        # date form used to be dropped, leaving the router nothing to honour.
        self.assertAlmostEqual(
            retry_after(format_datetime(now + timedelta(seconds=120), usegmt=True)),
            120,
            delta=5,
        )
        self.assertEqual(
            retry_after(format_datetime(now - timedelta(seconds=30), usegmt=True)),
            0.0,
        )
        self.assertEqual(retry_after("2.5"), 2.5)
        for unusable in ("", "later", "Sun, 99 Xxx 2026 99:99:99 GMT"):
            with self.subTest(raw_value=unusable):
                self.assertIsNone(retry_after(unusable))

    def test_validates_numeric_configuration_types(self) -> None:
        invalid = (
            {"timeout_seconds": True},
            {"timeout_seconds": "1"},
            {"max_transport_retries": True},
            {"max_transport_retries": 1.5},
            {"retry_backoff_seconds": False},
            {"retry_backoff_seconds": "0.1"},
        )
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                OpenAIChatCompletionsProvider(
                    "https://provider.example/v1", **options  # type: ignore[arg-type]
                )

    def test_credentials_require_https_or_an_http_loopback_endpoint(self) -> None:
        credential = lambda: "harmless-test-key"
        for base_url in (
            "http://localhost:8080/v1",
            "http://127.42.0.1:8080/v1",
            "http://[::1]:8080/v1",
        ):
            with self.subTest(base_url=base_url):
                provider = OpenAIChatCompletionsProvider(
                    base_url, credential=credential
                )
                self.assertEqual(
                    provider._request_headers()["Authorization"],
                    "Bearer harmless-test-key",
                )

        for base_url in (
            "http://provider.example/v1",
            "http://localhost.example/v1",
        ):
            with self.subTest(base_url=base_url), self.assertRaisesRegex(
                ValueError, "HTTPS or a loopback"
            ):
                OpenAIChatCompletionsProvider(base_url, credential=credential)

        provider = OpenAIChatCompletionsProvider("http://provider.example/v1")
        self.assertEqual(
            provider.endpoint, "http://provider.example/v1/chat/completions"
        )

    def test_validates_credential_header_query_and_accessor_boundaries(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        with self.assertRaisesRegex(ValueError, "without credentials"):
            OpenAIChatCompletionsProvider("https://user:pass@provider.example/v1")
        with self.assertRaisesRegex(ValueError, "query cannot contain credentials"):
            OpenAIChatCompletionsProvider(
                "https://provider.example/v1", query={"api_key": "value"}
            )
        with self.assertRaisesRegex(ValueError, "headers cannot contain credentials"):
            OpenAIChatCompletionsProvider(
                "https://provider.example/v1", headers={"Authorization": "value"}
            )
        with self.assertRaisesRegex(ValueError, "headers cannot contain credentials"):
            OpenAIChatCompletionsProvider(
                "https://provider.example/v1", headers={"X-Trace": secret}
            )

        def fail() -> str:
            raise RuntimeError(f"accessor leaked {secret}")

        provider = OpenAIChatCompletionsProvider(
            "https://provider.example/v1", credential=fail
        )
        with self.assertRaises(ProviderError) as raised:
            provider.complete(self.request())
        self.assertEqual(raised.exception.kind, ProviderErrorKind.AUTHENTICATION)
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, raised.exception.safe_message)

    def test_chat_completions_spells_the_effort_dial_flat_and_clamps_it_for_openai(self) -> None:
        cases = (
            ("https://api.openai.com/v1", "low", "low"),
            ("https://api.openai.com/v1", "high", "high"),
            ("https://api.openai.com/v1", "xhigh", "high"),
            ("https://api.openai.com/v1", "max", "high"),
            # Other gateways publish their own scale; W&B and DeepSeek accept
            # the levels above high, so the chosen level is sent unchanged.
            ("https://provider.example/v1", "low", "low"),
            ("https://provider.example/v1", "high", "high"),
            ("https://provider.example/v1", "xhigh", "xhigh"),
            ("https://api.inference.wandb.ai/v1", "max", "max"),
        )
        for base_url, asked, expected in cases:
            with self.subTest(base_url=base_url, effort=asked):
                client = FakeClient(
                    [sse_response([{"id": "r", "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}])]
                )
                provider = OpenAIChatCompletionsProvider(base_url)
                asked_request = replace(self.request(), reasoning_effort=asked)
                with patch("karox.providers.httpx.Client", return_value=client):
                    provider.complete(asked_request)
                payload = client.calls[0]["json"]
                self.assertEqual(payload["reasoning_effort"], expected)
                # Not the Responses-API nesting, which this endpoint rejects.
                self.assertNotIn("reasoning", payload)

    def test_reasoning_families_get_the_output_cap_they_accept(self) -> None:
        # These models reject max_tokens with a 400 rather than ignoring it, and
        # the provider's error body is never read, so the spelling is chosen from
        # the model family.
        cases = (
            ("gpt-4o", "max_tokens"),
            ("o1", "max_completion_tokens"),
            ("o3-mini", "max_completion_tokens"),
            ("o4-mini-2026-01-01", "max_completion_tokens"),
            ("gpt-5", "max_completion_tokens"),
            ("gpt-5.1-codex", "max_completion_tokens"),
            # A gateway prefix must not hide the family.
            ("openai/o3", "max_completion_tokens"),
            ("OpenAI/GPT-5", "max_completion_tokens"),
            # ...and a name that merely starts with the same letters must not be
            # mistaken for one.
            ("olmo-7b", "max_tokens"),
            ("gpt-5x-community", "max_tokens"),
        )
        for model, expected in cases:
            with self.subTest(model=model):
                client = FakeClient(
                    [sse_response([{"id": "r", "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}])]
                )
                provider = OpenAIChatCompletionsProvider("https://provider.example/v1")
                asked = replace(self.request(), model=model, max_output_tokens=512)
                with patch("karox.providers.httpx.Client", return_value=client):
                    provider.complete(asked)
                payload = client.calls[0]["json"]
                self.assertEqual(payload[expected], 512)
                other = (
                    "max_tokens"
                    if expected == "max_completion_tokens"
                    else "max_completion_tokens"
                )
                self.assertNotIn(other, payload)

    def test_chat_completions_omits_the_dial_when_none_was_asked(self) -> None:
        client = FakeClient(
            [sse_response([{"id": "r", "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}])]
        )
        provider = OpenAIChatCompletionsProvider("https://provider.example/v1")
        with patch("karox.providers.httpx.Client", return_value=client):
            provider.complete(self.request())

        self.assertNotIn("reasoning_effort", client.calls[0]["json"])

    def test_openrouter_requests_summary_without_changing_reasoning_effort(self) -> None:
        response = sse_response(
            [{"id": "r", "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]
        )
        request = replace(self.request(), reasoning_effort="high")

        openrouter_client = FakeClient([response])
        openrouter = OpenAIChatCompletionsProvider("https://openrouter.ai/api/v1")
        with patch("karox.providers.httpx.Client", return_value=openrouter_client):
            openrouter.complete(request)
        openrouter_payload = openrouter_client.calls[0]["json"]
        self.assertEqual(openrouter_payload["reasoning_effort"], "high")
        self.assertEqual(openrouter_payload["reasoning"], {"summary": "auto"})

        generic_client = FakeClient(
            [sse_response([{"id": "r", "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}])]
        )
        generic = OpenAIChatCompletionsProvider("https://provider.example/v1")
        with patch("karox.providers.httpx.Client", return_value=generic_client):
            generic.complete(request)
        self.assertEqual(generic_client.calls[0]["json"]["reasoning_effort"], "high")
        self.assertNotIn("reasoning", generic_client.calls[0]["json"])

    def test_reasoning_families_do_not_receive_temperature(self) -> None:
        """o-series/gpt-5 models reject temperature with a 400."""

        for model in ("o3-mini", "gpt-5.1", "openai/o4-mini"):
            with self.subTest(model=model):
                client = FakeClient(
                    [sse_response([{"id": "r", "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}])]
                )
                provider = OpenAIChatCompletionsProvider("https://provider.example/v1")
                asked = replace(self.request(), model=model, temperature=0.2)
                with patch("karox.providers.httpx.Client", return_value=client):
                    provider.complete(asked)
                self.assertNotIn("temperature", client.calls[0]["json"])

    def test_non_reasoning_models_still_receive_temperature(self) -> None:
        client = FakeClient(
            [sse_response([{"id": "r", "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}])]
        )
        provider = OpenAIChatCompletionsProvider("https://provider.example/v1")
        asked = replace(self.request(), model="gpt-4o", temperature=0.2)
        with patch("karox.providers.httpx.Client", return_value=client):
            provider.complete(asked)
        self.assertEqual(client.calls[0]["json"]["temperature"], 0.2)

    def test_empty_choices_keepalive_frame_is_tolerated(self) -> None:
        """Gateways emit ``choices: []`` frames without usage as keepalives."""

        client = FakeClient(
            [
                sse_response(
                    [
                        {"id": "r", "choices": []},
                        {
                            "id": "r",
                            "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
                        },
                        {"id": "r", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}},
                    ]
                )
            ]
        )
        provider = OpenAIChatCompletionsProvider("https://provider.example/v1")
        with patch("karox.providers.httpx.Client", return_value=client):
            events = list(provider.stream(self.request()))
        kinds = [event.kind for event in events]
        self.assertIn(ModelEventKind.TEXT_DELTA, kinds)
        self.assertEqual(kinds[-1], ModelEventKind.COMPLETION)

def response(records: list[tuple[str | None, object]]) -> httpx.Response:
    lines: list[str] = []
    for event_name, value in records:
        if event_name is not None:
            lines.append(f"event: {event_name}\n")
        lines.append(f"data: {json.dumps(value)}\n\n")
    request = httpx.Request("POST", "https://provider.example/v1")
    return httpx.Response(
        200,
        request=request,
        content="".join(lines).encode("utf-8"),
        headers={"Content-Type": "text/event-stream"},
    )

class AdapterEarlyReadErrorStream(httpx.SyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self.request = request

    def __iter__(self):
        yield from ()
        raise httpx.ReadError("stream broke before first event", request=self.request)

class AdapterStreamContext:
    def __init__(self, item: httpx.Response) -> None:
        self.item = item

    def __enter__(self) -> httpx.Response:
        return self.item

    def __exit__(self, *args: object) -> None:
        self.item.close()

class AdapterFakeClient:
    def __init__(self, items: list[httpx.Response]) -> None:
        self.items = items
        self.calls: list[dict[str, object]] = []

    def __enter__(self) -> "AdapterFakeClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs: object) -> AdapterStreamContext:
        self.calls.append({"method": method, "url": url, **kwargs})
        return AdapterStreamContext(self.items.pop(0))

def request() -> ModelRequest:
    return ModelRequest(
        model="test-model",
        messages=(ModelMessage("user", "work"),),
        deadline_seconds=2,
    )

class ProviderAdapterTests(unittest.TestCase):
    def complete(self, provider: object, records: list[tuple[str | None, object]]):
        client = AdapterFakeClient([response(records)])
        try:
            with patch("karox.provider_adapters.httpx.Client", return_value=client):
                result = provider.complete(request())  # type: ignore[attr-defined]
            return result, client
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    def test_provider_reuses_one_http_client_across_calls(self) -> None:
        records = [
            (None, {"type": "response.created", "response": {"id": "resp-1"}}),
            (
                None,
                {
                    "type": "response.completed",
                    "response": {"id": "resp-1", "usage": {}},
                },
            ),
        ]
        client = AdapterFakeClient([response(records), response(records)])
        provider = OpenAIResponsesProvider("https://provider.example/v1")
        try:
            with patch(
                "karox.provider_adapters.httpx.Client", return_value=client
            ) as constructor:
                first = provider.complete(request())
                second = provider.complete(request())
            self.assertEqual(constructor.call_count, 1)
            self.assertEqual(len(client.calls), 2)
            for result in (first, second):
                metrics = result.transport_metrics
                self.assertLessEqual(
                    metrics["response_headers_ms"], metrics["first_event_ms"]
                )
                self.assertLessEqual(metrics["first_event_ms"], metrics["total_ms"])
        finally:
            provider.close()

    def test_retries_read_error_after_headers_before_first_event(self) -> None:
        request_obj = httpx.Request("POST", "https://provider.example/v1/responses")
        interrupted = httpx.Response(
            200,
            request=request_obj,
            headers={"Content-Type": "text/event-stream"},
            stream=AdapterEarlyReadErrorStream(request_obj),
        )
        records = [
            (None, {"type": "response.created", "response": {"id": "resp-1"}}),
            (
                None,
                {
                    "type": "response.completed",
                    "response": {"id": "resp-1", "usage": {}},
                },
            ),
        ]
        client = AdapterFakeClient([interrupted, response(records)])
        provider = OpenAIResponsesProvider(
            "https://provider.example/v1",
            max_transport_retries=1,
            retry_backoff_seconds=0,
        )
        try:
            with patch("karox.provider_adapters.httpx.Client", return_value=client):
                result = provider.complete(request())
            self.assertEqual(result.response_id, "resp-1")
            self.assertEqual(result.transport_attempts, 2)
            self.assertEqual(len(client.calls), 2)
        finally:
            provider.close()

    def test_openai_responses_accepts_lifecycle_completion_events(self) -> None:
        records = [
            (None, {"type": "response.created", "response": {"id": "resp-1"}}),
            (None, {"type": "response.in_progress", "response": {"id": "resp-1"}}),
            (
                None,
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "id": "item-1",
                        "call_id": "call-1",
                        "name": "repo_read_file",
                    },
                },
            ),
            (None, {"type": "response.function_call_arguments.delta", "item_id": "item-1", "delta": "{}"}),
            (None, {"type": "response.function_call_arguments.done", "item_id": "item-1", "arguments": "{}"}),
            (None, {"type": "response.output_item.done", "output_index": 0}),
            (None, {"type": "response.content_part.added", "output_index": 1, "content_index": 0}),
            (None, {"type": "response.output_text.delta", "delta": "done"}),
            (None, {"type": "response.output_text.done", "text": "done"}),
            (None, {"type": "response.content_part.done", "output_index": 1, "content_index": 0}),
            (
                None,
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp-1",
                        "usage": {
                            "input_tokens": 3,
                            "output_tokens": 2,
                            "total_tokens": 5,
                        },
                    },
                },
            ),
        ]

        result, client = self.complete(
            OpenAIResponsesProvider("https://provider.example/v1"), records
        )

        self.assertEqual(result.content, "done")
        self.assertEqual(result.response_id, "resp-1")
        self.assertEqual(
            result.usage,
            {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            },
        )
        self.assertEqual(
            result.tool_calls,
            (ToolCall("call-1", "repo_read_file", "{}"),),
        )
        self.assertEqual(client.calls[0]["url"], "https://provider.example/v1/responses")

    def test_openai_responses_rejects_duplicate_call_identity_and_indexes(self) -> None:
        def added(index: int, item_id: str, call_id: str) -> tuple[None, object]:
            return (
                None,
                {
                    "type": "response.output_item.added",
                    "output_index": index,
                    "item": {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": call_id,
                        "name": "repo_read_file",
                    },
                },
            )

        malformed = (
            [added(0, "item-1", "call-1"), added(0, "item-2", "call-2")],
            [added(0, "item-1", "call-1"), added(1, "item-1", "call-2")],
            [added(0, "item-1", "call-1"), added(1, "item-2", "call-1")],
            [added(0, "item-1", "call-1"), added(1, "call-1", "call-2")],
            [
                added(0, "item-1", "call-1"),
                added(1, "item-2", "call-2"),
                (
                    None,
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": "item-1",
                        "call_id": "call-2",
                        "delta": "{}",
                    },
                ),
            ],
        )
        for records in malformed:
            with self.subTest(records=records), self.assertRaises(ProviderError) as raised:
                self.complete(
                    OpenAIResponsesProvider("https://provider.example/v1"), records
                )
            self.assertEqual(raised.exception.kind, ProviderErrorKind.MALFORMED_RESPONSE)

    def test_anthropic_tracks_content_block_types_and_indexes(self) -> None:
        valid = [
            ("message_start", {"message": {"id": "msg-1", "usage": {"input_tokens": 4}}}),
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "hello"}}),
            ("content_block_stop", {"index": 0}),
            ("content_block_start", {"index": 1, "content_block": {"type": "tool_use", "id": "call-1", "name": "git_status", "input": {}}}),
            ("content_block_delta", {"index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}}),
            ("content_block_stop", {"index": 1}),
            ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 2}}),
            ("message_stop", {}),
        ]

        result, _ = self.complete(
            AnthropicMessagesProvider("https://provider.example/v1"), valid
        )

        self.assertEqual(result.content, "hello")
        self.assertEqual(result.usage, {"prompt_tokens": 4, "completion_tokens": 2})
        self.assertEqual(result.tool_calls, (ToolCall("call-1", "git_status", "{}"),))

    def test_anthropic_rejects_duplicate_unknown_and_mismatched_blocks(self) -> None:
        message_start = (
            "message_start",
            {"message": {"id": "msg-1", "usage": {}}},
        )
        start_text = (
            "content_block_start",
            {"index": 0, "content_block": {"type": "text", "text": ""}},
        )
        malformed: tuple[list[tuple[str | None, object]], ...] = (
            [message_start, start_text, start_text],
            [message_start, ("content_block_delta", {"index": 7, "delta": {"type": "text_delta", "text": "bad"}})],
            [message_start, start_text, ("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}})],
            [message_start, ("content_block_stop", {"index": 7})],
            [message_start, start_text, ("message_stop", {})],
        )
        for records in malformed:
            with self.subTest(records=records), self.assertRaises(ProviderError) as raised:
                self.complete(
                    AnthropicMessagesProvider("https://provider.example/v1"), records
                )
            self.assertEqual(raised.exception.kind, ProviderErrorKind.MALFORMED_RESPONSE)

    def test_anthropic_streams_thinking_without_ending_the_turn(self) -> None:
        records = [
            ("message_start", {"message": {"id": "msg-1", "usage": {"input_tokens": 4}}}),
            (
                "content_block_start",
                {"index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            ),
            (
                "content_block_delta",
                {"index": 0, "delta": {"type": "thinking_delta", "thinking": "weighing options"}},
            ),
            (
                "content_block_delta",
                {"index": 0, "delta": {"type": "signature_delta", "signature": "c2ln"}},
            ),
            ("content_block_stop", {"index": 0}),
            (
                "content_block_start",
                {"index": 1, "content_block": {"type": "redacted_thinking", "data": "opaque"}},
            ),
            ("content_block_stop", {"index": 1}),
            (
                "content_block_start",
                {"index": 2, "content_block": {"type": "text", "text": ""}},
            ),
            ("content_block_delta", {"index": 2, "delta": {"type": "text_delta", "text": "the answer"}}),
            ("content_block_stop", {"index": 2}),
            ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 9}}),
            ("message_stop", {}),
        ]

        result, _ = self.complete(
            AnthropicMessagesProvider("https://provider.example/v1"), records
        )

        # Thinking is on by default on the current flagship model, so refusing
        # these blocks made that model unusable rather than safe.
        self.assertEqual(result.content, "the answer")
        self.assertEqual(result.reasoning, "weighing options")
        self.assertEqual(result.finish_reason, "end_turn")

    def test_anthropic_ignores_block_and_event_types_it_does_not_model(self) -> None:
        records = [
            ("message_start", {"message": {"id": "msg-1", "usage": {}}}),
            ("some_future_event", {"whatever": True}),
            (
                "content_block_start",
                {"index": 0, "content_block": {"type": "future_block", "payload": 1}},
            ),
            (
                "content_block_delta",
                {"index": 0, "delta": {"type": "future_delta", "payload": 2}},
            ),
            ("content_block_stop", {"index": 0}),
            (
                "content_block_start",
                {"index": 1, "content_block": {"type": "text", "text": "hi"}},
            ),
            ("content_block_stop", {"index": 1}),
            ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {}}),
            ("message_stop", {}),
        ]

        result, _ = self.complete(
            AnthropicMessagesProvider("https://provider.example/v1"), records
        )

        # Failing closed on content is right; failing closed on protocol
        # evolution just breaks working turns.
        self.assertEqual(result.content, "hi")
        self.assertIsNone(result.reasoning)

    def test_anthropic_sends_the_requested_output_ceiling(self) -> None:
        records = [
            ("message_start", {"message": {"id": "msg-1", "usage": {}}}),
            ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {}}),
            ("message_stop", {}),
        ]
        client = AdapterFakeClient([response(records)])
        requested = ModelRequest(
            model="test-model",
            messages=(ModelMessage("user", "work"),),
            deadline_seconds=2,
            max_output_tokens=64_000,
        )

        with patch("karox.provider_adapters.httpx.Client", return_value=client):
            AnthropicMessagesProvider("https://provider.example/v1").complete(requested)

        self.assertEqual(client.calls[0]["json"]["max_tokens"], 64_000)

    def test_anthropic_default_ceiling_is_not_four_thousand_tokens(self) -> None:
        records = [
            ("message_start", {"message": {"id": "msg-1", "usage": {}}}),
            ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {}}),
            ("message_stop", {}),
        ]
        client = AdapterFakeClient([response(records)])

        with patch("karox.provider_adapters.httpx.Client", return_value=client):
            AnthropicMessagesProvider("https://provider.example/v1").complete(request())

        self.assertGreater(client.calls[0]["json"]["max_tokens"], 4_096)

    @staticmethod
    def _turn(index: int) -> tuple[ModelMessage, ModelMessage]:
        return (
            ModelMessage(
                "assistant",
                None,
                tool_calls=(ToolCall(f"call-{index}", "repo_read_file", "{}"),),
            ),
            ModelMessage("tool", f"result {index}", tool_call_id=f"call-{index}"),
        )

    def _anthropic_payload(self, requested: ModelRequest) -> dict:
        records = [
            ("message_start", {"message": {"id": "msg-1", "usage": {}}}),
            ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {}}),
            ("message_stop", {}),
        ]
        client = AdapterFakeClient([response(records)])
        with patch("karox.provider_adapters.httpx.Client", return_value=client):
            AnthropicMessagesProvider("https://provider.example/v1").complete(requested)
        return client.calls[0]["json"]

    def test_anthropic_caches_the_static_prefix_and_the_newest_turn(self) -> None:
        messages = [ModelMessage("system", "rules"), ModelMessage("user", "work")]
        for index in range(3):
            messages.extend(self._turn(index))

        payload = self._anthropic_payload(
            ModelRequest(
                model="test-model",
                messages=tuple(messages),
                deadline_seconds=2,
                cache_key="session-a",
            )
        )

        # Tools render before the system prompt, so one breakpoint there covers
        # the entire static part of every request in the session.
        self.assertEqual(
            payload["system"],
            [{"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}],
        )
        blocks = [block for item in payload["messages"] for block in item["content"]]
        marked = [
            index for index, block in enumerate(blocks) if "cache_control" in block
        ]
        # The newest turn is cached so the next request can read it back.
        self.assertEqual(marked, [len(blocks) - 1])

    def test_anthropic_cached_prefix_is_byte_stable_as_the_turn_grows(self) -> None:
        # Caching is a prefix match and fails silently: one moved breakpoint and
        # every request pays full price with no error anywhere. The breakpoints
        # in the shared prefix must therefore land on the same blocks in both
        # requests, and must stay closer together than the twenty-block window
        # the API looks back through.
        base = [ModelMessage("system", "rules"), ModelMessage("user", "work")]
        for index in range(20):
            base.extend(self._turn(index))
        grown = list(base)
        grown.extend(self._turn(20))

        first = self._anthropic_payload(
            ModelRequest(
                model="test-model",
                messages=tuple(base),
                deadline_seconds=2,
                cache_key="session-a",
            )
        )
        second = self._anthropic_payload(
            ModelRequest(
                model="test-model",
                messages=tuple(grown),
                deadline_seconds=2,
                cache_key="session-a",
            )
        )

        def marked(payload: dict) -> list[int]:
            blocks = [item for message in payload["messages"] for item in message["content"]]
            return [index for index, block in enumerate(blocks) if "cache_control" in block]

        shared = set(marked(first)) & set(marked(second))
        self.assertTrue(shared, "the two requests share no cached breakpoint")
        newest = marked(second)
        self.assertLessEqual(
            min(newest[-1] - item for item in newest[:-1] if item < newest[-1]),
            20,
            "the newest breakpoint cannot see the previous one",
        )
        # The prefix bytes themselves must be identical up to the shared point.
        limit = max(shared)
        first_blocks = [item for message in first["messages"] for item in message["content"]]
        second_blocks = [item for message in second["messages"] for item in message["content"]]
        self.assertEqual(first_blocks[: limit + 1], second_blocks[: limit + 1])

    def test_anthropic_without_a_cache_key_sends_no_breakpoints(self) -> None:
        payload = self._anthropic_payload(
            ModelRequest(
                model="test-model",
                messages=(ModelMessage("system", "rules"), ModelMessage("user", "work")),
                deadline_seconds=2,
            )
        )

        # A one-shot caller would pay the cache-write premium and never read it
        # back, so caching is asked for rather than assumed.
        self.assertEqual(payload["system"], "rules")
        self.assertNotIn(
            "cache_control",
            json.dumps(payload["messages"]),
        )

    def test_anthropic_reports_what_the_cache_saved(self) -> None:
        records = [
            (
                "message_start",
                {
                    "message": {
                        "id": "msg-1",
                        "usage": {
                            "input_tokens": 12,
                            "cache_read_input_tokens": 4_000,
                            "cache_creation_input_tokens": 30,
                        },
                    }
                },
            ),
            ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {}}),
            ("message_stop", {}),
        ]
        client = AdapterFakeClient([response(records)])

        with patch("karox.provider_adapters.httpx.Client", return_value=client):
            result = AnthropicMessagesProvider(
                "https://provider.example/v1"
            ).complete(request())

        # Anthropic's input_tokens counts only the uncached remainder, so the
        # reported prompt used to be 12 tokens when 4,042 were really sent.
        # prompt_tokens now means the whole prompt on every provider, with the
        # cached parts named as the subset they are.
        self.assertEqual(result.usage["prompt_tokens"], 4_042)
        self.assertEqual(result.usage["cache_read_tokens"], 4_000)
        self.assertEqual(result.usage["cache_write_tokens"], 30)

    def test_gemini_keeps_private_thoughts_out_of_the_answer(self) -> None:
        records = [
            (
                None,
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "let me consider", "thought": True},
                                    {"text": "the answer"},
                                ]
                            },
                            "finishReason": "STOP",
                        }
                    ]
                },
            ),
        ]

        result, _ = self.complete(
            GeminiGenerateContentProvider("https://provider.example/v1"), records
        )

        self.assertEqual(result.content, "the answer")
        self.assertEqual(result.reasoning, "let me consider")

    def test_anthropic_requires_one_message_start_before_stream_content(self) -> None:
        message_start = (
            "message_start",
            {"message": {"id": "msg-1", "usage": {}}},
        )
        malformed: tuple[list[tuple[str | None, object]], ...] = (
            [("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})],
            [("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "bad"}})],
            [("content_block_stop", {"index": 0})],
            [("message_delta", {"delta": {}, "usage": {}})],
            [("message_stop", {})],
            [message_start, message_start],
        )
        for records in malformed:
            with self.subTest(records=records), self.assertRaises(ProviderError) as raised:
                self.complete(
                    AnthropicMessagesProvider("https://provider.example/v1"), records
                )
            self.assertEqual(raised.exception.kind, ProviderErrorKind.MALFORMED_RESPONSE)

    def test_gemini_assigns_global_tool_indexes_and_stable_generated_ids(self) -> None:
        records = [
            (
                None,
                {
                    "responseId": "gemini-response",
                    "candidates": [
                        {"content": {"parts": [{"functionCall": {"name": "first", "args": {"a": 1}}}]}}
                    ],
                },
            ),
            (
                None,
                {
                    "responseId": "gemini-response",
                    "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3},
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "done"},
                                    {"functionCall": {"name": "second", "args": {}}},
                                ]
                            },
                            "finishReason": "STOP",
                        }
                    ],
                },
            ),
        ]

        result, client = self.complete(
            GeminiGenerateContentProvider("https://provider.example/v1beta"), records
        )

        self.assertEqual(result.content, "done")
        self.assertEqual([item.name for item in result.tool_calls], ["first", "second"])
        self.assertEqual([item.raw_arguments for item in result.tool_calls], ['{"a":1}', "{}"])
        self.assertNotEqual(result.tool_calls[0].call_id, result.tool_calls[1].call_id)
        self.assertTrue(result.tool_calls[0].call_id.endswith("-0"))
        self.assertTrue(result.tool_calls[1].call_id.endswith("-1"))
        self.assertIn("/models/test-model:streamGenerateContent?alt=sse", client.calls[0]["url"])

    def test_shared_sse_reader_enforces_resource_bounds(self) -> None:
        cases = (
            (
                "MAX_SSE_LINE_CHARS",
                7,
                [(None, {})],
                "line exceeds",
            ),
            (
                "MAX_SSE_EVENTS",
                1,
                [(None, {}), (None, {})],
                "too many events",
            ),
            (
                "MAX_SSE_TOTAL_CHARS",
                20,
                [(None, {}), (None, {}), (None, {})],
                "stream exceeds",
            ),
        )
        for attribute, limit, records, message in cases:
            with self.subTest(attribute=attribute):
                item = response(records)
                with (
                    patch.object(OpenAIResponsesProvider, attribute, limit),
                    self.assertRaisesRegex(ProviderError, message) as raised,
                ):
                    list(
                        OpenAIResponsesProvider._sse_records(
                            item,
                            float("inf"),
                        )
                    )
                self.assertEqual(
                    raised.exception.kind,
                    ProviderErrorKind.MALFORMED_RESPONSE,
                )

    def test_rejects_invalid_known_usage_values_for_every_adapter(self) -> None:
        invalid_values = (True, -1, 1.5, "1")
        cases = (
            (
                OpenAIResponsesProvider("https://provider.example/v1"),
                lambda value: [
                    (
                        None,
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp-1",
                                "usage": {"input_tokens": value},
                            },
                        },
                    )
                ],
            ),
            (
                AnthropicMessagesProvider("https://provider.example/v1"),
                lambda value: [
                    (
                        "message_start",
                        {
                            "message": {
                                "id": "msg-1",
                                "usage": {"input_tokens": value},
                            }
                        },
                    )
                ],
            ),
            (
                GeminiGenerateContentProvider("https://provider.example/v1beta"),
                lambda value: [
                    (
                        None,
                        {"usageMetadata": {"promptTokenCount": value}},
                    )
                ],
            ),
        )
        for provider, records_for in cases:
            for value in invalid_values:
                with (
                    self.subTest(
                        provider=provider.provider_name,
                        value=value,
                    ),
                    self.assertRaises(ProviderError) as raised,
                ):
                    self.complete(provider, records_for(value))
                self.assertEqual(
                    raised.exception.kind,
                    ProviderErrorKind.MALFORMED_RESPONSE,
                )

    def test_gemini_invalid_persisted_tool_json_has_safe_error(self) -> None:
        secret = "sk-secret-persisted-value"
        provider = GeminiGenerateContentProvider(
            "https://provider.example/v1beta"
        )
        invalid_request = ModelRequest(
            model="test-model",
            messages=(
                ModelMessage(
                    "assistant",
                    tool_calls=(
                        ToolCall("call-1", "repo_read_file", f'{{"token":"{secret}"'),
                    ),
                ),
            ),
        )

        with self.assertRaises(ProviderError) as raised:
            provider.complete(invalid_request)

        self.assertEqual(raised.exception.kind, ProviderErrorKind.INVALID_REQUEST)
        self.assertEqual(
            raised.exception.safe_message,
            "Gemini request contains invalid persisted tool data",
        )
        self.assertNotIn(secret, str(raised.exception))

    def _payload(self, provider: object, records: list[tuple[str | None, object]], **fields):
        client = AdapterFakeClient([response(records)])
        asked = ModelRequest(
            model="test-model",
            messages=(ModelMessage("user", "work"),),
            deadline_seconds=2,
            **fields,
        )
        with patch("karox.provider_adapters.httpx.Client", return_value=client):
            provider.complete(asked)  # type: ignore[attr-defined]
        return client.calls[0]["json"]

    _ANTHROPIC_DONE = [
        ("message_start", {"message": {"id": "msg-1", "usage": {}}}),
        ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {}}),
        ("message_stop", {}),
    ]
    _GEMINI_DONE = [
        (None, {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}),
    ]
    _RESPONSES_DONE = [
        (None, {"type": "response.created", "response": {"id": "resp-1"}}),
        (None, {"type": "response.completed", "response": {"id": "resp-1", "usage": {}}}),
    ]

    def test_each_wire_carries_the_requested_effort_where_it_belongs(self) -> None:
        # One dial in KaroX, four different spellings on the wire. Each provider
        # rejects the others' field, so the placement is the whole feature.
        anthropic = self._payload(
            AnthropicMessagesProvider("https://provider.example/v1"),
            self._ANTHROPIC_DONE,
            reasoning_effort="high",
        )
        self.assertEqual(anthropic["output_config"], {"effort": "high"})
        self.assertNotIn("effort", anthropic)

        responses = self._payload(
            OpenAIResponsesProvider("https://provider.example/v1"),
            self._RESPONSES_DONE,
            reasoning_effort="medium",
        )
        self.assertEqual(responses["reasoning"], {"effort": "medium"})

        gemini = self._payload(
            GeminiGenerateContentProvider("https://provider.example/v1beta"),
            self._GEMINI_DONE,
            reasoning_effort="low",
        )
        self.assertEqual(
            gemini["generationConfig"]["thinkingConfig"], {"thinkingLevel": "low"}
        )

    def test_a_level_a_provider_lacks_is_clamped_not_sent_verbatim(self) -> None:
        # Anthropic accepts all five; the other two top out at "high". Sending
        # "max" there would be rejected outright, and dropping the dial would
        # quietly give the caller the default when they asked for the ceiling.
        cases = (
            ("xhigh", "high"),
            ("max", "high"),
            ("medium", "medium"),
        )
        for asked, expected in cases:
            with self.subTest(effort=asked):
                anthropic = self._payload(
                    AnthropicMessagesProvider("https://provider.example/v1"),
                    self._ANTHROPIC_DONE,
                    reasoning_effort=asked,
                )
                self.assertEqual(anthropic["output_config"], {"effort": asked})
                responses = self._payload(
                    OpenAIResponsesProvider("https://provider.example/v1"),
                    self._RESPONSES_DONE,
                    reasoning_effort=asked,
                )
                self.assertEqual(responses["reasoning"], {"effort": expected})
                gemini = self._payload(
                    GeminiGenerateContentProvider("https://provider.example/v1beta"),
                    self._GEMINI_DONE,
                    reasoning_effort=asked,
                )
                self.assertEqual(
                    gemini["generationConfig"]["thinkingConfig"],
                    {"thinkingLevel": expected},
                )

    def test_no_effort_asked_means_no_field_sent(self) -> None:
        # The provider's own default must stay in force when nothing is asked.
        anthropic = self._payload(
            AnthropicMessagesProvider("https://provider.example/v1"),
            self._ANTHROPIC_DONE,
        )
        self.assertNotIn("output_config", anthropic)
        responses = self._payload(
            OpenAIResponsesProvider("https://provider.example/v1"),
            self._RESPONSES_DONE,
        )
        self.assertNotIn("reasoning", responses)
        gemini = self._payload(
            GeminiGenerateContentProvider("https://provider.example/v1beta"),
            self._GEMINI_DONE,
        )
        self.assertNotIn("thinkingConfig", gemini.get("generationConfig", {}))

    def test_a_signed_thinking_block_survives_the_stream_whole(self) -> None:
        # The signature is what the API verifies when the block comes back, so it
        # has to arrive byte-exact and attached to its own block -- not folded
        # into the reasoning string shown on screen.
        records = [
            ("message_start", {"message": {"id": "msg-1", "usage": {}}}),
            (
                "content_block_start",
                {"index": 0, "content_block": {"type": "thinking", "thinking": "wei"}},
            ),
            (
                "content_block_delta",
                {"index": 0, "delta": {"type": "thinking_delta", "thinking": "ghing"}},
            ),
            (
                "content_block_delta",
                {"index": 0, "delta": {"type": "signature_delta", "signature": "Ab-"}},
            ),
            (
                "content_block_delta",
                {"index": 0, "delta": {"type": "signature_delta", "signature": "sk-9=="}},
            ),
            ("content_block_stop", {"index": 0}),
            (
                "content_block_start",
                {
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "git_status",
                        "input": {},
                    },
                },
            ),
            ("content_block_stop", {"index": 1}),
            ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {}}),
            ("message_stop", {}),
        ]

        result, _ = self.complete(
            AnthropicMessagesProvider("https://provider.example/v1"), records
        )

        self.assertEqual(len(result.reasoning_blocks), 1)
        block = result.reasoning_blocks[0]
        self.assertEqual(block.kind, "thinking")
        self.assertEqual(block.text, "weighing")
        # Assembled from both fragments in order, and untouched: this value looks
        # secret-shaped to the display redactor, which is why it never goes
        # through it.
        self.assertEqual(block.signature, "Ab-sk-9==")
        self.assertTrue(block.replayable)
        # Still kept off the answer, and still on the reasoning channel.
        self.assertIsNone(result.content)
        self.assertEqual(result.reasoning, "weighing")
        self.assertEqual([call.call_id for call in result.tool_calls], ["call-1"])

    def test_a_redacted_thinking_block_keeps_its_opaque_payload(self) -> None:
        records = [
            ("message_start", {"message": {"id": "msg-1", "usage": {}}}),
            (
                "content_block_start",
                {
                    "index": 0,
                    "content_block": {
                        "type": "redacted_thinking",
                        "data": "EncRypTedPayl0ad",
                        "signature": "sig-1",
                    },
                },
            ),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {}}),
            ("message_stop", {}),
        ]

        result, _ = self.complete(
            AnthropicMessagesProvider("https://provider.example/v1"), records
        )

        self.assertEqual(len(result.reasoning_blocks), 1)
        block = result.reasoning_blocks[0]
        self.assertEqual(block.kind, "redacted_thinking")
        self.assertEqual(block.data, "EncRypTedPayl0ad")
        self.assertEqual(block.text, "")
        # There is nothing readable to leak, and nothing to show either.
        self.assertIsNone(result.reasoning)

    def test_thinking_goes_back_ahead_of_the_tool_call_it_preceded(self) -> None:
        # The API verifies each signature against the block it signs and refuses a
        # turn whose thinking was edited or dropped, so this ordering is the
        # difference between a working tool turn and a rejected one.
        payload = self._anthropic_payload(
            ModelRequest(
                model="test-model",
                messages=(
                    ModelMessage("user", "work"),
                    ModelMessage(
                        "assistant",
                        "on it",
                        tool_calls=(ToolCall("call-1", "git_status", "{}"),),
                        reasoning_blocks=(
                            ReasoningBlock("thinking", text="first", signature="s1"),
                            ReasoningBlock(
                                "redacted_thinking", data="opaque", signature="s2"
                            ),
                        ),
                    ),
                    ModelMessage("tool", "clean", tool_call_id="call-1"),
                ),
                deadline_seconds=2,
            )
        )

        assistant = payload["messages"][1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(
            [block["type"] for block in assistant["content"]],
            ["thinking", "redacted_thinking", "text", "tool_use"],
        )
        self.assertEqual(assistant["content"][0]["thinking"], "first")
        self.assertEqual(assistant["content"][0]["signature"], "s1")
        # A redacted block carries only its opaque payload back.
        self.assertEqual(assistant["content"][1], {"type": "redacted_thinking", "data": "opaque"})

    def test_a_cache_breakpoint_never_lands_on_a_thinking_block(self) -> None:
        # Anthropic does not accept cache_control on a thinking block, so a
        # breakpoint that landed there would make the whole request invalid.
        messages = [ModelMessage("system", "rules"), ModelMessage("user", "work")]
        for index in range(6):
            messages.append(
                ModelMessage(
                    "assistant",
                    None,
                    tool_calls=(ToolCall(f"call-{index}", "git_status", "{}"),),
                    reasoning_blocks=(
                        ReasoningBlock("thinking", text=f"step {index}", signature="s"),
                    ),
                )
            )
            messages.append(ModelMessage("tool", f"result {index}", tool_call_id=f"call-{index}"))

        payload = self._anthropic_payload(
            ModelRequest(
                model="test-model",
                messages=tuple(messages),
                deadline_seconds=2,
                cache_key="session-a",
            )
        )

        marked = [
            block
            for item in payload["messages"]
            for block in item["content"]
            if "cache_control" in block
        ]
        self.assertTrue(marked)
        self.assertEqual(
            [block["type"] for block in marked if block["type"] == "thinking"], []
        )

        # The case that actually exercises the slide-back: the newest turn always
        # gets a breakpoint, and here the newest turn's last block is thinking.
        payload = self._anthropic_payload(
            ModelRequest(
                model="test-model",
                messages=(
                    ModelMessage("user", "work"),
                    ModelMessage(
                        "assistant",
                        None,
                        reasoning_blocks=(
                            ReasoningBlock("thinking", text="still weighing", signature="s"),
                        ),
                    ),
                ),
                deadline_seconds=2,
                cache_key="session-a",
            )
        )

        blocks = [
            block for item in payload["messages"] for block in item["content"]
        ]
        self.assertEqual([block["type"] for block in blocks], ["text", "thinking"])
        self.assertNotIn("cache_control", blocks[1])
        self.assertIn("cache_control", blocks[0])

    def test_a_block_that_no_longer_matches_its_signature_is_left_out(self) -> None:
        # Sending an edited block would be rejected outright, so the turn goes
        # without it rather than failing.
        payload = self._anthropic_payload(
            ModelRequest(
                model="test-model",
                messages=(
                    ModelMessage("user", "work"),
                    ModelMessage(
                        "assistant",
                        "on it",
                        reasoning_blocks=(
                            ReasoningBlock(
                                "thinking",
                                text="[REDACTED]",
                                signature="s1",
                                replayable=False,
                            ),
                            # A block with no signature cannot be authenticated
                            # either, whatever its text says.
                            ReasoningBlock("thinking", text="unsigned"),
                        ),
                    ),
                ),
                deadline_seconds=2,
            )
        )

        assistant = payload["messages"][1]
        self.assertEqual(
            [block["type"] for block in assistant["content"]], ["text"]
        )

    def test_an_effort_level_that_does_not_exist_is_refused_at_construction(self) -> None:
        with self.assertRaisesRegex(ValueError, "reasoning effort"):
            ModelRequest(
                model="test-model",
                messages=(ModelMessage("user", "work"),),
                reasoning_effort="maximum",
            )

class StreamedErrorKindTests(unittest.TestCase):
    """A streamed error must not tell a different story than a status code.

    ``tests/test_providers.py`` already pins the status path's ``402 ->
    BUDGET_EXCEEDED``. The same condition arriving inside a 200 SSE body has to
    land on the same kind, or routing retries and falls back differently for the
    same upstream state depending on how the provider chose to report it.
    """

    def test_an_explicit_402_is_a_budget_error(self) -> None:
        self.assertIs(
            stream_error_kind({"code": 402, "message": "quota_exceeded"}),
            ProviderErrorKind.BUDGET_EXCEEDED,
        )

    def test_a_credit_hint_without_a_code_stays_a_permission_problem(self) -> None:
        self.assertIs(
            stream_error_kind(
                {"type": "billing_error", "message": "insufficient credit"}
            ),
            ProviderErrorKind.PERMISSION,
        )
