from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _support import SRC  # noqa: F401

from karox import tui


class _Composer:
    def __init__(self) -> None:
        self.value = ""
        self.cursor_position = 0

    def clear(self) -> None:
        self.value = ""
        self.cursor_position = 0


def _changed(composer: _Composer, value: str) -> SimpleNamespace:
    composer.value = value
    composer.cursor_position = len(value)
    return SimpleNamespace(input=composer, value=value)


def _submitted(composer: _Composer) -> SimpleNamespace:
    return SimpleNamespace(input=composer, value=composer.value)


def test_raw_windows_stream_over_five_kb_collapses_and_submits_losslessly() -> None:
    app = tui.KaroXApp(Path.cwd(), language="ru")
    composer = _Composer()
    first = "abcdefghijkl"
    tail = "продолжение большого промпта " * 220
    full_prompt = first + "\n" + tail

    # Twelve characters arriving as separate events inside a few milliseconds are
    # impossible human typing but match the Windows Terminal fallback mode where
    # bracketed paste is missing and the clipboard becomes ordinary key input.
    times = [1.000 + index * 0.004 for index in range(len(first))]
    with (
        patch.object(app, "_update_command_menu"),
        patch.object(
            tui.time,
            "monotonic",
            side_effect=times + [times[-1] + 0.001] * 6,
        ),
    ):
        value = ""
        for character in first:
            value += character
            app.composer_changed(_changed(composer, value))

    assert app._composer_raw_capture_active is True
    assert composer.value.startswith("[вставка:")
    assert app._composer_raw_capture_buffer == first

    # The next Enter belongs to the pasted document, not to the user's submit.
    with (
        patch.object(app, "_update_command_menu"),
        patch.object(app, "_submit_task") as submit,
        patch.object(tui.time, "monotonic", return_value=times[-1] + 0.004),
    ):
        app.input_submitted(_submitted(composer))
    submit.assert_not_called()
    assert app._composer_raw_capture_buffer == first + "\n"

    # The rest can arrive in one terminal chunk or many small changes. Either way
    # it is kept out of the one-line composer and retained in the hidden buffer.
    target = app._composer_raw_capture_target
    with (
        patch.object(app, "_update_command_menu"),
        patch.object(tui.time, "monotonic", return_value=times[-1] + 0.008),
    ):
        app.composer_changed(_changed(composer, target + tail))

    assert len(app._composer_raw_capture_buffer) > 5_000
    assert "5." in composer.value or "6." in composer.value
    assert composer.value.startswith("[вставка:")

    # A later Enter is the human submit. It finalises the numbered marker, expands
    # it exactly once, and dispatches the complete prompt.
    with (
        patch.object(app, "_update_command_menu"),
        patch.object(app, "_submit_task") as submit,
        patch.object(tui.time, "monotonic", return_value=times[-1] + 1.0),
    ):
        app.input_submitted(_submitted(composer))

    submit.assert_called_once_with(full_prompt)
    assert composer.value == ""
    assert not app._pasted_blocks
    assert app._composer_raw_capture_active is False


def test_one_large_programmatic_value_is_not_misclassified_as_raw_paste() -> None:
    app = tui.KaroXApp(Path.cwd(), language="en")
    composer = _Composer()
    value = "/workspace " + ("x" * 6_000)

    with (
        patch.object(app, "_update_command_menu"),
        patch.object(tui.time, "monotonic", return_value=10.0),
    ):
        app.composer_changed(_changed(composer, value))

    assert not bool(getattr(app, "_composer_raw_capture_active", False))
    assert composer.value == value
    assert int(getattr(app, "_composer_burst_events", 0)) == 1


def test_fast_but_short_human_typing_stays_literal() -> None:
    app = tui.KaroXApp(Path.cwd(), language="en")
    composer = _Composer()
    values = ["h", "he", "hel", "hell", "hello"]

    with patch.object(app, "_update_command_menu"):
        for index, value in enumerate(values):
            with patch.object(tui.time, "monotonic", return_value=20.0 + index * 0.01):
                app.composer_changed(_changed(composer, value))

    assert not bool(getattr(app, "_composer_raw_capture_active", False))
    assert composer.value == "hello"


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
