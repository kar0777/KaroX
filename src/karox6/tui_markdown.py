"""The model's answers as formatted terminal text, rendered while they stream.

Text is rendered a whole line at a time (the line is final once its newline has
arrived); the open line can be shown as it grows (:meth:`Markdown.partial`) and is
replaced by its rendering when it ends, so formatting never jumps around mid-line.
Tables are held until their last row, then drawn with aligned columns.

What it does: headings (bold, coloured; the ``#`` marks stay, dimmed), bullet and
numbered lists, block quotes, rules, ``inline code``, **bold**, *italic*, links, fenced
code with syntax colours (karox6.tui_highlight) and tables. The markdown signs that
carry meaning when copied (fences, ``#``) stay in the text; without colours the text is
passed through unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from karox6.tui_text import _ANSI, Palette, cell_width, clip_cells

_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([^`\s]*)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)([-*+])\s+(\[[ xX]\]\s+)?(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d{1,3}[.)])\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_INLINE = re.compile(
    r"(?P<code>`+)(?P<code_text>.+?)(?P=code)"
    r"|\*\*(?P<bold>[^*]+?)\*\*|__(?P<bold2>[^_]+?)__"
    r"|(?<![\w*])\*(?P<italic>[^*\s][^*]*?)\*(?![\w*])"
    r"|(?<![\w_])_(?P<italic2>[^_\s][^_]*?)_(?![\w_])"
    r"|\[(?P<link>[^\]]+)\]\((?P<url>[^)\s]+)\)"
)


class Markdown:
    def __init__(
        self,
        paint: Palette,
        glyph: dict[str, str],
        columns: Callable[[], int] | None = None,
    ) -> None:
        self.paint = paint
        self.glyph = glyph
        self.columns = columns or (lambda: 100)
        self._open = ""  # the line still being streamed
        self._code: str | None = None  # the open fence ("```"), None outside code
        self._family: str | None = None
        self._table: list[str] = []

    # -- streaming --------------------------------------------------------------------

    def feed(self, text: str) -> str:
        """Add streamed text; returns the rendering of the lines it completed."""
        if not self.paint.enabled:
            return text  # no colours: the text as it came
        self._open += text
        if "\n" not in self._open:
            return ""
        *done, self._open = self._open.split("\n")
        return "".join(self._line(line) for line in done)

    def partial(self) -> str:
        """The open line as it stands (inline formatting only), for a live preview."""
        if not self.paint.enabled or not self._open:
            return ""
        if self._code is not None:
            return self._open
        if self._table or _TABLE_ROW.match(self._open + "|"):
            return self._open
        return self._inline(self._open)

    def flush(self) -> str:
        """The end of the answer: render what is left, close what is open."""
        if not self.paint.enabled:
            return ""
        rest = ""
        if self._open:
            rest = self._line(self._open, newline=False)
            self._open = ""
        rest += self._end_table()
        self._code = None
        self._family = None
        return rest

    # -- lines ------------------------------------------------------------------------

    def _line(self, line: str, newline: bool = True) -> str:
        end = "\n" if newline else ""
        p = self.paint
        if self._code is not None:
            fence = _FENCE.match(line)
            if (
                fence
                and fence.group(2)[0] == self._code[0]
                and len(fence.group(2)) >= len(self._code)
            ):
                self._code = None
                return p("subtle", line) + end
            from karox6.tui_highlight import highlight

            return highlight(line, self._family, p) + end
        if self._table:
            if _TABLE_ROW.match(line):
                self._table.append(line)
                return ""
            return self._end_table() + self._line(line, newline)
        if _TABLE_ROW.match(line) and newline:
            self._table.append(line)
            return ""
        fence = _FENCE.match(line)
        if fence:
            from karox6.tui_highlight import language

            self._code = fence.group(2)
            self._family = language(fence.group(3))
            return p("subtle", line) + end
        heading = _HEADING.match(line)
        if heading:
            marks = heading.group(1)
            role = "heading" if len(marks) <= 2 else "bold"
            return p("subtle", marks + " ") + self._inline(heading.group(2), role) + end
        if _RULE.match(line):
            return p("subtle", "─" * max(min(self.columns() - 1, 72), 3)) + end
        bullet = _BULLET.match(line)
        if bullet:
            indent, _mark, task, text = bullet.groups()
            box = ""
            if task:
                done = task.strip().lower() == "[x]"
                box = p("success" if done else "muted", task.strip()) + " "
            mark = p("accent", self.glyph.get("bullet", "-"))
            return f"{indent}{mark} {box}{self._inline(text)}{end}"
        numbered = _NUMBERED.match(line)
        if numbered:
            indent, number, text = numbered.groups()
            return f"{indent}{p('accent', number)} {self._inline(text)}{end}"
        quote = _QUOTE.match(line)
        if quote:
            bar = p("subtle", self.glyph.get("pipe", "| "))
            return bar + self._inline(quote.group(1), "muted+italic") + end
        return self._inline(line) + end

    def _inline(self, text: str, base: str = "") -> str:
        p = self.paint
        out: list[str] = []
        pos = 0

        def plain(chunk: str) -> None:
            if chunk:
                out.append(p(base, chunk) if base else chunk)

        def join(role: str) -> str:
            return f"{base}+{role}" if base else role

        for match in _INLINE.finditer(text):
            plain(text[pos : match.start()])
            pos = match.end()
            groups = match.groupdict()
            if groups["code_text"] is not None:
                out.append(p(join("code"), groups["code_text"]))
            elif groups["bold"] is not None or groups["bold2"] is not None:
                out.append(p(join("bold"), groups["bold"] or groups["bold2"]))
            elif groups["italic"] is not None or groups["italic2"] is not None:
                out.append(p(join("italic"), groups["italic"] or groups["italic2"]))
            elif groups["link"] is not None:
                out.append(p(join("accent"), groups["link"]))
                if groups["url"] != groups["link"]:
                    out.append(p("muted", f" ({groups['url']})"))
        plain(text[pos:])
        return "".join(out)

    # -- tables -----------------------------------------------------------------------

    def _end_table(self) -> str:
        rows, self._table = self._table, []
        if not rows:
            return ""
        if len(rows) < 2 or not _TABLE_RULE.match(rows[1]):
            return "".join(self._inline(row) + "\n" for row in rows)
        aligns = [_align(cell) for cell in _cells(rows[1])]
        body = [_cells(row) for row in [rows[0], *rows[2:]]]
        count = max(len(r) for r in body)
        cells = [[self._inline(c) for c in r] + [""] * (count - len(r)) for r in body]
        aligns += ["<"] * (count - len(aligns))
        widths = [max(_width(r[i]) for r in cells) for i in range(count)]
        room = self.columns() - 1 - 3 * (count - 1)
        while sum(widths) > room and max(widths) > 1:
            widest = widths.index(max(widths))
            widths[widest] -= 1
        p = self.paint
        sep = p("subtle", " │ ")
        out = []
        for n, row in enumerate(cells):
            parts = []
            for i, cell in enumerate(row):
                shown = _fit(cell, widths[i])
                pad = widths[i] - _width(shown)
                if aligns[i] == ">":
                    shown = " " * pad + shown
                elif aligns[i] == "^":
                    shown = " " * (pad // 2) + shown + " " * (pad - pad // 2)
                else:
                    shown = shown + " " * pad
                parts.append(p("bold", shown) if n == 0 else shown)
            out.append(sep.join(parts).rstrip() + "\n")
            if n == 0:
                out.append(p("subtle", "─┼─".join("─" * w for w in widths)) + "\n")
        return "".join(out)


def _cells(row: str) -> list[str]:
    text = row.strip()
    text = text[1:] if text.startswith("|") else text
    text = text[:-1] if text.endswith("|") and not text.endswith("\\|") else text
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", text)]


def _align(cell: str) -> str:
    cell = cell.strip()
    if cell.startswith(":") and cell.endswith(":"):
        return "^"
    return ">" if cell.endswith(":") else "<"


def _width(painted: str) -> int:
    return cell_width(_ANSI.sub("", painted))


def _fit(painted: str, width: int) -> str:
    if _width(painted) <= width:
        return painted
    return clip_cells(_ANSI.sub("", painted), width)
