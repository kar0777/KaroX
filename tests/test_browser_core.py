"""End-to-end registration proof: browser.command through CoreRuntime.execute.

This is the wiring the integrator reproduces in the product entrypoints:

1. Build a :class:`BrowserSession` (policy + artifacts + upload roots) for the
   KaroX session.
2. Wrap it in a :class:`BrowserToolAdapter` whose capability guard reuses the
   runtime's own :class:`CapabilityPolicy`.
3. Register ``browser_tool_definition()`` and ``adapter.core_handler()`` in the
   CoreRuntime's definition/handler maps (the ``ExtendedCoreRuntime`` pattern).

After that a plain ``CoreCommand("browser.command", ...)`` runs through the
same policy, Smart-Stop risk and audit path as every other tool. The test uses
the live local fixture site from test_browser_live.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from karox.artifacts import ArtifactStore
from karox.browser.netpolicy import BrowserAccessPolicy
from karox.browser.session import BrowserSession
from karox.browser.tools import BrowserToolAdapter, browser_tool_definition
from karox.core import CoreRuntime
from karox.models import (
    AccessProfile,
    Capability,
    CoreCommand,
    Origin,
    OriginKind,
)
from karox.policy import CapabilityPolicy
from karox.risk_engine import RiskEngine, SmartStopRequired
from karox.sessions import SessionStore

pytest.importorskip("playwright")

# Pytest puts the tests directory on sys.path (prepend import mode), so the
# shared fixture-site helper is importable directly.
from _browser_site import DOWNLOAD_BODY, LOGIN_PASS, LOGIN_USER, site_server  # noqa: E402


@pytest.fixture(scope="module")
def site():
    with site_server() as base_url:
        yield base_url


def _build_core(
    tmp_path: Path,
    profile: AccessProfile,
    session_id: str,
    origin: Origin,
    grants: set[Capability] | None = None,
) -> tuple[CoreRuntime, CapabilityPolicy]:
    repository = tmp_path / "repo"
    repository.mkdir(exist_ok=True)
    (repository / "readme.txt").write_text("repo", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions")
    store.create(repository, "browser integration", profile, session_id=session_id)
    policy = CapabilityPolicy(profile)
    # Non-user origins start with no effective capabilities: the product
    # entrypoints grant them explicitly, and the effective set is always the
    # intersection with the profile.
    policy.set_grants(origin, grants if grants is not None else set())
    core = CoreRuntime(
        repository,
        policy,
        store,
        tmp_path / "audit.jsonl",
        risk=RiskEngine(),
    )
    return core, policy


def _register_browser(
    core: CoreRuntime,
    policy: CapabilityPolicy,
    origin: Origin,
    tmp_path: Path,
    session_id: str,
    *,
    downloads: bool = True,
) -> BrowserSession:
    artifacts_root = tmp_path / f"artifacts-{session_id}"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    uploads = tmp_path / f"uploads-{session_id}"
    uploads.mkdir(parents=True, exist_ok=True)
    session = BrowserSession(
        ArtifactStore(session_id, root=artifacts_root),
        BrowserAccessPolicy(
            session_id=session_id,
            downloads=downloads,
            plain_credentials=True,
            network_inspection=False,
        ),
        upload_roots=(uploads,),
        browser_root=tmp_path / f"browser-{session_id}",
    )
    adapter = BrowserToolAdapter(
        session, capability_guard=lambda cap: policy.require(origin, cap)
    )
    definition = browser_tool_definition()
    core._handlers[definition.name] = adapter.core_handler()
    core._definitions[definition.name] = definition
    return session


def test_browser_command_runs_through_core_with_policy_and_risk(tmp_path, site):
    session_id = f"core-{uuid.uuid4().hex[:8]}"
    origin = Origin(OriginKind.NATIVE_AGENT, "kernel-test")
    core, policy = _build_core(
        tmp_path,
        AccessProfile.BROWSER_CONTROL,
        session_id,
        origin,
        grants={
            Capability.BROWSER_READ,
            Capability.BROWSER_INPUT,
            Capability.NETWORK,
            Capability.REPO_READ,
        },
    )
    session = _register_browser(core, policy, origin, tmp_path, session_id)
    try:
        opened = core.execute(
            CoreCommand(
                "browser.command",
                {"action": "open", "payload": {"url": f"{site}/"}},
                session_id,
                origin,
                deadline_seconds=60.0,
            )
        )
        assert opened.ok is True
        assert opened.data["ok"] is True and opened.data["action"] == "open"

        snapshot = core.execute(
            CoreCommand(
                "browser.command",
                {"action": "snapshot", "payload": {}},
                session_id,
                origin,
                deadline_seconds=30.0,
            )
        )
        assert snapshot.data["ok"] is True
        assert snapshot.data["snapshot"]["headings"][0]["text"] == "Research Index"

        # A malformed call is a typed InvalidCommand, not a crash.
        from karox.core import InvalidCommand

        with pytest.raises(InvalidCommand):
            core.execute(
                CoreCommand(
                    "browser.command",
                    {"action": "launch_the_missiles", "payload": {}},
                    session_id,
                    origin,
                    deadline_seconds=10.0,
                )
            )

        # browser.upload is HIGH in the shared risk vocabulary: Smart Stop
        # requires a human confirmation token before the handler ever runs.
        with pytest.raises(SmartStopRequired):
            core.execute(
                CoreCommand(
                    "browser.command",
                    {"action": "upload", "payload": {"selector": "#file", "path": "x"}},
                    session_id,
                    origin,
                    deadline_seconds=10.0,
                )
            )
    finally:
        session.force_close()


def test_read_only_profile_can_observe_but_not_input(tmp_path, site):
    session_id = f"ro-{uuid.uuid4().hex[:8]}"
    origin = Origin(OriginKind.NATIVE_AGENT, "reader-test")
    core, policy = _build_core(
        tmp_path,
        AccessProfile.READ_ONLY,
        session_id,
        origin,
        grants={Capability.BROWSER_READ, Capability.REPO_READ},
    )
    session = _register_browser(core, policy, origin, tmp_path, session_id)
    try:
        # READ_ONLY carries browser.read but not browser.input, so even `open`
        # is refused by the per-action capability guard -- structured, typed,
        # and the browser never launched.
        refused = core.execute(
            CoreCommand(
                "browser.command",
                {"action": "open", "payload": {"url": f"{site}/"}},
                session_id,
                origin,
                deadline_seconds=30.0,
            )
        )
        assert refused.data["ok"] is False
        assert refused.data["error_kind"] == "permission_denied"
        assert session.is_open is False
    finally:
        session.force_close()


def test_network_capability_required_for_external_open(tmp_path):
    """An external URL needs Capability.NETWORK on top of browser input."""

    session_id = f"net-{uuid.uuid4().hex[:8]}"
    origin = Origin(OriginKind.NATIVE_AGENT, "external-test")
    core, policy = _build_core(
        tmp_path,
        AccessProfile.BROWSER_CONTROL,
        session_id,
        origin,
        grants={
            Capability.BROWSER_READ,
            Capability.BROWSER_INPUT,
            Capability.NETWORK,
            Capability.REPO_READ,
        },
    )
    # The profile grants NETWORK; deny it explicitly for this origin to prove
    # the adapter asks for it when the payload leaves the machine.
    policy.set_denies(origin, {Capability.NETWORK})
    session = _register_browser(core, policy, origin, tmp_path, session_id)
    try:
        refused = core.execute(
            CoreCommand(
                "browser.command",
                {
                    "action": "open",
                    "payload": {"url": "https://example.com/"},
                },
                session_id,
                origin,
                deadline_seconds=30.0,
            )
        )
        assert refused.data["ok"] is False
        assert refused.data["error_kind"] == "permission_denied"
        assert session.is_open is False
    finally:
        session.force_close()

def test_browser_command_downloads_and_stores_the_artifact(tmp_path, site):
    """A requested download is saved in the artifact store, not refused.

    The default agent-run policy enables downloads (the artifact lives in
    KaroX's store, never the repository), so a benchmark that asks for a
    downloaded deliverable can actually produce one.
    """
    import hashlib

    session_id = f"dl-{uuid.uuid4().hex[:8]}"
    origin = Origin(OriginKind.NATIVE_AGENT, "kernel-test")
    core, policy = _build_core(
        tmp_path,
        AccessProfile.BROWSER_CONTROL,
        session_id,
        origin,
        grants={
            Capability.BROWSER_READ,
            Capability.BROWSER_INPUT,
            Capability.NETWORK,
        },
    )
    session = _register_browser(core, policy, origin, tmp_path, session_id)
    try:
        opened = core.execute(
            CoreCommand(
                "browser.command",
                {"action": "open", "payload": {"url": f"{site}/"}},
                session_id,
                origin,
                deadline_seconds=60.0,
            )
        )
        assert opened.ok is True

        downloaded = core.execute(
            CoreCommand(
                "browser.command",
                {
                    "action": "download_start",
                    "payload": {"selector": "#dl-link"},
                },
                session_id,
                origin,
                deadline_seconds=60.0,
            )
        )
        assert downloaded.ok is True, downloaded.data
        data = downloaded.data
        assert data["status"] == "completed"
        assert data["suggested_filename"] == "report.bin"
        assert data["size"] == len(DOWNLOAD_BODY)
        assert data["sha256"] == hashlib.sha256(DOWNLOAD_BODY).hexdigest()
    finally:
        session.force_close()

def test_download_over_the_artifact_limit_fails_before_reading(tmp_path, site, monkeypatch):
    """An oversized download is refused on the temp file, not read into RAM.

    The artifact store enforces the same ceiling, but only after ``read_bytes``
    has already loaded the whole file. The session checks the saved size first
    and removes the temp file.
    """
    import karox.browser.session as session_module

    monkeypatch.setattr(session_module, "MAX_ARTIFACT_BYTES", 1)
    session_id = f"dlcap-{uuid.uuid4().hex[:8]}"
    origin = Origin(OriginKind.NATIVE_AGENT, "kernel-test")
    core, policy = _build_core(
        tmp_path,
        AccessProfile.BROWSER_CONTROL,
        session_id,
        origin,
        grants={
            Capability.BROWSER_READ,
            Capability.BROWSER_INPUT,
            Capability.NETWORK,
        },
    )
    session = _register_browser(core, policy, origin, tmp_path, session_id)
    try:
        opened = core.execute(
            CoreCommand(
                "browser.command",
                {"action": "open", "payload": {"url": f"{site}/"}},
                session_id,
                origin,
                deadline_seconds=60.0,
            )
        )
        assert opened.ok is True
        refused = core.execute(
            CoreCommand(
                "browser.command",
                {"action": "download_start", "payload": {"selector": "#dl-link"}},
                session_id,
                origin,
                deadline_seconds=60.0,
            )
        )
        assert refused.data["ok"] is False
        assert "artifact limit" in str(refused.data.get("error"))
    finally:
        session.force_close()

def _engine_browser(tmp_path, monkeypatch, *, plain_credentials):
    """Register the agent-run browser surface against the fixture site."""

    from karox import engine_tools

    monkeypatch.setattr(engine_tools, "runtime_dir", lambda: tmp_path / "runtime")
    session_id = f"eng-{uuid.uuid4().hex[:8]}"
    origin = Origin(OriginKind.NATIVE_AGENT, "kernel-test")
    core, policy = _build_core(
        tmp_path,
        AccessProfile.BROWSER_CONTROL,
        session_id,
        origin,
        grants={Capability.BROWSER_READ, Capability.BROWSER_INPUT},
    )
    runtime = engine_tools.register_browser_tool(
        core,
        capability_policy=policy,
        origin=origin,
        session_id=session_id,
        plain_credentials=plain_credentials,
    )
    return core, runtime, session_id, origin


def _browser(core, session_id, origin, action, payload=None):
    return core.execute(
        CoreCommand(
            "browser.command",
            {"action": action, "payload": payload or {}},
            session_id,
            origin,
            deadline_seconds=60.0,
        )
    )


def test_engine_browser_refuses_secret_fill_and_character_press_by_default(
    tmp_path, site, monkeypatch
):
    """The password guard covers typing, not just ``fill``.

    A single-character ``press`` writes into the focused element exactly like
    ``fill``; before the guard it bypassed the password refusal one character
    at a time. The fixture login form makes both paths observable.
    """
    core, runtime, session_id, origin = _engine_browser(
        tmp_path, monkeypatch, plain_credentials=False
    )
    try:
        assert _browser(core, session_id, origin, "open", {"url": f"{site}/"}).data[
            "ok"
        ] is True

        filled = _browser(
            core, session_id, origin, "fill", {"selector": "#pw", "value": LOGIN_PASS}
        )
        assert filled.data["ok"] is False
        assert "password" in filled.data["error"]

        targeted = _browser(
            core, session_id, origin, "press", {"selector": "#pw", "key": "a"}
        )
        assert targeted.data["ok"] is False
        assert "password" in targeted.data["error"]

        focused = _browser(core, session_id, origin, "focus", {"selector": "#pw"})
        assert focused.data["ok"] is True
        page_level = _browser(core, session_id, origin, "press", {"key": "a"})
        assert page_level.data["ok"] is False
        assert "password" in page_level.data["error"]

        # A named key on the same field is a command, not text: Enter submits.
        submitted = _browser(
            core, session_id, origin, "press", {"selector": "#pw", "key": "enter"}
        )
        assert submitted.data["ok"] is True
        assert submitted.data["key"] == "Enter"
    finally:
        runtime.close_all()


def test_engine_browser_logs_in_with_test_credentials_when_allowed(
    tmp_path, site, monkeypatch
):
    """The benchmark flow: fill the fixture login, submit, reach the page.

    Plain test credentials are a per-run user permission; with it the whole
    login path works through the agent-run surface, and without it the same
    fill is refused (covered above).
    """
    core, runtime, session_id, origin = _engine_browser(
        tmp_path, monkeypatch, plain_credentials=True
    )
    try:
        assert _browser(core, session_id, origin, "open", {"url": f"{site}/"}).data[
            "ok"
        ] is True
        assert _browser(
            core,
            session_id,
            origin,
            "fill",
            {"selector": "#user", "value": LOGIN_USER},
        ).data["ok"] is True
        password = _browser(
            core, session_id, origin, "fill", {"selector": "#pw", "value": LOGIN_PASS}
        )
        assert password.data["ok"] is True
        assert password.data["protected_field"] is True

        clicked = _browser(
            core, session_id, origin, "click", {"selector": "#login-btn"}
        )
        assert clicked.data["ok"] is True
        _browser(core, session_id, origin, "wait_navigation", {"timeout_ms": 10_000})

        text = _browser(core, session_id, origin, "get_text", {"selector": "#who"})
        assert text.data["ok"] is True
        assert text.data["text"].strip() == f"user={LOGIN_USER}"
    finally:
        runtime.close_all()


def test_engine_browser_press_accepts_punctuation_and_common_names(
    tmp_path, site, monkeypatch
):
    """``press`` handles the spellings models actually emit."""

    core, runtime, session_id, origin = _engine_browser(
        tmp_path, monkeypatch, plain_credentials=False
    )
    try:
        assert _browser(core, session_id, origin, "open", {"url": f"{site}/"}).data[
            "ok"
        ] is True
        dash = _browser(
            core, session_id, origin, "press", {"selector": "#user", "key": "-"}
        )
        assert dash.data["ok"] is True
        assert dash.data["key"] == "-"
        state = _browser(core, session_id, origin, "get_form_state", {})
        username = [
            field
            for form in state.data["forms"]
            for field in form["fields"]
            if field.get("id") == "user"
        ][0]
        assert username["value"] == "-"

        spaced = _browser(
            core, session_id, origin, "press", {"selector": "#user", "key": "ctrl + a"}
        )
        assert spaced.data["ok"] is True
        assert spaced.data["key"] == "Control+a"

        named = _browser(core, session_id, origin, "press", {"key": "return"})
        assert named.data["ok"] is True
        assert named.data["key"] == "Enter"
    finally:
        runtime.close_all()


def test_engine_browser_get_text_without_a_selector_reads_the_page(
    tmp_path, site, monkeypatch
):
    """A snapshot already scopes the page; reading it must not need a selector."""

    core, runtime, session_id, origin = _engine_browser(
        tmp_path, monkeypatch, plain_credentials=False
    )
    try:
        assert _browser(core, session_id, origin, "open", {"url": f"{site}/"}).data[
            "ok"
        ] is True
        text = _browser(core, session_id, origin, "get_text", {})
        assert text.data["ok"] is True
        assert text.data["target"] == "page"
        assert "Research Index" in text.data["text"]
    finally:
        runtime.close_all()
