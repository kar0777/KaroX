#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path

from patch_native_notion_provider import patch_powershell, patch_shell
from patch_notion_provider import patch_ps, patch_sh
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
        (root / "scripts" / "karox_paths.py").write_text('LEGACY_NAME = "RepoPilotBridge"\n', encoding="utf-8")
        (root / "scripts" / "product_doctor.py").write_text('LEGACY_BRAND = "Repo" + "PilotBridge"\n', encoding="utf-8")
        (root / "scripts" / "notion_setup_wizard.py").write_text(
            'CONFIG = "RepoPilotBridge"\n'
            'RU = "Tailscale должен быть запущен"\n'
            'EN = "Tailscale must be running"\n',
            encoding="utf-8",
        )

        result = rewrite(root)
        assert result["changedCount"] == 3
        assert "RepoPilotBridge" not in (root / "start.ps1").read_text(encoding="utf-8")
        assert "scripts\\karox_admin_entry.py" in (root / "start.ps1").read_text(encoding="utf-8")
        assert "RepoPilotBridge" not in (root / "start.sh").read_text(encoding="utf-8")
        assert "RepoPilotBridge" in (root / "scripts" / "karox_paths.py").read_text(encoding="utf-8")
        assert '"Repo" + "PilotBridge"' in (root / "scripts" / "product_doctor.py").read_text(encoding="utf-8")
        wizard = (root / "scripts" / "notion_setup_wizard.py").read_text(encoding="utf-8")
        assert 'CONFIG = "KaroX"' in wizard
        assert "Tailscale должен быть запущен" in wizard
        assert "Tailscale must be running" in wizard

    repo_root = Path(__file__).resolve().parents[1]
    bootstrap = (repo_root / "bootstrap.ps1").read_text(encoding="utf-8-sig")
    install_guard = (repo_root / "scripts" / "install_guard.ps1").read_text(encoding="utf-8-sig")
    cli_source = (repo_root / "scripts" / "karox_cli.py").read_text(encoding="utf-8")
    notion_entry = (repo_root / "server" / "notion_entry.py").read_text(encoding="utf-8")
    assert "scripts\\install_guard.ps1" in bootstrap
    assert "Stop-RecordedSessions" in install_guard
    assert "Stop-OrphanedRuntimeProcesses" in install_guard
    assert "taskkill.exe /PID" in install_guard
    assert "$attempts = 4" in install_guard
    assert "from karox_stop import main as stop_main" in cli_source
    assert "register_agent_tools" in notion_entry

    installer = (repo_root / "install.karox.ps1").read_text(encoding="utf-8-sig")
    assert "function Set-KaroXPath" in installer
    assert "function Write-LegacyForwarder" in installer
    assert "function Move-OutOf-AppDirectory" in installer
    assert "Move-OutOf-AppDirectory\n    if (Test-Path -LiteralPath $AppDir)" in installer
    assert "@($BinDir) + $userItems" in installer
    assert "KaroX\\bin\\karox.ps1" in installer
    assert "Where-Object { `$_.Name -ne 'bin' }" in installer
    assert "$legacyInCurrentPath" in installer
    assert installer.count("Set-Location -LiteralPath $Root") == 2

    ps_launcher = (repo_root / "start.ps1").read_text(encoding="utf-8-sig")
    sh_launcher = (repo_root / "start.sh").read_text(encoding="utf-8")
    wizard_source = (repo_root / "scripts" / "notion_setup_wizard.py").read_text(encoding="utf-8")
    doctor_source = (repo_root / "scripts" / "product_doctor.py").read_text(encoding="utf-8")
    native_source = (repo_root / "scripts" / "native_notion_provider.py").read_text(encoding="utf-8")
    assert "notion_setup_wizard.py" in ps_launcher
    assert "notion_setup_wizard.py" in sh_launcher
    assert "patch_native_notion_provider.py" in ps_launcher
    assert "patch_native_notion_provider.py" in sh_launcher
    assert "Для постоянного адреса Notion" in wizard_source
    assert "Tailscale must be running to give Notion a permanent URL" in wizard_source
    assert "scripts/notion_setup_wizard.py" in doctor_source
    assert "Built-in Notion provider" in native_source
    assert "Встроенный провайдер Notion" in native_source

    ps_core = (repo_root / "start.core.ps1").read_text(encoding="utf-8-sig")
    sh_core = (repo_root / "start.core.sh").read_text(encoding="utf-8")
    generated_ps = patch_powershell(patch_ps(ps_core, str(repo_root)))
    generated_sh = patch_shell(patch_sh(sh_core, str(repo_root)))
    for generated in (generated_ps, generated_sh):
        assert "native_notion_provider.py" in generated
        assert "NOTION PROVIDER IS LIVE" in generated
        assert "ПРОВАЙДЕР NOTION ЗАПУЩЕН" in generated
        assert "COPY URL" in generated
        assert "COPY TOKEN" in generated
    assert 'return "tailscale"' in generated_ps
    assert 'tunnel_provider="tailscale"' in generated_sh or 'printf tailscale' in generated_sh

    print("KaroX native Notion provider, process-safe updater, and runtime rebrand checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
