from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks" / "agent_throughput" / "latest_gpt_web_keepalive_reproduction.json"


async def _app(scope, receive, send):
    assert scope["type"] == "http"
    body = b"ok"
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2"), (b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": body})


def _read_response(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            return data
        data += chunk
    head, body = data.split(b"\r\n\r\n", 1)
    content_length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            content_length = int(value.strip())
            break
    while len(body) < content_length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body


def _probe(timeout_keep_alive: int, pause_seconds: float) -> dict[str, object]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    config = uvicorn.Config(
        _app,
        host="127.0.0.1",
        port=port,
        log_level="critical",
        access_log=False,
        timeout_keep_alive=timeout_keep_alive,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started

    first_ok = False
    second_ok = False
    second_error: str | None = None
    sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    sock.settimeout(2.0)
    try:
        request = b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n"
        sock.sendall(request)
        first = _read_response(sock)
        first_ok = b"200 OK" in first and first.endswith(b"ok")
        time.sleep(pause_seconds)
        try:
            sock.sendall(request)
            second = _read_response(sock)
            second_ok = b"200 OK" in second and second.endswith(b"ok")
            if not second_ok:
                second_error = "connection_closed_or_empty_response"
        except OSError as exc:
            second_error = type(exc).__name__
    finally:
        sock.close()
        server.should_exit = True
        thread.join(timeout=5.0)
        listener.close()
    return {
        "timeout_keep_alive": timeout_keep_alive,
        "pause_seconds": pause_seconds,
        "first_ok": first_ok,
        "second_ok": second_ok,
        "second_error": second_error,
    }


def test_keepalive_reproduction() -> None:
    pause_seconds = 6.25
    short = _probe(5, pause_seconds)
    long = _probe(300, pause_seconds)
    payload = {
        "schema_version": 1,
        "benchmark": "gpt-web-uvicorn-keepalive-reproduction",
        "uvicorn_version": uvicorn.__version__,
        "short": short,
        "long": long,
        "isolation": {"localhost_only": True, "live_bridge_touched": False, "saved_profile_touched": False},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert short["first_ok"] is True
    assert short["second_ok"] is False
    assert long["first_ok"] is True
    assert long["second_ok"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
