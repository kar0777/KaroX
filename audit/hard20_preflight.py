"""HARD20 fail-closed *readiness* and accounting simulator.

This deliberately has NO HTTP client, API secret handling, or function that
dispatches a paid request. A bookkeeping counter alone cannot enforce a
provider bill if an agent can bypass it. W&B monthly limits may overrun.
Do NOT turn this into an affirmative spend authorization by flipping JSON.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cost(value: object) -> Decimal:
    """Reject floats / unknown charge amounts; use decimal strings for dollars."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Cost must be a decimal string, integer or Decimal")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Invalid decimal cost") from error
    if not result.is_finite() or result < 0:
        raise ValueError("Cost must be finite and nonnegative")
    return result


def reservation_allowed(
    *,
    pilot: bool,
    committed_total: object,
    committed_pilot: object,
    additional_reservation: object,
    total_cap: object = "30",
    pilot_cap: object = "3",
) -> bool:
    """Arithmetic preflight only. Does NOT authorize a network request."""
    total = cost(committed_total)
    piloted = cost(committed_pilot)
    pending = cost(additional_reservation)
    global_limit = cost(total_cap)
    pilot_limit = cost(pilot_cap)
    if pending <= 0 or piloted > total or piloted > pilot_limit:
        return False
    if total + pending > global_limit:
        return False
    if pilot and piloted + pending > pilot_limit:
        return False
    return True


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def readiness(root: Path = ROOT) -> dict:
    """Never queries W&B or runs a competitor; all checks are local."""
    root = root.resolve()
    manifest = json.loads((root / "HARD20_TASK_MANIFEST.json").read_text(encoding="utf-8"))
    budget = json.loads((root / "HARD20_BUDGET_POLICY.json").read_text(encoding="utf-8"))
    resources = json.loads((root / "HARD20_RESOURCE_POLICY.json").read_text(encoding="utf-8"))
    telemetry = json.loads((root / "HARD20_TELEMETRY_SCHEMA.json").read_text(encoding="utf-8"))
    order = json.loads((root / "HARD20_ORDER_SCHEDULE.json").read_text(encoding="utf-8"))
    protocol = root / "docs/benchmarks/HARD20_KAROX_TRIAD_PROTOCOL_20261108.md"
    blocks = []
    def require(condition: bool, reason: str) -> None:
        if not condition:
            blocks.append(reason)

    require(sha256(protocol) == manifest["protocol_sha256"], "PROTOCOL_SHA_CHANGED")
    tasks = manifest["tasks"]
    require(len(tasks) == 20 and len({x["id"] for x in tasks}) == 20,
            "NOT_20_UNIQUE_PUBLIC_TASKS")
    require(len(order["tasks"]) == 20, "RUN_ORDER_NOT_DEFINED")
    arms = {"karox5", "opencode_v2", "deepseek_harness"}
    require(all(set(t["order"]) == arms and len(t["order"]) == 3
                for t in order["tasks"]), "RUN_ORDER_NOT_EXACT_THREE")
    require(all(t.get("local_red_green_verified") is True
                and t.get("grader_isolated_from_agent") is True
                and t.get("license_individually_verified") is True
                and t.get("state") == "FROZEN"
                for t in tasks), "TASK_FREEZE_RED_GREEN_INCOMPLETE")
    require(manifest.get("count_frozen_for_competition") == 20,
            "FROZEN_TASK_COUNT_NOT_20")

    require(budget.get("paid_calls_enabled") is True,
            "PAID_CALLS_DISABLED_BY_POLICY")
    require(budget.get("hard_budget_cap_at_provider_verified") is True,
            "PROVIDER_HARD_CAP_NOT_PROVEN")
    require(budget.get("locked_outbound_metered_gateway_deployed") is True,
            "METERING_GATEWAY_NOT_DEPLOYED")
    require(budget.get("sandbox_denies_bypass_direct_provider_access") is True,
            "CLI_CAN_BYPASS_METER")
    require(budget.get("all_three_official_clients_restricted_to_gateway") is True,
            "THREE_CLI_EGRESS_NOT_PROVEN")
    require(budget.get("per_request_output_token_hard_ceiling_verified") is True,
            "MAX_CHARGE_PER_REQUEST_UNKNOWN")
    require(budget.get("full_price_table_and_charge_units_verified") is True,
            "PROVIDER_BILLING_RATE_NOT_CONFIRMED")
    require(budget.get("provider_api_key_access_only_via_host_gateway") is True,
            "CLI_CAN_ACCESS_PROVIDER_KEY_DIRECTLY")
    require(budget.get("max_effort_payload_verified_for_all_arms") is True,
            "REASONING_MAX_WIRE_NOT_VERIFIED")
    require(telemetry["state"] != "SCHEMA_ONLY_NO_NEW_RUNS",
            "TELEMETRY_CAPTURE_NOT_DEPLOYED")
    require(resources["max_heavy_jobs"] == 1, "PARALLEL_HEAVY_RUNS_NOT_ALLOWED")
    require(not resources["docker_image_pulls_authorized_in_this_phase"],
            "NO_IMAGE_PULLS_IN_READ_ONLY_PHASE")

    free_c = round(shutil.disk_usage("C:\\").free / 2**30, 4)
    free_d = round(shutil.disk_usage("D:\\").free / 2**30, 4)
    require(free_c >= resources["min_free_gib"]["C"], "C_RESERVE_TOO_LOW")
    require(free_d >= resources["min_free_gib"]["D"], "D_RESERVE_TOO_LOW")
    # This checker does not inspect gateway execution or remote invoice. Even
    # if every local declaration were made true, paid calls remain UNAUTHORIZED
    # until the separately implemented hard egress gate is independently tested.
    blocks.append("NO_ENFORCING_EGRESS_GATEWAY_IN_THIS_PROGRAM")
    return {
        "status": "BLOCKED_NO_PAID_CALLS",
        "paid_calls_authorized": False,
        "pilot_paid_attempts": 0,
        "main_paid_attempts": 0,
        "actual_new_spend_usd": 0,
        "estimated_new_spend_usd": 0,
        "global_cap_usd": 30,
        "pilot_cap_usd": 3,
        "c_free_gib_at_check": free_c,
        "d_free_gib_at_check": free_d,
        "task_candidates": len(tasks),
        "task_locally_red_green_ready": sum(
            x["local_red_green_verified"] is True for x in tasks
        ),
        "task_frozen": manifest["count_frozen_for_competition"],
        "unique_blockers": sorted(set(blocks)),
        "protocol_sha256": sha256(protocol),
        "task_manifest_sha256": sha256(root / "HARD20_TASK_MANIFEST.json"),
        "budget_policy_sha256": sha256(root / "HARD20_BUDGET_POLICY.json"),
        "simulated_budget_reservation_is_not_billing_enforcement": True,
    }


def main() -> int:
    output = ROOT / "HARD20_PREFLIGHT_RESULT.json"
    if output.exists():
        raise FileExistsError("Refusing to overwrite prior HARD20 preflight evidence")
    result = readiness()
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "BLOCKED_NO_PAID_CALLS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
