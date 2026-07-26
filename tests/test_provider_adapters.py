from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import httpx

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox.provider_adapters import (
    AnthropicMessagesProvider,
    GeminiGenerateContentProvider,
    OpenAIResponsesProvider,
)
from karox.providers import (
    ModelMessage,
    ModelRequest,
    ProviderError,
    ProviderErrorKind,
    ToolCall,
)


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


class StreamContext:
    def __init__(self, item: httpx.Response) -> None:
        self.item = item

    def __enter__(self) -> httpx.Response:
        return self.item

    def __exit__(self, *args: object) -> None:
        self.item.close()


class FakeClient:
    def __init__(self, items: list[httpx.Response]) -> None:
        self.items = items
        self.calls: list[dict[str, object]] = []

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs: object) -> StreamContext:
        self.calls.append({"method": method, "url": url, **kwargs})
        return StreamContext(self.items.pop(0))


def request() -> ModelRequest:
    return ModelRequest(
        model="test-model",
        messages=(ModelMessage("user", "work"),),
        deadline_seconds=2,
    )


class ProviderAdapterTests(unittest.TestCase):
    def complete(self, provider: object, records: list[tuple[str | None, object]]):
        client = FakeClient([response(records)])
        with patch("karox.provider_adapters.httpx.Client", return_value=client):
            result = provider.complete(request())  # type: ignore[attr-defined]
        return result, client

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
        client = FakeClient([response(records)])
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
        client = FakeClient([response(records)])

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
        client = FakeClient([response(records)])
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
        client = FakeClient([response(records)])

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


if __name__ == "__main__":
    unittest.main()
