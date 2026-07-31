from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _path_setup
from karox.credentials import CredentialBackend
from karox.remote_lease import EllipsisLeaseError, EllipsisLeaseStore


class MemoryCredentialBackend(CredentialBackend):
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.values[(service, account)] = secret

    def get(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def delete(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


class EllipsisLeaseTests(unittest.TestCase):
    def test_lease_is_bound_and_persisted_without_secret(self) -> None:
        with tempfile.TemporaryDirectory(prefix="karox-lease-") as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            backend = MemoryCredentialBackend()
            store = EllipsisLeaseStore(root / "leases", backend=backend)
            lease, secret = store.mint(
                credential_name="lease-one",
                local_session_id="local-one",
                ellipsis_session_id="ellipsis-one",
                repository=repository,
                access_profile="workspace_write",
                ttl_seconds=300,
            )
            persisted = store.path("lease-one").read_text(encoding="utf-8")
            self.assertNotIn(secret, persisted)
            self.assertNotIn(str(repository), persisted)
            self.assertEqual(json.loads(persisted)["fingerprint"], lease.fingerprint)
            self.assertEqual(
                store.resolve(
                    "lease-one",
                    local_session_id="local-one",
                    repository=repository,
                    access_profile="workspace_write",
                ),
                secret,
            )
            with self.assertRaises(EllipsisLeaseError):
                store.validate("lease-one", ellipsis_session_id="ellipsis-two")
            other = root / "other"
            other.mkdir()
            with self.assertRaises(EllipsisLeaseError):
                store.validate("lease-one", repository=other)

    def test_revoke_deletes_secret_and_stops_session_processes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="karox-lease-revoke-") as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            backend = MemoryCredentialBackend()
            store = EllipsisLeaseStore(root / "leases", backend=backend)
            store.mint(
                credential_name="lease-two",
                local_session_id="local-two",
                ellipsis_session_id="ellipsis-two",
                repository=repository,
                access_profile="workspace_write",
                ttl_seconds=300,
            )
            with patch("karox.remote_lease.stop_session_processes") as cleanup:
                revoked = store.revoke("lease-two")
            cleanup.assert_called_once_with("local-two")
            self.assertIsNotNone(revoked.revoked_at)
            with self.assertRaises(EllipsisLeaseError):
                store.validate("lease-two")
            self.assertEqual(backend.values, {})


if __name__ == "__main__":
    unittest.main()
