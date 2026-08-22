from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from karox.artifacts import ArtifactStore
from karox.browser_access import BrowserAccessPolicy, SecureBrowserSessionManager
from karox.browser_credential_injection import BrowserCredentialInjectionError
from karox.browser_credentials import BrowserCredentialError, BrowserCredentialStore
from karox.browser_session import BrowserSecurityError
from karox.extension_browser import ChromeExtensionBrowserSessionManager
from karox.hosted_tools_runtime import BROWSER_FILL_CREDENTIAL, _HOSTED_EXTRA_TOOLS


class _MemoryBackend:
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


class _Locator:
    def __init__(self, metadata: dict[str, str]) -> None:
        self.first = self
        self.metadata = metadata
        self.events: list[str] = []
        self.filled_value: str | None = None

    def evaluate(self, script: str):  # noqa: ANN001
        if "text-security" in script:
            self.events.append("mask")
            return True
        return dict(self.metadata)

    def fill(self, value: str, *, timeout: int) -> None:
        assert timeout > 0
        self.events.append("fill")
        self.filled_value = value


class _Page:
    def __init__(self, locator: _Locator) -> None:
        self._locator = locator

    def locator(self, selector: str) -> _Locator:
        assert selector
        return self._locator


def _store() -> tuple[BrowserCredentialStore, str]:
    store = BrowserCredentialStore(_MemoryBackend())
    result = store.set(
        "test-google",
        username="robot@example.invalid",
        password="local-only-example-secret",
    )
    return store, result["reference"]


def test_playwright_fill_credential_masks_before_fill_and_returns_no_secret(monkeypatch) -> None:
    store, reference = _store()
    locator = _Locator(
        {
            "type": "password",
            "name": "password",
            "id": "password",
            "aria": "Password",
            "placeholder": "",
            "text": "",
            "context": "Sign in",
        }
    )
    session_id = "credential-playwright-test"
    manager = SecureBrowserSessionManager(
        ArtifactStore(session_id),
        BrowserAccessPolicy(session_id=session_id),
        credential_store=store,
    )
    monkeypatch.setattr(manager, "_assert_agent_input_allowed", lambda: None)
    monkeypatch.setattr(manager, "_active_page", lambda: _Page(locator))

    result = manager.fill_credential(
        {"selector": "#password", "reference": reference, "field": "password"},
        5,
    )

    assert locator.events == ["mask", "fill"]
    assert locator.filled_value == "local-only-example-secret"
    assert result == {
        "selector": "#password",
        "filled": True,
        "field": "password",
        "reference": reference,
        "masked": True,
    }
    assert "local-only-example-secret" not in repr(result)


def test_profile_credential_allowlist_blocks_unapproved_reference(monkeypatch) -> None:
    store, reference = _store()
    locator = _Locator(
        {
            "type": "password",
            "name": "password",
            "id": "password",
            "aria": "Password",
            "placeholder": "",
            "text": "",
            "context": "Sign in",
        }
    )
    session_id = "credential-allowlist-test"
    manager = SecureBrowserSessionManager(
        ArtifactStore(session_id),
        BrowserAccessPolicy(
            session_id=session_id,
            allowed_credential_refs=("os-keyring:browser/another-test-account",),
        ),
        credential_store=store,
    )
    monkeypatch.setattr(manager, "_assert_agent_input_allowed", lambda: None)
    monkeypatch.setattr(manager, "_active_page", lambda: _Page(locator))

    with pytest.raises(BrowserSecurityError, match="not approved"):
        manager.fill_credential(
            {"selector": "#password", "reference": reference, "field": "password"},
            5,
        )
    assert locator.events == []


def test_playwright_profile_approved_credential_can_fill_email_without_plain_email_allowlist(monkeypatch) -> None:
    store, reference = _store()
    locator = _Locator(
        {
            "type": "email",
            "name": "email",
            "id": "email",
            "aria": "Email",
            "placeholder": "",
            "text": "",
            "context": "Sign in",
        }
    )
    session_id = "credential-email-test"
    manager = SecureBrowserSessionManager(
        ArtifactStore(session_id),
        BrowserAccessPolicy(
            session_id=session_id,
            allowed_credential_refs=(reference,),
        ),
        credential_store=store,
    )
    monkeypatch.setattr(manager, "_assert_agent_input_allowed", lambda: None)
    monkeypatch.setattr(manager, "_active_page", lambda: _Page(locator))

    result = manager.fill_credential(
        {"selector": "#email", "reference": reference, "field": "username"},
        5,
    )
    assert locator.events == ["mask", "fill"]
    assert locator.filled_value == "robot@example.invalid"
    assert result["reference"] == reference
    assert "robot@example.invalid" not in repr(result)


def test_extension_fill_credential_keeps_secret_out_of_public_result() -> None:
    store, reference = _store()
    with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
        os.environ,
        {"KAROX_VNEXT_RUNTIME_DIR": str(Path(directory) / "runtime")},
    ):
        session_id = "credential-extension-test"
        manager = ChromeExtensionBrowserSessionManager(
            ArtifactStore(session_id),
            BrowserAccessPolicy(
                session_id=session_id,
                external_https=True,
                headed=True,
                user_takeover=True,
                backend="extension",
            ),
            credential_store=store,
        )
        calls: list[tuple[str, dict[str, object]]] = []

        def local_call(method: str, params: dict[str, object], deadline_seconds: float):
            assert deadline_seconds > 0
            calls.append((method, dict(params)))
            if method == "inspect":
                return {
                    "type": "password",
                    "name": "password",
                    "id": "password",
                    "aria": "Password",
                    "placeholder": "",
                    "secret": True,
                }
            if method == "fill_secret":
                return {"filled": True, "secret": True}
            raise AssertionError(method)

        manager._call = local_call  # type: ignore[method-assign]
        manager._assert_agent_input_allowed = lambda: None  # type: ignore[method-assign]
        result = manager.fill_credential(
            {"selector": "#password", "reference": reference, "field": "password"},
            5,
        )
        manager.close(force=True)

    assert calls[-1] == (
        "fill_secret",
        {"selector": "#password", "value": "local-only-example-secret"},
    )
    assert "local-only-example-secret" not in repr(result)
    assert "value" not in result


def test_extension_secret_echo_error_is_replaced_with_generic_error() -> None:
    store, reference = _store()
    with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
        os.environ,
        {"KAROX_VNEXT_RUNTIME_DIR": str(Path(directory) / "runtime")},
    ):
        session_id = "credential-extension-error-test"
        manager = ChromeExtensionBrowserSessionManager(
            ArtifactStore(session_id),
            BrowserAccessPolicy(
                session_id=session_id,
                external_https=True,
                headed=True,
                user_takeover=True,
                backend="extension",
            ),
            credential_store=store,
        )

        def local_call(method: str, params: dict[str, object], deadline_seconds: float):
            if method == "inspect":
                return {"type": "password", "name": "password", "id": "password"}
            raise RuntimeError(f"driver echoed {params.get('value')}")

        manager._call = local_call  # type: ignore[method-assign]
        manager._assert_agent_input_allowed = lambda: None  # type: ignore[method-assign]
        with pytest.raises(BrowserCredentialInjectionError) as raised:
            manager.fill_credential(
                {"selector": "#password", "reference": reference, "field": "password"},
                5,
            )
        manager.close(force=True)

    assert "local-only-example-secret" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_hosted_credential_tool_schema_can_only_name_an_opaque_reference() -> None:
    metadata = _HOSTED_EXTRA_TOOLS[BROWSER_FILL_CREDENTIAL]
    properties = metadata.input_schema["properties"]
    assert set(properties) == {"selector", "reference", "field"}
    assert "value" not in properties
    assert metadata.capability is not None
    assert metadata.read_only is False


def test_extension_worker_secret_fill_contract_is_non_observing() -> None:
    extension = Path(__file__).resolve().parents[1] / "src" / "karox" / "browser_extension"
    worker = (extension / "service_worker.js").read_text(encoding="utf-8-sig")
    helpers = (extension / "dom_helpers.js").read_text(encoding="utf-8-sig")

    # The worker advertises/dispatches the opaque secret-fill action, while the
    # exact DOM engine it injects owns the non-observing value handling.
    assert '"fill", "fill_secret"' in worker
    assert 'action === "fill" || action === "fill_secret"' in helpers
    assert 'el.setAttribute("data-karox-secret", "true")' in helpers
    assert 'filled: true, secret: true' in helpers
    assert 'if (sensitive) return { action: "text", result: "success", text: "", secret: true };' in helpers
    assert 'el.dataset.karoxSecret === "true"' in helpers
