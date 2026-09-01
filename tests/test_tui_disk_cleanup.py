from __future__ import annotations

from unittest.mock import patch

import pytest

from _support import SRC  # noqa: F401
from _tui_harness import karox_app, visible_text
from karox import tui


pytestmark = pytest.mark.skipif(not tui._HAS_TEXTUAL, reason="textual is not installed")


def test_agent_argv_passes_every_protected_workspace_root() -> None:
    argv = tui._agent_argv(
        "clean junk",
        tui.Path("C:/"),
        (),
        "session",
        protected_paths=("C:/work/a", "D:/projects/b"),
    )
    pairs = [
        (argv[index], argv[index + 1])
        for index in range(len(argv) - 1)
        if argv[index] == "--protected-path"
    ]
    assert pairs == [
        ("--protected-path", "C:/work/a"),
        ("--protected-path", "D:/projects/b"),
    ]


@pytest.mark.asyncio
async def test_cleanup_plan_opens_compact_human_confirmation() -> None:
    plan = {
        "plan_id": "a" * 32,
        "root": "C:/",
        "total_bytes": 5 * 1024**3,
        "file_count": 1200,
        "target_count": 3,
        "targets": [{"data_risk": "rebuildable"}],
        "risk_level": "rebuildable",
        "affected_apps": ["Chrome", "npm"],
        "code_projects": ["site"],
    }
    async with karox_app(model=None, language="ru") as (app, pilot):
        plan["root"] = str(app.repository)
        with patch.object(app, "_pending_cleanup_plan", return_value=plan):
            assert app._offer_cleanup_plan("session") is True
            await pilot.pause()
            assert isinstance(app.screen, tui.ConfirmScreen)
            text = visible_text(app)
            assert "Удалится:" in text
            assert "Chrome" in text
            assert "site" in text
            assert "Необратимо" in text
            # The impact copy itself stays bounded to four useful lines; the
            # Textual modal adds border/button rows around it.
            assert len(tui.cleanup_impact_preview(plan, language="ru").splitlines()) <= 4
            assert len([line for line in text.splitlines() if line.strip()]) <= 16
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, tui.ConfirmScreen)


@pytest.mark.asyncio
async def test_cleanup_approval_calls_local_apply_with_project_protection() -> None:
    result = {"status": "completed", "removed_target_count": 2}
    async with karox_app(model=None, language="en") as (app, pilot):
        with (
            patch.object(app, "_maintenance_protected_paths", return_value=("C:/project",)),
            patch.object(tui, "apply_cleanup_plan", return_value=result) as apply,
        ):
            app._on_cleanup_plan_answer(True, "b" * 32, str(app.repository))
            await pilot.pause(0.2)
            apply.assert_called_once_with(
                "b" * 32,
                root=str(app.repository),
                confirmed_by_user=True,
                protected_roots=("C:/project",),
            )
            assert app.query_one("#composer").disabled is False
            assert "Cleanup finished" in app._transcript().plain_text
