"""Anthropic Messages streaming adapter."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, Optional
import httpx
from .providers import (
    ModelEvent,
    ModelEventKind,
    ModelRequest,
    ProviderError,
    ProviderErrorKind,
    ReasoningBlock,
    ToolCallDelta,
)

from .provider_adapter_base import (
    _StreamingAdapter,
    _is_gemini_signature,
    stream_error_kind,
)


_ANTHROPIC_REASONING_BLOCKS = frozenset({"thinking", "redacted_thinking"})

_ANTHROPIC_DEFAULT_MAX_TOKENS = 32_000

class AnthropicMessagesProvider(_StreamingAdapter):
    provider_name = "anthropic_messages"

    def _endpoint(self, request: ModelRequest) -> str:
        return self._url("/messages")

    def _request_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            **self._headers,
        }
        secret = self._secret()
        if secret is not None:
            headers["x-api-key"] = secret
        return headers

    @staticmethod
    def _messages(request: ModelRequest) -> tuple[str, list[Dict[str, Any]]]:
        system: list[str] = []
        messages: list[Dict[str, Any]] = []
        for message in request.messages:
            if message.role == "system":
                if message.content:
                    system.append(message.content)
                continue
            if message.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content or "",
                }
                if messages and messages[-1]["role"] == "user" and isinstance(
                    messages[-1]["content"], list
                ):
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
                continue
            role = "assistant" if message.role == "assistant" else "user"
            blocks: list[Dict[str, Any]] = []
            # Thinking comes first, in the order the model produced it, and comes
            # back with the tool calls it preceded: the API verifies each
            # signature and refuses a turn whose thinking was edited or dropped.
            # A block whose stored text no longer matches what was signed is left
            # out rather than sent and rejected.
            for reasoning in message.reasoning_blocks:
                if not reasoning.replayable or not reasoning.signature:
                    continue
                if _is_gemini_signature(reasoning):
                    continue
                if reasoning.kind == "redacted_thinking":
                    blocks.append(
                        {"type": "redacted_thinking", "data": reasoning.data}
                    )
                else:
                    blocks.append(
                        {
                            "type": "thinking",
                            "thinking": reasoning.text,
                            "signature": reasoning.signature,
                        }
                    )
            if message.content is not None:
                blocks.append({"type": "text", "text": message.content})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call.call_id,
                    "name": call.name,
                    "input": json.loads(call.raw_arguments),
                }
                for call in message.tool_calls
            )
            messages.append({"role": role, "content": blocks})
        return "\n\n".join(system), messages

    @staticmethod
    def _cache_breakpoints(messages: list[Dict[str, Any]]) -> None:
        """Mark the blocks Anthropic should cache the prompt up to.

        Caching is a prefix match, so a breakpoint is only useful where the
        bytes before it will be identical on the next request. Two rules follow.

        Positions are placed on a fixed grid rather than measured back from the
        end, so a breakpoint written on one turn sits at the same offset on the
        next and can actually be read. And the gap between consecutive
        breakpoints stays under the twenty-block window the API looks back
        through, or a long turn would step over every entry the previous turn
        wrote and silently miss.

        The last block always gets one so the newest turn is cached for the turn
        after it. Anthropic allows four breakpoints per request and the system
        block holds one, leaving three here.

        Spacing is measured over every block, because the lookback window counts
        every block -- but a breakpoint is only ever written onto one that accepts
        it. A thinking block does not, so a position that lands on one slides back
        to the nearest block that does rather than making the whole request
        invalid.
        """
        blocks = [
            block
            for message in messages
            for block in message["content"]
            if isinstance(block, dict)
        ]
        if not blocks:
            return

        def cacheable_at_or_before(position: int) -> int:
            while position >= 0 and blocks[position].get("type") in _ANTHROPIC_REASONING_BLOCKS:
                position -= 1
            return position

        stride = 15
        grid = [index for index in range(stride, len(blocks) - 1, stride)]
        chosen = {*grid[-2:], len(blocks) - 1}
        resolved = {
            position
            for position in (cacheable_at_or_before(index) for index in chosen)
            if position >= 0
        }
        for index in sorted(resolved):
            blocks[index]["cache_control"] = {"type": "ephemeral"}

    def _request_payload(self, request: ModelRequest) -> Dict[str, Any]:
        try:
            system, messages = self._messages(request)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_REQUEST,
                "persisted tool arguments are not valid JSON",
            ) from exc
        if request.cache_key is not None:
            self._cache_breakpoints(messages)
        payload: Dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            # Anthropic requires max_tokens, so a default is unavoidable. 4096
            # was far below what current models allow and silently truncated a
            # long refactor; the routed model's registered ceiling is used when
            # it is known, and this is only the floor for an unregistered model.
            "max_tokens": request.max_output_tokens or _ANTHROPIC_DEFAULT_MAX_TOKENS,
            "stream": True,
        }
        if system:
            # Tools render before the system prompt, so one breakpoint on the
            # system block caches the whole static prefix -- the tool table and
            # the instructions -- which is the largest part that never changes.
            payload["system"] = (
                [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                if request.cache_key is not None
                else system
            )
        if request.tools:
            payload["tools"] = [
                {
                    "name": item.name,
                    "description": item.description,
                    "input_schema": item.input_schema,
                }
                for item in request.tools
            ]
            payload["tool_choice"] = {"type": "any" if request.tool_choice == "required" else "auto"}
        if request.reasoning_effort is not None:
            # Anthropic carries the dial inside output_config, and accepts the
            # full range KaroX exposes, so nothing is narrowed here.
            payload["output_config"] = {"effort": request.reasoning_effort}
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        return payload

    def _events(
        self, response: httpx.Response, attempts: int, deadline: float
    ) -> Iterator[ModelEvent]:
        response_id: Optional[str] = None
        finish_reason: Optional[str] = None
        message_started = False
        block_types: Dict[int, str] = {}
        stopped_blocks: set[int] = set()
        # Per block index, because a signature belongs to one block rather than to
        # the concatenated reasoning string.
        reasoning_parts: Dict[int, Dict[str, Any]] = {}
        terminal_event: Optional[ModelEvent] = None
        for event_name, data in self._sse_records(response, deadline):
            if terminal_event is not None:
                continue
            value = self._json_event(data, response.status_code)
            kind = event_name or value.get("type")
            if kind == "message_start":
                if message_started:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic message_start is duplicate",
                    )
                message = value.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic message_start is invalid",
                    )
                message_started = True
                response_id = message["id"]
                try:
                    # input_tokens counts only what was NOT served from cache,
                    # so reporting it alone understates the prompt and hides
                    # whether caching worked at all. Caching fails silently --
                    # one changed byte in the prefix and every request pays full
                    # price with no error -- so the read count is the only
                    # evidence there is.
                    usage = self._usage(
                        message.get("usage", {}),
                        {
                            "input_tokens": "prompt_tokens",
                            "cache_read_input_tokens": "cache_read_tokens",
                            "cache_creation_input_tokens": "cache_write_tokens",
                        },
                    )
                except ValueError as exc:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic usage is invalid",
                    ) from exc
                # Anthropic reports the three parts of the prompt separately;
                # every other provider reports the total with the cached part as
                # a subset of it. Normalising to the second shape gives one
                # meaning of prompt_tokens across the product -- the whole
                # prompt -- and one costing rule that is correct for all of them.
                if usage:
                    usage["prompt_tokens"] = (
                        usage.get("prompt_tokens", 0)
                        + usage.get("cache_read_tokens", 0)
                        + usage.get("cache_write_tokens", 0)
                    )
                if usage:
                    yield ModelEvent(
                        ModelEventKind.USAGE,
                        usage=usage,
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
                continue
            if kind == "content_block_start":
                if not message_started:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic content block started before message_start",
                    )
                index = value.get("index")
                block = value.get("content_block")
                if isinstance(index, bool) or not isinstance(index, int) or not isinstance(block, dict):
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic content block start is invalid",
                    )
                if index < 0 or index in block_types:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic content block index is duplicate or invalid",
                    )
                block_type = block.get("type")
                if not isinstance(block_type, str) or not block_type:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic content block has no type",
                    )
                # Anything other than text or a tool call is recorded so its
                # deltas and its stop event can be accounted for, then ignored.
                # Refusing unknown block types aborted the turn outright: the
                # current Anthropic models emit `thinking` blocks, and on the
                # flagship model thinking is on by default, so the strict list
                # made that model unusable rather than safe.
                block_types[index] = block_type
                if block_type == "text":
                    initial_text = block.get("text", "")
                    if not isinstance(initial_text, str):
                        raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Anthropic text block is invalid")
                    if initial_text:
                        yield ModelEvent(ModelEventKind.TEXT_DELTA, text_delta=initial_text, response_id=response_id, transport_attempts=attempts)
                elif block_type in _ANTHROPIC_REASONING_BLOCKS:
                    initial_thinking = block.get("thinking", "")
                    if not isinstance(initial_thinking, str):
                        initial_thinking = ""
                    opaque = block.get("data", "")
                    reasoning_parts[index] = {
                        "kind": block_type,
                        "text": [initial_thinking],
                        # A redacted block's payload is encrypted and arrives
                        # whole; only readable thinking streams in deltas.
                        "data": opaque if isinstance(opaque, str) else "",
                        "signature": block.get("signature", "")
                        if isinstance(block.get("signature"), str)
                        else "",
                    }
                    if initial_thinking:
                        yield ModelEvent(
                            ModelEventKind.REASONING_DELTA,
                            reasoning_delta=initial_thinking,
                            response_id=response_id,
                            transport_attempts=attempts,
                        )
                elif block_type == "tool_use":
                    call_id, name = block.get("id"), block.get("name")
                    if not isinstance(call_id, str) or not isinstance(name, str):
                        raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Anthropic tool block is invalid")
                    initial = block.get("input", {})
                    if not isinstance(initial, dict):
                        raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Anthropic tool input is invalid")
                    arguments = "" if initial == {} else json.dumps(initial, ensure_ascii=False, separators=(",", ":"))
                    yield ModelEvent(ModelEventKind.TOOL_CALL_DELTA, tool_call_delta=ToolCallDelta(index, call_id, name, arguments), response_id=response_id, transport_attempts=attempts)
                continue
            if kind == "content_block_delta":
                if not message_started:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic content delta arrived before message_start",
                    )
                index, delta = value.get("index"), value.get("delta")
                if isinstance(index, bool) or not isinstance(index, int) or not isinstance(delta, dict):
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Anthropic content delta is invalid")
                if index not in block_types or index in stopped_blocks:
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Anthropic content delta references an unknown block")
                delta_type = delta.get("type")
                expected_type = block_types[index]
                if delta_type == "text_delta" and expected_type == "text" and isinstance(delta.get("text"), str):
                    yield ModelEvent(ModelEventKind.TEXT_DELTA, text_delta=delta["text"], response_id=response_id, transport_attempts=attempts)
                elif delta_type == "input_json_delta" and expected_type == "tool_use" and isinstance(delta.get("partial_json"), str):
                    yield ModelEvent(ModelEventKind.TOOL_CALL_DELTA, tool_call_delta=ToolCallDelta(index, arguments_fragment=delta["partial_json"]), response_id=response_id, transport_attempts=attempts)
                elif (
                    delta_type == "thinking_delta"
                    and expected_type in _ANTHROPIC_REASONING_BLOCKS
                    and isinstance(delta.get("thinking"), str)
                ):
                    if index in reasoning_parts:
                        reasoning_parts[index]["text"].append(delta["thinking"])
                    yield ModelEvent(
                        ModelEventKind.REASONING_DELTA,
                        reasoning_delta=delta["thinking"],
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
                elif delta_type == "signature_delta" and expected_type in _ANTHROPIC_REASONING_BLOCKS:
                    # The signature authenticates the block it arrives inside, and
                    # the API verifies it when the block comes back with the tool
                    # result it preceded. It is accumulated verbatim -- never
                    # re-encoded, never normalized -- because any edit is rejected.
                    fragment = delta.get("signature")
                    if index in reasoning_parts and isinstance(fragment, str):
                        reasoning_parts[index]["signature"] += fragment
                    continue
                elif expected_type not in {"text", "tool_use"}:
                    # A delta belonging to a block type this adapter does not
                    # model. Ignoring it keeps a new Anthropic block type from
                    # ending the turn; the block is not part of the answer.
                    continue
                else:
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Anthropic content delta does not match its block")
                continue
            if kind == "content_block_stop":
                if not message_started:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic content stop arrived before message_start",
                    )
                index = value.get("index")
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or index not in block_types
                    or index in stopped_blocks
                ):
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic content stop references an unknown block",
                    )
                stopped_blocks.add(index)
                pending = reasoning_parts.pop(index, None)
                if pending is not None:
                    # Both the text and the signature are complete only now, so
                    # this is the one moment the whole block can be handed over.
                    yield ModelEvent(
                        ModelEventKind.REASONING_DELTA,
                        reasoning_block=ReasoningBlock(
                            kind=pending["kind"],
                            text="".join(pending["text"]),
                            data=pending["data"],
                            signature=pending["signature"],
                        ),
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
                continue
            if kind == "message_delta":
                if not message_started:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic message delta arrived before message_start",
                    )
                delta = value.get("delta")
                if not isinstance(delta, dict):
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Anthropic message delta is invalid")
                raw_reason = delta.get("stop_reason")
                if raw_reason is not None and not isinstance(raw_reason, str):
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Anthropic stop reason is invalid")
                finish_reason = raw_reason
                try:
                    usage = self._usage(value.get("usage", {}), {"output_tokens": "completion_tokens"})
                except ValueError as exc:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic usage is invalid",
                    ) from exc
                if usage:
                    yield ModelEvent(ModelEventKind.USAGE, usage=usage, response_id=response_id, transport_attempts=attempts)
                continue
            if kind == "message_stop":
                if not message_started:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic message stopped before message_start",
                    )
                if len(stopped_blocks) != len(block_types):
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic message stopped with open content blocks",
                    )
                terminal_event = ModelEvent(
                    ModelEventKind.COMPLETION,
                    finish_reason=finish_reason,
                    response_id=response_id,
                    transport_attempts=attempts,
                )
                continue
            if kind == "error":
                raise ProviderError(
                    stream_error_kind(value.get("error")),
                    "Anthropic reported a stream error",
                )
            if kind == "ping":
                continue
            # Failing closed on content is right; failing closed on protocol
            # evolution is not. An event type added by the provider after this
            # code was written carries no content KaroX acts on, so it is
            # skipped instead of ending an otherwise healthy turn.
            continue
        if terminal_event is not None:
            yield terminal_event
            return
        raise ProviderError(ProviderErrorKind.TRANSPORT, "Anthropic stream ended before message_stop")
