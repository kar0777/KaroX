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
from _browser_site import DOWNLOAD_BODY, site_server  # noqa: E402


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
