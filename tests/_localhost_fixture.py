"""Tiny localhost HTTP fixture for browser-runtime proof (NOT Facebook).

Serves a single HTML page with an input, button, select, and a console.log
call so a real ``BrowserSessionManager`` can exercise open/snapshot/fill/click/
select/press/screenshot/console/network_failures/close against localhost only.
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

HTML = b"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>KaroX localhost fixture</title></head>
<body>
  <h1>KaroX localhost fixture</h1>
  <p id="status">ready</p>
  <input id="name" type="text" placeholder="name" autocomplete="off">
  <button id="go" type="button">Go</button>
  <select id="color">
    <option value="red">Red</option>
    <option value="green">Green</option>
    <option value="blue">Blue</option>
  </select>
  <script>
    console.log("fixture ready");
    let goBtn = document.getElementById("go");
    let nameInput = document.getElementById("name");
    let statusEl = document.getElementById("status");
    goBtn.addEventListener("click", function () {
      statusEl.textContent = "clicked: " + nameInput.value;
      console.log("clicked", nameInput.value);
    });
  </script>
</body>
</html>
"""


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server contract
        body = HTML if self.path == "/" else b"not found"
        self.send_response(200 if self.path == "/" else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence the server log
        pass


class LocalhostFixture:
    """A started HTTP server on 127.0.0.1 with a deterministic HTML page."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.server = HTTPServer(("127.0.0.1", self.port), _Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

    def start(self) -> "LocalhostFixture":
        self.thread.start()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def stop(self) -> None:
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.server.server_close()
        except Exception:
            pass


def start_localhost_fixture() -> LocalhostFixture:
    return LocalhostFixture().start()
