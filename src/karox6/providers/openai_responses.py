"""OpenAI Responses streaming adapter."""

from __future__ import annotations

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


@dataclass(slots=True)
class OpenAIResponsesProvider(StreamProvider):
    name: str = "openai-responses"

    def build_body(self, request: ModelRequest) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for m in request.messages:
            if m.role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": m.tool_call_id or "",
                        "output": m.content,
                    }
                )
            elif m.role == "assistant":
                # Output order of the original response: reasoning, message, function calls.
                items.extend(m.extra.get("responses_reasoning") or [])
                if m.content:
                    items.append({"role": "assistant", "content": m.content})
                for c in m.tool_calls:
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": c.id,
                            "name": c.name,
                            "arguments": c.arguments,
                        }
                    )
            elif m.images:
                content: list[dict[str, Any]] = (
                    [{"type": "input_text", "text": m.content}] if m.content else []
                )
                for image in m.images:
                    payload = image_payload(image)
                    if payload is None:
                        content.append({"type": "input_text", "text": image_note(image)})
                    else:
                        url = f"data:{payload[0]};base64,{payload[1]}"
                        content.append({"type": "input_image", "image_url": url})
                items.append({"role": "user", "content": content})
            else:
                items.append({"role": "user", "content": m.content})
        body: dict[str, Any] = {
            "model": request.model,
            "input": items,
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if request.system:
            body["instructions"] = request.system
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
                for t in request.tools
            ]
        if request.max_output_tokens is not None:
            body["max_output_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.reasoning_effort:
            body["reasoning"] = {"effort": request.reasoning_effort, "summary": "auto"}
        body.update(request.extra_body)
        return body

    async def complete(
        self, request: ModelRequest, on_delta: DeltaCallback | None = None
    ) -> ModelResponse:
        text: list[str] = []
        thoughts: list[str] = []
        items: dict[int, dict[str, Any]] = {}
        fragments: dict[int, list[str]] = {}
        usage = Usage(requests=1)
        finish: str | None = None
        model: str | None = None

        def feed(event: dict[str, Any]) -> None:
            nonlocal finish, model
            typ = event.get("type")
            if typ in ("error", "response.failed"):
                nested = event.get("error") or (event.get("response") or {}).get("error")
                # The `error` event itself has its code and message at the top level.
                raise stream_error(
                    nested or {"code": event.get("code"), "message": event.get("message")}
                )
            if typ in ("response.output_text.delta", "response.reasoning_summary_text.delta"):
                value = event.get("delta", "")
                thought = typ == "response.reasoning_summary_text.delta"
                (thoughts if thought else text).append(value)
                if on_delta:
                    on_delta(Delta("reasoning" if thought else "text", value))
            elif typ in ("response.output_item.added", "response.output_item.done"):
                idx = int(event.get("output_index", 0))
                items[idx] = event.get("item") or {}
            elif typ == "response.function_call_arguments.delta":
                fragments.setdefault(int(event.get("output_index", 0)), []).append(
                    event.get("delta", "")
                )
            elif typ in ("response.completed", "response.incomplete"):
                response = event.get("response") or {}
                model = response.get("model") or model
                u = response.get("usage") or {}
                usage.input_tokens = int(u.get("input_tokens") or 0)
                usage.cached_tokens = int(
                    (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
                )
                usage.output_tokens = int(u.get("output_tokens") or 0)
                usage.reasoning_tokens = int(
                    (u.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
                )
                # Cut by the output limit, stopped by a filter (not the model's answer,
                # so not a normal end), or whole.
                reason = (response.get("incomplete_details") or {}).get("reason")
                finish = (
                    "stop"
                    if typ == "response.completed"
                    else {"max_output_tokens": "length", "content_filter": "content_filter"}.get(
                        str(reason), "other"
                    )
                )

        headers = {"content-type": "application/json", **self.headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        await self._stream(
            self.base_url.rstrip("/") + "/responses",
            self.build_body(request),
            headers,
            feed,
            flowing=lambda: bool(text) or bool(fragments),
        )
        if finish is None:
            raise ProviderError("transport", "stream ended before the response finished")
        calls = [
            ToolCall(
                item.get("call_id", ""),
                item.get("name", ""),
                item.get("arguments") or "".join(fragments.get(i, [])),
            )
            for i, item in sorted(items.items())
            if item.get("type") == "function_call"
        ]
        reasoning = [item for _, item in sorted(items.items()) if item.get("type") == "reasoning"]
        msg = Message(
            "assistant",
            "".join(text),
            tool_calls=calls,
            reasoning="".join(thoughts) or None,
            extra={"responses_reasoning": reasoning} if reasoning else {},
        )
        if is_empty(msg) and finish != "length":
            raise ProviderError("empty_response", "the model returned no text and no tool calls")
        if calls and finish == "stop":
            finish = "tool_calls"
        return ModelResponse(msg, usage, finish, model)
