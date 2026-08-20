from __future__ import annotations

from pathlib import Path

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox.browser_access import BrowserAccessPolicy
from karox.models import AccessProfile
from karox.web_bridge_launcher import WebBridgeConnectConfig, _bridge_argv, web_bridge_diagnostics


def test_headed_takeover_profile_is_persistent_extension_browser() -> None:
    config = WebBridgeConnectConfig(
        profile="chatgpt-web",
        repository=Path.cwd(),
        access_profile=AccessProfile.BROWSER_CONTROL,
        tunnel="custom",
        public_url="https://bridge.example",
        browser_external_https=True,
        browser_headed=True,
        browser_user_takeover=True,
    )

    diagnostics = web_bridge_diagnostics(config, session_id="restart-contract")

    assert "karox.browser.fill_credential" in config.tools
    assert diagnostics["browser_permission"]["backend"] == "extension"
    assert diagnostics["browser_isolation"]["dedicated_chrome_profile"] is True
    assert diagnostics["browser_lifecycle"]["survives_bridge_restart"] is True
    assert diagnostics["browser_isolation"]["main_chrome_profile_visible"] is False


def test_non_takeover_browser_reports_no_bridge_restart_persistence() -> None:
    config = WebBridgeConnectConfig(
        profile="chatgpt-web",
        repository=Path.cwd(),
        access_profile=AccessProfile.BROWSER_CONTROL,
        tunnel="custom",
        public_url="https://bridge.example",
        browser_external_https=True,
        browser_headed=False,
        browser_user_takeover=False,
    )

    diagnostics = web_bridge_diagnostics(config, session_id="playwright-contract")

    assert "karox.browser.fill_credential" in config.tools
    assert diagnostics["browser_permission"]["backend"] == "playwright"
    assert diagnostics["browser_isolation"]["dedicated_chrome_profile"] is False
    assert diagnostics["browser_lifecycle"]["survives_bridge_restart"] is False


def test_saved_bridge_child_argv_carries_secret_free_profile_binding() -> None:
    config = WebBridgeConnectConfig(
        profile="chatgpt-web",
        repository=Path.cwd(),
        saved_profile_name="chatgpt-dev",
        access_profile=AccessProfile.WORKSPACE_WRITE,
        tunnel="custom",
        public_url="https://bridge.example",
    )

    argv = _bridge_argv(
        config,
        session_id="saved-profile-binding-session",
        public_url="https://bridge.example",
    )

    marker = argv.index("--saved-profile-name")
    assert argv[marker + 1] == "chatgpt-dev"
    assert "chatgpt-dev" not in argv[:marker]


def test_browser_policy_exposes_exact_saved_profile_binding() -> None:
    policy = BrowserAccessPolicy(
        session_id="saved-profile-binding-session",
        headed=True,
        user_takeover=True,
        backend="extension",
        saved_profile_id="chatgpt-dev",
    )

    assert policy.saved_profile_id == "chatgpt-dev"
    assert policy.to_diagnostics()["saved_profile_id"] == "chatgpt-dev"
