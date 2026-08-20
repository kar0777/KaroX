from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import patch

from karox import cli


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
        to_dict=lambda: {"verdict": "stale_owned_process", "owned_orphan_pid": 4242},
    )

    with (
        patch.object(cli, "WebBridgeProfileStore") as store_cls,
        patch(
            "karox.port_ownership.check_port_ownership",
            return_value=ownership,
        ),
        patch(
            "karox.web_bridge_launcher._reclaim_orphaned_bridge_listener",
            return_value=True,
        ) as reclaim,
        patch.object(cli, "_saved_profile_connect_config", return_value=object()),
        patch.object(cli, "run_web_bridge", return_value=0),
    ):
        store_cls.return_value.get.return_value = saved_profile
        result = cli._handle_bridge_lifecycle(args)

    assert result == 0
    assert reclaim.call_args.args[0] == 4242
    assert reclaim.call_args.kwargs == {"port": 8765, "session_id": "web-saved-abc"}


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
        to_dict=lambda: {"verdict": "stale_owned_process", "owned_orphan_pid": 4242},
    )

    with (
        patch.object(cli, "WebBridgeProfileStore") as store_cls,
        patch(
            "karox.port_ownership.check_port_ownership",
            return_value=ownership,
        ),
        patch(
            "karox.web_bridge_launcher._reclaim_orphaned_bridge_listener",
            return_value=False,
        ),
        patch.object(cli, "run_web_bridge") as run_bridge,
    ):
        store_cls.return_value.get.return_value = SimpleNamespace(port=8765)
        result = cli._handle_bridge_lifecycle(args)

    assert result == 1
    run_bridge.assert_not_called()


def test_saved_bridge_restart_rebuilds_complete_connect_namespace() -> None:
    args = argparse.Namespace(
        saved="clickup-opus",
        bridge_command="restart",
        json=True,
    )
    saved_profile = SimpleNamespace(port=8765)
    ownership = SimpleNamespace(verdict="free")
    captured: dict[str, object] = {}

    def fake_saved_profile_connect_config(profile: object, **kwargs: object) -> object:
        captured["profile"] = profile
        captured.update(kwargs)
        return object()

    with (
        patch.object(cli, "WebBridgeProfileStore") as store_cls,
        patch(
            "karox.port_ownership.check_port_ownership",
            return_value=ownership,
        ),
        patch.object(
            cli,
            "_saved_profile_connect_config",
            side_effect=fake_saved_profile_connect_config,
        ),
        patch.object(cli, "run_web_bridge", return_value=0),
    ):
        store_cls.return_value.get.return_value = saved_profile
        result = cli._handle_bridge_lifecycle(args)

    assert result == 0
    assert captured["profile"] is saved_profile
    assert captured["port"] is None
    assert captured["repository"] is None
    assert captured["tool"] is None
    assert captured["write"] is False
