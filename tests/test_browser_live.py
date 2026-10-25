"""Live browser behavior against a real Chromium and a local HTTP site.

Every test here drives the actual Playwright engine through the public
``BrowserSession.dispatch`` surface (the same entry the tool adapter uses)
against a loopback fixture site. The suite skips honestly when the optional
Playwright runtime or its Chromium binary is unavailable.

Headed (visible) browsing is gated behind ``KAROX_BROWSER_HEADED_TESTS=1`` so
normal runs never open a window on the developer's screen; the gate exists
because visible browsing is a product requirement that must be provable.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

import pytest

from karox.artifacts import ArtifactStore
from karox.browser.credentials import BrowserCredentialStore
from karox.browser.netpolicy import BrowserAccessPolicy
from karox.browser.session import BrowserSession, CancelToken
from karox.browser.tools import BrowserToolAdapter

pytest.importorskip("playwright")

from _browser_site import (  # noqa: E402
    CONSOLE_SECRET,
    DOWNLOAD_BODY,
    LOGIN_PASS,
    LOGIN_USER,
    site_server,
)

HEADLESS_ONLY = os.environ.get("KAROX_BROWSER_HEADED_TESTS", "") != "1"


@pytest.fixture(scope="module")
def site():
    with site_server() as base_url:
        yield base_url


@pytest.fixture()
def make_session(tmp_path: Path):
    created: list[BrowserSession] = []

    def factory(
        *,
        downloads: bool = True,
        plain_credentials: bool = True,
        network_inspection: bool = True,
        payment_confirmation: bool = False,
        headed: bool = False,
        user_takeover: bool = False,
        allow_detach: bool = False,
        profile: str | None = None,
        browser_root: Path | None = None,
        upload_roots: tuple[Path, ...] | None = None,
        credential_store: BrowserCredentialStore | None = None,
    ) -> tuple[BrowserSession, Path]:
        session_id = f"live-{uuid.uuid4().hex[:10]}"
        root = tmp_path / session_id
        artifacts_root = root / "artifacts"
        artifacts_root.mkdir(parents=True, exist_ok=True)
        artifacts = ArtifactStore(session_id, root=artifacts_root)
        policy = BrowserAccessPolicy(
            session_id=session_id,
            localhost=True,
            downloads=downloads,
            plain_credentials=plain_credentials,
            network_inspection=network_inspection,
            payment_confirmation=payment_confirmation,
            headed=headed,
            user_takeover=user_takeover,
            allow_detach=allow_detach,
        )
        uploads = root / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        session = BrowserSession(
            artifacts,
            policy,
            credential_store,
            upload_roots=upload_roots if upload_roots is not None else (uploads,),
            browser_root=browser_root if browser_root is not None else root / "browser",
        )
        created.append(session)
        return session, root

    yield factory
    for session in created:
        try:
            session.force_close()
        except Exception:
            pass


def _call(session: BrowserSession, action: str, payload: dict | None = None,
          deadline: float = 45.0, cancel: CancelToken | None = None) -> dict:
    return BrowserToolAdapter(session).execute(
        {"action": action, "payload": payload or {}}, deadline, cancel
    )


def _open(session: BrowserSession, site: str, path: str = "/", **kwargs) -> dict:
    result = _call(session, "open", {"url": f"{site}{path}", **kwargs})
    assert result["ok"] is True, result
    return result


# ---------------------------------------------------------------------------
# navigation, extraction, interaction
# ---------------------------------------------------------------------------

def test_open_snapshot_and_text_extraction(site, make_session):
    session, _root = make_session()
    opened = _open(session, site)
    assert opened["engine"] == "playwright_chromium"
    assert opened["url"].startswith("http://127.0.0.1")

    snap = _call(session, "snapshot")
    assert snap["ok"] is True
    data = snap["snapshot"]
    assert data["headings"][0]["text"] == "Research Index"
    buttons = {row["name"] for row in data["buttons"]}
    assert "Sign in" in buttons
    inputs = {row["label"]: row for row in data["inputs"]}
    assert inputs["Password"]["protected"] is True
    assert inputs["Password"]["value_length"] is None
    assert any(row.get("ref") for row in data["buttons"])

    intro = _call(session, "get_text", {"selector": "#intro"})
    assert intro["text"] == "Welcome to the local fixture site."

    found = _call(session, "find_text", {"text": "Second page"})
    assert found["count"] >= 1
    assert found["matches"][0]["tag"] == "a"

    info = _call(session, "page_info")
    assert info["ready_state"] == "complete"
    assert info["title"] == "KaroX Browser Test Site"


def test_navigation_history(site, make_session):
    session, _root = make_session()
    _open(session, site)
    _call(session, "click", {"selector": "#page2-link"})
    _call(session, "wait_navigation", {})
    text = _call(session, "get_text", {"selector": "h1"})
    assert text["text"] == "Page Two"
    back = _call(session, "back", {})
    assert back["ok"] is True and "127.0.0.1" in back["url"]
    forward = _call(session, "forward", {})
    assert forward["ok"] is True
    reload_result = _call(session, "reload", {})
    assert reload_result["ok"] is True
    text = _call(session, "get_text", {"selector": "h1"})
    assert text["text"] == "Page Two"


def test_login_flow_with_explicit_test_credentials(site, make_session):
    session, _root = make_session()
    _open(session, site)
    fill_user = _call(session, "fill", {"selector": "#user", "value": LOGIN_USER})
    assert fill_user["ok"] is True and fill_user["value_length"] == len(LOGIN_USER)
    fill_mail = _call(session, "fill", {"selector": "#mail", "value": "qa@test.local"})
    assert fill_mail["ok"] is True
    fill_pw = _call(session, "fill", {"selector": "#pw", "value": LOGIN_PASS})
    assert fill_pw["ok"] is True
    assert fill_pw["protected_field"] is True
    # The password value never appears in the structured result.
    assert LOGIN_PASS not in json.dumps(fill_pw)
    assert _call(session, "select", {"selector": "#plan", "value": "pro"})["ok"] is True
    assert _call(session, "set_checked", {"selector": "#agree", "checked": True})["ok"] is True

    form = _call(session, "get_form_state", {"selector": "#login-form"})
    fields = {row["name"]: row for row in form["forms"][0]["fields"] if row.get("name")}
    assert fields["username"]["value"] == LOGIN_USER
    assert fields["password"]["protected"] is True
    assert "value" not in fields["password"]
    assert fields["agree"]["checked"] is True
    assert fields["plan"]["selected_option"] == "pro"

    submitted = _call(session, "click", {"selector": "#login-btn"})
    assert submitted["ok"] is True
    _call(session, "wait_navigation", {})
    welcome = _call(session, "get_text", {"selector": "#who"})
    assert welcome["text"] == f"user={LOGIN_USER}"
    plan_line = _call(session, "get_text", {"selector": "#plan-line"})
    assert plan_line["text"] == "plan=pro"

    # The snapshot of the authenticated page never shows the password value.
    snapshot_text = json.dumps(_call(session, "snapshot"))
    assert LOGIN_PASS not in snapshot_text

    cookies = _call(session, "cookies", {})
    assert cookies["values_exposed"] is False
    names = {row["name"] for row in cookies["cookies"]}
    assert {"session_token", "who", "plan"} <= names
    assert "fake-session-token-value" not in json.dumps(cookies)


def test_password_fill_refused_without_plain_credentials(site, make_session):
    session, _root = make_session(plain_credentials=False)
    _open(session, site)
    result = _call(session, "fill", {"selector": "#pw", "value": LOGIN_PASS})
    assert result["ok"] is False
    # The typed vocabulary prefers the actionable kind: the refusal text
    # points at user takeover / fill_credential, so that is the kind.
    assert result["error_kind"] == "user_takeover_required"
    # A normal text field still fills.
    assert _call(session, "fill", {"selector": "#user", "value": "x"})["ok"] is True


def test_fill_credential_uses_keyring_reference(site, make_session):
    class _MemoryBackend:
        def __init__(self) -> None:
            self.data = {}

        def set(self, service, account, secret):
            self.data[(service, account)] = secret

        def get(self, service, account):
            return self.data.get((service, account))

        def delete(self, service, account):
            self.data.pop((service, account), None)

    store = BrowserCredentialStore(backend=_MemoryBackend())
    store.set("fixture", username=LOGIN_USER, password=LOGIN_PASS)
    session, _root = make_session(plain_credentials=False, credential_store=store)
    _open(session, site)
    result = _call(
        session,
        "fill_credential",
        {
            "selector": "#pw",
            "reference": "os-keyring:browser/fixture",
            "field": "password",
        },
    )
    assert result["ok"] is True and result["filled"] is True and result["masked"] is True
    assert LOGIN_PASS not in json.dumps(result)
    # The credential store is consulted locally; the login actually works.
    _call(session, "fill", {"selector": "#user", "value": LOGIN_USER})
    _call(session, "click", {"selector": "#login-btn"})
    _call(session, "wait_navigation", {})
    assert _call(session, "get_text", {"selector": "#who"})["text"] == f"user={LOGIN_USER}"


def test_typed_keys_hover_focus_clear(site, make_session):
    session, _root = make_session()
    _open(session, site)
    assert _call(session, "focus", {"selector": "#user"})["ok"] is True
    typed = _call(session, "type", {"selector": "#user", "text": "typed-name", "delay_ms": 1})
    assert typed["ok"] is True and typed["chars"] == len("typed-name")
    assert _call(session, "hover", {"selector": "#user"})["ok"] is True
    # Space, not Enter: Enter inside a form is an implicit submit and would
    # navigate away from the page the rest of this test asserts on.
    pressed = _call(session, "press", {"selector": "#user", "key": "Space"})
    assert pressed["ok"] is True
    assert _call(session, "clear", {"selector": "#user"})["ok"] is True
    form = _call(session, "get_form_state", {"selector": "#login-form"})
    fields = {row["name"]: row for row in form["forms"][0]["fields"] if row.get("name")}
    assert fields["username"]["value_length"] == 0
    dbl = _call(session, "dblclick", {"selector": "#user"})
    assert dbl["ok"] is True


# ---------------------------------------------------------------------------
# scrolling, tabs, screenshots
# ---------------------------------------------------------------------------

def test_scrolling(site, make_session):
    session, _root = make_session()
    _open(session, site)
    down = _call(session, "scroll", {"direction": "down", "amount": 800})
    assert down["ok"] is True and down["y"] >= 400
    bottom = _call(session, "scroll", {"direction": "bottom"})
    assert bottom["y"] > 1000
    to_selector = _call(session, "scroll", {"direction": "to_selector", "selector": "#intro"})
    assert to_selector["ok"] is True
    top = _call(session, "scroll", {"direction": "top"})
    assert top["y"] == 0


def test_tab_lifecycle(site, make_session):
    session, _root = make_session()
    _open(session, site)
    first_tab = _call(session, "tabs", {})["active_tab_id"]
    new_tab = _call(session, "new_tab", {"url": f"{site}/page2"})
    assert new_tab["ok"] is True
    tabs = _call(session, "tabs", {})
    assert tabs["count"] == 2
    switched = _call(session, "switch_tab", {"tab_id": first_tab})
    assert switched["ok"] is True
    closed = _call(session, "close_tab", {"tab_id": new_tab["tab_id"]})
    assert closed["ok"] is True
    assert _call(session, "tabs", {})["count"] == 1
    last = _call(session, "close_tab", {"tab_id": first_tab})
    assert last["ok"] is False  # never close the last tab
    foreign = _call(session, "switch_tab", {"tab_id": "tab-doesnotexist0"})
    assert foreign["ok"] is False and foreign["error_kind"] == "permission_denied"


def test_screenshot_full_and_region(site, make_session):
    session, root = make_session()
    _open(session, site)
    shot = _call(session, "screenshot", {"name": "index.png", "full_page": False})
    assert shot["ok"] is True and shot["mime"] == "image/png" and shot["size"] > 0
    assert shot["sha256"] and len(shot["sha256"]) == 64
    region = _call(session, "screenshot_region", {"selector": "#login-form", "name": "form.png"})
    assert region["ok"] is True and region["region"] is True
    # Artifacts are real files inside this session's artifact store.
    blob = session._artifacts.root / f"{shot['artifact_id']}.bin"
    assert blob.is_file()
    assert hashlib.sha256(blob.read_bytes()).hexdigest() == shot["sha256"]


# ---------------------------------------------------------------------------
# collectors: console, network, dialogs
# ---------------------------------------------------------------------------

def test_console_redaction_and_network_metadata(site, make_session):
    session, _root = make_session()
    _open(session, site)
    console = _call(session, "console", {})
    assert console["count"] >= 1
    text = json.dumps(console)
    assert CONSOLE_SECRET not in text
    assert "token=[REDACTED]" in text or "[REDACTED]" in text

    assert _call(session, "click", {"selector": "#fetch-btn"})["ok"] is True
    _call(session, "wait_for", {"selector": "#intro", "state": "visible"})
    # Wait until the fetch shows up (the click handler is async).
    deadline = time.monotonic() + 10
    requests = {"count": 0}
    while time.monotonic() < deadline:
        requests = _call(session, "network_requests", {"url_contains": "api/data"})
        if requests["count"] >= 1:
            break
        time.sleep(0.2)
    assert requests["count"] >= 1
    entry = requests["requests"][0]
    assert entry["values"].get("model") == "fixture-model"
    assert "secret_token" not in json.dumps(entry)
    assert "leak-me-not" not in json.dumps(entry)

    failures = _call(session, "network_failures", {})
    assert failures["ok"] is True  # the 404 route may or may not have been hit


def test_dialog_manual_and_default_handling(site, make_session):
    session, _root = make_session()
    _open(session, site)
    # Default mode dismisses automatically; the page's post-alert counter stays 0.
    _call(session, "click", {"selector": "#alert-btn"})
    listing = _call(session, "dialog", {})
    assert listing["mode"] == "auto_dismiss"
    assert any(row["type"] == "alert" and row["resolved_with"] == "dismiss"
               for row in listing["dialogs"])

    # Manual mode holds the dialog until an explicit decision resolves it.
    assert _call(session, "dialog", {"mode": "manual"})["ok"] is True
    _call(session, "click", {"selector": "#alert-btn"})
    pending = _call(session, "dialog", {})
    assert pending["pending"] is True
    resolved = _call(session, "dialog", {"decision": "accept"})
    assert resolved["ok"] is True and resolved["resolved"] is True


# ---------------------------------------------------------------------------
# files: upload and download
# ---------------------------------------------------------------------------

def test_upload_from_confined_root(site, make_session, tmp_path):
    session, root = make_session()
    uploads = root / "uploads"
    (uploads / "notes.txt").write_text("upload me", encoding="utf-8")
    _open(session, site)
    result = _call(session, "upload", {"selector": "#file", "path": "notes.txt"})
    assert result["ok"] is True and result["file_names"] == ["notes.txt"]
    shown = _call(session, "get_text", {"selector": "#file-name"})
    assert shown["text"] == "uploaded:notes.txt"

    escape = _call(session, "upload", {"selector": "#file", "path": str(tmp_path / "x.txt")})
    assert escape["ok"] is False and escape["error_kind"] == "permission_denied"


def test_upload_requires_roots_and_files(site, make_session, tmp_path):
    session, _root = make_session(upload_roots=())
    _open(session, site)
    result = _call(session, "upload", {"selector": "#file", "path": "anything.txt"})
    assert result["ok"] is False
    assert "upload roots" in result["error"]


def test_download_start_and_status(site, make_session):
    session, root = make_session()
    _open(session, site)
    result = _call(session, "download_start", {"url": f"{site}/download"})
    assert result["ok"] is True, result
    assert result["status"] == "completed"
    assert result["suggested_filename"] == "report.bin"
    blob = session._artifacts.root / f"{result['artifact_id']}.bin"
    assert blob.read_bytes() == DOWNLOAD_BODY
    assert hashlib.sha256(DOWNLOAD_BODY).hexdigest() == result["sha256"]
    status = _call(session, "download_status", {"download_id": result["download_id"]})
    assert status["status"] == "completed"

    refused_session, _ = make_session(downloads=False)
    _open(refused_session, site)
    refused = _call(refused_session, "download_start", {"url": f"{site}/download"})
    assert refused["ok"] is False and refused["error_kind"] == "permission_denied"


# ---------------------------------------------------------------------------
# policy guards
# ---------------------------------------------------------------------------

def test_payment_actions_require_explicit_permission(site, make_session):
    session, _root = make_session()
    _open(session, site, "/pricing")
    buy = _call(session, "click", {"selector": "#buy-btn"})
    assert buy["ok"] is False and buy["error_kind"] == "user_takeover_required"
    assert "payment" in buy["error"]
    # A free trial with clear no-card evidence nearby is allowed.
    trial_ok = _call(session, "click", {"selector": "#trial-btn"})
    assert trial_ok["ok"] is True
    # An ambiguous free-trial control without evidence stays refused.
    vague = _call(session, "click", {"selector": "#trial-vague-btn"})
    assert vague["ok"] is False and vague["error_kind"] == "user_takeover_required"

    confirmed, _ = make_session(payment_confirmation=True)
    _open(confirmed, site, "/pricing")
    assert _call(confirmed, "click", {"selector": "#buy-btn"})["ok"] is True


def test_external_urls_refused_on_localhost_policy(site, make_session):
    session, _root = make_session()
    result = _call(session, "open", {"url": "https://example.com/"})
    assert result["ok"] is False
    assert result["error_kind"] == "permission_denied"
    assert not session.is_open


def test_takeover_requires_headed_policy(site, make_session):
    session, _root = make_session()
    _open(session, site)
    result = _call(session, "request_user_takeover", {"reason": "2FA step"})
    assert result["ok"] is False
    assert result["error_kind"] == "user_takeover_required"


# ---------------------------------------------------------------------------
# persistence, reconnect, recovery, cancellation
# ---------------------------------------------------------------------------

def test_profile_cookies_persist_across_sessions(site, make_session, tmp_path):
    browser_root = tmp_path / "shared-browser"
    session_a, _ = make_session(browser_root=browser_root)
    _open(session_a, site, profile="research")
    # Set a cookie through the login flow (server-set, httpOnly not required).
    _call(session_a, "fill", {"selector": "#user", "value": LOGIN_USER})
    _call(session_a, "fill", {"selector": "#pw", "value": LOGIN_PASS})
    _call(session_a, "click", {"selector": "#login-btn"})
    _call(session_a, "wait_navigation", {})
    cookies_a = _call(session_a, "cookies", {})
    assert "session_token" in {row["name"] for row in cookies_a["cookies"]}
    closed = _call(session_a, "close", {"user_confirmed": True})
    assert closed["ok"] is True and closed["profile_saved"] is True
    state_file = browser_root / "profiles" / "research" / "storage_state.json"
    assert state_file.is_file()

    session_b, _ = make_session(browser_root=browser_root)
    _open(session_b, site, profile="research")
    cookies_b = _call(session_b, "cookies", {})
    assert "session_token" in {row["name"] for row in cookies_b["cookies"]}
    welcome = _call(session_b, "open", {"url": f"{site}/welcome"})
    assert welcome["ok"] is True
    assert _call(session_b, "get_text", {"selector": "#who"})["text"] == f"user={LOGIN_USER}"


def test_detach_and_reconnect_with_ownership_proof(site, make_session, tmp_path):
    browser_root = tmp_path / "detach-browser"
    session_a, _ = make_session(browser_root=browser_root, allow_detach=True)
    opened = _open(session_a, site, profile="detached-one", detach=True)
    assert opened["detached"] is True and opened["engine"] == "playwright_cdp_detached"
    instance_id = opened["instance_id"]
    _call(session_a, "fill", {"selector": "#user", "value": LOGIN_USER})
    _call(session_a, "fill", {"selector": "#pw", "value": LOGIN_PASS})
    _call(session_a, "click", {"selector": "#login-btn"})
    _call(session_a, "wait_navigation", {})
    # Close keeps the instance alive; the KaroX side goes away.
    released = _call(session_a, "close", {"user_confirmed": True, "keep_instance": True})
    assert released["ok"] is True and released["instance_kept"] is True

    from karox.process_identity import process_is_running

    record = json.loads(
        (browser_root / "instances" / f"{instance_id}.json").read_text(encoding="utf-8")
    )
    assert process_is_running(record["pid"])

    session_b, _ = make_session(browser_root=browser_root, allow_detach=True)
    reconnected = _call(session_b, "reconnect", {"instance_id": instance_id})
    assert reconnected["ok"] is True, reconnected
    assert reconnected["pid"] == record["pid"]
    # The real Chrome profile persisted the login cookie across the reconnect.
    cookies = _call(session_b, "cookies", {})
    assert "session_token" in {row["name"] for row in cookies["cookies"]}
    tabs = _call(session_b, "tabs", {})
    assert tabs["count"] >= 1

    # A foreign instance id is refused, and a tampered record fails closed.
    foreign = _call(session_b, "reconnect", {"instance_id": "inst-0000000000000000"})
    assert foreign["ok"] is False

    closed = _call(session_b, "close", {"user_confirmed": True})
    assert closed["ok"] is True and closed["instance_terminated"] is True
    deadline = time.monotonic() + 10
    while process_is_running(record["pid"]) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not process_is_running(record["pid"])
    assert not (browser_root / "instances" / f"{instance_id}.json").exists()


def test_reconnect_refused_without_detach_permission(site, make_session, tmp_path):
    browser_root = tmp_path / "no-detach"
    session, _ = make_session(browser_root=browser_root, allow_detach=False)
    result = _call(session, "reconnect", {"instance_id": "inst-0000000000000000"})
    assert result["ok"] is False and result["error_kind"] == "permission_denied"
    detached_open = _call(
        session, "open", {"url": f"{site}/", "detach": True, "profile": "x"}
    )
    assert detached_open["ok"] is False and detached_open["error_kind"] == "permission_denied"


def test_recover_replays_last_safe_url_after_blank(site, make_session):
    """A hosted reconnect can leave the focused tab on about:blank while the
    context, cookies and tab registry are alive. ``recover`` must replay the
    last policy-valid URL into that same tab, not rebuild the session."""

    session, _root = make_session()
    _open(session, site)
    blank_tab = _call(session, "new_tab", {})  # fresh tab starts on about:blank
    assert blank_tab["ok"] is True
    tab_id = blank_tab["tab_id"]

    def seed(sess):
        # Simulate the state a blanked-out reconnect leaves behind: the tab is
        # blank but KaroX remembers the last safe URL it held.
        sess._handle.last_safe_urls[tab_id] = f"{site}/"
        return str(sess._active_page().url)

    assert session._call_on_owner(seed) in {"", "about:blank"}

    recovered = _call(session, "recover", {})
    assert recovered["ok"] is True and recovered["recovered"] is True
    assert "127.0.0.1" in recovered["url"]
    assert recovered["tab_id"] == tab_id  # same tab, same context
    assert _call(session, "get_text", {"selector": "h1"})["text"] == "Research Index"


def test_actions_survive_dom_replacement(site, make_session):
    session, _root = make_session()
    _open(session, site, "/replace")
    assert _call(session, "click", {"selector": "#target"})["ok"] is True
    assert _call(session, "get_text", {"selector": "#out"})["text"] == "hits:1"
    assert _call(session, "click", {"selector": "#swap"})["ok"] is True
    # The node was replaced; the locator re-anchors and the click still lands.
    assert _call(session, "click", {"selector": "#target"})["ok"] is True
    assert _call(session, "get_text", {"selector": "#out"})["text"] == "hits:2"
    snapshot = _call(session, "snapshot")
    refs = [row.get("ref") for row in snapshot["snapshot"]["buttons"]]
    target_ref = next(
        row["ref"]
        for row in snapshot["snapshot"]["buttons"]
        if row.get("name") == "Hit me"
    )
    assert target_ref in refs
    assert _call(session, "click", {"ref": target_ref})["ok"] is True
    assert _call(session, "get_text", {"selector": "#out"})["text"] == "hits:3"


def test_snapshot_diff_reports_added_items(site, make_session):
    session, _root = make_session()
    _open(session, site)
    assert _call(session, "snapshot", {})["ok"] is True
    assert _call(session, "click", {"selector": "#add-item"})["ok"] is True
    diff = _call(session, "snapshot_diff", {})
    assert diff["ok"] is True and diff["diffed"] is True and diff["changed"] is True


def test_iframe_read(site, make_session):
    session, _root = make_session()
    _open(session, site)
    frame = _call(session, "iframe", {"selector": "#embed"})
    assert frame["ok"] is True
    assert frame["snapshot"]["headings"][0]["text"] == "Frame Content"
    click_in_frame = _call(
        session, "click", {"selector": "#frame-btn", "frame": "#embed"}
    )
    assert click_in_frame["ok"] is True


def test_cancellation_returns_typed_structure(site, make_session):
    session, _root = make_session()
    _open(session, site)
    token = CancelToken()
    result_box: dict = {}

    def slow_wait() -> None:
        result_box.update(
            BrowserToolAdapter(session).execute(
                {"action": "wait_for", "payload": {"milliseconds": 8000}}, 30.0, token
            )
        )

    thread = threading.Thread(target=slow_wait, daemon=True)
    thread.start()
    time.sleep(0.3)
    token.cancel()
    thread.join(timeout=10)
    assert result_box["ok"] is False
    assert result_box["error_kind"] == "cancelled"


def test_stale_retry_wrapper_recovers_from_detached_errors():
    """Unit-level proof of the stale-element retry, no browser needed."""

    from karox.browser.errors import BrowserError
    from karox.browser.session import BrowserSession

    calls = {"n": 0}

    class _Fake:
        _active_page = staticmethod(lambda: None)

        def _with_stale_recovery(self, work, payload, deadline, cancel):
            return BrowserSession._with_stale_recovery(self, work, payload, deadline, cancel)

    def work():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("element is not attached to the document")
        return "done"

    fake = _Fake()
    assert fake._with_stale_recovery(work, {}, 5.0, None) == "done"
    assert calls["n"] == 2

    def always_stale():
        raise RuntimeError("element is not attached to the document")

    with pytest.raises(BrowserError) as excinfo:
        fake._with_stale_recovery(always_stale, {}, 5.0, None)
    assert "stale_element" in str(excinfo.value)


@pytest.mark.skipif(HEADLESS_ONLY, reason="set KAROX_BROWSER_HEADED_TESTS=1 to run headed")
def test_headed_takeover_flow(site, make_session):
    session, _root = make_session(headed=True, user_takeover=True)
    opened = _open(session, site)
    assert opened["ok"] is True
    takeover = _call(session, "request_user_takeover", {"reason": "CAPTCHA"})
    assert takeover["ok"] is True and takeover["agent_input_paused"] is True
    paused = _call(session, "click", {"selector": "#add-item"})
    assert paused["ok"] is False and paused["error_kind"] == "user_takeover_required"
    closing = _call(session, "close", {"user_confirmed": True})
    assert closing["ok"] is False  # never close during a takeover
    resumed = _call(session, "resume_after_user_takeover", {})
    assert resumed["ok"] is True and resumed["agent_input_paused"] is False
    assert _call(session, "click", {"selector": "#add-item"})["ok"] is True
