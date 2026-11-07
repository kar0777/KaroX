"""Offline, browser-free tests for Mission 14 visual pipeline efficiency and safety."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from karox import web_check
from karox.browser_bootstrap import BrowserBootstrapStatus
from karox.providers import ModelResponse
from karox.visual_check import VisualChecker
from karox.web_check import (
    _FRAME_GATE_JS,
    _PAGE_PROBE_JS,
    _PIXEL_STATS_JS,
    _RESIZE_PROBE_JS,
    _SVG_FIT_JS,
    RENDER_SETTLE_MS,
    observe_page,
    render_page,
    render_problems,
)


class _FakeView:
    """In-memory fake Playwright Page recording waits, frame-gate calls and probes."""

    def __init__(
        self,
        *,
        probe_data: dict[str, Any] | None = None,
        measured_data: dict[str, Any] | None = None,
        resize_after: dict[str, Any] | None = None,
        gate_frames: list[int | None] | None = None,
        wait_for_function_raises: bool = False,
        probe_raises: bool = False,
        shots: list[bytes] | None = None,
        console_messages: list[tuple[str, str]] | None = None,
        page_errors: list[str] | None = None,
    ) -> None:
        self.probe_data = (
            dict(probe_data)
            if probe_data is not None
            else {
                "canvases": 0,
                "canvas_rect": None,
                "paints_with_three": False,
                "page_background": "rgb(255, 255, 255)",
                "dynamic_dom": False,
            }
        )
        self.measured_data = (
            dict(measured_data)
            if measured_data is not None
            else {
                "sampled": 100,
                "structured": 25,
                "background_like": 10,
                "tones": 8,
                "dominant_color": "255,255,255",
                "dominant_share": 0.2,
                "canvas_rect": self.probe_data.get("canvas_rect"),
                "luma_mean": 0.65,
                "luma_std": 0.18,
                "dark_bottom_share": 0.05,
                "bottom_sampled": 25,
                "bottom_structured_share": 0.2,
            }
        )
        self.resize_after = (
            dict(resize_after)
            if resize_after is not None
            else {
                "innerWidth": 1000,
                "innerHeight": 700,
                "canvas": {
                    "clientWidth": 1000,
                    "clientHeight": 700,
                    "width": 1000,
                    "height": 700,
                },
            }
        )
        self.gate_frames = list(gate_frames) if gate_frames is not None else [2, 2, 4, 4, 5]
        self.wait_for_function_raises = wait_for_function_raises
        self.probe_raises = probe_raises
        self.shots = list(shots) if shots is not None else [b"shot-1", b"shot-2"]
        self.console_messages = list(console_messages or [])
        self.initial_page_errors = list(page_errors or [])
        self.timeouts: list[int] = []
        self.wait_functions: list[tuple[Any, int]] = []
        self.gate_actions: list[str] = []
        self.screenshots_taken: int = 0
        self.viewports: list[dict[str, int]] = []
        self.closed = False
        self._listeners: dict[str, list[Any]] = {}

    def on(self, event: str, handler: Any) -> None:
        self._listeners.setdefault(event, []).append(handler)

    def add_init_script(self, script: str) -> None:
        assert script == _FRAME_GATE_JS

    def goto(self, url: str) -> None:
        self.url = url
        for kind, text in self.console_messages:
            msg = type("_Msg", (), {"type": kind, "text": text})()
            for handler in self._listeners.get("console", ()):
                handler(msg)
        for err in self.initial_page_errors:
            for handler in self._listeners.get("pageerror", ()):
                handler(err)

    def wait_for_timeout(self, ms: int) -> None:
        self.timeouts.append(ms)

    def wait_for_function(self, js: str, *, arg: Any = None, polling: int = 100, timeout: int = 0) -> None:
        del js, polling
        self.wait_functions.append((arg, timeout))
        if self.wait_for_function_raises:
            raise TimeoutError("wait_for_function timed out")

    def evaluate(self, js: str, arg: Any = None) -> Any:
        if js == _SVG_FIT_JS:
            return True
        if "__karoxFrameGate" in js:
            action = str(arg)
            self.gate_actions.append(action)
            return self.gate_frames.pop(0) if self.gate_frames else 0
        if js == _PAGE_PROBE_JS:
            if self.probe_raises:
                raise RuntimeError("page probe failed")
            return dict(self.probe_data)
        if js == _PIXEL_STATS_JS:
            return dict(self.measured_data)
        if js == _RESIZE_PROBE_JS:
            return dict(self.resize_after)
        return None

    def screenshot(self) -> bytes:
        idx = min(self.screenshots_taken, len(self.shots) - 1)
        self.screenshots_taken += 1
        return self.shots[idx]

    def set_viewport_size(self, size: dict[str, int]) -> None:
        self.viewports.append(dict(size))

    def close(self) -> None:
        self.closed = True


class _FakeProvider:
    def __init__(self, answer: str = '{"verdict": "pass", "issues": []}') -> None:
        self.answer = answer
        self.requests: list[Any] = []

    def complete(self, request: Any) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            content=self.answer,
            tool_calls=(),
            finish_reason="stop",
            usage={"prompt_tokens": 100, "completion_tokens": 10},
        )


def _install_fake_browser(monkeypatch: pytest.MonkeyPatch, view: _FakeView) -> None:
    monkeypatch.setattr(
        "karox.browser_bootstrap.playwright_chromium_status",
        lambda: BrowserBootstrapStatus(
            ready=True,
            package_available=True,
            chromium_available=True,
            executable="/fake/chromium",
            error=None,
        ),
    )
    monkeypatch.setattr(web_check, "_run_in_browser_thread", lambda work: work())

    class _Browser:
        def new_page(self, viewport: dict[str, int]) -> _FakeView:
            view.viewports.append(dict(viewport))
            return view

    monkeypatch.setattr(web_check, "_shared_browser", lambda: ("fake-pw", _Browser()))


def test_is_static_document_requires_clean_markup_without_scripts_animations_or_external_assets(
    tmp_path: Path,
) -> None:
    is_static_document = getattr(web_check, "is_static_document", None)
    assert callable(is_static_document), "is_static_document helper is missing"
    clean_svg = tmp_path / "pelican.svg"
    clean_svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<circle cx="50" cy="50" r="40" fill="#2266aa"/></svg>',
        encoding="utf-8",
    )
    assert is_static_document(clean_svg) is True

    scripted_svg = tmp_path / "scripted.svg"
    scripted_svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        "<script>requestAnimationFrame(() => {});</script><rect width='10' height='10'/></svg>",
        encoding="utf-8",
    )
    assert is_static_document(scripted_svg) is False

    animated_svg = tmp_path / "animated.svg"
    animated_svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<circle cx="50" cy="50" r="20"><animate attributeName="r" from="10" to="40" dur="1s"/></circle>'
        "</svg>",
        encoding="utf-8",
    )
    assert is_static_document(animated_svg) is False

    css_anim_svg = tmp_path / "css_anim.svg"
    css_anim_svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        "<style>@keyframes pulse { from { opacity: 0.5; } to { opacity: 1; } }</style>"
        "<rect width='20' height='20'/></svg>",
        encoding="utf-8",
    )
    assert is_static_document(css_anim_svg) is False

    external_svg = tmp_path / "external.svg"
    external_svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<image href="https://example.com/pic.png" width="50" height="50"/></svg>',
        encoding="utf-8",
    )
    assert is_static_document(external_svg) is False

    clean_html = tmp_path / "static.html"
    clean_html.write_text(
        "<!doctype html><html><head><style>body { color: #111; }</style></head>"
        "<body><h1>Report</h1><p>Static content.</p></body></html>",
        encoding="utf-8",
    )
    assert is_static_document(clean_html) is True

    canvas_html = tmp_path / "canvas.html"
    canvas_html.write_text(
        "<!doctype html><html><body><canvas id='c'></canvas></body></html>",
        encoding="utf-8",
    )
    assert is_static_document(canvas_html) is False


def test_quiescent_static_document_skips_unconditional_fixed_waits_and_second_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svg = tmp_path / "diagram.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">'
        '<rect x="10" y="10" width="180" height="80" fill="#336699"/></svg>',
        encoding="utf-8",
    )
    view = _FakeView(gate_frames=[0])
    _install_fake_browser(monkeypatch, view)

    obs = observe_page(svg, settle_ms=2000, check_resize=True)

    assert view.timeouts == []
    assert len(obs["frames"]) == 1
    assert obs["static_proven"] is True
    assert obs["gate_status"] == "ready"
    assert obs["timings"]["unconditional_wait_ms"] == 0


def test_dynamic_3d_canvas_retains_stabilization_second_frame_and_resize_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = tmp_path / "scene.html"
    page.write_text(
        "<!doctype html><html><body><canvas></canvas>"
        "<script type='module'>import * as THREE from 'https://esm.sh/three';</script>"
        "</body></html>",
        encoding="utf-8",
    )
    view = _FakeView(
        probe_data={
            "canvases": 1,
            "canvas_rect": {"x": 0, "y": 0, "width": 1280, "height": 800},
            "paints_with_three": True,
            "page_background": "rgb(0, 0, 0)",
            "dynamic_dom": True,
        },
        gate_frames=[2, 2, 4, 4, 5],
    )
    _install_fake_browser(monkeypatch, view)

    obs = observe_page(page, settle_ms=2000, check_resize=True)

    assert RENDER_SETTLE_MS in view.timeouts
    assert 2000 in view.timeouts
    assert len(obs["frames"]) == 2
    assert obs["static_proven"] is False
    assert obs["resize"]["ok"] is True
    assert view.timeouts == [RENDER_SETTLE_MS, 2000]


def test_frame_gate_timeout_or_absent_gate_is_recorded_and_never_fabricates_static_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svg = tmp_path / "slow.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="50" height="50"/></svg>',
        encoding="utf-8",
    )
    view = _FakeView(
        gate_frames=[None, None, None],
        wait_for_function_raises=True,
    )
    _install_fake_browser(monkeypatch, view)

    obs = observe_page(svg, settle_ms=2000, check_resize=False)

    assert obs["static_proven"] is False
    assert obs["gate_status"] in {"timed_out", "gate_unavailable"}
    assert len(obs["frames"]) == 2


def test_probe_error_fails_closed_and_reports_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = tmp_path / "broken.html"
    page.write_text("<!doctype html><html><body><h1>Broken</h1></body></html>", encoding="utf-8")
    view = _FakeView(probe_raises=True)
    _install_fake_browser(monkeypatch, view)

    rendered = render_page(page)

    assert "error" in rendered
    assert "probe_error" in rendered["error"]
    assert any("could not be rendered" in item for item in rendered["problems"])


def test_still_rendering_outcome_when_gate_returns_fewer_than_target_frames_on_dynamic_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = tmp_path / "slow_3d.html"
    page.write_text(
        "<!doctype html><html><body><canvas></canvas><script>requestAnimationFrame(() => {});</script></body></html>",
        encoding="utf-8",
    )
    view = _FakeView(
        probe_data={
            "canvases": 1,
            "canvas_rect": {"x": 0, "y": 0, "width": 1280, "height": 800},
            "paints_with_three": True,
            "page_background": "rgb(0, 0, 0)",
            "dynamic_dom": True,
        },
        gate_frames=[1, 1, 1],
    )
    _install_fake_browser(monkeypatch, view)

    obs = observe_page(page, settle_ms=2000, check_resize=False)

    assert obs["static_proven"] is False
    assert obs["gate_status"] == "still_rendering"
    assert len(obs["frames"]) == 2


def test_visual_checker_sends_single_frame_and_honest_text_when_static_proven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svg = tmp_path / "pelican.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<circle cx="50" cy="50" r="40" fill="#2266aa"/></svg>',
        encoding="utf-8",
    )
    view = _FakeView(gate_frames=[0], shots=[b"static-png"])
    _install_fake_browser(monkeypatch, view)
    provider = _FakeProvider('{"verdict": "pass", "issues": []}')
    checker = VisualChecker(
        provider=provider,  # type: ignore[arg-type]
        model="test-model",
        artifacts=tmp_path / "visual",
        vision=True,
        goal="Draw an SVG pelican.",
    )

    result = checker(svg)

    assert result["verdict"] == "pass"
    assert len(result["frames"]) == 1
    assert Path(result["frames"][0]).is_file()
    user_msg = provider.requests[0].messages[-1]
    assert len(user_msg.images) == 1
    assert "One frame follows" in user_msg.content
    assert "Two frames follow" not in user_msg.content


def test_visual_checker_preserves_two_frames_and_timing_text_for_dynamic_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = tmp_path / "game.html"
    page.write_text(
        "<!doctype html><html><body><canvas></canvas><script>console.log(1);</script></body></html>",
        encoding="utf-8",
    )
    view = _FakeView(
        probe_data={
            "canvases": 1,
            "canvas_rect": {"x": 0, "y": 0, "width": 1280, "height": 800},
            "paints_with_three": True,
            "page_background": "rgb(0, 0, 0)",
            "dynamic_dom": True,
        },
        shots=[b"frame-1", b"frame-2"],
    )
    _install_fake_browser(monkeypatch, view)
    provider = _FakeProvider('{"verdict": "pass", "issues": []}')
    checker = VisualChecker(
        provider=provider,  # type: ignore[arg-type]
        model="test-model",
        artifacts=tmp_path / "visual",
        vision=True,
        goal="Build a 3D game.",
    )

    result = checker(page)

    assert result["verdict"] == "pass"
    assert len(result["frames"]) == 2
    user_msg = provider.requests[0].messages[-1]
    assert len(user_msg.images) == 2
    assert "Two frames follow" in user_msg.content


def test_missing_playwright_reports_skipped_or_unavailable_never_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = tmp_path / "index.html"
    page.write_text("<!doctype html><html><body><h1>Hi</h1></body></html>", encoding="utf-8")
    monkeypatch.setattr(
        "karox.browser_bootstrap.playwright_chromium_status",
        lambda: BrowserBootstrapStatus(
            ready=False,
            package_available=False,
            chromium_available=False,
            executable=None,
            error="chromium_unavailable",
        ),
    )

    rendered = render_page(page)
    observed = observe_page(page)
    provider = _FakeProvider('{"verdict": "pass", "issues": []}')
    checker = VisualChecker(
        provider=provider,  # type: ignore[arg-type]
        model="test-model",
        artifacts=tmp_path / "visual",
        vision=True,
        goal="Check page.",
    )
    verdict = checker(page)

    assert rendered == {"skipped": "chromium_unavailable"}
    assert observed == {"skipped": "chromium_unavailable"}
    assert verdict["verdict"] == "unavailable"
    assert verdict["verdict"] != "pass"
    assert provider.requests == []


def test_console_errors_page_errors_and_resize_mismatch_override_vision_pass_Even_on_static_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svg = tmp_path / "broken_console.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<rect width="80" height="80" fill="red"/></svg>',
        encoding="utf-8",
    )
    view = _FakeView(
        gate_frames=[0],
        console_messages=[("error", "Failed to load resource")],
        page_errors=["ReferenceError: x is not defined"],
    )
    _install_fake_browser(monkeypatch, view)
    provider = _FakeProvider('{"verdict": "pass", "issues": []}')
    checker = VisualChecker(
        provider=provider,  # type: ignore[arg-type]
        model="test-model",
        artifacts=tmp_path / "visual",
        vision=True,
        goal="Draw a red square.",
    )

    result = checker(svg)

    assert result["verdict"] == "needs_work"
    assert result["console_errors"] == ["Failed to load resource"]
    assert result["page_errors"] == ["ReferenceError: x is not defined"]
    assert any("ReferenceError" in issue for issue in result["issues"])
    assert any("Failed to load resource" in issue for issue in result["issues"])
    assert render_problems("x.svg", measured={"sampled": 0}, probe={"canvases": 0})["problems"]


def test_observation_fingerprint_invalidates_on_page_asset_viewport_query_or_backend_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fingerprint_fn = getattr(web_check, "observation_fingerprint", None)
    assert callable(fingerprint_fn), "observation_fingerprint helper is missing"

    page = tmp_path / "index.html"
    css = tmp_path / "style.css"
    js = tmp_path / "app.js"
    img = tmp_path / "hero.svg"
    font = tmp_path / "ui.woff2"
    css.write_text("body { background: #111; }", encoding="utf-8")
    js.write_text("console.log('v1');", encoding="utf-8")
    img.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    font.write_bytes(b"woff2-v1")
    page.write_text(
        "<!doctype html><html><head><link rel='stylesheet' href='style.css'></head>"
        "<body><img src='hero.svg'><script src='app.js'></script></body></html>",
        encoding="utf-8",
    )

    base = fingerprint_fn(page, serve_root=tmp_path, viewport=(1280, 800), query="", backend="software")
    assert isinstance(base, str) and len(base) == 64

    # Changing CSS invalidates
    css.write_text("body { background: #222; }", encoding="utf-8")
    after_css = fingerprint_fn(page, serve_root=tmp_path, viewport=(1280, 800), query="", backend="software")
    assert after_css != base

    # Changing JS invalidates
    js.write_text("console.log('v2');", encoding="utf-8")
    after_js = fingerprint_fn(page, serve_root=tmp_path, viewport=(1280, 800), query="", backend="software")
    assert after_js not in {base, after_css}

    # Changing image or font invalidates
    font.write_bytes(b"woff2-v2")
    after_font = fingerprint_fn(page, serve_root=tmp_path, viewport=(1280, 800), query="", backend="software")
    assert after_font not in {base, after_css, after_js}

    # Changing viewport, query, served path, or render backend invalidates
    diff_viewport = fingerprint_fn(page, serve_root=tmp_path, viewport=(1000, 700), query="", backend="software")
    diff_query = fingerprint_fn(page, serve_root=tmp_path, viewport=(1280, 800), query="?mode=dark", backend="software")
    diff_backend = fingerprint_fn(page, serve_root=tmp_path, viewport=(1280, 800), query="", backend="hardware")
    copy_page = tmp_path / "copy.html"
    copy_page.write_bytes(page.read_bytes())
    diff_path = fingerprint_fn(copy_page, serve_root=tmp_path, viewport=(1280, 800), query="", backend="software")

    assert len({after_font, diff_viewport, diff_query, diff_backend, diff_path}) == 5

