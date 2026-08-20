"""Process identity is the proof that defeats PID reuse.

These tests pin the one rule the connection lifecycle depends on: an alive PID
is never proof, and only a matching operating-system creation time (plus any
recorded launcher digests) may authorise adoption or termination.
"""

from __future__ import annotations

import json
import os
import unittest

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.process_identity import (
    REASON_ARGV_MISMATCH,
    REASON_CREATE_TIME_MISMATCH,
    REASON_CREATE_TIME_UNAVAILABLE,
    REASON_EXECUTABLE_MISMATCH,
    REASON_NOT_RECORDED,
    REASON_NOT_RUNNING,
    REASON_OWNER_MISMATCH,
    REASON_VERIFIED,
    ProcessIdentity,
    ProcessIdentityError,
    argv_digest,
    capture_process_identity,
    executable_digest,
    owner_digest,
    process_is_running,
    read_process_create_time_ns,
    verify_process_identity,
)


ALIVE = lambda _pid: True  # noqa: E731 - a one-line test double reads better here
DEAD = lambda _pid: False  # noqa: E731


class ProcessIdentityCaptureTests(unittest.TestCase):
    def test_capture_records_digests_and_never_the_raw_command(self) -> None:
        identity = capture_process_identity(
            4321,
            executable="C:/tools/cloudflared.exe",
            argv=["cloudflared", "tunnel", "--token", "super-secret-value"],
            owner="Egor",
            now=1000.0,
            create_time_reader=lambda _pid: 777_000_000,
        )
        serialized = json.dumps(identity.to_dict(), sort_keys=True)
        self.assertNotIn("super-secret-value", serialized)
        self.assertNotIn("cloudflared", serialized)
        self.assertNotIn("egor", serialized.lower())
        self.assertEqual(identity.pid, 4321)
        self.assertEqual(identity.create_time_ns, 777_000_000)
        self.assertTrue(identity.provable)

    def test_capture_without_a_platform_reader_is_not_provable(self) -> None:
        identity = capture_process_identity(
            4321, create_time_reader=lambda _pid: None, now=1.0
        )
        self.assertIsNone(identity.create_time_ns)
        self.assertFalse(identity.provable)

    def test_round_trip_rejects_unknown_and_malformed_fields(self) -> None:
        identity = capture_process_identity(
            7, now=1.0, create_time_reader=lambda _pid: 5
        )
        self.assertEqual(ProcessIdentity.from_dict(identity.to_dict()), identity)
        with self.assertRaises(ProcessIdentityError):
            ProcessIdentity.from_dict({**identity.to_dict(), "extra": 1})
        with self.assertRaises(ProcessIdentityError):
            ProcessIdentity.from_dict({"pid": 0})
        with self.assertRaises(ProcessIdentityError):
            ProcessIdentity.from_dict({"pid": 7, "executable_sha256": "nothex"})

    def test_digests_are_stable_and_distinguishing(self) -> None:
        self.assertEqual(argv_digest(["a", "b"]), argv_digest(["a", "b"]))
        self.assertNotEqual(argv_digest(["a", "b"]), argv_digest(["ab"]))
        self.assertNotEqual(argv_digest(["a", "b"]), argv_digest(["a", "b", "c"]))
        self.assertEqual(owner_digest("Egor"), owner_digest("egor"))
        with self.assertRaises(ProcessIdentityError):
            argv_digest("not-a-sequence")
        with self.assertRaises(ProcessIdentityError):
            executable_digest("")


class ProcessIdentityVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = capture_process_identity(
            4321,
            executable="C:/tools/karox-bridge.exe",
            argv=["karox", "bridge", "serve"],
            owner="Egor",
            now=1000.0,
            create_time_reader=lambda _pid: 777_000_000,
        )

    def test_matching_creation_time_verifies(self) -> None:
        verdict = verify_process_identity(
            self.identity,
            pid_alive=ALIVE,
            create_time_reader=lambda _pid: 777_000_000,
        )
        self.assertTrue(verdict.proven)
        self.assertTrue(verdict.alive)
        self.assertEqual(verdict.reason, REASON_VERIFIED)

    def test_recycled_pid_is_refused(self) -> None:
        verdict = verify_process_identity(
            self.identity,
            pid_alive=ALIVE,
            create_time_reader=lambda _pid: 999_000_000,
        )
        self.assertFalse(verdict.proven)
        self.assertTrue(verdict.alive)
        self.assertEqual(verdict.reason, REASON_CREATE_TIME_MISMATCH)

    def test_dead_process_is_not_proven(self) -> None:
        verdict = verify_process_identity(
            self.identity,
            pid_alive=DEAD,
            create_time_reader=lambda _pid: 777_000_000,
        )
        self.assertFalse(verdict.proven)
        self.assertFalse(verdict.alive)
        self.assertEqual(verdict.reason, REASON_NOT_RUNNING)

    def test_platform_without_creation_time_is_unproven_not_verified(self) -> None:
        verdict = verify_process_identity(
            self.identity, pid_alive=ALIVE, create_time_reader=lambda _pid: None
        )
        self.assertFalse(verdict.proven)
        self.assertEqual(verdict.reason, REASON_CREATE_TIME_UNAVAILABLE)

    def test_missing_identity_is_reported_as_not_recorded(self) -> None:
        verdict = verify_process_identity(
            None, pid_alive=ALIVE, create_time_reader=lambda _pid: 1
        )
        self.assertFalse(verdict.proven)
        self.assertEqual(verdict.reason, REASON_NOT_RECORDED)

    def test_a_raising_liveness_probe_is_treated_as_dead(self) -> None:
        def explode(_pid: int) -> bool:
            raise OSError("probe failed")

        verdict = verify_process_identity(
            self.identity,
            pid_alive=explode,
            create_time_reader=lambda _pid: 777_000_000,
        )
        self.assertFalse(verdict.proven)
        self.assertEqual(verdict.reason, REASON_NOT_RUNNING)

    def test_expected_launcher_drift_is_refused(self) -> None:
        for expected, reason in (
            ({"expected_executable": "C:/tools/other.exe"}, REASON_EXECUTABLE_MISMATCH),
            ({"expected_argv": ["karox", "bridge", "other"]}, REASON_ARGV_MISMATCH),
            ({"expected_owner": "someone-else"}, REASON_OWNER_MISMATCH),
        ):
            with self.subTest(reason=reason):
                verdict = verify_process_identity(
                    self.identity,
                    pid_alive=ALIVE,
                    create_time_reader=lambda _pid: 777_000_000,
                    **expected,
                )
                self.assertFalse(verdict.proven)
                self.assertEqual(verdict.reason, reason)

    def test_expected_launcher_match_still_verifies(self) -> None:
        verdict = verify_process_identity(
            self.identity,
            pid_alive=ALIVE,
            create_time_reader=lambda _pid: 777_000_000,
            expected_executable="C:/tools/karox-bridge.exe",
            expected_argv=["karox", "bridge", "serve"],
            expected_owner="EGOR",
        )
        self.assertTrue(verdict.proven)


class PlatformReaderTests(unittest.TestCase):
    def test_own_process_creation_time_is_stable_or_honestly_unavailable(self) -> None:
        self.assertTrue(process_is_running(os.getpid()))
        first = read_process_create_time_ns(os.getpid())
        second = read_process_create_time_ns(os.getpid())
        self.assertEqual(first, second)
        if first is None:
            self.skipTest("this platform cannot report a process creation time")
        self.assertIsInstance(first, int)
        self.assertGreater(first, 0)

    def test_invalid_pids_never_report_a_creation_time(self) -> None:
        for pid in (0, -1, True):
            with self.subTest(pid=pid):
                self.assertIsNone(read_process_create_time_ns(pid))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
