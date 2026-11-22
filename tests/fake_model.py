"""A scripted OpenAI-compatible model server for end-to-end tests of the real CLI.

    with FakeModel([Reply(text="hello"), Reply(calls=[("bash", {"command": "ls"})])]) as model:
        ... run `karox6 chat --base-url {model.url} --model fake` ...
        model.requests  # the JSON bodies the CLI sent, in order

Replies are served in order as SSE streams (or JSON without ``stream``); when the script
runs out the server answers "(script exhausted)". Chunk pacing, a delay before the first
byte and HTTP errors are scriptable, so tests can interrupt a turn, measure responsiveness
and exercise retries.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Reply:
    text: str = ""
    reasoning: str = ""
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    chunk_size: int = 24  # characters per streamed text chunk
    chunk_delay: float = 0.0  # seconds between chunks
    first_byte_delay: float = 0.0  # seconds before the response starts
    status: int = 200  # >= 400: an error answer with ``body``
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    finish: str | None = None  # default: "tool_calls" with calls, else "stop"
    usage: dict[str, Any] | None = None
    cut_stream: bool = False  # drop the connection mid-reply (no finish, no [DONE])


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # the body ends when the connection closes

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        model: FakeModel = self.server.model  # type: ignore[attr-defined]
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        reply, number = model._next(body)
        try:
            if reply.first_byte_delay:
                time.sleep(reply.first_byte_delay)
            if reply.status >= 400:
                failure = json.dumps({"error": {"message": "scripted failure"}})
                data = (reply.body or failure).encode()
                self.send_response(reply.status)
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if body.get("stream"):
                self._stream(reply, number)
            else:
                self._whole(reply, number)
        except (BrokenPipeError, ConnectionError, OSError):
            model.dropped += 1  # the client went away (a cancelled turn)

    def _usage(self, reply: Reply, number: int) -> dict[str, Any]:
        if reply.usage is not None:
            return reply.usage
        prompt = 1000 + 400 * number
        return {
            "prompt_tokens": prompt,
            "completion_tokens": 20 + len(reply.text) // 4,
            "prompt_tokens_details": {"cached_tokens": prompt - 200 if number else 0},
        }

    def _calls(self, reply: Reply, number: int) -> list[dict[str, Any]]:
        return [
            {
                "index": i,
                "id": f"call_{number}_{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for i, (name, args) in enumerate(reply.calls)
        ]

    def _whole(self, reply: Reply, number: int) -> None:
        message: dict[str, Any] = {"role": "assistant", "content": reply.text or None}
        if reply.reasoning:
            message["reasoning_content"] = reply.reasoning
        if reply.calls:
            message["tool_calls"] = self._calls(reply, number)
        finish = reply.finish or ("tool_calls" if reply.calls else "stop")
        payload = {
            "id": f"fake-{number}",
            "model": "fake",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": self._usage(reply, number),
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, reply: Reply, number: int) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def send(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> None:
            chunk = {"id": f"fake-{number}", "model": "fake", **extra}
            chunk["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()
            if reply.chunk_delay:
                time.sleep(reply.chunk_delay)

        size = max(reply.chunk_size, 1)
        for i in range(0, len(reply.reasoning), size):
            send({"reasoning_content": reply.reasoning[i : i + size]})
        pieces = [reply.text[i : i + size] for i in range(0, len(reply.text), size)]
        if reply.cut_stream:
            pieces = pieces[: max(len(pieces) // 2, 1)]
        for piece in pieces:
            send({"content": piece})
        if reply.cut_stream:
            return  # the connection closes without a finish reason
        for call in self._calls(reply, number):
            send({"tool_calls": [call]})
        finish = reply.finish or ("tool_calls" if reply.calls else "stop")
        send({}, finish)
        usage = {"id": f"fake-{number}", "model": "fake", "choices": []}
        usage["usage"] = self._usage(reply, number)
        self.wfile.write(b"data: " + json.dumps(usage).encode() + b"\n\ndata: [DONE]\n\n")
        self.wfile.flush()


class FakeModel:
    def __init__(self, replies: list[Reply] | None = None) -> None:
        self.replies = list(replies or [])
        self.requests: list[dict[str, Any]] = []
        self.dropped = 0
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.model = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def add(self, *replies: Reply) -> None:
        with self._lock:
            self.replies.extend(replies)

    def _next(self, body: dict[str, Any]) -> tuple[Reply, int]:
        with self._lock:
            number = len(self.requests)
            self.requests.append(body)
            reply = self.replies.pop(0) if self.replies else Reply(text="(script exhausted)")
        return reply, number

    def __enter__(self) -> FakeModel:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
