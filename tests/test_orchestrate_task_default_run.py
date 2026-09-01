from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from karox import tui
from karox.cli_shortcuts import normalize_cli_arguments


def test_cli_bare_orchestrate_executes_by_default() -> None:
    assert normalize_cli_arguments(["orchestrate", "inspect", "the", "project"]) == [
        "orchestrate",
        "run",
        "--objective",
        "inspect the project",
    ]
    assert normalize_cli_arguments(["orchestrate", "plan", "inspect", "the", "project"]) == [
        "orchestrate",
        "plan",
        "--objective",
        "inspect the project",
    ]


@pytest.mark.asyncio
async def test_tui_bare_orchestrate_executes_isolated_team_by_default() -> None:
    root = Path(tempfile.mkdtemp())
    with (
        patch.object(tui, "_load_language", return_value="en"),
        patch.object(tui, "_selected_model", return_value=None),
    ):
        app = tui.KaroXApp(root, language="en")
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            with patch.object(app, "run_worker") as run_worker:
                app._orchestrate_command("inspect the project")
            run_worker.assert_called_once()
            assert run_worker.call_args.kwargs["exclusive"] is True
            execute = run_worker.call_args.args[0]
            with (
                patch.object(tui, "_capture_cli", return_value=(0, "{}")) as capture,
                patch.object(app, "call_from_thread"),
            ):
                execute()
            argv = capture.call_args.args[0]
            assert argv[:2] == ["orchestrate", "run"]
            assert "--isolate-implementers" in argv
