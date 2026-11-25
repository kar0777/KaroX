"""OpenAI-compatible Chat Completions provider (streaming, tool calls, usage).

Works with OpenAI, DeepSeek, OpenRouter, LiteLLM gateways, vLLM, llama.cpp and the
Gemini OpenAI-compatible endpoint. Round-trips what such servers need back:
``reasoning_content`` (DeepSeek thinking + tools) and ``extra_content`` on tool calls
(Gemini ``thought_signature``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from karox6.core.messages import Message, ToolCall, Usage, image_note, image_payload
from karox6.providers.base import (
    STREAM_IDLE_S,
    Delta,
    DeltaCallback,
    ErrorCode,
    ModelRequest,
    ModelResponse,
    ProviderError,
    StreamStalled,
    is_empty,
    retry_after_seconds,
    stream_lines,
)

if TYPE_CHECKING:
    import httpx

_REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_text")
_CONTEXT_HINTS = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "context window",
    # the same refusal in other servers' words
    "prompt is too long",  # Anthropic: prompt is too long: N tokens > M maximum
    "exceed context limit",  # Anthropic: input length and `max_tokens` exceed context limit
    "exceeds the maximum number of tokens",  # Gemini: The input token count (N) exceeds ...
    "maximum prompt length",  # xAI: This model's maximum prompt length is N but ...
    "exceeds the available context size",  # llama.cpp server
    "input is too long",  # Bedrock: Input is too long for requested model.
)
# In a 5xx body only explicit overflow wording counts ("failed to load context window
# configuration" is a gateway fault to retry, not an overflow).
_OVERFLOW_HINTS = (
    "context_length_exceeded",
    "maximum context",
    "too many tokens",
    "exceeds the context",
    "exceed the context",
    "context length exceeded",
    "prompt is too long",
    "exceed context limit",
    "maximum prompt length",
    "exceeds the available context size",
    "exceeds the maximum number of tokens",
)


# (what to change, the field the server's error names)
_QUIRKS = (
    ("max_completion_tokens", "max_completion_tokens"),
    ("no_temperature", "temperature"),
    ("no_stream_options", "stream_options"),
    ("no_reasoning_effort", "reasoning_effort"),
)
_REFUSAL_WORDS = (
    "unsupported",
    "not supported",
    "does not support",
    "unknown",
    "unrecognized",
    "unexpected",
    "extra inputs",
    "not permitted",
    "only the default",
)


@dataclass(slots=True)
class OpenAIChatProvider:
    base_url: str
    api_key: str | None = None
    name: str = "openai-chat"
    stream: bool = True
    timeout_s: float = 900.0
    headers: dict[str, str] = field(default_factory=dict)
    stream_idle_s: float | None = STREAM_IDLE_S  # silence allowed once the answer flows
    # What this server turned out not to accept (learned from its 400 answers, see
    # ``_learn``): the request is then built without it for the rest of the session.
    quirks: set[str] = field(default_factory=set)
    _client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            import httpx

            timeout = httpx.Timeout(self.timeout_s, connect=30.0)
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    def _learn(self, exc: ProviderError) -> bool:
        """A request field this server refuses, read from its error: True when the next
        request will differ (so it is worth sending again). OpenAI's reasoning models
        want ``max_completion_tokens`` instead of ``max_tokens`` and no ``temperature``;
        some compatible servers reject ``stream_options`` or ``reasoning_effort``."""
        if exc.code != "bad_request" or exc.status not in (400, 422):
            return False
        text = str(exc).lower()
        if not any(word in text for word in _REFUSAL_WORDS):
            return False
        for quirk, field_name in _QUIRKS:
            if quirk not in self.quirks and field_name in text:
                self.quirks.add(quirk)
                return True
        return False

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def build_body(self, request: ModelRequest) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend(to_wire(m) for m in request.messages)
        body: dict[str, Any] = {"model": request.model, "messages": messages}
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
        quirks = self.quirks
        if request.max_output_tokens:
            name = "max_completion_tokens" if "max_completion_tokens" in quirks else "max_tokens"
            body[name] = request.max_output_tokens
        if request.temperature is not None and "no_temperature" not in quirks:
            body["temperature"] = request.temperature
        if request.reasoning_effort and "no_reasoning_effort" not in quirks:
            body["reasoning_effort"] = request.reasoning_effort
        if self.stream:
            body["stream"] = True
            if "no_stream_options" not in quirks:
                body["stream_options"] = {"include_usage": True}
        body.update(request.extra_body)
        return body

    async def complete(
        self, request: ModelRequest, on_delta: DeltaCallback | None = None
    ) -> ModelResponse:
        while True:
            try:
                return await self._attempt(request, on_delta)
            except ProviderError as exc:
                # A field the server refuses is dropped (or renamed) and the request
                # sent again at once: nothing was generated or billed for a 400.
                if not self._learn(exc):
                    raise

    async def _attempt(
        self, request: ModelRequest, on_delta: DeltaCallback | None = None
    ) -> ModelResponse:
        import httpx

        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json", **self.headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = self.build_body(request)
        acc = _Accumulator()
        try:
            if body.get("stream"):
                async with self._http().stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        raise _http_error(resp.status_code, resp.text, resp.headers)
                    try:
                        async for line in stream_lines(
                            resp, self.stream_idle_s, lambda: acc.flowing
                        ):
                            acc.feed_sse_line(line, on_delta)
                            if acc.done:
                                break  # a reset after [DONE] must not discard the reply
                    except (httpx.TimeoutException, httpx.TransportError, StreamStalled):
                        # The connection dropped after the reply finished (finish reason
                        # and usage arrived, only [DONE] is missing): keep it rather than
                        # pay for the whole request again. Without usage the max_tokens
                        # check below could not run, so that case is retried.
                        if not acc.finished_cleanly():
                            raise
                    if acc.finish_reason is None and not (acc.done and acc.usage_seen):
                        # A proxy may close the stream with [DONE] after its upstream
                        # dropped mid-reply; partial text is not an answer. A gateway that
                        # never sends finish_reason still sends the final usage chunk.
                        raise ProviderError(
                            "transport", "stream ended before the response finished"
                        )
            else:
                resp = await self._http().post(url, json=body, headers=headers)
                if resp.status_code >= 400:
                    raise _http_error(resp.status_code, resp.text, resp.headers)
                acc.feed_complete(resp.json())
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", f"request timed out: {exc!r}") from exc
        except httpx.TransportError as exc:
            raise ProviderError("transport", f"connection failed: {exc!r}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("server", f"invalid JSON from server: {exc}") from exc
        response = acc.result()
        cap = body.get("max_tokens") or body.get("max_completion_tokens")  # as sent
        if cap and response.finish_reason != "length" and response.usage.output_tokens == cap:
            # Some routers report tool_calls (or stop) for a reply cut at max_tokens; its
            # last call's arguments are then incomplete, so treat it as the cut it is.
            response.finish_reason = "length"
        if is_empty(response.message) and response.finish_reason not in (
            "length",
            "content_filter",
        ):
            raise ProviderError("empty_response", "the model returned no text and no tool calls")
        return response


def to_wire(m: Message) -> dict[str, Any]:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id or "", "content": m.content}
    if m.role == "assistant":
        # content null is valid only beside tool calls (a reasoning-only reply cut at the
        # token limit has neither).
        out: dict[str, Any] = {
            "role": "assistant",
            "content": (m.content or None) if m.tool_calls else m.content,
        }
        if m.tool_calls:
            out["tool_calls"] = []
            for call in m.tool_calls:
                wire: dict[str, Any] = {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments or "{}"},
                }
                if "extra_content" in call.extra:
                    wire["extra_content"] = call.extra["extra_content"]
                out["tool_calls"].append(wire)
        if m.reasoning:
            out[str(m.extra.get("reasoning_field") or "reasoning_content")] = m.reasoning
        return out
    if m.images:
        parts: list[dict[str, Any]] = [{"type": "text", "text": m.content}] if m.content else []
        for image in m.images:
            payload = image_payload(image)
            if payload is None:
                parts.append({"type": "text", "text": image_note(image)})
            else:
                url = f"data:{payload[0]};base64,{payload[1]}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
        return {"role": m.role, "content": parts}
    return {"role": m.role, "content": m.content}


@dataclass(slots=True)
class _PartialCall:
    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


class _Accumulator:
    def __init__(self) -> None:
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.reasoning_field: str | None = None
        self.calls: dict[int, _PartialCall] = {}
        self.finish_reason: str | None = None
        self.usage = Usage(requests=1)
        self.usage_seen = False
        self.model: str | None = None
        self.done = False
        self.flowing = False  # answer tokens (text or tool calls) have started to arrive

    def feed_sse_line(self, line: str, on_delta: DeltaCallback | None) -> None:
        line = line.strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            return
        data = line[5:].strip()
        if data == "[DONE]":
            self.done = True
            return
        chunk = json.loads(data)
        if isinstance(chunk, dict) and chunk.get("error"):
            err = chunk["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            code = err.get("code") if isinstance(err, dict) else None
            kind = err.get("type") if isinstance(err, dict) else None
            labels = [str(x) for x in (code, kind) if x is not None and not str(x).isdigit()]
            text = ": ".join([*labels, str(message)])
            raise _http_error(_sse_error_status(code, kind), text, {})
        self._chunk(chunk, on_delta)

    def _chunk(self, chunk: dict[str, Any], on_delta: DeltaCallback | None) -> None:
        self.model = chunk.get("model") or self.model
        if chunk.get("usage"):
            self.usage_seen = True
            self._usage(chunk["usage"])
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if isinstance(text, str) and text:
                self.flowing = True
                self.text.append(text)
                if on_delta:
                    on_delta(Delta("text", text))
            for name in _REASONING_FIELDS:
                thought = delta.get(name)
                if isinstance(thought, str) and thought:
                    self.reasoning.append(thought)
                    self.reasoning_field = self.reasoning_field or name
                    if on_delta:
                        on_delta(Delta("reasoning", thought))
                    break
            for part in delta.get("tool_calls") or []:
                self.flowing = True
                self._call_delta(part)
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

    def _call_delta(self, part: dict[str, Any]) -> None:
        index = int(part.get("index", len(self.calls)))
        call = self.calls.setdefault(index, _PartialCall())
        if part.get("id"):
            call.id = part["id"]
        fn = part.get("function") or {}
        name = fn.get("name")
        if name and name != call.name:
            # Most servers send the name once; a few repeat it in every chunk.
            call.name = name if not call.name else call.name + name
        if fn.get("arguments"):
            call.arguments.append(fn["arguments"])
        if part.get("extra_content"):
            call.extra["extra_content"] = part["extra_content"]

    def feed_complete(self, payload: dict[str, Any]) -> None:
        self.model = payload.get("model")
        if payload.get("usage"):
            self._usage(payload["usage"])
        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError("empty_response", "response has no choices")
        choice = choices[0]
        msg = choice.get("message") or {}
        if isinstance(msg.get("content"), str):
            self.text.append(msg["content"])
        for name in _REASONING_FIELDS:
            if isinstance(msg.get(name), str) and msg[name]:
                self.reasoning.append(msg[name])
                self.reasoning_field = name
                break
        for index, raw in enumerate(msg.get("tool_calls") or []):
            fn = raw.get("function") or {}
            call = _PartialCall(
                id=raw.get("id") or "",
                name=fn.get("name") or "",
                arguments=[fn.get("arguments") or ""],
            )
            if raw.get("extra_content"):
                call.extra["extra_content"] = raw["extra_content"]
            self.calls[index] = call
        self.finish_reason = choice.get("finish_reason")
        self.done = True

    def _usage(self, raw: dict[str, Any]) -> None:
        prompt = int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
        completion = int(raw.get("completion_tokens") or raw.get("output_tokens") or 0)
        details = raw.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens") or raw.get("prompt_cache_hit_tokens") or 0)
        reasoning = int((raw.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        self.usage = Usage(
            input_tokens=prompt,
            cached_tokens=cached,
            output_tokens=completion,
            reasoning_tokens=reasoning,
            requests=1,
        )

    def finished_cleanly(self) -> bool:
        """The reply is complete although the connection dropped before its end: with usage
        seen, or tool calls with a terminal finish reason other than a cut ("length") whose
        arguments are all complete JSON. Retrying it would pay for the same reply again."""
        if self.finish_reason is None:
            return False
        if self.usage_seen:
            return True
        if not self.calls or _normalize_finish(self.finish_reason, False) != "stop":
            return False  # a text reply may be one cut at max_tokens that says "stop"
        for call in self.calls.values():
            try:
                json.loads("".join(call.arguments) or "{}")
            except ValueError:
                return False
        return True

    def result(self) -> ModelResponse:
        calls = []
        for n, index in enumerate(sorted(self.calls)):
            part = self.calls[index]
            if not part.name:
                continue
            calls.append(
                ToolCall(
                    id=part.id or f"call_{n}",
                    name=part.name,
                    arguments="".join(part.arguments),
                    extra=part.extra,
                )
            )
        extra: dict[str, Any] = {}
        if self.reasoning_field and self.reasoning_field != "reasoning_content":
            extra["reasoning_field"] = self.reasoning_field
        message = Message(
            "assistant",
            "".join(self.text),
            tool_calls=calls,
            reasoning="".join(self.reasoning) or None,
            extra=extra,
        )
        finish = _normalize_finish(self.finish_reason, bool(calls))
        if finish == "other":
            # "abort", "error", ...: the server stopped generating; partial text is not
            # the model's final answer (it would end the run as completed).
            raise ProviderError(
                "server", f"the reply stopped early (finish_reason={self.finish_reason})"
            )
        return ModelResponse(
            message=message, usage=self.usage, finish_reason=finish, model=self.model
        )


def _sse_error_status(code: Any, kind: Any) -> int:
    """HTTP-like status for an error sent inside the stream; its code is often a string
    ("rate_limit_exceeded", "context_length_exceeded") rather than a number."""
    if str(code).isdigit():
        return int(str(code))
    label = f"{code} {kind}".lower()
    if "rate" in label or "quota" in label:
        return 429
    if any(hint in label for hint in _CONTEXT_HINTS) or "context" in label:
        return 400
    if "overload" in label:
        return 529
    return 500


def _normalize_finish(raw: str | None, has_calls: bool) -> str:
    if raw in ("length", "max_tokens"):
        return "length"
    if raw == "content_filter":
        return "content_filter"
    if has_calls:
        return "tool_calls"
    return "stop" if raw in (None, "stop", "end_turn", "tool_calls", "function_call") else "other"


# A 429/402 that means "out of money or allowance", not "too fast": retrying for minutes
# only hides the reason from the user.
_QUOTA_HINTS = (
    "insufficient_quota",
    "insufficient balance",
    "insufficient_balance",
    "exceeded your current quota",
    "quota exceeded",
    "daily_usage_exceeded",
    "billing",
    "top up your balance",
    "credit balance is too low",
    # an allowance per day: minutes of waiting do not bring it back
    "daily limit",
    "daily quota",
    "per day",
    "per-day",
    "дневной лимит",
    "суточный лимит",
)
# A "quota" refusal that names a short wait is a per-minute window in quota wording
# (Gemini's free tier: "You exceeded your current quota ... Please retry in 23s").
QUOTA_SHORT_WAIT_S = 120.0


def _http_error(status: int, text: str, headers: Any) -> ProviderError:
    snippet = " ".join(text.split())[:500]
    retry_after = retry_after_seconds(headers, text)
    code: ErrorCode
    lowered = snippet.lower()
    if status in (402, 429) and any(hint in lowered for hint in _QUOTA_HINTS):
        short = retry_after is not None and retry_after <= QUOTA_SHORT_WAIT_S
        code = "rate_limit" if status == 429 and short else "quota"
    elif status == 429:
        code = "rate_limit"
    elif status in (401, 403):
        code = "auth"
    elif status in (408, 504):
        code = "timeout"
    elif status >= 500 and any(hint in lowered for hint in _OVERFLOW_HINTS):
        # Gateways wrap an upstream context overflow in a 500/502; retrying the same
        # oversized request cannot succeed.
        code = "context_window"
    elif status >= 500 or status == 409:
        code = "server"
    elif any(hint in lowered for hint in _CONTEXT_HINTS):
        code = "context_window"
    else:
        code = "bad_request"
    return ProviderError(code, snippet or f"HTTP {status}", status=status, retry_after=retry_after)
