from __future__ import annotations

import pytest

from _support import SRC  # noqa: F401
from karox.action_policy import (
    ActionConfirmationRequired,
    ActionDecisionEngine,
    ActionDisposition,
    ActionHardBlocked,
    ConsequenceClass,
    IntentScope,
)
from karox.risk_engine import ConfirmationRejected, RiskAction, RiskLevel


def action(kind: str, **kwargs: object) -> RiskAction:
    return RiskAction(kind=kind, session_id="session", **kwargs)  # type: ignore[arg-type]


def test_bulk_local_commit_is_guarded_auto_even_when_risk_is_high() -> None:
    engine = ActionDecisionEngine()
    decision = engine.decide(
        action(
            "git.commit",
            paths=tuple(f"src/file_{index}.py" for index in range(20)),
            modify_count=20,
        )
    )
    assert decision.assessment.level is RiskLevel.HIGH
    assert decision.disposition is ActionDisposition.GUARDED_AUTO
    assert decision.requires_confirmation is False
    assert "local_reversible_commit" in decision.reasons


def test_rebuildable_delete_does_not_need_a_second_user_round_trip() -> None:
    engine = ActionDecisionEngine()
    decision = engine.authorize(
        action(
            "repo.delete",
            paths=("frontend/node_modules", "build/cache"),
            delete_count=2,
            recursive=True,
        )
    )
    assert decision.disposition is ActionDisposition.GUARDED_AUTO
    assert decision.consequence is ConsequenceClass.REBUILDABLE
    assert decision.impact["consequence"] == "rebuildable"


def test_source_delete_needs_confirmation_without_delete_intent() -> None:
    engine = ActionDecisionEngine()
    pending = action(
        "repo.delete",
        paths=("src/legacy.py",),
        delete_count=1,
    )
    with pytest.raises(ActionConfirmationRequired) as stopped:
        engine.authorize(pending, user_intent="refactor the parser")
    assert stopped.value.decision.consequence is ConsequenceClass.WORKSPACE
    assert "delete_not_scoped_by_user" in stopped.value.decision.reasons


def test_explicit_user_delete_intent_scopes_workspace_deletion() -> None:
    engine = ActionDecisionEngine()
    pending = action(
        "repo.delete",
        paths=("src/legacy.py",),
        delete_count=1,
    )
    for task in (
        "delete the obsolete legacy implementation",
        "удали старую реализацию, она больше не нужна",
    ):
        decision = engine.authorize(pending, user_intent=task)
        assert decision.disposition is ActionDisposition.GUARDED_AUTO
        assert decision.intent_authorized is True


def test_checkpoint_turns_unrequested_workspace_delete_into_guarded_auto() -> None:
    engine = ActionDecisionEngine()
    pending = action(
        "repo.delete",
        paths=("src/legacy.py",),
        delete_count=1,
        reversible_by_checkpoint=True,
    )
    decision = engine.authorize(pending, user_intent="refactor the parser")
    assert decision.disposition is ActionDisposition.GUARDED_AUTO
    assert "rollback_checkpoint_available" in decision.reasons
    assert decision.impact["rollback_available"] is True


def test_checkpoint_never_scopes_personal_data_outside_repository() -> None:
    engine = ActionDecisionEngine()
    pending = action(
        "repo.delete",
        paths=(r"C:\Users\me\Documents\taxes.xlsx",),
        delete_count=1,
        outside_repository=True,
        reversible_by_checkpoint=True,
    )
    with pytest.raises(ActionConfirmationRequired):
        engine.authorize(pending, user_intent="refactor the parser")


def test_cleanup_intent_does_not_authorize_arbitrary_personal_data() -> None:
    engine = ActionDecisionEngine()
    pending = action(
        "repo.delete",
        paths=(r"C:\Users\me\Documents\taxes.xlsx",),
        delete_count=1,
        outside_repository=True,
    )
    with pytest.raises(ActionConfirmationRequired):
        engine.authorize(pending, user_intent="почисти диск от мусора")


def test_cache_name_outside_workspace_does_not_lower_personal_data_risk() -> None:
    engine = ActionDecisionEngine()
    pending = action(
        "repo.delete",
        paths=(r"C:\Users\me\Documents\cache",),
        delete_count=1,
        outside_repository=True,
    )
    decision = engine.decide(pending, user_intent="refactor the parser")
    assert decision.consequence is ConsequenceClass.USER_DATA
    assert decision.disposition is ActionDisposition.CONFIRM


def test_same_cache_is_rebuildable_when_selected_workspace_contains_it() -> None:
    engine = ActionDecisionEngine()
    pending = action(
        "repo.delete",
        paths=(r"C:\Users\me\AppData\Local\Tool\Cache",),
        delete_count=1,
        outside_repository=False,
    )
    decision = engine.authorize(pending, user_intent="почисти C: от мусора")
    assert decision.consequence is ConsequenceClass.REBUILDABLE
    assert decision.disposition is ActionDisposition.GUARDED_AUTO


def test_opaque_script_delete_is_not_assumed_repository_scoped() -> None:
    engine = ActionDecisionEngine()
    pending = action(
        "process.run_unknown",
        details={"deletion_requested": True, "deletion_paths": []},
    )
    decision = engine.decide(pending, user_intent="почисти проект от мусора")
    assert decision.consequence is ConsequenceClass.UNKNOWN
    assert decision.disposition is ActionDisposition.CONFIRM
    with pytest.raises(ActionConfirmationRequired):
        engine.authorize(pending, user_intent="почисти проект от мусора")


def test_named_personal_target_can_be_scoped_by_explicit_delete_request() -> None:
    engine = ActionDecisionEngine()
    pending = action(
        "repo.delete",
        paths=(r"C:\Users\me\Downloads\old-installer.exe",),
        delete_count=1,
        outside_repository=True,
    )
    decision = engine.authorize(
        pending,
        user_intent="удали old-installer.exe из Downloads",
    )
    assert decision.disposition is ActionDisposition.GUARDED_AUTO
    assert decision.intent_authorized is True


def test_system_path_is_a_hard_boundary_even_when_user_says_delete() -> None:
    engine = ActionDecisionEngine()
    pending = action(
        "repo.delete",
        paths=(r"C:\Windows\System32\drivers\etc\hosts",),
        delete_count=1,
        outside_repository=True,
    )
    with pytest.raises(ActionHardBlocked) as blocked:
        engine.authorize(pending, user_intent="delete that file")
    assert blocked.value.decision.disposition is ActionDisposition.HARD_BLOCK
    assert blocked.value.decision.consequence is ConsequenceClass.SYSTEM


def test_unknown_future_tool_is_confirmable_not_blanket_denied() -> None:
    engine = ActionDecisionEngine()
    pending = action("brand.new.mutator")
    decision = engine.decide(pending)
    assert decision.assessment.level is RiskLevel.HIGH
    assert decision.disposition is ActionDisposition.CONFIRM
    with pytest.raises(ActionConfirmationRequired):
        engine.authorize(pending)


def test_browser_send_keeps_exact_human_gate_even_when_user_asked_to_send() -> None:
    engine = ActionDecisionEngine()
    pending = action("browser.send_message")
    with pytest.raises(ActionConfirmationRequired):
        engine.authorize(pending, user_intent="draft a reply")
    with pytest.raises(ActionConfirmationRequired) as stopped:
        engine.authorize(pending, user_intent="send the reply to Alex")
    assert stopped.value.decision.intent_authorized is True
    grant = engine.risk.ledger.issue(stopped.value.decision.assessment)
    decision = engine.authorize(
        pending,
        user_intent="send the reply to Alex",
        confirmation_token=grant.token,
    )
    assert decision.disposition is ActionDisposition.CONFIRM
    assert decision.intent_authorized is True


def test_explicit_push_publish_and_deploy_still_require_one_shot_human_gate() -> None:
    engine = ActionDecisionEngine()
    for kind, task in (
        ("git.push", "push the verified branch to origin"),
        ("package.publish", "publish the package after tests pass"),
        ("release.publish", "publish the release"),
        ("deploy", "deploy this to production"),
    ):
        with pytest.raises(ActionConfirmationRequired) as stopped:
            engine.authorize(action(kind), user_intent=task)
        assert stopped.value.decision.intent_authorized is True
        grant = engine.risk.ledger.issue(stopped.value.decision.assessment)
        decision = engine.authorize(
            action(kind),
            user_intent=task,
            confirmation_token=grant.token,
        )
        assert decision.disposition is ActionDisposition.CONFIRM
        assert decision.intent_authorized is True


def test_force_push_requires_exact_force_intent_and_a_one_shot_human_gate() -> None:
    engine = ActionDecisionEngine()
    pending = action("git.force_push")
    with pytest.raises(ActionConfirmationRequired) as ordinary:
        engine.authorize(pending, user_intent="push the branch to origin")
    assert ordinary.value.decision.intent_authorized is False
    with pytest.raises(ActionConfirmationRequired) as stopped:
        engine.authorize(
            pending, user_intent="force-push the rewritten branch to origin"
        )
    assert stopped.value.decision.intent_authorized is True
    grant = engine.risk.ledger.issue(stopped.value.decision.assessment)
    decision = engine.authorize(
        pending,
        user_intent="force-push the rewritten branch to origin",
        confirmation_token=grant.token,
    )
    assert decision.disposition is ActionDisposition.CONFIRM
    assert decision.intent_authorized is True


def test_financial_or_account_effects_keep_exact_human_confirmation() -> None:
    engine = ActionDecisionEngine()
    pending = action("payment")
    with pytest.raises(ActionConfirmationRequired) as stopped:
        engine.authorize(pending, user_intent="send the payment")
    grant = engine.risk.ledger.issue(stopped.value.decision.assessment)
    decision = engine.authorize(
        pending,
        user_intent="send the payment",
        confirmation_token=grant.token,
    )
    assert decision.disposition is ActionDisposition.CONFIRM
    with pytest.raises(ConfirmationRejected):
        engine.authorize(
            pending,
            user_intent="send the payment",
            confirmation_token=grant.token,
        )


def test_intent_compiler_is_local_and_small() -> None:
    cleanup = IntentScope.compile("Почисти C: только от мусора и кэшей")
    assert cleanup.cleanup_requested
    assert cleanup.deletion_requested
    assert not cleanup.external_requested

    external = IntentScope.compile("publish the package after tests pass")
    assert external.external_requested
    assert not external.deletion_requested
