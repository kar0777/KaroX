"""Secret-safety contract for bridge credential rotation.

Phase 0 of the recovery plan requires that rotating a bridge credential never
exposes the new value through stdout, stderr, logs or snapshots.  These tests
pin that contract at three layers:

* the ``BridgeCredentialStore`` library API never returns the secret;
* the ``rotate-key`` CLI command emits only a fingerprint and an opaque
  reference unless an explicit, double-flagged ``--reveal-secret`` is given;
* the ``--copy`` path hands ``Bearer <secret>`` to the clipboard (and schedules
  an auto-clear) without ever printing it, and degrades safely when no
  clipboard is available.

No test here reads a real OS keyring or a real clipboard: a fake backend holds
the value and the clipboard module is monkeypatched to record what it received.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from karox import clipboard
from karox.bridge import BridgeCredentialStore
from karox.credentials import CredentialError


class _FakeCredentialBackend:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.store[(service, account)] = secret

    def get(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def delete(self, service: str, account: str) -> None:
        if (service, account) not in self.store:
            raise CredentialError("credential does not exist")
        self.store.pop((service, account), None)


class _RecordingClipboard:
    def __init__(self) -> None:
        self.written: list[str] = []
        self.cleared_after: list[int] = []

    def write_text(self, text: str) -> bool:
        self.written.append(text)
        return True

    def schedule_clear(self, seconds: int = clipboard.CLIPBOARD_AUTO_CLEAR_SECONDS) -> None:
        self.cleared_after.append(int(seconds))


class _UnavailableClipboard:
    def __init__(self) -> None:
        self.write_calls = 0

    def write_text(self, text: str) -> bool:
        self.write_calls += 1
        return False

    def schedule_clear(self, seconds: int = clipboard.CLIPBOARD_AUTO_CLEAR_SECONDS) -> None:
        # Still invoked even when the copy itself fails: the scheduler is
        # fail-soft by design and must not raise.
        return None


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Invoke the karox CLI capturing stdout/stderr without touching the desk.

    Imported lazily so a collection-time import error in another test module
    cannot block these assertions. ``SystemExit`` (how the CLI signals a bad
    flag combination) is captured as a non-zero return code rather than raised.
    """
    from karox import cli

    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


class BridgeCredentialStoreRotateSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = _FakeCredentialBackend()
        self.store = BridgeCredentialStore(backend=self.backend)

    def test_rotate_omits_secret_from_result(self) -> None:
        self.store.set("bridge-1", "old-value")
        rotated = self.store.rotate("bridge-1")
        self.assertNotIn("secret", rotated)
        self.assertEqual(rotated["status"], "rotated")
        self.assertTrue(rotated["fingerprint"].startswith("sha256:"))

    def test_rotate_replaces_value(self) -> None:
        self.store.set("bridge-1", "old-value")
        self.store.rotate("bridge-1")
        self.assertNotEqual(
            self.store.resolve("os-keyring:bridge/bridge-1"), "old-value"
        )

    def test_rotate_failure_preserves_previous_credential(self) -> None:
        self.store.set("bridge-1", "previous-value")
        with mock.patch.object(self.backend, "set", side_effect=RuntimeError("offline")):
            with self.assertRaises(CredentialError):
                self.store.rotate("bridge-1")
        # The old value must still resolve: a failed write must not have left a
        # deleted intermediate state.
        self.assertEqual(
            self.store.resolve("os-keyring:bridge/bridge-1"), "previous-value"
        )


class RotateKeyCliSafetyTests(unittest.TestCase):
    """The ``rotate-key`` command is the user-facing rotation surface."""

    def setUp(self) -> None:
        self.backend = _FakeCredentialBackend()
        # Seed an existing credential so the "old value" is defined and so we
        # can assert the new value differs from it.
        self.store = BridgeCredentialStore(backend=self.backend)
        self.store.set("clickup-opus", "seed-existing-value")

        self.clipboard = _RecordingClipboard()

        self._patches = [
            # The CLI imported the class into its own namespace at load time, so
            # patching ``karox.bridge.BridgeCredentialStore`` is not enough --
            # ``cli.BridgeCredentialStore`` is what the handler actually calls.
            # Redirect that binding so no run ever reaches the real OS keyring.
            mock.patch("karox.cli.BridgeCredentialStore", self._make_store),
            mock.patch.object(clipboard, "write_text", self.clipboard.write_text),
            mock.patch.object(clipboard, "schedule_clear", self.clipboard.schedule_clear),
        ]
        for patcher in self._patches:
            patcher.start()
        self.addCleanup(self._stop_patches)

    def _make_store(self, backend=None) -> BridgeCredentialStore:
        # The CLI constructs ``BridgeCredentialStore()`` with no arguments; we
        # redirect every such construction to a store wired to our fake backend
        # so no real keyring is touched.
        return BridgeCredentialStore(backend=self.backend)

    def _stop_patches(self) -> None:
        for patcher in self._patches:
            patcher.stop()

    def _new_secret_now_in_keyring(self) -> str:
        return self.backend.store[("KaroX/bridge", "clickup-opus")]

    def test_copy_current_credential_puts_bearer_on_clipboard_without_printing(self) -> None:
        code, out, err = _run_cli(
            ["bridge", "credential", "copy", "clickup-opus", "--quiet", "--json"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.clipboard.written, ["Bearer seed-existing-value"])
        self.assertEqual(
            self.clipboard.cleared_after,
            [clipboard.CLIPBOARD_AUTO_CLEAR_SECONDS],
        )
        payload = json.loads(out)
        self.assertEqual(payload["status"], "copied")
        self.assertEqual(
            payload["fingerprint"],
            BridgeCredentialStore.fingerprint("seed-existing-value"),
        )
        self.assertEqual(
            payload["auto_clear_seconds"],
            clipboard.CLIPBOARD_AUTO_CLEAR_SECONDS,
        )
        self.assertNotIn("seed-existing-value", out + err)

    def test_copy_current_credential_clipboard_failure_is_nonzero_and_secret_safe(self) -> None:
        unavailable = _UnavailableClipboard()
        with mock.patch.object(clipboard, "write_text", unavailable.write_text):
            code, out, err = _run_cli(
                ["bridge", "credential", "copy", "clickup-opus", "--json"]
            )
        self.assertEqual(code, 1)
        self.assertGreaterEqual(unavailable.write_calls, 1)
        self.assertNotIn("seed-existing-value", out + err)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "clipboard_unavailable")
        self.assertEqual(payload["auto_clear_seconds"], 0)
        self.assertIn("not printed", err)

    def test_plain_rotation_never_prints_secret(self) -> None:
        code, out, err = _run_cli(
            ["bridge", "credential", "rotate-key", "clickup-opus", "--json"]
        )
        self.assertEqual(code, 0)
        new_secret = self._new_secret_now_in_keyring()
        self.assertNotIn("seed-existing-value", out)
        self.assertNotIn(new_secret, out)
        self.assertNotIn(new_secret, err)
        payload = json.loads(out)
        self.assertNotIn("secret", payload)
        self.assertEqual(payload["status"], "rotated")
        self.assertTrue(payload["fingerprint"].startswith("sha256:"))

    def test_copy_puts_bearer_value_on_clipboard_not_stdout(self) -> None:
        code, out, err = _run_cli(
            ["bridge", "credential", "rotate-key", "clickup-opus", "--copy", "--quiet", "--json"]
        )
        self.assertEqual(code, 0)
        new_secret = self._new_secret_now_in_keyring()

        self.assertEqual(self.clipboard.written, [f"Bearer {new_secret}"])
        self.assertEqual(self.clipboard.cleared_after, [clipboard.CLIPBOARD_AUTO_CLEAR_SECONDS])
        self.assertEqual(json.loads(out)["clipboard"], "copied")
        # The secret must not leak through any of the printed channels even
        # though it was on the clipboard.
        self.assertNotIn(new_secret, out)
        self.assertNotIn(new_secret, err)

    def test_clipboard_unavailable_does_not_print_secret(self) -> None:
        unavailable = _UnavailableClipboard()
        with mock.patch.object(clipboard, "write_text", unavailable.write_text):
            code, out, err = _run_cli(
                ["bridge", "credential", "rotate-key", "clickup-opus", "--copy", "--json"]
            )
        self.assertEqual(code, 0)
        new_secret = self._new_secret_now_in_keyring()
        self.assertGreaterEqual(unavailable.write_calls, 1)
        self.assertNotIn(new_secret, out)
        self.assertNotIn(new_secret, err)
        self.assertEqual(json.loads(out)["clipboard"], "unavailable")
        self.assertIn("not printed", err)

    def test_reveal_secret_requires_explicit_yes(self) -> None:
        code, out, err = _run_cli(
            ["bridge", "credential", "rotate-key", "clickup-opus", "--reveal-secret", "--json"]
        )
        self.assertNotEqual(code, 0)
        # Refused before rotating: the original value is still in place.
        self.assertEqual(self._new_secret_now_in_keyring(), "seed-existing-value")
        self.assertNotIn("seed-existing-value", out)

    def test_reveal_secret_with_yes_prints_secret_with_warning(self) -> None:
        code, out, err = _run_cli(
            [
                "bridge",
                "credential",
                "rotate-key",
                "clickup-opus",
                "--reveal-secret",
                "--yes",
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        new_secret = self._new_secret_now_in_keyring()
        payload = json.loads(out)
        self.assertEqual(payload["secret"], new_secret)
        self.assertIn("WARNING", err)
        self.assertNotIn("seed-existing-value", out)

    def test_rotation_changes_value_and_is_atomic(self) -> None:
        code, out, _ = _run_cli(
            ["bridge", "credential", "rotate-key", "clickup-opus", "--json"]
        )
        self.assertEqual(code, 0)
        new_secret = self._new_secret_now_in_keyring()
        self.assertNotEqual(new_secret, "seed-existing-value")
        # Fingerprint in output matches the value actually stored.
        self.assertEqual(json.loads(out)["fingerprint"], BridgeCredentialStore.fingerprint(new_secret))


class ClipboardHelperTests(unittest.TestCase):
    def test_bearer_value_format(self) -> None:
        self.assertEqual(clipboard.bearer_value("abc"), "Bearer abc")

    def test_bearer_value_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            clipboard.bearer_value("")

    def test_schedule_clear_is_fail_soft(self) -> None:
        # A broken writer inside the timer callback must not raise out of the
        # scheduler construction path.
        with mock.patch.object(clipboard, "write_text", side_effect=RuntimeError("boom")):
            timer = clipboard.schedule_clear(0)
        try:
            # The timer fires almost immediately (clamped to >=1s); wait for it
            # so the swallowed exception is exercised.
            if timer is not None:
                timer.join(timeout=2)
        finally:
            pass


if __name__ == "__main__":
    unittest.main()
