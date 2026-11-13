"""OpenAI chat-completions stream parsing and HTTP error mapping."""

from __future__ import annotations

from .provider_types import (  # noqa: F401
    REASONING_EFFORTS as REASONING_EFFORTS,
    CONNECT_TIMEOUT_SECONDS as CONNECT_TIMEOUT_SECONDS,
    CredentialAccessor as CredentialAccessor,
    ImageAttachment as ImageAttachment,
    ModelEvent as ModelEvent,
    ModelEventKind as ModelEventKind,
    ModelMessage as ModelMessage,
    ModelRequest as ModelRequest,
    ModelResponse as ModelResponse,
    Provider as Provider,
    ProviderError as ProviderError,
    ProviderErrorKind as ProviderErrorKind,
    ProviderTool as ProviderTool,
    ReasoningBlock as ReasoningBlock,
    ToolCall as ToolCall,
    ToolCallDelta as ToolCallDelta,
    _ABOVE_HIGH_EFFORTS as _ABOVE_HIGH_EFFORTS,
    _ERROR_BODY_LIMIT as _ERROR_BODY_LIMIT,
    _GO_DURATION_PART as _GO_DURATION_PART,
    _OPTIONAL_TUNING_KEYS as _OPTIONAL_TUNING_KEYS,
    _REASONING_FAMILY as _REASONING_FAMILY,
    _REPLAYED_REASONING_KEY as _REPLAYED_REASONING_KEY,
    _TOOL_NAME as _TOOL_NAME,
    _VENDOR_CODE as _VENDOR_CODE,
    _accepts_prompt_cache_key as _accepts_prompt_cache_key,
    _effort_scale_stops_at_high as _effort_scale_stops_at_high,
    _go_duration_seconds as _go_duration_seconds,
    _has_optional_tuning as _has_optional_tuning,
    _join_identity_fragments as _join_identity_fragments,
    _looks_numeric as _looks_numeric,
    _replays_reasoning as _replays_reasoning,
    _usage_count as _usage_count,
    _vendor_error_codes as _vendor_error_codes,
    _without_optional_tuning as _without_optional_tuning,
    accumulate_response as accumulate_response,
    close_provider_quietly as close_provider_quietly,
    output_token_field as output_token_field,
    transport_timeout as transport_timeout,
)
import json
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import (
    Any,
    Dict,
    Iterator,
    Mapping,
    Optional,
)
import httpx


class ProviderStreamMixin:
    """Methods composed into OpenAIChatCompletionsProvider."""

    @classmethod
    def _stream_events(
        cls,
        response: httpx.Response,
        attempts: int,
        deadline: float,
    ) -> Iterator[ModelEvent]:
        data_lines: list[str] = []
        finish_reason: Optional[str] = None
        response_id: Optional[str] = None
        event_count = 0
        total_chars = 0
        terminal_event: Optional[ModelEvent] = None

        lines = response.iter_lines()
        while True:
            # After [DONE] the answer is complete and paid for: a connection
            # that then breaks or idles past the deadline must not turn it into
            # a transport failure that re-sends (and re-bills) the request.
            try:
                line = next(lines)
            except StopIteration:
                break
            except httpx.TransportError:
                if terminal_event is not None:
                    break
                raise
            if time.monotonic() >= deadline:
                if terminal_event is not None:
                    break
                raise ProviderError(
                    ProviderErrorKind.TRANSPORT,
                    "provider request deadline expired while reading stream",
                    status_code=response.status_code,
                    transport_attempts=attempts,
                )
            if not isinstance(line, str):
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE,
                    "provider SSE stream contained a non-text line",
                    status_code=response.status_code,
                )
            if len(line) > cls.MAX_SSE_LINE_CHARS:
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE,
                    "provider SSE line exceeds the size limit",
                    status_code=response.status_code,
                )
            total_chars += len(line) + 1
            if total_chars > cls.MAX_SSE_TOTAL_CHARS:
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE,
                    "provider SSE stream exceeds the size limit",
                    status_code=response.status_code,
                )
            if line == "":
                if not data_lines:
                    continue
                event_count += 1
                if event_count > cls.MAX_SSE_EVENTS:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "provider SSE stream contains too many events",
                        status_code=response.status_code,
                    )
                data = "\n".join(data_lines)
                data_lines.clear()
                if terminal_event is not None:
                    # The protocol is complete, but the HTTP response must still
                    # reach EOF before its socket can return to the client pool.
                    continue
                if data.strip() == "[DONE]":
                    terminal_event = ModelEvent(
                        ModelEventKind.COMPLETION,
                        finish_reason=finish_reason,
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
                    continue
                events, chunk_finish, chunk_id = cls._parse_stream_chunk(
                    data, response.status_code, attempts
                )
                if chunk_finish is not None:
                    finish_reason = chunk_finish
                if chunk_id is not None:
                    if response_id is not None and response_id != chunk_id:
                        raise ProviderError(
                            ProviderErrorKind.MALFORMED_RESPONSE,
                            "provider changed response ID during stream",
                            status_code=response.status_code,
                        )
                    response_id = chunk_id
                yield from events
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "data":
                data_lines.append(value if separator else "")

        if data_lines and terminal_event is None:
            event_count += 1
            if event_count > cls.MAX_SSE_EVENTS:
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE,
                    "provider SSE stream contains too many events",
                    status_code=response.status_code,
                )
            data = "\n".join(data_lines)
            if data.strip() == "[DONE]":
                terminal_event = ModelEvent(
                    ModelEventKind.COMPLETION,
                    finish_reason=finish_reason,
                    response_id=response_id,
                    transport_attempts=attempts,
                )
            else:
                cls._parse_stream_chunk(data, response.status_code, attempts)
        if terminal_event is not None:
            yield terminal_event
            return
        raise ProviderError(
            ProviderErrorKind.TRANSPORT,
            "provider stream ended before [DONE]",
            status_code=response.status_code,
            transport_attempts=attempts,
        )

    @staticmethod
    def _parse_stream_chunk(
        data: str,
        status_code: int,
        attempts: int,
    ) -> tuple[list[ModelEvent], Optional[str], Optional[str]]:
        try:
            value = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise ProviderError(
                ProviderErrorKind.MALFORMED_RESPONSE,
                "provider SSE event contained invalid JSON",
                status_code=status_code,
            ) from exc

        if isinstance(value, dict) and "choices" not in value and isinstance(value.get("error"), dict):
            # LiteLLM/vLLM gateways report an overload, rate limit or quota
            # inside a 200 stream; it is classified like the same error sent
            # with a status code, not as a malformed answer.
            from .provider_adapter_base import stream_error_kind

            raise ProviderError(
                stream_error_kind(value["error"]),
                "provider reported an error inside the stream",
                status_code=status_code,
                transport_attempts=attempts,
            )
        try:
            if not isinstance(value, dict):
                raise ValueError("event root must be an object")
            response_id = value.get("id")
            if response_id is not None and not isinstance(response_id, str):
                raise ValueError("response ID must be text or null")

            events: list[ModelEvent] = []
            raw_usage = value.get("usage")
            if raw_usage is not None:
                if not isinstance(raw_usage, dict):
                    raise ValueError("usage must be an object")
                usage: Dict[str, int] = {}
                # prompt_tokens counts the whole prompt on this wire, but the
                # cached share of it bills at a tenth of the rate, so a cost
                # figure computed without it is simply wrong. Caching also
                # fails silently, and this count is the only evidence it worked.
                details = raw_usage.get("prompt_tokens_details")
                detail_source = details if isinstance(details, dict) else {}
                for name, source, target in (
                    ("prompt_tokens", raw_usage, "prompt_tokens"),
                    ("completion_tokens", raw_usage, "completion_tokens"),
                    ("total_tokens", raw_usage, "total_tokens"),
                ):
                    raw_count = source.get(name)
                    if raw_count is None:
                        # A gateway may spell an absent count as null. Usage is
                        # metadata; it must not invalidate a complete answer.
                        continue
                    usage[target] = _usage_count(raw_count)
                # Cache accounting has three spellings on this wire. The detail
                # object is OpenAI's; prompt_cache_hit_tokens is DeepSeek's
                # (the live W&B route), and cache_read_input_tokens appears on
                # some proxies. First present wins; counting two would inflate
                # the cached share and understate the cost.
                for name, source in (
                    ("cached_tokens", detail_source),
                    ("prompt_cache_hit_tokens", raw_usage),
                    ("cache_read_input_tokens", raw_usage),
                ):
                    raw_count = source.get(name)
                    if raw_count is None:
                        continue
                    usage["cache_read_tokens"] = _usage_count(raw_count)
                    break
                events.append(
                    ModelEvent(
                        ModelEventKind.USAGE,
                        usage=usage,
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
                )

            choices = value.get("choices")
            if choices is None and raw_usage is not None:
                # A usage-only terminal chunk may omit choices entirely; the
                # empty-choices path below already treats that as a keepalive.
                choices = []
            if not isinstance(choices, list):
                raise ValueError("choices must be an array")
            if not choices:
                # An empty-choices chunk with no usage is a gateway keepalive
                # frame, not a contract violation: some OpenAI-compatible
                # gateways emit them during long generations. The other
                # adapters tolerate unknown/empty events, so this one must not
                # kill an otherwise healthy stream either.
                return events, None, response_id

            choice = choices[0]
            if not isinstance(choice, dict):
                raise ValueError("choice must be an object")
            # OpenAI-compatible gateways are not perfectly uniform at the
            # terminal/tool-call boundaries. OpenRouter can emit ``delta: null``
            # on a finish-only chunk. That is an empty fragment, not a malformed
            # response. Non-null non-objects remain invalid so bad schemas still
            # fail closed.
            raw_delta = choice.get("delta")
            if raw_delta is None:
                delta: Dict[str, Any] = {}
            elif isinstance(raw_delta, dict):
                delta = raw_delta
            else:
                raise ValueError("choice delta must be an object or null")
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ValueError("content delta must be text or null")
                events.append(
                    ModelEvent(
                        ModelEventKind.TEXT_DELTA,
                        text_delta=content,
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
                )

            # Reasoning models on the OpenAI-compatible wire may put raw private
            # deliberation on a separate key. Preserve it on the private reasoning
            # channel for protocol/replay needs, but never merge it into content or
            # public progress; both spellings are used by compatible gateways.
            for name in ("reasoning_content", "reasoning"):
                thought = delta.get(name)
                if isinstance(thought, str) and thought:
                    events.append(
                        ModelEvent(
                            ModelEventKind.REASONING_DELTA,
                            reasoning_delta=thought,
                            response_id=response_id,
                            transport_attempts=attempts,
                        )
                    )
                    break

            # OpenRouter standardizes structured reasoning into reasoning_details.
            # Only the explicitly public ``reasoning.summary`` type may cross into
            # KaroX progress UI. Raw ``reasoning.text`` and encrypted details are
            # deliberately ignored here; the raw reasoning channel above remains
            # private and never becomes assistant/progress content.
            raw_reasoning_details = delta.get("reasoning_details")
            if raw_reasoning_details is not None:
                if not isinstance(raw_reasoning_details, list):
                    raise ValueError("reasoning_details must be an array or null")
                for detail in raw_reasoning_details:
                    if not isinstance(detail, dict):
                        raise ValueError("reasoning detail must be an object")
                    detail_type = detail.get("type")
                    if detail_type != "reasoning.summary":
                        continue
                    summary = detail.get("summary")
                    if summary is not None and not isinstance(summary, str):
                        raise ValueError("reasoning summary must be text or null")
                    if summary:
                        events.append(
                            ModelEvent(
                                ModelEventKind.REASONING_SUMMARY_DELTA,
                                reasoning_summary_delta=summary,
                                response_id=response_id,
                                transport_attempts=attempts,
                            )
                        )

            # Streaming tool calls are fragments. Only the first chunk is
            # required to carry id/name; later chunks commonly carry those fields
            # as null and append only ``arguments``. Treat null optional fields as
            # empty fragments, while preserving strict validation for genuinely
            # wrong types. The accumulated ToolCall still requires a non-empty
            # final id and name, so an actually incomplete stream is rejected at
            # assembly time.
            raw_calls = delta.get("tool_calls")
            if raw_calls is None:
                raw_calls = []
            if not isinstance(raw_calls, list):
                raise ValueError("tool-call deltas must be an array or null")
            for raw in raw_calls:
                if not isinstance(raw, dict):
                    raise ValueError("tool-call delta must be an object")
                index = raw.get("index")
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or index < 0
                ):
                    raise ValueError("tool-call delta requires a non-negative index")
                raw_type = raw.get("type")
                # Later fragments of one call carry the identity fields as empty
                # strings rather than null on some OpenAI-compatible endpoints
                # (measured on StepFun's Step Plan endpoint: the first chunk has
                # the id and type, the rest send {"id": "", "type": ""} while
                # appending arguments). An empty string is that same empty
                # fragment, not a different kind of tool call.
                if isinstance(raw_type, str) and not raw_type.strip():
                    raw_type = None
                if raw_type is not None and raw_type != "function":
                    raise ValueError("only function tool-call deltas are supported")
                raw_call_id = raw.get("id")
                if raw_call_id is not None and not isinstance(raw_call_id, str):
                    raise ValueError("tool-call ID must be text or null")
                raw_function = raw.get("function")
                if raw_function is None:
                    function: Dict[str, Any] = {}
                elif isinstance(raw_function, dict):
                    function = raw_function
                else:
                    raise ValueError("tool-call function must be an object or null")
                raw_name = function.get("name")
                raw_arguments = function.get("arguments")
                if raw_name is not None and not isinstance(raw_name, str):
                    raise ValueError("tool-call name must be text or null")
                if raw_arguments is not None and not isinstance(raw_arguments, str):
                    raise ValueError("tool-call arguments must be text or null")
                call_id = raw_call_id or ""
                name = raw_name or ""
                arguments = raw_arguments or ""
                events.append(
                    ModelEvent(
                        ModelEventKind.TOOL_CALL_DELTA,
                        tool_call_delta=ToolCallDelta(
                            index=index,
                            call_id_fragment=call_id,
                            name_fragment=name,
                            arguments_fragment=arguments,
                        ),
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
                )

            finish_reason = choice.get("finish_reason")
            if finish_reason is not None and not isinstance(finish_reason, str):
                raise ValueError("finish reason must be text or null")
            return events, finish_reason, response_id
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                ProviderErrorKind.MALFORMED_RESPONSE,
                f"invalid Chat Completions stream event: {exc}",
                status_code=status_code,
            ) from exc

    @staticmethod
    def _retry_after(raw_value: Optional[str]) -> Optional[float]:
        """Return the wait a provider asked for, in seconds.

        RFC 9110 allows ``Retry-After`` to carry either a delay in seconds or an
        HTTP date, and real gateways send both.  Reading only the number threw
        the date form away and left the router guessing its own backoff.
        """
        if not raw_value:
            return None
        try:
            seconds = float(raw_value)
        except ValueError:
            try:
                moment = parsedate_to_datetime(raw_value)
            except (TypeError, ValueError):
                return None
            if moment is None:
                return None
            if moment.tzinfo is None:
                # An HTTP date without a usable zone is UTC by definition.
                moment = moment.replace(tzinfo=timezone.utc)
            seconds = (moment - datetime.now(timezone.utc)).total_seconds()
        if not math.isfinite(seconds):
            return None
        return max(0.0, seconds)

    @classmethod
    def _rate_limit_reset(cls, headers: Mapping[str, str]) -> Optional[float]:
        """The wait a 429 implies when the provider sent no ``Retry-After``.

        OpenAI sends ``retry-after-ms`` and ``x-ratelimit-reset-{requests,tokens}``
        as Go durations (``6m0s``, ``250ms``); Anthropic sends
        ``anthropic-ratelimit-*-reset`` as RFC 3339 times. The reset of a limit
        that is exhausted (remaining 0) is the one that matters; without
        remaining counts the longest reset is the safe reading.
        """

        lowered = {str(key).lower(): str(value) for key, value in headers.items()}
        raw_ms = lowered.get("retry-after-ms")
        if raw_ms:
            try:
                value = float(raw_ms) / 1000.0
            except ValueError:
                value = float("nan")
            if math.isfinite(value) and value >= 0:
                return value
        resets: list[tuple[float, bool]] = []
        for key, value in lowered.items():
            if key.startswith("x-ratelimit-reset-"):
                seconds = _go_duration_seconds(value)
                limit = key[len("x-ratelimit-reset-"):]
                remaining = lowered.get("x-ratelimit-remaining-" + limit)
            elif key.startswith("anthropic-ratelimit-") and key.endswith("-reset"):
                seconds = cls._retry_after(value) if not _looks_numeric(value) else None
                if seconds is None:
                    try:
                        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if moment.tzinfo is None:
                        moment = moment.replace(tzinfo=timezone.utc)
                    seconds = (moment - datetime.now(timezone.utc)).total_seconds()
                limit = key[len("anthropic-ratelimit-"):-len("-reset")]
                remaining = lowered.get(f"anthropic-ratelimit-{limit}-remaining")
            else:
                continue
            if seconds is None or not math.isfinite(seconds):
                continue
            resets.append((max(0.0, seconds), remaining is not None and remaining.strip() == "0"))
        if not resets:
            return None
        exhausted = [seconds for seconds, empty in resets if empty]
        return max(exhausted or [seconds for seconds, _ in resets])

    @classmethod
    def _http_error(cls, response: httpx.Response) -> ProviderError:
        status = response.status_code
        if status == 401:
            kind = ProviderErrorKind.AUTHENTICATION
        elif status == 402:
            kind = ProviderErrorKind.BUDGET_EXCEEDED
        elif status == 403:
            kind = ProviderErrorKind.PERMISSION
        elif status == 429:
            kind = ProviderErrorKind.RATE_LIMIT
        elif status in {404, 410}:
            kind = ProviderErrorKind.MODEL_UNAVAILABLE
        elif status in {400, 405, 409, 415, 422}:
            kind = ProviderErrorKind.INVALID_REQUEST
        elif status >= 500 or status == 408:  # 408: the gateway timed out waiting upstream
            kind = ProviderErrorKind.PROVIDER_INTERNAL
        else:
            kind = ProviderErrorKind.INVALID_REQUEST
        retry_after = cls._retry_after(response.headers.get("Retry-After"))
        if retry_after is None and status == 429:
            retry_after = cls._rate_limit_reset(response.headers)
        detail = response.reason_phrase or "provider request failed"
        codes = _vendor_error_codes(response)
        if status == 402 and "insufficient_quota" in codes:  # a gateway quota throttle: "not now" (W&B, HARD12 tb1)
            kind = ProviderErrorKind.RATE_LIMIT
        if codes:
            detail = f"{detail} ({', '.join(codes)})"
        return ProviderError(
            kind,
            f"HTTP {status}: {detail}",
            status_code=status,
            retry_after=retry_after,
        )
