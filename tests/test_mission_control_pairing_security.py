from __future__ import annotations

import threading
import unittest
from unittest import mock

from karox.mission_control_server import (
    MissionControlServerError,
    PairingAuthority,
    _safe_bind_host,
)


class MissionControlPairingSecurityTests(unittest.TestCase):
    def test_pairing_code_is_single_use_but_session_outlives_pairing_window(self) -> None:
        now = [100.0]
        with mock.patch("karox.mission_control_server.time.time", lambda: now[0]):
            pairing = PairingAuthority(ttl_seconds=10)
            self.assertIsNone(pairing.pair("wrong"))
            token = pairing.pair(pairing.code)
            self.assertIsNotNone(token)
            assert token is not None
            self.assertTrue(pairing.valid(token))
            self.assertIsNone(pairing.pair(pairing.code))
            now[0] = 111.0
            self.assertIsNone(pairing.pair(pairing.code))
            self.assertTrue(pairing.valid(token))

    def test_concurrent_pairing_requests_cannot_reuse_the_same_code(self) -> None:
        pairing = PairingAuthority(ttl_seconds=10)
        barrier = threading.Barrier(3)
        results: list[str | None] = []
        lock = threading.Lock()

        def attempt() -> None:
            barrier.wait()
            token = pairing.pair(pairing.code)
            with lock:
                results.append(token)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)

        tokens = [token for token in results if token is not None]
        self.assertEqual(len(results), 2)
        self.assertEqual(len(tokens), 1)
        self.assertTrue(pairing.valid(tokens[0]))

    def test_bind_host_is_loopback_or_tailscale_only(self) -> None:
        self.assertEqual(_safe_bind_host("localhost"), "127.0.0.1")
        self.assertEqual(_safe_bind_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(_safe_bind_host("100.100.10.20"), "100.100.10.20")
        self.assertEqual(_safe_bind_host("fd7a:115c:a1e0::1"), "fd7a:115c:a1e0::1")
        for unsafe in ("0.0.0.0", "::", "192.168.1.10", "8.8.8.8", "example.com"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(MissionControlServerError):
                    _safe_bind_host(unsafe)


if __name__ == "__main__":
    unittest.main()
