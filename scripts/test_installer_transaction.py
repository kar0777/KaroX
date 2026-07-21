"""Regression contracts for safe KaroX self-update and atomic Windows install."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
installer = (ROOT / "install.karox.ps1").read_text(encoding="utf-8-sig")
guard = (ROOT / "scripts" / "install_guard.ps1").read_text(encoding="utf-8-sig")
admin = (ROOT / "scripts" / "karox_admin.py").read_text(encoding="utf-8")
patcher = (ROOT / "scripts" / "patch_notion_provider.py").read_text(encoding="utf-8")

# Never remove the live app before a complete candidate has passed validation.
assert 'app.new-' in installer
assert '$RollbackAppDir = Join-Path $RuntimeDir "app.previous"' in installer
assert 'function Recover-PendingRollback' in installer
assert 'function Test-StagedLaunchers' in installer
assert 'function Promote-StagedApp' in installer
assert 'The previous installation was preserved' in installer
assert 'Copy-AppFiles $StagingAppDir' in installer
assert 'Test-StagedLaunchers $StagingAppDir' in installer
assert 'Promote-StagedApp' in installer
assert 'Remove-Item -LiteralPath $AppDir -Recurse -Force' not in installer
prepare_body = installer[installer.index('function Prepare-StagedApp') : installer.index('function Promote-StagedApp')]
assert 'Test-StagedLaunchers $StagingAppDir' in prepare_body
activation_flow = installer[installer.rindex('try {\n    Promote-StagedApp') :]
assert 'Promote-StagedApp' in activation_flow

# The guard must stop the real v4 runtime, including app_entry and watchdog.
assert 'app_entry:app' in guard
assert 'karox_supervisor\\.py' in guard
assert 'Wait-UpdateParentExit' in guard
assert 'app-lock' in guard

# The updater must exit before replacement and keep its bootstrap script alive.
assert 'KAROX_UPDATE_PARENT_PID' in admin
assert 'subprocess.Popen' in admin
assert 'subprocess.call(command, env=env)' not in admin
assert 'tempfile.mkdtemp' in admin
assert 'TemporaryDirectory(prefix="karox-update-")' not in admin

# Patcher must match the per-session provider flow introduced in 4.1.3.
assert 'Select-SessionTunnelProvider' in patcher
assert 'PATCHER_VERSION = "3.13.1"' in patcher

print("KaroX atomic installer/update regression checks passed")
