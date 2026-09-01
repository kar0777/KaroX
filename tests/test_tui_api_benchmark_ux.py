from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401

from karox import tui


def test_task_token_counter_is_compact_and_truthful() -> None:
    assert tui._token_counter_text() == "in— out— cache—"
    assert (
        tui._token_counter_text(12_345, 3_100, 8_700, cache_reported=True)
        == "in12.3k out3.1k cache8.7k"
    )
    assert (
        tui._token_counter_text(12_345, 3_100, 0, cache_reported=False)
        == "in12.3k out3.1k cache—"
    )


def test_narrow_header_keeps_model_and_usage_counter() -> None:
    usage = "in12.3k out3.1k cache8.7k"
    line = tui._header_line(
        repository="KaroX-v5",
        model="openrouter/stealth/ox-alpha",
        activity="",
        width=46,
        effort="auto",
        usage=usage,
        language="ru",
    )
    assert "stealth/ox-alpha" in line
    assert usage in line
    assert len(line) <= 46


def test_standard_header_keeps_usage_counter() -> None:
    usage = "in12.3k out3.1k cache8.7k"
    line = tui._header_line(
        repository="KaroX-v5",
        model="openrouter/stealth/ox-alpha",
        activity="completed",
        width=80,
        effort="auto",
        usage=usage,
        language="en",
    )
    assert "openrouter/stealth/ox-alpha" in line
    assert usage in line
    assert len(line) <= 80


def test_wide_header_keeps_project_effort_and_usage() -> None:
    line = tui._header_line(
        repository="KaroX-v5",
        model="openrouter/stealth/ox-alpha",
        activity="completed",
        width=160,
        effort="auto",
        economy=True,
        usage="in120 out45 cache64",
        language="en",
    )
    assert "@KaroX-v5" in line
    assert "effort auto" in line
    assert "in120 out45 cache64" in line


def test_resumed_task_does_not_inherit_old_cache_provenance() -> None:
    app = tui.KaroXApp(Path.cwd(), language="en")
    app.active_session = "session-a"
    app._usage_session_id = "session-a"
    old_event = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "cache_read_tokens": 80,
        "cache_metrics_reported": True,
    }
    app._usage_baseline = {
        "requests": 1,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "cache_read_tokens": 80,
        "events": [old_event],
    }
    current = {
        "requests": 2,
        "prompt_tokens": 300,
        "completion_tokens": 70,
        "cache_read_tokens": 80,
        "events": [
            old_event,
            {
                "prompt_tokens": 200,
                "completion_tokens": 50,
                "cache_read_tokens": 0,
                "cache_metrics_reported": False,
            },
        ],
    }
    with patch.object(app, "_session_usage", return_value=current):
        assert app._task_usage_text() == "in200 out50 cache—"


def test_measured_zero_cache_is_not_rendered_as_unavailable() -> None:
    app = tui.KaroXApp(Path.cwd(), language="en")
    app.active_session = "session-a"
    app._usage_session_id = "session-a"
    app._usage_baseline = {"requests": 0, "events": []}
    current = {
        "requests": 1,
        "prompt_tokens": 200,
        "completion_tokens": 50,
        "cache_read_tokens": 0,
        "events": [
            {
                "prompt_tokens": 200,
                "completion_tokens": 50,
                "cache_read_tokens": 0,
                "cache_metrics_reported": True,
            }
        ],
    }
    with patch.object(app, "_session_usage", return_value=current):
        assert app._task_usage_text() == "in200 out50 cache0"


def test_ready_welcome_is_one_line_in_both_languages() -> None:
    for language in ("ru", "en"):
        assert "\n" not in tui._TEXT[language]["welcome_ready"]
        assert "/models" in tui._TEXT[language]["welcome_ready"]
        assert "/effort" in tui._TEXT[language]["welcome_ready"]
