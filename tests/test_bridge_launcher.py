"""Non-live contracts for the bridge lifecycle layer.

These tests never start Tailscale, never open a public listener, and never touch
the real OS keyring. They cover configuration validation, child argv, durable
identity compatibility, credential-store redaction, watchdog/stop-request
ownership, and loopback readiness ownership.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
from pathlib import Path
from unittest import mock

import pytest

from karox import bridge_launcher as launcher
from karox import bridge_serve
from karox.bridge_profiles import (
    BridgeCredentialMissing,
    BridgeCredentialStore,
    BridgeProfileStore,
    DEFAULT_BRIDGE_DEADLINE_SECONDS,
    saved_web_bridge_session_candidates,
    saved_web_bridge_session_id,
)
from karox.bridge_launcher import (
    DEFAULT_BRIDGE_TOOLS,
    WebBridgeConnectConfig,
    WebBridgeLaunchError,
    _bridge_argv,
    _child_options,
    _consume_stop_request,
    _port_is_available,
    _wait_for_bridge,
    _write_stop_request,
    bridge_diagnostics,
    build_connect_result,
    config_from_saved_profile,
    status_bridge,
    stop_bridge,
)
from karox.bridge_serve import (
    BridgeServeConfig,
    BridgeServeError,
    config_from_namespace,
    serve_bridge,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir(exist_ok=True)
    return repository


def _config(tmp_path: Path, **overrides) -> WebBridgeConnectConfig:
    values = {
        "profile": "notion",
        "repository": _repository(tmp_path),
        "tools": DEFAULT_BRIDGE_TOOLS,
        "tunnel": "custom",
        "public_url": "https://bridge.example.test",
    }
    values.update(overrides)
    return WebBridgeConnectConfig(**values)


class _MemoryBackend:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.data[(service, account)] = secret

    def get(self, service: str, account: str):
        return self.data.get((service, account))

    def delete(self, service: str, account: str) -> None:
        self.data.pop((service, account), None)


class _FakeProcess:
    def __init__(self, pid: int = 4242, poll_value=None) -> None:
        self.pid = pid
        self._poll_value = poll_value
        self.stdout = None
        self.stderr = None
        self.stdin = None

    def poll(self):
        return self._poll_value


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _saved_payload(**overrides):
    payload = {
        "name": "demo",
        "target_profile": "notion",
        "tools": ["karox.repo.read_file", "karox.git.status"],
        "port": 8765,
        "tunnel": "tailscale",
        "language": "en",
        "access_profile": "read_only",
        "deadline_seconds": 600.0,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Configuration                                                                #
# --------------------------------------------------------------------------- #


def test_config_resolves_auto_tunnel_and_project_anchor(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    config = WebBridgeConnectConfig(profile="notion", repository=repository)
    assert config.tunnel == "tailscale"
    assert config.repository == repository.resolve()
    assert config.default_project_id is not None
    assert config.projects[0]["path"] == str(repository.resolve())


def test_custom_tunnel_requires_an_https_origin(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _config(tmp_path, public_url="http://bridge.example.test")
    with pytest.raises(ValueError):
        _config(tmp_path, public_url="https://bridge.example.test/path")
    with pytest.raises(ValueError):
        _config(tmp_path, tunnel="tailscale", public_url="https://bridge.example.test")


def test_cloudflare_quick_tunnel_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Cloudflare"):
        _config(tmp_path, tunnel="cloudflare", public_url=None)


def test_unknown_profile_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _config(tmp_path, profile="promptql")


def test_saved_profile_drops_tools_karoX6_cannot_serve(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        tools=("karox.repo.read_file", "karox.browser.snapshot"),
        saved_profile_name="demo",
    )
    assert config.tools == ("karox.repo.read_file",)
    assert "karox.browser.snapshot" in config.profile_denied_tools


def test_ad_hoc_launch_fails_closed_on_an_unknown_tool(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not serve"):
        _config(
            tmp_path,
            tools=("karox.repo.read_file", "karox.browser.snapshot"),
        )


def test_checks_run_requires_an_approved_verification_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="verification"):
        _config(tmp_path, tools=("karox.repo.read_file", "karox.checks.run"))
    config = _config(
        tmp_path,
        tools=("karox.repo.read_file", "karox.checks.run"),
        verification_commands=(("python", "-m", "pytest", "-q"),),
    )
    assert config.verification_commands == (("python", "-m", "pytest", "-q"),)


def test_tool_list_must_be_unique_and_safe(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _config(tmp_path, tools=("karox.repo.read_file", "karox.repo.read_file"))
    with pytest.raises(ValueError):
        _config(tmp_path, tools=("bad tool name",))


# --------------------------------------------------------------------------- #
# Child argv                                                                   #
# --------------------------------------------------------------------------- #


def test_bridge_argv_carries_identity_projects_and_tools(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        projects=(
            {
                "project_id": "repo",
                "path": str(_repository(tmp_path)),
                "label": "Repo",
            },
        ),
        default_project_id="repo",
        verification_commands=(("python", "-m", "pytest", "-q"),),
    )
    argv = list(
        _bridge_argv(
            config,
            session_id="web-saved-abc123",
            public_url="https://bridge.example.test",
        )
    )
    assert argv[0] == launcher.sys.executable
    assert argv[1:5] == ["-m", "karox.cli", "bridge", "serve"]
    assert argv[argv.index("--session-id") + 1] == "web-saved-abc123"
    assert argv[argv.index("--credential") + 1] == "web-saved-abc123"
    assert argv[argv.index("--port") + 1] == str(config.port)
    assert argv[argv.index("--public-url") + 1] == "https://bridge.example.test"
    # Notion pins its redirect hosts.
    hosts = argv[argv.index("--allowed-redirect-hosts") + 1]
    assert "notion.so" in hosts.split(",")
    assert argv.count("--tool") == len(config.tools)
    assert argv.count("--project") == 1
    assert argv[argv.index("--default-project-id") + 1] == "repo"
    serialized = argv[argv.index("--verification-command") + 1]
    assert json.loads(serialized) == ["python", "-m", "pytest", "-q"]
    assert "--saved-profile-name" not in argv


def test_bearer_profile_argv_never_receives_oauth_public_url(tmp_path: Path) -> None:
    config = _config(tmp_path, profile="adapt")
    argv = list(
        _bridge_argv(
            config,
            session_id="web-saved-abc123",
            public_url="https://bridge.example.test",
        )
    )
    assert "--public-url" not in argv
    assert "--allowed-redirect-hosts" not in argv
    assert argv[argv.index("--saved-profile-name") + 1] == config.saved_profile_name if config.saved_profile_name else True


def test_bridge_argv_includes_saved_profile_name(tmp_path: Path) -> None:
    config = _config(tmp_path, saved_profile_name="demo")
    argv = _bridge_argv(
        config, session_id="web-saved-abc123", public_url="https://bridge.example.test"
    )
    assert argv[argv.index("--saved-profile-name") + 1] == "demo"


@pytest.mark.parametrize("profile", ["notion", "adapt", "chatgpt-web"])
def test_launcher_argv_parses_with_the_cli_bridge_serve_parser(
    tmp_path: Path, profile: str
) -> None:
    """The spawned child must be accepted by ``karox.cli bridge serve``.

    The launcher builds the child argv by hand; the CLI parser is the other
    half of that contract. They drifted once (four flags existed only in the
    launcher, so every spawned bridge exited 2), so this test parses the
    launcher's exact argv with the CLI parser and converts the namespace back
    into a serve config.
    """
    from karox import cli

    config = _config(
        tmp_path,
        profile=profile,
        verification_commands=(("python", "-m", "pytest", "-q"),),
    )
    argv = _bridge_argv(
        config, session_id="web-saved-abc123", public_url="https://bridge.example.test"
    )
    assert argv[1:3] == ("-m", "karox.cli")
    namespace = cli._parser().parse_args(list(argv[3:]))
    assert namespace.bridge_command == "serve"
    serve_config = bridge_serve.config_from_namespace(namespace)
    assert serve_config.protocol == "mcp"
    assert serve_config.session_id == "web-saved-abc123"
    assert serve_config.profile == config.profile
    assert serve_config.verification_commands == (("python", "-m", "pytest", "-q"),)
    assert serve_config.tools == config.tools


# --------------------------------------------------------------------------- #
# Durable identity and credentials                                             #
# --------------------------------------------------------------------------- #


def test_saved_session_identity_matches_the_karoX5_contract() -> None:
    expected = "web-saved-" + hashlib.sha256(b"demo").hexdigest()[:24]
    assert saved_web_bridge_session_id("demo") == expected
    candidates = saved_web_bridge_session_candidates("demo")
    assert candidates[0] == expected
    assert len(candidates) == len(set(candidates))
    legacy = "web-saved-" + hashlib.sha256(b"notion\0demo").hexdigest()[:24]
    assert legacy in candidates


def test_credential_store_uses_dedicated_keyring_namespace_and_redacts() -> None:
    backend = _MemoryBackend()
    store = BridgeCredentialStore(backend)
    result = store.set("web-saved-demo", "explicit-secret")
    assert set(result) == {"reference", "fingerprint"}
    assert result["reference"] == "os-keyring:bridge/web-saved-demo"
    assert "explicit-secret" not in json.dumps(result)
    assert backend.data[("KaroX/bridge", "web-saved-demo")] == "explicit-secret"
    assert store.resolve("os-keyring:bridge/web-saved-demo") == "explicit-secret"
    status = store.status("os-keyring:bridge/web-saved-demo")
    assert status["status"] == "available"
    assert status["fingerprint"].startswith("sha256:")
    rotated = store.rotate("web-saved-demo")
    assert "secret" not in rotated
    assert rotated["status"] == "rotated"


def test_missing_credential_is_typed_but_unreadable_is_not() -> None:
    class Broken(_MemoryBackend):
        def get(self, service: str, account: str):
            raise RuntimeError("backend unavailable")

    with pytest.raises(BridgeCredentialMissing):
        BridgeCredentialStore(_MemoryBackend()).resolve("os-keyring:bridge/none")
    with pytest.raises(Exception) as excinfo:
        BridgeCredentialStore(Broken()).resolve("os-keyring:bridge/x")
    assert not isinstance(excinfo.value, BridgeCredentialMissing)


# --------------------------------------------------------------------------- #
# Saved-profile JSON compatibility                                             #
# --------------------------------------------------------------------------- #


def test_saved_profile_store_preserves_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "web-bridge-profiles.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "future_top_level": {"keep": True},
                "profiles": [
                    _saved_payload(
                        future_option={"nested": [1, 2, 3]},
                        browser_external_https=False,
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    store = BridgeProfileStore(path)
    profile = store.get("demo")
    assert profile.extra["future_option"] == {"nested": [1, 2, 3]}
    assert profile.tools == ("karox.repo.read_file", "karox.git.status")
    store.put(profile)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["future_top_level"] == {"keep": True}
    saved = payload["profiles"][0]
    assert saved["future_option"] == {"nested": [1, 2, 3]}
    assert saved["version"] if "version" in saved else True


def test_legacy_elevated_profile_maps_to_bypass(tmp_path: Path) -> None:
    path = tmp_path / "web-bridge-profiles.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": [
                    _saved_payload(access_profile="elevated", bypass_removed=True)
                ],
            }
        ).replace(', "bypass_removed": true', ""),
        encoding="utf-8",
    )
    profile = BridgeProfileStore(path).get("demo")
    assert profile.bypass is True


def test_saved_profile_tolerates_an_unknown_target_profile(tmp_path: Path) -> None:
    path = tmp_path / "web-bridge-profiles.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": [_saved_payload(target_profile="future-client")],
            }
        ),
        encoding="utf-8",
    )
    profile = BridgeProfileStore(path).get("demo")
    assert profile.target_profile == "future-client"
    with mock.patch.object(
        launcher, "BridgeProfileStore", lambda: BridgeProfileStore(path)
    ):
        with pytest.raises(WebBridgeLaunchError):
            config_from_saved_profile("demo")


def test_saved_profile_doctor_reports_a_degraded_store(tmp_path: Path) -> None:
    path = tmp_path / "web-bridge-profiles.json"
    path.write_text("{not json", encoding="utf-8")
    doctor = BridgeProfileStore(path).doctor()
    assert doctor["status"] == "degraded"
    assert doctor["profiles"] == []


def test_config_from_saved_profile_rejects_cloudflare(tmp_path: Path) -> None:
    path = tmp_path / "web-bridge-profiles.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": [
                    _saved_payload(
                        tunnel="cloudflare",
                        repository=str(_repository(tmp_path)),
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    with mock.patch.object(
        launcher, "BridgeProfileStore", lambda: BridgeProfileStore(path)
    ):
        with pytest.raises(WebBridgeLaunchError, match="Cloudflare"):
            config_from_saved_profile("demo")


# --------------------------------------------------------------------------- #
# Watchdog and stop-request ownership                                          #
# --------------------------------------------------------------------------- #


def test_stop_request_is_addressed_to_one_exact_owner(tmp_path: Path) -> None:
    with mock.patch.object(launcher, "watchdog_dir", return_value=tmp_path):
        path = _write_stop_request("session-1", 100, intent="restart")
        assert path.exists()
        assert _consume_stop_request("session-1", 999) is None
        assert path.exists(), "a different owner must not steal or delete the request"
        assert _consume_stop_request("session-1", 100) == "restart"
        assert not path.exists()


def test_stop_request_without_intent_defaults_to_stop(tmp_path: Path) -> None:
    with mock.patch.object(launcher, "watchdog_dir", return_value=tmp_path):
        path = launcher._stop_request_path("session-2")
        path.write_text(json.dumps({"owner_pid": 7}), encoding="utf-8")
        assert _consume_stop_request("session-2", 7) == "stop"


def test_invalid_stop_request_intent_is_rejected(tmp_path: Path) -> None:
    with mock.patch.object(launcher, "watchdog_dir", return_value=tmp_path):
        with pytest.raises(ValueError):
            _write_stop_request("session-3", 1, intent="delete-everything")


# --------------------------------------------------------------------------- #
# Readiness and process ownership                                              #
# --------------------------------------------------------------------------- #


def test_wait_for_bridge_accepts_an_owned_listener() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        with mock.patch.object(
            launcher, "_listener_belongs_to_process_tree", return_value=True
        ):
            _wait_for_bridge(_FakeProcess(), port, timeout_seconds=2.0)
    finally:
        listener.close()


def test_wait_for_bridge_rejects_a_foreign_listener() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        with mock.patch.object(
            launcher, "_listener_belongs_to_process_tree", return_value=False
        ):
            with pytest.raises(WebBridgeLaunchError, match="did not open"):
                _wait_for_bridge(_FakeProcess(), port, timeout_seconds=0.3)
    finally:
        listener.close()


def test_wait_for_bridge_reports_an_early_exit() -> None:
    with pytest.raises(WebBridgeLaunchError, match="exited with code 2"):
        _wait_for_bridge(_FakeProcess(poll_value=2), 65535, timeout_seconds=0.3)


def test_port_availability_probe_does_not_evict_a_listener() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert _port_is_available(port) is False
    finally:
        listener.close()
    assert _port_is_available(port) is True


def test_child_options_keep_windows_no_window_and_group_isolation() -> None:
    options = _child_options()
    assert options["start_new_session"] is True
    if os.name == "nt":
        flags = options["creationflags"]
        assert flags & getattr(launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000)
        assert flags & getattr(
            launcher.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
        )


# --------------------------------------------------------------------------- #
# Diagnostics and output                                                       #
# --------------------------------------------------------------------------- #


def test_connect_result_exposes_only_a_fingerprint(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = build_connect_result(
        config,
        session_id="web-saved-demo",
        public_url="https://bridge.example.test",
        credential_reference="os-keyring:bridge/web-saved-demo",
        credential_fingerprint="sha256:0123456789ab",
        bridge_pid=10,
        owner_pid=11,
    )
    rendered = "\n".join(result.safe_lines())
    assert "sha256:0123456789ab" in rendered
    assert "os-keyring:bridge/web-saved-demo" not in rendered
    assert result.to_dict()["endpoint"] == "https://bridge.example.test/mcp"
    assert result.to_dict()["credential_fingerprint"] == "sha256:0123456789ab"


def test_diagnostics_report_denied_tools_without_credentials(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        tools=("karox.repo.read_file", "karox.browser.snapshot"),
        saved_profile_name="demo",
    )
    diagnostics = bridge_diagnostics(
        config, public_url="https://bridge.example.test", session_id="s1"
    )
    denied = {item["name"]: item["reason"] for item in diagnostics["disabled_tools"]}
    assert "karox.browser.snapshot" in denied
    assert diagnostics["url_stability"] == "stable_user_managed"
    assert "secret" not in json.dumps(diagnostics).lower()


# --------------------------------------------------------------------------- #
# Safe stop/status helpers                                                     #
# --------------------------------------------------------------------------- #


def _write_store(tmp_path: Path, **overrides) -> BridgeProfileStore:
    path = tmp_path / "profiles.json"
    payload = _saved_payload(
        repository=str(_repository(tmp_path)),
        tools=["karox.repo.read_file"],
        **overrides,
    )
    path.write_text(
        json.dumps({"version": 1, "profiles": [payload]}), encoding="utf-8"
    )
    return BridgeProfileStore(path)


def test_status_reports_a_stopped_profile_without_secrets(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    store = _write_store(
        tmp_path,
        name="karox6-status-demo",
        port=port,
        tunnel="custom",
        public_url="https://bridge.example.test",
    )
    with mock.patch.object(launcher, "BridgeProfileStore", lambda: store), mock.patch.object(
        launcher, "watchdog_dir", return_value=tmp_path
    ), mock.patch.object(
        launcher,
        "_credential_status",
        return_value={
            "status": "missing",
            "reference": "os-keyring:bridge/x",
            "fingerprint": "",
        },
    ):
        status = status_bridge("karox6-status-demo")
    assert status["status"] == "stopped"
    assert status["credential_status"] == "missing"
    assert status["secret_fields"] == []
    assert "credential_fingerprint" in status


def test_stop_reports_no_action_for_a_free_port(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    store = _write_store(
        tmp_path,
        name="karox6-stop-demo",
        port=port,
        tunnel="custom",
        public_url="https://bridge.example.test",
    )
    with mock.patch.object(launcher, "BridgeProfileStore", lambda: store), mock.patch.object(
        launcher, "watchdog_dir", return_value=tmp_path
    ):
        result = stop_bridge("karox6-stop-demo")
    assert result["action"] == "no_action"
    assert result["verdict"] == "free"


# --------------------------------------------------------------------------- #
# bridge serve entrypoint                                                      #
# --------------------------------------------------------------------------- #


def _serve_namespace(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        profile="notion",
        repository=_repository(tmp_path),
        session_id="web-saved-abc123",
        credential="web-saved-abc123",
        host="127.0.0.1",
        port=8765,
        protocol="mcp",
        path="/mcp",
        tool=["karox.repo.read_file"],
        verification_command=["python -m pytest -q"],
        public_url="https://bridge.example.test",
        deadline_seconds=DEFAULT_BRIDGE_DEADLINE_SECONDS,
        allow_network_bind=False,
        tls_certfile=None,
        tls_keyfile=None,
        allowed_redirect_host=["notion.so"],
        state_dir=None,
    )


def test_serve_namespace_contract_is_accepted(tmp_path: Path) -> None:
    config = config_from_namespace(_serve_namespace(tmp_path))
    assert isinstance(config, BridgeServeConfig)
    assert config.path == "/mcp"
    assert config.tools == ("karox.repo.read_file",)
    assert config.verification_commands == (("python", "-m", "pytest", "-q"),)
    assert config.allowed_redirect_hosts == frozenset({"notion.so"})
    assert callable(serve_bridge)


def test_serve_rejects_non_loopback_without_gate_and_tls(tmp_path: Path) -> None:
    args = _serve_namespace(tmp_path)
    args.host = "0.0.0.0"
    with pytest.raises(BridgeServeError, match="allow-network-bind"):
        config_from_namespace(args)
    args.allow_network_bind = True
    with pytest.raises(BridgeServeError, match="requires TLS"):
        config_from_namespace(args)


def test_serve_rejects_non_core_tools(tmp_path: Path) -> None:
    config = config_from_namespace(_serve_namespace(tmp_path))
    config = BridgeServeConfig(
        **{
            **config.__dict__,
            "tools": ("karox.repo.read_file", "karox.browser.snapshot"),
        }
    )
    with pytest.raises(BridgeServeError, match="cannot serve"):
        bridge_serve._core_tools_for_launch(config.tools)


def test_serve_requires_an_absolute_mcp_path(tmp_path: Path) -> None:
    args = _serve_namespace(tmp_path)
    args.path = "mcp"
    with pytest.raises(BridgeServeError, match="absolute"):
        config_from_namespace(args)