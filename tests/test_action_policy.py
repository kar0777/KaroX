from __future__ import annotations

import pytest

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
    assert "destructive_delete_requires_user" in stopped.value.decision.reasons


def test_explicit_user_delete_intent_still_needs_confirmation_outside_bypass() -> None:
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
        with pytest.raises(ActionConfirmationRequired) as stopped:
            engine.authorize(pending, user_intent=task)
        assert stopped.value.decision.intent_authorized is True
        assert "destructive_delete_requires_user" in stopped.value.decision.reasons


def test_checkpoint_does_not_silently_approve_source_deletion() -> None:
    engine = ActionDecisionEngine()
    pending = action(
        "repo.delete",
        paths=("src/legacy.py",),
        delete_count=1,
        reversible_by_checkpoint=True,
    )
    with pytest.raises(ActionConfirmationRequired) as stopped:
        engine.authorize(pending, user_intent="refactor the parser")
    assert stopped.value.decision.impact["rollback_available"] is True
    assert "destructive_delete_requires_user" in stopped.value.decision.reasons


def test_bypass_allows_autonomous_workspace_deletion_only() -> None:
    engine = ActionDecisionEngine()
    pending = action(
        "repo.delete",
        paths=("src/legacy.py",),
        delete_count=1,
    )
    decision = engine.authorize(
        pending,
        user_intent="refactor the parser",
        bypass_mode=True,
    )
    assert decision.disposition is ActionDisposition.GUARDED_AUTO
    assert decision.consequence is ConsequenceClass.WORKSPACE
    assert "bypass_workspace_delete" in decision.reasons


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
    with pytest.raises(ActionConfirmationRequired) as stopped:
        engine.authorize(
            pending,
            user_intent="удали old-installer.exe из Downloads",
        )
    assert stopped.value.decision.intent_authorized is True
    assert stopped.value.decision.consequence is ConsequenceClass.USER_DATA


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


@pytest.mark.parametrize(
    "name",
    ["skill.list", "skill.load", "skill.read", "visual.check"],
)
def test_the_v7_read_tools_are_observations_not_confirmations(name: str) -> None:
    """A tool the prompt offers and the policy permits must not need approval.

    Measured, not hypothetical: the first v7 arena run answered its own
    ``skill_load`` with ``ActionConfirmationRequired`` -- an unmapped command
    name is classified unknown-and-therefore-high, so a read of a recipe the run
    was told about, and a look at the page it just wrote, both arrived as
    consequences somebody had to approve. These four names are the fix, and this
    test is what keeps them named.
    """

    from karox.risk_mapping import action_for_command, risk_kind_for
    from karox.models import CoreCommand, Origin, OriginKind

    command = CoreCommand(
        name=name,
        arguments={"name": "web-3d-scene"} if name.startswith("skill.") else {"path": "index.html"},
        session_id="session",
        origin=Origin(OriginKind.NATIVE_AGENT, "test-agent"),
        idempotency_key=f"{name}-1",
    )
    assert risk_kind_for(name, command.arguments) == name
    mapped = action_for_command(command)
    assert mapped.kind == name
    decision = ActionDecisionEngine().decide(mapped, user_intent="build the page")
    assert decision.consequence is ConsequenceClass.OBSERVATION
    assert decision.disposition is ActionDisposition.AUTO


def test_skill_run_is_a_workspace_process_not_an_unknown_confirmation() -> None:
    """The advertised leased ``skill.run`` tool must be executable.

    Its name had no risk mapping, so every call was classified
    unknown-and-therefore-high and the engine's typed-tool path could never
    redeem it. A skill script is a repository-owned recipe the catalogue
    validated, so it sits at the dev-command level: medium risk, workspace
    consequence, no user round-trip when the session holds ``process.run``.
    """
    from karox.models import CoreCommand, Origin, OriginKind
    from karox.risk_mapping import action_for_command, risk_kind_for

    command = CoreCommand(
        name="skill.run",
        arguments={"name": "web-3d-scene"},
        session_id="session",
        origin=Origin(OriginKind.NATIVE_AGENT, "test-agent"),
        idempotency_key="skill-run-1",
    )
    assert risk_kind_for("skill.run", command.arguments) == "process.run_dev"
    mapped = action_for_command(command)
    assert mapped.kind == "process.run_dev"
    decision = ActionDecisionEngine().decide(mapped, user_intent="run the skill")
    assert decision.consequence is ConsequenceClass.WORKSPACE
    assert decision.disposition is not ActionDisposition.CONFIRM
