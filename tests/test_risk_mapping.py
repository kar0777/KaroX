"""Core commands must reach Smart Stop with the right shape.

The mapping is where a dangerous action gets its true size. A batch that edits
fifty files must not arrive at the RiskEngine looking like one bounded write,
and a `process.run` that happens to be `git push` must not arrive looking like
an ordinary subprocess.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.models import CoreCommand, Origin, OriginKind
from karox.risk_engine import RiskEngine, RiskLevel, SmartStopRequired
from karox.risk_mapping import (
    action_for_command,
    extract_paths,
    risk_kind_for,
)


def _command(name: str, arguments: dict | None = None) -> CoreCommand:
    return CoreCommand(
        name=name,
        arguments=arguments or {},
        session_id="s-1",
        origin=Origin(kind=OriginKind.NATIVE_AGENT, identity="tester"),
    )


class RiskKindTests(unittest.TestCase):
    def test_reads_and_writes_map_to_their_tiers(self) -> None:
        for name, expected in (
            ("repo.read_file", "repo.read"),
            ("repo.search", "repo.search"),
            ("repo.write_file", "repo.write"),
            ("repo.edit_file", "repo.write"),
            ("git.status", "git.read"),
            ("git.commit", "git.commit"),
            ("tests.run", "tests.run"),
        ):
            with self.subTest(name=name):
                self.assertEqual(risk_kind_for(name, {}), expected)

    def test_an_unmapped_command_keeps_its_name_and_lands_as_high(self) -> None:
        kind = risk_kind_for("some.future.tool", {})
        self.assertEqual(kind, "some.future.tool")
        assessment = RiskEngine().assess(
            action_for_command(_command("some.future.tool"))
        )
        self.assertEqual(assessment.level, RiskLevel.HIGH)

    def test_an_outbound_mcp_call_is_never_treated_as_a_read(self) -> None:
        self.assertEqual(risk_kind_for("mcp.clickup.create_task", {}), "mcp.call")


class BrowserMappingTests(unittest.TestCase):
    def test_the_stable_browser_surface_is_scored_per_action(self) -> None:
        for action, expected in (
            ("snapshot", "browser.snapshot"),
            ("get_text", "browser.read"),
            ("click", "browser.input"),
            ("upload", "browser.upload"),
            ("screenshot", "browser.screenshot"),
        ):
            with self.subTest(action=action):
                self.assertEqual(
                    risk_kind_for("browser.command", {"action": action}), expected
                )

    def test_an_unnamed_browser_action_is_not_assumed_safe(self) -> None:
        self.assertEqual(risk_kind_for("browser.command", {}), "browser.input")
        self.assertEqual(
            risk_kind_for("browser.command", {"action": "invented"}), "browser.input"
        )

    def test_uploading_a_file_stops(self) -> None:
        engine = RiskEngine()
        action = action_for_command(
            _command("browser.command", {"action": "upload", "path": "a.pdf"})
        )
        self.assertEqual(engine.assess(action).level, RiskLevel.HIGH)


class ProcessMappingTests(unittest.TestCase):
    def test_git_push_hiding_inside_process_run_is_still_a_push(self) -> None:
        kind = risk_kind_for("process.run", {"argv": ["git", "push", "origin", "main"]})
        self.assertEqual(kind, "git.push")

    def test_full_dev_command_keeps_the_semantic_risk_of_its_argv(self) -> None:
        self.assertEqual(
            risk_kind_for("dev.command", {"argv": ["git", "push", "origin", "main"]}),
            "git.push",
        )
        self.assertEqual(
            risk_kind_for("dev.command", {"argv": ["npm", "publish"]}),
            "package.publish",
        )
        self.assertEqual(
            risk_kind_for("dev.command", {"argv": ["npm", "install"]}),
            "package.install",
        )

    def test_direct_delete_argv_exposes_the_real_target_to_the_action_engine(self) -> None:
        action = action_for_command(
            _command("dev.command", {"argv": ["rm", "-rf", "build/cache"]})
        )
        self.assertTrue(action.details["deletion_requested"])
        self.assertEqual(action.details["deletion_paths"], ["build/cache"])
        self.assertEqual(action.paths, ("build/cache",))

    @unittest.skipUnless(os.name == "nt", "drive-letter paths are Windows semantics")
    def test_absolute_delete_target_is_marked_outside_repository(self) -> None:
        action = action_for_command(
            _command("dev.command", {"argv": ["rm", "C:/Users/me/old.tmp"]}),
            repository=Path("C:/work/repo"),
        )
        self.assertTrue(action.outside_repository)

    def test_a_forced_push_is_recognised_separately(self) -> None:
        kind = risk_kind_for(
            "process.run", {"argv": ["git", "push", "--force", "origin", "main"]}
        )
        self.assertEqual(kind, "git.force_push")

    def test_a_windows_executable_path_is_still_recognised(self) -> None:
        kind = risk_kind_for(
            "process.run", {"argv": ["C:\\Program Files\\Git\\bin\\git.exe", "clean"]}
        )
        self.assertEqual(kind, "git.clean")

    def test_a_hard_reset_is_destructive_but_a_soft_reset_is_not(self) -> None:
        self.assertEqual(
            risk_kind_for("process.run", {"argv": ["git", "reset", "--hard"]}),
            "git.reset_hard",
        )
        self.assertEqual(
            risk_kind_for("process.run", {"argv": ["git", "reset", "HEAD~1"]}),
            "git.read",
        )

    def test_creating_a_tag_is_not_deleting_one(self) -> None:
        self.assertEqual(
            risk_kind_for("process.run", {"argv": ["git", "tag", "v5.0.0"]}),
            "process.run_unknown",
        )
        self.assertEqual(
            risk_kind_for("process.run", {"argv": ["git", "tag", "-d", "v5.0.0"]}),
            "git.tag_delete",
        )

    def test_publishing_a_package_is_recognised(self) -> None:
        for argv, expected in (
            (["npm", "publish"], "package.publish"),
            (["twine", "upload", "dist/*"], "package.publish"),
            (["pip", "install", "requests"], "package.install"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(risk_kind_for("process.run", {"argv": argv}), expected)

    def test_an_unknown_subprocess_is_high_risk(self) -> None:
        engine = RiskEngine()
        action = action_for_command(
            _command("process.run", {"argv": ["mystery-binary", "--go"]})
        )
        self.assertEqual(engine.assess(action).level, RiskLevel.HIGH)


class PathExtractionTests(unittest.TestCase):
    def test_single_and_plural_path_keys_are_both_found(self) -> None:
        self.assertEqual(extract_paths({"path": "a.py"}), ("a.py",))
        self.assertEqual(extract_paths({"paths": ["a.py", "b.py"]}), ("a.py", "b.py"))

    def test_a_batch_payload_does_not_hide_its_size(self) -> None:
        arguments = {
            "operations": [
                {"operation": "write", "path": f"src/mod_{index}.py"}
                for index in range(30)
            ]
        }
        self.assertEqual(len(extract_paths(arguments)), 30)

    def test_duplicates_are_collapsed_but_order_is_kept(self) -> None:
        arguments = {"paths": ["b.py", "a.py", "b.py"]}
        self.assertEqual(extract_paths(arguments), ("b.py", "a.py"))

    def test_non_string_payload_values_are_ignored(self) -> None:
        self.assertEqual(extract_paths({"paths": [1, None, "ok.py"]}), ("ok.py",))


class ActionShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = RiskEngine()

    def test_one_file_edit_stays_a_bounded_medium_action(self) -> None:
        action = action_for_command(
            _command("repo.edit_file", {"path": "src/karox/core.py"})
        )
        self.assertEqual(action.modify_count, 1)
        self.assertEqual(action.target, "src/karox/core.py")
        self.assertEqual(self.engine.assess(action).level, RiskLevel.MEDIUM)

    def test_a_large_batch_reaches_smart_stop_as_a_bulk_mutation(self) -> None:
        arguments = {
            "operations": [
                {"operation": "write", "path": f"src/mod_{index}.py"}
                for index in range(30)
            ]
        }
        action = action_for_command(_command("repo.command", arguments))
        assessment = self.engine.assess(action)
        self.assertEqual(action.modify_count, 30)
        self.assertIn("bulk_mutation", assessment.reasons)
        with self.assertRaises(SmartStopRequired):
            self.engine.authorize(action)

    def test_batch_deletions_are_counted_as_deletions(self) -> None:
        arguments = {
            "operations": [
                {"operation": "delete", "path": f"build/out_{index}.js"}
                for index in range(8)
            ]
        }
        action = action_for_command(_command("repo.command", arguments))
        self.assertEqual(action.delete_count, 8)
        self.assertIn("bulk_delete", self.engine.assess(action).reasons)

    def test_a_recursive_flag_or_glob_is_detected(self) -> None:
        flagged = action_for_command(
            _command("repo.delete_file", {"path": "build", "recursive": True})
        )
        globbed = action_for_command(_command("repo.delete_file", {"path": "build/**"}))
        self.assertTrue(flagged.recursive)
        self.assertTrue(globbed.recursive)
        self.assertIn("recursive_delete", self.engine.assess(flagged).reasons)

    def test_the_repository_share_is_carried_through(self) -> None:
        action = action_for_command(
            _command(
                "repo.command",
                {
                    "operations": [
                        {"operation": "write", "path": f"m{index}.py"}
                        for index in range(6)
                    ]
                },
            ),
            repository_file_count=12,
        )
        assessment = self.engine.assess(action)
        self.assertIn("large_repository_share", assessment.reasons)
        self.assertEqual(assessment.preview["repository_fraction"], 0.5)

    def test_the_origin_is_recorded_but_does_not_change_the_verdict(self) -> None:
        arguments = {"path": "build", "recursive": True}
        verdicts = set()
        for identity in ("chatgpt-web", "openai-api", "clickup-mcp", "subagent-7"):
            command = CoreCommand(
                name="repo.delete_file",
                arguments=arguments,
                session_id="s-1",
                origin=Origin(kind=OriginKind.HOSTED_CLIENT, identity=identity),
            )
            action = action_for_command(command)
            self.assertEqual(action.source, command.origin.key)
            verdicts.add(self.engine.assess(action).level)
        self.assertEqual(len(verdicts), 1)


if __name__ == "__main__":
    unittest.main()
