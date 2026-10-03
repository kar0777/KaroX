from __future__ import annotations

import unittest

from karox.providers import ModelMessage
from karox.semantic_shadow import (
    ShadowAuthority,
    ShadowConfidence,
    ShadowEffectKind,
    ShadowSnapshot,
    ShadowWouldCall,
    audit_history_delta,
    classify_before_model,
    request_char_counts,
    summarize_shadow_usage,
)


def snapshot(**overrides: object) -> ShadowSnapshot:
    values: dict[str, object] = {
        "step": 2,
        "session_revision": 7,
        "phase": "execution",
        "status": "active",
        "repo_fingerprint": "repo-fingerprint",
        "branch": "main",
        "changed_files": (),
        "requirements_digest": "requirements-digest",
        "active_requirement_ids": ("R1",),
        "active_requirement_bytes": 20,
        "provider_history_entries": 4,
        "request_message_count": 3,
        "request_chars": 100,
        "request_dynamic_chars": 40,
        "advertised_tool_count": 8,
        "advertised_tool_schema_bytes": 500,
        "checks_count": 0,
        "failures_count": 0,
        "evidence_count": 0,
        "jobs_count": 0,
        "unfinished_actions_count": 0,
        "plan_todo_count": 0,
        "plan_doing_count": 0,
        "plan_done_count": 0,
        "verified_snapshot_present": False,
        "verification_complete": False,
        "answer_complete": False,
        "awaiting_visual_review": False,
        "changed_once": False,
        "require_change": True,
    }
    values.update(overrides)
    return ShadowSnapshot(**values)  # type: ignore[arg-type]


class SemanticShadowTests(unittest.TestCase):
    def test_snapshot_fingerprint_is_deterministic_and_state_sensitive(self) -> None:
        left = snapshot()
        right = snapshot()

        self.assertEqual(left.fingerprint(), right.fingerprint())
        self.assertNotEqual(left.fingerprint(), snapshot(step=3).fingerprint())

    def test_unknown_state_fails_safe(self) -> None:
        decision = classify_before_model(snapshot())

        self.assertEqual(decision.would_call_model, ShadowWouldCall.UNKNOWN)
        self.assertEqual(decision.authority_gap, ShadowAuthority.UNKNOWN)
        self.assertEqual(decision.confidence, ShadowConfidence.UNKNOWN)

    def test_first_step_is_semantic_required(self) -> None:
        decision = classify_before_model(snapshot(step=1))

        self.assertEqual(decision.would_call_model, ShadowWouldCall.YES)
        self.assertEqual(decision.authority_gap, ShadowAuthority.SEMANTIC)
        self.assertEqual(decision.confidence, ShadowConfidence.SEMANTIC_REQUIRED)

    def test_verified_state_is_certain_runtime_completion(self) -> None:
        decision = classify_before_model(snapshot(verification_complete=True))

        self.assertEqual(decision.would_call_model, ShadowWouldCall.NO)
        self.assertEqual(decision.authority_gap, ShadowAuthority.RUNTIME)
        self.assertEqual(decision.confidence, ShadowConfidence.CERTAIN_MECHANICAL)
        self.assertEqual(
            decision.available_runtime_transitions,
            ("finish_from_verified_state",),
        )

    def test_answer_state_is_certain_runtime_completion(self) -> None:
        decision = classify_before_model(snapshot(answer_complete=True))

        self.assertEqual(decision.would_call_model, ShadowWouldCall.NO)
        self.assertEqual(
            decision.available_runtime_transitions,
            ("finish_from_answer_state",),
        )

    def test_known_runtime_transition_is_certain_mechanical(self) -> None:
        decision = classify_before_model(
            snapshot(
                runtime_transitions=("checks.run", "git.status", "git.diff"),
            )
        )

        self.assertEqual(decision.would_call_model, ShadowWouldCall.NO)
        self.assertEqual(decision.authority_gap, ShadowAuthority.RUNTIME)
        self.assertEqual(decision.confidence, ShadowConfidence.CERTAIN_MECHANICAL)
        self.assertEqual(
            decision.available_runtime_transitions,
            ("checks.run", "git.status", "git.diff"),
        )

    def test_visual_review_dominates_verified_state(self) -> None:
        decision = classify_before_model(
            snapshot(
                verification_complete=True,
                awaiting_visual_review=True,
            )
        )

        self.assertEqual(decision.would_call_model, ShadowWouldCall.YES)
        self.assertEqual(decision.authority_gap, ShadowAuthority.SEMANTIC)

    def test_request_char_counts_separate_dynamic_tail(self) -> None:
        total, dynamic = request_char_counts(
            (
                ModelMessage(role="system", content="system"),
                ModelMessage(role="user", content="hello"),
                ModelMessage(role="assistant", content="world"),
            )
        )

        self.assertEqual(total, len("systemhelloworld"))
        self.assertEqual(dynamic, len("helloworld"))

    def test_observation_only_effect(self) -> None:
        effect = audit_history_delta(
            snapshot=snapshot(),
            new_entries=(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "call_id": "read",
                            "name": "repo_read_file",
                            "raw_arguments": '{"path":"a.py"}',
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "read",
                    "core_name": "repo.read_file",
                    "result": {
                        "ok": True,
                        "mutation": False,
                        "data": {
                            "path": "a.py",
                            "content_sha256": "a" * 64,
                        },
                        "evidence": [],
                    },
                },
            ),
        )

        self.assertEqual(effect.kind, ShadowEffectKind.OBSERVATION_ONLY)
        self.assertEqual(effect.read_calls, 1)
        self.assertEqual(effect.tool_calls, 1)
        self.assertFalse(effect.changed)
        self.assertEqual(len(effect.read_result_signatures), 1)

    def test_proof_only_effect(self) -> None:
        effect = audit_history_delta(
            snapshot=snapshot(),
            new_entries=(
                {
                    "role": "tool",
                    "core_name": "checks.run",
                    "result": {
                        "ok": True,
                        "mutation": False,
                        "data": {"verification_eligible": True},
                        "evidence": [{"evidence_id": "E1"}],
                    },
                },
                {
                    "role": "tool",
                    "core_name": "git.status",
                    "result": {
                        "ok": True,
                        "mutation": False,
                        "data": {},
                        "evidence": [{"evidence_id": "E2"}],
                    },
                },
                {
                    "role": "tool",
                    "core_name": "git.diff",
                    "result": {
                        "ok": True,
                        "mutation": False,
                        "data": {},
                        "evidence": [{"evidence_id": "E3"}],
                    },
                },
            ),
        )

        self.assertEqual(effect.kind, ShadowEffectKind.PROOF_ONLY)
        self.assertEqual(effect.check_calls, 3)
        self.assertEqual(effect.new_evidence_count, 3)

    def test_mutation_effect_counts_generated_payload(self) -> None:
        raw = '{"path":"a.py","content":"print(1)"}'
        effect = audit_history_delta(
            snapshot=snapshot(),
            new_entries=(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "call_id": "write",
                            "name": "repo_write_file",
                            "raw_arguments": raw,
                        }
                    ],
                },
                {
                    "role": "tool",
                    "core_name": "repo.write_file",
                    "result": {
                        "ok": True,
                        "mutation": True,
                        "data": {"changed": True, "path": "a.py"},
                        "evidence": [{"evidence_id": "E1"}],
                    },
                },
            ),
        )

        self.assertEqual(effect.kind, ShadowEffectKind.SEMANTIC_CHANGE)
        self.assertTrue(effect.changed)
        self.assertEqual(effect.mutation_calls, 1)
        self.assertEqual(effect.generated_argument_bytes, len(raw.encode("utf-8")))
        self.assertEqual(effect.mutation_argument_bytes, len(raw.encode("utf-8")))

    def test_protocol_error_effect(self) -> None:
        effect = audit_history_delta(
            snapshot=snapshot(),
            new_entries=(
                {
                    "role": "tool",
                    "core_name": "repo.read_file",
                    "error": {
                        "type": "malformed_arguments",
                        "message": "bad json",
                    },
                },
            ),
        )

        self.assertEqual(effect.kind, ShadowEffectKind.PROTOCOL_RECOVERY)
        self.assertEqual(effect.tool_error_count, 1)

    def test_completed_state_text_is_report_only(self) -> None:
        effect = audit_history_delta(
            snapshot=snapshot(verification_complete=True),
            new_entries=({"role": "assistant", "content": "done", "tool_calls": []},),
        )

        self.assertEqual(effect.kind, ShadowEffectKind.REPORT_ONLY)

    def test_summary_counts_only_shadow_main_events(self) -> None:
        summary = summarize_shadow_usage(
            {
                "events": [
                    {
                        "shadow_version": "phase0a-v0",
                        "shadow_would_call_model": "no",
                        "shadow_confidence": "certain_mechanical",
                        "shadow_reason_codes": "completion_already_verified",
                        "shadow_effect_kind": "report_only",
                        "shadow_request_chars": 100,
                        "shadow_dynamic_chars": 40,
                        "prompt_tokens": 20,
                        "completion_tokens": 3,
                        "cache_read_tokens": 10,
                    },
                    {"kind": "compaction_brief", "prompt_tokens": 99},
                ]
            }
        )

        self.assertEqual(summary["main_model_boundaries"], 1)
        self.assertEqual(summary["certain_mechanical_boundaries"], 1)
        self.assertEqual(summary["certain_mechanical_fraction"], 1.0)
        self.assertEqual(summary["request_chars"], 100)
        self.assertEqual(summary["prompt_tokens"], 20)


    def test_summary_exposes_repeat_and_mechanical_opportunity_profile(self) -> None:
        summary = summarize_shadow_usage(
            {
                "events": [
                    {
                        "step": 1,
                        "shadow_version": "phase0a-v0",
                        "shadow_would_call_model": "yes",
                        "shadow_confidence": "semantic_required",
                        "shadow_effect_kind": "observation_only",
                        "shadow_read_result_signatures": "read-same",
                        "prompt_tokens": 100,
                        "cache_read_tokens": 80,
                        "completion_tokens": 10,
                    },
                    {
                        "step": 2,
                        "shadow_version": "phase0a-v0",
                        "shadow_would_call_model": "unknown",
                        "shadow_confidence": "unknown",
                        "shadow_effect_kind": "observation_only",
                        "shadow_read_result_signatures": "read-same",
                        "prompt_tokens": 200,
                        "cache_read_tokens": 150,
                        "completion_tokens": 20,
                    },
                    {
                        "step": 3,
                        "shadow_version": "phase0a-v0",
                        "shadow_would_call_model": "unknown",
                        "shadow_confidence": "unknown",
                        "shadow_effect_kind": "proof_only",
                        "shadow_proof_result_signatures": "proof-one",
                        "prompt_tokens": 300,
                        "cache_read_tokens": 250,
                        "completion_tokens": 30,
                    },
                    {
                        "step": 4,
                        "shadow_version": "phase0a-v0",
                        "shadow_would_call_model": "no",
                        "shadow_confidence": "certain_mechanical",
                        "shadow_reason_codes": "completion_already_verified",
                        "shadow_effect_kind": "report_only",
                        "prompt_tokens": 400,
                        "cache_read_tokens": 350,
                        "completion_tokens": 40,
                    },
                ]
            }
        )

        self.assertEqual(summary["repeated_unchanged_reads"]["boundaries"], 1)
        self.assertEqual(summary["repeated_unchanged_reads"]["calls"], 1)
        opportunity = summary["deterministic_transition_candidates"]
        self.assertEqual(opportunity["boundaries"], 3)
        self.assertEqual(opportunity["reason_counts"]["repeated_unchanged_read"], 1)
        self.assertEqual(opportunity["reason_counts"]["proof_only"], 1)
        self.assertEqual(opportunity["reason_counts"]["prospective_runtime"], 1)
        self.assertEqual(summary["cost_by_effect"]["proof_only"]["calls"], 1)
        self.assertEqual(summary["top_costly_boundaries"][0]["step"], 4)


if __name__ == "__main__":
    unittest.main()
