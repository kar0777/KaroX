"""Economy OFF vs ON: the controlled local evaluation is honest end-to-end.

Two real kernel runs execute the identical scripted task with economy OFF
and ON against the same repository fixture. The comparison must (a) prove
quality parity from the runs themselves, (b) report only counters both
runs actually persisted, (c) claim no dollars, and (d) fail the
optimization loudly when parity breaks.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.agent import AgentKernel, AgentLimits, SYSTEM_PROMPT
from karox.core import CoreRuntime
from karox.economy_eval import RunOutcome, compare_runs
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.providers import ModelRequest, ModelResponse, ToolCall
from karox.sessions import SessionStore
from karox.usage_report import MEASURED, UNAVAILABLE


class _Provider:
    provider_name = "test_provider"

    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("provider received an unexpected request")
        return self.responses.pop(0)


def _script() -> list[ModelResponse]:
    """The identical task script both modes replay."""

    def call(call_id: str, name: str, arguments: object) -> ToolCall:
        return ToolCall(call_id, name, json.dumps(arguments, ensure_ascii=False))

    def response(*calls: ToolCall, content: str | None = None) -> ModelResponse:
        return ModelResponse(
            content=content,
            tool_calls=tuple(calls),
            finish_reason="tool_calls" if calls else "stop",
            usage={"prompt_tokens": 30, "completion_tokens": 3},
            response_id=None,
        )

    return [
        response(call("read-1", "repo_read_file", {"path": "sample.txt"})),
        response(call("read-2", "repo_read_file", {"path": "sample.txt"})),
        response(content="sample.txt contains the word before"),
    ]


class EconomyOffOnEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run_mode(self, *, economy: bool, tag: str) -> tuple[object, dict]:
        repository = self.root / f"repo-{tag}"
        initialize_git_repository(repository)
        (repository / "sample.txt").write_text("before\n", encoding="utf-8")
        sessions = SessionStore(self.root / f"sessions-{tag}")
        sessions.create(
            repository,
            "what does sample.txt contain?",
            AccessProfile.WORKSPACE_WRITE,
            session_id="session",
        )
        origin = Origin(OriginKind.NATIVE_AGENT, "test-agent")
        policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        policy.set_grants(
            origin,
            {
                Capability.REPO_READ,
                Capability.REPO_WRITE,
                Capability.PROCESS_RUN,
                Capability.CHECKS_RUN,
                Capability.GIT_READ,
            },
        )
        core = CoreRuntime(
            repository,
            policy,
            sessions,
            self.root / f"audit-{tag}.jsonl",
            verification_commands=[[sys.executable, "-c", "print('ok')"]],
        )
        kernel = AgentKernel(
            provider=_Provider(_script()),
            model="test-model",
            core=core,
            sessions=sessions,
            origin=origin,
            limits=AgentLimits(max_seconds=30),
            system_prompt=SYSTEM_PROMPT,
            economy_mode=economy,
        )
        report = kernel.run("session")
        record = sessions.load("session")
        usage = record.usage if isinstance(record.usage, dict) else {}
        return report, usage

    def _report_outcome(self, report: object) -> RunOutcome:
        return RunOutcome(
            answer=str(getattr(report, "message", "") or ""),
            outcome=str(getattr(report, "outcome", "")),
            files_changed=(),
            verified=bool(getattr(report, "verified", False)),
        )

    def test_off_and_on_reach_identical_quality(self) -> None:
        off_report, off_usage = self._run_mode(economy=False, tag="off")
        on_report, on_usage = self._run_mode(economy=True, tag="on")
        comparison = compare_runs(
            off_usage=off_usage,
            on_usage=on_usage,
            off_outcome=self._report_outcome(off_report),
            on_outcome=self._report_outcome(on_report),
        )
        self.assertTrue(comparison.quality_parity, comparison.parity_issues)

    def test_measured_metrics_come_from_both_runs(self) -> None:
        off_report, off_usage = self._run_mode(economy=False, tag="off")
        on_report, on_usage = self._run_mode(economy=True, tag="on")
        comparison = compare_runs(
            off_usage=off_usage,
            on_usage=on_usage,
            off_outcome=self._report_outcome(off_report),
            on_outcome=self._report_outcome(on_report),
        )
        by_name = {metric.name: metric for metric in comparison.metrics}
        prefix = by_name["prefix stable steps"]
        self.assertEqual(prefix.label, MEASURED)
        self.assertEqual(prefix.off, prefix.on)
        reads = by_name["read cache hits"]
        self.assertEqual(reads.label, MEASURED)

    def test_render_claims_no_dollars_and_labels_every_metric(self) -> None:
        off_report, off_usage = self._run_mode(economy=False, tag="off")
        on_report, on_usage = self._run_mode(economy=True, tag="on")
        comparison = compare_runs(
            off_usage=off_usage,
            on_usage=on_usage,
            off_outcome=self._report_outcome(off_report),
            on_outcome=self._report_outcome(on_report),
        )
        rendered = "\n".join(comparison.render())
        self.assertIn("quality parity: PASS", rendered)
        self.assertIn("no invented dollars", rendered)
        self.assertNotIn("USD", rendered)
        for line in comparison.render():
            if line.strip().startswith(("OFF", "quality", "ECONOMY", "cost", "parity")):
                continue
            if ": OFF " in line:
                self.assertTrue(
                    MEASURED in line or UNAVAILABLE in line, line
                )

    def test_parity_failure_fails_the_optimization(self) -> None:
        off_report, off_usage = self._run_mode(economy=False, tag="off")
        on_report, on_usage = self._run_mode(economy=True, tag="on")
        broken = RunOutcome(
            answer="a different answer",
            outcome=str(getattr(on_report, "outcome", "")),
            files_changed=(),
            verified=bool(getattr(on_report, "verified", False)),
        )
        comparison = compare_runs(
            off_usage=off_usage,
            on_usage=on_usage,
            off_outcome=self._report_outcome(off_report),
            on_outcome=broken,
        )
        self.assertFalse(comparison.quality_parity)
        rendered = "\n".join(comparison.render())
        self.assertIn("FAIL", rendered)

    def test_metric_absent_on_either_side_is_unavailable(self) -> None:
        comparison = compare_runs(
            off_usage={},
            on_usage={},
            off_outcome=RunOutcome("a", "answer", (), True),
            on_outcome=RunOutcome("a", "answer", (), True),
        )
        for metric in comparison.metrics:
            self.assertEqual(metric.label, UNAVAILABLE)
            self.assertIn("—", metric.render())


if __name__ == "__main__":
    unittest.main()
