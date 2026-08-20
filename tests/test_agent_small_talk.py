from __future__ import annotations

from types import SimpleNamespace

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.agent import AgentKernel, _is_small_talk_task


def _record(task: str, content: str = "Привет!") -> SimpleNamespace:
    return SimpleNamespace(
        task=task,
        changed_files=[],
        provider_history=[{"role": "assistant", "content": content}],
    )


def test_small_talk_classifier_is_conservative() -> None:
    assert _is_small_talk_task("привет")
    assert _is_small_talk_task("Hello!")
    assert _is_small_talk_task("как дела?")
    assert not _is_small_talk_task("привет, исправь баг в TUI")
    assert not _is_small_talk_task("почему кнопка подключения не работает?")


def test_small_talk_answer_needs_no_repository_basis() -> None:
    kernel = AgentKernel.__new__(AgentKernel)
    kernel.require_change = False

    assert kernel._answer_complete(_record("привет"))
    assert not kernel._answer_complete(_record("что делает sample.txt?"))
