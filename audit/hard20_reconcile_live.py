"""Reconcile immutable HARD20 DEV CLI results against independent posthoc grades.

Read-only by default. Never rewrites per-arm results, graders, or archives.
A patch PASS does not imply a clean CLI completion or a matched leaderboard.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK = "real-three-orbitcontrols-35003-v2"
ARMS = ("karox5", "opencode_v2", "deepseek_harness")
SETS = ("hard20-max-runs", "paired-linux")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_host_grades() -> dict[tuple[str, str, str], dict]:
    grades: dict[tuple[str, str, str], dict] = {}
    for source in SETS:
        ledger = ROOT / source / "HARD20_HOST_REGRADE_LEDGER.jsonl"
        if not ledger.is_file():
            continue
        for number, line in enumerate(ledger.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            key = (row.get("label", ""), row.get("task_id", ""), row.get("arm", ""))
            if not all(key) or key[2] not in ARMS:
                raise ValueError(f"Unusable host grade {ledger}:{number}")
            if key in grades:
                if grades[key] != row:
                    raise ValueError(f"Conflicting posthoc grading evidence for {key}")
                continue
            grades[key] = row
    return grades


def reconcile() -> dict:
    grades = load_host_grades()
    rows: list[dict] = []
    for family in SETS:
        for evidence in sorted((ROOT / family).glob("hard20-*/*/*/result.json")):
            arm = evidence.parent.name
            task_id = evidence.parent.parent.name
            label = evidence.parent.parent.parent.name
            if arm not in ARMS or task_id != TASK:
                continue
            cli = json.loads(evidence.read_text(encoding="utf-8"))
            if cli.get("run_id") is None or cli.get("task_id") != task_id or cli.get("arm") != arm:
                raise ValueError(f"Result identity invalid: {evidence}")
            judge = grades.get((label, task_id, arm))
            if judge is not None and judge.get("source_cli_result_sha256") != digest(evidence):
                raise ValueError(f"Result has changed since independent regrade: {evidence}")
            raw = cli.get("quality_classification") or {}
            exit_code = cli.get("agent_exit_code")
            external = judge.get("external_grade") if judge else "UNJUDGED"
            clean = bool(judge and external == "PASS" and exit_code == 0
                         and cli.get("harness_config_untampered") is True
                         and (cli.get("terminal_events") or {}).get("terminal_seen") is True
                         and judge.get("clean_user_success") is True)
            status = ("CLEAN_PASS" if clean else
                      "PATCH_PASS_BUT_CLI_INCOMPLETE" if external == "PASS" else
                      "QUALITY_FAIL" if external == "FAIL" else
                      "INFRA_OR_NOT_GRADED")
            rows.append({
                "source_family": family,
                "label": label,
                "task_id": task_id,
                "arm": arm,
                "result_sha256": digest(evidence),
                "split": cli.get("security_class", "DEV_UNPROTECTED"),
                "external_grade": external,
                "clean_cli_success": clean,
                "cli_exit_code": exit_code,
                "terminal_seen": (cli.get("terminal_events") or {}).get("terminal_seen"),
                "raw_grader_exit": cli.get("grader_exit_code"),
                "raw_quality": raw.get("status"),
                "reported_usage": (cli.get("terminal_events") or {}).get("usage"),
                "wall_seconds": cli.get("agent_wall_seconds"),
                "classification": status,
                "actual_provider_invoice_usd": None,
                "independent_report": (
                    "verified against CLI result sha256" if judge is not None else None),
                "not_holdout": True,
            })
    summary = {}
    for arm in ARMS:
        records = [x for x in rows if x["arm"] == arm]
        tally = Counter(x["classification"] for x in records)
        summary[arm] = {"attempts": len(records), "external_patch_pass": sum(
            x["external_grade"] == "PASS" for x in records),
            "clean_cli_pass": sum(x["clean_cli_success"] for x in records),
            "unjudged": sum(x["external_grade"] == "UNJUDGED" for x in records),
            "classifications": dict(tally)}
    return {
        "schema": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "task_id": TASK,
        "split": "PUBLIC_DEV_NOT_BLIND",
        "matched_leaderboard_valid": False,
        "reason": "Different labels/retries and infrastructure failures; never infer a paired winner.",
        "source_sets": list(SETS),
        "rows": rows,
        "summary": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="Write new reconciled report; do not touch evidence.")
    args = parser.parse_args()
    result = reconcile()
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    print(json.dumps({"rows": len(result["rows"]), "summary": result["summary"],
                      "matched_leaderboard_valid": False}, ensure_ascii=False))
    if args.write:
        target = ROOT / "HARD20_RECONCILED_LIVE_20261108.json"
        if target.exists():
            raise FileExistsError(f"Refuse to overwrite previous report: {target}")
        target.write_text(payload, encoding="utf-8")
        print(f"Report created: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
