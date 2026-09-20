"""Headless Secret Service emulation: real keyring + real child, no OS daemon.

The test server implements only the D-Bus subset used here and keeps synthetic
secrets in RAM. It is NOT an encryption-at-rest or GNOME/PAM conformance test.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from _support import SRC  # noqa: F401
from karox import credential_session as session
from karox.credentials import CredentialError, CredentialStore, KeyringBackend


def body_interface_invalid(body):
    return body[0] not in {
        "org.freedesktop.Secret.Collection", "org.freedesktop.Secret.Item"
    }


class _BusHandler(socketserver.StreamRequestHandler):
    def handle(self):
        from jeepney import new_error, new_method_return
        from jeepney.low_level import HeaderFields, Parser

        self.connection.settimeout(10)
        if self.rfile.read(1) != b"\x00":
            return
        if not self.rfile.readline().startswith(b"AUTH EXTERNAL"):
            return
        self.wfile.write(b"OK " + b"1" * 32 + b"\r\n")
        if self.rfile.readline() != b"BEGIN\r\n":
            return
        parser = Parser()
        serial = 0
        while data := self.rfile.read1(65536):
            for msg in parser.feed(data):
                method = msg.header.fields[HeaderFields.member]
                path = msg.header.fields[HeaderFields.path]
                interface = msg.header.fields.get(HeaderFields.interface)
                expected_interface = {
                    "ReadAlias": "org.freedesktop.Secret.Service",
                    "Get": "org.freedesktop.DBus.Properties",
                }.get(method)
                if expected_interface is not None and interface != expected_interface:
                    serial += 1
                    self.wfile.write(new_error(
                        msg, "org.freedesktop.DBus.Error.UnknownInterface"
                    ).serialise(serial=serial))
                    continue
                if method == "Get" and body_interface_invalid(msg.body):
                    serial += 1
                    self.wfile.write(new_error(
                        msg, "org.freedesktop.DBus.Error.UnknownInterface"
                    ).serialise(serial=serial))
                    continue
                bus = self.server.emulator
                bus.calls.append(method)
                if method == "Hello" and bus.stall_hello:
                    # Keep the authenticated peer open but never answer Hello.
                    # Client timeout/close releases this handler; no sleeper.
                    if not self.rfile.read(1):
                        bus.hello_peer_closed.set()
                    return
                signature, body = bus.reply(method, path, msg.body)
                if method == "CreateItem" and bus.lose_write_reply:
                    # Commit happened above, but the caller never gets a reply.
                    return
                if signature == "ERROR":
                    response = new_error(msg, body[0])
                else:
                    response = new_method_return(msg, signature, body)
                serial += 1
                self.wfile.write(response.serialise(serial=serial))


class _EmulatedBus:
    """Private local socket, no daemon, no stored password/password file."""

    collection = "/org/freedesktop/secrets/collection/login"

    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "bus"
        self.calls = []
        self.items = {}
        self.running = True
        self.locked = False
        self.item_locked = False
        self.stall_hello = False
        self.lose_write_reply = False
        self.hello_peer_closed = threading.Event()
        self.server = socketserver.ThreadingUnixStreamServer(str(self.path), _BusHandler)
        self.server.daemon_threads = True
        self.server.emulator = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.environment = patch.dict(
            os.environ,
            {
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(self.path),
                "KAROX_TEST_ALLOW_REAL_KEYRING": "1",  # points ONLY at this private emulator
                "PYTHON_KEYRING_BACKEND": "keyring.backends.SecretService.Keyring",
            },
        )
        self.environment.start()
        return self

    def __exit__(self, *_):
        self.environment.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.directory.cleanup()

    def reply(self, method, path, body):
        if method == "Hello":
            return "s", (":1.20",)
        if method == "NameHasOwner":
            return "b", (self.running,)
        if method == "GetNameOwner":
            return "s", (":1.5",)
        if method == "StartServiceByName":
            self.running = True
            return "u", (1,)
        if method == "ReadAlias":
            return "o", (self.collection,)
        if method == "Get":
            if body[1] == "Locked":
                return "v", (("b", self.locked or (path in self.items and self.item_locked)),)
            if body[1] == "Label":
                return "v", (("s", "Synthetic test collection/item"),)
        if method == "AddMatch":
            return "", ()
        if method == "SearchItems":
            return "ao", (
                [
                    p
                    for p, (attrs, _) in self.items.items()
                    if all(attrs.get(k) == v for k, v in body[0].items())
                ],
            )
        if method == "OpenSession":
            if body[0] != "plain":
                return "ERROR", ("org.freedesktop.DBus.Error.NotSupported",)
            return "vo", (("s", ""), "/org/freedesktop/secrets/session/test")
        if method == "CreateItem":
            item = self.collection + "/item" + str(len(self.items))
            self.items[item] = (
                body[0]["org.freedesktop.Secret.Item.Attributes"][1],
                bytes(body[1][2]),
            )
            return "oo", (item, "/")
        if method == "GetSecret":
            return "(oayays)", ((body[0], b"", self.items[path][1], "text/plain"),)
        if method == "Delete":
            del self.items[path]
            return "o", ("/",)
        raise AssertionError("Unexpected D-Bus operation: " + method)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux Secret Service tests")
class HeadlessSessionTests(unittest.TestCase):
    def test_authenticated_bus_without_hello_reply_is_bounded_and_closed(self):
        with _EmulatedBus() as bus:
            bus.stall_hello = True
            started = time.monotonic()
            report = session.inspect_secret_service()
            self.assertEqual(report["status"], "unavailable")
            self.assertEqual(report["error_type"], "TimeoutError")
            self.assertLess(time.monotonic() - started, 8)
            self.assertEqual(bus.calls, ["Hello"])
            self.assertTrue(bus.hello_peer_closed.wait(1))

    def test_lost_write_reply_still_cleans_disposable_item_and_fails(self):
        with _EmulatedBus() as bus, patch.object(session.subprocess, "run") as run:
            bus.lose_write_reply = True
            with self.assertRaises(CredentialError):
                session.verify_child_storage(consent=True)
            self.assertIn("CreateItem", bus.calls)
            self.assertIn("Delete", bus.calls)
            self.assertEqual(bus.items, {})
            run.assert_not_called()

    def test_lost_write_reply_and_cleanup_failure_remain_unavailable(self):
        with (
            _EmulatedBus() as bus,
            patch.object(KeyringBackend, "delete", side_effect=RuntimeError("sensitive")),
        ):
            bus.lose_write_reply = True
            with self.assertRaisesRegex(CredentialError, "cleanup failed") as raised:
                session.verify_child_storage(consent=True)
            self.assertEqual(len(bus.items), 1)
            self.assertNotIn("sensitive", str(raised.exception))

    def test_absent_session_fails_closed_without_starting_anything(self):
        with (
            patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/missing/karox-bus"}),
            patch("subprocess.run") as run,
            patch.object(session, "_connect") as connect,
        ):
            report = session.inspect_secret_service()
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["reason"], "no-session-bus")
        run.assert_not_called()
        connect.assert_not_called()

    def test_private_socket_and_same_user_required(self):
        with _EmulatedBus() as bus:
            os.chmod(bus.directory.name, 0o755)
            with self.assertRaises(CredentialError):
                session.session_bus_address()
            os.chmod(bus.directory.name, 0o700)
            with patch.object(session.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaises(CredentialError):
                    session.session_bus_address()

    def test_canonical_existing_bus_is_discovered_without_persistent_config(self):
        # Emulate /run/user/<uid>/bus metadata; never create a host user bus.
        from types import SimpleNamespace
        import stat

        uid = os.getuid()
        directory = SimpleNamespace(st_uid=uid, st_mode=stat.S_IFDIR | 0o700)
        socket = SimpleNamespace(st_uid=uid, st_mode=stat.S_IFSOCK | 0o600)
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(Path, "lstat", side_effect=[directory, socket]),
            patch.object(Path, "resolve", return_value=Path(f"/run/user/{uid}")),
        ):
            self.assertEqual(session.session_bus_address(), f"unix:path=/run/user/{uid}/bus")

    def test_unsafe_explicit_addresses_do_not_fall_back(self):
        for address in (
            "",
            "autolaunch:",
            "tcp:host=localhost",
            "unix:abstract=/tmp/a",
            "unix:path=/tmp/a;unix:path=/tmp/b",
            "unix:path=/tmp/a%2fb",
        ):
            with (
                self.subTest(address=address),
                patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": address}),
            ):
                with self.assertRaises(CredentialError):
                    session.session_bus_address()

    def test_doctor_never_activates_or_unlocks_or_creates(self):
        from karox.bridge import BridgeCredentialStore
        from karox.mcp_client import McpCredentialStore
        from karox.browser_credentials import BrowserCredentialStore

        with _EmulatedBus() as bus:
            for running, locked, reason in [
                (False, False, "service-not-running"),
                (True, True, "collection-locked"),
            ]:
                bus.running, bus.locked = running, locked
                self.assertEqual(session.inspect_secret_service()["reason"], reason)
                for store in (
                    CredentialStore(),
                    BridgeCredentialStore(),
                    McpCredentialStore(),
                    BrowserCredentialStore(),
                ):
                    with self.assertRaises(CredentialError):
                        store.doctor()
            self.assertNotIn("StartServiceByName", bus.calls)
            self.assertNotIn("Unlock", bus.calls)
            self.assertNotIn("CreateCollection", bus.calls)
            self.assertNotIn("GetSecret", bus.calls)

    def test_no_default_and_volatile_session_collections_are_not_green(self):
        with _EmulatedBus() as bus:
            bus.collection = "/"
            self.assertEqual(session.inspect_secret_service()["reason"], "no-default-collection")
            bus.collection = "/org/freedesktop/secrets/collection/session"
            self.assertEqual(
                session.inspect_secret_service()["reason"], "non-durable-session-collection"
            )

    def test_actions_require_consent_before_any_side_effect(self):
        with patch.object(session, "_connect") as connect, patch("subprocess.run") as run:
            for action in (
                session.activate_secret_service,
                session.unlock_secret_service,
                session.verify_child_storage,
            ):
                with self.assertRaisesRegex(CredentialError, "consent"):
                    action()
        connect.assert_not_called()
        run.assert_not_called()

    def test_activation_only_with_consent_and_locked_still_unavailable(self):
        with _EmulatedBus() as bus:
            bus.running = False
            bus.locked = True
            report = session.activate_secret_service(consent=True)
            self.assertIn("StartServiceByName", bus.calls)
            self.assertEqual(report["status"], "unavailable")
            self.assertEqual(report["reason"], "collection-locked")

    def test_real_keyring_and_fresh_interpreter_roundtrip_with_cleanup(self):
        with _EmulatedBus() as bus:
            report = session.verify_child_storage(consent=True)
            self.assertEqual(report["verification"], "write-child-read-delete")
            self.assertEqual(bus.items, {})
            self.assertIn("CreateItem", bus.calls)
            self.assertIn("GetSecret", bus.calls)
            self.assertIn("Delete", bus.calls)
            self.assertNotIn("Unlock", bus.calls)

    def test_bridge_credential_is_resolvable_by_independent_process(self):
        from karox.bridge import BridgeCredentialStore

        with _EmulatedBus():
            store = BridgeCredentialStore()
            created = store.set("headless-test", store.generate())
            command = [
                sys.executable,
                "-c",
                (
                    "from karox.bridge import BridgeCredentialStore; "
                    "s=BridgeCredentialStore(); "
                    "print(s.fingerprint(s.resolve('os-keyring:bridge/headless-test')))"
                ),
            ]
            child = subprocess.run(command, capture_output=True, timeout=20, check=False)
            self.assertEqual(child.returncode, 0, child.stderr.decode())
            self.assertEqual(child.stdout.decode().strip(), created["fingerprint"])
            store.delete("headless-test")

    def test_item_locked_after_probe_does_not_prompt_or_fall_back(self):
        with _EmulatedBus() as bus:
            store = CredentialStore()
            store.set("test", "synthetic-test-value")
            bus.item_locked = True
            with self.assertRaisesRegex(CredentialError, "locked"):
                store.resolve("os-keyring:provider/test")
            self.assertNotIn("Unlock", bus.calls)

    def test_plaintext_and_chaining_backend_overrides_are_rejected(self):
        with _EmulatedBus(), patch("keyring.get_keyring") as discover:
            for backend in (
                "keyrings.alt.file.PlaintextKeyring",
                "keyring.backends.chainer.ChainerBackend",
                "keyrings.alt.file.EncryptedKeyring",
            ):
                with patch.dict(os.environ, {"PYTHON_KEYRING_BACKEND": backend}):
                    with self.assertRaisesRegex(CredentialError, "override refused"):
                        CredentialStore().doctor()
            discover.assert_not_called()

    def test_failure_payload_never_contains_dbus_exception_data(self):
        marker = "synthetic-sensitive-error-detail"
        with (
            patch.object(session, "session_bus_address", return_value="unix:path=/fake"),
            patch.object(session, "_connect", side_effect=RuntimeError(marker)),
        ):
            report = session.inspect_secret_service()
        self.assertNotIn(marker, json.dumps(report))
        self.assertEqual(report["status"], "unavailable")

    def test_json_cli_doctor_unavailable_and_setup_plan_are_truthful(self):
        from karox.cli import main

        with _EmulatedBus() as bus:
            bus.running = False
            for command in (["credential", "doctor", "--json"], ["credential", "setup", "--json"]):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    code = main(command)
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(output.getvalue())["status"], "unavailable")
            self.assertNotIn("StartServiceByName", bus.calls)

    def test_verification_cleans_up_after_child_failure_and_timeout(self):
        with _EmulatedBus() as bus:
            for effect in (
                subprocess.CompletedProcess([], 1, b"", b""),
                subprocess.TimeoutExpired("probe", 20),
            ):
                kwargs = (
                    {"side_effect": effect}
                    if isinstance(effect, Exception)
                    else {"return_value": effect}
                )
                with patch.object(session.subprocess, "run", **kwargs):
                    with self.assertRaises(CredentialError):
                        session.verify_child_storage(consent=True)
                self.assertEqual(bus.items, {})

    def test_cleanup_failure_is_not_success(self):
        with (
            _EmulatedBus(),
            patch.object(KeyringBackend, "delete", side_effect=RuntimeError("sensitive")),
        ):
            with self.assertRaisesRegex(CredentialError, "cleanup failed") as raised:
                session.verify_child_storage(consent=True)
        self.assertNotIn("sensitive", str(raised.exception))

    def test_unlock_password_is_private_and_daemon_exit_is_not_proof(self):
        from types import SimpleNamespace
        import stat

        info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o755)
        password = "synthetic-unlock-passphrase"
        with (
            _EmulatedBus() as bus,
            patch.dict(os.environ, {
                "LD_PRELOAD": "/untrusted/library.so",
                "KAROX_PROVIDER_TEST_API_KEY": "synthetic-provider-token",
            }),
            patch.object(Path, "stat", return_value=info),
            patch("sys.stdin.isatty", return_value=True),
            patch.object(session, "session_bus_address", return_value="unix:path=" + str(bus.path)),
            patch.object(session.getpass, "getpass", side_effect=[password, password]),
            patch.object(
                session.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
            ) as run,
        ):
            bus.locked = True
            report = session.unlock_secret_service(consent=True)
            self.assertEqual(report["status"], "unavailable")
            self.assertEqual(run.call_args.kwargs["input"], password.encode())
            self.assertNotIn(password, str(run.call_args.args))
            self.assertNotIn(password, str(run.call_args.kwargs["env"]))
            self.assertNotIn("LD_PRELOAD", run.call_args.kwargs["env"])
            self.assertNotIn("KAROX_PROVIDER_TEST_API_KEY", run.call_args.kwargs["env"])
            self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_unlock_rejects_noninteractive_empty_and_mismatch(self):
        from types import SimpleNamespace
        import stat

        info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o755)
        with (
            _EmulatedBus(),
            patch.object(Path, "stat", return_value=info),
            patch.object(session, "session_bus_address", return_value="unix:path=/fake"),
            patch.object(session.subprocess, "run") as run,
        ):
            with patch("sys.stdin.isatty", return_value=False):
                with self.assertRaisesRegex(CredentialError, "local interactive"):
                    session.unlock_secret_service(consent=True)
            for values in ([""], ["one", "two"]):
                with (
                    patch("sys.stdin.isatty", return_value=True),
                    patch.object(session.getpass, "getpass", side_effect=values),
                ):
                    with self.assertRaises(CredentialError):
                        session.unlock_secret_service(consent=True)
            run.assert_not_called()

    def test_existing_collection_adapter_pins_owner_and_refuses_prompts(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from jeepney import DBusAddress, HeaderFields, new_method_call
        from karox.credential_secret_service import _PinnedConnection

        connection = MagicMock()
        connection.send_and_get_reply.return_value = SimpleNamespace(body=())
        pinned = _PinnedConnection(connection, ":1.7")
        message = new_method_call(
            DBusAddress(
                "/org/freedesktop/secrets",
                "org.freedesktop.secrets",
                "org.freedesktop.Secret.Service",
            ),
            "ReadAlias",
            "s",
            ("default",),
        )
        pinned.send_and_get_reply(message)
        self.assertEqual(message.header.fields[HeaderFields.destination], ":1.7")
        self.assertEqual(connection.send_and_get_reply.call_args.kwargs["timeout"], 5.0)
        with self.assertRaisesRegex(CredentialError, "interactive approval"):
            pinned.filter(None)
        pinned.close()
        connection.close.assert_called_once()

    def test_keyring_property_environment_cannot_change_storage_schema(self):
        with _EmulatedBus(), patch.dict(os.environ, {
            "KEYRING_PROPERTY_SCHEME": "KeePassXC",
            "KEYRING_PROPERTY_GET_PASSWORD": "invalid-override",
        }):
            backend = session.secure_linux_backend()
            self.assertEqual(backend._query("s", "a"), {"service": "s", "username": "a"})
            self.assertTrue(callable(backend.get_password))

    def test_repeated_backend_selection_does_not_register_unbounded_classes(self):
        import keyring.backend

        with _EmulatedBus():
            CredentialStore().doctor()
            count = len(keyring.backend.KeyringBackend._classes)
            for _ in range(5):
                CredentialStore().doctor()
            self.assertEqual(len(keyring.backend.KeyringBackend._classes), count)

    def test_adapter_exceptions_never_escape_with_secret_content(self):
        from unittest.mock import MagicMock

        backend = MagicMock()
        backend.set_password.side_effect = RuntimeError("sensitive-secret")
        backend.get_password.side_effect = RuntimeError("sensitive-secret")
        backend.delete_password.side_effect = RuntimeError("sensitive-secret")
        with patch.object(KeyringBackend, "_module", return_value=backend):
            for call in (
                lambda: KeyringBackend().set("s", "a", "sensitive-secret"),
                lambda: KeyringBackend().get("s", "a"),
                lambda: KeyringBackend().delete("s", "a"),
            ):
                with self.assertRaises(CredentialError) as raised:
                    call()
                self.assertNotIn("sensitive-secret", str(raised.exception))

    def test_hidden_prompt_fallback_is_refused(self):
        from types import SimpleNamespace
        import getpass
        import stat

        info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o755)
        with (
            _EmulatedBus(),
            patch.object(Path, "stat", return_value=info),
            patch.object(session, "session_bus_address", return_value="unix:path=/fake"),
            patch("sys.stdin.isatty", return_value=True),
            patch.object(session.getpass, "getpass", side_effect=getpass.GetPassWarning),
            patch.object(session.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(CredentialError, "non-echoing"):
                session.unlock_secret_service(consent=True)
            run.assert_not_called()

    def test_test_isolation_blocks_consent_actions_too(self):
        with (
            patch.dict(
                os.environ, {"KAROX_TEST_ISOLATION": "1", "KAROX_TEST_ALLOW_REAL_KEYRING": ""}
            ),
            patch("subprocess.run") as run,
            patch.object(session, "_connect") as connect,
        ):
            with self.assertRaisesRegex(CredentialError, "test isolation"):
                session.activate_secret_service(consent=True)
        run.assert_not_called()
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
