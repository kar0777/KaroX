from __future__ import annotations

from unittest.mock import patch

from karox.web_bridge_launcher import start_saved_bridge
from karox.web_bridge_profiles import WebBridgeProfileError, WebBridgeProfileStore


def test_start_saved_bridge_accepts_legacy_migration_kwarg() -> None:
    """Profile transactions may pass the compatibility flag into start()."""

    with patch.object(
        WebBridgeProfileStore,
        "get",
        side_effect=WebBridgeProfileError("profile does not exist"),
    ):
        result = start_saved_bridge(
            "missing-profile",
            timeout_seconds=0.1,
            allow_legacy_migration=True,
        )

    assert result["action"] == "error"
    assert result["saved_profile"] == "missing-profile"
    assert "profile does not exist" in result["error"]
