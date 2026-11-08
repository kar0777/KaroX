"""Pre-registered Windows Chromium regrade for future Linux Three.js DEV arms.

Only KaroX 5, official OpenCode V2, genuine DeepSeek Harness are eligible.
The Linux browser grader is INFRA_UNAVAILABLE in this environment, not a
coding-agent defeat. Regrade each already-finished Linux output on the SAME
independent Windows Chromium backend; never rewrite raw result.json.
No model/provider calls are made by this program.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    from audit.verify_saved_run_provenance import verify as verify_saved_arm
except ModuleNotFoundError:
    # Also works when invoked as python audit/windows_regrade_three_35003.py.
    from verify_saved_run_provenance import verify as verify_saved_arm

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "tasks" / "real-three-orbitcontrols-35003-v2"
ENV = ROOT / "audit" / "THREE_35003_ENVIRONMENT_20261108.json"
ARMS = ("karox5", "opencode_v2", "deepseek_harness")
EXPECTED_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash"
EXPECTED_PROVIDER = "https://api.inference.wandb.ai/v1"


def verify_model_route(previous: dict) -> None:
    """Refuse to score a three-way run with a different recorded model route."""
    if (previous.get("model") != EXPECTED_MODEL
            or previous.get("provider_base_url") != EXPECTED_PROVIDER):
        raise RuntimeError("Recorded model/provider differs from frozen three-way DEV protocol")


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hash_tree(folder: Path) -> str:
    digest = hashlib.sha256()
    # Match Linux runner ordering exactly, independent of Windows Path ordering.
    for file in sorted(folder.rglob("*"), key=lambda item: item.relative_to(folder).as_posix()):
        if file.is_file():
            digest.update(file.relative_to(folder).as_posix().encode())
            digest.update(b"\0")
            digest.update(file.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def clean(text: str) -> str:
    text = re.sub(
        r"(?i)(authorization|bearer|api[_-]?key|password|token|secret)\s*[:=]\s*['\"]?[^\s,'\";]+",
        r"\1=[REDACTED]", text,
    )
    return re.sub(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_.-]{12,}\b", "[REDACTED]", text)


def classify_agent_run(previous: dict, independent_grade: dict) -> dict:
    """Separate final-code quality from CLI completion and infrastructure.

    These are pre-registered adjudication rules, not agent-reported evidence.
    A correct page with an empty patch is not a successfully solved bug.
    """
    original = previous.get("quality_classification") or {}
    cli_complete = original.get("cli_completed") is True
    config_intact = previous.get("harness_config_untampered") is True
    blocked = (
        previous.get("agent_timed_out") is True
        or original.get("status") in ("SETUP_BLOCKED", "INFRA_SETUP_BLOCKED")
    )
    # A working browser cannot rehabilitate a corrupt/invalid original agent run.
    source_evidence_valid = (
        original.get("validity") is True
        and original.get("status") != "INVALID"
    )
    patch_bytes = previous.get("patch_bytes")
    patch_nonempty = type(patch_bytes) is int and patch_bytes > 0
    artifact_correct = independent_grade.get("grade") == "PASS" and patch_nonempty
    return {
        "cli_completed_from_original_runner": cli_complete,
        "runner_config_integrity": config_intact,
        "source_evidence_valid": source_evidence_valid,
        "agent_infra_blocked": blocked,
        "patch_nonempty": patch_nonempty,
        "artifact_correct": artifact_correct,
        "clean_user_success": (
            artifact_correct and cli_complete and config_intact
            and source_evidence_valid and not blocked
        ),
    }


def evaluate(workspace: Path, proof: Path, timeout: int) -> dict:
    if proof.exists():
        raise FileExistsError("Refusing to overwrite old visual proof: " + str(proof))
    proof.mkdir(parents=True)
    before = hash_tree(workspace)
    env = dict(os.environ)
    env.update({
        "TASK_WORKSPACE": str(workspace),
        "VISUAL_PROOF_DIR": str(proof),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    command = [
        sys.executable, "-B", "-m", "pytest", "-q", "-rA",
        "-p", "no:cacheprovider", "-o", "addopts=",
        str(TASK / "grader" / "test_browser.py"),
    ]
    start = time.monotonic()
    try:
        process = subprocess.run(
            command, cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        rc = process.returncode
        log = clean(process.stdout[-10000:])
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        rc = 124
        log = clean(str(exc.stdout)[-10000:])
        timed_out = True
    evidence = []
    for file in sorted(proof.iterdir()):
        if file.is_file():
            evidence.append({
                "name": file.name,
                "bytes": file.stat().st_size,
                "sha256": hash_file(file),
            })
    event_path = proof / "event_results.json"
    events = json.loads(event_path.read_text(encoding="utf-8")) if event_path.is_file() else None
    png = proof / "scene_before.png"
    # Browser launch errors and unavailable software are infrastructure, not
    # code-quality failures. Source/interaction assertion failures remain FAIL.
    browser_unavailable = any(
        phrase in log for phrase in (
            "Executable doesn't exist", "Host system is missing dependencies",
            "BrowserType.launch: Target page, context or browser has been closed",
            "Failed to launch browser", "playwright._impl._errors.Error:",
        )
    ) and events is None
    success = (
        rc == 0 and not timed_out and png.is_file() and png.stat().st_size > 1000
        and isinstance(events, dict) and not events.get("page_errors")
        and events.get("end_before_hover", 0) >= 1
        and events.get("end_after_hover") == events.get("end_before_hover")
        and abs(events.get("azimuth_after_hover", 1) -
                events.get("azimuth_before_hover", 0)) < 0.003
    )
    unchanged = before == hash_tree(workspace)
    if not unchanged:
        raise RuntimeError("The independent grader modified the candidate workspace")
    return {
        "grade": (
            "ENV_UNAVAILABLE" if timed_out or browser_unavailable else
            "PASS" if success else "FAIL"
        ),
        "exit": rc,
        "timed_out": timed_out,
        "seconds": round(time.monotonic() - start, 4),
        "workspace_sha256_before": before,
        "workspace_unchanged": unchanged,
        "events": events,
        "visual_files": evidence,
        "redacted_log_tail": log,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True,
                        help="Existing paired-linux run label, never starts an agent")
    parser.add_argument("--max-grader-seconds", type=int, default=75)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,90}", args.label):
        parser.error("Unsafe run label")
    import shutil
    if shutil.disk_usage("C:\\").free < 7 * 1024**3:
        parser.error("C: reserve below 7 GiB")
    if shutil.disk_usage("D:\\").free < 15 * 1024**3:
        parser.error("D: reserve below 15 GiB")
    fixed = json.loads(ENV.read_text(encoding="utf-8"))
    task_sha = hash_file(TASK / "task.json")
    grader_sha = hash_file(TASK / "grader" / "test_browser.py")
    fixture_hash = hash_tree(TASK / "fixture")
    if fixed["task_sha256"] != task_sha or fixed["grader_sha256"] != grader_sha:
        raise RuntimeError("Frozen task/grader changed after registration")
    base = ROOT / "paired-linux" / args.label / "real-three-orbitcontrols-35003-v2"
    if not base.is_dir():
        parser.error("No saved Linux run outputs; regrader cannot run an agent")
    out = base / "WINDOWS_INDEPENDENT_3D_ADJUDICATION.json"
    if out.exists():
        raise FileExistsError("Refusing to replace existing adjudication")
    report = {
        "schema": 1, "task_id": "real-three-orbitcontrols-35003-v2",
        "label": args.label, "arms": {}, "scored_holdout": False,
        "grader_sha256": grader_sha, "task_sha256": task_sha,
        "baseline_fixture_tree_sha256": fixture_hash,
        "browser_version": fixed["browser_version"],
        "browser_sha256": fixed["browser_sha256"],
        "evaluation_lane": "EXTERNAL_WINDOWS_CHROMIUM_AFTER_LINUX_AGENT",
        "parity": "same frozen Windows independent grader and Chromium for every arm",
        "linux_native_browser": "ENV_UNAVAILABLE",
    }
    for arm in ARMS:
        folder = base / arm
        raw = folder / "result.json"
        workspace = folder / "workspace"
        if not raw.is_file() or not workspace.is_dir():
            report["arms"][arm] = {"status": "NOT_RUN_NOT_A_LOSS"}
            continue
        previous = json.loads(raw.read_text(encoding="utf-8"))
        verify_model_route(previous)
        if previous.get("task_id") != report["task_id"]:
            raise RuntimeError(f"Wrong task id: {arm}")
        if previous.get("fixture_sha256") != fixture_hash:
            raise RuntimeError(f"Different input fixture: {arm}")
        if previous.get("task_sha256") != task_sha:
            raise RuntimeError(f"Different task prompt: {arm}")
        if previous.get("grader_sha256") != hash_tree(TASK / "grader"):
            raise RuntimeError(f"Changed grader in raw run: {arm}")
        # Fail closed before browsing: verify fixture Git HEAD, staged patch,
        # source file bytes and all ledger SHA values. An altered workspace is
        # not a competitor failure and cannot be rehabilitated by a visual PASS.
        provenance = verify_saved_arm(folder, TASK)
        proof = folder / "windows_visual_proof"
        grade = evaluate(workspace, proof, args.max_grader_seconds)
        grade["saved_run_provenance"] = provenance
        grade["workspace_provenance_valid"] = provenance["valid"] is True
        grade.update(classify_agent_run(previous, grade))
        grade["estimated_cost_usd"] = None
        grade["actual_provider_invoice_usd"] = None
        grade["usage_from_raw_result"] = previous.get("terminal_events", {}).get("usage")
        grade["agent_wall_seconds"] = previous.get("agent_wall_seconds")
        report["arms"][arm] = grade
    report["three_way_complete"] = all(
        report["arms"].get(arm, {}).get("grade") in ("PASS", "FAIL")
        and report["arms"][arm].get("source_evidence_valid") is True
        and report["arms"][arm].get("workspace_provenance_valid") is True
        and report["arms"][arm].get("runner_config_integrity") is True
        and not report["arms"][arm].get("agent_infra_blocked")
        for arm in ARMS
    )
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(json.dumps({
        "report": str(out),
        "three_way_complete": report["three_way_complete"],
        "statuses": {a: report["arms"][a].get("grade", report["arms"][a].get("status")) for a in ARMS},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
