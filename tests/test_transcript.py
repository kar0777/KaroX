"""The conversation pane: styled runs, wrapping by terminal cells, incremental updates."""

import random
import time

from karox6.tui_app import MAX_LOGICAL_LINES, _runs, _TranscriptControl, _wrap_fragments
from karox6.tui_text import Palette


def frame(control, width=40, height=10):
    content = control.create_content(width, height)
    return [content.get_line(i) for i in range(content.line_count)]


def text_of(lines):
    return ["".join(text for _, text in line) for line in lines]


def test_colour_codes_become_styled_runs_not_a_fragment_per_character():
    paint = Palette(True)
    runs = _runs(
        paint("dim", "thinking") + " plain " + paint("red", "x error") + paint("bold", "B")
    )
    assert runs == [("dim", "thinking"), ("", " plain "), ("ansired", "x error"), ("bold", "B")]
    assert _runs("\x1b[1;36m> \x1b[0mhello") == [("ansicyan bold", "> "), ("", "hello")]
    assert _runs("no codes") == [("", "no codes")]
    assert _runs("") == []
    # codes the chat never writes still render (through the full parser), merged into runs
    assert _runs("\x1b[38;5;208morange\x1b[0m") == [("#ff8700", "orange")]


def test_a_style_holds_across_the_lines_of_one_write():
    control = _TranscriptControl()
    control.append(Palette(True)("dim", "first thought\nsecond thought") + "\n")
    lines = frame(control)
    assert lines[0] == [("dim", "first thought")]
    assert lines[1] == [("dim", "second thought")]  # was plain: the code sat on line one


def test_wrapping_counts_terminal_cells():
    wrapped = _wrap_fragments([("", "абвгдежзик")], 4)
    assert text_of(wrapped) == ["абвг", "дежз", "ик"]
    # a wide character never straddles the edge
    wrapped = _wrap_fragments([("", "a日本語b")], 4)
    assert text_of(wrapped) == ["a日", "本語", "b"]
    assert text_of(_wrap_fragments([("x", "ab"), ("y", "cdef")], 3)) == ["abc", "def"]
    assert _wrap_fragments([("x", "ab"), ("y", "cdef")], 3)[0] == [("x", "ab"), ("y", "c")]
    assert _wrap_fragments([], 5) == [[]]


def test_tabs_expand_to_stops_of_four_across_writes():
    control = _TranscriptControl()
    control.append("a\tb")
    control.append("\tc\n")
    control.append("\tindented\n")
    assert text_of(frame(control))[:2] == ["a   b   c", "    indented"]


def test_streaming_into_a_line_equals_wrapping_it_at_once():
    rng = random.Random(7)
    paint = Palette(True)
    pieces = []
    alphabet = ["word ", "слово ", "日本", "🙂", "x" * 37, "\n", "\t", "ab\ncd", " "]
    for _ in range(600):
        piece = rng.choice(alphabet)
        if rng.random() < 0.3:
            piece = paint(rng.choice(["dim", "red", "green", "bold"]), piece)
        pieces.append(piece)
    for width in (7, 20, 53):
        live = _TranscriptControl()
        for piece in pieces:
            live.append(piece)
            if rng.random() < 0.5:
                frame(live, width)  # a frame between writes, as while a reply streams
        whole = _TranscriptControl()
        whole.append("".join(pieces))
        assert frame(live, width) == frame(whole, width)
        # and after a resize both re-wrap to the same lines
        assert frame(live, width + 5) == frame(whole, width + 5)


def test_a_long_streamed_paragraph_costs_its_new_text_not_its_length():
    control = _TranscriptControl()
    paint = Palette(True)
    started = time.perf_counter()
    for _ in range(3000):  # 30,000 characters in one unbroken line, a frame per piece
        control.append(paint("dim", "word word "))
        control.create_content(100, 30)
    elapsed = time.perf_counter() - started
    assert control.total_visual_lines == 300
    assert elapsed < 2.0  # was ~30 s: every piece re-parsed and re-wrapped the whole line


def test_old_lines_are_dropped_past_the_cap_and_the_view_stays_consistent():
    control = _TranscriptControl()
    for i in range(MAX_LOGICAL_LINES + 10):
        control.append(f"line {i}\n")
    lines = text_of(frame(control, 30, 5))
    assert len(lines) <= MAX_LOGICAL_LINES
    assert lines[-1] == f"line {MAX_LOGICAL_LINES + 9}"
    assert lines[0] != "line 0"
    control.append("tail")
    assert text_of(frame(control, 30, 5))[-1] == "tail"


def test_a_resize_keeps_the_text_at_the_top_of_a_scrolled_up_view():
    control = _TranscriptControl()
    for n in range(200):
        control.append(f"line {n:03d} " + "word " * (n % 7 * 6) + "\n")
    frame(control, width=80, height=10)
    control.scroll_by(-120)  # reading somewhere in the middle
    top = text_of(frame(control, width=80, height=10))[control.scroll_top]
    assert top.startswith("line ")
    for width in (40, 120, 23, 80):
        rows = text_of(frame(control, width=width, height=10))
        assert rows[control.scroll_top].startswith(top[:8]), (width, top)
    assert not control.follow
