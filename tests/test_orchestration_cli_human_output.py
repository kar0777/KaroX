from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

from karox import orchestration_cli


class FakePlan:
    def to_dict(self) -> dict:
        return {
            "run_id": "run-internal",
            "policy": {"preset": "balanced"},
            "orchestrator_endpoint": {
                "endpoint_id": "api:openai:strong",
                "display_name": "Strong Orchestrator",
            },
            "steps": [
                {
                    "step": {"step_id": "implement", "role": "implementer"},
                    "endpoint": {
                        "endpoint_id": "sub:codex",
                        "display_name": "Codex subscription",
                    },
                    "effort_level": "high",
                }
            ],
        }


def _args(*, json_output: bool) -> argparse.Namespace:
    return argparse.Namespace(orchestrate_command="plan", json=json_output)


def test_plan_plain_output_uses_human_presenter(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        orchestration_cli,
        "_plan_selection_from_args",
        lambda _args: SimpleNamespace(plan=FakePlan(), delegation_dict=lambda: None),
    )
    assert orchestration_cli._handle_orchestrate(_args(json_output=False)) == 0
    text = capsys.readouterr().out
    assert "KaroX plan" in text
    assert "Implementer → Codex subscription" in text
    assert "api:openai:strong" not in text
    assert "run-internal" not in text
    assert "effort high" not in text


def test_plan_json_output_preserves_full_machine_contract(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        orchestration_cli,
        "_plan_selection_from_args",
        lambda _args: SimpleNamespace(plan=FakePlan(), delegation_dict=lambda: None),
    )
    assert orchestration_cli._handle_orchestrate(_args(json_output=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "run-internal"
    assert payload["orchestrator_endpoint"]["endpoint_id"] == "api:openai:strong"
    assert payload["steps"][0]["effort_level"] == "high"
