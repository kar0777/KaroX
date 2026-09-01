from __future__ import annotations

from karox.orchestration_presenter import preset_label, render_plan, render_run, role_label


def _plan() -> dict:
    return {
        "run_id": "run-internal-id",
        "policy": {"preset": "balanced"},
        "orchestrator_endpoint": {"endpoint_id": "api:provider:orch", "display_name": "Strong Orchestrator"},
        "steps": [
            {"step": {"step_id": "plan", "role": "planner"}, "endpoint": {"endpoint_id": "api:provider:orch", "display_name": "Strong Orchestrator"}, "effort_level": "high"},
            {"step": {"step_id": "implement", "role": "implementer"}, "endpoint": {"endpoint_id": "sub:codex", "display_name": "Codex subscription"}, "effort_level": "medium"},
            {"step": {"step_id": "review", "role": "reviewer"}, "endpoint": {"endpoint_id": "sub:claude", "display_name": "Claude subscription"}, "effort_level": "high"},
        ],
    }


def test_labels_translate_internal_vocabulary() -> None:
    english = preset_label("balanced", "en")
    russian = preset_label("balanced", "ru")
    assert "Balanced" in english
    assert "Баланс" in russian
    assert "premium" not in english.casefold()
    assert "premium" not in russian.casefold()
    assert "routing" not in preset_label("custom", "en").casefold()
    assert "маршрут" not in preset_label("custom", "ru").casefold()
    assert role_label("implementer", "en") == "Implementer"
    assert role_label("reviewer", "ru") == "Независимая проверка"


def test_plan_default_is_team_first_and_hides_internal_metadata() -> None:
    text = render_plan(_plan(), "en")
    assert "KaroX plan" in text
    assert "Implementer → Codex subscription" in text
    assert "Independent reviewer → Claude subscription" in text
    assert "api:provider:orch" not in text
    assert "sub:codex" not in text
    assert "run-internal-id" not in text
    assert "effort high" not in text


def test_plan_details_preserve_exact_metadata() -> None:
    text = render_plan(_plan(), "en", details=True)
    assert "effort high" in text
    assert "Run ID: run-internal-id" in text
    assert "implement" in text


def test_run_default_prioritizes_outcome_team_cost_and_saving() -> None:
    result = {
        "plan": _plan(),
        "status": "passed",
        "total_tokens": 123456,
        "total_cost_usd": 1.25,
        "steps": [
            {"step_id": "implement", "endpoint_id": "sub:codex", "status": "passed"},
            {"step_id": "review", "endpoint_id": "sub:claude", "status": "passed"},
        ],
        "savings_receipt": {
            "actual_cost": {"amount": 1.25, "currency": "USD", "evidence": "estimated"},
            "savings": {"amount": 3.30, "currency": "USD", "evidence": "estimated"},
            "savings_percent": 72.5,
        },
    }
    text = render_run(result, "en")
    assert "KaroX: passed" in text
    assert "Implementer → Codex subscription" in text
    assert "AI cost estimate: $1.2500" in text
    assert "Estimated saving: 72.5%" in text
    assert "123456" not in text
    assert "run-internal-id" not in text
    details = render_run(result, "en", details=True)
    assert "Tokens: 123456" in details
    assert "Run ID: run-internal-id" in details


def test_run_uses_measured_labels_only_for_measured_money() -> None:
    result = {
        "plan": _plan(),
        "status": "passed",
        "total_cost_usd": 1.0,
        "steps": [],
        "savings_receipt": {
            "actual_cost": {"amount": 1.0, "currency": "USD", "evidence": "measured"},
            "savings": {"amount": 1.0, "currency": "USD", "evidence": "measured"},
            "savings_percent": 50.0,
        },
    }
    text = render_run(result, "en")
    assert "Measured AI cost: $1.0000" in text
    assert "Measured saving: 50.0%" in text
