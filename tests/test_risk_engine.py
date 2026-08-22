"""Smart Stop must behave identically for every agent source.

These tests pin the scenarios from the frozen scope that decide whether KaroX
is safe to run unattended: a dangerous bulk delete stops, a model cannot
confirm its own action, a web page cannot forge a confirmation, and push and
publish stay blocked.
"""

from __future__ import annotations

import json
import unittest

from _support import SRC  # noqa: F401 - inserts src on sys.path

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


if __name__ == "__main__":
    unittest.main()
