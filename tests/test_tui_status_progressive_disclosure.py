from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from karox import tui


def _status_text(command: str) -> str:
    root = Path(tempfile.mkdtemp())
    with (
        patch.object(tui, "_load_preferences", return_value={}),
        patch.object(tui, "_load_effort_level", return_value="auto"),
        patch.object(tui, "_load_agent_mode", return_value="build"),
        patch.object(tui, "_selected_model", return_value=None),
        patch(
            "karox.build_identity.build_identity",
            return_value=SimpleNamespace(
                summary=lambda: "test-build",
                package_path="C:/internal/karox/package",
            ),
        ),
    ):
        app = tui.KaroXApp(root, language="en")
        with patch.object(app, "_write") as write:
            app._handle_command(command)
        return str(write.call_args.args[0])


def test_status_default_shows_daily_decision_facts_only() -> None:
    text = _status_text("/status")
    for label in ("Project:", "Model:", "Mode:", "Effort:", "Cost profile:", "Task:"):
        assert label in text
    assert "Bridge:[/]" not in text
    assert "Build:[/] test-build" not in text
    assert "Code:[/]" not in text
    assert "test-build" not in text
    assert "C:/internal/karox/package" not in text


def test_status_details_preserves_runtime_diagnostics_for_power_users() -> None:
    text = _status_text("/status details")
    assert "Bridge:[/]" in text
    assert "Build:[/] test-build" in text
    assert "Code:[/] C:/internal/karox/package" in text
