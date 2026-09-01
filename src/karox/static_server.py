"""Tiny loopback-only static server for HTML-only KaroX projects.

The managed service supervisor launches this module from the selected project
root.  It deliberately has no public-bind option: HOST must resolve to a
loopback spelling and PORT is the only caller-overridable setting exposed by
the matching managed-server profile.
"""

from __future__ import annotations

import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _port_from_environment() -> int:
    raw = os.environ.get("PORT", "8765")
    try:
        port = int(raw)
    except ValueError as exc:
        raise SystemExit("PORT must be an integer") from exc
    if not 1024 <= port <= 65535:
        raise SystemExit("PORT must be between 1024 and 65535")
    return port


def main() -> None:
    host = os.environ.get("HOST", "127.0.0.1").strip().lower()
    if host not in _LOOPBACK_HOSTS:
        raise SystemExit("HOST must stay on loopback")
    port = _port_from_environment()
    server = ThreadingHTTPServer((host, port), SimpleHTTPRequestHandler)
    server.daemon_threads = True
    print(f"KaroX static server ready on http://{host}:{port}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
