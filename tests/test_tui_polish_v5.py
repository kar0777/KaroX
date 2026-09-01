from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from _support import SRC  # noqa: F401
from _tui_harness import isolated_karox_directories

from karox import tui


@unittest.skipUnless(tui._HAS_TEXTUAL, "textual is not installed")
class TuiPolishV5Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = self.enterContext(isolated_karox_directories())

    async def test_short_repository_question_is_local_and_spends_no_model_call(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.repository, language="ru")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app._transcript().clear()
                with patch.object(app, "run_worker") as run_worker:
                    app._submit_task("в какой папке ты работаешь?")
                await pilot.pause()

                run_worker.assert_not_called()
                self.assertFalse(app.agent_busy)
                text = app._transcript().plain_text
                self.assertIn("Рабочая папка:", text)
                self.assertIn(str(self.repository), text)

    def test_local_status_matcher_is_conservative_and_bilingual(self) -> None:
        self.assertEqual(tui._local_status_query_kind("где ты работаешь?"), "folder")
        self.assertEqual(tui._local_status_query_kind("what working directory are you in?"), "folder")
        self.assertEqual(tui._local_status_query_kind("какую модель ты используешь?"), "model")
        self.assertEqual(tui._local_status_query_kind("what reasoning effort?"), "effort")
        self.assertEqual(
            tui._local_status_query_kind("проверь в какой папке лежит конфиг и исправь его"),
            "",
        )

    def test_large_prompt_is_compact_only_in_the_view(self) -> None:
        prompt = "Сделай production-ready интерфейс\n" + "\n".join(
            f"Требование {index}: подробно реализуй этот пункт" for index in range(1, 30)
        )
        preview = tui._compact_user_message(prompt, "ru")
        self.assertNotEqual(preview, prompt)
        self.assertIn("промпт:", preview)
        self.assertIn("стр.", preview)
        self.assertLess(len(preview), 320)
        self.assertTrue(preview.startswith("Сделай production-ready интерфейс"))

        english = tui._compact_user_message("x" * 900, "en")
        self.assertIn("prompt:", english)
        self.assertIn("chars", english)
        self.assertLess(len(english), 320)

        medium_single_line = "x" * 300
        medium_preview = tui._compact_user_message(medium_single_line, "en")
        self.assertNotEqual(medium_preview, medium_single_line)
        self.assertLessEqual(len(medium_preview.splitlines()[0]), 120)
        self.assertIn("prompt:", medium_preview)

    def test_progress_preview_prefers_one_complete_public_sentence(self) -> None:
        preview = tui._compact_progress_text(
            "I found the provider wiring. Next I will inspect five implementation files and compare several alternatives before editing."
        )
        self.assertEqual(preview, "I found the provider wiring.")
        self.assertNotIn("Next I will", preview)

    async def test_large_single_line_paste_becomes_marker_but_expands_losslessly(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.repository, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                composer = app.query_one("#composer", tui.CommandInput)
                long_prompt = "single line requirement " * 80
                event = SimpleNamespace(
                    text=long_prompt,
                    prevent_default=Mock(),
                    stop=Mock(),
                )
                composer._on_paste(event)
                self.assertIn("[paste #", composer.value)
                self.assertIn("chars]", composer.value)
                self.assertLess(len(composer.value), 80)
                expanded = app._expand_pasted_blocks(composer.value)
                self.assertEqual(expanded, long_prompt)
                self.assertFalse(app._pasted_blocks)
                event.prevent_default.assert_called_once_with()
                event.stop.assert_called_once_with()

    async def test_app_targeted_paste_event_reaches_focused_composer(self) -> None:
        """Real Textual dispatch must not depend on Paste targeting CommandInput."""
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.repository, language="ru")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                composer = app.query_one("#composer", tui.CommandInput)
                composer.focus()

                long_prompt = "Первая строка\n" + "\n".join(
                    f"требование {index}" for index in range(1, 60)
                )
                app.post_message(tui.events.Paste(long_prompt))
                await pilot.pause()

                self.assertIn("[вставка #", composer.value)
                with patch.object(app, "_submit_task") as submit:
                    await pilot.press("enter")
                    await pilot.pause()
                submit.assert_called_once_with(long_prompt)
                self.assertFalse(app._pasted_blocks)

                composer.clear()
                app.post_message(tui.events.Paste("короткая вставка"))
                await pilot.pause()
                self.assertEqual(composer.value, "короткая вставка")

    async def test_short_message_bubble_does_not_span_the_conversation(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.repository, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                transcript = app._transcript()
                transcript.clear()
                block = transcript.add_user("short", title="You")
                await pilot.pause()
                self.assertLess(block.size.width, transcript.size.width)

    async def test_tool_call_preamble_is_progress_not_a_second_answer(self) -> None:
        state = self.repository / "fake-session.json"
        state.write_text("{}", encoding="utf-8")
        history = [
            {
                "role": "assistant",
                "content": "I will inspect the relevant files first.",
                "tool_calls": [{"name": "repo.search", "call_id": "call-1"}],
            },
            {"role": "assistant", "content": "Final answer.", "tool_calls": []},
        ]
        fake_store = SimpleNamespace(
            state_path=lambda _session_id: state,
            load=lambda _session_id: SimpleNamespace(provider_history=history),
        )
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.repository, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app.agent_busy = True
                app.active_session = "session-a"
                app._typed_activity_sessions = set()
                with (
                    patch.object(tui, "SessionStore", return_value=fake_store),
                    patch.object(app, "_write_progress") as progress,
                    patch.object(app, "_write_assistant") as answer,
                    patch.object(app, "_begin_step") as begin,
                ):
                    app._poll_assistant_content()

                progress.assert_called_once_with("I will inspect the relevant files first.")
                answer.assert_called_once_with("Final answer.")
                begin.assert_called_once()

    async def test_quiet_transcript_poll_does_not_replay_history_from_zero(self) -> None:
        class FakeTranscriptStore:
            def __init__(self) -> None:
                self.replays: list[int] = []

            def latest_sequence(self, _session_id: str) -> int:
                return 0

            def replay(self, _session_id: str, *, from_sequence: int):
                self.replays.append(from_sequence)
                return ()

        fake = FakeTranscriptStore()
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.repository, language="ru")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app.agent_busy = True
                app.active_session = "session-a"
                app._history_seen = 0
                with (
                    patch("karox.transcript_shadow.get_transcript_store", return_value=fake),
                    patch.object(app, "_poll_assistant_content"),
                ):
                    app._poll_typed_transcript()
                    app._poll_typed_transcript()

                self.assertEqual(fake.replays, [0])
                self.assertEqual(app._history_seen, 1)

    async def test_closed_activity_groups_never_become_transcript_spam(self) -> None:
        class FakeStream:
            def __init__(self) -> None:
                self.finished = 0
                self.drained = 0

            def finish(self) -> None:
                self.finished += 1

            def pop_closed(self):
                self.drained += 1
                return (SimpleNamespace(kind="investigating"),)

        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.repository, language="ru")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                stream = FakeStream()
                app._activity_group_stream = stream
                with patch.object(app, "_write_progress") as progress:
                    app._flush_activity_groups(finished=True)
                self.assertEqual(stream.finished, 1)
                self.assertEqual(stream.drained, 1)
                progress.assert_not_called()

    def test_live_progress_uses_coding_cli_wording_not_repeated_history_wording(self) -> None:
        russian = tui._activity_text(
            tui.ActivityAction(kind=tui.ACTIVITY_SEARCHING), english=False
        )
        english = tui._activity_text(
            tui.ActivityAction(kind=tui.ACTIVITY_EDITING), english=True
        )
        self.assertIn("Ищу нужное место", russian)
        self.assertIn("Updating code", english)
        self.assertNotIn("Изучает проект", russian)


if __name__ == "__main__":
    unittest.main()
