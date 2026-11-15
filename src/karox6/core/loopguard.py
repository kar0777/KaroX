"""Repeat-call guard: the only mid-run intervention besides limits and format errors.

Port of DSH's repeat-tool-reminder (MIT, see THIRD_PARTY_NOTICES.md): a run of
consecutive identical calls (same tool, same canonical arguments) gets an advisory
reminder at 3, 5 and 8 repeats. KaroX adds one hard stop (default 20) so a stuck
model cannot burn the whole budget.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

FIRST_REMINDER = (
    "You are repeating the exact same tool call with identical arguments. Carefully analyze the "
    "previous result before calling again: if the task is not complete, try a different approach "
    "or different arguments instead of repeating the call."
)
REPEAT_REMINDER = (
    "Repeated tool call detected:\n- tool: {tool}\n- consecutive_calls: {count}\n"
    "- arguments: {arguments}\nThe repeated calls are not making progress. Do not call this tool "
    "with these exact arguments again. Inspect the latest result and choose a different action, "
    "different arguments, or finish the task if enough evidence has been gathered."
)
PREVIEW_CHARS = 500


def canonical_arguments(raw: str) -> str:
    try:
        value: Any = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return raw.strip()
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(slots=True)
class LoopGuard:
    thresholds: tuple[int, ...] = (3, 5, 8)
    hard_stop: int = 20
    _key: tuple[str, str] | None = None
    _count: int = 0
    _pending: list[str] = field(default_factory=list)

    def observe(self, tool: str, raw_arguments: str) -> bool:
        """Record one executed call. Returns True when the hard stop is reached."""
        canonical = canonical_arguments(raw_arguments)
        key = (tool, canonical)
        if key == self._key:
            self._count += 1
        else:
            self._key, self._count = key, 1
        if self._count == self.thresholds[0]:
            self._pending.append(FIRST_REMINDER)
        elif self._count in self.thresholds[1:]:
            preview = canonical
            if len(preview) > PREVIEW_CHARS:
                extra = len(preview) - PREVIEW_CHARS
                preview = preview[:PREVIEW_CHARS] + f"… (+{extra} more chars)"
            self._pending.append(
                REPEAT_REMINDER.format(tool=tool, count=self._count, arguments=preview)
            )
        return self.hard_stop > 0 and self._count >= self.hard_stop

    def take_reminders(self) -> list[str]:
        out, self._pending = self._pending, []
        return out

    def reset(self) -> None:
        self._key, self._count = None, 0
        self._pending.clear()
