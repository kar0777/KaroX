from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox.credentials import CredentialError, CredentialReference, CredentialStore


class MemoryBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.values[(service, account)] = secret

    def get(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def delete(self, service: str, account: str) -> None:
        if self.values.pop((service, account), None) is None:
            raise CredentialError("credential does not exist")


class ExplodingBackend(MemoryBackend):
    def get(self, service: str, account: str) -> str | None:
        secret = next(iter(self.values.values()))
        raise RuntimeError(f"backend leaked {secret}")


class CredentialStoreTests(unittest.TestCase):
    def test_round_trip_uses_only_opaque_references_and_fingerprints(self) -> None:
        backend = MemoryBackend()
        store = CredentialStore(backend)
        secret = "harmless-test-provider-key"

        created = store.set("primary", secret)

        self.assertEqual(created["reference"], "os-keyring:provider/primary")
        self.assertRegex(created["fingerprint"], r"^sha256:[0-9a-f]{12}$")
        self.assertNotIn(secret, str(created))
        self.assertEqual(store.resolve(created["reference"]), secret)
        self.assertEqual(store.accessor(created["reference"])(), secret)
        self.assertEqual(store.delete("primary")["status"], "deleted")
        with self.assertRaises(CredentialError):
            store.resolve(created["reference"])

    def test_rejects_invalid_references_and_secret_controls(self) -> None:
        store = CredentialStore(MemoryBackend())
        for reference in ("primary", "env:KEY", "os-keyring:provider/../bad"):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                CredentialReference.parse(reference)
        for secret in ("", "line\nfeed", "nul\x00byte"):
            with self.subTest(secret=secret), self.assertRaises(ValueError):
                store.set("primary", secret)

    def test_environment_credentials_resolve_with_no_keyring_at_all(self) -> None:
        secret = "harmless-ci-provider-key"
        reference = "env:KAROX_PROVIDER_CI_API_KEY"
        store = CredentialStore()

        with (
            # A container or CI runner has no Secret Service, and frequently no
            # keyring module either.
            patch.dict(sys.modules, {"keyring": None}),
            patch.dict(
                os.environ,
                # A trailing newline is what `KEY=$(cat key.txt)` leaves behind.
                {"KAROX_PROVIDER_CI_API_KEY": f"{secret}\n"},
            ),
        ):
            self.assertEqual(store.resolve(reference), secret)
            self.assertEqual(store.accessor(reference)(), secret)
            report = store.doctor(reference)
            with self.assertRaises(CredentialError):
                store.doctor()

        self.assertEqual(report["backend"], "environment")
        self.assertEqual(report["protection"], "process-environment")
        self.assertEqual(report["status"], "ok")
        self.assertNotIn("os-keyring", str(report))
        with patch.dict(sys.modules, {"keyring": None}):
            self.assertEqual(store.doctor(reference)["status"], "missing")
            with self.assertRaises(CredentialError):
                store.resolve(reference)

    def test_environment_references_stay_inside_the_karox_namespace(self) -> None:
        for reference in (
            "env:AWS_SECRET_ACCESS_KEY",
            "env:GITHUB_TOKEN",
            "env:KAROX_PROVIDER_API_KEY",
            "env:karox_provider_ci_api_key",
            "env:KAROX_PROVIDER_CI_API_KEY_",
            "env:",
        ):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                CredentialReference.parse(reference)
        self.assertEqual(
            str(CredentialReference.parse("env:KAROX_PROVIDER_CI_API_KEY")),
            "env:KAROX_PROVIDER_CI_API_KEY",
        )

    def test_a_missing_keyring_never_installs_software(self) -> None:
        with (
            patch.dict(sys.modules, {"keyring": None}),
            patch("subprocess.run") as run,
            self.assertRaises(CredentialError) as raised,
        ):
            CredentialStore().resolve("os-keyring:provider/primary")

        # Resolving a secret is a read; it must not reach a package index.
        run.assert_not_called()
        self.assertIn("pip install keyring", str(raised.exception))
        self.assertIn("env:KAROX_PROVIDER_", str(raised.exception))

    def test_backend_exceptions_do_not_expose_secret_values(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        backend = ExplodingBackend()
        backend.set("KaroX/provider", "primary", secret)
        store = CredentialStore(backend)

        with self.assertRaises(CredentialError) as raised:
            store.resolve("os-keyring:provider/primary")

        self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
