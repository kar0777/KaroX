"""Port and process ownership verdicts for saved bridge profiles.

Phase 0.3: the launcher must classify who holds a port before it can restart a
saved bridge. These tests pin the verdict codes and the ownership metadata
using a fake watchdog record and monkeypatched liveness/creation-time probes,
so no real process or OS port table is touched.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from karox.port_ownership import (
    OWNERSHIP_FREE,
    OWNERSHIP_REUSE_SAME,
    OWNERSHIP_STALE_OWNED,
    OWNERSHIP_UNRELATED,
    OwnershipMetadata,
    OwnershipVerdict,
    check_port_ownership,
    prove_bridge_process_identity,
    prove_saved_bridge_owner_identity,
)


def prove_saved_bridge_owner_identity_for_test(argv: list[str], profile: str) -> bool:
    """Run the owner proof against a fixed argv instead of a real process."""
    with mock.patch("karox.port_ownership._process_command_line", return_value=argv):
        return prove_saved_bridge_owner_identity(4242, profile)


class _FakeWatchdog:
    """A temporary watchdog dir populated with one record."""

    def __init__(self, tmp_path: Path, *, session_id: str, record: dict) -> None:
        self.dir = tmp_path / "web-bridge"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{session_id}.json"
        self.path.write_text(json.dumps(record), encoding="utf-8")


def _record(*, owner_pid: int = 99999, session_id: str = "web-saved-abc", port: int = 8765, **extra):
    base = {
        "session_id": session_id,
        "owner_pid": owner_pid,
        "profile": "chatgpt-web",
        "saved_profile": "clickup-opus",
        "port": port,
        "public_url": "https://example.ts.net/mcp",
        "tunnel": "tailscale",
        "started_at": 1700000000.0,
    }
    base.update(extra)
    return base


class CheckPortOwnershipTests(unittest.TestCase):
    """The five cases A-E from the brief, as ownership verdicts."""

    def setUp(self) -> None:
        # The session-id resolver returns one candidate so only one watchdog
        # file is consulted.
        self._candidates = mock.patch(
            "karox.port_ownership.saved_web_bridge_session_candidates",
            return_value=("web-saved-test",),
        )
        self._candidates.start()
        self.addCleanup(self._candidates.stop)

    def test_port_free_no_watchdog(self) -> None:
        with mock.patch("karox.port_ownership._port_has_listener", return_value=False):
            verdict = check_port_ownership("clickup-opus", port=8765)
        self.assertEqual(verdict.verdict, OWNERSHIP_FREE)
        self.assertIsNone(verdict.metadata.pid)

    def test_same_profile_live_bridge_reuse(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with mock.patch("karox.port_ownership.watchdog_dir", return_value=tmp_path / "web-bridge"):
                _FakeWatchdog(tmp_path, session_id="web-saved-test", record=_record(owner_pid=12345, session_id="web-saved-test"))
                with mock.patch("karox.port_ownership._process_is_alive", return_value=True), \
                     mock.patch("karox.port_ownership.read_process_create_time_ns", return_value=1337):
                    verdict = check_port_ownership("clickup-opus", port=8765)
        self.assertEqual(verdict.verdict, OWNERSHIP_REUSE_SAME)
        self.assertEqual(verdict.metadata.pid, 12345)
        self.assertTrue(verdict.metadata.pid_proven)
        self.assertEqual(verdict.metadata.process_start_time_ns, 1337)
        self.assertEqual(verdict.metadata.tunnel_type, "tailscale")
        self.assertEqual(verdict.metadata.public_url, "https://example.ts.net/mcp")
        self.assertEqual(verdict.metadata.credential_reference, "os-keyring:bridge/web-saved-test")

    def test_stale_owned_pid_create_time_unverifiable(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with mock.patch("karox.port_ownership.watchdog_dir", return_value=tmp_path / "web-bridge"):
                _FakeWatchdog(tmp_path, session_id="web-saved-test", record=_record(owner_pid=12345))
                with mock.patch("karox.port_ownership._process_is_alive", return_value=True), \
                     mock.patch("karox.port_ownership.read_process_create_time_ns", return_value=None):
                    verdict = check_port_ownership("clickup-opus", port=8765)
        self.assertEqual(verdict.verdict, OWNERSHIP_STALE_OWNED)
        self.assertFalse(verdict.metadata.pid_proven)

    def test_unrelated_process_holding_port(self) -> None:
        with mock.patch("karox.port_ownership._port_has_listener", return_value=True), \
             mock.patch("karox.port_ownership._port_owning_pid", return_value=55555):
            verdict = check_port_ownership("clickup-opus", port=8765)
        self.assertEqual(verdict.verdict, OWNERSHIP_UNRELATED)
        self.assertEqual(verdict.unrelated_pid, 55555)
        self.assertIsNone(verdict.metadata.pid)

    def test_malformed_watchdog_treated_as_unrelated(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wd_dir = tmp_path / "web-bridge"
            wd_dir.mkdir(parents=True)
            (wd_dir / "web-saved-test.json").write_text("not json {{{", encoding="utf-8")
            with mock.patch("karox.port_ownership.watchdog_dir", return_value=wd_dir), \
                 mock.patch("karox.port_ownership._port_has_listener", return_value=True):
                verdict = check_port_ownership("clickup-opus", port=8765)
        self.assertEqual(verdict.verdict, OWNERSHIP_UNRELATED)


class OrphanedOwnedBridgeTests(unittest.TestCase):
    """A surviving ``bridge serve`` child is ours, not a foreign process.

    When an owner dies its local MCP child can keep holding the port. The old
    behaviour classified that child as ``unrelated_process``, which made both
    ``bridge restart --saved`` and the sibling supervisor refuse to act -- a
    permanent deadlock on a port KaroX itself occupies. Ownership must be proven
    from the holder's own command line (module, subcommand and this profile's
    session id), and only then reported as a stale *owned* process.
    """

    def setUp(self) -> None:
        self._candidates = mock.patch(
            "karox.port_ownership.saved_web_bridge_session_candidates",
            return_value=("web-saved-test",),
        )
        self._candidates.start()
        self.addCleanup(self._candidates.stop)

    def _check(self, argv, *, holder_pid: int = 4242, record_extra: dict | None = None):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wd_dir = tmp_path / "web-bridge"
            with mock.patch("karox.port_ownership.watchdog_dir", return_value=wd_dir):
                _FakeWatchdog(
                    tmp_path,
                    session_id="web-saved-test",
                    record=_record(
                        owner_pid=12345,
                        session_id="web-saved-test",
                        **(record_extra or {"bridge_pid": holder_pid}),
                    ),
                )
                # The owner is gone; its child still listens.
                with mock.patch("karox.port_ownership._process_is_alive", return_value=False), \
                     mock.patch("karox.port_ownership.read_process_create_time_ns", return_value=None), \
                     mock.patch("karox.port_ownership._port_has_listener", return_value=True), \
                     mock.patch("karox.port_ownership._port_owning_pid", return_value=holder_pid), \
                     mock.patch("karox.port_ownership._process_command_line", return_value=argv):
                    return check_port_ownership("clickup-opus", port=8765)

    def _bridge_argv(self, session_id: str = "web-saved-test") -> list[str]:
        return [
            "C:\\Python313\\python.exe",
            "-m",
            "karox.cli",
            "bridge",
            "serve",
            "--profile",
            "chatgpt-web",
            "--session-id",
            session_id,
            "--port",
            "8765",
        ]

    def test_orphaned_bridge_child_is_reported_as_owned_and_recoverable(self) -> None:
        verdict = self._check(self._bridge_argv())
        self.assertEqual(verdict.verdict, OWNERSHIP_STALE_OWNED)
        self.assertEqual(verdict.owned_orphan_pid, 4242)
        self.assertIn("owned_orphan_pid", verdict.to_dict())
        # The saved identity is still readable, and the dead owner is not
        # presented as a process anyone may terminate.
        self.assertEqual(verdict.metadata.session_id, "web-saved-test")
        self.assertFalse(verdict.metadata.pid_proven)

    def test_foreign_port_holder_stays_unrelated(self) -> None:
        verdict = self._check(["C:\\nginx\\nginx.exe", "-g", "daemon off;"])
        self.assertEqual(verdict.verdict, OWNERSHIP_UNRELATED)
        self.assertIsNone(verdict.owned_orphan_pid)
        self.assertEqual(verdict.unrelated_pid, 4242)

    def test_unreadable_command_line_fails_closed(self) -> None:
        verdict = self._check(None)
        self.assertEqual(verdict.verdict, OWNERSHIP_UNRELATED)
        self.assertIsNone(verdict.owned_orphan_pid)

    def test_bridge_for_another_session_stays_unrelated(self) -> None:
        verdict = self._check(self._bridge_argv("web-saved-someone-else"))
        self.assertEqual(verdict.verdict, OWNERSHIP_UNRELATED)
        self.assertIsNone(verdict.owned_orphan_pid)

    def test_proof_accepts_inline_session_id_form(self) -> None:
        argv = [
            "python.exe",
            "-m",
            "karox.cli",
            "bridge",
            "serve",
            "--session-id=web-saved-test",
        ]
        self.assertEqual(
            prove_bridge_process_identity(4242, ("web-saved-test",), command_line=argv),
            "web-saved-test",
        )

    def test_proof_rejects_a_process_that_only_mentions_the_session_id(self) -> None:
        argv = ["python.exe", "-m", "karox.cli", "bridge", "status", "--saved", "web-saved-test"]
        self.assertIsNone(
            prove_bridge_process_identity(4242, ("web-saved-test",), command_line=argv)
        )


class LiveOwnerWithoutWatchdogRecordTests(unittest.TestCase):
    """A listener whose owner is alive is not an orphan to be reclaimed.

    An owner that lost its watchdog record is invisible to the record-based
    check, so its still-serving child looked orphaned. Reclaiming only that child
    is futile: the live owner respawns it, and every freshly spawned owner then
    dies binding the port ("detached bridge process exited before becoming
    ready"). The verdict must name the proven owner so recovery recycles it.
    """

    def setUp(self) -> None:
        self._candidates = mock.patch(
            "karox.port_ownership.saved_web_bridge_session_candidates",
            return_value=("web-saved-test",),
        )
        self._candidates.start()
        self.addCleanup(self._candidates.stop)

    def _verdict(self, owner_argv, *, holder_pid: int = 4242, owner_pid: int = 909):
        child_argv = [
            "python.exe",
            "-m",
            "karox.cli",
            "bridge",
            "serve",
            "--session-id",
            "web-saved-test",
        ]

        class _FakeParent:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def parent(self):  # noqa: ANN202 - test double
                return None

        class _FakeChild:
            def parent(self):  # noqa: ANN202 - test double
                return _FakeParent(owner_pid)

        fake_psutil = mock.MagicMock()
        fake_psutil.Process.return_value = _FakeChild()

        def command_line(pid: int) -> object:
            return owner_argv if pid == owner_pid else child_argv

        with mock.patch.dict("sys.modules", {"psutil": fake_psutil}), \
             mock.patch("karox.port_ownership._port_has_listener", return_value=True), \
             mock.patch("karox.port_ownership._port_owning_pid", return_value=holder_pid), \
             mock.patch(
                 "karox.port_ownership._process_command_line", side_effect=command_line
             ):
            return check_port_ownership("clickup-opus", port=8765)

    def test_a_live_proven_owner_is_named_instead_of_being_called_gone(self) -> None:
        verdict = self._verdict(
            [
                "python.exe",
                "-m",
                "karox.cli",
                "bridge",
                "connect",
                "--saved",
                "clickup-opus",
            ]
        )
        self.assertEqual(verdict.verdict, OWNERSHIP_STALE_OWNED)
        self.assertEqual(verdict.owned_orphan_pid, 4242)
        self.assertEqual(verdict.live_unrecorded_owner_pid, 909)
        self.assertIn("live_unrecorded_owner_pid", verdict.to_dict())
        self.assertIn("still running without a watchdog record", verdict.reason)

    def test_a_parent_owning_another_profile_is_never_claimed(self) -> None:
        verdict = self._verdict(
            [
                "python.exe",
                "-m",
                "karox.cli",
                "bridge",
                "connect",
                "--saved",
                "someone-elses-profile",
            ]
        )
        self.assertEqual(verdict.verdict, OWNERSHIP_STALE_OWNED)
        self.assertEqual(verdict.owned_orphan_pid, 4242)
        self.assertIsNone(verdict.live_unrecorded_owner_pid)

    def test_an_unrelated_parent_is_never_claimed_as_owner(self) -> None:
        verdict = self._verdict(["C:\\Windows\\explorer.exe"])
        self.assertEqual(verdict.verdict, OWNERSHIP_STALE_OWNED)
        self.assertIsNone(verdict.live_unrecorded_owner_pid)

    def test_owner_proof_requires_the_connect_subcommand(self) -> None:
        self.assertFalse(
            prove_saved_bridge_owner_identity_for_test(
                ["python.exe", "-m", "karox.cli", "bridge", "status", "--saved", "p"],
                "p",
            )
        )
        self.assertTrue(
            prove_saved_bridge_owner_identity_for_test(
                ["python.exe", "-m", "karox.cli", "bridge", "connect", "--saved=p"],
                "p",
            )
        )


class WatchdogReadResilienceTests(unittest.TestCase):
    """A record being atomically replaced is contention, not "no bridge".

    ``write_watchdog`` replaces the record on every heartbeat and retries while
    a reader holds it open. The reader needs the mirror image of that patience:
    without it a rename race is misread as "no owned bridge", which downgrades a
    healthy bridge to ``unrelated_process``.
    """

    def setUp(self) -> None:
        self._candidates = mock.patch(
            "karox.port_ownership.saved_web_bridge_session_candidates",
            return_value=("web-saved-test",),
        )
        self._candidates.start()
        self.addCleanup(self._candidates.stop)

    def test_transient_read_error_is_retried_before_giving_up(self) -> None:
        import tempfile

        payload = json.dumps(_record(owner_pid=12345, session_id="web-saved-test"))
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wd_dir = tmp_path / "web-bridge"
            wd_dir.mkdir(parents=True, exist_ok=True)
            (wd_dir / "web-saved-test.json").write_text(payload, encoding="utf-8")
            with mock.patch("karox.port_ownership.watchdog_dir", return_value=wd_dir), \
                 mock.patch("karox.port_ownership._process_is_alive", return_value=True), \
                 mock.patch("karox.port_ownership.read_process_create_time_ns", return_value=1337), \
                 mock.patch(
                     "karox.port_ownership._read_watchdog_text",
                     side_effect=[PermissionError("being replaced"), payload],
                 ) as reader:
                verdict = check_port_ownership("clickup-opus", port=8765)
        self.assertEqual(verdict.verdict, OWNERSHIP_REUSE_SAME)
        self.assertEqual(reader.call_count, 2)

    def test_missing_record_is_not_retried(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            wd_dir = Path(tmp) / "web-bridge"
            wd_dir.mkdir(parents=True, exist_ok=True)
            with mock.patch("karox.port_ownership.watchdog_dir", return_value=wd_dir), \
                 mock.patch("karox.port_ownership._port_has_listener", return_value=False), \
                 mock.patch(
                     "karox.port_ownership._read_watchdog_text",
                     side_effect=FileNotFoundError("no record"),
                 ) as reader:
                verdict = check_port_ownership("clickup-opus", port=8765)
        self.assertEqual(verdict.verdict, OWNERSHIP_FREE)
        self.assertEqual(reader.call_count, 1)


    def test_extract_bridge_process_info(self) -> None:
        from karox.port_ownership import extract_bridge_process_info

        cmd = [
            "pythonw.exe",
            "-m",
            "karox.cli",
            "bridge",
            "serve",
            "--profile",
            "chatgpt-web",
            "--session-id",
            "web-saved-12345",
            "--public-url",
            "https://myhost.ts.net",
        ]
        info = extract_bridge_process_info(9999, command_line=cmd)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.get("session_id"), "web-saved-12345")
        self.assertEqual(info.get("profile"), "chatgpt-web")
        self.assertEqual(info.get("public_url"), "https://myhost.ts.net")

    def test_foreign_karox_bridge_diagnosis(self) -> None:
        cmd = [
            "pythonw.exe",
            "-m",
            "karox.cli",
            "bridge",
            "serve",
            "--profile",
            "chatgpt-web",
            "--session-id",
            "web-saved-other",
        ]
        with mock.patch("karox.port_ownership._find_active_watchdog", return_value=(None, None)), \
             mock.patch("karox.port_ownership._port_has_listener", return_value=True), \
             mock.patch("karox.port_ownership._port_owning_pid", return_value=8888), \
             mock.patch("karox.port_ownership._process_command_line", return_value=cmd):
            verdict = check_port_ownership("aura-browser", port=8765)
            self.assertEqual(verdict.verdict, OWNERSHIP_UNRELATED)
            self.assertIn("in use by KaroX bridge", verdict.reason)
            self.assertIn("web-saved-other", verdict.reason)


class OwnershipMetadataTests(unittest.TestCase):
    def test_to_dict_has_no_secret_fields(self) -> None:
        meta = OwnershipMetadata(
            profile="p", credential_reference="os-keyring:bridge/x", session_id="x",
            pid=1, process_start_time_ns=2, executable_path=None, repository=None,
            local_host="127.0.0.1", local_port=8765, public_url="https://x",
            tunnel_type="tailscale", route_identity=None, config_digest=None,
            creation_timestamp=1.0, watchdog_path="/x.json", pid_proven=True,
        )
        d = meta.to_dict()
        for forbidden in ("secret", "token", "password", "value"):
            self.assertNotIn(forbidden, d, d)


if __name__ == "__main__":
    unittest.main()
