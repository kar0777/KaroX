"""Contracts for KaroX's built-in HTML-only managed dev server."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from _support import SRC  # noqa: F401

import karox.static_server as static_server


class StaticServerTests(unittest.TestCase):
    def test_default_port_is_safe_and_non_privileged(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(static_server._port_from_environment(), 8765)

    def test_invalid_or_privileged_port_is_refused(self) -> None:
        for value in ("not-a-port", "80", "70000"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"PORT": value}, clear=True):
                with self.assertRaises(SystemExit):
                    static_server._port_from_environment()

    def test_main_refuses_public_host_before_opening_socket(self) -> None:
        with (
            mock.patch.dict(os.environ, {"HOST": "0.0.0.0", "PORT": "8765"}, clear=True),
            mock.patch.object(static_server, "ThreadingHTTPServer") as server,
        ):
            with self.assertRaisesRegex(SystemExit, "loopback"):
                static_server.main()
        server.assert_not_called()

    def test_main_binds_loopback_and_serves_until_shutdown(self) -> None:
        instance = mock.Mock()
        with (
            mock.patch.dict(os.environ, {"HOST": "127.0.0.1", "PORT": "9123"}, clear=True),
            mock.patch.object(static_server, "ThreadingHTTPServer", return_value=instance) as server,
        ):
            static_server.main()
        server.assert_called_once_with(("127.0.0.1", 9123), static_server.SimpleHTTPRequestHandler)
        instance.serve_forever.assert_called_once_with(poll_interval=0.25)
        instance.server_close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
