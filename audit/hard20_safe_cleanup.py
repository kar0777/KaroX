"""Remove ONLY a registered, archived and independently regraded disposable workspace.

Default: read-only dry run. Never deletes results, logs, reference grader or ZIP.
The actual deletion is explicitly scoped to one workspace subfolder.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
TASK = "real-three-orbitcontrols-35003-v2"
ARMS = ("karox5", "opencode_v2", "deepseek_harness")
LABEL_RE = re.compile(r"^hard20-[a-zA-Z0-9._-]{5,120}$")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect(label: str, arm: str, *, resume_interrupted: bool = False) -> dict:
    if not LABEL_RE.fullmatch(label) or arm not in ARMS:
        raise ValueError("Only explicit HARD20 label and known agent arm accepted")
    registration = ROOT / "HARD20_RUN_LEDGER.jsonl"
    records = [json.loads(x) for x in registration.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not any(x.get("label") == label and x.get("arm") == arm
               and x.get("task_id") == TASK and x.get("run_completed") is True for x in records):
        raise RuntimeError("No completed registered disposable arm: refusing deletion")
    directory = ROOT / "paired-linux" / label / TASK / arm
    workspace = directory / "workspace"
    if directory.is_symlink() or workspace.is_symlink():
        raise RuntimeError("Cannot delete symlinked workspace")
    if not workspace.is_dir() or workspace.resolve().parent != directory.resolve():
        raise RuntimeError("Workspace absent or outside registered result directory")
    if any(item.is_symlink() for item in workspace.rglob("*")):
        raise RuntimeError("Workspace has symlinks: manual review required")
    archive = directory.parent / (arm + ".HARD20_EVIDENCE.zip")
    manifest = archive.with_name(archive.name + ".manifest.json")
    if not archive.is_file() or not manifest.is_file():
        raise RuntimeError("No archived original evidence and manifest")
    frozen = json.loads(manifest.read_text(encoding="utf-8"))
    if frozen.get("archive_sha256") != sha(archive):
        raise RuntimeError("Evidence archive differs from original SHA manifest")
    if frozen.get("source") != f"paired-linux/{label}/{TASK}/{arm}":
        raise RuntimeError("Evidence manifest points at different run")
    files = frozen.get("contents") or []
    if not any(x["path"].startswith("workspace/") for x in files):
        raise RuntimeError("ZIP does not contain original workspace files")
    with zipfile.ZipFile(archive) as zipper:
        if zipper.testzip() is not None:
            raise RuntimeError("Evidence ZIP CRC failure")
        expected = [x["path"] for x in files]
        expected.append("_HARD20_VERIFIED_CONTENTS.json")
        if sorted(zipper.namelist()) != sorted(expected):
            raise RuntimeError("Evidence ZIP members differ from SHA manifest")
        for row in files:
            member = PurePosixPath(row["path"])
            if member.is_absolute() or ".." in member.parts or member.parts[0] == "":
                raise RuntimeError("Unsafe archive member path")
            content = zipper.read(row["path"])
            if hashlib.sha256(content).hexdigest() != row["sha256"] or len(content) != row["bytes"]:
                raise RuntimeError("Archive failed lossless round-trip")
            if row["path"].startswith("workspace/"):
                current = workspace / Path(*member.parts[1:])
                if not current.is_file():
                    if not resume_interrupted:
                        raise RuntimeError("Current workspace changed after archive")
                elif sha(current) != row["sha256"]:
                    raise RuntimeError("Current workspace changed after archive")
    expected_paths = {x["path"] for x in files if x["path"].startswith("workspace/")}
    actual_paths = {p.relative_to(directory).as_posix() for p in workspace.rglob("*") if p.is_file()}
    if not actual_paths.issubset(expected_paths):
        raise RuntimeError("Unarchived files in disposable workspace: preserve them")
    report = directory / "host_chromium_regrade.json"
    proof_zip = directory.parent / (TASK + "__" + arm + "__host_regrade.zip")
    if not report.is_file() or not proof_zip.is_file():
        raise RuntimeError("No independent host grader report/archive; preserve workspace")
    grade = json.loads(report.read_text(encoding="utf-8"))
    original = directory / "result.json"
    if (grade.get("task_id") != TASK or grade.get("arm") != arm
            or grade.get("label") != label
            or grade.get("source_cli_result_sha256") != sha(original)
            or grade.get("external_grade") not in ("PASS", "FAIL", "INFRA_INVALID")):
        raise RuntimeError("Host grade does not bind to original CLI result")
    with zipfile.ZipFile(proof_zip) as zipper:
        if zipper.testzip() is not None or sha(report) != hashlib.sha256(
            zipper.read("host_chromium_regrade.json")).hexdigest():
            raise RuntimeError("Independent grader evidence failed archive check")
    size = sum(p.stat().st_size for p in workspace.rglob("*") if p.is_file())
    return {
        "label": label, "arm": arm, "task": TASK, "workspace": str(workspace),
        "bytes_reclaimable": size, "external_grade": grade["external_grade"],
        "original_evidence_sha256": sha(archive), "independent_grade_sha256": sha(proof_zip)
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--arm", required=True, choices=ARMS)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume-interrupted", action="store_true",
                        help="Only missing files from already verified ZIP are tolerated")
    a = parser.parse_args()
    entry = inspect(a.label, a.arm, resume_interrupted=a.resume_interrupted)
    if not a.execute:
        print(json.dumps({"dry_run": True, **entry}, ensure_ascii=False))
        return 0
    c_before = shutil.disk_usage("C:\\").free
    d_before = shutil.disk_usage("D:\\").free
    workspace = Path(entry["workspace"])
    def onexc(func, path, error):
        candidate = Path(path)
        if (not candidate.resolve().is_relative_to(workspace.resolve())
                or not isinstance(error, PermissionError)):
            raise error
        os.chmod(candidate, 0o777 if candidate.is_dir() else 0o666)
        func(path)
    with (ROOT / "HARD20_SAFE_CLEANUP_LEDGER.jsonl").open("a", encoding="utf-8") as writer:
        writer.write(json.dumps({"event": "CLEANUP_VALIDATED_BEGIN", **entry,
                                 "resumed": a.resume_interrupted}, ensure_ascii=False) + "\n")
    shutil.rmtree(workspace, onexc=onexc)
    entry.update({
        "dry_run": False, "removed_registered_workspace": True,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "c_free_change_bytes": shutil.disk_usage("C:\\").free - c_before,
        "d_free_change_bytes": shutil.disk_usage("D:\\").free - d_before,
    })
    with (ROOT / "HARD20_SAFE_CLEANUP_LEDGER.jsonl").open("a", encoding="utf-8") as writer:
        writer.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps(entry, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
