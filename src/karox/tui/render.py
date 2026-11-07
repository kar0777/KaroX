"""TUI rendering helpers and palette constants."""

from __future__ import annotations

import io
from typing import Sequence
from rich.cells import cell_len
from rich.console import Console
from rich.text import Text
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

BULLET = "●"

ELBOW = "⎿"

_FACTS_WIDE = 100

_FACTS_MEDIUM = 60

CONTEXT_WARNING_FRACTION = 0.8

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

status_segments = header_segments

_HANG_MARKERS = frozenset("●⎿✓✗!>∴◆☐☒◼")

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

_WRAP_CONSOLE: Console | None = None


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
