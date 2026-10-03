from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "semantic_shadow_report.py"


def _usage() -> dict[str, object]:
    return {
        "events": [
            {
                "step": 1,
                "shadow_version": "phase0a-v0",
                "shadow_would_call_model": "yes",
                "shadow_confidence": "semantic_required",
                "shadow_effect_kind": "observation_only",
                "shadow_read_result_signatures": "same-read",
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
                "shadow_read_result_signatures": "same-read",
                "prompt_tokens": 200,
                "cache_read_tokens": 150,
                "completion_tokens": 20,
            },
        ]
    }


def test_cli_reads_full_session_json(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"usage": _usage()}), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--json", str(path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    report = payload[str(path)]
    assert report["main_model_boundaries"] == 2
    assert report["repeated_unchanged_reads"] == {"boundaries": 1, "calls": 1}
    assert report["deterministic_transition_candidates"]["boundaries"] == 1


def test_cli_reuses_embedded_report_summary(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    summary = {
        "main_model_boundaries": 7,
        "deterministic_transition_candidates": {
            "boundaries": 2,
            "fraction": 0.285714,
            "reason_counts": {"proof_only": 2},
        },
    }
    path.write_text(
        json.dumps({"project_context": {"semantic_shadow": summary}}),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--json", str(path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload[str(path)] == summary


def test_cli_reports_invalid_payload(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 1
    assert "no session usage" in completed.stdout
