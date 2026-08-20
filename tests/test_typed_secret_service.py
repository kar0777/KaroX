"""Tests for the typed secret service: OAuth approval password vs static Bearer.

The TUI ServiceConnectScreen P key and the CLI
``karox bridge oauth approval-password`` share one canonical service
(:func:`karox.web_bridge_launcher.copy_oauth_approval_password`). This ensures:
  * the P key copies the OAuth approval password, never a generic Bearer;
  * OAuth and static Bearer actions use different typed purposes;
  * the secret is never returned to the UI — only fingerprint and status;
  * clipboard auto-clear is scheduled;
  * credential rotation produces the current value, not a stale log value.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.web_bridge_launcher import (
    SecretPurpose,
    copy_oauth_approval_password,
    copy_static_bearer_credential_for_saved_bridge,
    copy_static_bearer_credential_reference,
    copy_static_bearer_credential_for_connection,
    resolve_oauth_approval_password,
)


class TypedSecretPurposeTests(unittest.TestCase):
    def test_oauth_purpose_is_distinct_from_bearer_purpose(self) -> None:
        self.assertNotEqual(SecretPurpose.OAUTH_APPROVAL_PASSWORD, SecretPurpose.STATIC_BEARER)

    def test_oauth_purpose_constant_value(self) -> None:
        self.assertEqual(SecretPurpose.OAUTH_APPROVAL_PASSWORD, "oauth_approval_password")

    def test_bearer_purpose_constant_value(self) -> None:
        self.assertEqual(SecretPurpose.STATIC_BEARER, "static_bearer")


class CopyOAuthApprovalPasswordTests(unittest.TestCase):
    def test_returns_oauth_purpose_not_bearer(self) -> None:
        with patch("karox.web_bridge_launcher.resolve_oauth_approval_password", return_value=("secret-value", "os-keyring:bridge/test")):
            with patch("karox.clipboard.write_text", return_value=True):
                with patch("karox.clipboard.schedule_clear", return_value=None):
                    result = copy_oauth_approval_password("test-profile")
        self.assertEqual(result.purpose, SecretPurpose.OAUTH_APPROVAL_PASSWORD)
        self.assertNotEqual(result.purpose, SecretPurpose.STATIC_BEARER)

    def test_copies_to_clipboard_when_credential_exists(self) -> None:
        with patch("karox.web_bridge_launcher.resolve_oauth_approval_password", return_value=("secret-value", "os-keyring:bridge/test")):
            with patch("karox.clipboard.write_text", return_value=True) as write:
                with patch("karox.clipboard.schedule_clear", return_value=None) as clear:
                    result = copy_oauth_approval_password("test-profile")
        self.assertTrue(result.copied)
        write.assert_called_once_with("secret-value")
        clear.assert_called_once()

    def test_returns_error_when_credential_missing(self) -> None:
        with patch("karox.web_bridge_launcher.resolve_oauth_approval_password", return_value=(None, None)):
            with patch("karox.clipboard.write_text", return_value=True) as write:
                result = copy_oauth_approval_password("missing-profile")
        self.assertFalse(result.copied)
        self.assertIsNotNone(result.error)
        self.assertIn("missing-profile", result.error)
        write.assert_not_called()

    def test_result_never_contains_secret(self) -> None:
        with patch("karox.web_bridge_launcher.resolve_oauth_approval_password", return_value=("super-secret-value-12345", "os-keyring:bridge/test")):
            with patch("karox.clipboard.write_text", return_value=True):
                with patch("karox.clipboard.schedule_clear", return_value=None):
                    result = copy_oauth_approval_password("test-profile")
        # Result is a dataclass — check all fields for the secret
        self.assertNotIn("super-secret-value-12345", result.fingerprint)
        self.assertNotIn("super-secret-value-12345", result.reference or "")
        self.assertNotIn("super-secret-value-12345", result.error or "")
        self.assertNotIn("super-secret-value-12345", result.purpose)
        # Fingerprint should be sha256 prefix, not the raw secret
        self.assertTrue(result.fingerprint.startswith("sha256:"))

    def test_clipboard_unavailable_returns_copied_false(self) -> None:
        with patch("karox.web_bridge_launcher.resolve_oauth_approval_password", return_value=("secret", "os-keyring:bridge/test")):
            with patch("karox.clipboard.write_text", return_value=False):
                with patch("karox.clipboard.schedule_clear", return_value=None):
                    result = copy_oauth_approval_password("test")
        self.assertFalse(result.copied)
        # Still scheduled clear even if clipboard write failed
        self.assertEqual(result.auto_clear_seconds, 120)

    def test_rotated_credential_produces_current_value(self) -> None:
        """Rotation changes the keyring value; the service resolves the current one."""
        # First call resolves the old credential
        with patch("karox.web_bridge_launcher.resolve_oauth_approval_password", return_value=("old-secret", "ref")):
            with patch("karox.clipboard.write_text", return_value=True) as write1:
                with patch("karox.clipboard.schedule_clear"):
                    result1 = copy_oauth_approval_password("test")
        self.assertEqual(write1.call_args.args[0], "old-secret")
        fp1 = result1.fingerprint

        # Second call after rotation resolves the new credential
        with patch("karox.web_bridge_launcher.resolve_oauth_approval_password", return_value=("new-secret", "ref")):
            with patch("karox.clipboard.write_text", return_value=True) as write2:
                with patch("karox.clipboard.schedule_clear"):
                    result2 = copy_oauth_approval_password("test")
        self.assertEqual(write2.call_args.args[0], "new-secret")
        fp2 = result2.fingerprint
        self.assertNotEqual(fp1, fp2)


class CopyStaticBearerTests(unittest.TestCase):
    def test_saved_bridge_copies_authorization_bearer_format(self) -> None:
        with patch(
            "karox.web_bridge_launcher.resolve_oauth_approval_password",
            return_value=("fixture-value", "os-keyring:bridge/notion"),
        ):
            with patch("karox.clipboard.write_text", return_value=True) as write:
                with patch("karox.clipboard.schedule_clear", return_value=None) as clear:
                    result = copy_static_bearer_credential_for_saved_bridge("notion")
        self.assertTrue(result.copied)
        self.assertEqual(result.purpose, SecretPurpose.STATIC_BEARER)
        write.assert_called_once_with("Bearer fixture-value")
        clear.assert_called_once()
        self.assertNotIn("fixture-value", repr(result))

    def test_saved_bridge_missing_credential_is_an_error(self) -> None:
        with patch(
            "karox.web_bridge_launcher.resolve_oauth_approval_password",
            return_value=(None, None),
        ):
            result = copy_static_bearer_credential_for_saved_bridge("notion")
        self.assertFalse(result.copied)
        self.assertEqual(result.purpose, SecretPurpose.STATIC_BEARER)
        self.assertIn("notion", result.error or "")

    def test_returns_bearer_purpose_not_oauth(self) -> None:
        with patch("karox.connections.ConnectionCredentialStore") as MockStore:
            store = MockStore.return_value
            store.resolve.return_value = "clickup-api-key"
            with patch("karox.clipboard.write_text", return_value=True):
                with patch("karox.clipboard.schedule_clear", return_value=None):
                    result = copy_static_bearer_credential_reference("os-keyring:connection/test")
        self.assertEqual(result.purpose, SecretPurpose.STATIC_BEARER)
        self.assertNotEqual(result.purpose, SecretPurpose.OAUTH_APPROVAL_PASSWORD)

    def test_copies_bearer_format(self) -> None:
        with patch("karox.connections.ConnectionCredentialStore") as MockStore:
            store = MockStore.return_value
            store.resolve.return_value = "my-api-key"
            with patch("karox.clipboard.write_text", return_value=True) as write:
                with patch("karox.clipboard.schedule_clear", return_value=None):
                    result = copy_static_bearer_credential_reference("os-keyring:connection/test")
        self.assertTrue(result.copied)
        # Must be Bearer <secret> format, not raw secret
        write.assert_called_once_with("Bearer my-api-key")

    def test_empty_reference_returns_error(self) -> None:
        result = copy_static_bearer_credential_reference("")
        self.assertFalse(result.copied)
        self.assertIsNotNone(result.error)

    def test_missing_credential_returns_error(self) -> None:
        from karox.credentials import CredentialError
        with patch("karox.connections.ConnectionCredentialStore") as MockStore:
            store = MockStore.return_value
            store.resolve.side_effect = CredentialError("not found")
            result = copy_static_bearer_credential_reference("os-keyring:connection/missing")
        self.assertFalse(result.copied)
        self.assertIsNotNone(result.error)

    def test_result_never_contains_raw_secret(self) -> None:
        with patch("karox.connections.ConnectionCredentialStore") as MockStore:
            store = MockStore.return_value
            store.resolve.return_value = "raw-bearer-secret"
            with patch("karox.clipboard.write_text", return_value=True):
                with patch("karox.clipboard.schedule_clear", return_value=None):
                    result = copy_static_bearer_credential_reference("os-keyring:connection/test")
        self.assertNotIn("raw-bearer-secret", result.fingerprint)
        self.assertNotIn("raw-bearer-secret", result.reference or "")
        self.assertTrue(result.fingerprint.startswith("sha256:"))

    def test_for_connection_looks_up_registry(self) -> None:
        """copy_static_bearer_credential_for_connection resolves via registry."""
        mock_target = type("MockTarget", (), {
            "credential_ref": "os-keyring:connection/c-test",
        })()
        with patch("karox.connections.connection_registry") as mock_reg_fn:
            mock_reg_fn.return_value.get.return_value = mock_target
            with patch("karox.connections.ConnectionCredentialStore") as MockStore:
                store = MockStore.return_value
                store.resolve.return_value = "secret-val"
                with patch("karox.clipboard.write_text", return_value=True):
                    with patch("karox.clipboard.schedule_clear", return_value=None):
                        result = copy_static_bearer_credential_for_connection("c-test")
        self.assertTrue(result.copied)
        self.assertEqual(result.purpose, SecretPurpose.STATIC_BEARER)

    def test_for_connection_no_credential_ref_returns_error(self) -> None:
        mock_target = type("MockTarget", (), {"credential_ref": None})()
        with patch("karox.connections.connection_registry") as mock_reg_fn:
            mock_reg_fn.return_value.get.return_value = mock_target
            result = copy_static_bearer_credential_for_connection("c-test")
        self.assertFalse(result.copied)
        self.assertIsNotNone(result.error)


class ResolveOAuthApprovalPasswordTests(unittest.TestCase):
    def test_returns_none_when_no_credential(self) -> None:
        from karox.credentials import CredentialError
        with patch("karox.web_bridge_launcher.saved_web_bridge_session_candidates", return_value=("session-1",)):
            with patch("karox.bridge.BridgeCredentialStore") as MockStore:
                store = MockStore.return_value
                store.resolve.side_effect = CredentialError("not found")
                secret, ref = resolve_oauth_approval_password("missing")
        self.assertIsNone(secret)
        self.assertIsNone(ref)

    def test_returns_secret_and_reference_when_found(self) -> None:
        with patch("karox.web_bridge_launcher.saved_web_bridge_session_candidates", return_value=("session-1",)):
            with patch("karox.bridge.BridgeCredentialStore") as MockStore:
                store = MockStore.return_value
                store.resolve.return_value = "the-secret"
                secret, ref = resolve_oauth_approval_password("found")
        self.assertEqual(secret, "the-secret")
        self.assertEqual(ref, "os-keyring:bridge/session-1")


class DiscoverSavedBridgeProfilesTests(unittest.TestCase):
    """B7: discover saved bridge profiles by target profile, not display name."""

    @staticmethod
    def _write_profile(
        directory: Path,
        *,
        saved_profile: str = "clickup-opus",
        profile: str = "chatgpt-web",
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_profile": saved_profile,
            "profile": profile,
            "session_id": f"web-saved-{saved_profile}",
            "public_url": "https://example.test",
            "tunnel": "tailscale",
            "bridge_pid": 1234,
            "persistent_session": True,
            "credential": "must-not-return",
            "token": "must-not-return",
        }
        (directory / f"web-saved-{saved_profile}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_discover_saved_bridge_profiles_reads_web_bridge_dir(self) -> None:
        from karox.web_bridge_launcher import discover_saved_bridge_profiles

        with tempfile.TemporaryDirectory() as temporary:
            bridge_dir = Path(temporary)
            self._write_profile(bridge_dir)
            with patch("karox.web_bridge_launcher.watchdog_dir", return_value=bridge_dir):
                profiles = discover_saved_bridge_profiles()
        chatgpt_profiles = [p for p in profiles if p.get("profile") == "chatgpt-web"]
        self.assertEqual(len(chatgpt_profiles), 1)
        p = chatgpt_profiles[0]
        self.assertEqual(p["saved_profile"], "clickup-opus")
        self.assertTrue(p["session_id"].startswith("web-saved-"))
        self.assertTrue(p["public_url"].startswith("https://"))

    def test_discover_for_chatgpt_web_preset_finds_chatgpt_profile(self) -> None:
        from karox.web_bridge_launcher import discover_saved_bridge_profiles_for_preset

        with tempfile.TemporaryDirectory() as temporary:
            bridge_dir = Path(temporary)
            self._write_profile(bridge_dir)
            with patch("karox.web_bridge_launcher.watchdog_dir", return_value=bridge_dir):
                profiles = discover_saved_bridge_profiles_for_preset("chatgpt-web")
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["profile"], "chatgpt-web")

    def test_discover_for_clickup_preset_finds_generic_streamable(self) -> None:
        from karox.web_bridge_launcher import discover_saved_bridge_profiles_for_preset

        with tempfile.TemporaryDirectory() as temporary:
            bridge_dir = Path(temporary)
            self._write_profile(
                bridge_dir,
                saved_profile="clickup-generic",
                profile="generic-streamable-http",
            )
            with patch("karox.web_bridge_launcher.watchdog_dir", return_value=bridge_dir):
                profiles = discover_saved_bridge_profiles_for_preset("clickup")
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["profile"], "generic-streamable-http")

    def test_discover_never_returns_secrets(self) -> None:
        from karox.web_bridge_launcher import discover_saved_bridge_profiles

        with tempfile.TemporaryDirectory() as temporary:
            bridge_dir = Path(temporary)
            self._write_profile(bridge_dir)
            with patch("karox.web_bridge_launcher.watchdog_dir", return_value=bridge_dir):
                profiles = discover_saved_bridge_profiles()
        self.assertEqual(len(profiles), 1)
        for key in ("credential", "token", "secret", "bridge_credential"):
            self.assertNotIn(key, profiles[0])

    def test_discover_returns_empty_list_when_dir_missing(self) -> None:
        from karox.web_bridge_launcher import discover_saved_bridge_profiles
        with patch("karox.web_bridge_launcher.watchdog_dir", return_value=Path("/nonexistent")):
            profiles = discover_saved_bridge_profiles()
        self.assertEqual(profiles, [])


if __name__ == "__main__":
    unittest.main()
