from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from _support import SRC  # noqa: F401
from _tui_harness import isolated_karox_directories
from karox import tui


pytestmark = pytest.mark.skipif(not tui._HAS_TEXTUAL, reason="textual is not installed")


def test_reasoning_phase_is_visible_without_exposing_chain_of_thought() -> None:
    line = tui._activity_text(
        tui.ActivityAction(kind=tui.ACTIVITY_REASONING, elapsed_seconds=8.4),
        english=False,
    )
    assert line == "Обдумываю задачу · 8 с"
    assert "reasoning" not in line.casefold()


def test_provider_failure_is_not_mislabeled_as_verification_failure() -> None:
    line = tui._activity_text(
        tui.ActivityAction(
            kind=tui.ACTIVITY_FAILED,
            reason="provider_error:malformed_response",
        ),
        english=False,
    )
    assert line.startswith("Ошибка ответа модели")
    assert "Проверка не прошла" not in line


@pytest.mark.asyncio
async def test_machine_provider_code_becomes_human_message() -> None:
    with isolated_karox_directories() as repository:
        with patch.object(
            tui,
            "_selected_model",
            return_value=SimpleNamespace(provider_id="openrouter", model_id="stealth/ox-alpha"),
        ):
            app = tui.KaroXApp(repository, language="ru")
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                text = app._friendly_provider_message("provider_error:malformed_response")
                assert "provider_error" not in text
                assert "Ответ модели" in text


@pytest.mark.asyncio
async def test_major_progress_phases_persist_once_like_a_coding_cli() -> None:
    with isolated_karox_directories() as repository:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(repository, language="ru")
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app._transcript().clear()
                app._reset_activity()
                for kind in (
                    tui.ACTIVITY_REASONING,
                    tui.ACTIVITY_READING,
                    tui.ACTIVITY_SEARCHING,
                    tui.ACTIVITY_READING,
                    tui.ACTIVITY_EDITING,
                    tui.ACTIVITY_TESTING,
                    tui.ACTIVITY_BROWSER,
                ):
                    app._set_activity_kind(kind)
                await pilot.pause()

                progress = [item.plain_text for item in app.query(".message-progress")]
                # Generic reasoning stays only on the live elapsed row. Persisted
                # progress is bounded to three useful rows; the current phase
                # updates the third rather than growing a tool-history wall.
                assert progress == [
                    "› Смотрю нужные места в коде",
                    "› Вношу изменения",
                    "› Проверяю результат в браузере",
                ]


@pytest.mark.asyncio
async def test_reasoning_summary_fragments_update_one_public_progress_row() -> None:
    with isolated_karox_directories() as repository:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(repository, language="en")
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app._transcript().clear()
                app._reset_activity()

                app._write_reasoning_summary_delta(1, "Inspecting the ")
                app._write_reasoning_summary_delta(1, "relevant layout.")
                await pilot.pause()
                progress = list(app.query(".message-progress"))
                assert len(progress) == 1
                assert progress[0].plain_text == "› Inspecting the relevant layout."

                app._write_reasoning_summary_delta(2, "Checking browser output.")
                await pilot.pause()
                progress = list(app.query(".message-progress"))
                assert len(progress) == 2
                assert progress[-1].plain_text == "› Checking browser output."


def test_failure_copy_names_provider_and_project_checks_separately() -> None:
    provider = tui._activity_text(
        tui.ActivityAction(
            kind=tui.ACTIVITY_FAILED,
            reason="provider_error:malformed_response",
        ),
        english=False,
    )
    verification = tui._activity_text(
        tui.ActivityAction(kind=tui.ACTIVITY_FAILED, reason="unverified_changes"),
        english=False,
    )
    assert provider.startswith("Ошибка ответа модели")
    assert "Проверки проекта" not in provider
    assert verification.startswith("Проверки проекта не прошли")
    assert "/sessions" in provider
    assert "/sessions" in verification
