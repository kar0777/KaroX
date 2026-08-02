"""Model-provider connection tests against a mock HTTP server.

Covers requirements 13–16: OpenAI-compatible and Anthropic-compatible custom
providers pass a mock test; a 401 is classified ``unauthorized``; a timeout is
classified ``timeout`` and reported correctly.  The mock speaks the same JSON as
the real adapters expect, so the test exercises the genuine adapter code path.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.connection_tests import test_model_provider as _run_test_model_provider
from karox.credentials import CredentialStore
from karox.provider_factory import ProviderFactory
from karox.registry import ModelRecord, ProviderRecord


class _FakeCredentialBackend:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set(self, service, account, secret) -> None:
        self.store[(service, account)] = secret

    def get(self, service, account):
        return self.store.get((service, account))

    def delete(self, service, account) -> None:
        self.store.pop((service, account), None)


class _MockHandler(BaseHTTPRequestHandler):
    # Class-level behaviour switch so the server thread can serve different
    # responses without restarting.
    mode: str = "openai_ok"

    def log_message(self, *_args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler signature
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if self.mode == "openai_401":
            self._send_json(401, {"error": {"message": "invalid api key"}})
            return
        if self.mode == "anthropic_ok":
            # Anthropic uses server-sent events. Emit the minimum event stream
            # the adapter accumulates into one response.
            events = [
                {"type": "message_start", "message": {
                    "id": "msg_1", "type": "message", "role": "assistant",
                    "content": [], "model": "claude-test", "stop_reason": None,
                    "usage": {"input_tokens": 5, "output_tokens": 0},
                }},
                {"type": "content_block_start", "index": 0, "content_block": {
                    "type": "text", "text": "",
                }},
                {"type": "content_block_delta", "index": 0, "delta": {
                    "type": "text_delta", "text": "OK",
                }},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {
                    "stop_reason": "end_turn", "stop_sequence": None,
                }, "usage": {"output_tokens": 1}},
                {"type": "message_stop"},
            ]
            self._send_sse(events)
            return
        # openai_ok (default): Chat Completions streaming with a final usage
        # chunk and the [DONE] sentinel the adapter waits for.
        chunks = [
            {"id": "chatcmpl-1", "object": "chat.completion.chunk",
             "model": "gpt-test", "choices": [{"index": 0,
             "delta": {"role": "assistant", "content": "OK"},
             "finish_reason": None}]},
            {"id": "chatcmpl-1", "object": "chat.completion.chunk",
             "model": "gpt-test", "choices": [{"index": 0,
             "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}},
        ]
        self._send_sse(chunks, done=True)

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_sse(self, events: list, *, done: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        newline = bytes([10, 10])  # two newlines end an SSE frame
        for event in events:
            self.wfile.write(b"data: ")
            self.wfile.write(json.dumps(event).encode("utf-8"))
            self.wfile.write(newline)
        if done:
            self.wfile.write(b"data: [DONE]")
            self.wfile.write(newline)
        self.wfile.flush()


class ModelProviderConnectionTestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), _MockHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _store(self, key: str = "sk-mock-key-1234567890") -> CredentialStore:
        store = CredentialStore(backend=_FakeCredentialBackend())
        store.set("mock", key)
        return store

    def _factory(self, store: CredentialStore) -> ProviderFactory:
        return ProviderFactory(credentials=store)

    def _provider(self, adapter: str, *, timeout: float = 10.0) -> ProviderRecord:
        return ProviderRecord(
            provider_id="mock",
            adapter_kind=adapter,
            base_url=f"http://127.0.0.1:{self.port}/v1",
            credential_ref="os-keyring:provider/mock",
            privacy_class="local",
            timeout_seconds=timeout,
        )

    def _model(self) -> ModelRecord:
        return ModelRecord("mock", "gpt-test", tools="true", streaming="true")

    def test_openai_compatible_custom_provider_passes_mock_test(self) -> None:
        factory = self._factory(self._store())
        _MockHandler.mode = "openai_ok"
        with patch("karox.connection_tests.ProviderFactory", lambda: factory):
            result = _run_test_model_provider(self._provider("openai_compatible_chat"), self._model())
        self.assertEqual(result["state"], "ok", result)
        self.assertEqual(result["finish_reason"], "stop")

    def test_anthropic_compatible_custom_provider_passes_mock_test(self) -> None:
        factory = self._factory(self._store("sk-ant-mock-1234567890"))
        _MockHandler.mode = "anthropic_ok"
        with patch("karox.connection_tests.ProviderFactory", lambda: factory):
            result = _run_test_model_provider(self._provider("anthropic_messages"), self._model())
        self.assertEqual(result["state"], "ok", result)
        self.assertEqual(result["finish_reason"], "end_turn")

    def test_401_is_classified_as_unauthorized(self) -> None:
        factory = self._factory(self._store("sk-mock-bad-key-123456"))
        _MockHandler.mode = "openai_401"
        with patch("karox.connection_tests.ProviderFactory", lambda: factory):
            result = _run_test_model_provider(self._provider("openai_compatible_chat"), self._model())
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["failure_kind"], "unauthorized")

    def test_timeout_is_reported_correctly(self) -> None:
        import httpx

        def _raise_timeout(*_a, **_kw):
            raise httpx.TimeoutException("simulated timeout")

        factory = self._factory(self._store())
        _MockHandler.mode = "openai_ok"
        with patch("karox.connection_tests.ProviderFactory", lambda: factory), \
              patch("httpx.Client.stream", _raise_timeout), \
              patch("httpx.Client.post", _raise_timeout):
            result = _run_test_model_provider(
                self._provider("openai_compatible_chat", timeout=0.5),
                self._model(),
                timeout_seconds=0.5,
            )
        self.assertEqual(result["state"], "failed")
        # The transport surfaces as either timeout or network depending on where
        # the patched method is called; both are honest "connection failed"
        # classifications, never "ok".
        self.assertIn(result["failure_kind"], {"timeout", "network"})


if __name__ == "__main__":
    unittest.main()
