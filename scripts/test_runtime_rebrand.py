#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path

from rebrand_runtime import rewrite


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "scripts").mkdir()
        (root / "start.ps1").write_text(
            '$RuntimeDir = Join-Path $env:LOCALAPPDATA "RepoPilotBridge"\n'
            '$Admin = "scripts\\karox_cli.py"\n',
            encoding="utf-8",
        )
        (root / "start.sh").write_text(
            'RUNTIME="$HOME/.local/share/RepoPilotBridge"\n'
            'ADMIN="scripts/karox_cli.py"\n',
            encoding="utf-8",
        )
        (root / "scripts" / "karox_paths.py").write_text(
            'LEGACY_NAME = "RepoPilotBridge"\n', encoding="utf-8"
        )
        (root / "scripts" / "product_doctor.py").write_text(
            'LEGACY_BRAND = "Repo" + "PilotBridge"\n', encoding="utf-8"
        )

        result = rewrite(root)

        assert result["changedCount"] == 2
        assert "RepoPilotBridge" not in (root / "start.ps1").read_text(encoding="utf-8")
        assert "scripts\\karox_admin_entry.py" in (root / "start.ps1").read_text(encoding="utf-8")
        assert "RepoPilotBridge" not in (root / "start.sh").read_text(encoding="utf-8")
        assert "RepoPilotBridge" in (root / "scripts" / "karox_paths.py").read_text(encoding="utf-8")
        assert '"Repo" + "PilotBridge"' in (root / "scripts" / "product_doctor.py").read_text(encoding="utf-8")

    repo_root = Path(__file__).resolve().parents[1]
    installer = (repo_root / "install.karox.ps1").read_text(encoding="utf-8-sig")
    assert "function Set-KaroXPath" in installer
    assert "function Write-LegacyForwarder" in installer
    assert "@($BinDir) + $userItems" in installer
    assert "KaroX\\bin\\karox.ps1" in installer
    assert "Where-Object { `$_.Name -ne 'bin' }" in installer
    assert "$legacyInCurrentPath" in installer

    print("KaroX runtime rebrand and Windows shim migration checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
