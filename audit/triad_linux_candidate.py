"""Repaired-wheel KaroX 5 adapter for the frozen *three official CLI* Linux DEV arena.

The candidate runs in its own lab venv, not via PYTHONPATH overlay. Both the
entrypoint and any '-I -m karox.web_check' child therefore load the SAME wheel.
The original Karo lab installation is left intact. Dependency wheels are reused,
not hermetically reinstalled. No API calls occur during module validation.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import triad_linux as linux

BASE = Path.home() / ".cache/karox-fair-linux-20261108"
VENV = BASE / "karox-candidate-browser-evidence-20261108"
CANDIDATE_PYTHON = VENV / "bin" / "python"
CANDIDATE_BIN = VENV / "bin" / "karox"
WHEEL = linux.core.ROOT / "dist-triad-browser-producer" / "karox_runtime-5.0.0-py3-none-any.whl"
EXPECTED_SHA = "da881029a7959e47f3e5188d3920ef6219bad99fe9f18ab77a89dd871b4d265d"
# Frozen host-approved offline Chromium; no network or browser installation.
# SHA is checked *before* granting the executable to any Karo process; the
# installed Karo checker checks it again immediately before browser launch.
APPROVED_BROWSER = (
    Path.home() / ".cache/puppeteer/chrome-headless-shell/"
    "linux-156.0.8078.47/chrome-headless-shell-linux64/chrome-headless-shell"
)
APPROVED_BROWSER_SHA = "201ce52b0e6e571702a9bd747f2552e3d17818f1789e0967e6392d5e1a6167da"


def freeze_candidate() -> None:
    if not WHEEL.is_file() or hashlib.sha256(WHEEL.read_bytes()).hexdigest() != EXPECTED_SHA:
        raise RuntimeError("Candidate wheel SHA differs from independently frozen build")
    if not CANDIDATE_PYTHON.is_file() or not CANDIDATE_BIN.is_file():
        raise RuntimeError("Candidate isolated venv is missing; never fall back to baseline")
    # The explicit -I at the first process prevents the task's PYTHONPATH from
    # replacing Karo at startup. Its verification subprocess uses -I as well.
    first_line = CANDIDATE_BIN.read_text(encoding="utf-8").splitlines()[0]
    if first_line != f"#!{CANDIDATE_PYTHON} -I":
        raise RuntimeError("Candidate CLI must be launched through isolated Python -I")

    sites = list((VENV / "lib").glob("python*/site-packages"))
    if len(sites) != 1:
        raise RuntimeError("Cannot uniquely locate candidate venv site-packages")
    site = sites[0]
    dependencies = BASE / "karox-venv" / "lib" / site.parent.name / "site-packages"
    pth = site / "_karox_reuse_base_lab_dependencies.pth"
    if not pth.is_file() or pth.read_text(encoding="utf-8") != str(dependencies) + "\n":
        raise RuntimeError("Candidate dependency reuse path changed")
    if not dependencies.is_dir():
        raise RuntimeError("Original LAB dependency environment missing")

    # Recheck every package file against the authenticated wheel rather than
    # trusting only a single patched module.
    with zipfile.ZipFile(WHEEL) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Candidate wheel has an invalid ZIP member")
        for item in archive.namelist():
            if not item.startswith("karox/") or item.endswith("/"):
                continue
            local = site / item
            if not local.is_file():
                raise RuntimeError(f"Missing wheel package member: {item}")
            if hashlib.sha256(local.read_bytes()).digest() != hashlib.sha256(
                archive.read(item)
            ).digest():
                raise RuntimeError(f"Wheel target differs from sealed wheel: {item}")

    # This probe deliberately runs outside the task and without model transport.
    # -I must point at the candidate venv for both runtime and checker.
    probe = subprocess.run(
        [
            str(CANDIDATE_PYTHON), "-I", "-c",
            "import karox.web_document as w, karox.web_check as c; "
            "print(w.__file__); print(c.__file__)",
        ],
        cwd=linux.core.ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    if probe.returncode or any(
        not Path(path).resolve().is_relative_to(site.resolve())
        for path in probe.stdout.strip().splitlines()
    ) or len(probe.stdout.strip().splitlines()) != 2:
        raise RuntimeError("Isolated candidate runtime/checker import origin invalid")


base_command = linux.core.command_for_arm


def command_for_repaired_wheel(
    arm: str, workspace: Path, task: dict, env: dict[str, str], max_seconds: int
):
    if arm != "karox5":
        # Official OpenCode V2 and DSH remain byte-for-byte unchanged.
        return base_command(arm, workspace, task, env, max_seconds)

    freeze_candidate()
    # The installed browser belongs to host infrastructure, not the task.
    # In particular this refuses a forged, replaced or unapproved executable.
    if (
        not APPROVED_BROWSER.is_file()
        or APPROVED_BROWSER.is_symlink()
        or hashlib.sha256(APPROVED_BROWSER.read_bytes()).hexdigest()
        != APPROVED_BROWSER_SHA
    ):
        raise RuntimeError("DEV browser producer failed its immutable host SHA check")
    env["KAROX_APPROVED_CHROMIUM_EXECUTABLE"] = str(APPROVED_BROWSER)
    env["KAROX_APPROVED_CHROMIUM_SHA256"] = APPROVED_BROWSER_SHA

    # Same exact candidate wheel in CLI and its -I subprocess; only the
    # candidate entrypoint changes, never OpenCode or DeepSeek Harness.
    linux.core.KARO = CANDIDATE_BIN
    command, profile_file = base_command(arm, workspace, task, env, max_seconds)
    if (workspace / "index.html").is_file() and (
        task.get("require_visual") is True
        or task.get("category") == "threejs-webgl"
    ):
        # Browser execution is explicitly added to Core's approved verification
        # allowlist. It is generic installed KaroX web_check, NOT the grader
        # or the hidden/reference test. Source lives outside agent workspace.
        approved = [
            str(CANDIDATE_PYTHON), "-m", "karox.web_check",
            "--render", "index.html",
        ]
        command += ["--verification-command", json.dumps(approved)]
    return command, profile_file


linux.core.command_for_arm = command_for_repaired_wheel

if __name__ == "__main__":
    raise SystemExit(linux.core.main())
