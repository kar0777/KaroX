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

from _support import SRC  # noqa: F401
from karox.providers import (
    ModelEventKind,
    ModelMessage,
    ModelRequest,
    OpenAIChatCompletionsProvider,
    ProviderError,
    ProviderErrorKind,
    ProviderTool,
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
                client = FakeClient(
                    [
                        error_response(
                            status,
                            json.dumps({"error": secret}),
                            **{"Retry-After": "2.5"},
                        )
                    ]
                )
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

    def test_chat_completions_spells_the_effort_dial_flat_and_clamps_it(self) -> None:
        cases = (("low", "low"), ("high", "high"), ("xhigh", "high"), ("max", "high"))
        for asked, expected in cases:
            with self.subTest(effort=asked):
                client = FakeClient(
                    [sse_response([{"id": "r", "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}])]
                )
                provider = OpenAIChatCompletionsProvider("https://provider.example/v1")
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


if __name__ == "__main__":
    unittest.main()
