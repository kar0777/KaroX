"""Fault-injection stand: a real loopback server misbehaves, the run survives.

Every scenario goes through the real HTTP transport (httpx), the real SSE
parser and the engine bridge's retry loop -- only the server is scripted. A
transient failure (429, 5xx, 529 overload, a cut or stalled stream, a reset
connection, a corrupted frame) must end in the answer; a request the provider
refuses as invalid must fail at once, without retries that would only repeat it.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, List

import pytest

from karox.engine_provider_bridge import EngineProviderModel
from karox.providers import OpenAIChatCompletionsProvider, ProviderError, ProviderErrorKind

_FINISH_FRAMES = [
    {
        "id": "r",
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "finish", "arguments": json.dumps({"answer": "done"})},
                        }
                    ]
                }
            }
        ],
    },
    {"id": "r", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    {"id": "r", "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
]


class _Script:
    def __init__(self, steps: List[str]) -> None:
        self.steps = list(steps)
        self.hits = 0
        self.lock = threading.Lock()

    def next(self) -> str:
        with self.lock:
            self.hits += 1
            return self.steps.pop(0) if self.steps else "ok"


def _handler(script: _Script):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # quiet
            return None

        def _sse_headers(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()

        def _frame(self, value: Any) -> None:
            data = value if isinstance(value, str) else json.dumps(value)
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            step = script.next()
            if step in {"429", "500", "502", "503", "529", "400"}:
                body = json.dumps({"error": {"message": "scripted", "type": "scripted"}}).encode()
                self.send_response(int(step))
                if step == "429":
                    self.send_header("Retry-After", "0")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                return
            if step == "reset":
                # Drop the connection without a status line.
                self.connection.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00"
                )
                self.connection.close()
                return
            self._sse_headers()
            if step == "truncate":
                self._frame({"id": "r", "choices": [{"delta": {"content": "par"}}]})
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if step == "badjson":
                self._frame("{this is not json")
                return
            if step == "stall":
                time.sleep(1.5)
                return
            if step == "drip":
                for frame in _FINISH_FRAMES:
                    time.sleep(0.3)
                    self._frame(frame)
                self._frame("[DONE]")
                return
            for frame in _FINISH_FRAMES:
                self._frame(frame)
            self._frame("[DONE]")

    return Handler


@pytest.fixture
def stand() -> Iterator[Any]:
    servers: List[ThreadingHTTPServer] = []

    def start(steps: List[str]) -> tuple[str, _Script]:
        script = _Script(steps)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(script))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}/v1", script

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _run(base_url: str, *, timeout: float = 5.0) -> Dict[str, Any]:
    provider = OpenAIChatCompletionsProvider(
        base_url, timeout_seconds=timeout, max_transport_retries=0
    )
    model = EngineProviderModel(
        provider=provider,
        model_id="m",
        tools=(),
        deadline_seconds=120.0,
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )
    try:
        return model.query([{"role": "system", "content": "s"}, {"role": "user", "content": "go"}])
    finally:
        provider.close()


@pytest.mark.parametrize(
    "fault",
    ["429", "500", "502", "503", "529", "reset", "truncate", "badjson", "stall"],
)
def test_a_transient_fault_is_retried_to_the_answer(stand, fault: str) -> None:
    base_url, script = stand([fault])
    message = _run(base_url, timeout=1.0)
    assert message["tool_calls"][0]["name"] == "finish"
    assert script.hits == 2


def test_three_faults_in_a_row_still_end_in_the_answer(stand) -> None:
    base_url, script = stand(["529", "truncate", "429"])
    message = _run(base_url, timeout=1.0)
    assert message["tool_calls"][0]["name"] == "finish"
    assert script.hits == 4


def test_a_slow_drip_within_the_idle_timeout_is_not_a_failure(stand) -> None:
    base_url, script = stand(["drip"])
    message = _run(base_url, timeout=1.0)
    assert message["tool_calls"][0]["name"] == "finish"
    assert script.hits == 1


def test_an_invalid_request_is_not_retried_beyond_one_reduced_shape(stand) -> None:
    # The only second attempt is the documented one without optional tuning
    # fields; when that is refused too, the first error is the answer.
    base_url, script = stand(["400", "400"])
    with pytest.raises(ProviderError) as caught:
        _run(base_url)
    assert caught.value.kind is ProviderErrorKind.INVALID_REQUEST
    assert script.hits == 2


def test_a_corrupted_stream_is_not_retried_forever(stand) -> None:
    base_url, script = stand(["badjson"] * 5)
    with pytest.raises(ProviderError) as caught:
        _run(base_url)
    assert caught.value.kind is ProviderErrorKind.MALFORMED_RESPONSE
    assert script.hits == 3


def test_an_unreachable_host_fails_fast_on_connect() -> None:
    # Nothing listens on this port: the connect fails immediately rather than
    # waiting out the read budget.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    started = time.monotonic()
    with pytest.raises(ProviderError) as caught:
        provider = OpenAIChatCompletionsProvider(
            f"http://127.0.0.1:{port}/v1", timeout_seconds=60.0, max_transport_retries=0
        )
        model = EngineProviderModel(
            provider=provider,
            model_id="m",
            tools=(),
            deadline_seconds=25.0,
            sleep=lambda _seconds: None,
            jitter=lambda: 0.0,
        )
        model.query([{"role": "user", "content": "go"}])
    assert caught.value.kind is ProviderErrorKind.TRANSPORT
    assert time.monotonic() - started < 20.0


def _rate_limited(headers: Dict[str, str]) -> ProviderError:
    import httpx

    response = httpx.Response(
        429,
        headers=headers,
        request=httpx.Request("POST", "https://api.example/v1/chat/completions"),
    )
    return OpenAIChatCompletionsProvider._http_error(response)


def test_openai_rate_limit_reset_headers_set_the_wait() -> None:
    error = _rate_limited(
        {
            "x-ratelimit-remaining-requests": "12",
            "x-ratelimit-reset-requests": "1s",
            "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-reset-tokens": "6m0s",
        }
    )
    assert error.retry_after == 360.0


def test_retry_after_ms_wins_and_retry_after_still_comes_first() -> None:
    assert _rate_limited({"retry-after-ms": "250"}).retry_after == 0.25
    assert _rate_limited({"Retry-After": "3", "retry-after-ms": "250"}).retry_after == 3.0


def test_anthropic_rate_limit_reset_is_read() -> None:
    from datetime import datetime, timedelta, timezone

    soon = (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat()
    error = _rate_limited(
        {
            "anthropic-ratelimit-input-tokens-remaining": "0",
            "anthropic-ratelimit-input-tokens-reset": soon,
        }
    )
    assert error.retry_after is not None and 15.0 <= error.retry_after <= 21.0


def test_go_durations_parse() -> None:
    from karox.providers import _go_duration_seconds

    assert _go_duration_seconds("1h2m3.5s") == 3723.5
    assert _go_duration_seconds("250ms") == 0.25
    assert _go_duration_seconds("7") == 7.0
    assert _go_duration_seconds("soon") is None
