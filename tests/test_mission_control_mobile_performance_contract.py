from __future__ import annotations

from karox import mission_control_server


def test_mobile_mission_control_uses_slow_visibility_aware_polling() -> None:
    html = mission_control_server._HTML
    assert "setInterval(refresh,5000)" in html
    assert "if(document.hidden)return" in html
    assert "visibilitychange" in html
    assert "setInterval(refresh,1000)" not in html
    assert "setInterval(refresh,500)" not in html
    assert "requestAnimationFrame(refresh)" not in html


def test_mobile_mission_control_only_fetches_new_screenshot_artifacts() -> None:
    html = mission_control_server._HTML
    assert "id===shownScreenshot" in html
    assert "id===pendingScreenshot" in html
    assert "refreshScreenshot(d.latest_screenshot_artifact_id||'')" in html
    assert "i.src='/api/screenshot?v='+encodeURIComponent(id)" in html


def test_mobile_mission_control_does_not_embed_credentials_in_navigation() -> None:
    html = mission_control_server._HTML
    pair_html = mission_control_server._PAIR_HTML
    for text in (html, pair_html):
        assert "api_key=" not in text.casefold()
        assert "token=" not in text.casefold()
        assert "credential=" not in text.casefold()
