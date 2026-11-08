"""HARD20 execution readiness under explicit user-authorized observe-only billing.

This is a LOCAL preflight, not a dispatch/proxy or statement of provider invoices.
The historical Phase0 preflight remains preserved separately in git and audit.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = "deepseek-ai/DeepSeek-V4.1-Flash"
URL = "https://api.inference.wandb.ai/v1"


def cost(value: object) -> Decimal:
    """Reject unverifiable floats and invalid negative accounting fields."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Value must be finite Decimal, integer or decimal string")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Not a decimal quantity") from error
    if not result.is_finite() or result < 0:
        raise ValueError("Expected finite nonnegative value")
    return result


def estimated_usd(*, input_uncached: object, input_cached: object, output: object) -> Decimal:
    """W&B published token rate estimate; NEVER an invoiced total."""
    return (
        Decimal("0.20") * cost(input_uncached)
        + Decimal("0.03") * cost(input_cached)
        + Decimal("0.65") * cost(output)
    ) / Decimal(1_000_000)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def readiness(root: Path = ROOT, *, task_id: str = "real-three-orbitcontrols-35003-v2") -> dict:
    """Check ONE frozen task. Do not demand 20 freezes before first real run."""
    root = root.resolve()
    blocks = []

    def check(value: bool, why: str) -> None:
        if not value:
            blocks.append(why)

    policy = json.loads((root / "HARD20_BUDGET_POLICY.json").read_text(encoding="utf-8"))
    resource = json.loads((root / "HARD20_RESOURCE_POLICY.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "HARD20_TASK_MANIFEST.json").read_text(encoding="utf-8"))
    legacy_protocol = root / "docs/benchmarks/HARD20_KAROX_TRIAD_PROTOCOL_20261108.md"
    authority = root / "docs/benchmarks/HARD20_SOL_EXECUTE_MAX_20261108.md"
    check(sha(legacy_protocol) == manifest["protocol_sha256"], "OLD_PROTOCOL_CHANGED")
    check(authority.is_file(), "NEW_AUTHORITY_MISSING")
    check(authority.is_file() and sha(authority) == policy["authority_sha256"],
          "NEW_AUTHORITY_SHA_CHANGED")
    check(policy.get("state") == "OBSERVE_ONLY_NO_HARD_CAP", "OLD_FINANCIAL_GATES_ACTIVE")
    check(policy.get("paid_calls_enabled") is True, "USER_AUTHORIZATION_NOT_RECORDED")
    check(policy.get("global_hard_spend_cap_usd") is None, "OBSOLETE_HARD_CAP_PRESENT")
    check(policy.get("pilot_hard_spend_cap_usd") is None, "OBSOLETE_PILOT_CAP_PRESENT")
    check(policy.get("token_budget_cap") is None, "OBSOLETE_TOKEN_CAP_PRESENT")
    check(policy.get("step_budget_cap") is None, "OBSOLETE_STEP_CAP_PRESENT")
    check(policy.get("api_request_budget_cap") is None, "OBSOLETE_REQUEST_CAP_PRESENT")
    check(policy.get("endpoint") == URL and policy.get("model") == MODEL,
          "MODEL_ROUTE_MISMATCH")
    check(policy.get("no_anthropic") and policy.get("no_user_projects")
          and policy.get("no_github_push"), "SECURITY_POLICY_INCOMPLETE")
    check(resource.get("max_heavy_jobs") == 1, "PARALLEL_HEAVY_RUNS_NOT_ALLOWED")
    check(resource.get("cleanup", {}).get("only_registered_per_run_disposable_workspace"),
          "CLEANUP_REGISTERED_WORKSPACE_REQUIRED")

    freeze_path = root / "HARD20_TASK_FREEZE_35003_v2.json"
    if task_id == "real-three-orbitcontrols-35003-v2" and freeze_path.is_file():
        freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
        task = root / "tasks" / task_id
        check(freeze.get("task_id") == task_id, "TASK_ID_MISMATCH")
        check(sha(task / "task.json") == freeze["task_sha256"], "FROZEN_TASK_MODIFIED")
        check(sha(task / "grader/test_browser.py") == freeze["grader_sha256"],
              "FROZEN_GRADER_MODIFIED")
        check(freeze.get("red_exit") == 1 and freeze.get("green_exit") == 0,
              "RED_GREEN_NOT_PROVEN")
        check(freeze.get("grader_isolated_from_agent") is True,
              "GRADER_ACCESS_NOT_ISOLATED")
    else:
        blocks.append("TASK_NOT_INDIVIDUALLY_FROZEN")

    free_c = round(shutil.disk_usage("C:\\").free / 2**30, 4)
    free_d = round(shutil.disk_usage("D:\\").free / 2**30, 4)
    check(free_c >= resource["min_free_gib"]["C"], "C_RESERVE_TOO_LOW")
    check(free_d >= resource["min_free_gib"]["D"], "D_RESERVE_TOO_LOW")
    return {
        "schema": 2,
        "status": "READY_LOCAL_ONLY" if not blocks else "BLOCKED_NONFINANCIAL_PRECHECK",
        "paid_calls_user_authorized": policy.get("paid_calls_enabled") is True,
        "financial_mode": policy.get("state"),
        "old_3_30_caps_enforced": False,
        "task_id": task_id,
        "task_candidates": len(manifest["tasks"]),
        "task_frozen_for_this_run": task_id == "real-three-orbitcontrols-35003-v2" and not blocks,
        "c_free_gib": free_c,
        "d_free_gib": free_d,
        "blockers": sorted(set(blocks)),
        "no_api_requests_dispatched_by_this_preflight": True,
        "actual_invoiced_usd": None,
        "estimated_usd": None,
        "max_effort_wire_verified_for_all_arms": False,
        "authority_sha256": sha(authority) if authority.is_file() else None,
        "budget_policy_sha256": sha(root / "HARD20_BUDGET_POLICY.json"),
    }


def main() -> int:
    out = ROOT / "HARD20_EXECUTE_PREFLIGHT_RESULT.json"
    if out.exists():
        raise FileExistsError("Live preflight immutable; choose new filename for later audit")
    result = readiness()
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "READY_LOCAL_ONLY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
