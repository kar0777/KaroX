from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from _support import SRC  # noqa: F401
from _tui_harness import isolated_karox_directories

from karox import tui
from karox.paths import session_dir
from karox.sessions import SessionStore


pytestmark = pytest.mark.skipif(not tui._HAS_TEXTUAL, reason="textual is not installed")


class _TypedStore:
    def __init__(self) -> None:
        self.replay_calls: list[int] = []
        self.events = [
            SimpleNamespace(
                sequence=0,
                kind="ToolCallStarted",
                payload={"tool": "repo.read_file", "call_id": "read-1"},
                parent_id=None,
            ),
            SimpleNamespace(
                sequence=1,
                kind="ToolCallCompleted",
                payload={"tool": "repo.read_file", "call_id": "read-1", "ok": True},
                parent_id=None,
            ),
        ]

    def latest_sequence(self, _session_id: str) -> int:
        return 1

    def replay(self, _session_id: str, *, from_sequence: int = 0):
        self.replay_calls.append(from_sequence)
        return [item for item in self.events if item.sequence >= from_sequence]


@pytest.mark.asyncio
async def test_quiet_typed_poll_does_not_replay_the_entire_run() -> None:
    """A no-change 350 ms tick must be a no-op, not a replay from sequence zero."""

    fake = _TypedStore()
    with isolated_karox_directories() as repository:
        with mock.patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(repository, language="ru")
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app.agent_busy = True
                app.active_session = "typed-session"
                with (
                    mock.patch(
                        "karox.transcript_shadow.get_transcript_store",
                        return_value=fake,
                    ),
                    mock.patch.object(app, "_poll_assistant_content"),
                ):
                    app._poll_typed_transcript()
                    app._poll_typed_transcript()

                assert fake.replay_calls == [0]
                assert app._history_seen == 2
                assert "typed-session" in app._typed_activity_sessions


def test_long_prompt_preview_is_compact_but_short_prompt_is_unchanged() -> None:
    short = "Проверь этот файл"
    assert tui._compact_user_message(short, "ru") == short

    long_prompt = "Сделай production-ready интерфейс\n" + "\n".join(
        f"Требование {index}: подробно проверить поведение компонента" for index in range(1, 90)
    )
    ru = tui._compact_user_message(long_prompt, "ru")
    en = tui._compact_user_message(long_prompt, "en")

    assert "Сделай production-ready интерфейс" in ru
    assert "промпт:" in ru and "стр." in ru and "симв." in ru
    assert "prompt:" in en and "lines" in en and "chars" in en
    assert "Требование 89" not in ru
    assert len(ru) < len(long_prompt) // 4


@pytest.mark.asyncio
async def test_large_paste_is_sent_in_full_while_chat_can_render_a_preview() -> None:
    full = "Первая строка\n" + "\n".join(f"деталь {index}" for index in range(80))
    with isolated_karox_directories() as repository:
        with mock.patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(repository, language="ru")
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                composer = app.query_one("#composer", tui.Input)
                marker = app._register_pasted_block(full)
                composer.value = marker
                composer.focus()
                with mock.patch.object(app, "_submit_task") as submit:
                    await pilot.press("enter")
                    await pilot.pause()
                submit.assert_called_once_with(full)


@pytest.mark.asyncio
async def test_short_messages_shrink_to_content_and_progress_has_no_answer_card() -> None:
    with isolated_karox_directories() as repository:
        with mock.patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(repository, language="ru")
            async with app.run_test(size=(120, 36)) as pilot:
                await pilot.pause()
                app._write_user("Короткий вопрос")
                app._write_assistant("Короткий ответ.")
                app._write_progress("Проверю структуру проекта и затем тесты.")
                await pilot.pause(0.2)

                conversation = app.query_one("#conversation", tui.TranscriptView)
                user = list(app.query(".message-user"))[-1]
                assistant = list(app.query(".message-assistant"))[-1]
                progress = list(app.query(".message-progress"))[-1]

                assert user.size.width < conversation.size.width
                assert assistant.size.width < conversation.size.width
                assert progress.size.width < conversation.size.width
                assert "› Проверю структуру" in progress.plain_text
                assert str(progress.styles.border_top[0]) == ""


@pytest.mark.asyncio
async def test_typed_tool_activity_prevents_provider_history_from_drawing_tools_twice() -> None:
    with isolated_karox_directories() as repository:
        store = SessionStore(session_dir())
        store.create(repository, "task", session_id="typed-history")
        lease = store.acquire("typed-history", "test")
        try:
            record = store.load("typed-history")
            record.provider_history.extend(
                [
                    {
                        "role": "assistant",
                        "content": "Сначала проверю структуру проекта.",
                        "tool_calls": [
                            {"call_id": "read-1", "name": "repo.read_file", "arguments": {}}
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "read-1",
                        "core_name": "repo.read_file",
                        "result": {"ok": True},
                    },
                ]
            )
            store.save(record, record.revision, lease)
        finally:
            store.release(lease)

        with mock.patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(repository, language="ru")
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app.agent_busy = True
                app.active_session = "typed-history"
                app._typed_activity_sessions.add("typed-history")
                with (
                    mock.patch.object(app, "_write_progress") as progress,
                    mock.patch.object(app, "_write_assistant") as answer,
                    mock.patch.object(app, "_begin_step") as begin,
                    mock.patch.object(app, "_finish_step") as finish,
                ):
                    app._poll_assistant_content()

                progress.assert_called_once_with("Сначала проверю структуру проекта.")
                answer.assert_not_called()
                begin.assert_not_called()
                finish.assert_not_called()


class _SameFingerprintPath:
    def stat(self):
        return SimpleNamespace(st_mtime_ns=123, st_size=456)


class _HistoryStore:
    def __init__(self) -> None:
        self.histories = {
            "session-a": [
                {"role": "assistant", "content": "A1", "tool_calls": []},
                {"role": "assistant", "content": "A2", "tool_calls": []},
            ],
            "session-b": [
                {"role": "assistant", "content": "B1", "tool_calls": []},
            ],
        }

    def state_path(self, _session_id: str):
        # Deliberately identical stat fingerprints for both sessions. The
        # observer must key fingerprints by session instead of assuming a global
        # mtime/size pair identifies the active history.
        return _SameFingerprintPath()

    def load(self, session_id: str):
        return SimpleNamespace(provider_history=self.histories[session_id])


@pytest.mark.asyncio
async def test_provider_history_cursor_and_fingerprint_are_per_session() -> None:
    fake_store = _HistoryStore()
    with isolated_karox_directories() as repository:
        with mock.patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(repository, language="en")
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app.agent_busy = True
                with (
                    mock.patch.object(tui, "SessionStore", return_value=fake_store),
                    mock.patch.object(app, "_write_assistant") as answer,
                ):
                    app.active_session = "session-a"
                    app._poll_assistant_content()
                    assert [call.args[0] for call in answer.call_args_list] == ["A1", "A2"]

                    answer.reset_mock()
                    app.active_session = "session-b"
                    app._poll_assistant_content()
                    answer.assert_called_once_with("B1")

                    answer.reset_mock()
                    app.active_session = "session-a"
                    app._poll_assistant_content()
                    answer.assert_not_called()

                assert app._provider_history_seen == {"session-a": 2, "session-b": 1}
                assert set(app._provider_history_fingerprints) == {"session-a", "session-b"}
