"""Windows cancellation regressions; mocked Win32 calls, not GUI evidence."""

from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from karox import core


@pytest.fixture
def windows(monkeypatch):
    platform = SimpleNamespace(name="nt", kill=Mock(), killpg=Mock())
    monkeypatch.setattr(core, "os", platform)
    return platform


def owned_tree(monkeypatch, *, job=456):
    # Constructor normally assigns a Win32 job. Keep an owned Popen instance
    # and model an already-assigned handle so API loss occurs at termination.
    monkeypatch.setattr(core, "_windows_job_api", lambda: None)
    child = Mock(pid=123)
    tree = core.ProcessTree(child)
    tree._job = job
    return tree, child


def test_taskkill_uses_shared_hidden_process_options(monkeypatch, windows):
    shared_options = Mock(wraps=core._new_process_group_kwargs)
    monkeypatch.setattr(core, "_new_process_group_kwargs", shared_options)
    run = Mock()
    monkeypatch.setattr(core.subprocess, "run", run)

    core._kill_windows_process_tree(123)

    shared_options.assert_called_once_with()
    run.assert_called_once_with(
        ["taskkill", "/F", "/T", "/PID", "123"],
        stdout=core.subprocess.DEVNULL,
        stderr=core.subprocess.DEVNULL,
        timeout=10.0,
        check=False,
        creationflags=0x08000000 | 0x00000200,
    )
    flags = run.call_args.kwargs["creationflags"]
    assert not flags & (0x00000008 | 0x00000010)  # detached / new console


@pytest.mark.parametrize("job_result", ["false", "missing", "error", "no_job"])
def test_job_failure_falls_back_to_owned_tree(monkeypatch, windows, job_result):
    tree, child = owned_tree(monkeypatch, job=None if job_result == "no_job" else 456)
    api = Mock()
    api.TerminateJobObject.return_value = False
    if job_result == "error":
        api.TerminateJobObject.side_effect = OSError("job unavailable")
    monkeypatch.setattr(
        core, "_windows_job_api",
        lambda: None if job_result == "missing" else (api, None),
    )
    fallback = Mock()
    monkeypatch.setattr(core, "_kill_windows_process_tree", fallback)
    order = Mock()
    order.attach_mock(fallback, "tree")
    order.attach_mock(child.kill, "kill")
    order.attach_mock(child.wait, "wait")

    tree.terminate()

    assert order.mock_calls == [
        call.tree(123), call.kill(), call.wait(timeout=tree.KILL_GRACE_SECONDS),
    ]
    if job_result in {"false", "error"}:
        api.TerminateJobObject.assert_called_once_with(456, 1)
    else:
        api.TerminateJobObject.assert_not_called()
    child.send_signal.assert_not_called()
    windows.kill.assert_not_called()
    windows.killpg.assert_not_called()


def test_successful_job_termination_skips_fallback(monkeypatch, windows):
    tree, child = owned_tree(monkeypatch)
    api = Mock()
    api.TerminateJobObject.return_value = True
    monkeypatch.setattr(core, "_windows_job_api", lambda: (api, None))
    fallback = Mock()
    monkeypatch.setattr(core, "_kill_windows_process_tree", fallback)

    tree.terminate()

    api.TerminateJobObject.assert_called_once_with(456, 1)
    fallback.assert_not_called()
    child.kill.assert_called_once_with()
    child.wait.assert_called_once_with(timeout=tree.KILL_GRACE_SECONDS)
    child.send_signal.assert_not_called()
    windows.kill.assert_not_called()
    windows.killpg.assert_not_called()


@pytest.mark.parametrize("failure", ["exit_status", "spawn", "timeout"])
@pytest.mark.parametrize("child_gone", [False, True])
def test_failed_taskkill_still_kills_and_reaps_direct_child(
    monkeypatch, windows, failure, child_gone,
):
    tree, child = owned_tree(monkeypatch)
    # The assigned job API has disappeared; exercise the real taskkill helper.
    run = Mock(return_value=SimpleNamespace(returncode=1))
    if failure == "spawn":
        run.side_effect = OSError("taskkill unavailable")
    elif failure == "timeout":
        run.side_effect = core.subprocess.TimeoutExpired("taskkill", 10.0)
    monkeypatch.setattr(core.subprocess, "run", run)
    if child_gone:
        child.kill.side_effect = ProcessLookupError("already exited")
        child.wait.side_effect = core.subprocess.TimeoutExpired("child", 5.0)
    order = Mock()
    order.attach_mock(run, "taskkill")
    order.attach_mock(child.kill, "kill")
    order.attach_mock(child.wait, "wait")

    tree.terminate()

    run.assert_called_once()
    assert run.call_args.args[0] == ["taskkill", "/F", "/T", "/PID", "123"]
    assert run.call_args.kwargs["creationflags"] == 0x08000000 | 0x00000200
    assert [entry[0] for entry in order.mock_calls] == ["taskkill", "kill", "wait"]
    child.kill.assert_called_once_with()
    child.wait.assert_called_once_with(timeout=tree.KILL_GRACE_SECONDS)
    child.send_signal.assert_not_called()
    windows.kill.assert_not_called()
    windows.killpg.assert_not_called()
