"""The widgets the compact client is made of.

Each one owns exactly one kind of line on screen: chrome, a user turn, a
streaming answer, a tool observation, a notice, the composer, the footer. There
is no second presentation path, so a fact cannot render two different ways.

The header follows the rule the classic shell proved: the model is the primary
fact and is never dropped, non-defaults are stated and defaults are not, and a
field that no longer fits disappears whole instead of being cut into a
different-looking value.
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import LoadingIndicator, Markdown, Static, TextArea

GOLD = "#d4b676"
MUTED = "#8a7e6a"
TEXT = "#e5e5e5"
GREEN = "#8aab7e"
RED = "#e0a3a3"
AMBER = "#c6a56b"
DIM = "#8a7e6a"

#: One icon per tool category, so a running line says what kind of work it is
#: before the name is read. The finished line switches to the outcome mark.
CATEGORY_ICONS = {
    "read": "○",
    "edit": "✎",
    "test": "≡",
    "mcp": "⬢",
    "skill": "★",
    "plan": "▤",
    "web": "◎",
    "inspect": "⊙",
    "subagent": "⧉",
    "other": "→",
}


_HEADER_WIDE = 100
_HEADER_MEDIUM = 72
CONTEXT_WARNING_FRACTION = 0.8


def _clip(value: str, limit: int) -> str:
    flat = " ".join(value.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _token_text(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M tok"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k tok"
    return f"{value} tok"


def header_segments(
    *,
    model: str,
    repository: str,
    branch: str = "",
    tokens: int = 0,
    context_fraction: float | None = None,
    effort: str = "auto",
    mode: str = "build",
    activity: str = "",
    running: bool = False,
    width: int = _HEADER_WIDE,
    language: str = "en",
) -> list[tuple[str, str]]:
    """The header's facts in display order, already fitted to ``width``.

    Pure so a test can assert the fitting rules without a screen: the model is
    kept at every width, effort/mode appear only when they diverge from the
    default, the context note appears only near the window limit, and fields
    drop whole from the least important end.
    """

    candidates: dict[str, tuple[str, str]] = {}
    candidates["model"] = (model or "no model", AMBER if model else RED)
    if tokens > 0:
        candidates["tokens"] = (_token_text(tokens), MUTED)
    if context_fraction is not None and context_fraction >= CONTEXT_WARNING_FRACTION:
        label = "контекст" if language == "ru" else "ctx"
        candidates["ctx"] = (f"{label} {int(context_fraction * 100)}%", GOLD)
    if mode and mode != "build":
        candidates["mode"] = (mode, MUTED)
    if effort and effort != "auto":
        candidates["effort"] = (f"effort {effort}" if language != "ru" else f"глубина {effort}", MUTED)
    if repository:
        candidates["repo"] = (repository, f"bold {TEXT}")
    if branch:
        candidates["branch"] = (branch, MUTED)
    if activity:
        candidates["activity"] = (activity, GOLD)
    if running:
        candidates["live"] = ("● live", GOLD)

    if width >= _HEADER_WIDE:
        display: tuple[str, ...] = (
            "model", "tokens", "ctx", "mode", "effort", "repo", "branch",
            "activity", "live",
        )
    elif width >= _HEADER_MEDIUM:
        display = ("model", "tokens", "ctx", "repo", "activity", "live")
    else:
        display = ("model", "tokens", "activity", "live")
    # Least important first: the branch goes before the repo, decoration before
    # the live marker, and the model is not in this list at all.
    drop_order = ("branch", "repo", "effort", "mode", "ctx", "activity", "tokens", "live")

    kept = [key for key in display if key in candidates]

    def joined(keys: Sequence[str]) -> int:
        return sum(len(candidates[key][0]) for key in keys) + 3 * max(0, len(keys) - 1)

    budget = max(12, width - 4)
    for key in drop_order:
        if len(kept) <= 1 or joined(kept) <= budget:
            break
        if key in kept:
            kept.remove(key)
    while len(kept) > 1 and joined(kept) > budget:
        kept.pop()
    return [candidates[key] for key in kept]


def tool_card_text(
    tool: str,
    category: str,
    ok: bool | None,
    summary: str,
    duration: float,
) -> Text:
    """The one tool line, pure so the exact render is testable headless.

    The category icon runs while the call is open; the outcome mark replaces it
    on finish, so a scrolling log stays readable at a glance.
    """

    if ok is True:
        icon, word, colour = "✓", "ok", GREEN
    elif ok is False:
        icon, word, colour = "✗", "failed", RED
    else:
        icon = CATEGORY_ICONS.get(category, "→")
        word, colour = "running", GOLD
    pieces: list[tuple[str, str]] = [
        (f"{icon} ", colour),
        (tool or "tool", f"bold {TEXT}"),
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


def welcome_text(title: str, body: str, facts: Sequence[str] = ()) -> Text:
    """The opening card: title, body, then the window's facts, dim."""

    pieces: list[tuple[str, str]] = [(title + "\n", f"bold {GOLD}"), (body, MUTED)]
    for fact in facts:
        if fact:
            pieces.append(("\n" + fact, DIM))
    return Text.assemble(*pieces)


class HeaderBar(Static):
    """One line: what this window is attached to, fitted to the real width."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._text_value = "KaroX"

    def current_text(self) -> str:
        return self._text_value

    def on_mount(self) -> None:
        self.update(Text("KaroX", style=f"bold {GOLD}"))

    def update_state(
        self,
        *,
        repository: str,
        branch: str,
        model: str,
        tokens: int,
        running: bool,
        effort: str = "auto",
        mode: str = "build",
        activity: str = "",
        context_fraction: float | None = None,
        language: str = "en",
        width: int | None = None,
    ) -> None:
        effective = width if width is not None else max(0, self.size.width)
        segments = header_segments(
            model=model,
            repository=repository,
            branch=branch,
            tokens=tokens,
            context_fraction=context_fraction,
            effort=effort,
            mode=mode,
            activity=activity,
            running=running,
            width=effective or _HEADER_WIDE,
            language=language,
        )
        pieces: list[tuple[str, str]] = [("KaroX", f"bold {GOLD}")]
        plain: list[str] = ["KaroX"]
        for text, style in segments:
            pieces.append(("  ·  ", DIM))
            pieces.append((text, style))
            plain.append(text)
        self._text_value = "  ·  ".join(plain)
        self.update(Text.assemble(*pieces))


class ActivityBar(Horizontal):
    """The one moving line: a real spinner plus the latest typed fact.

    Hidden whenever nothing is happening, so an empty screen stays empty.
    """

    def compose(self) -> ComposeResult:
        yield LoadingIndicator(id="activity-spin")
        yield Static("", id="activity-line")

    def on_mount(self) -> None:
        self.display = False

    def set_activity(self, text: str, level: str = "") -> None:
        if not text:
            self.display = False
            return
        colour = {"error": RED, "warning": GOLD, "success": GREEN}.get(level, MUTED)
        self.display = True
        self.query_one("#activity-line", Static).update(
            Text.assemble(("› ", GOLD), (text, colour))
        )


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
    """Streams plain text behind a caret, then swaps in a markdown render."""

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
        live.update(Text.assemble((text, TEXT), ("▌", GOLD)) if text else Text(""))

    def _paint_reasoning(self) -> None:
        line = self.query_one("#reasoning", Static)
        line.display = True
        line.update(
            Text(f"✳ reasoning · {self._reasoning_chars:,} chars", style=DIM)
        )


class ToolCard(Static):
    """One line per tool call; the summary is the observation, not decoration.

    The category icon runs while the call is open and the outcome mark replaces
    it on finish, so a scrolling log stays readable at a glance. Clicking a
    card asks the app for the unclipped detail.
    """

    class Selected(Message):
        def __init__(self, card: "ToolCard") -> None:
            super().__init__()
            self.card = card

    def __init__(self, tool: str, call_id: str = "", category: str = "other") -> None:
        self.tool = tool
        self.call_id = call_id
        self.category = category
        self._started = time.monotonic()
        self._ok: bool | None = None
        self._summary = ""
        self._duration = 0.0
        super().__init__(
            self._tool_text(None, "", 0.0),
            classes=f"tool-card tool-{category}",
            markup=False,
        )

    def finish(self, ok: bool | None, summary: str = "", duration: float = 0.0) -> None:
        self._ok = ok
        self._summary = summary
        self._duration = duration or (time.monotonic() - self._started)
        self.update(self._tool_text(ok, summary, self._duration))

    def detail_text(self) -> str:
        state = (
            "ok" if self._ok is True else "failed" if self._ok is False else "running"
        )
        lines = [
            f"tool: {self.tool or '?'}",
            f"category: {self.category}",
            f"state: {state}",
        ]
        if self.call_id:
            lines.append(f"call id: {self.call_id}")
        if self._duration:
            lines.append(f"duration: {self._duration:.2f}s")
        if self._summary:
            lines.append("")
            lines.append(self._summary)
        return "\n".join(lines)

    def on_click(self, event: events.Click) -> None:
        self.post_message(self.Selected(self))
        event.stop()

    def _tool_text(self, ok: bool | None, summary: str, duration: float) -> Text:
        return tool_card_text(self.tool, self.category, ok, summary, duration)


class Notice(Static):
    def __init__(self, text: str, level: str = "info") -> None:
        self.text = text
        self.level = level
        icon = {"info": "ℹ", "success": "✓", "warning": "!", "error": "✗"}.get(level, "ℹ")
        colour = {"info": AMBER, "success": GREEN, "warning": GOLD, "error": RED}.get(
            level, AMBER
        )
        super().__init__(
            Text.assemble((f"{icon} ", colour), (text, TEXT)),
            classes=f"notice notice-{level}",
            markup=False,
        )


class WelcomeCard(Static):
    def __init__(self, title: str, body: str, facts: Sequence[str] = ()) -> None:
        super().__init__(
            welcome_text(title, body, facts), classes="welcome", markup=False
        )


class ChatInput(TextArea):
    """Enter sends; Ctrl+J inserts a newline; Up/Down recall submitted tasks.

    History navigation only engages at the document edges, so editing a
    multi-line draft never loses it: the draft is restored on the way down.
    """

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    _HISTORY_LIMIT = 100

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._history: list[str] = []
        self._history_index = -1
        self._history_draft = ""

    def push_history(self, value: str) -> None:
        if value and (not self._history or self._history[-1] != value):
            self._history.append(value)
            if len(self._history) > self._HISTORY_LIMIT:
                self._history = self._history[-self._HISTORY_LIMIT :]
        self._history_index = -1
        self._history_draft = ""

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            value = str(self.text).strip()  # type: ignore[has-type]
            if value:
                self.post_message(self.Submitted(value))
                self.text = ""  # type: ignore[has-type]
            return
        if event.key in {"shift+enter", "ctrl+j"}:
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.key == "up" and self._history and self.cursor_location[0] == 0:
            event.prevent_default()
            event.stop()
            self._recall_older()
            return
        if (
            event.key == "down"
            and self._history_index >= 0
            and self.cursor_at_last_line
        ):
            event.prevent_default()
            event.stop()
            self._recall_newer()
            return
        await super()._on_key(event)

    def _recall_older(self) -> None:
        if self._history_index == -1:
            self._history_draft = str(self.text)
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        self.load_text(self._history[self._history_index])

    def _recall_newer(self) -> None:
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.load_text(self._history[self._history_index])
            return
        self._history_index = -1
        self.load_text(self._history_draft)
        self._history_draft = ""
