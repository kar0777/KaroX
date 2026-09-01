from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from _support import initialize_git_repository
from karox.desktop_apps import Window, _windows
from karox.hosted_tools_runtime import BROWSER_COMMAND, HostedToolsRuntime
from karox.models import AccessProfile, Origin, OriginKind
from karox.sessions import SessionStore


def _payload(result):
    return result if isinstance(result, dict) else result.structuredContent


def test_hosted_browser_command_can_opt_in_and_attach_desktop_app(
    tmp_path: Path, monkeypatch
) -> None:
    repository = tmp_path / "repo"
    initialize_git_repository(repository)
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("KAROX_VNEXT_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(runtime_dir))

    sessions = SessionStore(tmp_path / "sessions")
    sessions.create(
        repository,
        "desktop hosted integration",
        AccessProfile.ELEVATED,
        session_id="desktop-hosted",
    )
    runtime = HostedToolsRuntime(
        repository,
        sessions,
        "desktop-hosted",
        (BROWSER_COMMAND,),
        access_profile=AccessProfile.ELEVATED,
        hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "desktop-hosted-test"),
    )
    traycer = Window(1001, "Traycer - aqurium", (10, 20, 1210, 820), 42, False)

    with mock.patch("karox.desktop_apps._windows", return_value=[traycer]):
        discovered = _payload(
            runtime.execute(
                BROWSER_COMMAND,
                {"action": "app.discover", "payload": {"user_confirmed": True}},
                deadline_seconds=5,
            )
        )
        attached = _payload(
            runtime.execute(
                BROWSER_COMMAND,
                {
                    "action": "app.attach",
                    "payload": {
                        "app_id": "traycer",
                        "access": "observe",
                        "user_confirmed": True,
                    },
                },
                deadline_seconds=5,
            )
        )
        status = _payload(
            runtime.execute(
                BROWSER_COMMAND,
                {"action": "app.status", "payload": {}},
                deadline_seconds=5,
            )
        )

    assert discovered["ok"] is True
    assert discovered["action"] == "app.discover"
    assert discovered["windows"][0]["process_id"] == 42
    assert attached["ok"] is True
    assert attached["control_contract"]["focus_steal_blocked"] is True
    assert status["bindings"][0]["app_id"] == "traycer"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only live desktop integration")
def test_live_hosted_runtime_can_observe_attach_real_traycer(
    tmp_path: Path, monkeypatch
) -> None:
    if not any("traycer" in item.title.casefold() for item in _windows()):
        pytest.skip("Traycer is not currently open")

    repository = tmp_path / "repo"
    initialize_git_repository(repository)
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("KAROX_VNEXT_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(runtime_dir))
    sessions = SessionStore(tmp_path / "sessions")
    sessions.create(
        repository,
        "live desktop hosted integration",
        AccessProfile.ELEVATED,
        session_id="desktop-hosted-live",
    )
    runtime = HostedToolsRuntime(
        repository,
        sessions,
        "desktop-hosted-live",
        (BROWSER_COMMAND,),
        access_profile=AccessProfile.ELEVATED,
        hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "desktop-hosted-live-test"),
    )

    attached = _payload(
        runtime.execute(
            BROWSER_COMMAND,
            {
                "action": "app.attach",
                "payload": {
                    "app_id": "traycer",
                    "access": "observe",
                    "user_confirmed": True,
                },
            },
            deadline_seconds=5,
        )
    )

    assert attached["ok"] is True
    assert attached["attached"] is True
    assert "traycer" in attached["window"]["title"].casefold()
    assert attached["control_contract"]["focus_steal_blocked"] is True
