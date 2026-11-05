"""Finish re-checks the page sizes a run measured before its last change.

r2b sandbox round (2026-11-05): a game page fitted a 390x844 window when the
run measured it; a later sizing edit made it 678 px wide there, the run
re-checked only 1440x900, and the delivered page failed its mobile layout
requirement. Finish now re-opens each stale viewport the run itself used and
refuses once when the page scrolls sideways.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from karox.engine.exceptions import Submitted
from karox.engine_tool_env import (
    LAYOUT_REFUSAL_LIMIT,
    CoreToolEnvironment,
    layout_problem,
)
from karox.models import Origin, OriginKind


class _Browser:
    """Answers open/evaluate like the browser tool; widths per viewport."""

    def __init__(self, widths: dict) -> None:
        self.widths = widths
        self.commands: list = []
        self.viewport = None

    def execute(self, command, lease=None):  # noqa: ANN001 - test double
        assert command.name == "browser.command"
        assert lease is None
        self.commands.append(command.arguments)
        action = command.arguments["action"]
        if action == "open":
            payload = command.arguments["payload"]
            self.viewport = (payload["width"], payload["height"])
            return SimpleNamespace(ok=True, data={"action": "open", "viewport": {"width": payload["width"], "height": payload["height"]}})
        if action == "evaluate":
            width, _ = self.viewport
            scroll = self.widths.get(self.viewport, width)
            value = {"scroll_width": scroll, "viewport_width": width}
            if scroll > width:
                value["widest"] = {"element": "canvas#game", "left": 10, "right": scroll - 10, "width": scroll - 20}
            return SimpleNamespace(ok=True, data={"action": "evaluate", "value": value})
        raise AssertionError(action)


def _environment(browser: _Browser) -> CoreToolEnvironment:
    environment = object.__new__(CoreToolEnvironment)
    environment._core = browser
    environment._session_id = "session-1"
    environment._origin = Origin(kind=OriginKind.NATIVE_AGENT, identity="test-agent")
    environment._definitions = {"browser.command": object()}
    environment._aliases = {}
    environment._coverage_refusals = 0
    environment._layout_refusals = 0
    environment._action_seq = 0
    environment._last_change_seq = 0
    environment._viewports = {}
    environment._current_view = None
    environment.config = SimpleNamespace(output_char_limit=20_000)
    environment._kernel = SimpleNamespace(
        remaining_seconds=lambda: 600.0,
        note_coverage_refusal=lambda: None,
        _persist_engine_tool_error=lambda *args, **kwargs: None,
        _emit=lambda *args, **kwargs: None,
    )
    environment._coverage_problems = lambda arguments: []
    return environment


def _open(environment: CoreToolEnvironment, width: int, height: int) -> None:
    environment._action_seq += 1
    environment._note_viewport(
        {"action": "open", "payload": {"page": "index.html", "width": width, "height": height}},
        {"action": "open", "viewport": {"width": width, "height": height}},
    )


def _edit(environment: CoreToolEnvironment) -> None:
    environment._action_seq += 1
    environment._last_change_seq = environment._action_seq


FINISH = {"name": "finish", "tool_call_id": "call-1", "arguments": {"answer": "done"}}


def test_a_viewport_checked_before_the_last_edit_is_checked_again() -> None:
    browser = _Browser({(390, 844): 678})
    environment = _environment(browser)
    _open(environment, 390, 844)
    _edit(environment)  # the sizing edit that broke the narrow layout
    _open(environment, 1440, 900)  # re-checked only the wide size

    refused = environment._finish(FINISH)

    assert refused["returncode"] == 1
    text = refused["output"]
    assert "index.html at 390x844" in text
    assert "678 px wide in a 390 px viewport" in text
    assert "canvas#game" in text
    assert "1440x900" not in text, "a size opened after the last change is fresh"
    opened = [c["payload"] for c in browser.commands if c["action"] == "open"]
    assert opened == [{"page": "index.html", "width": 390, "height": 844}]

    # Bounded: the next finish goes through even if the page still overflows.
    assert LAYOUT_REFUSAL_LIMIT == 1
    with pytest.raises(Submitted):
        environment._finish(FINISH)


def test_a_page_that_fits_or_was_rechecked_finishes_without_extra_work() -> None:
    browser = _Browser({})
    environment = _environment(browser)
    _open(environment, 390, 844)
    _edit(environment)
    with pytest.raises(Submitted):
        environment._finish(FINISH)
    assert [c["action"] for c in browser.commands] == ["open", "evaluate"]

    # Opened again after the last edit: nothing is stale, nothing is opened.
    browser = _Browser({(390, 844): 678})
    environment = _environment(browser)
    _edit(environment)
    _open(environment, 390, 844)
    with pytest.raises(Submitted):
        environment._finish(FINISH)
    assert browser.commands == []


def test_reload_refreshes_the_open_viewport_and_url_pages_are_not_tracked() -> None:
    environment = _environment(_Browser({}))
    _open(environment, 390, 844)
    _edit(environment)
    environment._action_seq += 1
    environment._note_viewport({"action": "reload", "payload": {}}, {"action": "reload"})
    assert environment._viewports[("index.html", 390, 844)] > environment._last_change_seq

    environment._action_seq += 1
    environment._note_viewport(
        {"action": "open", "payload": {"url": "https://example.com/"}}, {"action": "open"}
    )
    assert environment._current_view is None
    assert list(environment._viewports) == [("index.html", 390, 844)]


def test_a_failed_recheck_never_blocks_finish() -> None:
    class _Broken:
        def execute(self, command, lease=None):  # noqa: ANN001 - test double
            raise RuntimeError("browser is gone")

    environment = _environment(_Browser({}))
    environment._core = _Broken()
    _open(environment, 390, 844)
    _edit(environment)
    with pytest.raises(Submitted):
        environment._finish(FINISH)


def test_layout_problem_reads_only_real_overflow() -> None:
    assert layout_problem("index.html", 390, 844, None) == ""
    assert layout_problem("index.html", 390, 844, {"value": {"scroll_width": 391, "viewport_width": 390}}) == ""
    line = layout_problem(
        "index.html", 390, 844, {"value": {"scroll_width": 678, "viewport_width": 390}}
    )
    assert line == "index.html at 390x844: the document is 678 px wide in a 390 px viewport, so the page scrolls sideways"
