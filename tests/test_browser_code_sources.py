"""CDN imports of a local page load through KaroX, and nothing else does."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from karox.browser import code_sources
from karox.browser.netpolicy import BrowserAccessPolicy
from karox.browser.session import BrowserSession

THREE = "https://unpkg.com/three@0.160.0/build/three.module.js"
LOCAL = "http://127.0.0.1:8123/index.html"


def _eligible(**overrides: Any) -> Any:
    arguments: Dict[str, Any] = {
        "method": "GET",
        "url": THREE,
        "resource_type": "script",
        "navigation": False,
        "frame_url": LOCAL,
    }
    arguments.update(overrides)
    return code_sources.eligible(**arguments)


def test_a_local_pages_module_import_from_a_code_source_is_served() -> None:
    assert _eligible() == THREE
    assert _eligible(resource_type="fetch") == THREE
    assert _eligible(url="https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js")


@pytest.mark.parametrize(
    "overrides",
    [
        {"method": "POST"},
        {"navigation": True},
        {"resource_type": "image"},
        {"resource_type": "document"},
        {"frame_url": "https://example.com/page.html"},
        {"frame_url": ""},
        {"url": "https://example.com/three.module.js"},
        {"url": "http://unpkg.com/three@0.160.0/build/three.module.js"},
        {"url": "https://user:pw@unpkg.com/three.js"},
        {"url": "https://unpkg.com:8443/three.js"},
    ],
)
def test_everything_else_stays_refused(overrides: Dict[str, Any]) -> None:
    assert _eligible(**overrides) is None


class _Frame:
    url = LOCAL


class _Request:
    def __init__(self, url: str = THREE, *, resource_type: str = "script") -> None:
        self.url = url
        self.method = "GET"
        self.resource_type = resource_type
        self.frame = _Frame()
        self.redirected_from = None

    def is_navigation_request(self) -> bool:
        return False


class _Route:
    def __init__(self, request: _Request) -> None:
        self.request = request
        self.fulfilled: List[Dict[str, Any]] = []
        self.aborted = False
        self.continued = False

    def fulfill(self, **kwargs: Any) -> None:
        self.fulfilled.append(kwargs)

    def abort(self) -> None:
        self.aborted = True

    def continue_(self) -> None:
        self.continued = True


def _session(*, enabled: bool) -> BrowserSession:
    session = object.__new__(BrowserSession)
    session.policy = BrowserAccessPolicy(session_id="s1", code_sources=enabled)
    session._explicit_navigation_target = None
    return session


def test_the_route_fulfils_a_code_source_with_cors(monkeypatch: pytest.MonkeyPatch) -> None:
    served = code_sources.CodeSourceResponse(200, "application/javascript", b"export const A = 1;")
    asked: List[str] = []

    def fake_fetch(url: str, **_: Any) -> Any:
        asked.append(url)
        return served

    monkeypatch.setattr(code_sources, "fetch", fake_fetch)
    route = _Route(_Request())
    _session(enabled=True)._route(route)

    assert asked == [THREE]
    assert not route.aborted and not route.continued
    (call,) = route.fulfilled
    assert call["status"] == 200 and call["body"] == b"export const A = 1;"
    assert call["headers"]["access-control-allow-origin"] == "*"
    assert call["headers"]["content-type"].startswith("application/javascript")


def test_without_the_permission_the_import_is_still_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        code_sources, "fetch", lambda url, **_: pytest.fail("fetched without permission")
    )
    route = _Route(_Request())
    _session(enabled=False)._route(route)
    assert route.aborted and not route.fulfilled


def test_a_failed_read_is_a_blocked_request_not_an_empty_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(code_sources, "fetch", lambda url, **_: None)
    route = _Route(_Request())
    _session(enabled=True)._route(route)
    assert route.aborted and not route.fulfilled


def test_binary_and_html_answers_are_not_served() -> None:
    assert code_sources._acceptable_type("application/javascript")
    assert code_sources._acceptable_type("text/css")
    assert code_sources._acceptable_type("application/wasm")
    assert code_sources._acceptable_type("font/woff2")
    assert not code_sources._acceptable_type("image/png")
    assert not code_sources._acceptable_type("application/octet-stream")
