"""Explicit-only acceptance for the real wheel currently present in dist/."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_current_wheel_matches_source_tree() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_wheel_contents.py")],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "wheel matches source" in completed.stdout.lower(), completed.stdout
