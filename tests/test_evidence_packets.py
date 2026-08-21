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
    git_packet,
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


if __name__ == "__main__":
    unittest.main()
