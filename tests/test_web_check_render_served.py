"""`karox.web_check --render` must judge a module page the way a grader loads it.

2026-11-10 (HARD20 1017 arena): render_page opened pages from ``file://``,
where Chromium blocks ``<script type="module">`` by CORS, so a working 3D page
was reported as a one-colour scene plus a console error. The shared browser
also called playwright_chromium_status() after starting sync_playwright() in
the same thread, which raised and made every render "playwright_unavailable".
Served pages then reported Chromium's automatic /favicon.ico 404 as an error.
"""

from __future__ import annotations

import urllib.request

import pytest

from karox.static_server import serve_repository


def test_missing_favicon_is_204_but_real_files_and_404s_are_unchanged(tmp_path) -> None:
    (tmp_path / "index.html").write_text("<p>x</p>", encoding="utf-8")
    with serve_repository(tmp_path) as base:
        with urllib.request.urlopen(base + "favicon.ico") as response:
            assert response.status == 204
            assert response.read() == b""
        with urllib.request.urlopen(base + "index.html") as response:
            assert response.status == 200
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(base + "missing.js")
        assert missing.value.code == 404


def test_repository_favicon_is_served_as_is(tmp_path) -> None:
    (tmp_path / "favicon.ico").write_bytes(b"ICO")
    with serve_repository(tmp_path) as base:
        with urllib.request.urlopen(base + "favicon.ico") as response:
            assert response.status == 200
            assert response.read() == b"ICO"


def test_render_page_runs_module_pages_over_loopback(tmp_path) -> None:
    from karox.browser_bootstrap import playwright_chromium_status
    from karox.web_check import check

    if not playwright_chromium_status().ready:
        pytest.skip("managed chromium is not provisioned")
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><head><title>t</title></head><body>"
        "<canvas id='c' width='200' height='200'></canvas>"
        "<script type='module' src='app.js'></script></body></html>",
        encoding="utf-8",
    )
    (tmp_path / "app.js").write_text(
        "const c = document.getElementById('c').getContext('2d');"
        "for (let i = 0; i < 20; i++) { c.fillStyle = `hsl(${i * 18},80%,50%)`;"
        " c.fillRect(i * 10, (i * 7) % 180, 10, 20); }",
        encoding="utf-8",
    )
    verdict = check(tmp_path, [tmp_path / "index.html"], render=True)
    render = verdict["render"]["index.html"]
    assert "skipped" not in render, render
    assert not render.get("console_errors"), render
    assert not any("CORS" in p or "console error" in p for p in verdict["problems"]), verdict
