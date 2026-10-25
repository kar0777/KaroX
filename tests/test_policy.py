"""Security policy contracts.

Ported from the v5 suite: capability profiles, origin-bound one-shot tokens,
risk classification, command mapping, the developer command guard, and the
safe child-environment allowlist.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Mapping
from unittest.mock import patch

import pytest

from karox.command_guard import DeveloperCommandBlocked, validate_developer_command_argv
from karox.models import AccessProfile, Capability, CoreCommand, Origin, OriginKind
from karox.policy import CapabilityPolicy, PolicyDenied
from karox.risk_engine import (
    REJECT_ALREADY_USED,
    REJECT_EXPIRED,
    REJECT_MISSING,
    REJECT_UNKNOWN,
    REJECT_WRONG_ACTION,
    REJECT_WRONG_SESSION,
    ConfirmationLedger,
    ConfirmationRejected,
    RiskAction,
    RiskEngine,
    RiskLevel,
    SmartStopRequired,
    build_bulk_preview,
    looks_like_system_path,
    risk_engine,
)
from karox.risk_mapping import action_for_command, extract_paths, risk_kind_for
from karox.security import _SAFE_CHILD_ENVIRONMENT, child_process_environment


class CapabilityPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.user = Origin(OriginKind.USER, "local-user")
        self.agent = Origin(OriginKind.NATIVE_AGENT, "agent")

    def test_profiles_grant_only_their_declared_capabilities(self) -> None:
        read_only = CapabilityPolicy(AccessProfile.READ_ONLY)
        self.assertTrue(read_only.decide(self.user, Capability.REPO_READ).allowed)
        self.assertTrue(read_only.decide(self.user, Capability.GIT_READ).allowed)
        self.assertFalse(read_only.decide(self.user, Capability.REPO_WRITE).allowed)

        elevated = CapabilityPolicy(AccessProfile.ELEVATED)
        self.assertTrue(elevated.decide(self.user, Capability.DESKTOP_INPUT).allowed)
        self.assertFalse(elevated.decide(self.user, Capability.GIT_PUSH).allowed)
        self.assertFalse(elevated.decide(self.user, Capability.PACKAGE_PUBLISH).allowed)

    def test_non_user_origins_are_deny_by_default_and_grants_are_profile_bounded(self) -> None:
        policy = CapabilityPolicy(AccessProfile.READ_ONLY)
        self.assertFalse(policy.decide(self.agent, Capability.REPO_READ).allowed)
        policy.set_grants(
            self.agent, {Capability.REPO_READ, Capability.REPO_WRITE}
        )
        self.assertTrue(policy.decide(self.agent, Capability.REPO_READ).allowed)
        self.assertFalse(policy.decide(self.agent, Capability.REPO_WRITE).allowed)
        policy.set_denies(self.agent, {Capability.REPO_READ})
        self.assertFalse(policy.decide(self.agent, Capability.REPO_READ).allowed)

    def test_explicit_token_is_origin_bound_expiring_and_one_shot(self) -> None:
        policy = CapabilityPolicy(AccessProfile.ELEVATED)
        token = "approval-token-unguessable"
        other = Origin(OriginKind.USER, "other-user")
        with patch("karox.policy.time.time", return_value=100.0):
            policy.add_token(token, self.user, {Capability.GIT_PUSH}, ttl_seconds=10)
        with patch("karox.policy.time.time", return_value=101.0):
            self.assertFalse(
                policy.decide(other, Capability.GIT_PUSH, token).allowed
            )
            self.assertTrue(policy.require(self.user, Capability.GIT_PUSH, token).allowed)
            with self.assertRaises(PolicyDenied):
                policy.require(self.user, Capability.GIT_PUSH, token)

        expired = "expired-token-unguessable"
        with patch("karox.policy.time.time", return_value=200.0):
            policy.add_token(expired, self.user, {Capability.GIT_PUSH}, ttl_seconds=5)
        with patch("karox.policy.time.time", return_value=206.0):
            decision = policy.decide(self.user, Capability.GIT_PUSH, expired)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "approval token expired")
        self.assertNotIn(expired, policy.explicit_tokens)

class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

def _read(**overrides: object) -> RiskAction:
    fields: dict[str, object] = {
        "kind": "repo.read",
        "session_id": "s-1",
        "summary": "read one file",
    }
    fields.update(overrides)
    return RiskAction(**fields)  # type: ignore[arg-type]

class RiskClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = RiskEngine()

    def test_reading_is_low_and_needs_nobody(self) -> None:
        assessment = self.engine.assess(_read())
        self.assertEqual(assessment.level, RiskLevel.LOW)
        self.assertFalse(assessment.requires_confirmation)

    def test_a_bounded_edit_is_medium_and_proceeds(self) -> None:
        action = _read(kind="repo.write", paths=("src/karox/core.py",), modify_count=1)
        assessment = self.engine.authorize(action)
        self.assertEqual(assessment.level, RiskLevel.MEDIUM)
        self.assertFalse(assessment.requires_confirmation)

    def test_an_unknown_action_kind_is_treated_as_high(self) -> None:
        # A tool added tomorrow must not bypass Smart Stop by not being listed.
        assessment = self.engine.assess(_read(kind="totally.new.tool"))
        self.assertEqual(assessment.level, RiskLevel.HIGH)
        self.assertTrue(assessment.requires_confirmation)
        self.assertIn("kind:unknown", assessment.reasons)

    def test_a_wide_edit_becomes_a_bulk_mutation(self) -> None:
        action = _read(
            kind="repo.write",
            paths=tuple(f"src/mod_{index}.py" for index in range(12)),
            modify_count=12,
        )
        assessment = self.engine.assess(action)
        self.assertEqual(assessment.level, RiskLevel.HIGH)
        self.assertIn("bulk_mutation", assessment.reasons)

    def test_many_test_targets_are_not_bulk_mutations(self) -> None:
        action = _read(
            kind="tests.run",
            paths=tuple(f"tests/test_{index}.py" for index in range(25)),
            repository_file_count=40,
        )
        assessment = self.engine.assess(action)
        self.assertEqual(assessment.level, RiskLevel.MEDIUM)
        self.assertFalse(assessment.requires_confirmation)
        self.assertNotIn("bulk_mutation", assessment.reasons)
        self.assertNotIn("large_repository_share", assessment.reasons)

    def test_a_recursive_delete_stops(self) -> None:
        action = _read(
            kind="repo.delete",
            summary="remove the build directory",
            paths=("build/",),
            delete_count=1,
            recursive=True,
        )
        assessment = self.engine.assess(action)
        self.assertEqual(assessment.level, RiskLevel.HIGH)
        self.assertIn("recursive_delete", assessment.reasons)
        self.assertTrue(assessment.requires_confirmation)

    def test_deleting_a_large_share_of_the_repository_stops(self) -> None:
        action = _read(
            kind="repo.delete",
            delete_count=60,
            repository_file_count=100,
        )
        assessment = self.engine.assess(action)
        self.assertIn("large_repository_share", assessment.reasons)
        self.assertEqual(assessment.preview["repository_fraction"], 0.6)

    def test_writing_to_a_system_path_is_critical(self) -> None:
        for path in (
            "C:/Windows/System32/drivers/etc/hosts",
            "/etc/passwd",
            "/usr/bin/python",
        ):
            with self.subTest(path=path):
                self.assertTrue(looks_like_system_path(path))
                assessment = self.engine.assess(
                    _read(kind="repo.write", paths=(path,), modify_count=1)
                )
                self.assertEqual(assessment.level, RiskLevel.CRITICAL)
                self.assertIn("system_path", assessment.reasons)

    def test_a_repository_path_is_not_mistaken_for_a_system_path(self) -> None:
        self.assertFalse(looks_like_system_path("src/karox/system_chrome.py"))
        self.assertFalse(looks_like_system_path("docs/etc/notes.md"))

    def test_doing_more_than_asked_is_its_own_hazard(self) -> None:
        action = _read(kind="repo.write", modify_count=1, beyond_user_request=True)
        assessment = self.engine.assess(action)
        self.assertEqual(assessment.level, RiskLevel.HIGH)
        self.assertIn("beyond_user_request", assessment.reasons)

class SourceIndependenceTests(unittest.TestCase):
    """The origin of an agent must never change the safety verdict."""

    def test_the_same_action_scores_identically_from_every_source(self) -> None:
        engine = RiskEngine()
        sources = (
            "openai_api",
            "anthropic_api",
            "gemini_api",
            "sponsor_api",
            "chatgpt_web",
            "claude_web",
            "mcp_client",
            "local_agent",
            "subagent",
        )
        verdicts = set()
        digests = set()
        for source in sources:
            assessment = engine.assess(
                _read(kind="repo.delete", delete_count=9, source=source)
            )
            verdicts.add((assessment.level, assessment.requires_confirmation))
            digests.add(assessment.action_digest)
        self.assertEqual(len(verdicts), 1)
        # The digest must also ignore the source, otherwise one human approval
        # would not cover the identical action from another front end.
        self.assertEqual(len(digests), 1)

class ConfirmationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.ledger = ConfirmationLedger(ttl_seconds=60.0, now=self.clock)
        self.engine = RiskEngine(ledger=self.ledger)
        self.action = _read(
            kind="repo.delete",
            summary="delete 40 generated files",
            paths=tuple(f"build/out_{index}.js" for index in range(40)),
            delete_count=40,
        )

    def test_a_model_cannot_confirm_its_own_action(self) -> None:
        with self.assertRaises(SmartStopRequired) as caught:
            self.engine.authorize(self.action)
        self.assertTrue(caught.exception.assessment.requires_confirmation)

    def test_a_forged_token_is_rejected(self) -> None:
        # This is the web-page case: a token scraped or invented from page
        # content or a tool result was never issued by the ledger.
        for forged in ("yes", "approved", "a" * 43, ""):
            with self.subTest(forged=forged):
                with self.assertRaises((ConfirmationRejected, SmartStopRequired)):
                    self.engine.authorize(self.action, confirmation_token=forged)

    def test_a_human_confirmation_lets_exactly_one_action_through(self) -> None:
        assessment = self.engine.assess(self.action)
        grant = self.ledger.issue(assessment)
        self.engine.authorize(self.action, confirmation_token=grant.token)
        with self.assertRaises(ConfirmationRejected) as caught:
            self.engine.authorize(self.action, confirmation_token=grant.token)
        self.assertEqual(caught.exception.reason, REJECT_ALREADY_USED)

    def test_changing_the_action_invalidates_the_confirmation(self) -> None:
        grant = self.ledger.issue(self.engine.assess(self.action))
        wider = _read(
            kind="repo.delete",
            summary="delete 40 generated files",
            paths=tuple(f"build/out_{index}.js" for index in range(41)),
            delete_count=41,
        )
        with self.assertRaises(ConfirmationRejected) as caught:
            self.engine.authorize(wider, confirmation_token=grant.token)
        self.assertEqual(caught.exception.reason, REJECT_WRONG_ACTION)

    def test_prose_alone_does_not_invalidate_a_confirmation(self) -> None:
        # Only what actually happens is part of the identity; a reworded
        # summary must not force the human to approve the same thing twice.
        grant = self.ledger.issue(self.engine.assess(self.action))
        reworded = RiskAction(
            kind=self.action.kind,
            session_id=self.action.session_id,
            summary="tidy up build output",
            source="chatgpt_web",
            paths=self.action.paths,
            delete_count=self.action.delete_count,
        )
        self.engine.authorize(reworded, confirmation_token=grant.token)

    def test_a_confirmation_from_another_session_is_refused(self) -> None:
        grant = self.ledger.issue(self.engine.assess(self.action))
        other = RiskAction(
            kind=self.action.kind,
            session_id="s-2",
            paths=self.action.paths,
            delete_count=self.action.delete_count,
        )
        with self.assertRaises(ConfirmationRejected) as caught:
            self.engine.authorize(other, confirmation_token=grant.token)
        # A different session changes the digest first, which is itself a
        # refusal; either reason is a correct stop.
        self.assertIn(
            caught.exception.reason, {REJECT_WRONG_ACTION, REJECT_WRONG_SESSION}
        )

    def test_a_confirmation_expires(self) -> None:
        grant = self.ledger.issue(self.engine.assess(self.action))
        self.clock.advance(61.0)
        with self.assertRaises(ConfirmationRejected) as caught:
            self.engine.authorize(self.action, confirmation_token=grant.token)
        self.assertEqual(caught.exception.reason, REJECT_EXPIRED)

    def test_missing_and_unknown_tokens_report_distinct_reasons(self) -> None:
        assessment = self.engine.assess(self.action)
        with self.assertRaises(ConfirmationRejected) as missing:
            self.ledger.redeem(None, assessment)
        self.assertEqual(missing.exception.reason, REJECT_MISSING)
        with self.assertRaises(ConfirmationRejected) as unknown:
            self.ledger.redeem("not-a-real-token", assessment)
        self.assertEqual(unknown.exception.reason, REJECT_UNKNOWN)

    def test_the_token_never_leaks_into_anything_a_model_can_read(self) -> None:
        assessment = self.engine.assess(self.action)
        grant = self.ledger.issue(assessment)
        exported = json.dumps(assessment.to_dict(), sort_keys=True, default=str)
        self.assertNotIn(grant.token, exported)
        self.assertNotIn(grant.token, json.dumps(grant.public_record(), default=str))

    def test_expired_and_used_entries_are_purged(self) -> None:
        assessment = self.engine.assess(self.action)
        self.ledger.issue(assessment)
        self.clock.advance(120.0)
        self.assertEqual(self.ledger.purge_expired(), 1)

class AlwaysBlockedTests(unittest.TestCase):
    """Publishing work outside the machine is never automatic."""

    def setUp(self) -> None:
        self.engine = RiskEngine()

    def test_push_publish_and_friends_are_critical_and_stop(self) -> None:
        for kind in (
            "git.push",
            "git.force_push",
            "git.reset_hard",
            "git.clean",
            "git.history_rewrite",
            "release.publish",
            "package.publish",
            "deploy",
            "payment",
            "billing.change",
            "subscription.change",
            "account.delete",
            "credential.export",
        ):
            with self.subTest(kind=kind):
                action = _read(kind=kind, summary=kind)
                assessment = self.engine.assess(action)
                self.assertEqual(assessment.level, RiskLevel.CRITICAL)
                with self.assertRaises(SmartStopRequired):
                    self.engine.authorize(action)

    def test_an_engine_cannot_be_configured_to_auto_approve_danger(self) -> None:
        for level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            with self.subTest(level=level.value):
                with self.assertRaises(ValueError):
                    RiskEngine(auto_approve_up_to=level)

class PreviewTests(unittest.TestCase):
    def test_the_preview_is_bounded_and_useful(self) -> None:
        action = _read(
            kind="repo.delete",
            paths=tuple(f"src/pkg/mod_{index}.py" for index in range(60)),
            delete_count=60,
            total_bytes=1_500_000,
            repository_file_count=200,
            reversible_by_checkpoint=True,
        )
        preview = build_bulk_preview(action)
        self.assertEqual(preview["file_count"], 60)
        self.assertEqual(len(preview["sample_paths"]), 20)
        self.assertTrue(preview["sample_truncated"])
        self.assertEqual(preview["deletions"], 60)
        self.assertEqual(preview["repository_fraction"], 0.3)
        self.assertTrue(preview["rollback_available"])
        self.assertEqual(preview["directories"], ["src/pkg"])

    def test_long_prose_is_clipped_so_a_confirmation_stays_readable(self) -> None:
        assessment = RiskEngine().assess(_read(summary="x" * 5000))
        self.assertLessEqual(len(assessment.headline), 500)

class SharedEngineTests(unittest.TestCase):
    def test_one_engine_governs_the_process(self) -> None:
        self.assertIs(risk_engine(), risk_engine())

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
            ("release.integrate_main_lineage", "git.commit"),
            ("release.check_prerelease", "git.read"),
            ("release.publish_prerelease", "browser.input"),
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

@pytest.mark.parametrize(
    "argv",
    [
        ["git", "push", "origin", "HEAD"],
        ["git", "commit", "-m", "bypass guarded commit"],
        ["cmd", "/c", "echo hidden"],
        ["powershell", "-Command", "Write-Output hidden"],
        ["npm", "publish"],
        ["pnpm", "login"],
        ["yarn", "npm", "publish"],
        ["twine", "upload", "dist/pkg.whl"],
        ["cargo", "publish"],
        ["gh", "auth", "login"],
        ["gh", "release", "create", "v1"],
        ["vercel", "deploy", "--prod"],
        ["netlify", "deploy", "--prod"],
        ["docker", "push", "example/image"],
        ["kubectl", "apply", "-f", "deployment.yaml"],
        ["terraform", "apply", "-auto-approve"],
        [sys.executable, "-c", "import os; os.system('git push origin HEAD')"],
        ["node", "-e", "require('child_process').execSync('npm publish')"],
    ],
)
def test_global_remote_side_effect_actions_are_blocked(argv: list[str]) -> None:
    with pytest.raises(DeveloperCommandBlocked):
        validate_developer_command_argv(argv)

@pytest.mark.parametrize(
    "argv",
    [
        [sys.executable, "-m", "pytest", "-q"],
        [sys.executable, "-c", "print('local')"],
        ["node", "-e", "console.log('local')"],
        ["npm", "install"],
        ["npm", "run", "build"],
        ["pip", "install", "build"],
        ["vercel", "status"],
        ["git", "status", "--short"],
        ["git", "-C", ".", "log", "-1"],
        ["gh", "pr", "view", "123"],
        ["docker", "build", "."],
    ],
)
def test_local_development_and_read_only_status_commands_remain_allowed(argv: list[str]) -> None:
    assert list(validate_developer_command_argv(argv)) == argv


class SafeChildEnvironmentAllowlistTests(unittest.TestCase):
    """A child gets a curated environment, never the host's secrets.

    ``child_process_environment`` is the seam between a verification command and
    the OS environment it runs in, so the allowlist is deny-by-default.
    """

    def _env(self, **extra: str) -> Mapping[str, str]:
        merged = {
            "PATH": "/usr/bin",
            "USERPROFILE": "C:\\Users\\x",
            "LOCALAPPDATA": "C:\\x",
        }
        merged.update(extra)
        return child_process_environment(source=merged)

    def test_playwright_browsers_path_is_forwarded(self) -> None:
        env = self._env(PLAYWRIGHT_BROWSERS_PATH=r"D:\DeveloperData\ms-playwright")
        self.assertEqual(
            env["PLAYWRIGHT_BROWSERS_PATH"], r"D:\DeveloperData\ms-playwright"
        )

    def test_node_options_is_not_forwarded(self) -> None:
        # Code-injection vector (--require / --import).
        env = self._env(NODE_OPTIONS="--require ./evil.mjs")
        self.assertNotIn("NODE_OPTIONS", env)

    def test_playwright_download_host_is_not_forwarded(self) -> None:
        # Download-source redirect: a child must not be pointed at an
        # attacker-controlled browser-binary host.
        env = self._env(PLAYWRIGHT_DOWNLOAD_HOST="https://evil.example/playwright")
        self.assertNotIn("PLAYWRIGHT_DOWNLOAD_HOST", env)

    def test_arbitrary_secret_env_is_not_forwarded(self) -> None:
        env = self._env(API_KEY="sk-supersecret", DATABASE_URL="postgres://u:p@h/db")
        self.assertNotIn("API_KEY", env)
        self.assertNotIn("DATABASE_URL", env)

    def test_baseline_benign_names_still_forwarded(self) -> None:
        env = self._env()
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["USERPROFILE"], "C:\\Users\\x")
        self.assertEqual(env["LOCALAPPDATA"], "C:\\x")

    def test_child_encoding_is_forced_to_utf8(self) -> None:
        env = self._env()
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONUTF8"], "1")

    def test_forced_encoding_overrides_a_hostile_host_value(self) -> None:
        env = self._env(PYTHONIOENCODING="cp866", PYTHONUTF8="0")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONUTF8"], "1")

    def test_allowlist_is_lowercase_insensitive_and_credential_free(self) -> None:
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH", _SAFE_CHILD_ENVIRONMENT)
        for name in _SAFE_CHILD_ENVIRONMENT:
            self.assertEqual(name, name.upper())
            self.assertNotIn("KEY", name)
            self.assertNotIn("SECRET", name)
            self.assertNotIn("TOKEN", name)
            self.assertNotIn("PASSWORD", name)
