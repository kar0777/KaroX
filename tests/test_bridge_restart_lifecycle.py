from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import patch

from karox import cli
from karox.process_identity import ProcessIdentity, argv_digest


def test_saved_bridge_restart_reclaims_its_own_orphaned_listener() -> None:
    """A dead owner's surviving bridge child must not block restart forever."""
    args = argparse.Namespace(
        saved="clickup-opus",
        bridge_command="restart",
        json=True,
    )
    saved_profile = SimpleNamespace(port=8765)
    ownership = SimpleNamespace(
        verdict="stale_owned_process",
        reason="our own bridge child outlived its owner",
        metadata=SimpleNamespace(session_id="web-saved-abc"),
        owned_orphan_pid=4242,
        live_unrecorded_owner_pid=None,
        owned_orphan_identity=ProcessIdentity(4242, 101, argv_sha256=argv_digest(["karox", "bridge", "serve", "--session-id", "web-saved-abc", "--port", "8765"])),
        to_dict=lambda: {"verdict": "stale_owned_process", "owned_orphan_pid": 4242},
    )

    with (
        patch.object(cli, "WebBridgeProfileStore") as store_cls,
        patch(
            "karox.port_ownership.check_port_ownership",
            return_value=ownership,
        ),
        patch(
            "karox.saved_bridge_recovery.reclaim_saved_bridge_orphan",
            return_value=True,
        ) as reclaim,
        patch(
            "karox.web_bridge_launcher.restart_saved_bridge",
            return_value={"saved_profile": "clickup-opus", "action": "restarted"},
        ) as restart,
        patch.object(cli, "run_web_bridge") as run_bridge,
    ):
        store_cls.return_value.get.return_value = saved_profile
        result = cli._handle_bridge_lifecycle(args)

    assert result == 0
    reclaim.assert_called_once_with("clickup-opus", port=8765, ownership=ownership)
    assert restart.call_args.args == ("clickup-opus",)
    run_bridge.assert_not_called()


def test_saved_bridge_restart_stops_when_the_orphan_cannot_be_reclaimed() -> None:
    args = argparse.Namespace(
        saved="clickup-opus",
        bridge_command="restart",
        json=True,
    )
    ownership = SimpleNamespace(
        verdict="stale_owned_process",
        reason="our own bridge child outlived its owner",
        metadata=SimpleNamespace(session_id="web-saved-abc"),
        owned_orphan_pid=4242,
        live_unrecorded_owner_pid=None,
        owned_orphan_identity=ProcessIdentity(4242, 101, argv_sha256=argv_digest(["karox", "bridge", "serve", "--session-id", "web-saved-abc", "--port", "8765"])),
        to_dict=lambda: {"verdict": "stale_owned_process", "owned_orphan_pid": 4242},
    )

    with (
        patch.object(cli, "WebBridgeProfileStore") as store_cls,
        patch(
            "karox.port_ownership.check_port_ownership",
            return_value=ownership,
        ),
        patch(
            "karox.saved_bridge_recovery.reclaim_saved_bridge_orphan",
            return_value=False,
        ),
        patch.object(cli, "run_web_bridge") as run_bridge,
    ):
        store_cls.return_value.get.return_value = SimpleNamespace(port=8765)
        result = cli._handle_bridge_lifecycle(args)

    assert result == 1
    run_bridge.assert_not_called()


def test_saved_bridge_restart_delegates_to_the_detached_restart_service() -> None:
    """The CLI must not become the bridge owner: a restart has to return."""
    args = argparse.Namespace(
        saved="clickup-opus",
        bridge_command="restart",
        json=True,
    )
    receipt = {
        "saved_profile": "clickup-opus",
        "action": "restarted",
        "recovery_armed": True,
        "supervisor_pid": 700,
        "owner_pid": 800,
        "public_url": "https://clickup.example.ts.net",
    }
    with (
        patch.object(cli, "WebBridgeProfileStore") as store_cls,
        patch(
            "karox.port_ownership.check_port_ownership",
            return_value=SimpleNamespace(
                verdict="free",
                owned_orphan_pid=None,
                to_dict=lambda: {"verdict": "free"},
            ),
        ),
        patch(
            "karox.web_bridge_launcher.restart_saved_bridge",
            return_value=receipt,
        ) as restart,
        patch.object(cli, "run_web_bridge") as run_bridge,
    ):
        store_cls.return_value.get.return_value = SimpleNamespace(port=8765)
        result = cli._handle_bridge_lifecycle(args)

    assert result == 0
    restart.assert_called_once()
    run_bridge.assert_not_called()


def test_saved_bridge_restart_reports_service_error_without_relaunch_hang() -> None:
    args = argparse.Namespace(
        saved="clickup-opus",
        bridge_command="restart",
        json=True,
    )
    with (
        patch.object(cli, "WebBridgeProfileStore") as store_cls,
        patch(
            "karox.port_ownership.check_port_ownership",
            return_value=SimpleNamespace(
                verdict="free",
                owned_orphan_pid=None,
                to_dict=lambda: {"verdict": "free"},
            ),
        ),
        patch(
            "karox.web_bridge_launcher.restart_saved_bridge",
            return_value={
                "saved_profile": "clickup-opus",
                "action": "error",
                "phase": "supervisor",
                "error": "restart recovery supervisor is unavailable",
            },
        ) as restart,
        patch.object(cli, "run_web_bridge") as run_bridge,
    ):
        store_cls.return_value.get.return_value = SimpleNamespace(port=8765)
        result = cli._handle_bridge_lifecycle(args)

    assert result == 1
    restart.assert_called_once()
    run_bridge.assert_not_called()
