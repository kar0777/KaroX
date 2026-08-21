"""Evidence Packet acceptance: lossless elision with honest accounting.

The invariants pinned here are the Part 2 contract: critical evidence and
the artifact reference survive every fidelity level, a failure is never
hidden by a low-detail mode, rendering is deterministic, and byte savings
are never invented when the raw size is unknown.
"""

from __future__ import annotations

import json
import unittest

from _support import SRC  # noqa: F401
from karox.evidence_packets import (
    ArtifactRef,
    EvidencePacket,
    Fidelity,
    PacketKind,
    build_packet,
    checks_job_packet,
    git_packet,
    packet_from_plan_result,
    search_packet,
    tests_packet as make_tests_packet,
)


def _failing_tests() -> EvidencePacket:
    return make_tests_packet(
        exit_code=1,
        passed=421,
        failed=1,
        duration_seconds=31.2,
        primary_failure="test_session_restore src/session.py:182",
        related_files=("src/session.py", "tests/test_sessions.py"),
        detail=("captured stderr line",),
        artifact=ArtifactRef("art-123", sha256="ab" * 32, raw_bytes=250_000),
    )


class CriticalEvidenceTests(unittest.TestCase):
    def test_failure_survives_every_fidelity(self) -> None:
        packet = _failing_tests()
        for fidelity in Fidelity:
            rendered = packet.render(fidelity)
            self.assertIn("test_session_restore", rendered, fidelity)
            self.assertIn('"status": "failed"', rendered, fidelity)

    def test_artifact_reference_survives_every_fidelity(self) -> None:
        packet = _failing_tests()
        for fidelity in Fidelity:
            payload = json.loads(packet.render(fidelity))
            self.assertEqual(payload["artifact"]["artifact_id"], "art-123")

    def test_truncated_search_flags_the_artifact_at_summary(self) -> None:
        packet = search_packet(
            query="needle",
            match_count=500,
            files=("a.py",),
            truncated=True,
            artifact=ArtifactRef("art-9"),
        )
        rendered = packet.render(Fidelity.SUMMARY)
        self.assertIn("truncated", rendered)


class FidelityTests(unittest.TestCase):
    def test_auto_expands_failures_and_compacts_successes(self) -> None:
        failing = _failing_tests()
        passing = make_tests_packet(exit_code=0, passed=10, failed=0)
        self.assertIs(failing.resolve_fidelity(Fidelity.AUTO), Fidelity.EVIDENCE)
        self.assertIs(passing.resolve_fidelity(Fidelity.AUTO), Fidelity.SUMMARY)

    def test_summary_drops_metrics_but_full_keeps_detail(self) -> None:
        packet = _failing_tests()
        summary = json.loads(packet.render(Fidelity.SUMMARY))
        full = json.loads(packet.render(Fidelity.FULL))
        self.assertNotIn("metrics", summary)
        self.assertEqual(full["metrics"]["passed"], 421)
        self.assertEqual(full["detail"], ["captured stderr line"])

    def test_rendering_is_deterministic(self) -> None:
        one = _failing_tests().render(Fidelity.FULL)
        two = _failing_tests().render(Fidelity.FULL)
        self.assertEqual(one, two)


class AccountingTests(unittest.TestCase):
    def test_bytes_avoided_measured_only_with_known_raw_size(self) -> None:
        with_size = _failing_tests()
        avoided = with_size.bytes_avoided(Fidelity.SUMMARY)
        self.assertIsNotNone(avoided)
        assert avoided is not None
        self.assertGreater(avoided, 200_000)

    def test_unknown_raw_size_stays_unknown(self) -> None:
        packet = git_packet(
            command="status", exit_code=0, summary="clean tree",
            artifact=ArtifactRef("art-1"),
        )
        self.assertIsNone(packet.bytes_avoided())

    def test_no_artifact_means_no_claimed_savings(self) -> None:
        packet = build_packet(exit_code=0, summary="wheel built")
        self.assertIsNone(packet.bytes_avoided())


class BuilderTests(unittest.TestCase):
    def test_tests_packet_matches_mandate_example_shape(self) -> None:
        packet = _failing_tests()
        self.assertIs(packet.kind, PacketKind.TESTS)
        self.assertEqual(packet.status, "failed")
        self.assertIn("421 passed", packet.summary)
        self.assertIn("31.2s", packet.summary)

    def test_git_packet_failure_carries_exit_code(self) -> None:
        packet = git_packet(command="commit", exit_code=128, summary="failed")
        self.assertEqual(packet.critical, ("git commit exit_code=128",))

    def test_build_packet_success_has_no_critical_noise(self) -> None:
        packet = build_packet(exit_code=0, summary="wheel built")
        self.assertEqual(packet.critical, ())
        self.assertFalse(packet.failed)

    def test_search_packet_counts_are_metrics(self) -> None:
        packet = search_packet(query="q", match_count=3, files=("a", "b"))
        self.assertEqual(packet.metrics["match_count"], 3)
        self.assertEqual(packet.metrics["file_count"], 2)


class ChecksJobPacketTests(unittest.TestCase):
    def test_passed_job_parses_counts_from_summary(self) -> None:
        packet = checks_job_packet(
            status="passed",
            exit_code=0,
            summary="421 passed, 0 failed in 31.2s",
            first_failure=None,
            duration_seconds=31.2,
            artifact=ArtifactRef("art-log", raw_bytes=100_000),
        )
        self.assertIs(packet.kind, PacketKind.TESTS)
        self.assertEqual(packet.status, "passed")
        self.assertEqual(packet.metrics["passed"], 421)
        avoided = packet.bytes_avoided()
        assert avoided is not None
        self.assertGreater(avoided, 90_000)

    def test_failed_job_never_renders_as_success(self) -> None:
        packet = checks_job_packet(
            status="failed",
            exit_code=1,
            summary="420 passed, 1 failed in 30.0s",
            first_failure="FAILED tests/test_session.py::test_restore",
            artifact=ArtifactRef("art-log"),
        )
        for fidelity in Fidelity:
            rendered = packet.render(fidelity)
            self.assertIn('"status": "failed"', rendered, fidelity)
            self.assertIn("test_restore", rendered, fidelity)
            self.assertIn("art-log", rendered, fidelity)

    def test_passed_status_with_failing_counts_stays_failed(self) -> None:
        packet = checks_job_packet(
            status="passed",
            exit_code=0,
            summary="1 failed, 2 passed",
            first_failure=None,
        )
        self.assertEqual(packet.status, "failed")

    def test_worker_death_without_exit_code_is_failed(self) -> None:
        packet = checks_job_packet(
            status="failed",
            exit_code=None,
            summary=None,
            first_failure=None,
            error_code="worker_exited_without_final_state",
        )
        self.assertEqual(packet.status, "failed")
        self.assertTrue(
            any("worker_exited" in line for line in packet.critical)
        )


class PlanResultPacketTests(unittest.TestCase):
    def test_checks_result_with_nonzero_exit_is_failed(self) -> None:
        packet = packet_from_plan_result(
            "checks",
            {
                "ok": True,
                "exit_code": 1,
                "summary": "1 failed, 2 passed",
                "first_failure": "FAILED tests/test_a.py::test_b",
            },
            success=True,
        )
        assert packet is not None
        self.assertEqual(packet.status, "failed")
        self.assertIs(packet.kind, PacketKind.TESTS)
        self.assertIn(
            "primary_failure: FAILED tests/test_a.py::test_b", packet.critical
        )

    def test_checks_success_summary_counts_become_metrics(self) -> None:
        packet = packet_from_plan_result(
            "checks",
            {"ok": True, "exit_code": 0, "data": {"summary": "3 passed"}},
            success=True,
        )
        assert packet is not None
        self.assertEqual(packet.status, "passed")
        self.assertEqual(packet.metrics["passed"], 3)

    def test_executor_failure_verdict_overrides_payload(self) -> None:
        packet = packet_from_plan_result(
            "search",
            {"ok": True, "match_count": 3, "matches": []},
            success=False,
        )
        assert packet is not None
        self.assertTrue(packet.failed)
        self.assertTrue(
            any("operation failed" in line for line in packet.critical)
        )

    def test_search_files_are_bounded_and_deduplicated(self) -> None:
        matches = [{"path": f"src/m{index}.py"} for index in range(30)]
        packet = packet_from_plan_result(
            "search",
            {"ok": True, "query": "needle", "match_count": 30, "matches": matches},
            success=True,
        )
        assert packet is not None
        self.assertEqual(len(packet.related_files), 20)
        self.assertTrue(any("truncated" in line for line in packet.critical))

    def test_untyped_action_returns_none(self) -> None:
        self.assertIsNone(
            packet_from_plan_result("read", {"ok": True}, success=True)
        )

    def test_timed_out_checks_are_failed(self) -> None:
        packet = packet_from_plan_result(
            "checks",
            {"ok": True, "exit_code": 0, "timed_out": True},
            success=True,
        )
        assert packet is not None
        self.assertEqual(packet.status, "failed")
        self.assertIn("timed_out=true", packet.critical)


if __name__ == "__main__":
    unittest.main()
