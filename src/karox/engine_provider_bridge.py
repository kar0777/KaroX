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
import time
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
    ProviderTool,
    ReasoningBlock,
    ToolCall,
    accumulate_response,
)

logger = logging.getLogger("karox.engine_provider_bridge")


#: The provider-safe name of the engine's submission tool. It is not a Core
#: command: it mutates nothing, it is the typed-tool equivalent of the stock
#: engine's ``echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`` marker.
FINISH_TOOL_NAME = "finish"

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
    return ModelResponse(
        content=message.get("content") if isinstance(message.get("content"), str) else None,
        tool_calls=_engine_tool_calls(message),
        finish_reason=extra.get("finish_reason") if isinstance(extra.get("finish_reason"), str) else None,
        usage={str(key): value for key, value in usage.items()},
        response_id=extra.get("response_id") if isinstance(extra.get("response_id"), str) else None,
        transport_attempts=int(extra.get("transport_attempts") or 1),
        selected_provider=extra.get("provider") if isinstance(extra.get("provider"), str) else None,
        selected_model=extra.get("model") if isinstance(extra.get("model"), str) else None,
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
        deadline_seconds: float = 120.0,
        images: Tuple[ImageAttachment, ...] = (),
        format_error_template: str = _FORMAT_ERROR_TEMPLATE,
        known_names: Optional[Set[str]] = None,
    ) -> None:
        self._provider = provider
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
            deadline_seconds=self._deadline_seconds,
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
        first_user_seen = False
        converted: List[ModelMessage] = []
        for entry in messages:
            role = str(entry.get("role", "user"))
            if role == "exit":
                continue
            content = entry.get("content")
            text = None if content is None else str(content)
            if role == "assistant":
                converted.append(
                    ModelMessage(
                        "assistant",
                        text,
                        tool_calls=_engine_tool_calls(entry),
                        reasoning_blocks=_engine_reasoning_blocks(entry),
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

    def _call(self, request: ModelRequest) -> ModelResponse:
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
                    "cost": response.cost or 0.0,
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
        message["extra"] = {
            "actions": actions,
            "response": self._persisted_response(response),
            "cost": response.cost or 0.0,
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


