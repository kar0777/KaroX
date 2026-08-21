"""P1.4 acceptance: the output policy is wired into the production call-site.

`tests/test_output_policy.py` proves the renderer as a standalone class. This
file proves the product path: a finished native-agent run (`AgentReport`) is
narrated to the user through `karox.output_policy` -- via `cli._print_agent_report`,
the same function `karox agent run` calls -- and the CLI parser actually exposes
`--output-mode`. If someone unwires the policy, this file fails, not a unit test
of an unused class.
"""

from __future__ import annotations

import contextlib
import io
import unittest

from karox import cli
from karox.agent import AgentReport
from karox.output_policy import OutputMode


def _report(**overrides: object) -> AgentReport:
    payload: dict[str, object] = dict(
        session_id="sess-wiring-1",
        status="verified",
        phase="done",
        verified=True,
        reason="verified",
        steps=3,
        changed_files=("src/app.py",),
        checks=({"command": "pytest", "ok": True},),
        git_state={},
        evidence=({"kind": "check"}, {"kind": "file_edit"}),
        usage={},
    )
    payload.update(overrides)
    return AgentReport(**payload)  # type: ignore[arg-type]


def _printed(report: AgentReport, mode: OutputMode) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        cli._print_agent_report(report, mode)
    return buffer.getvalue()


class OutputPolicyProductionWiringTests(unittest.TestCase):
    def test_adaptive_success_is_concise_with_details_ref(self) -> None:
        text = _printed(_report(), OutputMode.ADAPTIVE)
        self.assertIn("\u2713 completed with required local evidence in 3 step(s)", text)
        self.assertIn("2 evidence record(s)", text)
        self.assertIn("[Details]", text)
        # Concise means the legacy field dump stays behind DETAILED.
        self.assertNotIn("session_id:", text)
        self.assertNotIn("evidence_records:", text)

    def test_adaptive_failure_escalates_and_is_never_hidden(self) -> None:
        failed = _report(
            status="failed",
            verified=False,
            reason="step_limit",
            checks=({"command": "pytest", "ok": False},),
        )
        for mode in OutputMode:
            text = _printed(failed, mode)
            self.assertIn("\u2717 native agent stopped: step_limit", text, mode)
            self.assertIn("check failed: pytest", text, mode)
        adaptive = _printed(failed, OutputMode.ADAPTIVE)
        self.assertTrue(adaptive.splitlines()[0].startswith("\u2717"))

    def test_warnings_survive_every_mode(self) -> None:
        warned = _report(
            compaction={
                "count": 2,
                "before_tokens": 90000,
                "after_tokens": 30000,
                "within_ceiling": True,
            },
        )
        for mode in OutputMode:
            self.assertIn("context compacted 2x", _printed(warned, mode), mode)

    def test_detailed_keeps_the_complete_legacy_dump(self) -> None:
        text = _printed(_report(), OutputMode.DETAILED)
        self.assertIn("session_id: sess-wiring-1", text)
        self.assertIn("status: verified", text)
        self.assertIn("evidence_records: 2", text)

    def test_cli_parser_exposes_output_mode(self) -> None:
        parser = cli._parser()
        args = parser.parse_args(
            [
                "agent",
                "run",
                "--task",
                "demo",
                "--verification-command",
                '["python", "-m", "pytest", "-q"]',
                "--output-mode",
                "learning",
            ]
        )
        self.assertEqual(args.output_mode, "learning")
        args_default = parser.parse_args(
            [
                "agent",
                "run",
                "--task",
                "demo",
                "--verification-command",
                '["python", "-m", "pytest", "-q"]',
            ]
        )
        self.assertEqual(args_default.output_mode, OutputMode.ADAPTIVE.value)


if __name__ == "__main__":
    unittest.main()
