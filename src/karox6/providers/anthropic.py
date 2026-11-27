"""Anthropic Messages streaming adapter."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from karox6.core.messages import Message, ToolCall, Usage, image_note, image_payload
from karox6.providers._stream import StreamProvider, stream_error
from karox6.providers.base import (
    Delta,
    DeltaCallback,
    ModelRequest,
    ModelResponse,
    ProviderError,
    is_empty,
)

CACHE_MESSAGE_BREAKPOINTS = 2
_CLAUDE_VERSION = re.compile(r"claude-(?:opus|sonnet|haiku)-(\d+)(?:[-.](\d{1,2}))?(?!\d)")
_EFFORTS = {"minimal": "low", "low": "low", "medium": "medium", "high": "high"}
_EFFORTS |= {"xhigh": "xhigh", "max": "max"}


def adaptive_thinking(model: str) -> bool:
    """Models that take ``thinking: adaptive`` plus ``output_config.effort``: 4.6 and
    later, Fable and Mythos. They answer a ``budget_tokens`` request with HTTP 400 (4.7+)
    or deprecate it (4.6); older models only know the budget."""
    name = model.lower()
    if "fable" in name or "mythos" in name:
        return True
    match = _CLAUDE_VERSION.search(name)
    if match is None:
        return False
    major, minor = int(match.group(1)), int(match.group(2) or 0)
    return (major, minor) >= (4, 6)


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
                for image in m.images:
                    payload = image_payload(image)
                    if payload is None:
                        blocks.append({"type": "text", "text": image_note(image)})
                    else:
                        source = {"type": "base64", "media_type": payload[0], "data": payload[1]}
                        blocks.append({"type": "image", "source": source})
                if m.content or not m.images:
                    blocks.append({"type": "text", "text": m.content})
            if not blocks:
                continue  # the API rejects a message with no content blocks
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"].extend(blocks)
            else:
                messages.append({"role": role, "content": blocks})
        # Cache breakpoints (4 allowed; the system block below is one): the end of the
        # conversation, where this request writes its cache entry, and the end of the
        # user message before it, where the previous request wrote its own. The second
        # one makes the read exact: without it the API only looks 20 blocks back from
        # the new breakpoint, and a step with many parallel tool calls adds more blocks
        # than that, so the whole conversation was written to the cache again at 1.25x.
        marked = 0
        for msg in reversed(messages):
            if msg["role"] == "user" and msg["content"]:
                msg["content"][-1]["cache_control"] = {"type": "ephemeral"}
                marked += 1
                if marked == CACHE_MESSAGE_BREAKPOINTS:
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
        effort = _EFFORTS.get((request.reasoning_effort or "").lower())
        budget = {"low": 2048, "medium": 8192, "high": 16384, "max": 32000}.get(
            request.reasoning_effort or ""
        )
        if effort and adaptive_thinking(request.model):
            body["thinking"] = {"type": "adaptive"}
            if effort == "xhigh" and re.search(r"-4[-.]6(?!\d)", request.model):
                effort = "high"  # 4.6 has no xhigh (HTTP 400); max it does have
            body["output_config"] = {"effort": effort}
        elif budget:
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
                    "refusal": "content_filter",  # stopped by a safeguard: not an answer
                    "model_context_window_exceeded": "window",
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
            self.base_url.rstrip("/") + "/v1/messages",
            self.build_body(request),
            headers,
            feed,
            flowing=lambda: bool(text) or any(fragments.values()),
        )
        if not done:
            raise ProviderError("transport", "stream ended before the response finished")
        if finish == "window":
            # Input plus this reply filled the model's window: the conversation has to
            # shrink before the step is asked again; "continue" would hit the same wall.
            raise ProviderError(
                "context_window", "the reply stopped at the model's context window limit"
            )
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
        if is_empty(message) and finish not in ("length", "content_filter"):
            raise ProviderError("empty_response", "the model returned no text and no tool calls")
        return ModelResponse(message, usage, finish, model)
