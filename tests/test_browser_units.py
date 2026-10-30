"""Browser package unit contracts: errors, redaction, network policy,
credentials, and the engine-neutral tool adapter.

These tests never start a browser; the live behavior suite lives in
test_browser_live.py. What is pinned here is the typed surface the integrator
registers with karo-core: the stable ``browser.command`` action vocabulary,
its capability and risk mapping, structured failures, and cancellation.
"""

from __future__ import annotations

import threading
import time

import pytest

from karox.core import InvalidCommand
from karox.models import Capability
from karox.risk_engine import HIGH_KINDS, LOW_KINDS, MEDIUM_KINDS
from karox.risk_mapping import BROWSER_ACTION_RISK_KINDS, risk_kind_for


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

def test_error_taxonomy_is_typed_and_fails_soft():
    from karox.browser.errors import (
        BROWSER_ERROR_KINDS,
        BrowserCancelled,
        BrowserError,
        BrowserSecurityError,
        BrowserSessionError,
        classify_browser_error,
        prefix_browser_error,
    )

    assert issubclass(BrowserSecurityError, BrowserError)
    assert issubclass(BrowserSessionError, BrowserError)
    assert issubclass(BrowserCancelled, BrowserError)
    assert "cancelled" in BROWSER_ERROR_KINDS
    assert classify_browser_error("Element not found for selector x") == "element_not_found"
    assert classify_browser_error("Navigation timed out after 30s") == "navigation_timeout"
    assert classify_browser_error("something nobody classified") == "browser_error"
    # An engine that already speaks the taxonomy is believed verbatim.
    assert classify_browser_error("user_takeover_required: ask the person") == (
        "user_takeover_required"
    )
    assert prefix_browser_error("element_not_found: gone") == "element_not_found: gone"
    assert prefix_browser_error("weird boom").startswith("browser_error: ")


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------

def test_safe_network_url_strips_query_values_and_token_segments():
    from karox.browser.redaction import safe_network_url

    safe = safe_network_url(
        "https://shop.example/cart/abcdef0123456789abcdef0123456789?token=secret&size=3"
    )
    assert "secret" not in safe
    assert "[REDACTED]" in safe
    assert "size=3" not in safe.replace("size=[REDACTED]", "")
    assert safe.startswith("https://shop.example/cart/")
    # A plain URL survives unchanged apart from fragment removal.
    assert safe_network_url("http://127.0.0.1:8080/page#frag") == "http://127.0.0.1:8080/page"


def test_safe_console_text_redacts_blobs_and_clamps():
    from karox.browser.redaction import safe_console_text

    text = safe_console_text("key=sk_live_abcdefgh12345678 " + "A" * 5000)
    assert len(text) <= 1200
    assert "sk_live_abcdefgh12345678" not in text
    blobbed = safe_console_text("data:image/png;base64," + "B" * 200)
    assert "data:[REDACTED]" in blobbed


def test_allowed_json_values_never_leak_sensitive_fields():
    from karox.browser.redaction import allowed_json_values, safe_field_names

    payload = {
        "model": "gpt-x",
        "usage": {"input_tokens": 3, "output_tokens": 4},
        "authorization": "Bearer abc",
        "cookie": "session=xyz",
        "password": "hunter2",
    }
    values = allowed_json_values(payload)
    assert values.get("model") == "gpt-x"
    assert "authorization" not in values
    assert "cookie" not in values
    assert "password" not in values
    names = safe_field_names(payload)
    assert "model" in names
    assert not any("authorization" in name or "password" in name for name in names)


# ---------------------------------------------------------------------------
# network policy
# ---------------------------------------------------------------------------

def _policy(**overrides):
    from karox.browser.netpolicy import BrowserAccessPolicy

    base = {"session_id": "s-1"}
    base.update(overrides)
    return BrowserAccessPolicy(**base)


def test_policy_validation_is_strict():
    with pytest.raises(ValueError):
        _policy(session_id="  ")
    with pytest.raises(ValueError):
        _policy(allowed_domains=["example.com"])  # requires external_https
    with pytest.raises(ValueError):
        _policy(external_https=True, allowed_domains=["a.test"], denied_domains=["a.test"])
    with pytest.raises(ValueError):
        _policy(user_takeover=True, headed=False)
    with pytest.raises(ValueError):
        _policy(allowed_domains=["bad domain/slash"], external_https=True)
    policy = _policy(external_https=True, allowed_domains=["*.Example.COM"])
    assert policy.allowed_domains == ("example.com",)


def test_validate_browser_url_blocks_unsafe_shapes():
    from karox.browser.errors import BrowserSecurityError
    from karox.browser.netpolicy import validate_browser_url

    policy = _policy()
    for bad in (
        "file:///C:/secrets.txt",
        "javascript:alert(1)",
        "data:text/html,<script>",
        "https://user:pass@example.com/",
        "http://169.254.169.254/latest/meta-data/",
        "ftp://example.com/x",
        "",
        "not a url",
    ):
        with pytest.raises(BrowserSecurityError):
            validate_browser_url(bad, policy)


def test_validate_browser_url_localhost_and_external_rules(monkeypatch):
    import ipaddress

    from karox.browser import netpolicy
    from karox.browser.errors import BrowserSecurityError
    from karox.browser.netpolicy import validate_browser_url

    # Deterministic regardless of the machine's DNS: every external host in
    # this test resolves to one public address.
    monkeypatch.setattr(
        netpolicy,
        "_resolved_addresses",
        lambda host, port: (
            (ipaddress.ip_address("127.0.0.1"),)
            if host in {"localhost", "127.0.0.1"}
            else (ipaddress.ip_address("93.184.216.34"),)
        ),
    )

    local = _policy()
    assert validate_browser_url("http://127.0.0.1:8080/x", local) == "http://127.0.0.1:8080/x"
    assert validate_browser_url("http://localhost:3000/", local).startswith("http://localhost")
    with pytest.raises(BrowserSecurityError):
        validate_browser_url("https://example.com/", local)  # external not enabled

    strict = _policy(localhost=False, external_https=True, allowed_domains=["example.com"])
    with pytest.raises(BrowserSecurityError):
        validate_browser_url("http://127.0.0.1:8080/", strict)
    assert validate_browser_url("https://example.com/a", strict) == "https://example.com/a"
    assert validate_browser_url("https://sub.example.com/a", strict).startswith("https://sub.")
    with pytest.raises(BrowserSecurityError):
        validate_browser_url("https://other.com/", strict)
    with pytest.raises(BrowserSecurityError):
        validate_browser_url("http://example.com/a", strict)  # external requires https


def test_validate_browser_url_blocks_private_addresses_for_external(monkeypatch):
    import ipaddress

    from karox.browser import netpolicy
    from karox.browser.errors import BrowserSecurityError
    from karox.browser.netpolicy import validate_browser_url

    def resolve(host: str, port: int):
        if host == "10.0.0.5":
            return (ipaddress.ip_address("10.0.0.5"),)
        if host == "192.168.1.1":
            return (ipaddress.ip_address("192.168.1.1"),)
        if host == "rebind.example":
            # A public-looking name whose DNS now points into the LAN.
            return (ipaddress.ip_address("192.168.0.10"),)
        return (ipaddress.ip_address("93.184.216.34"),)

    monkeypatch.setattr(netpolicy, "_resolved_addresses", resolve)

    policy = _policy(external_https=True)
    # A literal private address can never pass the external check.
    with pytest.raises(BrowserSecurityError):
        validate_browser_url("https://10.0.0.5/x", policy)
    with pytest.raises(BrowserSecurityError):
        validate_browser_url("https://192.168.1.1/x", policy)
    with pytest.raises(BrowserSecurityError):
        validate_browser_url("https://rebind.example/x", policy)


def test_pinned_proxy_rejects_unauthenticated_and_private_targets():
    import socket

    from karox.browser.netpolicy import SessionPinnedProxy

    proxy = SessionPinnedProxy(_policy())
    proxy.start()
    try:
        host, port = proxy.server_url.removeprefix("http://").split(":")
        with socket.create_connection((host, int(port)), timeout=5) as sock:
            sock.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n")
            response = sock.recv(4096)
        assert b"407" in response
    finally:
        proxy.stop()


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------

class _MemoryBackend:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.data[(service, account)] = secret

    def get(self, service: str, account: str):
        return self.data.get((service, account))

    def delete(self, service: str, account: str) -> None:
        self.data.pop((service, account), None)


def test_credential_store_roundtrip_never_exposes_values_publicly():
    from karox.browser.credentials import BrowserCredentialReference, BrowserCredentialStore

    backend = _MemoryBackend()
    store = BrowserCredentialStore(backend=backend)
    meta = store.set("shop-test", username="qa@example.com", password="s3cret-value")
    assert meta["reference"] == "os-keyring:browser/shop-test"
    assert "s3cret-value" not in str(meta)
    assert meta["fingerprint"].startswith("sha256:")
    assert store.resolve_field("os-keyring:browser/shop-test", "password") == "s3cret-value"
    assert store.resolve_field(BrowserCredentialReference("shop-test"), "username") == (
        "qa@example.com"
    )
    with pytest.raises(Exception):
        store.resolve_field("os-keyring:browser/missing", "password")
    with pytest.raises(ValueError):
        store.set("bad name!", username="a", password="b")
    store.delete("shop-test")
    assert backend.data == {}


def test_credential_injection_masks_and_never_leaks_value():
    from karox.browser.credentials import (
        BrowserCredentialInjectionError,
        BrowserCredentialStore,
        inject_browser_credential,
    )

    class FakeLocator:
        def __init__(self) -> None:
            self.masked = False
            self.filled = None

        def evaluate(self, expression: str):
            self.masked = True
            return True

        def fill(self, value: str, *, timeout: int) -> None:
            self.filled = value

    class ExplodingLocator(FakeLocator):
        def fill(self, value: str, *, timeout: int) -> None:
            raise RuntimeError(f"driver leaked {value} into an exception")

    store = BrowserCredentialStore(backend=_MemoryBackend())
    store.set("t", username="u@example.com", password="pw-secret-123")
    locator = FakeLocator()
    result = inject_browser_credential(
        locator=locator,
        credential_store=store,
        reference="os-keyring:browser/t",
        field="password",
        timeout_ms=5000,
    )
    assert result.filled and result.masked
    assert locator.filled == "pw-secret-123"
    assert "pw-secret-123" not in str(result.to_dict())

    # A driver exception must not carry the credential out through the chain.
    with pytest.raises(BrowserCredentialInjectionError) as excinfo:
        inject_browser_credential(
            locator=ExplodingLocator(),
            credential_store=store,
            reference="os-keyring:browser/t",
            field="password",
            timeout_ms=5000,
        )
    assert "pw-secret-123" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


# ---------------------------------------------------------------------------
# tool adapter: catalog, capabilities, risk alignment
# ---------------------------------------------------------------------------

def test_action_vocabulary_matches_the_risk_mapping():
    from karox.browser.tools import BROWSER_ACTIONS

    # Every action the risk vocabulary already knows must be implemented, so
    # the race branch cannot regress the hosted allowlist surface.
    known = set(BROWSER_ACTION_RISK_KINDS) - {
        # Release-line actions owned elsewhere (hot release flow).
        "release.integrate_main_lineage",
        "release.check_prerelease",
        "release.publish_prerelease",
    }
    missing = known - set(BROWSER_ACTIONS)
    assert not missing, f"actions missing from the engine: {sorted(missing)}"


def test_read_actions_classify_low_and_input_actions_do_not():
    from karox.browser.tools import ACTION_CAPABILITIES, READ_ACTIONS

    for action, capability in ACTION_CAPABILITIES.items():
        kind = risk_kind_for("browser.command", {"action": action, "payload": {}})
        assert kind in LOW_KINDS | MEDIUM_KINDS | HIGH_KINDS, f"unknown kind {kind}"
        if action in READ_ACTIONS:
            assert capability == Capability.BROWSER_READ
            # A read action that the shared vocabulary grades above low is a
            # misalignment the integrator must see now, not in production.
            assert kind in LOW_KINDS or kind == "browser.input", (
                f"{action} reads but maps to {kind}"
            )
        else:
            assert capability == Capability.BROWSER_INPUT
            assert kind not in LOW_KINDS, f"input action {action} maps to low kind {kind}"


def test_tool_definition_is_the_stable_browser_command_surface():
    from karox.browser.tools import browser_tool_definition

    definition = browser_tool_definition()
    assert definition.name == "browser.command"
    assert definition.capability == Capability.BROWSER_READ
    # Browser side effects are not repository mutations: the per-action risk
    # gate (risk_kind_for) grades every call, and a repo mutation lease cannot
    # roll a click back. Integrators that want durable idempotency for input
    # actions register the mutates=True variant instead.
    assert definition.mutates is False
    assert definition.replayable is False
    assert browser_tool_definition(mutates=True).mutates is True
    schema = definition.input_schema
    assert schema["required"] == ["action"]
    assert set(schema["properties"]) >= {"action", "payload"}


class _RecordingSession:
    """Duck-typed stand-in proving the adapter contract without a browser."""

    is_open = True
    takeover_active = False
    context_id = "ctx-test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, float]] = []

    def dispatch(self, action: str, payload: dict, deadline_seconds: float, cancel=None):
        self.calls.append((action, dict(payload), deadline_seconds))
        if action == "wait_for":
            if cancel is not None and cancel.wait(0.05):
                from karox.browser.errors import BrowserCancelled

                raise BrowserCancelled("browser call cancelled")
            time.sleep(0.01)
        return {"echo": action}


def _adapter(session=None, guard=None, serve_root=None):
    from karox.browser.tools import BrowserToolAdapter

    return BrowserToolAdapter(
        session or _RecordingSession(),
        capability_guard=guard,
        serve_root=serve_root,
    )


def test_adapter_returns_structured_results_and_validates_shape():
    adapter = _adapter()
    result = adapter.execute({"action": "tabs", "payload": {}}, 30.0)
    assert result["ok"] is True
    assert result["action"] == "tabs"
    assert result["echo"] == "tabs"

    with pytest.raises(InvalidCommand):
        adapter.execute({"action": "", "payload": {}}, 30.0)
    with pytest.raises(InvalidCommand):
        adapter.execute({"action": "tabs", "payload": "not-an-object"}, 30.0)
    with pytest.raises(InvalidCommand):
        adapter.execute({"action": "not_a_real_action", "payload": {}}, 30.0)


def test_adapter_enforces_capability_per_action():
    denied = Capability.BROWSER_INPUT

    def guard(capability: Capability) -> None:
        if capability is denied:
            from karox.policy import PolicyDenied

            raise PolicyDenied("browser.input is not granted to this origin")

    adapter = _adapter(guard=guard)
    # Reads still work under a read-only guard.
    assert adapter.execute({"action": "snapshot", "payload": {}}, 30.0)["ok"] is True
    result = adapter.execute({"action": "click", "payload": {"selector": "#x"}}, 30.0)
    assert result["ok"] is False
    assert result["error_kind"] == "permission_denied"


def test_adapter_reports_security_failures_as_typed_structures():
    from karox.browser.errors import BrowserSecurityError

    class DenyingSession(_RecordingSession):
        def dispatch(self, action, payload, deadline_seconds, cancel=None):
            raise BrowserSecurityError("payment or subscription action requires permission")

    result = _adapter(DenyingSession()).execute({"action": "click", "payload": {}}, 30.0)
    assert result["ok"] is False
    assert result["error_kind"] == "permission_denied"
    assert "payment" in result["error"]


def test_adapter_cancellation_is_structured():
    from karox.browser.session import CancelToken

    token = CancelToken()
    adapter = _adapter()

    def cancel_soon() -> None:
        time.sleep(0.02)
        token.cancel()

    threading.Thread(target=cancel_soon, daemon=True).start()
    result = adapter.execute({"action": "wait_for", "payload": {}}, 30.0, cancel=token)
    assert result["ok"] is False
    assert result["error_kind"] == "cancelled"


def test_cancel_token_primitives():
    from karox.browser.errors import BrowserCancelled
    from karox.browser.session import CancelToken

    token = CancelToken()
    assert token.cancelled is False
    token.raise_if_cancelled()
    token.cancel()
    assert token.cancelled is True
    with pytest.raises(BrowserCancelled):
        token.raise_if_cancelled()
    assert token.wait(0.01) is True


def test_dropin_execute_browser_command_matches_worker_contract():
    from karox.browser.tools import execute_browser_command

    session = _RecordingSession()

    class Runtime:
        _browser = session

    result = execute_browser_command(Runtime(), {"action": "tabs", "payload": {}}, 30.0)
    assert result["ok"] is True and result["action"] == "tabs"

    class NoBrowserRuntime:
        _browser = None

    with pytest.raises(InvalidCommand):
        execute_browser_command(NoBrowserRuntime(), {"action": "tabs", "payload": {}}, 30.0)


def test_open_page_serves_the_repository_on_loopback(tmp_path) -> None:
    """A page the run wrote must be clickable, not only screenshot-able.

    The canvas/3D runs tried ``browser.command open`` with a ``file://`` URL and
    were refused: file is not localhost, so the call demanded NETWORK. That left
    restart and hotspot clicks unverified. ``open {page}`` now starts a loopback
    server rooted at the repository and navigates to the page through it.
    """

    import urllib.request

    (tmp_path / "index.html").write_text(
        "<html><body><button id='go'>served</button></body></html>",
        encoding="utf-8",
    )
    session = _RecordingSession()
    adapter = _adapter(session=session, serve_root=tmp_path)
    try:
        result = adapter.execute(
            {"action": "open", "payload": {"page": "index.html"}}, 30.0
        )

        assert result["ok"] is True
        action, payload, _ = session.calls[-1]
        assert action == "open"
        url = payload["url"]
        assert url.startswith("http://127.0.0.1:")
        with urllib.request.urlopen(url, timeout=5) as response:
            assert b"served" in response.read()
    finally:
        adapter.stop_server()


def test_serve_returns_the_loopback_url_without_navigating(tmp_path) -> None:
    (tmp_path / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    session = _RecordingSession()
    adapter = _adapter(session=session, serve_root=tmp_path)
    try:
        result = adapter.execute(
            {"action": "serve", "payload": {"path": "index.html"}}, 30.0
        )
    finally:
        adapter.stop_server()

    assert result["ok"] is True
    assert result["url"].startswith("http://127.0.0.1:")
    assert result["url"].endswith("/index.html")
    assert not session.calls


def test_open_page_refuses_a_path_outside_the_repository(tmp_path) -> None:
    (tmp_path / "inside.html").write_text("<html>ok</html>", encoding="utf-8")
    (tmp_path.parent / "outside.html").write_text("<html>no</html>", encoding="utf-8")
    adapter = _adapter(serve_root=tmp_path)
    try:
        with pytest.raises(InvalidCommand, match="inside the repository"):
            adapter.execute(
                {"action": "open", "payload": {"page": "../outside.html"}}, 30.0
            )
    finally:
        adapter.stop_server()


def test_serve_is_a_local_read_for_capability_and_risk(tmp_path) -> None:
    seen = []
    adapter = _adapter(guard=lambda capability: seen.append(capability), serve_root=tmp_path)
    try:
        adapter.execute({"action": "serve", "payload": {}}, 30.0)
    finally:
        adapter.stop_server()

    assert seen == [Capability.BROWSER_READ, Capability.REPO_READ]
    assert risk_kind_for("browser.command", {"action": "serve"}) == "browser.read"


def test_secret_fields_have_one_definition_for_python_and_both_probes() -> None:
    """The page probes and the action guard must classify the same fields secret.

    They had drifted: the Python guard knew `passwd` and `iban`, the page
    probes did not, so a bank-account or password field could come back with a
    plaintext length. One keyword list now feeds all three.
    """
    from karox.browser import probes

    for name in ("iban", "passwd", "password", "cvv", "api_key", "token"):
        assert probes.SECRET_FIELD_HINT.search(f"user_{name}") is not None
    # Both probes embed the shared JS regex, not a copy of their own.
    assert probes._JS_SECRET_HINT in probes.FORM_STATE_PROBE
    assert probes._JS_SECRET_HINT in probes.snapshot_probe(10)
    assert "SECRET_HINT.test" in probes.FORM_STATE_PROBE
    assert "SECRET_HINT.test" in probes.snapshot_probe(10)
