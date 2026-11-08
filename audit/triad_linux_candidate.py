"""Official Linux triad adapter with the repaired *wheel* for KaroX only.

Model/provider/tools/permissions and the two official competitor binaries remain
identical to audit/triad_linux.py. No extra agents. This module is prepared for
a future explicitly permitted DEV epoch; importing it requires the same W&B
credential readiness as the original Linux adapter. It does NOT launch a run.
The external Windows Chromium scorer was frozen separately before any paid arm.
"""
from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

import triad_linux as linux

BASE = Path.home() / ".cache/karox-fair-linux-20261108"
TARGET = BASE / "karox-candidate-wheel-20261108"
WHEEL = linux.core.ROOT / "dist-triad-candidate" / "karox_runtime-5.0.0-py3-none-any.whl"
EXPECTED_SHA = "485c8ebfbcff994941c09375eda7907c29ad99fd74bedb542b448c2b0affbdc0"


def freeze_candidate() -> None:
    if not WHEEL.is_file() or hashlib.sha256(WHEEL.read_bytes()).hexdigest() != EXPECTED_SHA:
        raise RuntimeError("Candidate wheel SHA differs from independently frozen build")
    module = TARGET / "karox" / "web_document.py"
    if not module.is_file():
        raise RuntimeError("Offline wheel target missing; do not fall back to baseline")
    with zipfile.ZipFile(WHEEL) as archive:
        original = archive.read("karox/web_document.py")
    if hashlib.sha256(module.read_bytes()).digest() != hashlib.sha256(original).digest():
        raise RuntimeError("Wheel target has been mutated; candidate code not authentic")


base_command = linux.core.command_for_arm


def command_for_repaired_wheel(arm: str, workspace: Path, task: dict,
                               env: dict[str, str], max_seconds: int):
    if arm == "karox5":
        # The native official v5 entrypoint stays the original lab executable,
        # but its Python import resolves against the repaired wheel *first*.
        # It reuses only dependency wheels from the existing lab environment.
        freeze_candidate()
        env["PYTHONPATH"] = str(TARGET) + os.pathsep + env.get("PYTHONPATH", "")
    return base_command(arm, workspace, task, env, max_seconds)


linux.core.command_for_arm = command_for_repaired_wheel

if __name__ == "__main__":
    raise SystemExit(linux.core.main())
