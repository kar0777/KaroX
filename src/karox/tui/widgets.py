"""The widgets the terminal client is made of.

The screen reads like a coding-agent CLI session: one transcript that only
grows, a working line with KaroX's animated dots while the agent works, a
framed composer and one quiet footer under it. Each widget owns exactly one
kind of line -- a user turn, a streamed answer, a tool call with its ``⎿``
result, a notice, a printed block -- so a fact cannot render two different
ways. Lists (the slash palette, a model or session picker) open inline above
the composer instead of on a screen of their own, and the keyboard never
leaves the composer.

The footer follows the rule the classic shell proved: the model is the primary
fact and is never dropped, non-defaults are stated and defaults are not, and a
field that no longer fits disappears whole instead of being cut into a
different-looking value.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from typing import Any, Sequence

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Input, LoadingIndicator, Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .i18n import plural, tr
from .tooltext import (
    call_line,
    diff_rows,
    duration_text,
    looks_like_diff,
    plan_rows,
    result_line,
)

GOLD = "#d4b676"
MUTED = "#8a7e6a"
TEXT = "#e5e5e5"
GREEN = "#8aab7e"
RED = "#e0a3a3"
AMBER = "#c6a56b"
DIM = "#6f6656"
ADDED_BG = "#1d2b1a"
REMOVED_BG = "#33201d"

#: The transcript's two marks: a bullet opens an answer or a tool call, the
#: elbow hangs a result under it.
BULLET = "●"
ELBOW = "⎿"

_FACTS_WIDE = 100
_FACTS_MEDIUM = 60
CONTEXT_WARNING_FRACTION = 0.8
#: Rows a folded tool line shows of its preview before "… +N".
FOLDED_DIFF_ROWS = 8
FOLDED_OUTPUT_ROWS = 4


def _clip(value: str, limit: int) -> str:
    flat = " ".join(value.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _token_text(value: int, language: str = "en") -> str:
    unit = "ток." if language == "ru" else "tok"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M {unit}"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k {unit}"
    return f"{value} {unit}"


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
    """The footer's facts in display order, already fitted to ``width``.

    Pure so a test can assert the fitting rules without a screen: the model is
    kept at every width, effort/mode appear only when they diverge from the
    default, the context note appears only near the window limit, and fields
    drop whole from the least important end.
    """

    candidates: dict[str, tuple[str, str]] = {}
    no_model = "модель не выбрана" if language == "ru" else "no model"
    candidates["model"] = (model or no_model, MUTED if model else RED)
    if tokens > 0:
        candidates["tokens"] = (_token_text(tokens, language), DIM)
    if context_fraction is not None and context_fraction >= CONTEXT_WARNING_FRACTION:
        label = "контекст" if language == "ru" else "ctx"
        candidates["ctx"] = (f"{label} {int(context_fraction * 100)}%", GOLD)
    if mode and mode != "build":
        candidates["mode"] = (mode, MUTED)
    if effort and effort != "auto":
        candidates["effort"] = (f"effort {effort}" if language != "ru" else f"глубина {effort}", MUTED)
    if repository:
        candidates["repo"] = (repository, MUTED)
    if branch:
        candidates["branch"] = (branch, DIM)
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


#: The facts on the footer are the classic header's facts.
status_segments = header_segments


# Glyphs a transcript line starts with before its text: the step and tool
# bullet, the result elbow, notice icons, the prompt, plan checkboxes.
_HANG_MARKERS = frozenset("●⎿✓✗!>∴◆☐☒◼")
_WRAP_CONSOLE: Console | None = None


def hang_indent(line: str) -> int:
    """Where a wrapped continuation of ``line`` starts: after its markers.

    ``  ⎿  Read 40 lines`` continues under ``Read``, ``> fix it`` under
    ``fix``, a preview row under its own first character. A marker glued to
    the text (``>>>``) is text, not a marker.
    """

    index = len(line) - len(line.lstrip(" "))
    while index < len(line) and line[index] in _HANG_MARKERS:
        end = index + 1
        while end < len(line) and line[end] == " ":
            end += 1
        if end == index + 1 or end >= len(line):
            break
        index = end
    return index


def hang_text(text: Text, width: int) -> Text:
    """Wrap ``text`` at ``width`` cells, each continuation under its line's text.

    Textual wraps a long line back to column 0, under the bullet, which reads
    as a new line of the transcript; a terminal agent hangs it under the text
    instead. Lines that fit are kept as they are, so a narrow-enough text
    comes back unchanged.
    """

    global _WRAP_CONSOLE
    if width < 16 or text.cell_len <= width and "\n" not in text.plain:
        return text
    lines = text.split("\n", allow_blank=True)
    if all(line.cell_len <= width for line in lines):
        return text
    if _WRAP_CONSOLE is None:
        _WRAP_CONSOLE = Console(
            file=io.StringIO(), width=width, color_system=None, legacy_windows=False
        )
    out: list[Text] = []
    for line in lines:
        if line.cell_len <= width:
            out.append(line)
            continue
        index = hang_indent(line.plain)
        indent = cell_len(line.plain[:index])
        if indent > width // 2:
            index, indent = 0, 0
        pieces = line[index:].wrap(_WRAP_CONSOLE, width - indent)
        for number, piece in enumerate(pieces):
            lead = line[:index] if number == 0 else Text(" " * indent)
            out.append(Text.assemble(lead, piece))
    return Text("\n").join(out)


class HangingStatic(Static):
    """A transcript line whose wrapped rows stay under its text.

    The logical text is kept and wrapped again for the width the widget has
    now -- on mount, on every resize, on every change of the text.
    """

    def __init__(self, content: Text, **kwargs: Any) -> None:
        self._logical = content
        self._hung_width = 0
        super().__init__(content, **kwargs)

    def set_text(self, content: Text) -> None:
        self._logical = content
        self._hang()

    def logical_text(self) -> Text:
        return self._logical

    def _hang(self) -> None:
        width = self.size.width
        self._hung_width = width
        self.update(hang_text(self._logical, width) if width > 0 else self._logical)

    def on_resize(self, event: events.Resize) -> None:
        if event.size.width != self._hung_width:
            self._hang()


def _preview_rows(preview: str, expanded: bool, lang: str) -> list[tuple[str, str]]:
    """The preview under a tool line as ``(text, style)`` rows, folded or whole."""

    rows: list[tuple[str, str]] = []
    if looks_like_diff(preview):
        parsed = diff_rows(preview)
        width = max((len(str(number)) for _kind, number, _text in parsed if number), default=1)
        shown = parsed if expanded else parsed[:FOLDED_DIFF_ROWS]
        for kind, number, text in shown:
            if kind == "…":
                rows.append((" " * width + "  ⋮", DIM))
                continue
            label = str(number).rjust(width)
            if kind == "+":
                rows.append((f"{label} + {text}", f"{TEXT} on {ADDED_BG}"))
            elif kind == "-":
                rows.append((f"{label} - {text}", f"{TEXT} on {REMOVED_BG}"))
            else:
                rows.append((f"{label}   {text}", DIM))
        hidden = len(parsed) - len(shown)
    else:
        lines = preview.splitlines()
        shown_lines = lines if expanded else lines[:FOLDED_OUTPUT_ROWS]
        rows.extend((line, MUTED) for line in shown_lines)
        hidden = len(lines) - len(shown_lines)
    if hidden > 0:
        rows.append((tr(lang, "toolres.more", lines=plural(lang, hidden, "line")), DIM))
    return rows


def tool_card_text(
    tool: str,
    category: str = "other",
    ok: bool | None = None,
    summary: str = "",
    duration: float = 0.0,
    *,
    language: str = "en",
    detail: str = "",
    preview: str = "",
    error: str = "",
    expanded: bool = False,
    blink: bool = False,
    call_id: str = "",
    interrupted: bool = False,
) -> Text:
    """One tool call as the transcript shows it; pure so the render is testable.

        ● Read(src/app.py)
          ⎿  Read 40 lines

    The bullet is the state: gold and blinking while the call runs, green when
    it succeeded, red when it failed. Under the elbow: the result in words,
    then a short preview -- a numbered diff for an edit, the last lines of a
    command -- folded to a few rows unless the line is expanded.
    """

    del category  # the title already says what kind of work it is
    if ok is True:
        bullet = GREEN
    elif ok is False:
        bullet = RED
    elif interrupted:
        bullet = DIM
    else:
        bullet = DIM if blink else GOLD
    head = call_line(tool, detail)
    title, paren, rest = head.partition("(")
    pieces: list[tuple[str, str]] = [(BULLET + " ", bullet), (title, f"bold {TEXT}")]
    if paren:
        pieces.append(("(" + rest, TEXT))
    plan = plan_rows(detail) if tool == "plan.update" else []
    if plan and ok is not False:
        marks = {"done": ("☒ ", GREEN), "doing": ("◼ ", GOLD)}
        for index, (status, text) in enumerate(plan):
            mark, colour = marks.get(status, ("☐ ", MUTED))
            lead = f"\n  {ELBOW}  " if index == 0 else "\n     "
            style = f"strike {DIM}" if status == "done" else (f"bold {TEXT}" if status == "doing" else TEXT)
            pieces.extend([(lead, DIM), (mark, colour), (text, style)])
    else:
        result = result_line(tool, ok, summary, language)
        if ok is False and error:
            result = tr(language, "toolres.error", detail=_clip(error, 300))
        elif ok is None and interrupted:
            result = tr(language, "toolres.interrupted")
        colour = RED if ok is False else MUTED
        pieces.append((f"\n  {ELBOW}  ", DIM))
        pieces.append((result, colour))
        if ok is not None and duration >= 2.0:
            pieces.append((" · " + duration_text(duration, language), DIM))
        if preview and ok is not None:
            for text, style in _preview_rows(preview, expanded, language):
                pieces.append(("\n     ", DIM))
                pieces.append((text, style))
    if expanded:
        # One quiet row of raw facts for cross-checking with the session log.
        extras = [tr(language, "tool.call", id=call_id)] if call_id else []
        if summary and ok is not None:
            extras.append(summary)
        if extras:
            pieces.append(("\n     " + " · ".join(extras), DIM))
    return Text.assemble(*pieces)


def welcome_text(title: str, body: str, facts: Sequence[str] = ()) -> Text:
    """The welcome box's content: the title, the one-line help, then the facts."""

    pieces: list[tuple[str, str]] = [("◆ ", GOLD), (title, f"bold {TEXT}")]
    if body:
        pieces.append(("\n\n  " + body, MUTED))
    shown = [fact for fact in facts if fact]
    if shown:
        pieces.append(("\n", DIM))
        for fact in shown:
            pieces.append(("\n  " + fact, DIM))
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
    """Footer: a quiet hint on the left, the window's facts on the right."""

    def compose(self) -> ComposeResult:
        yield Static("", id="hint")
        yield Static("", id="facts")

    def on_mount(self) -> None:
        self._facts_text = ""
        self._hint_text = ""

    def current_text(self) -> str:
        hint = getattr(self, "_hint_text", "")
        facts = getattr(self, "_facts_text", "")
        return " · ".join(part for part in (hint, facts) if part)

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

    def set_hint(self, text: str, level: str = "") -> None:
        colour = {"error": RED, "warning": GOLD, "success": GREEN, "mode": AMBER}.get(level, DIM)
        self._hint_text = text
        self.query_one("#hint", Static).update(Text(text, style=colour))

    #: The run state used to sit here; the hint took its place.
    set_state = set_hint


class WorkingLine(Vertical):
    """KaroX's animated dots and what the agent is doing right now.

        ● ● ● ● ●  Reading src/app.py… (12s · 1.2k tokens · esc to interrupt)
                   ⎿ Next: fix the parser

    Hidden whenever nothing is happening, so a finished screen stays still.
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="working-row"):
            yield LoadingIndicator(id="spinner")
            yield Static("", id="working-text")
        yield Static("", id="working-sub")

    def on_mount(self) -> None:
        self.display = False
        self.query_one("#working-sub", Static).display = False
        # The indicator turns its own timer on when it mounts, which can be
        # after this handler; settle it once the first frame is drawn.
        self.call_after_refresh(lambda: self._spin(self.display))

    def _spin(self, on: bool) -> None:
        # The dots repaint 16 times a second; a hidden line must not.
        spinner = self.query_one("#spinner", LoadingIndicator)
        spinner.auto_refresh = (1 / 16) if on else None

    def set_working(
        self, text: str, facts: str = "", sub: str = "", level: str = ""
    ) -> None:
        if not text:
            self.display = False
            self._spin(False)
            return
        colour = {"error": RED, "warning": GOLD, "success": GREEN}.get(level, GOLD)
        line = Text.assemble((text, f"bold {colour}"), ((" " + facts) if facts else "", DIM))
        self.query_one("#working-text", Static).update(line)
        below = self.query_one("#working-sub", Static)
        below.update(Text(sub, style=DIM))
        below.display = bool(sub)
        if not self.display:
            self.display = True
            self._spin(True)

    def current_text(self) -> str:
        widget = self.query_one("#working-text", Static)
        return str(widget.render()) if self.display else ""


class ChatLog(VerticalScroll):
    """The transcript follows its end only while the reader is at the end.

    Once the transcript overflows the view it is anchored (Textual keeps an
    anchored view at the bottom through every layout), so it follows new lines
    *and* lines that grow while shown: a streaming answer, a tool line gaining
    its preview. Scrolling up with the wheel or PageUp releases the anchor;
    scrolling back to the end restores it. Scrolling only when a widget was
    mounted let a growing line push the end out of view, after which the view
    stopped following for good. Until it overflows it is not anchored: short
    anchored content would sit at the bottom, and the welcome box belongs at
    the top.

    It never takes the focus: the keyboard belongs to the composer, which
    forwards PageUp/PageDown here, and the mouse wheel scrolls it directly.
    """

    can_focus = False

    def watch_virtual_size(self) -> None:
        # The content changed size; decide once the layout has settled.
        if not self.is_anchored:
            self.call_after_refresh(self._anchor_if_overflowing)

    def _anchor_if_overflowing(self) -> None:
        if not self.is_anchored and self.max_scroll_y > 0:
            self.anchor()

    def add(self, widget: Any) -> Any:
        self.mount(widget)
        return widget

    def clear_messages(self) -> None:
        self.anchor(False)
        self.remove_children()


class UserMessage(HangingStatic):
    """A submitted line behind ``>``; a ``/command`` is echoed dim, like a shell."""

    def __init__(self, text: str, *, command: bool = False) -> None:
        self.text = text
        self.command = command
        if command:
            content = Text.assemble(("> ", DIM), (text, MUTED))
        else:
            content = Text.assemble(("> ", MUTED), (text, TEXT))
        super().__init__(
            content,
            classes="command-echo" if command else "user-message",
            markup=False,
        )


class AssistantMessage(Vertical):
    """One stretch of the answer: ``●`` then text that streams, then markdown.

    A turn is a sequence of these and tool lines in the order they happened, so
    text written after a tool call appears after it instead of being glued to
    the text before it. The model's reasoning gets its own ``∴`` line above the
    answer: a live tail while it streams, folded to one line afterwards.
    """

    def __init__(self, language: str = "en") -> None:
        super().__init__(classes="assistant-message")
        self.language = language
        self._parts: list[str] = []
        self._reasoning: list[str] = []
        self._reasoning_chars = 0
        self._summary_mode = False
        self._thought_from = 0.0
        self._thought_until = 0.0
        self._paint_scheduled = False
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Static("", id="reasoning", classes="thinking-line", markup=False)
        with Horizontal(id="answer-row", classes="answer-row"):
            yield Static(BULLET, classes="answer-bullet", markup=False)
            with Vertical(classes="answer-body"):
                yield Static("", id="live", classes="live-answer", markup=False)
                yield Markdown("", id="final", classes="final-answer")

    def on_mount(self) -> None:
        self.query_one("#reasoning", Static).display = False
        self.query_one("#answer-row", Horizontal).display = False
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
        if self._reasoning_chars and not self._thought_until:
            self._thought_until = time.monotonic()
            if self.is_mounted:
                self._paint_reasoning()
        self._parts.append(delta)
        if self.is_mounted:
            self._schedule_paint()

    def append_reasoning(self, delta: str, *, summary: bool = False) -> None:
        if not delta or self._finished:
            return
        if not self._thought_from:
            self._thought_from = time.monotonic()
        self._reasoning_chars += len(delta)
        self._summary_mode = self._summary_mode or summary
        if len(self._reasoning) < 400:
            self._reasoning.append(delta)
        if self.is_mounted:
            self._schedule_paint()

    def finish(self, fallback: str = "") -> None:
        if self._finished:
            return
        self._finished = True
        if self._reasoning_chars and not self._thought_until:
            self._thought_until = time.monotonic()
        if fallback and not "".join(self._parts).strip():
            self._parts = [fallback]
        if self.is_mounted:
            self._render_final()

    def _render_final(self) -> None:
        text = "".join(self._parts).strip("\n")
        live = self.query_one("#live", Static)
        final = self.query_one("#final", Markdown)
        row = self.query_one("#answer-row", Horizontal)
        live.display = False
        if text.strip():
            final.update(text)
            final.display = True
            row.display = True
        else:
            final.display = False
            row.display = False
        if self._reasoning_chars:
            self._paint_reasoning()
        self.call_after_refresh(self.refresh)

    def answer_text(self) -> str:
        return "".join(self._parts)

    def reasoning_text(self) -> str:
        return "".join(self._reasoning)

    def thought_seconds(self) -> float:
        if not self._thought_from:
            return 0.0
        end = self._thought_until or time.monotonic()
        return max(0.0, end - self._thought_from)

    def is_blank(self) -> bool:
        # Not ``is_empty``: Textual's DOMNode.is_empty is the property behind
        # the ``:empty`` pseudo-class, and a method there is always truthy.
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
        if self._reasoning_chars:
            self._paint_reasoning()
        text = "".join(self._parts).lstrip("\n")
        live = self.query_one("#live", Static)
        row = self.query_one("#answer-row", Horizontal)
        live.display = bool(text)
        row.display = bool(text)
        live.update(Text.assemble((text, TEXT), ("▌", GOLD)) if text else Text(""))

    def _paint_reasoning(self) -> None:
        line = self.query_one("#reasoning", Static)
        line.display = True
        lang = self.language
        if self._finished or self._thought_until:
            if self._summary_mode:
                summary = " ".join(self.reasoning_text().split())
                line.update(
                    Text.assemble(
                        ("∴ ", DIM),
                        (_clip(summary, 600), f"italic {MUTED}"),
                    )
                )
                return
            elapsed = duration_text(self.thought_seconds(), lang)
            line.update(Text("∴ " + tr(lang, "thinking.done", elapsed=elapsed), style=f"italic {DIM}"))
            return
        tail = " ".join(self.reasoning_text()[-400:].split())
        if len(tail) > 160:
            tail = "…" + tail[-159:]
        line.update(
            Text.assemble(
                ("∴ " + tr(lang, "thinking.live"), f"italic {MUTED}"),
                (("\n  " + tail) if tail else "", f"italic {DIM}"),
            )
        )


class ToolCard(HangingStatic):
    """One tool call: ``● Title(target)`` and its ``⎿`` result under it.

    The bullet blinks while the call runs and turns green or red on finish. A
    click expands the line in place with its whole preview and raw facts, and
    a second click folds it again -- no screen of its own.
    """

    def __init__(
        self,
        tool: str,
        call_id: str = "",
        category: str = "other",
        *,
        language: str = "en",
        detail: str = "",
        expanded: bool = False,
    ) -> None:
        self.tool = tool
        self.call_id = call_id
        self.category = category
        self.language = language
        self.detail = detail
        self._started_at = time.monotonic()
        self._ok: bool | None = None
        self._summary = ""
        self._preview = ""
        self._error = ""
        self._duration = 0.0
        self._interrupted = False
        self._blink_on = False
        self._blinker: Any = None
        self.expanded = expanded
        super().__init__(
            self._tool_text(),
            classes=f"tool-card tool-{category}",
            markup=False,
        )

    def on_mount(self) -> None:
        if self._ok is None:
            self._blinker = self.set_interval(0.5, self._blink_tick)

    def _blink_tick(self) -> None:
        if self._ok is not None or self._interrupted:
            if self._blinker is not None:
                self._blinker.stop()
                self._blinker = None
            return
        self._blink_on = not self._blink_on
        self.set_text(self._tool_text())

    @property
    def ok(self) -> bool | None:
        return self._ok

    def finish(
        self, ok: bool | None, summary: str = "", duration: float = 0.0, preview: str = ""
    ) -> None:
        self._ok = ok
        self._summary = summary
        self._preview = preview
        self._duration = duration or (time.monotonic() - self._started_at)
        if self._blinker is not None:
            self._blinker.stop()
            self._blinker = None
        self.set_text(self._tool_text())

    def note_error(self, message: str) -> None:
        """The refusal or failure text of this call, shown on its ``⎿`` line."""

        self._error = message
        self.set_text(self._tool_text())

    def interrupt(self) -> None:
        """The run ended before this call reported back: stop the blinking."""

        if self._ok is not None:
            return
        self._interrupted = True
        self._duration = time.monotonic() - self._started_at
        if self._blinker is not None:
            self._blinker.stop()
            self._blinker = None
        self._blink_on = False
        self.set_text(self._tool_text())

    def set_expanded(self, value: bool) -> None:
        if self.expanded != value:
            self.expanded = value
            self.set_text(self._tool_text())

    def toggle(self) -> None:
        self.set_expanded(not self.expanded)

    def on_click(self, event: events.Click) -> None:
        self.toggle()
        event.stop()

    def _tool_text(self) -> Text:
        return tool_card_text(
            self.tool,
            self.category,
            self._ok,
            self._summary,
            self._duration,
            language=self.language,
            detail=self.detail,
            preview=self._preview,
            error=self._error,
            expanded=self.expanded,
            blink=self._blink_on,
            call_id=self.call_id,
            interrupted=self._interrupted,
        )


class Notice(HangingStatic):
    """A one-off fact from KaroX itself, hung under the turn, never boxed."""

    def __init__(self, text: str, level: str = "info") -> None:
        self.text = text
        self.level = level
        icon = {"info": ELBOW, "success": "✓", "warning": "!", "error": "✗"}.get(level, ELBOW)
        colour = {"info": DIM, "success": GREEN, "warning": GOLD, "error": RED}.get(level, DIM)
        body = TEXT if level in {"warning", "error"} else MUTED
        lines = text.splitlines() or [""]
        pieces: list[tuple[str, str]] = [(f"{icon}  ", colour), (lines[0], body)]
        for line in lines[1:]:
            pieces.append(("\n   " + line, MUTED))
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
    filter text as a task. Otherwise PageUp/PageDown scroll the transcript,
    Shift+Tab cycles the mode, and ``?`` typed into an empty composer toggles
    the shortcut list instead of being inserted.
    History navigation only engages at the document edges, so editing a
    multi-line draft never loses it: the draft is restored on the way down.
    ``recalled`` is the text the last recall loaded, so the app can tell a
    recalled ``/command`` from one being typed and not pop the palette open.
    """

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class ShortcutsToggled(Message):
        pass

    class ModeCycled(Message):
        pass

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
                # filter text is never a task, but a pasted /command still
                # runs (e.g. "/verify <command>" while the check picker waits).
                draft = str(self.text).strip()  # type: ignore[has-type]
                handled = menu.accept() or (
                    menu.kind != "commands" and not draft.startswith("/")
                )
            elif event.key == "escape":
                menu.cancel()
            else:
                handled = False
            if handled:
                event.prevent_default()
                event.stop()
                return
        if event.key == "shift+tab":
            event.prevent_default()
            event.stop()
            self.post_message(self.ModeCycled())
            return
        if event.character == "?" and not str(self.text):  # type: ignore[has-type]
            event.prevent_default()
            event.stop()
            self.post_message(self.ShortcutsToggled())
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
