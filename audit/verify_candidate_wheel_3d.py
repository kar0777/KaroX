"""Fresh-wheel and independent offline WebGL regrade; no model calls.

The venv is new on D: but deliberately reuses preinstalled Python dependencies
via --system-site-packages: dependency closure is NOT freshly isolated.
The real Chromium grader rechecks a COPIED historical output, not a new agent run.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_WHEEL_SHA256 = "485c8ebfbcff994941c09375eda7907c29ad99fd74bedb542b448c2b0affbdc0"
WHEEL = ROOT / "dist-triad-candidate" / "karox_runtime-5.0.0-py3-none-any.whl"
SOURCE = ROOT / "paired-dev" / "webgl-strict-epoch1-20261108" / "threejs-crystal-cavern-strict" / "karox5" / "workspace"
STAGING = ROOT / ".tmp" / "candidate-wheel-3d-20261108"
REPORT = ROOT / "audit" / "CANDIDATE_WHEEL_3D_GATE_20261108.json"
GRADER_LOG = ROOT / "audit" / "candidate_wheel_3d_grader_20261108.log"


def run(args: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None) -> dict:
    start = time.monotonic()
    try:
        proc = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        return {"exit": proc.returncode, "seconds": round(time.monotonic() - start, 3),
                "stdout": proc.stdout[-12000:], "stderr": proc.stderr[-3000:]}
    except subprocess.TimeoutExpired:
        return {"exit": 124, "seconds": round(time.monotonic() - start, 3),
                "error": "TIMEOUT_INFRA", "stdout": "", "stderr": ""}


def main() -> int:
    import shutil as disk
    if disk.disk_usage("C:\\").free < 7 * 1024 ** 3 or disk.disk_usage("D:\\").free < 15 * 1024 ** 3:
        raise RuntimeError("Disk reserve guard blocked wheel gate")
    if not WHEEL.is_file() or not SOURCE.is_dir():
        raise RuntimeError("Candidate wheel or raw result workspace missing")
    wheel_sha = hashlib.sha256(WHEEL.read_bytes()).hexdigest()
    if wheel_sha != EXPECTED_WHEEL_SHA256:
        raise RuntimeError("Wheel checksum differs from frozen candidate")
    if STAGING.exists():
        raise RuntimeError("Staging already exists, refusing to overwrite old evidence")
    STAGING.mkdir(parents=True)
    venv = STAGING / "venv"
    stage = STAGING / "historical_3d_output_copy"
    shutil.copytree(SOURCE, stage, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    report = {
        "mode": "offline_candidate_wheel_checker_plus_historical_artifact_independent_regrade",
        "new_paid_agent_run": False,
        "distribution_dependencies_hermetic": False,
        "dependency_reuse": "new --system-site-packages venv; existing host dependencies; no download",
        "wheel_sha256": wheel_sha,
        "source_workspace_unchanged": True,
        "source_raw_archival_result": str(SOURCE),
        "staging": str(STAGING),
    }
    report["venv"] = run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)], ROOT, 90)
    py = str(venv / "Scripts" / "python.exe")
    report["install"] = run([py, "-m", "pip", "install", "--no-index", "--no-deps",
                             "--no-cache-dir", "--ignore-installed", str(WHEEL)], ROOT, 100)
    if report["install"]["exit"] != 0:
        REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", "utf-8")
        return 1
    script = ("import json,karox;from karox import web_document;"
              "print(json.dumps({'version':karox.__version__,"
              "'origin':str(web_document.__file__),"
              "'shadow':web_document.shadowed_by_repository()}))")
    report["import"] = run([py, "-I", "-c", script], stage, 35)
    if report["import"]["exit"] != 0:
        REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", "utf-8")
        return 1
    info = json.loads(report["import"]["stdout"].strip().splitlines()[-1])
    report["import_details"] = info
    report["wheel_import_correct"] = (
        info["version"] == "5.0.0"
        and Path(info["origin"]).resolve().is_relative_to(venv.resolve())
        and info["shadow"] is None
    )
    cli = venv / "Scripts" / "karox.exe"
    report["cli_help"] = run([str(cli), "--help"], stage, 25)
    report["checker_positive"] = run(
        [py, "-m", "karox.web_check", "--root", str(stage), "index.html"], stage, 35
    )
    negative = STAGING / "deliberately_broken"
    negative.mkdir()
    (negative / "index.html").write_text(
        '<!doctype html><html><body><script type="module">import x from "missing-bare-module";</script></body></html>',
        encoding="utf-8",
    )
    report["checker_negative"] = run(
        [py, "-m", "karox.web_check", "--root", str(negative), "index.html"], negative, 35
    )
    positive_ok = False
    negative_ok = False
    try:
        pos = json.loads(report["checker_positive"]["stdout"])
        neg = json.loads(report["checker_negative"]["stdout"])
        positive_ok = (report["checker_positive"]["exit"] == 0
                       and pos.get("ok") is True
                       and pos.get("checked_by") == "karox.web_check")
        negative_ok = (report["checker_negative"]["exit"] != 0
                       and neg.get("ok") is False and bool(neg.get("problems")))
    except (ValueError, TypeError):
        pass
    report["positive_ok"] = positive_ok
    report["negative_ok"] = negative_ok
    visual = STAGING / "visual-proof"
    env = os.environ.copy()
    env.update({"TASK_WORKSPACE": str(stage), "TRIAD_AUDIT_DIR": str(ROOT / "audit"),
                "VISUAL_PROOF_DIR": str(visual), "PYTHONIOENCODING": "utf-8"})
    tests = [str(ROOT / "tasks/threejs-crystal-cavern-strict/grader/test_acceptance.py"),
             str(ROOT / "tasks/threejs-crystal-cavern-strict/grader/test_strict_browser.py")]
    report["independent_grader"] = run(
        [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", *tests],
        ROOT, 155, env,
    )
    GRADER_LOG.write_text(report["independent_grader"]["stdout"] +
                          "\nSTDERR:\n" + report["independent_grader"]["stderr"], encoding="utf-8")
    report["grader_4_of_4"] = (
        report["independent_grader"]["exit"] == 0
        and "4 passed" in report["independent_grader"]["stdout"]
        and "skipped" not in report["independent_grader"]["stdout"].lower()
    )
    proof = []
    if visual.exists():
        for path in sorted(visual.rglob("*")):
            if path.is_file():
                proof.append({"relative_path": path.relative_to(STAGING).as_posix(),
                              "bytes": path.stat().st_size,
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    report["visual_artifacts"] = proof
    report["success"] = all([
        report["wheel_import_correct"], report["cli_help"]["exit"] == 0,
        positive_ok, negative_ok, report["grader_4_of_4"],
        any(i["relative_path"].endswith(".png") for i in proof),
    ])
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(json.dumps({k: report[k] for k in (
        "success", "wheel_import_correct", "positive_ok", "negative_ok",
        "grader_4_of_4", "visual_artifacts"
    )}, ensure_ascii=False, indent=2))
    print("RECORD", REPORT)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
