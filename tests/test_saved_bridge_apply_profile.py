from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from karox.models import AccessProfile
from karox.project_registry import ProjectRegistry
from karox.web_bridge_launcher import apply_saved_bridge_profile
from karox.web_bridge_profiles import SavedWebBridgeProfile


def _profile(*, port: int = 8765) -> SavedWebBridgeProfile:
    return SavedWebBridgeProfile(
        name="apply-profile-test",
        target_profile="chatgpt-web",
        tools=("karox.repo.read_file",),
        access_profile=AccessProfile.WORKSPACE_WRITE,
        tunnel="tailscale",
        port=port,
        browser_external_https=True,
        browser_headed=True,
        browser_user_takeover=True,
    )


class _Store:
    def __init__(self, profile: SavedWebBridgeProfile) -> None:
        self.profile = profile
        self.puts: list[SavedWebBridgeProfile] = []

    def get(self, name: str) -> SavedWebBridgeProfile:
        assert name == self.profile.name
        return self.profile

    def put(self, profile: SavedWebBridgeProfile, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.profile = profile
        self.puts.append(profile)


def _ownership(verdict: str, *, pid_proven: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        verdict=verdict,
        metadata=SimpleNamespace(pid_proven=pid_proven),
    )


def test_free_stopped_profile_is_persisted_without_lifecycle_calls() -> None:
    previous = _profile()
    updated = _profile(port=9876)
    store = _Store(previous)
    with (
        mock.patch("karox.web_bridge_profiles.WebBridgeProfileStore", return_value=store),
        mock.patch(
            "karox.port_ownership.check_port_ownership",
            return_value=_ownership("free"),
        ) as ownership,
        mock.patch(
            "karox.saved_bridge_supervisor.saved_bridge_supervisor_status",
            return_value={"desired_running": False},
        ),
        mock.patch("karox.web_bridge_launcher.stop_saved_bridge") as stop,
        mock.patch("karox.web_bridge_launcher.start_saved_bridge") as start,
    ):
        result = apply_saved_bridge_profile(previous.name, updated)

    ownership.assert_called_once_with(previous.name, port=8765)
    stop.assert_not_called()
    start.assert_not_called()
    assert result["status"] == "saved"
    assert store.profile == updated


def test_live_profile_requires_explicit_restart_consent_before_any_mutation() -> None:
    previous = _profile()
    updated = _profile(port=9876)
    store = _Store(previous)
    with (
        mock.patch("karox.web_bridge_profiles.WebBridgeProfileStore", return_value=store),
        mock.patch(
            "karox.port_ownership.check_port_ownership",
            return_value=_ownership("reuse_same_profile", pid_proven=True),
        ),
        mock.patch(
            "karox.saved_bridge_supervisor.saved_bridge_supervisor_status",
            return_value={"desired_running": True},
        ),
        mock.patch("karox.web_bridge_launcher.stop_saved_bridge") as stop,
        mock.patch("karox.web_bridge_launcher.start_saved_bridge") as start,
    ):
        with pytest.raises(RuntimeError, match="explicit restart confirmation"):
            apply_saved_bridge_profile(previous.name, updated, allow_restart=False)

    stop.assert_not_called()
    start.assert_not_called()
    assert store.puts == []
    assert store.profile == previous


def test_live_profile_stops_old_port_before_writing_and_starts_new() -> None:
    previous = _profile()
    updated = _profile(port=9876)
    store = _Store(previous)
    order: list[str] = []

    def stop(_name: str, **_kwargs: Any) -> dict[str, Any]:
        assert store.profile.port == 8765
        order.append("stop-old")
        return {"action": "stopped"}

    def start(_name: str, **_kwargs: Any) -> dict[str, Any]:
        assert store.profile.port == 9876
        order.append("start-new")
        return {"action": "started"}

    with (
        mock.patch("karox.web_bridge_profiles.WebBridgeProfileStore", return_value=store),
        mock.patch(
            "karox.port_ownership.check_port_ownership",
            return_value=_ownership("reuse_same_profile", pid_proven=True),
        ) as ownership,
        mock.patch(
            "karox.saved_bridge_supervisor.saved_bridge_supervisor_status",
            return_value={"desired_running": True},
        ),
        mock.patch("karox.web_bridge_launcher.stop_saved_bridge", side_effect=stop),
        mock.patch("karox.web_bridge_launcher.start_saved_bridge", side_effect=start),
    ):
        result = apply_saved_bridge_profile(
            previous.name,
            updated,
            allow_restart=True,
        )

    ownership.assert_called_once_with(previous.name, port=8765)
    assert order == ["stop-old", "start-new"]
    assert result["status"] == "restarted"
    assert store.profile == updated


def test_failed_new_activation_rolls_profile_and_runtime_back() -> None:
    previous = _profile()
    updated = _profile(port=9876)
    store = _Store(previous)
    starts: list[int] = []

    def start(_name: str, **_kwargs: Any) -> dict[str, Any]:
        starts.append(store.profile.port)
        if store.profile.port == 9876:
            return {"action": "error", "error": "synthetic activation failure"}
        return {"action": "started"}

    with (
        mock.patch("karox.web_bridge_profiles.WebBridgeProfileStore", return_value=store),
        mock.patch(
            "karox.port_ownership.check_port_ownership",
            return_value=_ownership("reuse_same_profile", pid_proven=True),
        ),
        mock.patch(
            "karox.saved_bridge_supervisor.saved_bridge_supervisor_status",
            return_value={"desired_running": True},
        ),
        mock.patch(
            "karox.web_bridge_launcher.stop_saved_bridge",
            return_value={"action": "stopped"},
        ),
        mock.patch("karox.web_bridge_launcher.start_saved_bridge", side_effect=start),
    ):
        with pytest.raises(RuntimeError, match="previous connection was restored"):
            apply_saved_bridge_profile(
                previous.name,
                updated,
                allow_restart=True,
            )

    assert starts == [9876, 8765]
    assert store.profile == previous
    assert store.puts[-1] == previous


def test_stale_owned_profile_uses_guarded_stop_even_when_supervisor_not_desired() -> None:
    previous = _profile()
    updated = _profile(port=9876)
    store = _Store(previous)
    with (
        mock.patch("karox.web_bridge_profiles.WebBridgeProfileStore", return_value=store),
        mock.patch(
            "karox.port_ownership.check_port_ownership",
            return_value=_ownership("stale_owned_process"),
        ),
        mock.patch(
            "karox.saved_bridge_supervisor.saved_bridge_supervisor_status",
            return_value={"desired_running": False},
        ),
        mock.patch(
            "karox.web_bridge_launcher.stop_saved_bridge",
            return_value={"action": "stopped", "verdict": "stale_owned_process"},
        ) as stop,
        mock.patch("karox.web_bridge_launcher.start_saved_bridge") as start,
    ):
        result = apply_saved_bridge_profile(previous.name, updated)

    stop.assert_called_once()
    start.assert_not_called()
    assert result["status"] == "saved"
    assert store.profile == updated


def test_live_project_registry_hot_updates_without_restart(tmp_path: Path) -> None:
    anchor = tmp_path / "anchor"
    second = tmp_path / "second"
    anchor.mkdir()
    second.mkdir()
    previous = SavedWebBridgeProfile(
        name="apply-profile-test",
        target_profile="chatgpt-web",
        repository=str(anchor),
        tools=("karox.repo.read_file",),
        access_profile=AccessProfile.WORKSPACE_WRITE,
        tunnel="tailscale",
    )
    registry = ProjectRegistry.from_profile(
        repository=previous.repository,
        projects=previous.projects,
        default_project_id=previous.default_project_id,
    ).add(second, make_default=True)
    updated = SavedWebBridgeProfile(
        name=previous.name,
        target_profile=previous.target_profile,
        repository=previous.repository,
        projects=tuple(registry.to_payload()),
        default_project_id=registry.default_project_id,
        tools=previous.tools,
        access_profile=previous.access_profile,
        tunnel=previous.tunnel,
    )
    store = _Store(previous)
    with (
        mock.patch("karox.web_bridge_profiles.WebBridgeProfileStore", return_value=store),
        mock.patch("karox.port_ownership.check_port_ownership") as ownership,
        mock.patch("karox.saved_bridge_supervisor.saved_bridge_supervisor_status") as supervisor,
        mock.patch("karox.web_bridge_launcher.stop_saved_bridge") as stop,
        mock.patch("karox.web_bridge_launcher.start_saved_bridge") as start,
    ):
        result = apply_saved_bridge_profile(previous.name, updated, allow_restart=False)

    ownership.assert_not_called()
    supervisor.assert_not_called()
    stop.assert_not_called()
    start.assert_not_called()
    assert result["status"] == "hot_updated"
    assert result["runtime"]["live_reload"] == "next_tool_call"
    assert store.profile == updated
