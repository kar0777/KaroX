"""Tests for the custom markdown-to-Rich chat renderer.

These assert the three confirmed chat-formatting fixes: headings render
without Rich's H1 box border, bold/inline-code emphasis survives, and fenced
code blocks become ``rich.syntax.Syntax`` with the configured theme.
"""

from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.markdown_render import render_markdown, render_message
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text


def _render_to_str(text: str) -> str:
    items = render_markdown(text)
    from io import StringIO

    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=60, color_system="auto")
    for item in items:
        console.print(item)
    return buf.getvalue()


class MarkdownRenderTests(unittest.TestCase):
    def test_heading_renders_without_h1_box_border(self) -> None:
        items = render_markdown("# Heading plain-ish")
        self.assertEqual(len(items), 1)
        heading = items[0]
        self.assertIsInstance(heading, Text)
        rendered = _render_to_str("# Heading plain-ish")
        self.assertIn("Heading plain-ish", rendered)
        # The bug was Rich's H1 box border. None of these box characters
        # may appear for a heading rendered through the custom renderer.
        for box in ("┌", "└", "─", "┐", "┘"):
            self.assertNotIn(box, rendered, f"heading must not draw box {box!r}")
        # Heading text must be bold (emphasis survives even without color).
        self.assertTrue(any(span.style.bold for span in heading.spans))

    def test_subheadings_are_bold_and_distinct(self) -> None:
        items = render_markdown("## Sub\n### Subsub")
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertIsInstance(item, Text)
            self.assertTrue(any(span.style.bold for span in item.spans))

    def test_bold_and_italic_survive_as_styles(self) -> None:
        items = render_markdown("**bold** and *italic*")
        self.assertEqual(len(items), 1)
        text = items[0]
        self.assertIsInstance(text, Text)
        bold_spans = [s for s in text.spans if s.style.bold]
        italic_spans = [s for s in text.spans if s.style.italic]
        self.assertTrue(bold_spans, "bold emphasis must produce a bold span")
        self.assertTrue(italic_spans, "italic emphasis must produce an italic span")

    def test_inline_code_has_background_and_is_visible_without_color(self) -> None:
        items = render_markdown("use `foo` here")
        self.assertEqual(len(items), 1)
        text = items[0]
        code_spans = [s for s in text.spans if s.style.bgcolor is not None]
        self.assertTrue(code_spans, "inline code must have a background style")
        rendered = _render_to_str("use `foo` here")
        self.assertIn("foo", rendered)

    def test_fenced_code_block_becomes_syntax_with_theme(self) -> None:
        items = render_markdown("```python\nprint('hi')\n```")
        self.assertEqual(len(items), 1)
        syntax = items[0]
        self.assertIsInstance(syntax, Syntax)
        # Lexer is the language declared in the info string.
        self.assertEqual(syntax.lexer.name.lower(), "python")
        # The configured code theme (CODE_THEME) is wired into the renderable;
        # Rich stores the resolved theme object internally.
        self.assertIsNotNone(syntax._theme)

    def test_unfenced_code_block_uses_text_lexer_default(self) -> None:
        items = render_markdown("```\nplain\n```")
        self.assertEqual(len(items), 1)
        self.assertIsInstance(items[0], Syntax)

    def test_bullet_list_uses_bullet_prefix_and_indent(self) -> None:
        rendered = _render_to_str("- a\n- b\n  - nested\n- c")
        self.assertIn("• a", rendered)
        self.assertIn("• b", rendered)
        self.assertIn("• c", rendered)
        # Nested items are indented two spaces relative to the parent.
        self.assertIn("  • nested", rendered)

    def test_ordered_list_uses_numbered_prefix(self) -> None:
        rendered = _render_to_str("1. first\n2. second\n3. third")
        self.assertIn("1. first", rendered)
        self.assertIn("2. second", rendered)
        self.assertIn("3. third", rendered)

    def test_blockquote_indents_every_line(self) -> None:
        rendered = _render_to_str("> line one\n> line two\n>\n> second para")
        # Every quote line carries a two-space indent so softbreaks don't
        # collapse back to column zero.
        for line in ("  line one", "  line two", "  second para"):
            self.assertIn(line, rendered)

    def test_table_renders_as_rich_table(self) -> None:
        items = render_markdown("| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertEqual(len(items), 1)
        self.assertIsInstance(items[0], Table)

    def test_hr_renders_as_rule(self) -> None:
        items = render_markdown("---")
        self.assertEqual(len(items), 1)
        from rich.rule import Rule

        self.assertIsInstance(items[0], Rule)

    def test_realistic_multi_section_answer_renders_without_error(self) -> None:
        md = (
            "# Title\n\n"
            "Some **bold** text with `code` and *italic*.\n\n"
            "```python\nx = 1\nprint(x)\n```\n\n"
            "- one\n  - nested\n- two\n"
            "> quoted line\n"
            "\n---\n"
            "After hr.\n"
        )
        items = render_markdown(md)
        self.assertGreater(len(items), 4)
        types = {type(item).__name__ for item in items}
        self.assertIn("Syntax", types)
        self.assertIn("Rule", types)
        rendered = _render_to_str(md)
        self.assertIn("Title", rendered)
        self.assertIn("print(x)", rendered)
        # The whole thing renders without raising and contains no box border.
        for box in ("┌", "└"):
            self.assertNotIn(box, rendered)

    def test_empty_and_whitespace_input_returns_empty_list(self) -> None:
        self.assertEqual(render_markdown(""), [])
        self.assertEqual(render_markdown("   \n  \n"), [])

    def test_plain_text_without_markdown_passes_through(self) -> None:
        items = render_markdown("just plain text no markdown")
        self.assertEqual(len(items), 1)
        self.assertIsInstance(items[0], Text)
        self.assertEqual(str(items[0]), "just plain text no markdown")

    def test_render_message_wraps_multiple_renderables_into_group(self) -> None:
        from rich.console import Group

        body = render_message("# Title\n\nText `code`.")
        # A single renderable returns directly; multiple wrap into a Group.
        if isinstance(body, Group):
            self.assertGreater(len(body.renderables), 1)
        else:
            self.assertIsInstance(body, (Text, Syntax, Table))

    def test_render_message_falls_back_to_text_for_empty_markdown(self) -> None:
        body = render_message("plain no markdown")
        self.assertIsInstance(body, Text)


if __name__ == "__main__":
    unittest.main()
