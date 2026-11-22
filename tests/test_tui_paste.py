"""The compact placeholder a long paste leaves in the input line (karox6.tui_paste)."""

from karox6.tui_paste import MIN_BYTES, MIN_LINES, WARN_BYTES, PasteChunks, pasted_summary


def test_small_pastes_stay_text_but_long_ones_become_a_placeholder():
    pastes = PasteChunks()
    assert pastes.offer("one\ntwo\nthree") == "one\ntwo\nthree"  # a few lines: as typed
    assert not pastes

    data = "\n".join(f"line {i}" for i in range(MIN_LINES + 1))
    shown = pastes.offer(data)
    assert shown.startswith("[pasted ")
    assert shown.endswith("]")
    assert str(MIN_LINES + 1) in shown
    assert pastes


def test_the_placeholder_summarizes_lines_and_bytes():
    data = "\n".join("x" * 60 for _ in range(5000))  # ~300 KB
    assert pasted_summary(data) == "5,000 lines · 298 KB"


def test_expand_restores_typing_around_the_placeholder():
    pastes = PasteChunks()
    data = "\n".join(f"row {i}" for i in range(50))
    shown = pastes.offer(data)
    text = f"before {shown} and after"
    assert pastes.expand(text) == f"before {data} and after"
    # a half-deleted placeholder stays literal text; its payload goes nowhere
    assert pastes.expand(shown[:-4]) == shown[:-4]


def test_two_equal_pastes_get_distinct_placeholders_and_both_expand():
    pastes = PasteChunks()
    data = "a\n" * 20
    first, second = pastes.offer(data), pastes.offer(data)
    assert first != second
    assert pastes.expand(f"{first} / {second}") == f"{data} / {data}"


def test_a_paste_of_few_lines_but_many_bytes_also_collapses():
    pastes = PasteChunks()
    data = "x" * (MIN_BYTES + 10)  # one long line
    assert pastes.offer(data).startswith("[pasted 1 line ·")
    # the warn threshold is what the UI asks a second Enter for
    assert WARN_BYTES == 2 * 1024 * 1024
    assert pastes.expand(pastes.offer("y\n" * 3)) == "y\n" * 3  # small: not stored


def test_clear_forgets_everything():
    pastes = PasteChunks()
    shown = pastes.offer("\n".join(f"l{i}" for i in range(40)))
    pastes.clear()
    assert not pastes
    assert pastes.expand(shown) == shown  # nothing to expand into any more


# -- merge_pasted_runs: a paste a console delivers as plain key events -------------------


def _presses(text: str):
    """The KeyPresses a console makes of ``text`` (chars, ControlJ / ControlM)."""
    from prompt_toolkit.key_binding.key_processor import KeyPress
    from prompt_toolkit.keys import Keys

    out = []
    for char in text:
        if char == "\n":
            out.append(KeyPress(Keys.ControlJ, "\n"))
        elif char == "\r":
            out.append(KeyPress(Keys.ControlM, "\r"))
        else:
            out.append(KeyPress(char, char))
    return out


def test_a_key_burst_of_many_lines_merges_into_one_paste():
    from prompt_toolkit.keys import Keys

    from karox6.tui_input import merge_pasted_runs

    out, merged = merge_pasted_runs(_presses("one\ntwo\nthree"), hot=False)
    assert merged
    assert len(out) == 1
    assert out[0].key == Keys.BracketedPaste
    assert out[0].data == "one\ntwo\nthree"


def test_typed_text_with_enter_at_the_end_does_not_merge():
    from karox6.tui_input import merge_pasted_runs

    keys = _presses("hello\r")  # the \r is a person pressing Enter, it submits
    out, merged = merge_pasted_runs(keys, hot=False)
    assert not merged
    assert out == keys

    keys = _presses("alpha\nbeta\r")  # one line end inside, Enter last
    out, merged = merge_pasted_runs(keys, hot=False)
    assert not merged
    assert out == keys


def test_a_long_single_line_burst_merges_without_newlines():
    from prompt_toolkit.keys import Keys

    from karox6.tui_input import merge_pasted_runs

    out, merged = merge_pasted_runs(_presses("x" * 40), hot=False)
    assert merged
    assert out[0].key == Keys.BracketedPaste
    assert out[0].data == "x" * 40


def test_the_paste_tail_merges_while_hot_and_a_stray_marker_drops():
    from karox6.tui_input import merge_pasted_runs

    out, merged = merge_pasted_runs(_presses("tail\n"), hot=True)
    assert merged
    assert out[0].data == "tail\n"
    # ConPTY leaves the paste-end marker behind as literal text: it is dropped.
    out, merged = merge_pasted_runs(_presses("[201~"), hot=True)
    assert not merged
    assert out == []


def test_an_enter_right_after_the_paste_end_marker_submits():
    from prompt_toolkit.keys import Keys

    from karox6.tui_input import merge_pasted_runs

    # The marker arrives with the paste tail, an Enter in the same batch stays a key.
    out, still_open = merge_pasted_runs(_presses("tail\n[201~\r"), hot=True)
    assert not still_open
    assert [p.key for p in out] == [Keys.BracketedPaste, Keys.ControlM]
    assert out[0].data == "tail\n"
    # A short paste tail that only the marker proves is still merged.
    out, still_open = merge_pasted_runs(_presses("ab[201~"), hot=False)
    assert not still_open
    assert [(p.key, p.data) for p in out] == [(Keys.BracketedPaste, "ab")]


def test_real_keys_split_a_paste_run_but_stay_keys():
    from prompt_toolkit.key_binding.key_processor import KeyPress
    from prompt_toolkit.keys import Keys

    from karox6.tui_input import merge_pasted_runs

    keys = [
        *_presses("one\ntwo\nthree"),
        KeyPress(Keys.Escape, "\x1b"),
        *_presses("four\n"),
    ]
    out, merged = merge_pasted_runs(keys, hot=False)
    assert merged
    kinds = [p.key for p in out]
    assert kinds == [Keys.BracketedPaste, Keys.Escape, "f", "o", "u", "r", Keys.ControlJ]
