"""The engine's Model protocol on top of KaroX's provider layer.

One adapter: :class:`EngineProviderModel` turns a provider response into the
single typed action the vendored loop expects, and keeps the loop's recovery
ports (format-error budget, output-limit nudge, fenced-command salvage, prose
final answer) intact. Split out of ``engine_kernel.py`` so the bridge can be
read without the session/verification machinery around it.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import replace
from types import SimpleNamespace

# The vendored engine prints a startup banner unless told not to; a product
# import is silent even when this module is imported first.
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from jinja2 import StrictUndefined, Template

from .engine.exceptions import FormatError  # noqa: E402
from .engine_salvage import salvage_fenced_command
from .providers import (
    ImageAttachment,
    ModelEvent,
    ModelEventKind,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderErrorKind,
    ProviderTool,
    ReasoningBlock,
    ToolCall,
    accumulate_response,
)

logger = logging.getLogger("karox.engine_provider_bridge")


#: Failures that say "not now" rather than "never": a 429 concurrency or quota
#: refusal, a 5xx, and a transport failure (refused connection, a stream the
#: gateway cut, a read that hung past its deadline). Before this, one of them
#: ended the whole run as ``provider_error:*`` -- four arena runs died that way
#: with minutes of budget left -- because the direct ``--model/--base-url``
#: route has no router behind it to retry. A rejected payload, an
#: authentication failure or a missing model repeat identically and still end
#: the run at once.
_RETRYABLE_PROVIDER_ERRORS = frozenset(
    {
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.PROVIDER_INTERNAL,
        ProviderErrorKind.TRANSPORT,
    }
)
#: Retries per model call. With the delays below a call waits at most about
#: two minutes in total before the failure is reported.
_PROVIDER_RETRY_ATTEMPTS = 6
_PROVIDER_RETRY_BASE_SECONDS = 2.0
_PROVIDER_RETRY_MAX_SECONDS = 30.0
#: A retry is only worth sending when the call still has this long to run
#: after the wait; otherwise the run is better served by reporting the failure
#: while it can still record its proof.
_PROVIDER_RETRY_MIN_WINDOW_SECONDS = 20.0


def provider_retry_delay(
    error: BaseException,
    attempt: int,
    window_seconds: float,
    *,
    jitter: float = 0.0,
) -> Optional[float]:
    """Return the wait before re-sending one failed model call, or None.

    ``attempt`` counts the retries already made plus one. A provider's
    ``Retry-After`` is the starting point when it sent one, capped so a long
    quota reset cannot park the run; otherwise the wait doubles from
    ``_PROVIDER_RETRY_BASE_SECONDS``. Jitter is only ever added, so parallel
    runs that were refused together do not return together.
    """

    if not isinstance(error, ProviderError):
        return None
    if error.kind not in _RETRYABLE_PROVIDER_ERRORS:
        return None
    if attempt > _PROVIDER_RETRY_ATTEMPTS:
        return None
    asked = error.retry_after
    if (
        isinstance(asked, (int, float))
        and not isinstance(asked, bool)
        and asked == asked
        and asked > 0
    ):
        base = float(asked)
    else:
        base = _PROVIDER_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
    spread = min(max(float(jitter), 0.0), 1.0)
    delay = min(_PROVIDER_RETRY_MAX_SECONDS, base) * (1.0 + 0.25 * spread)
    if delay + _PROVIDER_RETRY_MIN_WINDOW_SECONDS > float(window_seconds):
        return None
    return delay


#: The provider-safe name of the engine's submission tool. It is not a Core
#: command: it mutates nothing, it is the typed-tool equivalent of the stock
#: engine's ``echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`` marker.
FINISH_TOOL_NAME = "finish"

#: A whole-document write carries the entire file in its arguments; the content
#: is on disk and the tool result names the path, the size and the diff. An
#: `edit_file` call is different: its old/new strings are the anchors the model
#: reuses in the next edits. Eliding those made the aquarium run re-read the
#: file 59 times instead of 14 and finish in 71 steps instead of 48, which cost
#: more in total than the arguments it saved.
_ELIDABLE_ARGUMENT_TOOLS = frozenset({"repo_write_file", "repo_apply_patch"})
_ELIDED_ARGUMENT_CHARS = 2_000
#: Keep the arguments of the most recent large completed calls so a model that
#: continues a chunked write can still see its own seam. Older calls are the
#: ones whose carried cost matters. Small calls (reads, checks) never take
#: these slots.
_ELIDED_ARGUMENT_KEEP = 2


def _elided_argument_marker(name: str) -> str:
    # Valid JSON on purpose: an OpenAI-compatible gateway rejects a tool call
    # whose arguments are not a JSON object (the aquarium run's step-31 400).
    return json.dumps(
        {
            "_karo_context_manager": (
                f"{name} arguments elided after the call ran; "
                "the tool result carries the outcome"
            )
        },
        ensure_ascii=False,
    )


def _completed_calls(
    messages: Sequence[Mapping[str, Any]],
) -> List[tuple[str, str, int]]:
    """Successful call ids, names and argument sizes, in completion order.

    ``finish`` is excluded: its arguments are the deliverable, and it never has
    a next request to save.
    """

    names: Dict[str, str] = {}
    sizes: Dict[str, int] = {}
    for entry in messages:
        if entry.get("role") != "assistant":
            continue
        for item in entry.get("tool_calls") or []:
            if isinstance(item, Mapping):
                call_id = str(item.get("call_id") or "")
                if call_id:
                    names[call_id] = str(item.get("name") or "")
                    sizes[call_id] = len(str(item.get("raw_arguments") or ""))
    completed: List[tuple[str, str, int]] = []
    for entry in messages:
        if entry.get("role") != "tool":
            continue
        call_id = str(entry.get("tool_call_id") or "")
        if (
            call_id
            and names.get(call_id) != FINISH_TOOL_NAME
            and "<returncode>0</returncode>" in str(entry.get("content") or "")
        ):
            completed.append(
                (call_id, names.get(call_id, ""), sizes.get(call_id, 0))
            )
    return completed


def _elide_applied_arguments(
    call: ToolCall, *, completed: Set[str], keep: Set[str]
) -> ToolCall:
    if (
        call.call_id in completed
        and call.call_id not in keep
        and call.name in _ELIDABLE_ARGUMENT_TOOLS
        and len(call.raw_arguments) > _ELIDED_ARGUMENT_CHARS
    ):
        return ToolCall(
            call_id=call.call_id,
            name=call.name,
            raw_arguments=_elided_argument_marker(call.name),
        )
    return call


#: The typed-tool restatement of the engine's format-error nudge. The
#: output-limit branch is the engine's own text so a truncated response gets
#: the same recovery it had in the race.
_FORMAT_ERROR_TEMPLATE = (
    '{% if finish_reason is defined and finish_reason in ["length", "tool_calls"] -%}\n'
    "Your previous response reached the output token limit "
    "(finish_reason={{ finish_reason }}) before you produced a complete tool "
    "call, so it was cut off. Respond more concisely and emit exactly one tool "
    "call for the next action.\n"
    "{%- else -%}\n"
    "Format error:\n\n"
    "<error>\n{{ error }}\n</error>\n\n"
    "Every response must carry its action as a tool call (exactly one preferred). "
    "When the task is finished, call the finish tool with the final answer. Do "
    "not answer in prose without a tool call; prose is only accepted as the "
    "final answer after repeated format errors.\n"
    "{%- endif %}"
)


def _engine_reasoning_blocks(entry: Mapping[str, Any]) -> Tuple[ReasoningBlock, ...]:
    stored = entry.get("reasoning_blocks")
    if not isinstance(stored, list):
        return ()
    blocks: List[ReasoningBlock] = []
    for item in stored:
        if not isinstance(item, Mapping):
            continue
        try:
            blocks.append(
                ReasoningBlock(
                    kind=str(item.get("kind")),
                    text=str(item.get("text") or ""),
                    data=str(item.get("data") or ""),
                    signature=str(item.get("signature") or ""),
                )
            )
        except ValueError:
            continue
    return tuple(blocks)


def _engine_tool_calls(entry: Mapping[str, Any]) -> Tuple[ToolCall, ...]:
    raw = entry.get("tool_calls")
    if not isinstance(raw, list):
        return ()
    calls: List[ToolCall] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        call_id = str(item.get("call_id", ""))
        name = str(item.get("name", ""))
        arguments = str(item.get("raw_arguments", ""))
        if not call_id or not name:
            continue
        try:
            calls.append(ToolCall(call_id=call_id, name=name, raw_arguments=arguments))
        except ValueError:
            continue
    return tuple(calls)


def _response_from_engine_message(message: Mapping[str, Any]) -> ModelResponse:
    """Rebuild the ModelResponse an engine assistant message was built from.

    The bridge persists the wire facts (provider, model, usage, finish reason)
    on the message so session accounting can run without keeping the response
    object alive.
    """
    extra = message.get("extra") or {}
    usage = extra.get("usage") if isinstance(extra.get("usage"), dict) else {}
    cost = extra.get("cost")
    cumulative_cost = extra.get("cumulative_cost")
    reason = extra.get("cost_unknown_reason")
    currency = extra.get("currency")
    pricing_version = extra.get("pricing_version")
    return ModelResponse(
        content=message.get("content") if isinstance(message.get("content"), str) else None,
        tool_calls=_engine_tool_calls(message),
        finish_reason=extra.get("finish_reason") if isinstance(extra.get("finish_reason"), str) else None,
        usage={str(key): value for key, value in usage.items()},
        response_id=extra.get("response_id") if isinstance(extra.get("response_id"), str) else None,
        transport_attempts=int(extra.get("transport_attempts") or 1),
        selected_provider=extra.get("provider") if isinstance(extra.get("provider"), str) else None,
        selected_model=extra.get("model") if isinstance(extra.get("model"), str) else None,
        cost=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
        cost_unknown_reason=reason if isinstance(reason, str) and reason else None,
        cumulative_cost=(
            float(cumulative_cost)
            if isinstance(cumulative_cost, (int, float))
            and not isinstance(cumulative_cost, bool)
            else None
        ),
        uncached_cost=(
            float(extra.get("uncached_cost"))
            if isinstance(extra.get("uncached_cost"), (int, float))
            and not isinstance(extra.get("uncached_cost"), bool)
            else None
        ),
        cache_savings=(
            float(extra.get("cache_savings"))
            if isinstance(extra.get("cache_savings"), (int, float))
            and not isinstance(extra.get("cache_savings"), bool)
            else None
        ),
        currency=currency if isinstance(currency, str) and currency else None,
        pricing_version=(
            pricing_version if isinstance(pricing_version, str) and pricing_version else None
        ),
    )


class EngineProviderModel:
    """The engine's Model protocol on KaroX's provider layer.

    ``query`` builds one :class:`ModelRequest` from the engine's message list,
    streams it when the provider can (emitting text/reasoning deltas as
    progress events) and folds the stream into the response the engine
    consumes. Tool calls become the engine's typed actions; everything else
    becomes the engine's ``FormatError`` recovery path with the response
    persisted in the shape the engine's finish contract reads.
    """

    def __init__(
        self,
        *,
        provider: Any,
        model_id: str,
        tools: Tuple[ProviderTool, ...] = (),
        max_output_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        event: Optional[Callable[..., None]] = None,
        deadline_seconds: float | Callable[[], float] = 120.0,
        images: Tuple[ImageAttachment, ...] = (),
        format_error_template: str = _FORMAT_ERROR_TEMPLATE,
        known_names: Optional[Set[str]] = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._provider = provider
        self._sleep = sleep
        self._jitter = jitter
        #: Retried provider failures of this run, newest last (bounded).
        self.provider_retries: List[Dict[str, Any]] = []
        self._model_id = model_id
        self._tools = tools
        # What the model may call: every resolvable Core alias plus finish,
        # not only what this origin is permitted. A call to an unadvertised
        # but real tool reaches Core and is recorded as a policy denial; only
        # a name nothing can resolve is a format error.
        self._known_names = set(known_names) if known_names is not None else {
            tool.name for tool in tools
        } | {FINISH_TOOL_NAME}
        self._max_output_tokens = max_output_tokens
        self._reasoning_effort = reasoning_effort
        self._event = event
        self._deadline_seconds = deadline_seconds
        self._images = images
        self._format_error_template = format_error_template
        self.config = SimpleNamespace(model_name=model_id, output_char_limit=50_000)
        #: The engine step this call belongs to; the agent keeps it current.
        self.step = 0

    # -- Model protocol ---------------------------------------------------

    def query(self, messages: List[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        request = ModelRequest(
            model=self._model_id,
            messages=self._request_messages(messages),
            tools=self._tools,
            max_output_tokens=self._max_output_tokens,
            reasoning_effort=self._reasoning_effort,
            deadline_seconds=self._current_deadline(),
        )
        response = self._call(request)
        if response.budget_exceeded:
            message = self._engine_message(response, [])
            message["extra"]["budget_exceeded"] = True
            message["extra"]["budget_reason"] = response.budget_reason or "budget"
            return message
        actions = self._parse_actions(response)
        return self._engine_message(response, actions)

    def format_message(self, **kwargs: Any) -> Dict[str, Any]:
        role = str(kwargs.get("role", "user"))
        content = kwargs.get("content")
        return {"role": role, "content": content if content is not None else ""}

    def format_observation_messages(
        self,
        message: Mapping[str, Any],
        outputs: Sequence[Mapping[str, Any]],
        template_vars: Optional[Mapping[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        del template_vars
        actions = (message.get("extra") or {}).get("actions") or []
        not_executed: Dict[str, Any] = {
            "output": "",
            "returncode": -1,
            "exception_info": "action was not executed",
        }
        padded: List[Mapping[str, Any]] = list(outputs) + [
            not_executed
        ] * (len(actions) - len(outputs))
        results: List[Dict[str, Any]] = []
        for action, output in zip(actions, padded):
            body = str(output.get("observation") or output.get("output") or "")
            exception = str(output.get("exception_info") or "")
            text = ""
            if exception:
                text += f"<exception>{exception}</exception>\n"
            text += f"<returncode>{output.get('returncode')}</returncode>\n<output>\n{body}</output>"
            tool_call_id = action.get("tool_call_id")
            if tool_call_id:
                results.append(
                    {"role": "tool", "content": text, "tool_call_id": str(tool_call_id)}
                )
            else:
                results.append({"role": "user", "content": text})
        return results

    def get_template_vars(self, **kwargs: Any) -> Dict[str, Any]:
        return {"model_name": self._model_id, **kwargs}

    def serialize(self) -> Dict[str, Any]:
        return {
            "info": {
                "config": {
                    "model": {
                        "model_name": self._model_id,
                        "model_type": f"{type(self).__module__}.{type(self).__name__}",
                    },
                }
            }
        }

    # -- internals --------------------------------------------------------

    def _request_messages(self, messages: Iterable[Mapping[str, Any]]) -> Tuple[ModelMessage, ...]:
        entries = list(messages)
        completed_calls = _completed_calls(entries)
        completed = {call_id for call_id, _, _ in completed_calls}
        large = [
            call_id
            for call_id, name, size in completed_calls
            if name in _ELIDABLE_ARGUMENT_TOOLS and size > _ELIDED_ARGUMENT_CHARS
        ]
        keep = set(large[-_ELIDED_ARGUMENT_KEEP:])
        first_user_seen = False
        converted: List[ModelMessage] = []
        for entry in entries:
            role = str(entry.get("role", "user"))
            if role == "exit":
                continue
            content = entry.get("content")
            text = None if content is None else str(content)
            if role == "assistant":
                calls = tuple(
                    _elide_applied_arguments(call, completed=completed, keep=keep)
                    for call in _engine_tool_calls(entry)
                )
                replayed = entry.get("reasoning_content")
                converted.append(
                    ModelMessage(
                        "assistant",
                        text,
                        tool_calls=calls,
                        reasoning_blocks=_engine_reasoning_blocks(entry),
                        reasoning_text=(
                            replayed if isinstance(replayed, str) and replayed else None
                        ),
                    )
                )
                continue
            if role == "tool":
                call_id = str(entry.get("tool_call_id", ""))
                if not call_id:
                    continue
                converted.append(ModelMessage("tool", text, tool_call_id=call_id))
                continue
            images: Tuple[ImageAttachment, ...] = ()
            if role == "user" and self._images and not first_user_seen:
                images = self._images
            first_user_seen = first_user_seen or role == "user"
            converted.append(ModelMessage(role, text, images=images))
        if not converted:
            converted.append(ModelMessage("user", ""))
        return tuple(converted)

    def _current_deadline(self) -> float:
        """The time one model call may take, read when the call starts.

        The kernel passes a callable so the cap shrinks with the time the run
        has left; a fixed number taken at construction let a call started at
        the end of the budget run its full cap past the run's own deadline.
        """

        value = self._deadline_seconds
        if callable(value):
            value = value()
        # ModelRequest accepts 0.05..3600 s; clamp here rather than fail the
        # request construction for a caller with a long fixed deadline.
        return min(3600.0, max(0.05, float(value)))

    def _call(self, request: ModelRequest) -> ModelResponse:
        attempt = 0
        while True:
            try:
                return self._call_once(request)
            except ProviderError as exc:
                attempt += 1
                window = self._current_deadline()
                try:
                    spread = float(self._jitter())
                except Exception:
                    spread = 0.0
                delay = provider_retry_delay(exc, attempt, window, jitter=spread)
                if delay is None:
                    raise
                record = {
                    "attempt": attempt,
                    "kind": exc.kind.value,
                    "status_code": exc.status_code,
                    "delay_seconds": round(delay, 2),
                    "error": str(exc)[:200],
                }
                self.provider_retries = (self.provider_retries + [record])[-50:]
                logger.warning(
                    "model call failed (%s); retry %d in %.1fs",
                    exc.kind.value,
                    attempt,
                    delay,
                )
                if self._event is not None:
                    try:
                        self._event(
                            "warning",
                            reason="provider_retry",
                            detail=(
                                f"model call failed ({exc.kind.value}"
                                + (f", HTTP {exc.status_code}" if exc.status_code else "")
                                + f"); retry {attempt} in {delay:.1f}s"
                            ),
                        )
                    except Exception:
                        logger.debug("dropping one provider_retry event", exc_info=True)
                self._sleep(delay)
                request = replace(request, deadline_seconds=self._current_deadline())

    def _call_once(self, request: ModelRequest) -> ModelResponse:
        stream = getattr(self._provider, "stream", None)
        if callable(stream):
            try:
                return accumulate_response(stream(request), observer=self._observe)
            except (NotImplementedError, AttributeError, TypeError):
                pass
        return self._provider.complete(request)

    def _observe(self, event: ModelEvent) -> None:
        if self._event is None:
            return
        try:
            if event.kind is ModelEventKind.TEXT_DELTA:
                self._event("text_delta", text_delta=event.text_delta)
            elif event.kind is ModelEventKind.REASONING_DELTA:
                self._event("reasoning_delta", reasoning_delta=event.reasoning_delta)
            elif event.kind is ModelEventKind.REASONING_SUMMARY_DELTA:
                self._event(
                    "reasoning_summary_delta",
                    reasoning_summary_delta=event.reasoning_summary_delta,
                )
        except Exception:
            # An observer is a display concern and may never end the run. One
            # malformed event must not mute the rest of the stream either.
            logger.debug("dropping one unrenderable model event", exc_info=True)

    def _parse_actions(self, response: ModelResponse) -> List[Dict[str, Any]]:
        calls = response.tool_calls
        if calls:
            actions: List[Dict[str, Any]] = []
            problems = 0
            for call in calls:
                error = ""
                arguments: Any = None
                try:
                    arguments = json.loads(call.raw_arguments or "")
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    error = f"Error parsing tool call arguments: {exc}."
                if call.name not in self._known_names:
                    error += f" Unknown tool '{call.name}'."
                if not isinstance(arguments, dict):
                    error += " Tool call arguments must be a JSON object."
                if error:
                    problems += 1
                    # A batch is independent by contract: one unparseable call
                    # must not discard the calls beside it, so the bad call
                    # becomes an observation the model can act on while the
                    # valid calls still run, in order. A lone bad call keeps
                    # the format-error recovery: there is nothing to preserve
                    # and the nudge states the mistake more clearly.
                    actions.append(
                        {
                            "name": call.name,
                            "arguments": {},
                            "tool_call_id": call.call_id,
                            "parse_error": error.strip(),
                        }
                    )
                    continue
                actions.append(
                    {"name": call.name, "arguments": arguments, "tool_call_id": call.call_id}
                )
            if problems and len(calls) == 1:
                raise self._format_error(
                    str(actions[0].get("parse_error") or ""), response, has_calls=True
                )
            size = len(actions)
            if size > 1:
                for index, action in enumerate(actions):
                    # The environment needs to know where a response's batch
                    # ends: an advisory test rerun after every edit in a
                    # three-edit response runs the same suite three times and
                    # leaves two stale red results in the model's next turn.
                    # A lone call carries no marker; it is trivially the tail.
                    action["batch_index"] = index
                    action["batch_size"] = size
            return actions
        salvaged = salvage_fenced_command(response.content or "")
        if salvaged is not None:
            # karo-core port 5: the model wrote the action into a fence. The
            # environment decides what a shell text can safely mean here.
            return [{"command": salvaged}]
        raise self._format_error(
            "No tool call found in the response.", response, has_calls=False
        )

    def _format_error(
        self, error: str, response: ModelResponse, *, has_calls: bool
    ) -> FormatError:
        content = Template(self._format_error_template, undefined=StrictUndefined).render(
            error=error,
            actions=[],
            has_tool_calls=has_calls,
            finish_reason=response.finish_reason,
        )
        return FormatError(
            {
                "role": "user",
                "content": content,
                "extra": {
                    "interrupt_type": "FormatError",
                    "response": self._persisted_response(response),
                    "cost": response.cost,
                    "cost_unknown_reason": response.cost_unknown_reason,
                    "cumulative_cost": response.cumulative_cost,
                    "uncached_cost": response.uncached_cost,
                    "cache_savings": response.cache_savings,
                    "currency": response.currency,
                    "pricing_version": response.pricing_version,
                    "usage": dict(response.usage),
                    "provider": response.selected_provider
                    or getattr(self._provider, "provider_name", ""),
                    "model": response.selected_model or self._model_id,
                },
            }
        )

    def _persisted_response(self, response: ModelResponse) -> Dict[str, Any]:
        """The OpenAI-shaped response the engine's finish contract reads.

        ``DefaultAgent._final_answer_text`` accepts a prose answer only when
        ``finish_reason == "stop"`` and the text is non-empty and fence-free;
        persisting the same shape here keeps that port honest through this
        bridge.
        """
        return {
            "choices": [
                {
                    "finish_reason": response.finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": [
                            {
                                "id": call.call_id,
                                "function": {
                                    "name": call.name,
                                    "arguments": call.raw_arguments,
                                },
                            }
                            for call in response.tool_calls
                        ],
                    },
                }
            ],
            "usage": dict(response.usage),
        }

    def _engine_message(
        self, response: ModelResponse, actions: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        message: Dict[str, Any] = {"role": "assistant", "content": response.content}
        if response.tool_calls:
            message["tool_calls"] = [
                {
                    "call_id": call.call_id,
                    "name": call.name,
                    "raw_arguments": call.raw_arguments,
                }
                for call in response.tool_calls
            ]
        if response.reasoning_blocks:
            message["reasoning_blocks"] = [
                {
                    "kind": block.kind,
                    "text": block.text,
                    "data": block.data,
                    "signature": block.signature,
                }
                for block in response.reasoning_blocks
            ]
        elif response.reasoning:
            # Flat deliberation from a Chat Completions gateway. Signed blocks
            # above carry their own replay; this is the unsigned form that a
            # thinking model expects back on the turns of its tool chain.
            message["reasoning_content"] = response.reasoning
        message["extra"] = {
            "actions": actions,
            "response": self._persisted_response(response),
            "cost": response.cost,
            "cost_unknown_reason": response.cost_unknown_reason,
            "cumulative_cost": response.cumulative_cost,
            "uncached_cost": response.uncached_cost,
            "cache_savings": response.cache_savings,
            "currency": response.currency,
            "pricing_version": response.pricing_version,
            "usage": dict(response.usage),
            "timestamp": time.time(),
            "provider": response.selected_provider
            or getattr(self._provider, "provider_name", ""),
            "model": response.selected_model or self._model_id,
            "finish_reason": response.finish_reason,
            "response_id": response.response_id,
            "transport_attempts": response.transport_attempts,
        }
        return message


