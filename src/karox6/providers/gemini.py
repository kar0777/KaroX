"""Native Gemini streaming adapter."""

from __future__ import annotations

import json
import secrets
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


def _schema(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _schema(v) for k, v in value.items() if k not in ("additionalProperties", "$schema")
        }
    if isinstance(value, list):
        return [_schema(v) for v in value]
    return value


@dataclass(slots=True)
class GeminiProvider(StreamProvider):
    name: str = "gemini"

    def build_body(self, request: ModelRequest) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        for m in request.messages:
            role = "model" if m.role == "assistant" else "user"
            parts: list[dict[str, Any]] = []
            if m.role == "tool":
                parts.append(
                    {
                        "functionResponse": {
                            "name": names.get(m.tool_call_id or "", m.name or ""),
                            "response": {"error" if m.is_error else "content": m.content},
                        }
                    }
                )
            else:
                if m.content:
                    part: dict[str, Any] = {"text": m.content}
                    if m.extra.get("gemini_text_signature"):
                        part["thoughtSignature"] = m.extra["gemini_text_signature"]
                    parts.append(part)
                for call in m.tool_calls:
                    names[call.id] = call.name
                    try:
                        args = json.loads(call.arguments)
                    except (ValueError, TypeError):
                        args = {}
                    part = {"functionCall": {"name": call.name, "args": args}}
                    if call.extra.get("thought_signature"):
                        part["thoughtSignature"] = call.extra["thought_signature"]
                    parts.append(part)
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": parts})
        body: dict[str, Any] = {"contents": contents}
        if request.system:
            body["systemInstruction"] = {"parts": [{"text": request.system}]}
        if request.tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": _schema(t.parameters),
                        }
                        for t in request.tools
                    ]
                }
            ]
        config: dict[str, Any] = {}
        if request.max_output_tokens is not None:
            config["maxOutputTokens"] = request.max_output_tokens
        if request.temperature is not None:
            config["temperature"] = request.temperature
        budget = {"low": 1024, "medium": 8192, "high": 24576, "max": -1}.get(
            request.reasoning_effort or ""
        )
        if budget is not None:
            config["thinkingConfig"] = {"thinkingBudget": budget, "includeThoughts": True}
        if config:
            body["generationConfig"] = config
        body.update(request.extra_body)
        return body

    async def complete(
        self, request: ModelRequest, on_delta: DeltaCallback | None = None
    ) -> ModelResponse:
        text: list[str] = []
        thoughts: list[str] = []
        calls: list[ToolCall] = []
        usage = Usage(requests=1)
        finish: str | None = None
        model: str | None = None
        signature: str | None = None
        batch = secrets.token_hex(4)

        def feed(event: dict[str, Any]) -> None:
            nonlocal finish, model, signature
            if event.get("error"):
                raise stream_error(event["error"])
            model = event.get("modelVersion") or model
            u = event.get("usageMetadata")
            if u:
                usage.input_tokens = int(u.get("promptTokenCount") or 0)
                usage.cached_tokens = int(u.get("cachedContentTokenCount") or 0)
                usage.reasoning_tokens = int(u.get("thoughtsTokenCount") or 0)
                usage.output_tokens = (
                    int(u.get("candidatesTokenCount") or 0) + usage.reasoning_tokens
                )
            for candidate in event.get("candidates") or []:
                finish = candidate.get("finishReason") or finish
                for part in (candidate.get("content") or {}).get("parts") or []:
                    if "text" in part:
                        value = part["text"]
                        thought = bool(part.get("thought"))
                        (thoughts if thought else text).append(value)
                        if not thought and part.get("thoughtSignature"):
                            signature = part["thoughtSignature"]
                        if on_delta:
                            on_delta(Delta("reasoning" if thought else "text", value))
                    if "functionCall" in part:
                        fn = part["functionCall"]
                        extra = (
                            {"thought_signature": part["thoughtSignature"]}
                            if part.get("thoughtSignature")
                            else {}
                        )
                        calls.append(
                            ToolCall(
                                # Gemini ids are optional; ours must stay unique across turns.
                                str(fn.get("id") or f"gemini_call_{batch}_{len(calls)}"),
                                fn.get("name", ""),
                                json.dumps(fn.get("args") or {}),
                                extra,
                            )
                        )

        headers = {"content-type": "application/json", **self.headers}
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        await self._stream(
            self.base_url.rstrip("/")
            + f"/v1beta/models/{request.model}:streamGenerateContent?alt=sse",
            self.build_body(request),
            headers,
            feed,
        )
        if finish is None:
            raise ProviderError("transport", "stream ended before the response finished")
        normalized = (
            "length"
            if finish == "MAX_TOKENS"
            else ("tool_calls" if calls else "stop")
            if finish == "STOP"
            else "content_filter"
            if finish
            in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY")
            else "other"
        )
        msg = Message(
            "assistant",
            "".join(text),
            tool_calls=calls,
            reasoning="".join(thoughts) or None,
            extra={"gemini_text_signature": signature} if signature else {},
        )
        if is_empty(msg) and normalized != "length":
            raise ProviderError("empty_response", "the model returned no text and no tool calls")
        return ModelResponse(msg, usage, normalized, model)
