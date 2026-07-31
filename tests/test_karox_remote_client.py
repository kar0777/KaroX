from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import _path_setup

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "remote" / "src"))

from karox_remote.client import (  # noqa: E402
    KaroXRemoteClient,
    RemoteClientError,
    RemoteEnvironment,
)


class FakeResponse:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self.payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.payload if size < 0 else self.payload[:size]


class KaroXRemoteClientTests(unittest.TestCase):
    def test_environment_repr_hides_connection_values(self) -> None:
        environment = RemoteEnvironment(
            "https://private-bridge.invalid",
            "super-secret-value",
            "local-session",
        )
        shown = repr(environment)
        self.assertNotIn("private-bridge", shown)
        self.assertNotIn("super-secret", shown)
        self.assertIn("local-session", shown)

    def test_missing_environment_fails_without_naming_values(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RemoteClientError) as raised:
                RemoteEnvironment.load()
        self.assertIn("unavailable", str(raised.exception))

    def test_tool_call_uses_raw_arguments_and_idempotency_header(self) -> None:
        captured: dict[str, object] = {}

        def open_request(request: object, timeout: float) -> FakeResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({"ok": True, "data": {"changed": True}})

        environment = RemoteEnvironment(
            "https://bridge.invalid",
            "credential-value",
            "local-session",
        )
        client = KaroXRemoteClient(environment, timeout_seconds=12.5)
        with patch("urllib.request.urlopen", side_effect=open_request):
            result = client.call(
                "karox.repo.write_file",
                {"path": "a.txt", "content": "hello"},
                idempotency_key="stable-mutation-key",
            )
        self.assertTrue(result["ok"])
        request = captured["request"]
        self.assertEqual(captured["timeout"], 12.5)
        self.assertEqual(request.full_url, "https://bridge.invalid/tools/karox.repo.write_file")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {"path": "a.txt", "content": "hello"},
        )
        self.assertEqual(
            request.get_header("X-karox-idempotency-key"),
            "stable-mutation-key",
        )
        self.assertEqual(request.get_header("X-karox-remote-protocol"), "1")
        self.assertEqual(request.get_header("X-karox-session-id"), "local-session")

    def test_network_error_does_not_leak_url_or_credential_in_exception_chain(self) -> None:
        environment = RemoteEnvironment(
            "https://do-not-leak.invalid",
            "do-not-leak-credential",
            "local-session",
        )
        client = KaroXRemoteClient(environment)
        failure = urllib.error.URLError("connection failed")
        with patch("urllib.request.urlopen", side_effect=failure):
            with self.assertRaises(RemoteClientError) as raised:
                client.get("/session")
        rendered = str(raised.exception)
        self.assertNotIn("do-not-leak.invalid", rendered)
        self.assertNotIn("do-not-leak-credential", rendered)
        self.assertIsNone(raised.exception.__cause__)

    def test_http_error_reports_only_bounded_error_code(self) -> None:
        environment = RemoteEnvironment(
            "https://bridge.invalid",
            "credential-value",
            "local-session",
        )
        client = KaroXRemoteClient(environment)
        error = urllib.error.HTTPError(
            "https://bridge.invalid/tools/karox.repo.read_file",
            403,
            "Forbidden",
            {},
            io.BytesIO(json.dumps({"error": "denied"}).encode("utf-8")),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RemoteClientError) as raised:
                client.call("karox.repo.read_file", {"path": "a.txt"})
        self.assertEqual(
            str(raised.exception),
            "local KaroX bridge rejected the request: HTTP 403 (denied)",
        )
        self.assertNotIn("bridge.invalid", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
