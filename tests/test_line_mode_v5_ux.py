from __future__ import annotations

import io
from pathlib import Path

from karox import tui


def test_line_mode_effort_and_mode_are_first_class_without_textual(
    tmp_path: Path, monkeypatch
) -> None:
    prefs = tmp_path / "ui.json"
    monkeypatch.setattr(tui, "_preferences_path", lambda: prefs)
    output: list[str] = []

    assert tui._line_local_command("/effort high", output.append)
    assert tui._load_effort_level() == "high"
    assert "Effort: high" in "".join(output)

    output.clear()
    assert tui._line_local_command("/mode plan", output.append)
    assert tui._load_agent_mode() == "plan"
    assert "Mode: plan" in "".join(output)


def test_line_mode_help_keeps_common_surface_small_but_exposes_power_commands() -> None:
    short = io.StringIO()
    tui._line_help(short.write)
    short_text = short.getvalue()
    assert "/models" in short_text
    assert "/effort" in short_text
    assert "/map" in short_text
    assert "/help all" in short_text
    assert "/memory" not in short_text

    full = io.StringIO()
    tui._line_help(full.write, all_commands=True)
    full_text = full.getvalue()
    assert "/memory" in full_text
    assert "/agents" in full_text
    assert "/mission" in full_text
    assert "/commands" in full_text


def test_redirected_line_mode_applies_effort_and_mode_without_model_call(
    tmp_path: Path, monkeypatch
) -> None:
    prefs = tmp_path / "ui.json"
    monkeypatch.setattr(tui, "_preferences_path", lambda: prefs)
    input_stream = io.StringIO("/effort extra-high\n/mode ideate\n/quit\n")
    output_stream = io.StringIO()

    result = tui._run_line_mode(
        tmp_path,
        input_stream=input_stream,
        output_stream=output_stream,
    )

    assert result == 0
    assert tui._load_effort_level() == "extra-high"
    assert tui._load_agent_mode() == "ideate"
    output = output_stream.getvalue()
    assert "Effort: extra-high" in output
    assert "Mode: ideate" in output
    assert "needs the interactive KaroX terminal" not in output
