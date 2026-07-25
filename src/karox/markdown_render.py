"""Custom markdown-to-Rich renderer for assistant chat messages.

Rich's built-in ``rich.markdown.Markdown`` hardcodes a box border around H1
headings and relies on color-only styles for emphasis, so model text in the
KaroX chat was rendered as a frame around ``#`` headers, stripped of bold /
inline-code emphasis, and code blocks lost syntax highlighting.  This module
parses markdown with ``markdown-it-py`` (already a transitive dependency of
Rich) and walks the token stream into Rich renderables directly, giving full
control over each element.  Emphasis uses bold + background styles so it remains
visible in terminals without color support.
"""

from __future__ import annotations

from typing import List, Optional

from markdown_it import MarkdownIt
from markdown_it.token import Token
from rich.console import Group, RenderableType
from rich.rule import Rule
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text


CODE_THEME = "monokai"
_HEADING_COLORS = (
    "#e6e6e6",  # h1
    "#d8d8d8",  # h2
    "#c8c8c8",  # h3
    "#b8b8b8",  # h4
    "#a8a8a8",  # h5
    "#989898",  # h6
)
_INLINE_CODE_STYLE = Style(bgcolor="#3a3a3a", color="#e6e6e6", bold=True)
_QUOTE_STYLE = Style(italic=True, color="#9a9a9a")
_LINK_STYLE = Style(underline=True, color="#6fb3d2")


def _parser() -> MarkdownIt:
    return MarkdownIt().enable("strikethrough").enable("table")


def _inline_text(token: Token) -> Text:
    """Render an inline token's children into one Rich ``Text``."""
    text = Text()
    style_stack: List[Style] = [Style()]
    children = token.children or []

    def current() -> Style:
        merged = Style()
        for item in style_stack:
            merged = merged + item
        return merged

    for child in children:
        ctype = child.type
        if ctype == "text":
            text.append(child.content, style=current())
        elif ctype == "softbreak":
            text.append("\n")
        elif ctype == "hardbreak":
            text.append("\n")
        elif ctype == "code_inline":
            text.append(child.content, style=_INLINE_CODE_STYLE)
        elif ctype == "strong_open":
            style_stack.append(Style(bold=True))
        elif ctype == "strong_close":
            if len(style_stack) > 1:
                style_stack.pop()
        elif ctype == "em_open":
            style_stack.append(Style(italic=True))
        elif ctype == "em_close":
            if len(style_stack) > 1:
                style_stack.pop()
        elif ctype == "s_open":
            style_stack.append(Style(strike=True))
        elif ctype == "s_close":
            if len(style_stack) > 1:
                style_stack.pop()
        elif ctype == "link_open":
            href = ""
            attrs = child.attrs or {}
            if isinstance(attrs, dict):
                href = str(attrs.get("href", ""))
            style_stack.append(_LINK_STYLE)
            if href:
                text.append("", style=Style(link=href))
        elif ctype == "link_close":
            if len(style_stack) > 1:
                style_stack.pop()
        elif ctype == "image":
            attrs = child.attrs or {}
            alt = ""
            if isinstance(attrs, dict):
                alt = str(attrs.get("alt", ""))
            text.append(f"[image: {alt}]" if alt else "[image]", style=_QUOTE_STYLE)
        elif ctype == "text_special" or ctype == "code_inline":
            text.append(child.content, style=_INLINE_CODE_STYLE)
        # Unknown inline children: ignore silently rather than crash.
    return text


def _heading(tag: str, inline: Token) -> Text:
    level = max(1, min(6, int(tag[1:]) if tag[1:].isdigit() else 1))
    color = _HEADING_COLORS[level - 1]
    base = _inline_text(inline)
    base.stylize(Style(bold=True, color=color))
    return base


def _code_block(token: Token) -> Syntax:
    code = token.content.rstrip("\n")
    info = (token.info or "").strip()
    lang = info.split()[0] if info else "text"
    return Syntax(
        code,
        lang,
        theme=CODE_THEME,
        word_wrap=True,
        padding=(0, 1),
        background_color="default",
    )


def render_markdown(text: str) -> List[RenderableType]:
    """Convert markdown text into a list of Rich renderables.

    Returns an empty list for empty input.  Never raises: unhandled tokens fall
    back to their raw content so a malformed model answer still renders.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    tokens = _parser().parse(text)
    out: List[RenderableType] = []
    i = 0
    n = len(tokens)

    def take_inline() -> Optional[Token]:
        nonlocal i
        if i < n and tokens[i].type == "inline":
            tok = tokens[i]
            i += 1
            return tok
        return None

    def skip_block(open_type: str, close_type: str) -> None:
        """Advance past a balanced block opened at the current token."""
        nonlocal i
        depth = 0
        while i < n:
            t = tokens[i]
            i += 1
            if t.type == open_type:
                depth += 1
            elif t.type == close_type:
                depth -= 1
                if depth <= 0:
                    break

    while i < n:
        token = tokens[i]
        i += 1
        ttype = token.type
        if ttype == "heading_open":
            inline = take_inline()
            if inline is not None:
                out.append(_heading(token.tag, inline))
            skip_block("heading_open", "heading_close")
        elif ttype == "paragraph_open":
            inline = take_inline()
            if inline is not None:
                out.append(_inline_text(inline))
            skip_block("paragraph_open", "paragraph_close")
        elif ttype in ("fence", "code_block"):
            out.append(_code_block(token))
        elif ttype == "hr":
            out.append(Rule(style="dim"))
        elif ttype == "bullet_list_open":
            items = _collect_list_lines(tokens, i, ordered=False)
            out.extend(items)
            i = _skip_to_close(tokens, i, "bullet_list_open", "bullet_list_close")
        elif ttype == "ordered_list_open":
            items = _collect_list_lines(tokens, i, ordered=True)
            out.extend(items)
            i = _skip_to_close(tokens, i, "ordered_list_open", "ordered_list_close")
        elif ttype == "blockquote_open":
            quote_lines, i = _collect_quote_lines(tokens, i)
            out.extend(quote_lines)
        elif ttype == "table_open":
            table, i = _collect_table(tokens, i)
            if table is not None:
                out.append(table)
        elif ttype == "inline":
            out.append(_inline_text(token))
        elif ttype == "softbreak" or ttype == "hardbreak":
            continue
        elif token.content:
            out.append(Text(token.content))
    return out


def _skip_to_close(tokens: List[Token], start: int, open_type: str, close_type: str) -> int:
    """Advance past a balanced block (open already consumed) and return the next index."""
    i = start
    n = len(tokens)
    depth = 1
    while i < n and depth > 0:
        t = tokens[i]
        i += 1
        if t.type == open_type:
            depth += 1
        elif t.type == close_type:
            depth -= 1
    return i


def _collect_list_lines(
    tokens: List[Token], start: int, *, ordered: bool, indent: str = ""
) -> List[Text]:
    """Collect a list block into one ``Text`` per printed line, prefixed and indented."""
    lines: List[Text] = []
    i = start
    n = len(tokens)
    counter = 0
    while i < n:
        token = tokens[i]
        if token.type == f"{'ordered' if ordered else 'bullet'}_list_close":
            i += 1
            break
        if token.type == "list_item_open":
            i += 1
            counter += 1
            prefix = f"{counter}. " if ordered else "• "
            item_lines, i = _collect_item_lines(tokens, i)
            for j, line in enumerate(item_lines):
                if j == 0:
                    lines.append(Text(indent + prefix).append(line))
                else:
                    lines.append(Text(indent + "  ").append(line))
        else:
            i += 1
    return lines


def _collect_item_lines(tokens: List[Token], start: int) -> tuple[List[Text], int]:
    """Collect one list item's lines (text + nested lists indented two spaces)."""
    lines: List[Text] = []
    i = start
    n = len(tokens)
    while i < n:
        token = tokens[i]
        if token.type == "list_item_close":
            i += 1
            break
        if token.type == "paragraph_open":
            i += 1
            if i < n and tokens[i].type == "inline":
                lines.append(_inline_text(tokens[i]))
                i += 1
            while i < n and tokens[i].type != "paragraph_close":
                i += 1
            if i < n and tokens[i].type == "paragraph_close":
                i += 1
        elif token.type == "bullet_list_open":
            i += 1
            nested = _collect_list_lines(tokens, i, ordered=False, indent="  ")
            i = _skip_to_close(tokens, i, "bullet_list_open", "bullet_list_close")
            lines.extend(nested)
        elif token.type == "ordered_list_open":
            i += 1
            nested = _collect_list_lines(tokens, i, ordered=True, indent="  ")
            i = _skip_to_close(tokens, i, "ordered_list_open", "ordered_list_close")
            lines.extend(nested)
        elif token.type == "inline":
            lines.append(_inline_text(token))
            i += 1
        else:
            i += 1
    return lines, i


def _collect_quote_lines(tokens: List[Token], start: int) -> tuple[List[Text], int]:
    lines: List[Text] = []
    i = start
    n = len(tokens)
    while i < n:
        token = tokens[i]
        i += 1
        if token.type == "blockquote_close":
            break
        if token.type == "paragraph_open" and i < n and tokens[i].type == "inline":
            inline = tokens[i]
            i += 1
            for sub in _inline_lines(inline):
                line = sub
                line.stylize(_QUOTE_STYLE)
                lines.append(Text("  ").append(line))
            while i < n and tokens[i].type != "paragraph_close":
                i += 1
            if i < n and tokens[i].type == "paragraph_close":
                i += 1
    return lines, i


def _inline_lines(token: Token) -> List[Text]:
    """Split an inline token into one ``Text`` per softbreak/hardbreak line."""
    parts: List[Text] = []
    current = Text()
    for child in token.children or []:
        if child.type in ("softbreak", "hardbreak"):
            parts.append(current)
            current = Text()
        else:
            _append_child(current, child)
    parts.append(current)
    return parts


def _append_child(text: Text, child: Token) -> None:
    """Append one inline child to ``text`` applying inline emphasis."""
    ctype = child.type
    if ctype == "text":
        text.append(child.content)
    elif ctype == "code_inline":
        text.append(child.content, style=_INLINE_CODE_STYLE)
    else:
        text.append(child.content)


def _collect_table(tokens: List[Token], start: int) -> tuple[Optional[Table], int]:
    table = Table(show_header=True, expand=True)
    header: Optional[List[str]] = None
    rows: List[List[str]] = []
    current_row: Optional[List[str]] = None
    i = start
    n = len(tokens)
    while i < n:
        token = tokens[i]
        i += 1
        if token.type == "table_close":
            break
        if token.type == "thead_open":
            header = []
        elif token.type == "tr_open":
            current_row = []
        elif token.type == "th_open":
            cell, i = _read_cell(tokens, i)
            if header is not None:
                header.append(cell)
        elif token.type == "td_open":
            cell, i = _read_cell(tokens, i)
            if current_row is not None:
                current_row.append(cell)
        elif token.type == "tr_close":
            if header is not None and not table.columns:
                for name in header:
                    table.add_column(Text(name, style=Style(bold=True)))
                header = None
            elif current_row is not None:
                rows.append(current_row)
            current_row = None
    for row in rows:
        table.add_row(*[Text(cell) for cell in row])
    if not table.columns:
        return None, i
    return table, i


def _read_cell(tokens: List[Token], start: int) -> tuple[str, int]:
    parts: List[str] = []
    i = start
    n = len(tokens)
    while i < n:
        token = tokens[i]
        i += 1
        if token.type in ("th_close", "td_close"):
            break
        if token.type == "inline":
            for child in token.children or []:
                if child.type == "text":
                    parts.append(child.content)
                elif child.type in ("softbreak", "hardbreak"):
                    parts.append(" ")
        elif token.content:
            parts.append(token.content)
    return "".join(parts).strip(), i


def render_message(message: str) -> RenderableType:
    """Render a model message into a single panel body renderable.

    Falls back to the raw text when the markdown renderer yields nothing, so a
    plain-text answer is never lost.
    """
    renderables = render_markdown(message)
    if not renderables:
        return Text(message)
    if len(renderables) == 1:
        return renderables[0]
    return Group(*renderables)


__all__ = ["CODE_THEME", "render_markdown", "render_message"]
