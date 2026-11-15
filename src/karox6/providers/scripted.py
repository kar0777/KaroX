"""Deterministic provider for tests and offline scenarios: replays scripted turns.

A script is a list of turns; each turn is either a final text answer or a list of tool
calls. ``requests`` keeps every request the session sent so tests can assert on
exactly what the model would have seen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from karox6.core.messages import Message, ToolCall, Usage
from karox6.providers.base import DeltaCallback, ModelRequest, ModelResponse, ProviderError


@dataclass(slots=True)
class Turn:
    text: str = ""
    calls: list[tuple[str, dict[str, Any] | str]] = field(default_factory=list)
    finish_reason: str | None = None
    reasoning: str | None = None
    error: ProviderError | None = None
    usage: Usage | None = None


@dataclass(slots=True)
class ScriptedProvider:
    turns: list[Turn]
    name: str = "scripted"
    requests: list[ModelRequest] = field(default_factory=list)
    _index: int = 0
    _call_seq: int = 0

    async def complete(
        self, request: ModelRequest, on_delta: DeltaCallback | None = None
    ) -> ModelResponse:
        self.requests.append(request)
        if self._index >= len(self.turns):
            turn = Turn(text="(script exhausted)")
        else:
            turn = self.turns[self._index]
            self._index += 1
        if turn.error is not None:
            raise turn.error
        calls = []
        for name, args in turn.calls:
            self._call_seq += 1
            raw = args if isinstance(args, str) else json.dumps(args)
            calls.append(ToolCall(id=f"call_{self._call_seq}", name=name, arguments=raw))
        finish = turn.finish_reason or ("tool_calls" if calls else "stop")
        message = Message("assistant", turn.text, tool_calls=calls, reasoning=turn.reasoning)
        usage = turn.usage or Usage(
            input_tokens=1000 + 100 * len(self.requests), output_tokens=50, requests=1
        )
        return ModelResponse(message=message, usage=usage, finish_reason=finish, model="scripted")

    async def aclose(self) -> None:
        return None
