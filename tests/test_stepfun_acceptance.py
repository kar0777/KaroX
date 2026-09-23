"""Offline safety and execution-path checks for the opt-in live harness."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from _support import ROOT
from _tui_harness import isolated_karox_directories
from karox.providers import ModelResponse, ToolCall

_SPEC = importlib.util.spec_from_file_location(
    "stepfun_acceptance", ROOT / "scripts/run_stepfun_acceptance.py"
)
assert _SPEC is not None and _SPEC.loader is not None
harness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(harness)


class FixtureProvider:
    provider_name = "stepfun-fixture"

    def __init__(self, answer='{"operation":"subtraction","observed":-1,"expected":5}'):
        self.requests = []
        self.answer = answer

    def complete(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return ModelResponse(None, (ToolCall(
                "read-source", "repo_read_file", '{"path":"sample.py"}'
            ),), "tool_calls", {"prompt_tokens": 3, "completion_tokens": 2})
        return ModelResponse(self.answer, (),
                             "stop", {"prompt_tokens": 4, "completion_tokens": 3})


class StepFunAcceptanceTests(unittest.TestCase):
    def test_readonly_lane_uses_real_core_and_requires_verified_tool_evidence(self):
        provider = FixtureProvider()
        with isolated_karox_directories(), tempfile.TemporaryDirectory() as temporary:
            with patch.object(harness, "OpenAIChatCompletionsProvider", return_value=provider):
                result = harness.acceptance_lane(Path(temporary), "unused-fixture-key",
                                                 "https://api.stepfun.ai/step_plan/v1", "review")
        self.assertEqual(result["status"], "passed", result)
        self.assertTrue(result["fixture_ok"])
        self.assertEqual(result["report"]["answer_basis"][0]["tool"], "repo.read_file")
        self.assertEqual(len(provider.requests), 2)
        self.assertTrue(all(r.model == "step-5-preview" for r in provider.requests))
        advertised = {tool.name for request in provider.requests for tool in request.tools}
        self.assertTrue(advertised.isdisjoint({"repo_write_file", "checks_run", "command_run"}))

    def test_verified_read_with_wrong_answer_cannot_pass_acceptance(self):
        provider = FixtureProvider('{"operation":"subtraction","observed":999,"expected":5}')
        with isolated_karox_directories(), tempfile.TemporaryDirectory() as temporary:
            with patch.object(harness, "OpenAIChatCompletionsProvider", return_value=provider):
                result = harness.acceptance_lane(Path(temporary), "unused-fixture-key",
                                                 "https://api.stepfun.ai/step_plan/v1", "review")
        self.assertTrue(result["report"]["verified"])
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["answer_ok"])

    def test_directory_listing_is_not_proof_of_reading_source(self):
        class ListingProvider(FixtureProvider):
            def complete(self, request):
                if not self.requests:
                    self.requests.append(request)
                    return ModelResponse(None, (ToolCall("list", "repo_list_files", "{}"),),
                                         "tool_calls", {})
                return super().complete(request)
        with isolated_karox_directories(), tempfile.TemporaryDirectory() as temporary:
            with patch.object(harness, "OpenAIChatCompletionsProvider", return_value=ListingProvider()):
                result = harness.acceptance_lane(Path(temporary), "unused-fixture-key",
                                                 "https://api.stepfun.ai/step_plan/v1", "review")
        self.assertTrue(result["answer_ok"])
        self.assertFalse(result["read_ok"])
        self.assertEqual(result["status"], "failed")

    def test_initialization_failure_is_failed_evidence_without_raw_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(harness.subprocess, "run", side_effect=subprocess.TimeoutExpired(
                "sensitive-command-value", 30
            )):
                result = harness.acceptance_lane(Path(temporary), "unused-fixture-key",
                                                 "https://api.stepfun.ai/step_plan/v1", "review")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_type"], "TimeoutExpired")
        self.assertNotIn("sensitive-command-value", str(result))


if __name__ == "__main__":
    unittest.main()
