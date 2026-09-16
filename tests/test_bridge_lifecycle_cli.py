from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import patch

from karox import cli


def _saved_profile(port: int = 8765) -> SimpleNamespace:
    return SimpleNamespace(port=port)


def _ownership(
    *,
    verdict: str,
    pid: int | None = None,
    pid_proven: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        verdict=verdict,
        reason="",
        metadata=SimpleNamespace(pid=pid, pid_proven=pid_proven, session_id="web-saved-abc"),
        owned_orphan_pid=None,
        to_dict=lambda: {"verdict": verdict},
    )


def test_stop_delegates_to_the_durable_stop_service() -> None:
    """CLI stop must disarm desired_running before a live owner is touched."""
    args = argparse.Namespace(saved="chatgpt-dev", bridge_command="stop", json=True)
    receipt = {
        "saved_profile": "chatgpt-dev",
        "action": "stopped",
        "verdict": "reuse_same_profile",
        "reason": "",
        "pid": 900,
    }
    with (
        patch.object(cli, "WebBridgeProfileStore") as store_cls,
        patch(
            "karox.port_ownership.check_port_ownership",
            return_value=_ownership(verdict="reuse_same_profile", pid=900),
        ),
        patch(
            "karox.web_bridge_launcher.stop_saved_bridge",
            return_value=receipt,
        ) as stop,
        patch.object(cli, "run_web_bridge") as run_bridge,
    ):
        store_cls.return_value.get.return_value = _saved_profile()
        result = cli._handle_bridge_lifecycle(args)

    assert result == 0
    stop.assert_called_once_with("chatgpt-dev")
    run_bridge.assert_not_called()


def test_stop_reports_service_error_without_raw_kill() -> None:
    args = argparse.Namespace(saved="chatgpt-dev", bridge_command="stop", json=True)
    with (
        patch.object(cli, "WebBridgeProfileStore") as store_cls,
        patch(
            "karox.port_ownership.check_port_ownership",
            return_value=_ownership(verdict="reuse_same_profile", pid=900),
        ),
        patch(
            "karox.web_bridge_launcher.stop_saved_bridge",
            return_value={
                "saved_profile": "chatgpt-dev",
                "action": "error",
                "verdict": "supervisor_state_error",
                "error": "could not persist or verify bridge desired state; refusing lifecycle change",
            },
        ),
    ):
        store_cls.return_value.get.return_value = _saved_profile()
        result = cli._handle_bridge_lifecycle(args)

    assert result == 1


def test_stop_leaves_free_port_and_foreign_processes_alone() -> None:
    args = argparse.Namespace(saved="chatgpt-dev", bridge_command="stop", json=True)

    with (
        patch.object(cli, "WebBridgeProfileStore") as store_cls,
        patch(
            "karox.port_ownership.check_port_ownership",
            return_value=_ownership(verdict="unrelated_process"),
        ),
        patch(
            "karox.web_bridge_launcher.stop_saved_bridge",
            return_value={
                "saved_profile": "chatgpt-dev",
                "action": "no_action",
                "verdict": "unrelated_process",
                "reason": "port 8765 is held by an unrelated process",
            },
        ) as stop,
    ):
        store_cls.return_value.get.return_value = _saved_profile()
        result = cli._handle_bridge_lifecycle(args)

    assert result == 1
    stop.assert_called_once_with("chatgpt-dev")

    args_different = argparse.Namespace(saved="aura-browser", bridge_command="stop", json=True)
    with (
        patch.object(cli, "WebBridgeProfileStore") as store_cls,
        patch(
            "karox.port_ownership.check_port_ownership",
            return_value=_ownership(verdict="free"),
        ),
        patch(
            "karox.web_bridge_launcher.stop_saved_bridge",
            return_value={
                "saved_profile": "aura-browser",
                "action": "no_action",
                "verdict": "free",
            },
        ),
    ):
        store_cls.return_value.get.return_value = _saved_profile()
        assert cli._handle_bridge_lifecycle(args_different) == 0


def test_start_delegates_to_the_detached_start_service() -> None:
    args = argparse.Namespace(saved="aura-browser", bridge_command="start", json=True)
    with (
        patch.object(cli, "WebBridgeProfileStore") as store_cls,
        patch(
            "karox.port_ownership.check_port_ownership",
            return_value=_ownership(verdict="free"),
        ),
        patch(
            "karox.web_bridge_launcher.start_saved_bridge",
            return_value={
                "saved_profile": "aura-browser",
                "action": "started",
                "owner_pid": 910,
            },
        ) as start,
        patch.object(cli, "run_web_bridge") as run_bridge,
    ):
        store_cls.return_value.get.return_value = _saved_profile()
        result = cli._handle_bridge_lifecycle(args)

    assert result == 0
    start.assert_called_once_with("aura-browser")
    run_bridge.assert_not_called()


def test_start_reports_service_error() -> None:
    args = argparse.Namespace(saved="aura-browser", bridge_command="start", json=True)
    with (
        patch.object(cli, "WebBridgeProfileStore") as store_cls,
        patch(
            "karox.port_ownership.check_port_ownership",
            return_value=_ownership(verdict="free"),
        ),
        patch(
            "karox.web_bridge_launcher.start_saved_bridge",
            return_value={
                "saved_profile": "aura-browser",
                "action": "error",
                "error": "browser tunnel precheck failed",
            },
        ),
    ):
        store_cls.return_value.get.return_value = _saved_profile()
        assert cli._handle_bridge_lifecycle(args) == 1
