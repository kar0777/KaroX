"""Anthropic Messages streaming adapter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from karox6.core.messages import Message, ToolCall, Usage
from karox6.providers._stream import StreamProvider, stream_error
from karox6.providers.base import (
    Delta,
    DeltaCallback,
    ModelRequest,
    ModelResponse,
    ProviderError,
    is_empty,
)


@dataclass(slots=True)
class AnthropicProvider(StreamProvider):
    name: str = "anthropic"

    def build_body(self, request: ModelRequest) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        for m in request.messages:
            role = "assistant" if m.role == "assistant" else "user"
            blocks: list[dict[str, Any]] = []
            if role == "assistant":
                blocks.extend(m.extra.get("anthropic_thinking") or [])
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for call in m.tool_calls:
                    try:
                        args = json.loads(call.arguments)
                    except (ValueError, TypeError):
                        args = {}
                    blocks.append(
                        {"type": "tool_use", "id": call.id, "name": call.name, "input": args}
                    )
            elif m.role == "tool":
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id or "",
                        "content": m.content,
                        "is_error": m.is_error,
                    }
                )
            else:
                blocks.append({"type": "text", "text": m.content})
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"].extend(blocks)
            else:
                messages.append({"role": role, "content": blocks})
        for msg in reversed(messages):
            if msg["role"] == "user" and msg["content"]:
                msg["content"][-1]["cache_control"] = {"type": "ephemeral"}
                break
        body: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens or 32000,
            "stream": True,
        }
        if request.system:
            body["system"] = [
                {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
            ]
        if request.tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in request.tools
            ]
        budget = {"low": 2048, "medium": 8192, "high": 16384, "max": 32000}.get(
            request.reasoning_effort or ""
        )
        if budget:
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            body["max_tokens"] = max(body["max_tokens"], budget + 8192)
        elif request.temperature is not None:
            body["temperature"] = request.temperature
        body.update(request.extra_body)
        return body

    async def complete(
        self, request: ModelRequest, on_delta: DeltaCallback | None = None
    ) -> ModelResponse:
        text: list[str] = []
        reasoning: list[str] = []
        blocks: dict[int, dict[str, Any]] = {}
        fragments: dict[int, list[str]] = {}
        usage = Usage(requests=1)
        finish = "other"
        model: str | None = None
        done = False

        def feed(event: dict[str, Any]) -> None:
            nonlocal finish, model, done
            kind = event.get("type")
            if kind == "error":
                raise stream_error(event.get("error"))
            if kind == "message_start":
                msg = event.get("message") or {}
                model = msg.get("model")
                u = msg.get("usage") or {}
                usage.cached_tokens = int(u.get("cache_read_input_tokens") or 0)
                usage.input_tokens = (
                    int(u.get("input_tokens") or 0)
                    + usage.cached_tokens
                    + int(u.get("cache_creation_input_tokens") or 0)
                )
            elif kind == "content_block_start":
                idx = int(event.get("index", 0))
                blocks[idx] = dict(event.get("content_block") or {})
                fragments[idx] = []
                b = blocks[idx]
                if b.get("type") == "text" and b.get("text"):
                    text.append(b["text"])
                if b.get("type") == "thinking" and b.get("thinking"):
                    reasoning.append(b["thinking"])
            elif kind == "content_block_delta":
                idx = int(event.get("index", 0))
                b = blocks.setdefault(idx, {})
                d = event.get("delta") or {}
                typ = d.get("type")
                if typ == "text_delta":
                    value = d.get("text", "")
                    text.append(value)
                    if on_delta:
                        on_delta(Delta("text", value))
                elif typ == "thinking_delta":
                    value = d.get("thinking", "")
                    b["thinking"] = b.get("thinking", "") + value
                    reasoning.append(value)
                    if on_delta:
                        on_delta(Delta("reasoning", value))
                elif typ == "signature_delta":
                    b["signature"] = b.get("signature", "") + d.get("signature", "")
                elif typ == "input_json_delta":
                    fragments.setdefault(idx, []).append(d.get("partial_json", ""))
            elif kind == "message_delta":
                raw = (event.get("delta") or {}).get("stop_reason")
                finish = {
                    "end_turn": "stop",
                    "stop_sequence": "stop",
                    "tool_use": "tool_calls",
                    "max_tokens": "length",
                }.get(str(raw), "other")
                usage.output_tokens = int((event.get("usage") or {}).get("output_tokens") or 0)
            elif kind == "message_stop":
                done = True

        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            **self.headers,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        await self._stream(
            self.base_url.rstrip("/") + "/v1/messages", self.build_body(request), headers, feed
        )
        if not done:
            raise ProviderError("transport", "stream ended before the response finished")
        calls = [
            ToolCall(
                id=b.get("id", ""),
                name=b.get("name", ""),
                arguments="".join(fragments.get(i, [])) or json.dumps(b.get("input") or {}),
            )
            for i, b in sorted(blocks.items())
            if b.get("type") == "tool_use"
        ]
        thinking = [
            b
            for _, b in sorted(blocks.items())
            if b.get("type") in ("thinking", "redacted_thinking")
        ]
        message = Message(
            "assistant",
            "".join(text),
            tool_calls=calls,
            reasoning="".join(reasoning) or None,
            extra={"anthropic_thinking": thinking} if thinking else {},
        )
        if is_empty(message) and finish != "length":
            raise ProviderError("empty_response", "the model returned no text and no tool calls")
        return ModelResponse(message, usage, finish, model)
