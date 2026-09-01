from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest import mock

from _support import SRC  # noqa: F401

import karox.cli as cli


def _store_with(**methods: object) -> mock.Mock:
    instance = mock.Mock()
    for name, value in methods.items():
        getattr(instance, name).return_value = value
    factory = mock.Mock(return_value=instance)
    return factory


def test_doctor_surfaces_repository_lease_degradation_without_misclassifying_tailscale_in_use() -> None:
    emitted: dict[str, object] = {}
    with (
        mock.patch.object(cli, "_provider_credential_doctor", return_value={"status": "ok"}),
        mock.patch.object(cli, "McpCredentialStore", _store_with(doctor={"status": "ok"})),
        mock.patch.object(cli, "BridgeCredentialStore", _store_with(doctor={"status": "ok"})),
        mock.patch.object(cli, "WebBridgeProfileStore", _store_with(doctor={"status": "ok"}, list=[])),
        mock.patch.object(cli, "RepositoryLeaseStore", _store_with(doctor={"status": "degraded", "stale": 1})),
        mock.patch.object(cli, "tailscale_doctor", return_value={"status": "in_use", "ready": True}),
        mock.patch.object(cli, "SessionStore", _store_with(list=[])),
        mock.patch.object(cli, "_pack_registry", return_value=mock.Mock(list=mock.Mock(return_value=[]))),
        mock.patch.object(
            cli,
            "_emit",
            side_effect=lambda value, *, json_output: emitted.setdefault("payload", value),
        ),
    ):
        exit_code = cli._handle_doctor(argparse.Namespace(json=True))

    assert exit_code == 0
    payload = emitted["payload"]
    assert isinstance(payload, dict)
    assert payload["status"] == "degraded"
    assert payload["checks"]["repository_leases"]["stale"] == 1
    assert payload["checks"]["tailscale"]["status"] == "in_use"


def test_doctor_is_ok_when_all_probes_are_healthy_or_in_use() -> None:
    emitted: dict[str, object] = {}
    with (
        mock.patch.object(cli, "_provider_credential_doctor", return_value={"status": "ok"}),
        mock.patch.object(cli, "McpCredentialStore", _store_with(doctor={"status": "ok"})),
        mock.patch.object(cli, "BridgeCredentialStore", _store_with(doctor={"status": "ok"})),
        mock.patch.object(cli, "WebBridgeProfileStore", _store_with(doctor={"status": "ok"}, list=[])),
        mock.patch.object(cli, "RepositoryLeaseStore", _store_with(doctor={"status": "ok", "stale": 0})),
        mock.patch.object(cli, "tailscale_doctor", return_value={"status": "in_use", "ready": True}),
        mock.patch.object(cli, "SessionStore", _store_with(list=[])),
        mock.patch.object(cli, "_pack_registry", return_value=mock.Mock(list=mock.Mock(return_value=[]))),
        mock.patch.object(
            cli,
            "_emit",
            side_effect=lambda value, *, json_output: emitted.setdefault("payload", value),
        ),
    ):
        exit_code = cli._handle_doctor(argparse.Namespace(json=True))

    assert exit_code == 0
    payload = emitted["payload"]
    assert isinstance(payload, dict)
    assert payload["status"] == "ok"


def test_saved_bridge_runtime_doctor_flags_only_desired_unhealthy_profiles() -> None:
    profiles = [
        SimpleNamespace(name="healthy"),
        SimpleNamespace(name="stale"),
        SimpleNamespace(name="disabled"),
    ]
    states = {
        "healthy": {
            "desired_running": True,
            "supervisor_alive": True,
            "supervisor_heartbeat_fresh": True,
            "supervisor_pid": 101,
        },
        "stale": {
            "desired_running": True,
            "supervisor_alive": False,
            "supervisor_heartbeat_fresh": False,
            "supervisor_pid": None,
            "last_error": "owner exited",
        },
        "disabled": {
            "desired_running": False,
            "supervisor_alive": False,
            "supervisor_heartbeat_fresh": False,
            "supervisor_pid": None,
        },
    }
    store = mock.Mock()
    store.list.return_value = profiles
    with (
        mock.patch.object(cli, "WebBridgeProfileStore", return_value=store),
        mock.patch(
            "karox.saved_bridge_supervisor.saved_bridge_supervisor_status",
            side_effect=lambda name: states[name],
        ),
    ):
        report = cli._saved_bridge_runtime_doctor()

    assert report["status"] == "degraded"
    assert report["desired_running_count"] == 2
    assert report["unhealthy_count"] == 1
    by_name = {item["name"]: item for item in report["bridges"]}
    assert by_name["healthy"]["healthy"] is True
    assert by_name["stale"]["healthy"] is False
    assert by_name["disabled"]["healthy"] is True
