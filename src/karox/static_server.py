"""Loopback static serving for pages the agent wrote.

A page with ``<script type="module">`` does not run from ``file://`` in
Chromium: the module is blocked by CORS, the canvas stays blank, and the console
records an error. The visual checker used to open every page that way, so it
judged a working 3D game as an empty scene while the hidden grader, which loads
the page over HTTP, passed all 30 checks. The browser tool got a loopback server
first; this module is the one implementation both callers share.

The server binds ``127.0.0.1`` only, serves one repository, refuses a resolved
path outside it (including a symlink), and is scoped to the caller's context.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

__all__ = [
    "LoopbackServer",
    "RepositoryRequestHandler",
    "page_url",
    "serve_repository",
]


class RepositoryRequestHandler(SimpleHTTPRequestHandler):
    """Serve one repository over loopback, with no path escape."""

    def __init__(self, *args: Any, root: Path, **kwargs: Any) -> None:
        self._repository_root = root
        super().__init__(*args, directory=str(root), **kwargs)

    def translate_path(self, path: str) -> str:
        candidate = Path(super().translate_path(path)).resolve()
        try:
            candidate.relative_to(self._repository_root)
        except ValueError:
            return str(self._repository_root / "__not_found__")
        return str(candidate)

    def send_head(self):  # type: ignore[no-untyped-def]
        # Chromium requests /favicon.ico on its own for every served page. A
        # repository without one is not a page error, but the 404 lands in the
        # console and the checker reported it as one. Answer that automatic
        # request with an empty 204 only when the repository has no such file.
        if self.path.split("?", 1)[0] == "/favicon.ico" and not (
            self._repository_root / "favicon.ico"
        ).is_file():
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        return super().send_head()

    def log_message(self, format: str, *args: Any) -> None:
        # A page load is not an operator log line; stdout belongs to the agent.
        return


class LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True


def page_url(base: str, root: Path, page: str | Path) -> str:
    """Return the loopback URL for one repository-relative file."""

    repository = Path(root).resolve()
    if not isinstance(page, (str, Path)) or not str(page).strip():
        raise ValueError("page must be a non-empty repository-relative path")
    relative = str(page).replace("\\", "/").lstrip("/")
    target = (repository / relative).resolve()
    try:
        target.relative_to(repository)
    except ValueError as exc:
        raise ValueError("page must stay inside the repository") from exc
    if not target.is_file():
        raise ValueError(f"page is not a file: {page}")
    return base + quote(target.relative_to(repository).as_posix())


@contextmanager
def serve_repository(root: Path) -> Iterator[str]:
    """Serve ``root`` on a random loopback port for the duration of the block."""

    repository = Path(root).resolve()
    if not repository.is_dir():
        raise ValueError("repository root is not a directory")
    server = LoopbackServer(
        ("127.0.0.1", 0),
        lambda *args, **kwargs: RepositoryRequestHandler(
            *args, root=repository, **kwargs
        ),
    )
    thread = threading.Thread(
        target=server.serve_forever,
        name="karox-static-server",
        daemon=True,
    )
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}/"
    finally:
        server.shutdown()
        server.server_close()