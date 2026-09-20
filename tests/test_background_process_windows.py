"""Mocked Windows flags at real background spawn seams (not GUI evidence)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from karox import check_jobs, core, detached_process, hosted_tools_runtime
from test_hosted_tools_runtime import _Base
from karox.hosted_tools_runtime import DEV_SERVER_START

NO_WINDOW = 0x08000000
NEW_GROUP = 0x00000200
BREAKAWAY = 0x01000000
DETACHED = 0x00000008


def assert_hidden_group(flags):
    assert flags & NO_WINDOW
    assert flags & NEW_GROUP
    # NO_WINDOW is ignored when either of these bits is combined with it.
    assert not flags & DETACHED
    assert not flags & 0x00000010  # CREATE_NEW_CONSOLE


def test_saved_owner_and_supervisor_fallback_stay_windowless(monkeypatch):
    monkeypatch.setattr(detached_process, "os", SimpleNamespace(name="nt"))
    popen = Mock(side_effect=[PermissionError("job forbids breakaway"), SimpleNamespace(pid=123)])
    monkeypatch.setattr(detached_process.subprocess, "Popen", popen)
    process, mechanism = detached_process.spawn_detached(["python", "-m", "karox.saved_bridge_supervisor"])
    assert process.pid == 123
    assert mechanism.startswith("in_caller_job")
    assert len(popen.call_args_list) == 2
    for call in popen.call_args_list:
        assert_hidden_group(call.kwargs["creationflags"])
        assert call.kwargs["stdin"] == subprocess.DEVNULL
        assert not call.kwargs["start_new_session"]
    assert popen.call_args_list[0].kwargs["creationflags"] ^ popen.call_args_list[1].kwargs["creationflags"] == BREAKAWAY


@pytest.mark.parametrize("success", [True, False])
@pytest.mark.parametrize("launcher", [check_jobs.developer_worker_launcher, check_jobs._default_worker_launcher])
def test_check_worker_never_drops_windowless_flags_to_retry(monkeypatch, tmp_path, success, launcher):
    monkeypatch.setattr(check_jobs, "os", SimpleNamespace(name="nt", environ=os.environ, pathsep=os.pathsep))
    child = SimpleNamespace(pid=123)
    popen = Mock(side_effect=[PermissionError("job"), child if success else OSError("failed")])
    monkeypatch.setattr(check_jobs.subprocess, "Popen", popen)
    if success:
        assert launcher(tmp_path / "job.json") is child
    else:
        with pytest.raises(OSError):
            launcher(tmp_path / "job.json")
    assert len(popen.call_args_list) == 2
    for call in popen.call_args_list:
        assert_hidden_group(call.kwargs["creationflags"])
        assert call.kwargs["shell"] is False


class TestHostedBackgroundLaunch(_Base):
    def test_production_managed_server_spawn_uses_shared_hidden_process_group(self):
        runtime = self._runtime(tools=(DEV_SERVER_START,))
        # Fail at the actual Popen seam, before registry/identity capture. No
        # fake popen_factory branch and no user command or interactive UI runs.
        with (
            patch.object(core, "os", SimpleNamespace(name="nt")),
            patch.object(hosted_tools_runtime, "_resolve_process_argv", side_effect=lambda argv: argv),
            patch.object(subprocess, "Popen", side_effect=OSError("spawn test")) as popen,
        ):
            result = runtime.execute(
                DEV_SERVER_START, {"argv": ["npm", "run", "start:safe"]}, deadline_seconds=10,
            )
        assert result.isError
        popen.assert_called_once()
        assert_hidden_group(popen.call_args.kwargs["creationflags"])
        assert popen.call_args.kwargs["shell"] is False
        assert Path(popen.call_args.kwargs["cwd"]) == self.repository
        assert "start_new_session" not in popen.call_args.kwargs


def test_ellipsis_git_preflight_uses_shared_hidden_group(monkeypatch, tmp_path):
    from karox import ellipsis_helpers
    monkeypatch.setattr(core, "os", SimpleNamespace(name="nt"))
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="main\n"))
    monkeypatch.setattr(ellipsis_helpers.subprocess, "run", run)
    assert ellipsis_helpers.git_text(tmp_path, ["branch", "--show-current"]) == "main"
    assert_hidden_group(run.call_args.kwargs["creationflags"])
    assert run.call_args.args[0] == ["git", "branch", "--show-current"]
