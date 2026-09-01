from __future__ import annotations

from karox.mission_control_server import _HTML


def test_mobile_mission_defaults_to_progress_cost_and_cache() -> None:
    assert "Progress<br>" in _HTML
    assert "Cost<br>" in _HTML
    assert "Cache<br>" in _HTML
    assert "Tokens<br>" not in _HTML
    assert "Context reused<br>" not in _HTML


def test_mobile_mission_hides_raw_endpoint_ids_from_default_cards() -> None:
    assert "a.endpoint_id" not in _HTML
    assert "roleLabels" in _HTML
    assert "Independent reviewer" in _HTML


def test_mobile_mission_reuses_unchanged_screenshot_artifact() -> None:
    assert "id===shownScreenshot||id===pendingScreenshot" in _HTML
    assert "refreshScreenshot(d.latest_screenshot_artifact_id||'')" in _HTML
    assert "?v='+encodeURIComponent(id)" in _HTML
    assert "?t='+Date.now()" not in _HTML


def test_mobile_mission_does_not_poll_while_page_is_hidden() -> None:
    assert "if(document.hidden)return" in _HTML
    assert "visibilitychange" in _HTML
    assert "setInterval(refresh,5000)" in _HTML


def test_mobile_mission_keeps_smart_stop_explanation() -> None:
    assert "Approval requests never bypass KaroX Smart Stop" in _HTML
    assert "Stop is applied only at a safe orchestration boundary" in _HTML
