"""Gemini GenerateContent streaming adapter."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Iterator, Optional
from urllib.parse import quote
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
    GEMINI_SIGNATURE_PREFIX,
    _StreamingAdapter,
    _gemini_thinking_level,
    _is_gemini_signature,
    stream_error_kind,
)


class GeminiGenerateContentProvider(_StreamingAdapter):
    provider_name = "gemini_generate_content"

    def _endpoint(self, request: ModelRequest) -> str:
        model = quote(request.model, safe="._-")
        return self._url(f"/models/{model}:streamGenerateContent", {"alt": "sse"})

    def _request_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            **self._headers,
        }
        secret = self._secret()
        if secret is not None:
            headers["x-goog-api-key"] = secret
        return headers

    @staticmethod
    def _contents(request: ModelRequest) -> tuple[Optional[Dict[str, Any]], list[Dict[str, Any]]]:
        systems: list[str] = []
        contents: list[Dict[str, Any]] = []
        names: Dict[str, str] = {}
        for message in request.messages:
            if message.role == "system":
                if message.content:
                    systems.append(message.content)
                continue
            if message.role == "tool":
                name = names.get(message.tool_call_id or "")
                if name is None:
                    raise ValueError("Gemini tool result references an unknown call")
                try:
                    parsed = json.loads(message.content or "null")
                except json.JSONDecodeError:
                    parsed = message.content or ""
                response_value = parsed if isinstance(parsed, dict) else {"result": parsed}
                part = {"functionResponse": {"name": name, "response": response_value}}
                if contents and contents[-1]["role"] == "user":
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
                continue
            role = "model" if message.role == "assistant" else "user"
            signatures = {
                str(block.data)[len(GEMINI_SIGNATURE_PREFIX):]: block.signature
                for block in message.reasoning_blocks
                if _is_gemini_signature(block) and block.signature
            }
            parts: list[Dict[str, Any]] = []
            if message.content is not None:
                text_part: Dict[str, Any] = {"text": message.content}
                if signatures.get("text"):
                    text_part["thoughtSignature"] = signatures["text"]
                parts.append(text_part)
            for call in message.tool_calls:
                names[call.call_id] = call.name
                call_part: Dict[str, Any] = {
                    "functionCall": {
                        "name": call.name,
                        "args": json.loads(call.raw_arguments),
                    }
                }
                signature = signatures.get("call:" + call.call_id)
                if signature:
                    call_part["thoughtSignature"] = signature
                parts.append(call_part)
            contents.append({"role": role, "parts": parts})
        system = (
            {"parts": [{"text": "\n\n".join(systems)}]} if systems else None
        )
        return system, contents

    def _request_payload(self, request: ModelRequest) -> Dict[str, Any]:
        try:
            system, contents = self._contents(request)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_REQUEST,
                "Gemini request contains invalid persisted tool data",
            ) from exc
        payload: Dict[str, Any] = {"contents": contents}
        if system is not None:
            payload["systemInstruction"] = system
        if request.tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": item.name,
                            "description": item.description,
                            "parameters": item.input_schema,
                        }
                        for item in request.tools
                    ]
                }
            ]
            payload["toolConfig"] = {
                "functionCallingConfig": {
                    "mode": "ANY" if request.tool_choice == "required" else "AUTO"
                }
            }
        generation: Dict[str, Any] = {}
        if request.temperature is not None:
            generation["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            generation["maxOutputTokens"] = request.max_output_tokens
        if request.reasoning_effort is not None:
            generation["thinkingConfig"] = {
                "thinkingLevel": _gemini_thinking_level(request.reasoning_effort)
            }
        if generation:
            payload["generationConfig"] = generation
        return payload

    def _events(
        self, response: httpx.Response, attempts: int, deadline: float
    ) -> Iterator[ModelEvent]:
        terminal_event: Optional[ModelEvent] = None
        response_id: Optional[str] = None
        call_scope = uuid.uuid4().hex
        next_tool_index = 0
        for _event_name, data in self._sse_records(response, deadline):
            if terminal_event is not None:
                continue
            value = self._json_event(data, response.status_code)
            if isinstance(value.get("error"), dict):
                raise ProviderError(
                    stream_error_kind(value.get("error")),
                    "Gemini reported a stream error",
                )
            raw_id = value.get("responseId")
            if raw_id is not None:
                if not isinstance(raw_id, str):
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini response ID is invalid")
                response_id = raw_id
            usage_raw = value.get("usageMetadata")
            if usage_raw is not None:
                try:
                    usage = self._usage(
                        usage_raw,
                        {
                            "promptTokenCount": "prompt_tokens",
                            "candidatesTokenCount": "completion_tokens",
                            "totalTokenCount": "total_tokens",
                            # Gemini caches implicitly, so the saving happens
                            # whether or not KaroX asked for it -- but it is
                            # invisible unless the count is read back.
                            "cachedContentTokenCount": "cache_read_tokens",
                            "thoughtsTokenCount": "reasoning_tokens",
                        },
                    )
                except ValueError as exc:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Gemini usage is invalid",
                    ) from exc
                yield ModelEvent(ModelEventKind.USAGE, usage=usage, response_id=response_id, transport_attempts=attempts)
            candidates = value.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                if usage_raw is not None:
                    continue
                raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini candidates must be a non-empty array")
            candidate = candidates[0]
            if not isinstance(candidate, dict):
                raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini candidate is invalid")
            content = candidate.get("content", {})
            if not isinstance(content, dict) or not isinstance(content.get("parts", []), list):
                raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini content is invalid")
            for part in content.get("parts", []):
                if not isinstance(part, dict):
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini content part is invalid")
                signature = part.get("thoughtSignature")
                if signature is not None and not isinstance(signature, str):
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini thought signature is invalid")
                if "text" in part:
                    if not isinstance(part["text"], str):
                        raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini text part is invalid")
                    if signature and part.get("thought") is not True:
                        yield ModelEvent(
                            ModelEventKind.REASONING_DELTA,
                            reasoning_block=ReasoningBlock(
                                "thinking",
                                data=GEMINI_SIGNATURE_PREFIX + "text",
                                signature=signature,
                            ),
                            response_id=response_id,
                            transport_attempts=attempts,
                        )
                    # A part flagged `thought` is the model's private reasoning,
                    # not its answer. Treating it as answer text streamed private
                    # deliberation to the user and persisted it as the response.
                    if part.get("thought") is True:
                        yield ModelEvent(
                            ModelEventKind.REASONING_DELTA,
                            reasoning_delta=part["text"],
                            response_id=response_id,
                            transport_attempts=attempts,
                        )
                    else:
                        yield ModelEvent(ModelEventKind.TEXT_DELTA, text_delta=part["text"], response_id=response_id, transport_attempts=attempts)
                if "functionCall" in part:
                    call = part["functionCall"]
                    if not isinstance(call, dict) or not isinstance(call.get("name"), str) or not isinstance(call.get("args"), dict):
                        raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini function call is invalid")
                    index = next_tool_index
                    next_tool_index += 1
                    response_scope = response_id or "no-response-id"
                    call_id = f"gemini-call-{call_scope}-{response_scope}-{index}"
                    if signature:
                        yield ModelEvent(
                            ModelEventKind.REASONING_DELTA,
                            reasoning_block=ReasoningBlock(
                                "thinking",
                                data=GEMINI_SIGNATURE_PREFIX + "call:" + call_id,
                                signature=signature,
                            ),
                            response_id=response_id,
                            transport_attempts=attempts,
                        )
                    yield ModelEvent(
                        ModelEventKind.TOOL_CALL_DELTA,
                        tool_call_delta=ToolCallDelta(
                            index,
                            call_id,
                            call["name"],
                            json.dumps(call["args"], ensure_ascii=False, separators=(",", ":")),
                        ),
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
            finish = candidate.get("finishReason")
            if finish is not None:
                if not isinstance(finish, str):
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini finish reason is invalid")
                terminal_event = ModelEvent(
                    ModelEventKind.COMPLETION,
                    finish_reason=finish,
                    response_id=response_id,
                    transport_attempts=attempts,
                )
                continue
        if terminal_event is not None:
            yield terminal_event
            return
        raise ProviderError(ProviderErrorKind.TRANSPORT, "Gemini stream ended before a finish reason")
