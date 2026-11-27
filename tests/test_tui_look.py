"""How the chat looks: colour roles and depths, glyph sets, markdown answers, code colours,
folding tool output, and the menus of commands and files."""

from __future__ import annotations

import pytest

from karox6.tui import _match_workspace_files, _rank_commands
from karox6.tui_app import _runs, _TranscriptControl
from karox6.tui_highlight import highlight, language
from karox6.tui_markdown import Markdown
from karox6.tui_text import _ANSI, Palette, _glyphs, cell_width, legacy_console, narrow
from karox6.tui_theme import Theme, color_depth, theme_name, xterm256, xterm_hex

GLYPH = _glyphs("utf-8")


def plain(text: str) -> str:
    return _ANSI.sub("", text)


# -- colours ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "platform", "depth"),
    [
        ({"NO_COLOR": "1", "COLORTERM": "truecolor"}, "linux", 0),
        ({"TERM": "dumb"}, "linux", 0),
        ({"COLORTERM": "truecolor"}, "linux", 24),
        ({"WT_SESSION": "x"}, "win32", 24),
        ({"TERM_PROGRAM": "vscode"}, "darwin", 24),
        ({"TERM": "xterm-kitty"}, "linux", 24),
        ({"TERM": "xterm-256color"}, "linux", 8),
        ({}, "win32", 8),
        ({"TERM": "xterm"}, "linux", 4),
        ({"TERM": "xterm-256color", "KAROX_COLOR_DEPTH": "4"}, "linux", 4),
        ({"PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_24_BIT"}, "linux", 24),
    ],
)
def test_colour_depth_of_the_terminal(env: dict[str, str], platform: str, depth: int) -> None:
    assert color_depth(env, platform) == depth


def test_theme_follows_the_setting_then_the_terminal() -> None:
    assert theme_name(None, {}) == "mid"  # background unknown: colours readable on both
    assert theme_name(None, {"COLORFGBG": "15;0"}) == "dark"
    assert theme_name(None, {"COLORFGBG": "0;15"}) == "light"
    assert theme_name("light", {}) == "light"
    assert theme_name("dark", {"KAROX_THEME": "ansi"}) == "ansi"
    assert theme_name("purple", {}) == "mid"


def test_roles_are_written_for_the_depth() -> None:
    dark24 = Theme("dark", 24)
    assert dark24.sgr("accent") == "38;2;122;162;247"
    assert dark24.pt("accent") == "fg:#7aa2f7"
    assert dark24.sgr("add_bg").startswith("48;2;")
    dark8 = Theme("dark", 8)
    assert dark8.sgr("accent") == f"38;5;{xterm256('7aa2f7')}"
    # at 256 colours prompt_toolkit gets the exact palette entry the SGR code names
    assert dark8.pt("accent") == f"fg:#{xterm_hex(xterm256('7aa2f7'))}"
    ansi = Theme("dark", 4)  # 16 colours: the terminal's own palette, whatever the theme
    assert ansi.name == "ansi"
    assert ansi.sgr("accent") == "36"
    assert ansi.sgr("muted") == "2"
    assert ansi.sgr("add_bg") == ""
    assert Theme("mid", 24).sgr("add_bg") == ""  # tinted backgrounds only when known
    assert Theme("dark", 0).sgr("accent") == ""
    assert Palette(True, Theme("light", 24))("error+del_bg", "x").startswith(
        "\x1b[38;2;198;40;40;48;2;"
    )
    assert Palette(False, dark24)("accent", "x") == "x"
    assert Palette(True)("red", "x") == "\x1b[31mx\x1b[0m"  # the old names still work


def test_xterm_palette_round_trip() -> None:
    for hex6 in ("000000", "ffffff", "ff8700", "5fafd7", "808080", "1e3a2a"):
        index = xterm256(hex6)
        assert 16 <= index <= 255
        assert xterm256(xterm_hex(index)) == index


def test_true_colour_and_256_colour_codes_become_runs() -> None:
    text = "\x1b[38;2;122;162;247mblue\x1b[0m \x1b[38;5;208;48;2;30;58;42mtint\x1b[0m"
    assert _runs(text) == [
        ("#7aa2f7", "blue"),
        ("", " "),
        ("#ff8700 bg:#1e3a2a", "tint"),
    ]
    assert _runs("\x1b[1;3;38;2;1;2;3mx\x1b[23my") == [
        ("#010203 bold italic", "x"),
        ("#010203 bold", "y"),
    ]


# -- glyphs ----------------------------------------------------------------------------


def test_glyph_sets_for_the_console_at_hand() -> None:
    assert legacy_console({}, "win32")  # a classic console window
    assert not legacy_console({"WT_SESSION": "1"}, "win32")
    assert not legacy_console({"TERM": "xterm-256color"}, "win32")
    assert not legacy_console({}, "linux")
    basic = _glyphs("utf-8", legacy=True)
    assert basic["ok"] == "√"
    assert basic["fail"] == "×"
    assert basic["spin"] == "·•●•"
    plain_set = _glyphs("ascii")
    assert plain_set["ok"] == "ok"
    assert plain_set["box"] == "ascii"
    for glyphs in (GLYPH, basic, plain_set):
        assert all(narrow(value) for value in glyphs.values())  # one cell each, no tables


# -- markdown --------------------------------------------------------------------------

ANSWER = """\
## Plan

Use `make test` and **then** *ship* it, see [docs](https://example.test/d).

- first
  - nested
- [x] done

3. third

> quoted

---

```python
def f(x):  # comment
    return "s" + 1
```

| name | n |
|:-----|--:|
| a | 1 |
| long name | 22 |

tail without newline"""


def render(text: str, chunk: int, theme: Theme | None = None, width: int = 80) -> str:
    md = Markdown(Palette(True, theme or Theme("dark", 24)), GLYPH, lambda: width)
    out = "".join(md.feed(text[i : i + chunk]) for i in range(0, len(text), chunk))
    return out + md.flush()


def test_markdown_renders_the_same_however_it_streams() -> None:
    whole = render(ANSWER, len(ANSWER))
    for chunk in (1, 3, 7, 40):
        assert render(ANSWER, chunk) == whole


def test_markdown_formats_blocks_and_inline_marks() -> None:
    lines = plain(render(ANSWER, 5)).splitlines()
    assert lines[0] == "Plan"  # a heading without its # marks
    assert lines[2] == "Use make test and then ship it, see docs (https://example.test/d)."
    assert "• first" in lines
    assert "  • nested" in lines
    assert "• [x] done" in lines
    assert "3. third" in lines
    assert "│ quoted" in lines
    assert "─" * 72 in lines
    assert "```python" in lines
    assert lines.count("```") == 1
    assert '    return "s" + 1' in lines
    table = [ln for ln in lines if "│" in ln and "quoted" not in ln]
    assert table == ["name      │  n", "a         │  1", "long name │ 22"]
    assert "──────────┼───" in lines
    assert lines[-1] == "tail without newline"


def test_markdown_colours_the_parts() -> None:
    out = render(ANSWER, 9)
    dark = Theme("dark", 24)
    assert f"\x1b[{dark.sgr('code')}mmake test\x1b[0m" in out
    assert "\x1b[1mthen\x1b[0m" in out
    assert f"\x1b[{dark.sgr('keyword')}mdef\x1b[0m" in out
    assert f"\x1b[{dark.sgr('comment')}m# comment\x1b[0m" in out
    assert f'\x1b[{dark.sgr("string")}m"s"\x1b[0m' in out
    assert f"\x1b[{dark.sgr('heading')}mPlan\x1b[0m" in out


def test_markdown_without_colours_is_the_text_as_it_came() -> None:
    md = Markdown(Palette(False), GLYPH)
    assert md.feed(ANSWER) == ANSWER
    assert md.flush() == ""


def test_markdown_partial_line_and_narrow_tables() -> None:
    md = Markdown(Palette(True, Theme("mid", 24)), GLYPH, lambda: 30)
    assert md.feed("so **far") == ""
    assert plain(md.partial()) == "so **far"
    assert plain(md.feed("** ok\n")) == "so far ok\n"
    assert md.partial() == ""
    wide = (
        "| "
        + " | ".join(["column"] * 6)
        + " |\n|"
        + "---|" * 6
        + "\n| "
        + " | ".join(["x" * 9] * 6)
        + " |\n"
    )
    out = plain(md.feed(wide) + md.flush())
    assert all(cell_width(line) <= 29 for line in out.splitlines())


def test_code_colours_by_language() -> None:
    paint = Palette(True, Theme("ansi", 4))
    assert language("Python") == "python"
    assert language("tsx") == "js"
    assert language("unknown-lang") is None
    assert highlight("x = 1", None, paint) == "x = 1"
    assert highlight("+added", "diff", paint) == "\x1b[32m+added\x1b[0m"
    js = highlight('const x = f("a") // c', "js", paint)
    assert "\x1b[35mconst\x1b[0m" in js
    assert "\x1b[2m// c\x1b[0m" in js
    assert plain(highlight("SELECT a FROM t -- q", "sql", paint)) == "SELECT a FROM t -- q"


# -- folding tool output ---------------------------------------------------------------


def test_blocks_fold_and_unfold_in_place() -> None:
    pane = _TranscriptControl()
    pane.append("before\n")
    pane.append_block("tool  ✓\n  … 3 lines (ctrl+o to expand)\n", "tool  ✓\n  └ a\n    b\n    c\n")
    pane.append("middle\n")
    pane.append_block("other\n", "other\n  └ x\n")
    pane.append("after\n")
    folded = pane.plain_text
    assert folded == ("before\ntool  ✓\n  … 3 lines (ctrl+o to expand)\nmiddle\nother\nafter\n")
    pane.create_content(40, 10)
    assert pane.toggle_blocks()
    assert pane.plain_text == "before\ntool  ✓\n  └ a\n    b\n    c\nmiddle\nother\n  └ x\nafter\n"
    content = pane.create_content(40, 10)
    assert content.line_count == 9  # rewrapped from the first block on
    pane.append_block("new\n", "new\n  └ full\n")  # a block that arrives while unfolded
    assert pane.plain_text.endswith("after\nnew\n  └ full\n")
    pane.toggle_blocks()
    assert pane.plain_text == folded + "new\n"


def test_live_line_shows_after_the_text_until_replaced() -> None:
    pane = _TranscriptControl()
    pane.append("done line\n")
    pane.set_live("stream\x1b[1mbold")
    content = pane.create_content(40, 5)
    assert content.line_count == 2
    assert "".join(t for _s, t, *_ in content.get_line(1)) == "streambold"
    pane.set_live("")
    assert pane.create_content(40, 5).line_count == 1


# -- menus -----------------------------------------------------------------------------


def test_command_menu_ranks_prefix_then_name_then_description() -> None:
    names = [c.name for c in _rank_commands("/")]
    assert names[0] == "/new"
    assert "/update" in names
    assert [c.name for c in _rank_commands("/re")][:3] == ["/resume", "/redo", "/reasoning"]
    assert "/undo" in [c.name for c in _rank_commands("/do")]  # inside the name
    assert "/diff" in [c.name for c in _rank_commands("/chang")]  # from its description


def test_file_menu_matches_prefix_name_substring_then_letters_in_order() -> None:
    files = ["src/karox6/tui_app.py", "src/karox6/tui.py", "docs/guide.md", "tests/test_tui.py"]
    assert _match_workspace_files(files, "src/") == ["src/karox6/tui_app.py", "src/karox6/tui.py"]
    assert _match_workspace_files(files, "guide")[0] == "docs/guide.md"
    assert _match_workspace_files(files, "tuiap") == ["src/karox6/tui_app.py"]
    assert _match_workspace_files(files, "zz") == []


def test_tinted_lines_run_to_the_edge_at_any_width() -> None:
    line = "\x1b[32;48;2;30;58;42m+ added\x1b[K\x1b[0m\n"
    assert _runs(line) == [("ansigreen bg:#1e3a2a class:fill", "+ added"), ("", "\n")]
    pane = _TranscriptControl()
    pane.append("plain\n" + line)
    for width in (40, 12):
        content = pane.create_content(width, 5)
        texts = ["".join(f[1] for f in content.get_line(i)) for i in range(content.line_count)]
        assert texts[0] == "plain"  # untouched
        assert texts[1] == "+ added".ljust(width)  # padded to the edge, never wrapped
    assert pane.plain_text == "plain\n+ added\n"
