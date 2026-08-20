from __future__ import annotations

import json

import pytest

from karox.browser_credentials import (
    BrowserCredentialError,
    BrowserCredentialReference,
    BrowserCredentialStore,
)


class MemoryCredentialBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.values[(service, account)] = secret

    def get(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def delete(self, service: str, account: str) -> None:
        try:
            del self.values[(service, account)]
        except KeyError as exc:
            raise BrowserCredentialError("credential does not exist") from exc


def test_reference_is_opaque_and_round_trips() -> None:
    reference = BrowserCredentialReference("gmail-test-1")

    assert str(reference) == "os-keyring:browser/gmail-test-1"
    assert BrowserCredentialReference.parse(str(reference)) == reference


@pytest.mark.parametrize("name", ["", " space", "slash/name", "x" * 129])
def test_reference_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError):
        BrowserCredentialReference(name)


def test_store_returns_only_secret_free_metadata() -> None:
    backend = MemoryCredentialBackend()
    store = BrowserCredentialStore(backend)

    result = store.set(
        "gmail-test-1",
        username="tester@example.invalid",
        password="example-passphrase",
    )

    serialized = json.dumps(result, sort_keys=True)
    assert result["reference"] == "os-keyring:browser/gmail-test-1"
    assert result["backend"] == "os-keyring"
    assert result["fingerprint"].startswith("sha256:")
    assert "tester@example.invalid" not in serialized
    assert "example-passphrase" not in serialized


def test_username_and_password_are_resolved_only_by_field() -> None:
    backend = MemoryCredentialBackend()
    store = BrowserCredentialStore(backend)
    store.set(
        "account-1",
        username="tester@example.invalid",
        password="example-passphrase",
    )

    reference = "os-keyring:browser/account-1"
    assert store.resolve_field(reference, "username") == "tester@example.invalid"
    assert store.resolve_field(reference, "password") == "example-passphrase"


def test_browser_credentials_use_dedicated_keyring_namespace() -> None:
    backend = MemoryCredentialBackend()
    store = BrowserCredentialStore(backend)
    store.set("account-1", username="tester", password="example-passphrase")

    assert ("KaroX/browser", "account-1") in backend.values
    assert all(service == "KaroX/browser" for service, _account in backend.values)


def test_missing_reference_fails_closed_without_secret_material() -> None:
    store = BrowserCredentialStore(MemoryCredentialBackend())

    with pytest.raises(BrowserCredentialError) as exc_info:
        store.resolve_field("os-keyring:browser/missing", "password")

    assert "missing" in str(exc_info.value)
    assert "password" not in str(exc_info.value).lower()


def test_corrupt_keyring_payload_fails_closed() -> None:
    backend = MemoryCredentialBackend()
    backend.values[("KaroX/browser", "broken")] = "not-json"
    store = BrowserCredentialStore(backend)

    with pytest.raises(BrowserCredentialError, match="invalid format"):
        store.resolve_field("os-keyring:browser/broken", "username")


def test_unknown_payload_version_fails_closed() -> None:
    backend = MemoryCredentialBackend()
    backend.values[("KaroX/browser", "future")] = json.dumps(
        {"version": 999, "username": "tester", "password": "value"}
    )
    store = BrowserCredentialStore(backend)

    with pytest.raises(BrowserCredentialError, match="unknown format"):
        store.resolve_field("os-keyring:browser/future", "username")


def test_control_characters_are_rejected_before_keyring_write() -> None:
    backend = MemoryCredentialBackend()
    store = BrowserCredentialStore(backend)

    with pytest.raises(ValueError, match="control characters"):
        store.set("account-1", username="tester", password="bad\nvalue")

    assert backend.values == {}


def test_delete_returns_only_reference_and_status() -> None:
    backend = MemoryCredentialBackend()
    store = BrowserCredentialStore(backend)
    store.set("account-1", username="tester", password="example-passphrase")

    result = store.delete("account-1")

    assert result == {
        "reference": "os-keyring:browser/account-1",
        "status": "deleted",
    }
    assert backend.values == {}


def test_doctor_reports_os_protected_browser_scope() -> None:
    store = BrowserCredentialStore(MemoryCredentialBackend())

    assert store.doctor() == {
        "status": "ok",
        "backend": "os-keyring",
        "protection": "os-protected",
        "scope": "browser",
    }
