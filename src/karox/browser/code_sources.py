"""Public code-source subresources for local pages in the agent browser.

A local page that imports three.js from unpkg is the ordinary shape of a
single-file 3D task, and the task often names the CDN URL outright. The agent
browser is limited to localhost, so those imports failed with
``net::ERR_FAILED`` and the model spent steps fetching the library into
scratch folders just to see its own page (the b0 aquarium run, 2026-11-05:
``.tmpcheck/``, ``tmpcheck/`` and a ``local-test.html`` copy, five steps),
while the render check -- which launches its own browser -- loaded the same
URLs without trouble.

The bytes now come through KaroX instead of the browser's own network: GET
only, no request body, no cookies or credentials, only the ``web_fetch`` host
allowlist, only as a subresource of a local page, and only for a session whose
profile may use ``code.fetch``. Redirects are followed only while they stay on
the allowlist. Responses are cached in memory for the process, bounded per file
and in total, so a page reloaded twenty times costs one download.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .netpolicy import is_local_browser_url

#: One library file. three.module.js r160 is 1.2 MB; a full build of a large
#: library stays well under this.
MAX_SOURCE_BYTES = 16 * 1024 * 1024
#: Everything cached for the process.
MAX_CACHE_BYTES = 64 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 30.0
_CHUNK = 64 * 1024
#: Resource kinds a page loads as code or data for code. A navigation, an
#: image or a media stream is never served this way.
_RESOURCE_TYPES = frozenset({"script", "stylesheet", "fetch", "xhr", "font", "other"})
_EXTRA_TYPES = frozenset({"application/wasm", "text/css"})
_FONT_PREFIX = "font/"


@dataclass(frozen=True)
class CodeSourceResponse:
    status: int
    content_type: str
    body: bytes


_cache: dict[str, CodeSourceResponse] = {}
_cache_bytes = 0
_lock = threading.Lock()


def _allowlisted(url: str) -> Optional[str]:
    """The normalized URL when it names an allowlisted code source, else None."""

    from ..core_tools import code_source_url

    try:
        normalized, _host = code_source_url(url)
    except Exception:  # noqa: BLE001 - any refusal means "not a code source"
        return None
    return normalized


def eligible(
    *,
    method: str,
    url: str,
    resource_type: str,
    navigation: bool,
    frame_url: str,
) -> Optional[str]:
    """The code-source URL to serve for this request, or None to refuse it."""

    if str(method or "").upper() != "GET" or navigation:
        return None
    if str(resource_type or "") not in _RESOURCE_TYPES:
        return None
    if not frame_url or not is_local_browser_url(frame_url):
        return None
    if urlsplit(str(url or "")).scheme != "https":
        return None
    return _allowlisted(str(url))


def _acceptable_type(content_type: str) -> bool:
    from ..core_tools import _is_code_source_type

    lowered = content_type.strip().lower()
    return (
        _is_code_source_type(lowered)
        or lowered in _EXTRA_TYPES
        or lowered.startswith(_FONT_PREFIX)
    )


class _Refused(Exception):
    """A redirect left the allowlist; the request is refused, not answered."""


class _StayOnCodeSources(HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(  # type: ignore[no-untyped-def]
        self, req, fp, code, msg, headers, newurl
    ):
        if _allowlisted(str(newurl)) is None:
            raise _Refused(str(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, *, timeout: float = FETCH_TIMEOUT_SECONDS) -> Optional[CodeSourceResponse]:
    """Read one allowlisted source, from the cache when it is there. Never raises."""

    global _cache_bytes
    with _lock:
        cached = _cache.get(url)
    if cached is not None:
        return cached
    request = Request(
        url,
        headers={
            "User-Agent": "KaroX-browser-code-source",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        },
        method="GET",
    )
    try:
        with build_opener(_StayOnCodeSources()).open(request, timeout=timeout) as response:
            final = str(getattr(response, "geturl", lambda: url)() or url)
            if _allowlisted(final) is None:
                return None
            status = int(getattr(response, "status", 200) or 200)
            content_type = str(response.headers.get_content_type() or "")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read1(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
                    return None
                chunks.append(chunk)
    except HTTPError as exc:
        # A 404 is an answer the page should see, not a blocked request: the
        # model learns that the path is wrong instead of that the net is off.
        return CodeSourceResponse(int(exc.code), "text/plain", b"")
    except (_Refused, URLError, OSError, TimeoutError, ValueError):
        return None
    if not _acceptable_type(content_type):
        return None
    result = CodeSourceResponse(status, content_type, b"".join(chunks))
    with _lock:
        if url not in _cache and _cache_bytes + len(result.body) <= MAX_CACHE_BYTES:
            _cache[url] = result
            _cache_bytes += len(result.body)
    return result


def response_headers(response: CodeSourceResponse) -> dict[str, str]:
    """Headers for ``route.fulfill``: the type, and CORS for module imports."""

    content_type = response.content_type or "application/octet-stream"
    if content_type.startswith("text/") or "javascript" in content_type or content_type.endswith("json"):
        content_type += "; charset=utf-8"
    return {
        "content-type": content_type,
        # Module scripts are fetched in CORS mode; the CDNs send this header
        # themselves and the served copy must keep it.
        "access-control-allow-origin": "*",
        "cache-control": "public, max-age=3600",
    }


def _reset_cache_for_tests() -> None:
    global _cache_bytes
    with _lock:
        _cache.clear()
        _cache_bytes = 0
