"""Lossless, allowlisted supplement to the frozen triad evidence ZIP.

Never reads arbitrary user paths, venv directories, credentials or project caches.
Never overwrites the 2026-11-08 original 846-asset ZIP. All original source
artifacts remain in place; ZIP includes SHA-256 of every copied byte sequence.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "TRIAD_EVIDENCE_20261108.zip"
ORIGINAL_SHA = "069fafcd6e00163230ce8dbd5323b3f7520827eb173b704dc276cd51071757c6"
OUT = ROOT / "TRIAD_EVIDENCE_SUPPLEMENT_20261108.zip"
OUT_SHA = ROOT / "TRIAD_EVIDENCE_SUPPLEMENT_20261108.sha256"

NAMED_FILES = [
    "audit/REGRESSION_SPLIT_PLAN_20261108.json",
    "audit/run_remaining_regression.py",
    "audit/pack_supplement_20261108.py",
    "audit/verify_candidate_wheel_3d.py",
    "audit/CANDIDATE_WHEEL_3D_GATE_20261108.json",
    "audit/candidate_wheel_3d_grader_20261108.log",
    "audit/security_parity_gate_20261108.log",
    "audit/full_regression_20261108.log",
    "audit/static_gate_20261108.json",
    "audit/real_three_35003_grader_v2.py",
    "audit/real_three_35003_normal_input_smoke.py",
    "audit/REAL_THREE_35003_RED_20261108.json",
    "audit/REAL_THREE_35003_RED_V2_20261108.json",
    "audit/REAL_THREE_35003_GREEN_V2_20261108.json",
    "audit/REAL_THREE_35003_ADJUDICATION_20261108.json",
    "audit/GOAL_CONTINUATION_CHECKPOINT_20261108.md",
    "TRIAD_EVIDENCE_20261108.sha256",
    "dist-triad-candidate/karox_runtime-5.0.0-py3-none-any.whl",
]
for group in range(6):
    NAMED_FILES.extend([
        f"audit/REGRESSION_GROUP_{group}_20261108.json",
        f"audit/regression_group_{group}_20261108.log",
    ])
NAMED_FILES.extend([
    "audit/REGRESSION_GROUP_2_EXTENDED_20261108.json",
    "audit/regression_group_2_extended_20261108.log",
])
PROOF_DIRS = [
    ".tmp/real-three-35003-red-proof-v2-20261108",
    ".tmp/real-three-35003-green-proof-v2-20261108",
    ".tmp/real-three-35003-normal-original-20261108",
    ".tmp/real-three-35003-normal-reference-20261108",
    ".tmp/candidate-wheel-3d-20261108/visual-proof",
]


def sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def main() -> int:
    c_free = shutil.disk_usage("C:\\").free
    d_free = shutil.disk_usage("D:\\").free
    if c_free < 7 * 1024**3 or d_free < 15 * 1024**3:
        raise RuntimeError("Disk reserve guard blocked evidence supplement")
    if OUT.exists() or OUT_SHA.exists():
        raise FileExistsError("Refusing to overwrite the supplement")
    if not ORIGINAL.is_file() or sha256(ORIGINAL.read_bytes()) != ORIGINAL_SHA:
        raise RuntimeError("Original ZIP absent or wrong SHA")
    files = set(NAMED_FILES)
    for folder in PROOF_DIRS:
        base = ROOT / folder
        if base.exists():
            for item in base.iterdir():  # bounded, flat, no venv traversal
                if item.is_file():
                    files.add(item.relative_to(ROOT).as_posix())
    existing = []
    missing = []
    root = ROOT.resolve()
    for rel in sorted(files):
        source = ROOT / rel
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise RuntimeError(f"Unsafe non-local path: {rel}")
        if source.is_file():
            existing.append(rel)
        else:
            missing.append(rel)
    temp = ROOT / "TRIAD_EVIDENCE_SUPPLEMENT_20261108.zip.partial"
    if temp.exists():
        raise FileExistsError("Refusing to overwrite earlier partial archive")
    manifest = {
        "schema": 1,
        "kind": "supplement_to_frozen_triads",
        "original_archive_sha256": ORIGINAL_SHA,
        "original_archive_file": ORIGINAL.name,
        "files": [],
        "requested_but_missing": missing,
        "not_scored_claims": ["Karo candidate has no new paid triad epoch", "Real Three #35003 is DEV RED/GREEN only"],
    }
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=8) as archive:
            for rel in existing:
                raw = (ROOT / rel).read_bytes()
                member = zipfile.ZipInfo(rel, date_time=(2026, 11, 8, 0, 0, 0))
                member.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(member, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=8)
                manifest["files"].append({"path": rel, "bytes": len(raw), "sha256": sha256(raw)})
            archive.writestr("EVIDENCE_SUPPLEMENT_MANIFEST.json", json.dumps(
                manifest, sort_keys=True, ensure_ascii=False, indent=2
            ).encode("utf-8"))
        with zipfile.ZipFile(temp) as archive:
            assert archive.testzip() is None
            for item in manifest["files"]:
                raw = archive.read(item["path"])
                assert len(raw) == item["bytes"] and sha256(raw) == item["sha256"]
        temp.rename(OUT)
        actual = sha256(OUT.read_bytes())
        OUT_SHA.write_text(f"{actual}  {OUT.name}\n", encoding="ascii")
        print(json.dumps({
            "archive": str(OUT), "sha256": actual,
            "asset_count": len(existing), "manifest_checked": True,
            "missing_optional": missing, "zip_bytes": OUT.stat().st_size,
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        # Keep partial evidence for diagnosis; do not delete arbitrary files.
        raise


if __name__ == "__main__":
    raise SystemExit(main())
