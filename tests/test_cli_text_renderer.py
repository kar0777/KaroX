"""Headless text output keeps the reply and the tool lines apart when both streams share a pipe."""

from __future__ import annotations

import io
import sys
from typing import Any

from karox6.cli import _TextRenderer
from karox6.core.events import AssistantMessage, TextDelta, ToolStarted


class _Pipe:
    """One pipe behind stdout (buffered until flush) and stderr (written at once), like `2>&1 | cat`."""

    def __init__(self) -> None:
        self.out = ""

    def stream(self, buffered: bool) -> Any:
        pipe = self

        class _Stream(io.StringIO):
            def write(self, text: str) -> int:
                if buffered:
                    return super().write(text)
                pipe.out += text
                return len(text)

            def flush(self) -> None:
                pipe.out += self.getvalue()
                self.seek(0)
                self.truncate()

        return _Stream()


def test_tool_line_starts_on_its_own_line(monkeypatch: Any) -> None:
    pipe = _Pipe()
    monkeypatch.setattr(sys, "stdout", pipe.stream(buffered=True))
    monkeypatch.setattr(sys, "stderr", pipe.stream(buffered=False))
    render = _TextRenderer()
    render(TextDelta(text="Clear bug."))
    render(AssistantMessage(step=1, text="Clear bug."))
    render(ToolStarted(call_id="1", name="edit", arguments='{"file_path": "calc.py"}'))
    sys.stdout.flush()
    assert "Clear bug.\n> edit calc.py\n" in pipe.out
