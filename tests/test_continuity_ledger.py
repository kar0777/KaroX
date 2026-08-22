"""Reasoning continuity ledger: dropped conclusions survive compaction.

Compaction keeps tool facts; these tests pin that it now also carries the
model's own stated conclusions -- quoted verbatim, bounded, and clearly
labeled as narration -- so a long session does not re-derive decisions it
already wrote down. Extraction only: nothing here may paraphrase.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.agent import AgentKernel, AgentLimits, SYSTEM_PROMPT
from karox.context_compiler import (
    CONTINUITY_MAX_STATEMENTS,
    CONTINUITY_STATEMENT_CHARS,
    continuity_lines,
)
from karox.core_tools import ExtendedCoreRuntime
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.providers import ModelRequest, ModelResponse
from karox.sessions import SessionStore


def _assistant(content: Any, blocks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"role": "assistant", "content": content}
    if blocks is not None:
        entry["reasoning_blocks"] = blocks
    return entry


class ContinuityLinesTests(unittest.TestCase):
    def test_statements_are_quoted_in_order(self) -> None:
        groups = [
            [_assistant("Root cause is the stale digest.\nMore detail.")],
            [{"role": "tool", "content": "ignored"}],
            [_assistant("Fix lands in tool_catalog.py.")],
        ]
        carried, stats = continuity_lines(groups)
        self.assertEqual(
            carried,
            (
                "turn 1: Root cause is the stale digest.",
                "turn 2: Fix lands in tool_catalog.py.",
            ),
        )
        self.assertEqual(stats.statements_carried, 2)
        self.assertEqual(stats.statement_chars, sum(len(s) for s in carried))

    def test_only_most_recent_statements_survive(self) -> None:
        groups = [
            [_assistant(f"Conclusion number {index}.")]
            for index in range(CONTINUITY_MAX_STATEMENTS + 5)
        ]
        carried, stats = continuity_lines(groups)
        self.assertEqual(len(carried), CONTINUITY_MAX_STATEMENTS)
        self.assertIn("Conclusion number 12.", carried[-1])
        self.assertEqual(stats.statements_carried, CONTINUITY_MAX_STATEMENTS)

    def test_long_statement_is_clipped(self) -> None:
        groups = [[_assistant("x" * 500)]]
        carried, _stats = continuity_lines(groups)
        self.assertEqual(len(carried), 1)
        self.assertLessEqual(
            len(carried[0]), CONTINUITY_STATEMENT_CHARS + len("turn 1: ")
        )
        self.assertTrue(carried[0].endswith("..."))

    def test_reasoning_blocks_are_counted_not_carried(self) -> None:
        groups = [
            [
                _assistant(
                    None,
                    blocks=[
                        {"kind": "thinking", "replayable": True},
                        {"kind": "thinking", "replayable": False},
                    ],
                )
            ]
        ]
        carried, stats = continuity_lines(groups)
        self.assertEqual(carried, ())
        self.assertEqual(stats.reasoning_blocks_dropped, 2)
        self.assertEqual(stats.replayable_blocks_dropped, 1)

    def test_empty_and_non_assistant_input(self) -> None:
        carried, stats = continuity_lines([])
        self.assertEqual(carried, ())
        self.assertEqual(stats.statements_carried, 0)
        carried, stats = continuity_lines(
            [[{"role": "tool", "content": "data"}, {"role": "user", "content": "hi"}]]
        )
        self.assertEqual(carried, ())

    def test_deterministic(self) -> None:
        groups = [[_assistant("Same input, same output.")]]
        self.assertEqual(continuity_lines(groups), continuity_lines(groups))


class _SilentProvider:
    provider_name = "test_provider"

    def complete(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError("summary tests never reach the provider")


class ContinuitySummaryWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "task",
            AccessProfile.WORKSPACE_WRITE,
            session_id="session",
        )
        self.origin = Origin(OriginKind.NATIVE_AGENT, "test-agent")
        self.policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        self.policy.set_grants(
            self.origin,
            {
                Capability.REPO_READ,
                Capability.REPO_WRITE,
                Capability.PROCESS_RUN,
                Capability.CHECKS_RUN,
                Capability.GIT_READ,
            },
        )
        self.core = ExtendedCoreRuntime(
            self.repository,
            self.policy,
            self.sessions,
            self.root / "audit.jsonl",
            verification_commands=[[sys.executable, "-c", "print('ok')"]],
        )
        self.kernel = AgentKernel(
            provider=_SilentProvider(),
            model="test-model",
            core=self.core,
            sessions=self.sessions,
            origin=self.origin,
            limits=AgentLimits(max_seconds=30),
            system_prompt=SYSTEM_PROMPT,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_summary_carries_labeled_conclusions(self) -> None:
        groups = [
            [
                _assistant("The bug lives in the digest refresh."),
                {"role": "tool", "content": "{}", "core_name": "repo.search"},
            ]
        ]
        summary = self.kernel._context_summary(groups)
        content = summary["content"]
        self.assertIn("quoted verbatim", content)
        self.assertIn("model narration, not verified evidence", content)
        self.assertIn("The bug lives in the digest refresh.", content)
        self.assertIn("Turns summarized: 1.", content)
        self.assertEqual(self.kernel._continuity_statements_carried, 1)
        self.assertGreater(self.kernel._continuity_chars_carried, 0)

    def test_summary_reports_dropped_reasoning_blocks(self) -> None:
        groups = [
            [
                _assistant(
                    "Decision recorded.",
                    blocks=[{"kind": "thinking", "replayable": True}],
                )
            ]
        ]
        summary = self.kernel._context_summary(groups)
        self.assertIn("reasoning blocks were dropped", summary["content"])

    def test_summary_without_conclusions_stays_clean(self) -> None:
        groups = [[{"role": "tool", "content": "{}", "core_name": "repo.search"}]]
        summary = self.kernel._context_summary(groups)
        self.assertNotIn("quoted verbatim", summary["content"])
        self.assertEqual(self.kernel._continuity_statements_carried, 0)


if __name__ == "__main__":
    unittest.main()
