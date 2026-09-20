"""Orphan recovery uses a process instance, not a port/PID kill shortcut."""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from karox import port_ownership as ports
from karox import saved_bridge_recovery as recovery
from karox import saved_bridge_supervisor as supervisor
from karox.process_identity import ProcessIdentity, argv_digest

PID = 424242
PORT = 8765
SESSION = "web-saved-orphan-test"
PROFILE = "orphan-test"
ARGV = [sys.executable, "-m", "karox.cli", "bridge", "serve", "--session-id", SESSION,
        "--port", str(PORT)]


def verdict(argv=None, **kwargs):
    return ports.OwnershipVerdict(
        ports.OWNERSHIP_STALE_OWNED, "owned orphan", ports._empty_metadata(),
        owned_orphan_pid=PID,
        owned_orphan_identity=ProcessIdentity(PID, 101, argv_sha256=argv_digest(argv or ARGV)),
        **kwargs,
    )


@pytest.fixture
def owned(monkeypatch):
    process = Mock(pid=PID)
    process.cmdline.return_value = ARGV.copy()
    process.exe.return_value = sys.executable
    process.username.return_value = "test-account"
    process.parents.return_value = []
    process.is_running.return_value = True
    caller = Mock()
    caller.username.return_value = "test-account"
    monkeypatch.setattr(recovery.psutil, "Process", lambda pid: caller if pid == os.getpid() else process)
    monkeypatch.setattr(recovery, "read_process_create_time_ns", lambda pid: 101)
    monkeypatch.setattr(recovery, "saved_web_bridge_session_candidates", lambda name: (SESSION,))
    monkeypatch.setattr(recovery, "_port_owning_pid", lambda port: PID)
    monkeypatch.setattr(recovery, "_port_available", lambda port: True)
    current = Mock(return_value=verdict())
    monkeypatch.setattr(recovery, "check_port_ownership", current)
    return process, current


def reclaim(ownership=None):
    return recovery.reclaim_saved_bridge_orphan(PROFILE, port=PORT, ownership=ownership or verdict())


def test_owned_orphan_revalidates_then_terminates_only_one_instance(owned):
    process, current = owned
    assert reclaim()
    current.assert_called_once_with(PROFILE, port=PORT)
    process.terminate.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=recovery.ORPHAN_RECLAIM_TIMEOUT_SECONDS)
    process.kill.assert_not_called()
    process.children.assert_not_called()


@pytest.mark.parametrize("snapshot", [
    replace(verdict(), owned_orphan_identity=None),
    replace(verdict(), owned_orphan_identity=ProcessIdentity(PID)),
    replace(verdict(), live_unrecorded_owner_pid=900),
    replace(verdict(), verdict=ports.OWNERSHIP_REUSE_SAME),
    replace(verdict(), owned_orphan_pid=900),
])
def test_missing_evidence_or_live_owner_never_signalled(owned, snapshot):
    assert not reclaim(snapshot)
    owned[0].terminate.assert_not_called()


@pytest.mark.parametrize("created", [None, 102])
def test_creation_time_unavailable_or_pid_reused(owned, monkeypatch, created):
    monkeypatch.setattr(recovery, "read_process_create_time_ns", lambda pid: created)
    assert not reclaim()
    owned[0].terminate.assert_not_called()


def test_pid_reuse_with_identical_argv_after_initial_verdict(owned):
    owned[1].return_value = replace(verdict(), owned_orphan_identity=ProcessIdentity(
        PID, 102, argv_sha256=argv_digest(ARGV)))
    assert not reclaim()
    owned[0].terminate.assert_not_called()


def test_argv_changes_at_signal_boundary(owned):
    owned[0].cmdline.side_effect = [ARGV, [*ARGV, "--changed"]]
    assert not reclaim()
    owned[0].terminate.assert_not_called()


def test_live_owner_reappears_during_validation(owned):
    owned[1].return_value = replace(verdict(), verdict=ports.OWNERSHIP_REUSE_SAME)
    assert not reclaim()
    owned[0].terminate.assert_not_called()


def test_other_account_cannot_be_claimed_by_session_identifier(owned):
    owned[0].username.return_value = "other-account"
    assert not reclaim()
    owned[0].terminate.assert_not_called()


@pytest.mark.parametrize("argv", [
    [sys.executable, "-c", "pass", "karox", "bridge", "serve", "--session-id", SESSION],
    ["not-karox.exe", "bridge", "serve", "--session-id", SESSION],
    [sys.executable, "-m", "karox.cli", "bridge", "status", "serve", "--session-id", SESSION],
    [*ARGV, "--session-id=another-session"],
])
def test_incidental_or_ambiguous_argv_is_not_identity(argv):
    assert ports.prove_bridge_process_identity(PID, (SESSION,), command_line=argv) is None


@pytest.mark.parametrize("changed", ["--port", "--session-id"])
def test_same_pid_on_other_port_or_session_is_not_reclaimed(owned, changed):
    argv = ARGV.copy()
    argv[argv.index(changed) + 1] = "9999"
    owned[0].cmdline.return_value = argv
    assert not reclaim(verdict(argv))
    owned[0].terminate.assert_not_called()


@pytest.mark.parametrize("argv", [[], [sys.executable, "-m", "karox.cli", "bridge", "connect",
                                      "--saved", PROFILE]])
def test_unreadable_or_live_owner_parent_fails_closed(owned, argv):
    parent = Mock(pid=777)
    parent.cmdline.return_value = argv
    owned[0].parents.return_value = [parent]
    assert not reclaim()
    owned[0].terminate.assert_not_called()


def test_access_denied_is_not_owner_absence(owned):
    owned[0].parents.side_effect = psutil.AccessDenied(PID)
    assert not reclaim()
    owned[0].terminate.assert_not_called()


def test_port_taken_by_other_instance_during_validation(owned, monkeypatch):
    monkeypatch.setattr(recovery, "_port_owning_pid", lambda port: PID + 1)
    assert not reclaim()
    owned[0].terminate.assert_not_called()


def test_timeout_never_escalates_to_unchecked_pid_kill(owned):
    owned[0].wait.side_effect = psutil.TimeoutExpired(10, PID)
    assert not reclaim()
    owned[0].terminate.assert_called_once()
    owned[0].kill.assert_not_called()


def test_port_must_be_free_before_reporting_success(owned, monkeypatch):
    monkeypatch.setattr(recovery, "_port_available", lambda port: False)
    monkeypatch.setattr(recovery, "ORPHAN_RECLAIM_TIMEOUT_SECONDS", 0)
    assert not reclaim()


def test_port_table_ignores_non_listening_connections(monkeypatch):
    address = SimpleNamespace(port=PORT)
    monkeypatch.setattr(psutil, "net_connections", lambda **kw: [
        SimpleNamespace(status=psutil.CONN_ESTABLISHED, laddr=address, pid=99),
        SimpleNamespace(status=psutil.CONN_LISTEN, laddr=address, pid=PID),
    ])
    assert ports._port_owning_pid(PORT) == PID


def test_classifier_captures_stable_listener_not_dead_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(ports, "watchdog_dir", lambda: tmp_path)
    monkeypatch.setattr(ports, "saved_web_bridge_session_candidates", lambda name: (SESSION,))
    monkeypatch.setattr(ports, "_port_has_listener", lambda port: True)
    monkeypatch.setattr(ports, "_port_owning_pid", lambda port: PID)
    monkeypatch.setattr(ports, "_process_command_line", lambda pid: ARGV)
    monkeypatch.setattr(ports, "_live_owner_of_listener", lambda *args: None)
    reader = Mock(return_value=101)
    monkeypatch.setattr(ports, "read_process_create_time_ns", reader)
    result = ports.check_port_ownership(PROFILE, port=PORT)
    assert result.owned_orphan_identity == verdict().owned_orphan_identity
    assert not result.metadata.pid_proven
    assert result.to_dict()["owned_orphan_identity"]["create_time_ns"] == 101
    reader.side_effect = [101, 102]
    assert ports.check_port_ownership(PROFILE, port=PORT).owned_orphan_identity is None


@pytest.mark.parametrize("proven", [True, False])
def test_supervisor_reclaims_before_start_and_never_starts_if_unproven(owned, monkeypatch, proven):
    from karox import web_bridge_launcher, web_bridge_profiles
    monkeypatch.setattr(supervisor, "_read_state", lambda name: {"desired_running": True})
    monkeypatch.setattr(supervisor, "_read_restart_migration", lambda name: None)
    monkeypatch.setattr(supervisor, "_read_restart_recovery", lambda name: None)
    monkeypatch.setattr(supervisor, "_last_owner_exit", lambda name: None)
    store = Mock()
    store.get.return_value = SimpleNamespace(port=PORT)
    monkeypatch.setattr(web_bridge_profiles, "WebBridgeProfileStore", lambda: store)
    evidence = verdict() if proven else replace(verdict(), owned_orphan_identity=None)
    monkeypatch.setattr(ports, "check_port_ownership", lambda *args, **kwargs: evidence)
    def start(*args, **kwargs):
        owned[0].terminate.assert_called_once()
        return {"action": "started", "owner_pid": 901}
    launch = Mock(side_effect=start)
    monkeypatch.setattr(web_bridge_launcher, "start_saved_bridge", launch)
    result = supervisor.supervisor_tick(PROFILE)
    assert result["status"] == ("recovered" if proven else "recovery_failed")
    if not proven:
        launch.assert_not_called()
        owned[0].terminate.assert_not_called()


def test_real_arbitrary_listener_survives_recovery_attempt(monkeypatch, tmp_path):
    """OS-backed negative test: no external bridge or credential is touched."""
    monkeypatch.setattr(ports, "watchdog_dir", lambda: tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", "import socket,time; s=socket.socket(); "
         "s.bind(('127.0.0.1',0)); s.listen(); print(s.getsockname()[1],flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout is not None
        port = int(process.stdout.readline())
        ownership = ports.check_port_ownership(PROFILE, port=port)
        assert ownership.verdict == ports.OWNERSHIP_UNRELATED
        assert not recovery.reclaim_saved_bridge_orphan(PROFILE, port=port, ownership=ownership)
        assert process.poll() is None
    finally:
        process.terminate()
        process.communicate(timeout=5)


def test_live_unrecognized_launch_parent_is_not_proof_of_an_orphan(owned):
    parent = Mock(pid=777)
    parent.cmdline.return_value = ["unrecognized-owner"]
    owned[0].parents.return_value = [parent]
    assert not reclaim()
    owned[0].terminate.assert_not_called()


def test_live_python_unbuffered_owner_is_recognized(monkeypatch):
    argv = [sys.executable, "-u", "-m", "karox.cli", "bridge", "connect", "--saved", PROFILE]
    monkeypatch.setattr(ports, "_process_command_line", lambda pid: argv)
    assert ports.prove_saved_bridge_owner_identity(PID, PROFILE)


def test_recovery_must_not_terminate_its_own_caller(owned, monkeypatch):
    monkeypatch.setattr(recovery.os, "getpid", lambda: PID)
    assert not reclaim()
    owned[0].terminate.assert_not_called()


@pytest.mark.parametrize("proven,created", [(False, 101), (True, 102), (True, None)])
def test_stale_watchdog_cannot_authorize_reused_or_foreign_owner(monkeypatch, proven, created):
    from karox import web_bridge_launcher, web_bridge_profiles
    metadata = replace(ports._empty_metadata(), pid=PID, pid_proven=True, process_start_time_ns=101)
    snapshot = ports.OwnershipVerdict(ports.OWNERSHIP_REUSE_SAME, "stale heartbeat", metadata)
    monkeypatch.setattr(supervisor, "_read_state", lambda name: {"desired_running": True})
    monkeypatch.setattr(supervisor, "_read_restart_migration", lambda name: None)
    monkeypatch.setattr(supervisor, "_read_restart_recovery", lambda name: None)
    monkeypatch.setattr(supervisor, "_read_owner_heartbeat", lambda path: 1.0)
    monkeypatch.setattr(supervisor, "read_process_create_time_ns", lambda pid: created)
    store = Mock()
    store.get.return_value = SimpleNamespace(port=PORT)
    monkeypatch.setattr(web_bridge_profiles, "WebBridgeProfileStore", lambda: store)
    monkeypatch.setattr(ports, "check_port_ownership", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(ports, "prove_saved_bridge_owner_identity", lambda *args: proven)
    kill, start = Mock(), Mock()
    monkeypatch.setattr(supervisor, "_force_stop_proven_owner", kill)
    monkeypatch.setattr(web_bridge_launcher, "start_saved_bridge", start)
    assert supervisor.supervisor_tick(PROFILE)["status"] == "recovery_failed"
    kill.assert_not_called()
    start.assert_not_called()


def test_owner_tree_cleanup_refuses_shared_caller_process_group(monkeypatch):
    fake_os = SimpleNamespace(name="posix", getpid=lambda: 99, getpgid=lambda pid: pid - 1, killpg=Mock())
    monkeypatch.setattr(supervisor, "os", fake_os)
    assert not supervisor._force_stop_proven_owner(PID)
    fake_os.killpg.assert_not_called()


def test_ambiguous_listener_pids_cannot_authorize_recovery(monkeypatch):
    connections = [SimpleNamespace(status=psutil.CONN_LISTEN,
                                  laddr=SimpleNamespace(port=PORT), pid=pid)
                   for pid in (PID, PID + 1)]
    monkeypatch.setattr(psutil, "net_connections", lambda **kwargs: connections)
    assert ports._port_owning_pid(PORT) is None


def test_owner_tree_stop_rejects_reused_snapshot(monkeypatch):
    process = Mock()
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    monkeypatch.setattr(supervisor, "read_process_create_time_ns", lambda pid: 102)
    monkeypatch.setattr(supervisor, "os", SimpleNamespace(
        name="posix", getpid=lambda: 99, getpgid=lambda pid: pid))
    assert not supervisor._force_stop_proven_owner(PID, 101)
    process.children.assert_not_called()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()


def test_owner_tree_stop_uses_captured_instances_not_pid_tree_tools(monkeypatch):
    owner, child, caller = Mock(), Mock(), Mock()
    for process in (owner, child, caller):
        process.username.return_value = "account"
    owner.children.return_value = [child]
    owner.is_running.return_value = True
    monkeypatch.setattr(psutil, "Process", lambda pid: owner if pid == PID else caller)
    monkeypatch.setattr(psutil, "wait_procs", Mock(side_effect=[([], [owner, child]), ([], [])]))
    monkeypatch.setattr(supervisor, "read_process_create_time_ns", lambda pid: 101)
    monkeypatch.setattr(supervisor, "os", SimpleNamespace(name="nt", getpid=lambda: 99))
    command = Mock(side_effect=AssertionError("PID-only taskkill forbidden"))
    monkeypatch.setattr(subprocess, "run", command)
    assert supervisor._force_stop_proven_owner(PID, 101)
    owner.terminate.assert_called_once_with()
    child.terminate.assert_called_once_with()
    owner.kill.assert_called_once_with()
    child.kill.assert_called_once_with()
    command.assert_not_called()
