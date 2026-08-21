"""The Constitution is WIRED: the production request prompt is composed.

The seam proven here is ``cli._run_agent`` -> ``AgentKernel(system_prompt=...)``:
the prompt the kernel sends on the real request path (the request-only system
message every provider call carries) is built by
``karox.constitution.compose_system_prompt`` -- Constitution core, provider
delta, and the runtime operating contract in the byte-stable prefix; mode,
effort, project, skill, and research deltas after the boundary -- and the
composition is recorded in the report's ``project_context`` as evidence.
"""

from __future__ import annotations

import unittest
from unittest import mock

from _support import SRC, initialize_git_repository  # noqa: F401
from _tui_harness import isolated_karox_directories
from karox import cli
from karox.agent import SYSTEM_PROMPT
from karox.cli import _parser
from karox.constitution import CONSTITUTION_CORE, PROVIDER_DELTAS
from karox.project_context import ProjectContext


def _args(repository, *extra):
    return _parser().parse_args(
        [
            "agent",
            "run",
            "--repository",
            str(repository),
            "--task",
            "fix feature",
            "--verification-command",
            '["python","-m","pytest"]',
            *extra,
        ]
    )


class ConstitutionWiringTests(unittest.TestCase):
    def _kernel_kwargs(self, *extra_argv):
        with isolated_karox_directories() as repository:
            initialize_git_repository(repository)
            project = ProjectContext(
                instructions="",
                environment="<environment>fixture</environment>",
                project_map="<project-map>fixture</project-map>",
                project_map_metadata={
                    "enabled": True,
                    "implementation": [],
                    "tests": [],
                    "docs": [],
                },
            )
            provider = mock.Mock()
            with (
                mock.patch(
                    "karox.cli._agent_provider",
                    return_value=(provider, "model-x", 32000, 4096),
                ),
                mock.patch(
                    "karox.cli.discover_project_context", return_value=project
                ),
                mock.patch(
                    "karox.cli._selected_mcp_runtime", return_value=(None, None)
                ),
                mock.patch(
                    "karox.transcript_shadow.make_transcript_observer",
                    return_value=None,
                ),
                mock.patch("karox.cli.AgentKernel") as kernel,
            ):
                # A report-shaped stand-in: plan/ideate runs walk the mode
                # artifact path, which reads provider_message from it.
                run_report = mock.Mock(provider_message="", project_context={})
                kernel.return_value.run.return_value = run_report
                result = cli._run_agent(_args(repository, *extra_argv))
                self.assertIs(result, run_report)
                return kernel.call_args.kwargs

    def test_request_prompt_is_composed_with_stable_prefix_order(self):
        kwargs = self._kernel_kwargs()
        prompt = kwargs["system_prompt"]
        core_at = prompt.find("# KaroX Agent Constitution")
        provider_at = prompt.find("## Provider:")
        runtime_at = prompt.find("bounded KaroX Core Runtime")
        project_at = prompt.find("<environment>fixture</environment>")
        self.assertEqual(core_at, 0)
        self.assertTrue(0 < provider_at < runtime_at < project_at)
        # The runtime operating contract arrives whole, not summarized.
        self.assertIn(SYSTEM_PROMPT, prompt)

    def test_composition_is_recorded_as_evidence(self):
        kwargs = self._kernel_kwargs()
        recorded = kwargs["project_context"]["constitution"]
        self.assertEqual(
            recorded["sections"][:3], ["core", "provider", "runtime"]
        )
        self.assertIn("project", recorded["sections"])
        self.assertRegex(recorded["prefix_sha256"], r"^[0-9a-f]{64}$")
        self.assertGreater(recorded["prefix_chars"], len(CONSTITUTION_CORE))
        self.assertGreater(recorded["dynamic_chars"], 0)

    def test_mode_and_effort_deltas_join_after_the_boundary(self):
        kwargs = self._kernel_kwargs("--mode", "plan", "--effort-level", "high")
        prompt = kwargs["system_prompt"]
        self.assertIn("Do not mutate production code", prompt)
        self.assertIn("Effort high:", prompt)
        recorded = kwargs["project_context"]["constitution"]
        self.assertIn("mode", recorded["sections"])
        self.assertIn("effort", recorded["sections"])
        # Deltas are dynamic: they join after the stable prefix boundary.
        self.assertGreater(
            prompt.find("Do not mutate production code"),
            prompt.find("bounded KaroX Core Runtime"),
        )

    def test_unrouted_run_gets_the_generic_delta(self):
        kwargs = self._kernel_kwargs()
        self.assertIn(PROVIDER_DELTAS["generic"], kwargs["system_prompt"])

    def test_legacy_run_without_flags_has_no_mode_or_effort_sections(self):
        kwargs = self._kernel_kwargs()
        sections = kwargs["project_context"]["constitution"]["sections"]
        self.assertNotIn("mode", sections)
        self.assertNotIn("effort", sections)


if __name__ == "__main__":
    unittest.main()
