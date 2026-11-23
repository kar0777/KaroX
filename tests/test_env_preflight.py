"""Tests for one-shot environment preflight (configured checkers and missing test/dev packages)."""

from __future__ import annotations

import asyncio
import json
import os
import platform
from pathlib import Path

from karox6.core import prompt as prompt_mod
from karox6.core.prompt import (
    _format_checkers_line,
    _format_missing_deps_line,
    environment_block,
    environment_preflight,
)
from karox6.core.session import Session, SessionConfig
from karox6.providers.scripted import ScriptedProvider, Turn
from karox6.tools.shell import detect_shell

_CHECKER_MISSING_SUFFIX = (
    " (try the project runner such as uv run / poetry run / npx before skipping;"
    " do not install unless the task asks)"
)
_DEPS_SUFFIX = " (at session start; do not install unless the task asks)"


def _clear_env_paths(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)


def _touch_exe(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o755)
    return path


def _make_site_packages(
    venv_dir: Path,
    dists: tuple[str, ...] | list[str] = (),
    *,
    layout: str = "win",
    py_version: str | None = None,
) -> Path:
    if layout == "posix":
        site = venv_dir / "lib" / "python3.12" / "site-packages"
    else:
        site = venv_dir / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    if py_version is not None:
        (venv_dir / "pyvenv.cfg").write_text(f"version_info = {py_version}\n", encoding="utf-8")
    for dist in dists:
        (site / dist).mkdir(parents=True, exist_ok=True)
    return site


def test_preflight_no_line_when_nothing_configured(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    assert environment_preflight(tmp_path) == []
    block = environment_block(tmp_path, detect_shell())
    assert "Checkers configured by the project:" not in block
    assert "Declared test/dev packages missing from" not in block


def test_preflight_pyproject_checkers_with_fake_venv(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 100\n\n"
        "[tool.mypy]\nstrict = true\n\n"
        "[tool.pytest.ini_options]\naddopts = '-q'\n\n"
        "[tool.black]\nline-length = 100\n",
        encoding="utf-8",
    )
    _touch_exe(tmp_path / ".venv" / "bin" / "ruff")
    _touch_exe(tmp_path / ".venv" / "Scripts" / "pytest.exe")

    lines = environment_preflight(tmp_path)
    assert lines == [
        "Checkers configured by the project: available: ruff (.venv), pytest (.venv); "
        f"not found at session start: mypy, black{_CHECKER_MISSING_SUFFIX}"
    ]


def test_preflight_all_or_none_checkers_available(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\n[tool.pytest.ini_options]\n",
        encoding="utf-8",
    )
    assert environment_preflight(tmp_path) == [
        f"Checkers configured by the project: not found at session start: ruff, pytest"
        f"{_CHECKER_MISSING_SUFFIX}"
    ]

    _touch_exe(tmp_path / ".venv" / "bin" / "ruff")
    _touch_exe(tmp_path / ".venv" / "bin" / "pytest")
    assert environment_preflight(tmp_path) == [
        "Checkers configured by the project: available at session start: "
        "ruff (.venv), pytest (.venv)"
    ]


def test_preflight_setup_cfg_tox_flake8_nox_and_external_path(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    path_bin = tmp_path / "ext_path"
    path_bin.mkdir()
    flake8_bin = path_bin / ("flake8.cmd" if platform.system() == "Windows" else "flake8")
    _touch_exe(flake8_bin)
    monkeypatch.setenv("PATH", str(path_bin))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)

    (ws / "setup.cfg").write_text(
        "[metadata]\nname = demo\n\n"
        "[tool:pytest]\naddopts = -q\n\n"
        "[isort]\nprofile = black\n\n"
        "[mypy-demo.*]\nignore_missing_imports = True\n\n"
        "[pylint.master]\njobs = 1\n",
        encoding="utf-8",
    )
    (ws / "tox.ini").write_text("[tox]\nenvlist = py312\n", encoding="utf-8")
    (ws / ".flake8").write_text("[flake8]\nmax-line-length = 100\n", encoding="utf-8")
    (ws / "noxfile.py").write_text("import nox\n", encoding="utf-8")
    _touch_exe(ws / "venv" / "bin" / "pytest")

    lines = environment_preflight(ws)
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith(
        "Checkers configured by the project: available: pytest (venv), flake8 (PATH);"
    )
    assert (
        f"not found at session start: isort, mypy, pylint, tox, nox{_CHECKER_MISSING_SUFFIX}"
        in line
    )
    assert len(f"- {line}") <= 300


def test_preflight_package_json_checkers_and_node_modules_bin(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    pkg = {
        "devDependencies": {
            "typescript": "^5.0.0",
            "eslint": "^9.0.0",
            "prettier": "^3.0.0",
            "eslint-plugin-react": "^7.0.0",
        },
        "scripts": {
            "test": "vitest run",
            "e2e": "./node_modules/.bin/jest --ci",
            "misc": "node jest.config.js && echo eslint-plugin-x",
        },
    }
    (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    node_bin = tmp_path / "node_modules" / ".bin"
    _touch_exe(node_bin / "eslint")
    _touch_exe(node_bin / "tsc.cmd")
    _touch_exe(node_bin / "vitest")

    lines = environment_preflight(tmp_path)
    assert lines == [
        "Checkers configured by the project: available: "
        "tsc (node_modules), eslint (node_modules), vitest (node_modules); "
        f"not found at session start: prettier, jest{_CHECKER_MISSING_SUFFIX}"
    ]


def test_preflight_posix_requires_x_ok(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    non_exec = tmp_path / ".venv" / "bin" / "ruff"
    non_exec.parent.mkdir(parents=True)
    non_exec.write_text("#!/bin/sh\n", encoding="utf-8")
    if os.name != "nt":
        non_exec.chmod(0o644)
    else:
        monkeypatch.setattr(prompt_mod.os, "name", "posix")
        monkeypatch.setattr(prompt_mod.os, "access", lambda _p, _m: False)

    lines = environment_preflight(tmp_path)
    assert lines == [
        f"Checkers configured by the project: not found at session start: ruff"
        f"{_CHECKER_MISSING_SUFFIX}"
    ]


def test_preflight_dist_info_and_egg_info_inspection_and_self_extras(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n\n"
        "[project.optional-dependencies]\n"
        "test = ['PyTest>=8', 'Pandas[excel]>=2.0', 'demo[extra-self]', 'demo']\n"
        "extra-self = ['numpy>=1.26', 'demo[test]']\n"
        "docs = ['sphinx']\n\n"
        "[dependency-groups]\n"
        "dev = [{include-group = 'extra-test'}, 'matplotlib>=3.8', 'ruff']\n"
        "extra-test = ['Scikit_Learn>=1.4']\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements-test-extra.txt").write_text(
        "# comment\n"
        "-r requirements.txt\n"
        "scipy>=1.11  # inline comment\n"
        "Pillow>=10.0 ; sys_platform != 'nonexistent_os'\n"
        "fake-other-os>=1.0 ; sys_platform == 'nonexistent_os'\n",
        encoding="utf-8",
    )
    _make_site_packages(
        tmp_path / ".venv",
        dists=["scipy-1.13.0.dist-info", "numpy-2.0.0-py3.12.egg-info"],
        layout="posix",
        py_version="3.12.4",
    )

    lines = environment_preflight(tmp_path)
    assert lines == [
        "Declared test/dev packages missing from .venv: "
        f"pandas, scikit-learn, matplotlib, pillow{_DEPS_SUFFIX}"
    ]


def test_preflight_setup_cfg_self_name_and_self_extras(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "setup.cfg").write_text(
        "[metadata]\nname = My_Demo\n\n"
        "[options.extras_require]\n"
        "dev =\n"
        "    my-demo\n"
        "    my_demo[sub]\n"
        "    matplotlib\n"
        "sub =\n"
        "    scipy\n"
        "    my-demo[dev]\n",
        encoding="utf-8",
    )
    _make_site_packages(tmp_path / "venv", dists=["scipy-1.13.0.dist-info"])

    lines = environment_preflight(tmp_path)
    assert lines == [f"Declared test/dev packages missing from venv: matplotlib{_DEPS_SUFFIX}"]


def test_preflight_workspace_interpreter_never_executed(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    sentinel = tmp_path / "executed.sentinel"
    (tmp_path / "pyproject.toml").write_text(
        "[project.optional-dependencies]\ntest = ['pandas']\n",
        encoding="utf-8",
    )
    if platform.system() == "Windows":
        fake_py = tmp_path / ".venv" / "Scripts" / "python.cmd"
        fake_py.parent.mkdir(parents=True)
        fake_py.write_text(f'@echo pwned > "{sentinel}"\n', encoding="utf-8")
        fake_bat = tmp_path / ".venv" / "Scripts" / "python.bat"
        fake_bat.write_text(f'@echo pwned > "{sentinel}"\n', encoding="utf-8")
    else:
        fake_py = tmp_path / ".venv" / "bin" / "python"
        fake_py.parent.mkdir(parents=True)
        fake_py.write_text(f'#!/bin/sh\necho pwned > "{sentinel}"\n', encoding="utf-8")
        fake_py.chmod(0o755)
    (tmp_path / "sitecustomize.py").write_text(
        f"open({str(sentinel)!r}, 'w').write('pwned')\n",
        encoding="utf-8",
    )
    _make_site_packages(tmp_path / ".venv", dists=[])

    def fail_subprocess(*_a, **_kw):
        raise AssertionError("subprocess.run must never be called by environment_preflight")

    monkeypatch.setattr(prompt_mod.subprocess, "run", fail_subprocess)

    lines = environment_preflight(tmp_path)
    assert not sentinel.exists()
    assert lines == [f"Declared test/dev packages missing from .venv: pandas{_DEPS_SUFFIX}"]


def test_preflight_workspace_python_bat_and_ruff_bat_ignored(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    sentinel = tmp_path / "bat_ran.sentinel"
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\n\n[project.optional-dependencies]\ntest = ['pandas']\n",
        encoding="utf-8",
    )
    _touch_exe(tmp_path / "python.bat", f'@echo pwned > "{sentinel}"\n')
    _touch_exe(tmp_path / "ruff.bat", f'@echo pwned > "{sentinel}"\n')
    _touch_exe(tmp_path / "ruff", f'#!/bin/sh\necho pwned > "{sentinel}"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))

    lines = environment_preflight(tmp_path)
    assert not sentinel.exists()
    assert lines == [
        f"Checkers configured by the project: not found at session start: ruff"
        f"{_CHECKER_MISSING_SUFFIX}"
    ]


def test_preflight_no_venv_omits_deps_line(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text(
        "[project.optional-dependencies]\ntest = ['nonexistent-karox-dep-xyz>=1']\n",
        encoding="utf-8",
    )
    # Even if a fake interpreter exists without site-packages, no venv is detected.
    _touch_exe(tmp_path / ".venv" / "bin" / "python")
    assert environment_preflight(tmp_path) == []


def test_preflight_explicit_virtual_env_site_packages(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    ext_venv = tmp_path / "poetry-venv"
    _make_site_packages(
        ext_venv,
        dists=["prompt_toolkit-3.0.48.dist-info"],
        layout="posix",
        py_version="3.12.2",
    )
    monkeypatch.setenv("VIRTUAL_ENV", str(ext_venv))
    (ws / "pyproject.toml").write_text(
        "[project.optional-dependencies]\n"
        "test = ['prompt-toolkit>=3', 'nonexistent_karox_dep_xyz>=1']\n",
        encoding="utf-8",
    )

    lines = environment_preflight(ws)
    assert lines == [
        "Declared test/dev packages missing from VIRTUAL_ENV: "
        f"nonexistent-karox-dep-xyz{_DEPS_SUFFIX}"
    ]


def test_preflight_markers_python_version_and_unknown_skipped(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text(
        "[project.optional-dependencies]\n"
        "test = [\n"
        "  'tomli>=2.0; python_version < \"3.11\"',\n"
        "  'exceptiongroup; python_version < \"3.11\"',\n"
        "  'backport-old; python_full_version < \"3.12.1\"',\n"
        "  'py312-only; python_version >= \"3.12\"',\n"
        "  'pypy-pkg; implementation_name == \"pypy\"',\n"
        "  'extra-cond; extra == \"all\"',\n"
        "  'pandas>=2.0',\n"
        "]\n",
        encoding="utf-8",
    )
    # pyvenv.cfg has 3.12.4 -> tomli/exceptiongroup/backport-old are False; py312-only is True;
    # implementation_name and extra cannot be evaluated statically -> skipped.
    _make_site_packages(tmp_path / ".venv", dists=[], py_version="3.12.4")
    lines = environment_preflight(tmp_path)
    assert lines == [
        f"Declared test/dev packages missing from .venv: py312-only, pandas{_DEPS_SUFFIX}"
    ]

    # Without pyvenv.cfg version, python_version markers cannot be evaluated -> skipped.
    (tmp_path / ".venv" / "pyvenv.cfg").unlink()
    lines_no_cfg = environment_preflight(tmp_path)
    assert lines_no_cfg == [f"Declared test/dev packages missing from .venv: pandas{_DEPS_SUFFIX}"]

    # With pyvenv.cfg 3.10.12 -> tomli, exceptiongroup, backport-old apply; py312-only does not.
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("version = 3.10.12\n", encoding="utf-8")
    lines_310 = environment_preflight(tmp_path)
    assert lines_310 == [
        "Declared test/dev packages missing from .venv: "
        f"tomli, exceptiongroup, backport-old, pandas{_DEPS_SUFFIX}"
    ]


def test_preflight_skips_editable_paths_urls_git_and_hash_options(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "requirements-test.txt").write_text(
        "-e .\n"
        "-e ./subpkg\n"
        ".\n"
        "./local-pkg\n"
        "../sibling-pkg\n"
        "C:\\local\\winpkg\n"
        "relative/subpkg\n"
        "git+https://github.com/org/repo.git@main\n"
        "https://example.com/pkg-1.0-py3-none-any.whl\n"
        "https://example.com/bare-url\n"
        "httpx @ https://example.com/httpx.whl\n"
        "hashed-pkg --hash=sha256:deadbeef\n"
        "pinned-hashed==1.0 \\\n"
        "    --hash=sha256:0123456789abcdef\n"
        "pandas>=2.0\n",
        encoding="utf-8",
    )
    _make_site_packages(tmp_path / ".venv", dists=[])

    lines = environment_preflight(tmp_path)
    assert lines == [f"Declared test/dev packages missing from .venv: pandas{_DEPS_SUFFIX}"]


def test_preflight_ruff_in_dev_group_and_on_path_not_reported_missing(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    ext_path = tmp_path / "ext_bin"
    ext_path.mkdir()
    _touch_exe(ext_path / ("ruff.cmd" if platform.system() == "Windows" else "ruff"))
    _touch_exe(ext_path / ("pre-commit.cmd" if platform.system() == "Windows" else "pre-commit"))
    monkeypatch.setenv("PATH", str(ext_path))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)

    (ws / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 100\n\n"
        "[dependency-groups]\n"
        "dev = ['ruff', 'mypy', 'pre-commit', 'pandas']\n",
        encoding="utf-8",
    )
    _make_site_packages(ws / ".venv", dists=[])

    lines = environment_preflight(ws)
    assert lines == [
        "Checkers configured by the project: available at session start: ruff (PATH)",
        f"Declared test/dev packages missing from .venv: pandas{_DEPS_SUFFIX}",
    ]


def test_preflight_omits_deps_line_when_all_installed(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "requirements-dev.txt").write_text("pandas\nscipy\n", encoding="utf-8")
    _make_site_packages(
        tmp_path / ".venv",
        dists=["pandas-2.2.0.dist-info", "scipy-1.13.0.dist-info"],
    )
    assert environment_preflight(tmp_path) == []


def test_preflight_survives_malformed_and_deeply_nested_configs(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text("[tool.ruff\nbroken toml", encoding="utf-8")
    (tmp_path / "package.json").write_text("{broken json", encoding="utf-8")
    (tmp_path / "setup.cfg").write_text("no section header = 1\n", encoding="utf-8")
    assert environment_preflight(tmp_path) == []

    # Deeply nested TOML array (~2000 levels) can trigger RecursionError in tomllib.
    depth = 2000
    (tmp_path / "pyproject.toml").write_text(
        "a = " + "[" * depth + "]" * depth + "\n",
        encoding="utf-8",
    )
    assert environment_preflight(tmp_path) == []


def test_preflight_checker_phase_respects_deadline(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\n[tool.mypy]\n\n[project.optional-dependencies]\ntest = ['pandas']\n",
        encoding="utf-8",
    )
    _make_site_packages(tmp_path / ".venv", dists=[])

    assert environment_preflight(tmp_path, budget_s=0.0) == []

    calls = 0

    def advancing_monotonic() -> float:
        nonlocal calls
        calls += 1
        # Initial deadline check passes, then checker loop sees expired deadline.
        return 100.0 if calls <= 3 else 200.0

    monkeypatch.setattr(prompt_mod.time, "monotonic", advancing_monotonic)
    assert environment_preflight(tmp_path, budget_s=1.0) == []


def test_preflight_caps_at_8_items_and_line_budget_by_whole_items():
    many = [f"pkg{i}" for i in range(12)]
    line = _format_missing_deps_line(many)
    assert line is not None
    assert len(f"- {line}") <= 300
    assert "pkg0, pkg1, pkg2, pkg3, pkg4, pkg5, pkg6, pkg7, ..." in line
    assert "pkg8" not in line

    long_names = [f"very-long-package-name-{i:02d}" for i in range(8)]
    long_line = _format_missing_deps_line(long_names)
    assert long_line is not None
    assert len(f"- {long_line}") <= 300
    assert ", ... (" in long_line
    body = long_line.split("Declared test/dev packages missing from .venv: ", 1)[1].split(" (", 1)[
        0
    ]
    parts = [p.strip() for p in body.split(",")]
    assert parts[-1] == "..."
    assert all(p in long_names for p in parts[:-1])

    long_checkers_avail = [f"checker-a-{i:02d} (.venv)" for i in range(8)]
    long_checkers_miss = [f"checker-m-{i:02d}" for i in range(8)]
    chk_line = _format_checkers_line(long_checkers_avail, long_checkers_miss)
    assert chk_line is not None
    assert len(f"- {chk_line}") <= 300
    assert ", ..." in chk_line


def test_preflight_stability_and_session_computes_once(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\n[tool.mypy]\n\n[project.optional-dependencies]\ntest = ['pandas']\n",
        encoding="utf-8",
    )
    _touch_exe(tmp_path / ".venv" / "bin" / "ruff")
    _make_site_packages(tmp_path / ".venv", dists=[])

    shell = detect_shell()
    first = environment_block(tmp_path, shell)
    second = environment_block(tmp_path, shell)
    assert first == second
    assert (
        "- Checkers configured by the project: available: ruff (.venv); "
        "not found at session start: mypy"
    ) in first
    assert "- Declared test/dev packages missing from .venv: pandas (" in first

    preflight_calls = 0
    real_preflight = prompt_mod.environment_preflight

    def counting_preflight(workspace, **kw):
        nonlocal preflight_calls
        preflight_calls += 1
        return real_preflight(workspace, **kw)

    monkeypatch.setattr(prompt_mod, "environment_preflight", counting_preflight)

    provider = ScriptedProvider(
        [
            Turn(calls=[("read_file", {"path": "pyproject.toml"})]),
            Turn(text="done"),
        ]
    )
    config = SessionConfig(model="test", workspace=tmp_path, home=tmp_path / "home")

    async def run_session():
        return [ev async for ev in Session(config, provider).run("inspect")]

    asyncio.run(run_session())
    assert preflight_calls == 1
    assert len(provider.requests) == 2
    assert provider.requests[0].system == provider.requests[1].system
