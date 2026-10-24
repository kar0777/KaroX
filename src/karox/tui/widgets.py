"""The seven widgets the compact client is made of.

Each one owns exactly one kind of line on screen: chrome, a user turn, a
streaming answer, a tool observation, a notice, the composer, the footer. There
is no second presentation path, so a fact cannot render two different ways.
"""

from __future__ import annotations

import time
from typing import Any

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Markdown, Static, TextArea

GOLD = "#d6b36a"
MUTED = "#8d96a5"
TEXT = "#d8dce3"
GREEN = "#7fb08a"
RED = "#d97b7b"
BLUE = "#7fa6c9"
DIM = "#6b7381"


def _clip(value: str, limit: int) -> str:
    flat = " ".join(value.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class HeaderBar(Static):
    """One line: what this window is attached to."""

    def on_mount(self) -> None:
        self.update(Text("KaroX", style=f"bold {GOLD}"))

    def update_state(
        self,
        *,
        repository: str,
        branch: str,
        model: str,
        session: str,
        tokens: int,
        running: bool,
    ) -> None:
        pieces: list[tuple[str, str]] = [("KaroX", f"bold {GOLD}"), ("  ·  ", DIM)]
        pieces.append((repository or "?", f"bold {TEXT}"))
        if branch:
            pieces.append(("  ·  ", DIM))
            pieces.append((branch, MUTED))
        pieces.append(("  ·  ", DIM))
        pieces.append((model or "no model", BLUE if model else RED))
        if session:
            pieces.append(("  ·  ", DIM))
            pieces.append((session, MUTED))
        if tokens:
            pieces.append(("  ·  ", DIM))
            pieces.append((_token_text(tokens), MUTED))
        if running:
            pieces.append(("  ·  ", DIM))
            pieces.append(("● live", GOLD))
        self.update(Text.assemble(*pieces))


class ActivityBar(Static):
    """The one moving line. Hidden whenever nothing is happening."""

    def set_activity(self, text: str, level: str = "") -> None:
        if not text:
            self.display = False
            return
        colour = {"error": RED, "warning": GOLD, "success": GREEN}.get(level, MUTED)
        self.display = True
        self.update(Text.assemble(("› ", GOLD), (text, colour)))


class StatusBar(Horizontal):
    """Footer: keys on the left, run state on the right."""

    def compose(self) -> ComposeResult:
        yield Static("", id="hint")
        yield Static("", id="state")

    def set_hint(self, text: str) -> None:
        self.query_one("#hint", Static).update(Text(text, style=DIM))

    def set_state(self, text: str, level: str = "") -> None:
        colour = {"error": RED, "warning": GOLD, "success": GREEN}.get(level, MUTED)
        self.query_one("#state", Static).update(Text(text, style=colour))


class ChatLog(VerticalScroll):
    """Messages are only auto-scrolled while the reader is already at the end."""

    def add(self, widget: Any) -> Any:
        at_end = self._at_end()
        self.mount(widget)
        if at_end:
            self.call_after_refresh(self.scroll_end, animate=False)
        return widget

    def clear_messages(self) -> None:
        self.remove_children()

    def _at_end(self) -> bool:
        try:
            return self.scroll_y >= self.max_scroll_y - 1
        except Exception:
            return True


class UserMessage(Static):
    def __init__(self, text: str) -> None:
        super().__init__(
            Text.assemble(("▌ ", GOLD), (text, f"bold {TEXT}")),
            classes="user-message",
        )


class AssistantMessage(Vertical):
    """Streams plain text, then swaps in a markdown render of the same bytes."""

    def __init__(self) -> None:
        super().__init__(classes="assistant-message")
        self._parts: list[str] = []
        self._reasoning: list[str] = []
        self._reasoning_chars = 0
        self._paint_scheduled = False
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Static("", id="reasoning", classes="reasoning-line")
        yield Static("", id="live", classes="live-answer", markup=False)
        yield Markdown("", id="final", classes="final-answer")

    def on_mount(self) -> None:
        self.query_one("#reasoning", Static).display = False
        self.query_one("#live", Static).display = False
        self.query_one("#final", Markdown).display = False
        if self._parts:
            self._paint()
        if self._reasoning_chars:
            self._paint_reasoning()

    def append_text(self, delta: str) -> None:
        if not delta or self._finished:
            return
        self._parts.append(delta)
        if self.is_mounted:
            self._schedule_paint()

    def append_reasoning(self, delta: str, *, summary: bool = False) -> None:
        if not delta:
            return
        self._reasoning_chars += len(delta)
        if len(self._reasoning) < 400:
            self._reasoning.append(delta if not summary else f"\n[summary] {delta}")
        if self.is_mounted:
            self._paint_reasoning()

    def finish(self, fallback: str = "") -> None:
        if self._finished:
            return
        self._finished = True
        if fallback and not "".join(self._parts).strip():
            self._parts = [fallback]
        if not self.is_mounted:
            return
        text = "".join(self._parts)
        live = self.query_one("#live", Static)
        final = self.query_one("#final", Markdown)
        if text:
            live.display = False
            final.update(text)
            final.display = True
        else:
            final.display = False
            live.display = False
        if self._reasoning_chars:
            self._paint_reasoning()
        self.call_after_refresh(self.refresh)

    def answer_text(self) -> str:
        return "".join(self._parts)

    def reasoning_chars(self) -> int:
        return self._reasoning_chars

    def reasoning_text(self) -> str:
        return "".join(self._reasoning)

    def _schedule_paint(self) -> None:
        if self._paint_scheduled:
            return
        self._paint_scheduled = True
        self.set_timer(0.06, self._paint)

    def _paint(self) -> None:
        self._paint_scheduled = False
        if not self.is_mounted or self._finished:
            return
        text = "".join(self._parts)
        live = self.query_one("#live", Static)
        live.display = bool(text)
        live.update(Text(text))

    def _paint_reasoning(self) -> None:
        line = self.query_one("#reasoning", Static)
        line.display = True
        line.update(
            Text(f"✳ reasoning · {self._reasoning_chars:,} chars", style=DIM)
        )


class ToolCard(Static):
    """One line per tool call; the summary is the observation, not decoration."""

    def __init__(self, tool: str, call_id: str = "") -> None:
        self.tool = tool
        self.call_id = call_id
        self._started = time.monotonic()
        super().__init__(self._tool_text(None, "", 0.0), classes="tool-card", markup=False)

    def finish(self, ok: bool | None, summary: str = "", duration: float = 0.0) -> None:
        elapsed = duration or (time.monotonic() - self._started)
        self.update(self._tool_text(ok, summary, elapsed))

    def _tool_text(self, ok: bool | None, summary: str, duration: float) -> Text:
        if ok is True:
            icon, word, colour = "✓", "ok", GREEN
        elif ok is False:
            icon, word, colour = "✗", "failed", RED
        else:
            icon, word, colour = "→", "running", GOLD
        pieces: list[tuple[str, str]] = [
            (f"{icon} ", colour),
            (self.tool or "tool", f"bold {TEXT}"),
            ("  ·  ", DIM),
            (word, colour),
        ]
        if duration:
            pieces.append(("  ·  ", DIM))
            pieces.append((f"{duration:.2f}s", MUTED))
        if summary:
            pieces.append(("  ·  ", DIM))
            pieces.append((_clip(summary, 110), MUTED))
        return Text.assemble(*pieces)


class Notice(Static):
    def __init__(self, text: str, level: str = "info") -> None:
        self.text = text
        self.level = level
        icon = {"info": "ℹ", "success": "✓", "warning": "!", "error": "✗"}.get(level, "ℹ")
        colour = {"info": BLUE, "success": GREEN, "warning": GOLD, "error": RED}.get(
            level, BLUE
        )
        super().__init__(
            Text.assemble((f"{icon} ", colour), (text, TEXT)),
            classes=f"notice notice-{level}",
            markup=False,
        )


class WelcomeCard(Static):
    def __init__(self, title: str, body: str) -> None:
        super().__init__(
            Text.assemble(
                (title + "\n", f"bold {GOLD}"),
                (body, MUTED),
            ),
            classes="welcome",
            markup=False,
        )


class ChatInput(TextArea):
    """Enter sends; Ctrl+J inserts a newline. Shift+Enter does too when sent."""

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            value = self.text.strip()
            if value:
                self.post_message(self.Submitted(value))
                self.text = ""
            return
        if event.key in {"shift+enter", "ctrl+j"}:
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        await super()._on_key(event)


def _token_text(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M tok"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k tok"
    return f"{value} tok"