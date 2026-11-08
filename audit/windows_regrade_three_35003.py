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

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "tasks" / "real-three-orbitcontrols-35003-v2"
ENV = ROOT / "audit" / "THREE_35003_ENVIRONMENT_20261108.json"
ARMS = ("karox5", "opencode_v2", "deepseek_harness")


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hash_tree(folder: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(folder.rglob("*")):
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
        if previous.get("task_id") != report["task_id"]:
            raise RuntimeError(f"Wrong task id: {arm}")
        if previous.get("fixture_sha256") != fixture_hash:
            raise RuntimeError(f"Different input fixture: {arm}")
        if previous.get("task_sha256") != task_sha:
            raise RuntimeError(f"Different task prompt: {arm}")
        if previous.get("grader_sha256") != hash_tree(TASK / "grader"):
            raise RuntimeError(f"Changed grader in raw run: {arm}")
        proof = folder / "windows_visual_proof"
        grade = evaluate(workspace, proof, args.max_grader_seconds)
        completion = previous.get("quality_classification", {}).get("cli_completed") is True
        integrity = previous.get("harness_config_untampered") is True
        source_infra = (
            previous.get("agent_timed_out") is True or
            previous.get("quality_classification", {}).get("status") in
            ("SETUP_BLOCKED", "INFRA_SETUP_BLOCKED")
        )
        grade["cli_completed_independent"] = completion
        grade["runner_config_integrity"] = integrity
        grade["agent_infra_blocked"] = source_infra
        grade["artifact_correct"] = grade["grade"] == "PASS"
        grade["clean_user_success"] = grade["artifact_correct"] and completion and integrity and not source_infra
        grade["estimated_cost_usd"] = None
        grade["actual_provider_invoice_usd"] = None
        grade["usage_from_raw_result"] = previous.get("terminal_events", {}).get("usage")
        grade["agent_wall_seconds"] = previous.get("agent_wall_seconds")
        report["arms"][arm] = grade
    report["three_way_complete"] = all(
        report["arms"].get(arm, {}).get("grade") in ("PASS", "FAIL") and
        not report["arms"][arm].get("agent_infra_blocked") for arm in ARMS
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
