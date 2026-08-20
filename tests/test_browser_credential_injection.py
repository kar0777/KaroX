"""Contracts for secret-safe local browser credential injection."""
from __future__ import annotations

import pytest

from karox.browser_credential_injection import (
    BrowserCredentialInjectionError,
    inject_browser_credential,
)
from karox.browser_credentials import BrowserCredentialStore


class _MemoryBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.values[(service, account)] = secret

    def get(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def delete(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


class _FakeLocator:
    def __init__(self, *, mask_result: bool = True, fill_error: Exception | None = None) -> None:
        self.mask_result = mask_result
        self.fill_error = fill_error
        self.events: list[tuple[str, object]] = []
        self.filled_value: str | None = None

    def evaluate(self, expression: str) -> bool:
        self.events.append(("evaluate", expression))
        return self.mask_result

    def fill(self, value: str, *, timeout: int) -> None:
        self.events.append(("fill", timeout))
        self.filled_value = value
        if self.fill_error is not None:
            raise self.fill_error


def _store() -> tuple[BrowserCredentialStore, str]:
    store = BrowserCredentialStore(_MemoryBackend())
    result = store.set(
        "test-account",
        username="browser-user@example.invalid",
        password="local-only-passphrase",
    )
    return store, result["reference"]


def test_username_is_resolved_locally_masked_before_fill_and_not_returned() -> None:
    store, reference = _store()
    locator = _FakeLocator()

    result = inject_browser_credential(
        locator=locator,
        credential_store=store,
        reference=reference,
        field="username",
        timeout_ms=5000,
    ).to_dict()

    assert locator.filled_value == "browser-user@example.invalid"
    assert [event[0] for event in locator.events] == ["evaluate", "fill"]
    assert result == {
        "filled": True,
        "field": "username",
        "reference": reference,
        "masked": True,
    }
    assert "browser-user@example.invalid" not in repr(result)


def test_password_is_never_returned_and_mask_script_marks_secret_field() -> None:
    store, reference = _store()
    locator = _FakeLocator()

    result = inject_browser_credential(
        locator=locator,
        credential_store=store,
        reference=reference,
        field="password",
        timeout_ms=1000,
    ).to_dict()

    assert locator.filled_value == "local-only-passphrase"
    mask_script = str(locator.events[0][1])
    assert "data-karox-secret" in mask_script
    assert "-webkit-text-security" in mask_script
    assert "local-only-passphrase" not in mask_script
    assert "local-only-passphrase" not in repr(result)


def test_masking_happens_before_secret_fill() -> None:
    store, reference = _store()
    locator = _FakeLocator()
    inject_browser_credential(
        locator=locator,
        credential_store=store,
        reference=reference,
        field="password",
        timeout_ms=1000,
    )
    assert [event[0] for event in locator.events] == ["evaluate", "fill"]


def test_browser_driver_exception_cannot_leak_secret_through_exception_chain() -> None:
    store, reference = _store()
    locator = _FakeLocator(fill_error=RuntimeError("driver echoed local-only-passphrase"))

    with pytest.raises(BrowserCredentialInjectionError) as caught:
        inject_browser_credential(
            locator=locator,
            credential_store=store,
            reference=reference,
            field="password",
            timeout_ms=1000,
        )

    assert "local-only-passphrase" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is not None
    assert caught.value.__suppress_context__ is True


def test_mask_failure_stops_before_fill() -> None:
    store, reference = _store()
    locator = _FakeLocator(mask_result=False)

    with pytest.raises(BrowserCredentialInjectionError, match="could not be masked"):
        inject_browser_credential(
            locator=locator,
            credential_store=store,
            reference=reference,
            field="password",
            timeout_ms=1000,
        )
    assert locator.filled_value is None


@pytest.mark.parametrize("timeout_ms", [0, -1, True, 1.5])
def test_invalid_timeout_is_rejected_before_secret_access(timeout_ms: object) -> None:
    store, reference = _store()
    with pytest.raises(ValueError, match="positive integer"):
        inject_browser_credential(
            locator=_FakeLocator(),
            credential_store=store,
            reference=reference,
            field="username",
            timeout_ms=timeout_ms,  # type: ignore[arg-type]
        )


def test_missing_reference_returns_only_safe_error() -> None:
    store = BrowserCredentialStore(_MemoryBackend())
    with pytest.raises(BrowserCredentialInjectionError) as caught:
        inject_browser_credential(
            locator=_FakeLocator(),
            credential_store=store,
            reference="os-keyring:browser/missing-account",
            field="password",
            timeout_ms=1000,
        )
    assert "secure local storage" in str(caught.value)
