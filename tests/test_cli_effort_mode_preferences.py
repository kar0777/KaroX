from __future__ import annotations

import json
from pathlib import Path

from karox import tui
from karox.cli import main


def test_shell_cli_effort_uses_same_persisted_preference_as_tui(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    prefs = tmp_path / "ui.json"
    monkeypatch.setattr(tui, "_preferences_path", lambda: prefs)

    assert main(["effort", "extra-high", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["effort"] == "extra-high"
    assert "Architecture" in payload["summary"]
    assert "72 steps" not in payload["summary"]
    assert tui._load_effort_level() == "extra-high"

    assert main(["effort", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["effort"] == "extra-high"

    assert main(["effort", "extra-high", "--details", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "72 steps" in payload["summary"]
    assert "verification rung 4/5" in payload["summary"]


def test_shell_cli_mode_uses_same_persisted_preference_as_tui(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    prefs = tmp_path / "ui.json"
    monkeypatch.setattr(tui, "_preferences_path", lambda: prefs)

    assert main(["mode", "ideate", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "ideate"
    assert tui._load_agent_mode() == "ideate"

    assert main(["mode", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "ideate"
