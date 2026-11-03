"""The widgets the terminal client is made of.

The screen reads like a terminal session: one transcript that only grows, a
composer at the bottom and one status line under it. Each widget owns exactly
one kind of line -- a user turn, a streamed answer, a tool line, a notice, a
printed block -- so a fact cannot render two different ways. Lists (the slash
palette, a model or session picker) open inline above the composer instead of
on a screen of their own, and the keyboard never leaves the composer.

The status line follows the rule the classic shell proved: the model is the
primary fact and is never dropped, non-defaults are stated and defaults are
not, and a field that no longer fits disappears whole instead of being cut into
a different-looking value.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Sequence

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Input, LoadingIndicator, Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

GOLD = "#d4b676"
MUTED = "#8a7e6a"
TEXT = "#e5e5e5"
GREEN = "#8aab7e"
RED = "#e0a3a3"
AMBER = "#c6a56b"
DIM = "#6f6656"

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

_FACTS_WIDE = 100
_FACTS_MEDIUM = 60
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
    width: int = _FACTS_WIDE,
    language: str = "en",
) -> list[tuple[str, str]]:
    """The status line's facts in display order, already fitted to ``width``.

    Pure so a test can assert the fitting rules without a screen: the model is
    kept at every width, effort/mode appear only when they diverge from the
    default, the context note appears only near the window limit, and fields
    drop whole from the least important end.
    """

    candidates: dict[str, tuple[str, str]] = {}
    no_model = "модель не выбрана" if language == "ru" else "no model"
    candidates["model"] = (model or no_model, AMBER if model else RED)
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
        candidates["repo"] = (repository, TEXT)
    if branch:
        candidates["branch"] = (branch, MUTED)
    if activity:
        candidates["activity"] = (activity, GOLD)
    if running:
        candidates["live"] = ("● live", GOLD)

    if width >= _FACTS_WIDE:
        display: tuple[str, ...] = (
            "model", "tokens", "ctx", "mode", "effort", "repo", "branch",
            "activity", "live",
        )
    elif width >= _FACTS_MEDIUM:
        display = ("model", "tokens", "ctx", "mode", "effort", "repo", "activity", "live")
    else:
        display = ("model", "tokens", "ctx", "mode", "activity", "live")
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


#: The facts on the status line are the classic header's facts.
status_segments = header_segments

_TOOL_WORDS = {
    "en": {"running": "running", "failed": "failed"},
    "ru": {"running": "выполняется", "failed": "ошибка"},
}


def tool_card_text(
    tool: str,
    category: str,
    ok: bool | None,
    summary: str,
    duration: float,
    *,
    language: str = "en",
) -> Text:
    """The one tool line, pure so the exact render is testable headless.

    The category icon runs while the call is open; the outcome mark replaces it
    on finish. A success needs no word -- the mark says it -- so the line keeps
    its width for the observation.
    """

    words = _TOOL_WORDS.get(language, _TOOL_WORDS["en"])
    pieces: list[tuple[str, str]] = []
    if ok is True:
        pieces.append(("✓ ", GREEN))
    elif ok is False:
        pieces.append(("✗ ", RED))
    else:
        pieces.append((CATEGORY_ICONS.get(category, "→") + " ", GOLD))
    pieces.append((tool or "tool", f"bold {TEXT}"))
    if ok is False:
        pieces.append(("  " + words["failed"], RED))
    elif ok is None:
        pieces.append(("  " + words["running"] + "…", GOLD))
    if duration:
        pieces.append((f"  {duration:.2f}s", DIM))
    if summary:
        pieces.append(("  " + _clip(summary, 110), MUTED))
    return Text.assemble(*pieces)


def welcome_text(title: str, body: str, facts: Sequence[str] = ()) -> Text:
    """The opening lines: title, the window's facts, dim, then the body."""

    pieces: list[tuple[str, str]] = [("◆ ", GOLD), (title, f"bold {GOLD}")]
    for fact in facts:
        if fact:
            pieces.append(("\n  " + fact, MUTED))
    if body:
        pieces.append(("\n\n" + body, DIM))
    return Text.assemble(*pieces)


@dataclass(frozen=True)
class MenuItem:
    """One row of an inline list: a stable key, what is shown, and search text."""

    key: str
    label: str
    detail: str = ""
    search: str = ""
    marked: bool = False

    def haystack(self) -> str:
        return f"{self.key} {self.label} {self.detail} {self.search}".lower()

    def prompt(self) -> Text:
        return Text.assemble(
            ("● " if self.marked else "  ", GOLD),
            (self.label, f"bold {TEXT}"),
            (("  " + self.detail) if self.detail else "", MUTED),
        )


class StatusLine(Horizontal):
    """Footer: the window's facts on the left, the run state on the right."""

    def compose(self) -> ComposeResult:
        yield Static("", id="facts")
        yield Static("", id="state")

    def on_mount(self) -> None:
        self._facts_text = ""

    def current_text(self) -> str:
        return getattr(self, "_facts_text", "")

    def set_facts(self, segments: Sequence[tuple[str, str]]) -> None:
        pieces: list[tuple[str, str]] = []
        plain: list[str] = []
        for index, (text, style) in enumerate(segments):
            if index:
                pieces.append((" · ", DIM))
            pieces.append((text, style))
            plain.append(text)
        self._facts_text = " · ".join(plain)
        self.query_one("#facts", Static).update(Text.assemble(*pieces) if pieces else Text(""))

    def set_state(self, text: str, level: str = "") -> None:
        colour = {"error": RED, "warning": GOLD, "success": GREEN}.get(level, MUTED)
        self.query_one("#state", Static).update(Text(text, style=colour))


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
        self.query_one("#activity-line", Static).update(Text(text, style=colour))


class ChatLog(VerticalScroll):
    """Messages are only auto-scrolled while the reader is already at the end.

    It never takes the focus: the keyboard belongs to the composer, which
    forwards PageUp/PageDown here, and the mouse wheel scrolls it directly.
    """

    can_focus = False

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
    """A submitted line; a ``/command`` is echoed dim, like a shell prompt."""

    def __init__(self, text: str, *, command: bool = False) -> None:
        self.text = text
        self.command = command
        if command:
            content = Text.assemble(("› ", MUTED), (text, MUTED))
        else:
            content = Text.assemble(("› ", f"bold {GOLD}"), (text, f"bold {TEXT}"))
        super().__init__(
            content,
            classes="command-echo" if command else "user-message",
            markup=False,
        )


_REASONING_WORDS = {"en": "reasoning · {count:,} chars", "ru": "размышления · {count:,} симв."}


class AssistantMessage(Vertical):
    """One stretch of the answer: streams behind a caret, then renders markdown.

    A turn is a sequence of these and tool lines in the order they happened, so
    text written after a tool call appears after it instead of being glued to
    the text before it.
    """

    def __init__(self, language: str = "en") -> None:
        super().__init__(classes="assistant-message")
        self.language = language
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
        if self._finished:
            self._render_final()
            return
        if self._parts:
            self._paint()
        if self._reasoning_chars:
            self._paint_reasoning()

    @property
    def finished(self) -> bool:
        return self._finished

    def append_text(self, delta: str) -> None:
        if not delta or self._finished:
            return
        self._parts.append(delta)
        if self.is_mounted:
            self._schedule_paint()

    def append_reasoning(self, delta: str, *, summary: bool = False) -> None:
        if not delta or self._finished:
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
        if self.is_mounted:
            self._render_final()

    def _render_final(self) -> None:
        text = "".join(self._parts).strip("\n")
        live = self.query_one("#live", Static)
        final = self.query_one("#final", Markdown)
        live.display = False
        if text.strip():
            final.update(text)
            final.display = True
        else:
            final.display = False
        if self._reasoning_chars:
            self._paint_reasoning()
        self.call_after_refresh(self.refresh)

    def answer_text(self) -> str:
        return "".join(self._parts)

    def reasoning_chars(self) -> int:
        return self._reasoning_chars

    def reasoning_text(self) -> str:
        return "".join(self._reasoning)

    def is_empty(self) -> bool:
        return not self.answer_text().strip() and not self._reasoning_chars

    def _schedule_paint(self) -> None:
        if self._paint_scheduled:
            return
        self._paint_scheduled = True
        self.set_timer(0.06, self._paint)

    def _paint(self) -> None:
        self._paint_scheduled = False
        if not self.is_mounted or self._finished:
            return
        text = "".join(self._parts).lstrip("\n")
        live = self.query_one("#live", Static)
        live.display = bool(text)
        live.update(Text.assemble((text, TEXT), ("▌", GOLD)) if text else Text(""))

    def _paint_reasoning(self) -> None:
        line = self.query_one("#reasoning", Static)
        line.display = True
        template = _REASONING_WORDS.get(self.language, _REASONING_WORDS["en"])
        line.update(Text("◇ " + template.format(count=self._reasoning_chars), style=DIM))


class ToolCard(Static):
    """One line per tool call; the summary is the observation, not decoration.

    The category icon runs while the call is open and the outcome mark replaces
    it on finish. A click expands the line in place with its unclipped detail
    and a second click folds it again -- no screen of its own.
    """

    def __init__(
        self, tool: str, call_id: str = "", category: str = "other", *, language: str = "en"
    ) -> None:
        self.tool = tool
        self.call_id = call_id
        self.category = category
        self.language = language
        self._started = time.monotonic()
        self._ok: bool | None = None
        self._summary = ""
        self._duration = 0.0
        self.expanded = False
        super().__init__(
            self._tool_text(),
            classes=f"tool-card tool-{category}",
            markup=False,
        )

    @property
    def ok(self) -> bool | None:
        return self._ok

    def finish(self, ok: bool | None, summary: str = "", duration: float = 0.0) -> None:
        self._ok = ok
        self._summary = summary
        self._duration = duration or (time.monotonic() - self._started)
        self.update(self._tool_text())

    def detail_text(self) -> str:
        lines = [self.tool or "tool"]
        if self.call_id:
            lines.append(("вызов " if self.language == "ru" else "call ") + self.call_id)
        if self._duration:
            lines.append(f"{self._duration:.2f}s")
        if self._summary:
            lines.append(self._summary)
        return "\n".join(lines)

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self.update(self._tool_text())

    def on_click(self, event: events.Click) -> None:
        self.toggle()
        event.stop()

    def _tool_text(self) -> Text:
        line = tool_card_text(
            self.tool,
            self.category,
            self._ok,
            self._summary,
            self._duration,
            language=self.language,
        )
        if not self.expanded:
            return line
        extra: list[tuple[str, str]] = []
        if self.call_id:
            extra.append(("\n    " + ("вызов " if self.language == "ru" else "call ") + self.call_id, DIM))
        if self._summary:
            for row in self._summary[:4000].splitlines() or [""]:
                extra.append(("\n    " + row, MUTED))
        return Text.assemble(line, *extra)


class Notice(Static):
    """A one-off fact from KaroX itself, marked by level, never boxed."""

    def __init__(self, text: str, level: str = "info") -> None:
        self.text = text
        self.level = level
        icon = {"info": "·", "success": "✓", "warning": "!", "error": "✗"}.get(level, "·")
        colour = {"info": MUTED, "success": GREEN, "warning": GOLD, "error": RED}.get(level, MUTED)
        body = TEXT if level in {"warning", "error"} else MUTED
        lines = text.splitlines() or [""]
        pieces: list[tuple[str, str]] = [(f"{icon} ", colour), (lines[0], body)]
        for line in lines[1:]:
            pieces.append(("\n  " + line, MUTED))
        super().__init__(Text.assemble(*pieces), classes=f"notice notice-{level}", markup=False)


class TextBlock(Static):
    """Printed output of a command (/help, /status, /session …): title, then lines."""

    def __init__(self, title: str, body: Text | str) -> None:
        self.title = title
        self.body = body if isinstance(body, Text) else Text(str(body), style=TEXT)
        content = Text.assemble((title, f"bold {GOLD}"), "\n", self.body) if title else self.body
        super().__init__(content, classes="text-block", markup=False)

    def plain_text(self) -> str:
        return f"{self.title}\n{self.body.plain}" if self.title else self.body.plain


class WelcomeCard(Static):
    def __init__(self, title: str, body: str, facts: Sequence[str] = ()) -> None:
        super().__init__(welcome_text(title, body, facts), classes="welcome", markup=False)


class InlineMenu(Vertical):
    """A list opened above the composer: the slash palette or a picker.

    The composer keeps the focus: what is typed filters the list, and the
    composer forwards ↑/↓, Tab, Enter and Esc here while the menu is open.
    """

    class Chosen(Message):
        def __init__(self, kind: str, key: str, complete: bool) -> None:
            super().__init__()
            self.kind = kind
            self.key = key
            self.complete = complete

    class Cancelled(Message):
        def __init__(self, kind: str) -> None:
            super().__init__()
            self.kind = kind

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.kind: str | None = None
        self._items: list[MenuItem] = []
        self._shown: list[MenuItem] = []
        self._current: str | None = None
        self._empty_text = "no matches"
        self.query_text = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="menu-title")
        yield OptionList(id="menu-options")
        yield Static("", id="menu-hint")

    def on_mount(self) -> None:
        self.display = False
        self.query_one("#menu-options", OptionList).can_focus = False

    @property
    def active(self) -> bool:
        return self.kind is not None and bool(self.display)

    def keys(self) -> list[str]:
        """Keys of the rows currently shown, in order (for tests and callers)."""

        return [item.key for item in self._shown]

    def highlighted_key(self) -> str | None:
        index = self.query_one("#menu-options", OptionList).highlighted
        if index is None or not 0 <= index < len(self._shown):
            return None
        return self._shown[index].key

    def open(
        self,
        kind: str,
        title: str,
        items: Sequence[MenuItem],
        *,
        hint: str = "",
        empty: str = "",
        current: str | None = None,
        query: str = "",
        filtered: bool = True,
    ) -> None:
        self.kind = kind
        self._items = list(items)
        self._current = current
        self._filtered = filtered
        if empty:
            self._empty_text = empty
        self.query_one("#menu-title", Static).update(Text(title, style=f"bold {GOLD}"))
        self.query_one("#menu-title", Static).display = bool(title)
        self.query_one("#menu-hint", Static).update(Text(hint, style=DIM))
        self.query_one("#menu-hint", Static).display = bool(hint)
        self.display = True
        self.filter(query)

    def filter(self, query: str) -> None:
        self.query_text = query
        tokens = query.lower().split() if getattr(self, "_filtered", True) else []
        shown = [item for item in self._items if all(token in item.haystack() for token in tokens)]
        self._shown = shown
        options = self.query_one("#menu-options", OptionList)
        options.clear_options()
        if not shown:
            options.add_option(Option(Text(self._empty_text, style=DIM), disabled=True))
            return
        options.add_options([Option(item.prompt(), id=f"item-{index}") for index, item in enumerate(shown)])
        highlight = 0
        if self._current is not None:
            for index, item in enumerate(shown):
                if item.key == self._current:
                    highlight = index
                    break
        options.highlighted = highlight
        options.scroll_to_highlight()

    def move(self, delta: int) -> None:
        if not self._shown:
            return
        options = self.query_one("#menu-options", OptionList)
        current = options.highlighted if options.highlighted is not None else 0
        options.highlighted = max(0, min(len(self._shown) - 1, current + delta))
        options.scroll_to_highlight()

    def accept(self, *, complete: bool = False) -> bool:
        key = self.highlighted_key()
        if key is None or self.kind is None:
            return False
        self.post_message(self.Chosen(self.kind, key, complete))
        return True

    def cancel(self) -> None:
        kind = self.kind
        self.close()
        if kind is not None:
            self.post_message(self.Cancelled(kind))

    def close(self) -> None:
        self.kind = None
        self._items = []
        self._shown = []
        self.display = False

    @on(OptionList.OptionSelected, "#menu-options")
    def _clicked(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        if self.kind is not None and 0 <= index < len(self._shown):
            self.post_message(self.Chosen(self.kind, self._shown[index].key, False))


class SecretPrompt(Input):
    """A masked one-line entry that replaces the composer for an API key.

    The value goes straight to the CLI's stdin and is never echoed, stored in
    the composer history or written to the transcript.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    class Cancelled(Message):
        pass

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("password", True)
        super().__init__(*args, **kwargs)

    def action_cancel(self) -> None:
        self.value = ""
        self.post_message(self.Cancelled())


class ChatInput(TextArea):
    """Enter sends; Ctrl+J inserts a newline; Up/Down recall submitted lines.

    While an inline menu is open, ↑/↓, PageUp/PageDown, Tab, Enter and Esc
    drive the menu and typing filters it; Enter in a picker never sends the
    filter text as a task. Otherwise PageUp/PageDown scroll the transcript.
    History navigation only engages at the document edges, so editing a
    multi-line draft never loses it: the draft is restored on the way down.
    ``recalled`` is the text the last recall loaded, so the app can tell a
    recalled ``/command`` from one being typed and not pop the palette open.
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
        self.inline_menu: InlineMenu | None = None
        self.scroller: VerticalScroll | None = None
        self.recalled: str | None = None

    def push_history(self, value: str) -> None:
        if value and (not self._history or self._history[-1] != value):
            self._history.append(value)
            if len(self._history) > self._HISTORY_LIMIT:
                self._history = self._history[-self._HISTORY_LIMIT :]
        self._history_index = -1
        self._history_draft = ""
        self.recalled = None

    def submitted_lines(self) -> list[str]:
        return list(self._history)

    def set_text(self, value: str) -> None:
        """Replace the draft and put the cursor after it."""

        self.load_text(value)
        self.move_cursor(self.document.end)

    async def _on_key(self, event: events.Key) -> None:
        menu = self.inline_menu
        if menu is not None and menu.active:
            handled = True
            if event.key == "up":
                menu.move(-1)
            elif event.key == "down":
                menu.move(1)
            elif event.key == "pageup":
                menu.move(-8)
            elif event.key == "pagedown":
                menu.move(8)
            elif event.key == "tab":
                menu.accept(complete=True)
            elif event.key == "enter":
                # The palette falls through to a plain submit when nothing
                # matches (so an unknown /command is reported); a picker's
                # filter text is never a task.
                handled = menu.accept() or menu.kind != "commands"
            elif event.key == "escape":
                menu.cancel()
            else:
                handled = False
            if handled:
                event.prevent_default()
                event.stop()
                return
        if event.key in {"pageup", "pagedown"} and self.scroller is not None:
            event.prevent_default()
            event.stop()
            if event.key == "pageup":
                self.scroller.scroll_page_up(animate=False)
            else:
                self.scroller.scroll_page_down(animate=False)
            return
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
        self.recalled = self._history[self._history_index]
        self.load_text(self.recalled)

    def _recall_newer(self) -> None:
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.recalled = self._history[self._history_index]
            self.load_text(self.recalled)
            return
        self._history_index = -1
        self.recalled = self._history_draft
        self.load_text(self._history_draft)
        self._history_draft = ""
