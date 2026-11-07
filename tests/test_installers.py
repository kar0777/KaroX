"""The one-line 5.0.0 installer, driven against a fake pipx.

Every scenario runs the real ``scripts/install.sh`` or ``scripts/install.ps1``
with a recording fake pipx. No test touches the user's real KaroX data: config,
runtime, APPDATA, LOCALAPPDATA and HOME all point into ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = "karox-runtime-5.0.0-test-spec"
FAKE_PIPX = r'''
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_PIPX_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")
if args[:2] == ["list", "--json"]:
    print(os.environ.get("FAKE_PIPX_LIST") or '{"venvs": {}}')
elif args[:1] == ["environment"]:
    print(os.environ["FAKE_PIPX_VENVS"])
elif args[:1] == ["install"]:
    target = os.path.join(os.environ["FAKE_PIPX_VENVS"], "karox-runtime")
    if not os.path.isdir(target):
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", target], check=True)
'''


def _venvs(**versions: str) -> str:
    return json.dumps(
        {
            "venvs": {
                name: {"metadata": {"main_package": {"package_version": version}}}
                for name, version in versions.items()
            }
        }
    )


def _powershell() -> Optional[str]:
    if os.name != "nt":
        return None
    return shutil.which("powershell") or shutil.which("pwsh")


SHELLS = [
    pytest.param(
        "sh",
        marks=pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None, reason="POSIX sh"),
    ),
    pytest.param(
        "powershell",
        marks=pytest.mark.skipif(_powershell() is None, reason="Windows PowerShell"),
    ),
]


def _install(
    shell: str,
    tmp_path: Path,
    listing: str = "",
    *,
    env: Optional[dict[str, str]] = None,
    karox_version: Optional[str] = None,
    venvs_name: str = "venvs",
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "fake_pipx.py"
    script.write_text(FAKE_PIPX, encoding="utf-8")
    if shell == "sh":
        pipx = bin_dir / "pipx-fake"
        pipx.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        pipx.chmod(0o755)
        # Pin the command version so the test never sees the developer venv.
        karox = bin_dir / "karox"
        karox.write_text(
            f"#!/bin/sh\necho 'karox {karox_version or '0.0.0-test'}'\n",
            encoding="utf-8",
        )
        karox.chmod(0o755)
        path = os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent), "/usr/bin", "/bin"])
        argv = ["sh", str(ROOT / "scripts" / "install.sh")]
    else:
        pipx = bin_dir / "pipx-fake.cmd"
        pipx.write_text('@"%FAKE_PIPX_PYTHON%" "%FAKE_PIPX_SCRIPT%" %*\r\n', encoding="ascii")
        (bin_dir / "karox.cmd").write_text(
            f"@echo karox {karox_version or '0.0.0-test'}\r\n", encoding="utf-8"
        )
        windows = os.environ.get("SystemRoot", r"C:\Windows")
        path = os.pathsep.join(
            [str(bin_dir), str(Path(sys.executable).parent), os.path.join(windows, "System32"), windows,
             os.path.join(windows, "System32", "WindowsPowerShell", "v1.0")]
        )
        argv = [str(_powershell()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(ROOT / "scripts" / "install.ps1")]
    venvs = tmp_path / venvs_name
    venvs.mkdir(exist_ok=True)
    log = tmp_path / "pipx.log"
    data = tmp_path / "data"
    config = data / "config"
    runtime = data / "runtime"
    config.mkdir(parents=True)
    runtime.mkdir(parents=True)
    (config / "providers.json").write_text("{}", encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("KAROX_") and key.upper() not in {"PATH", "APPDATA", "LOCALAPPDATA", "HOME"}
    }
    environment.update(
        {
            "PATH": path,
            "KAROX_PIPX": str(pipx),
            "KAROX_SPEC": SPEC,
            "KAROX_CONFIG_DIR": str(config),
            "KAROX_RUNTIME_DIR": str(runtime),
            "APPDATA": str(data / "appdata"),
            "LOCALAPPDATA": str(data / "localappdata"),
            "HOME": str(data / "home"),
            "FAKE_PIPX_LOG": str(log),
            "FAKE_PIPX_LIST": listing,
            "FAKE_PIPX_VENVS": str(venvs),
            "FAKE_PIPX_PYTHON": sys.executable,
            "FAKE_PIPX_SCRIPT": str(script),
        }
    )
    environment.update(env or {})
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=180,
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return completed, calls, venvs, data


def _installs(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if call[:1] == ["install"]]


def _backup(data: Path) -> list[Path]:
    return sorted(data.rglob("*.backup-*"))


@pytest.mark.parametrize("shell", SHELLS)
def test_fresh_machine_installs_karox(shell: str, tmp_path: Path) -> None:
    completed, calls, _venvs_dir, data = _install(shell, tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _installs(calls) == [["install", SPEC]]
    assert not _backup(data)


@pytest.mark.parametrize("shell", SHELLS)
def test_existing_v5_is_not_replaced_without_opt_in(shell: str, tmp_path: Path) -> None:
    completed, calls, _venvs_dir, _data = _install(
        shell, tmp_path, _venvs(**{"karox-runtime": "5.0.0rc4"})
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _installs(calls) == []
    assert "KAROX_UPGRADE=1" in completed.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_upgrade_backs_up_data_and_forces_the_install(shell: str, tmp_path: Path) -> None:
    completed, calls, _venvs_dir, data = _install(
        shell,
        tmp_path,
        _venvs(**{"karox-runtime": "5.0.0rc4"}),
        env={"KAROX_UPGRADE": "1"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _installs(calls) == [["install", "--force", SPEC]]
    assert _backup(data), completed.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_upgrade_from_6_alpha_is_explicit_and_backed_up(shell: str, tmp_path: Path) -> None:
    completed, calls, _venvs_dir, data = _install(
        shell,
        tmp_path,
        _venvs(**{"karox-runtime": "6.0.0a3"}),
        env={"KAROX_UPGRADE": "1"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _installs(calls) == [["install", "--force", SPEC]]
    assert _backup(data)


@pytest.mark.parametrize("shell", SHELLS)
def test_side_by_side_karox6_is_left_untouched(shell: str, tmp_path: Path) -> None:
    listing = _venvs(**{"karox-runtime": "5.0.0rc4", "karox-runtime6": "6.0.0a3"})
    completed, calls, _venvs_dir, _data = _install(shell, tmp_path, listing)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _installs(calls) == []
    assert "side-by-side karox6" in completed.stdout
    assert "5.0.0rc4" in completed.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_karox_outside_pipx_requires_opt_in_then_upgrades(shell: str, tmp_path: Path) -> None:
    completed, calls, _venvs_dir, _data = _install(shell, tmp_path, karox_version="5.0.0rc4")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _installs(calls) == []
    assert "outside pipx" in completed.stdout

    completed, calls, _venvs_dir, data = _install(
        shell,
        tmp_path / "upgrade",
        karox_version="5.0.0rc4",
        env={"KAROX_UPGRADE": "1"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _installs(calls) == [["install", "--force", SPEC]]
    assert _backup(data)


@pytest.mark.parametrize("shell", SHELLS)
def test_dry_run_changes_nothing(shell: str, tmp_path: Path) -> None:
    completed, calls, _venvs_dir, data = _install(
        shell,
        tmp_path,
        _venvs(**{"karox-runtime": "5.0.0rc4"}),
        env={"KAROX_UPGRADE": "1", "KAROX_DRY_RUN": "1"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _installs(calls) == []
    assert "would run:" in completed.stdout
    assert "would copy" in completed.stdout
    assert not _backup(data)


@pytest.mark.parametrize("shell", SHELLS)
def test_a_non_ascii_data_path_still_gets_a_backup(shell: str, tmp_path: Path) -> None:
    completed, _calls, _venvs_dir, data = _install(
        shell,
        tmp_path,
        _venvs(**{"karox-runtime": "5.0.0rc4"}),
        env={"KAROX_UPGRADE": "1"},
        venvs_name="venvs-проверка",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _backup(data), completed.stdout + completed.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_missing_spec_is_refused_before_any_install(shell: str, tmp_path: Path) -> None:
    completed, calls, _venvs_dir, _data = _install(
        shell, tmp_path, env={"KAROX_SPEC": ""}
    )
    assert completed.returncode != 0
    assert _installs(calls) == []
    assert "KAROX_SPEC is required" in completed.stdout + completed.stderr
