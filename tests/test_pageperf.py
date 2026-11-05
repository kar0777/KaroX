"""Pure helpers behind the browser's frame, input and evaluate reports."""

from __future__ import annotations

from karox.browser import pageperf


def test_slow_frame_report_only_for_slow_frames() -> None:
    assert pageperf.slow_frame_report({"frame_ms": 16}) is None
    assert pageperf.slow_frame_report({"frame_ms": "junk"}) is None
    assert pageperf.slow_frame_report(None) is None
    report = pageperf.slow_frame_report({"frame_ms": 640, "frames": 4})
    assert report is not None
    assert report["frame_ms"] == 640 and report["slow"] is True
    assert "640 ms" in report["note"]


def test_slow_input_note_reports_duration_past_the_threshold() -> None:
    assert pageperf.slow_input_note("click", 0.4) is None
    note = pageperf.slow_input_note("click", 7.25)
    assert note == {
        "elapsed_ms": 7250,
        "slow": pageperf.SLOW_INPUT_NOTE.format(action="click", seconds=7.25),
    }
    assert "7.2 s" in note["slow"] or "7.3 s" in note["slow"]


def test_evaluate_timeout_is_bounded_by_the_action_deadline() -> None:
    assert pageperf.evaluate_timeout_ms({}, 45.0) == pageperf.DEFAULT_EVALUATE_TIMEOUT_MS
    assert pageperf.evaluate_timeout_ms({"timeout_ms": 300}, 45.0) == 300
    assert pageperf.evaluate_timeout_ms({"timeout_ms": 10}, 45.0) == 250
    assert pageperf.evaluate_timeout_ms({"timeout_ms": "x"}, 45.0) == pageperf.DEFAULT_EVALUATE_TIMEOUT_MS
    # never past the tool deadline (minus a margin), never past 30 s
    assert pageperf.evaluate_timeout_ms({"timeout_ms": 60_000}, 5.0) == 4500
    assert pageperf.evaluate_timeout_ms({"timeout_ms": 60_000}, 120.0) == 29_500


def test_bounded_value_keeps_small_values_and_truncates_large_ones() -> None:
    assert pageperf.bounded_value({"a": [1, 2]}) == {"value": {"a": [1, 2]}}
    large = pageperf.bounded_value("y" * (pageperf.MAX_EVALUATE_CHARS + 50))
    assert large["truncated"] is True
    assert len(large["value_text"]) == pageperf.MAX_EVALUATE_CHARS
    assert large["chars"] == pageperf.MAX_EVALUATE_CHARS + 52  # JSON quotes


def test_sources_wrap_expressions_and_bodies_with_a_timeout() -> None:
    expression = pageperf.expression_source("document.title;;", 1234)
    assert "document.title" in expression and "document.title;" not in expression
    assert "1234" in expression and "Promise.race" in expression
    body = pageperf.body_source("return 1;", 99)
    assert "return 1;" in body and "99" in body


def test_frame_probe_switch(monkeypatch) -> None:
    monkeypatch.delenv("KAROX_BROWSER_FRAME_PROBE", raising=False)
    assert pageperf.frame_probe_enabled() is True
    for off in ("0", "false", "off", "NO"):
        monkeypatch.setenv("KAROX_BROWSER_FRAME_PROBE", off)
        assert pageperf.frame_probe_enabled() is False
