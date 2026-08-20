from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from karox.models import AccessProfile
from karox.web_bridge_launcher import delete_saved_bridge_profile
from karox.web_bridge_profiles import SavedWebBridgeProfile


def _profile(name: str = "chatgpt-dev", refs: tuple[str, ...] = ()) -> SavedWebBridgeProfile:
    return SavedWebBridgeProfile(
        name=name,
        target_profile="chatgpt-web",
        tools=("karox.repo.read_file",),
        access_profile=AccessProfile.WORKSPACE_WRITE,
        tunnel="tailscale",
        browser_credential_refs=refs,
    )


class _Store:
    def __init__(self, current: SavedWebBridgeProfile, siblings: tuple[SavedWebBridgeProfile, ...] = ()) -> None:
        self.current = current
        self.siblings = siblings
        self.deleted = False

    def get(self, name: str) -> SavedWebBridgeProfile:
        assert name == self.current.name
        return self.current

    def list(self) -> tuple[SavedWebBridgeProfile, ...]:
        return (self.current, *self.siblings)

    def delete(self, name: str) -> SavedWebBridgeProfile:
        assert name == self.current.name
        self.deleted = True
        return self.current


class _BrowserRegistry:
    def __init__(self, instances: tuple[object, ...] = ()) -> None:
        self.instances = instances
        self.deleted: list[str] = []

    def list_instances(self) -> tuple[object, ...]:
        return self.instances

    def delete(self, instance_id: str) -> bool:
        self.deleted.append(instance_id)
        return True


def _supervisor_paths(root: Path):
    return (
        root / "state.json",
        root / "heartbeat.json",
        root / "desired.json",
    )


def test_delete_aborts_before_identity_or_profile_when_owned_stop_fails() -> None:
    profile = _profile()
    store = _Store(profile)
    with (
        mock.patch("karox.web_bridge_profiles.WebBridgeProfileStore", return_value=store),
        mock.patch(
            "karox.web_bridge_launcher.stop_saved_bridge",
            return_value={"action": "error", "error": "synthetic stop failure"},
        ),
        mock.patch("karox.web_bridge_launcher.delete_saved_web_bridge_identity") as identity,
    ):
        result = delete_saved_bridge_profile(profile.name)

    assert result["action"] == "error"
    assert result["phase"] == "stop"
    assert store.deleted is False
    identity.assert_not_called()


def test_delete_preserves_shared_browser_credential_and_deletes_exclusive_one() -> None:
    shared = "os-keyring:browser/shared-test"
    exclusive = "os-keyring:browser/exclusive-test"
    profile = _profile(refs=(shared, exclusive))
    sibling = _profile(name="claude-dev", refs=(shared,))
    store = _Store(profile, (sibling,))
    credentials = mock.Mock()
    browser_registry = _BrowserRegistry()

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state, heartbeat, desired = _supervisor_paths(root)
        for path in (state, heartbeat, desired):
            path.write_text("{}", encoding="utf-8")
        with (
            mock.patch("karox.web_bridge_profiles.WebBridgeProfileStore", return_value=store),
            mock.patch("karox.web_bridge_launcher.stop_saved_bridge", return_value={"action": "stopped"}),
            mock.patch(
                "karox.web_bridge_launcher.delete_saved_web_bridge_identity",
                return_value={"session": "deleted", "credential": "deleted"},
            ),
            mock.patch("karox.managed_browser.BrowserInstanceRegistry", return_value=browser_registry),
            mock.patch("karox.browser_credentials.BrowserCredentialStore", return_value=credentials),
            mock.patch("karox.saved_bridge_supervisor.saved_bridge_supervisor_status", return_value={"supervisor_alive": False}),
            mock.patch("karox.saved_bridge_supervisor.supervisor_state_path", return_value=state),
            mock.patch("karox.saved_bridge_supervisor.supervisor_heartbeat_path", return_value=heartbeat),
            mock.patch("karox.saved_bridge_supervisor.supervisor_desired_state_path", return_value=desired),
        ):
            result = delete_saved_bridge_profile(profile.name)

    assert result["action"] == "deleted"
    assert result["browser_credentials_shared"] == [shared]
    assert result["browser_credentials_deleted"] == [exclusive]
    credentials.delete.assert_called_once_with("exclusive-test")
    assert store.deleted is True
    assert result["supervisor_cleanup"] == "deleted"


def test_delete_never_touches_foreign_or_unverified_live_managed_browser() -> None:
    profile = _profile()
    store = _Store(profile)
    matching_stale = SimpleNamespace(
        instance_id="owned-stale",
        saved_profile_id=profile.name,
        session_id="web-saved-current",
        browser_pid=0,
        user_data_dir="unused-stale",
    )
    matching_unverified_live = SimpleNamespace(
        instance_id="owned-unverified-live",
        saved_profile_id=profile.name,
        session_id="web-saved-current",
        browser_pid=12345,
        user_data_dir="unused-live",
    )
    foreign = SimpleNamespace(
        instance_id="foreign",
        saved_profile_id="claude-dev",
        session_id="web-saved-current",
        browser_pid=0,
        user_data_dir="foreign-profile",
    )
    registry = _BrowserRegistry((matching_stale, matching_unverified_live, foreign))

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state, heartbeat, desired = _supervisor_paths(root)
        with (
            mock.patch("karox.web_bridge_profiles.WebBridgeProfileStore", return_value=store),
            mock.patch("karox.web_bridge_launcher.stop_saved_bridge", return_value={"action": "stopped"}),
            mock.patch(
                "karox.web_bridge_launcher.delete_saved_web_bridge_identity",
                return_value={"session": "deleted", "credential": "deleted"},
            ),
            mock.patch(
                "karox.web_bridge_launcher.saved_web_bridge_session_candidates",
                return_value=("web-saved-current",),
            ),
            mock.patch("karox.managed_browser.BrowserInstanceRegistry", return_value=registry),
            mock.patch("karox.managed_browser.verify_managed_browser", return_value=False) as verify,
            mock.patch("karox.web_bridge_launcher._process_is_alive", side_effect=lambda pid: pid == 12345),
            mock.patch("karox.extension_browser._terminate_profile_chrome") as terminate,
            mock.patch("karox.browser_credentials.BrowserCredentialStore"),
            mock.patch("karox.saved_bridge_supervisor.saved_bridge_supervisor_status", return_value={"supervisor_alive": False}),
            mock.patch("karox.saved_bridge_supervisor.supervisor_state_path", return_value=state),
            mock.patch("karox.saved_bridge_supervisor.supervisor_heartbeat_path", return_value=heartbeat),
            mock.patch("karox.saved_bridge_supervisor.supervisor_desired_state_path", return_value=desired),
        ):
            result = delete_saved_bridge_profile(profile.name)

    assert registry.deleted == ["owned-stale"]
    assert result["browser_instances_deleted"] == ["owned-stale"]
    assert result["browser_cleanup_pending"] == ["owned-unverified-live"]
    assert result["cleanup_pending"] is True
    verify.assert_called_once_with(matching_unverified_live)
    terminate.assert_not_called()
