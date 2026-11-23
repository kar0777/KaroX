"""Tests for one-shot environment preflight (configured checkers and missing test deps)."""

from __future__ import annotations

import asyncio
import json
import platform
import subprocess
import sys

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


def _clear_env_paths(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)


def test_preflight_no_line_when_nothing_configured(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    assert environment_preflight(tmp_path) == []
    block = environment_block(tmp_path, detect_shell())
    assert "Checkers configured by the project:" not in block
    assert "Declared test dependencies not installed:" not in block


def test_preflight_pyproject_checkers_with_fake_venv(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 100\n\n"
        "[tool.mypy]\nstrict = true\n\n"
        "[tool.pytest.ini_options]\naddopts = '-q'\n\n"
        "[tool.black]\nline-length = 100\n",
        encoding="utf-8",
    )
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "ruff").write_text("", encoding="utf-8")
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts" / "pytest.exe").write_text("", encoding="utf-8")

    lines = environment_preflight(tmp_path)
    assert lines == [
        "Checkers configured by the project: available: ruff, pytest; "
        "not installed: mypy, black (do not install; skip or note it)"
    ]


def test_preflight_all_or_none_checkers_available(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\n[tool.pytest.ini_options]\n",
        encoding="utf-8",
    )
    assert environment_preflight(tmp_path) == [
        "Checkers configured by the project: not installed: ruff, pytest "
        "(do not install; skip or note it)"
    ]

    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "ruff").write_text("", encoding="utf-8")
    (tmp_path / ".venv" / "bin" / "pytest").write_text("", encoding="utf-8")
    assert environment_preflight(tmp_path) == [
        "Checkers configured by the project: available: ruff, pytest"
    ]


def test_preflight_setup_cfg_tox_flake8_nox_and_path(tmp_path, monkeypatch):
    path_bin = tmp_path / "fake_path"
    path_bin.mkdir()
    flake8_bin = path_bin / ("flake8.cmd" if platform.system() == "Windows" else "flake8")
    flake8_bin.write_text("", encoding="utf-8")
    if platform.system() != "Windows":
        flake8_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(path_bin))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)

    (tmp_path / "setup.cfg").write_text(
        "[metadata]\nname = demo\n\n"
        "[tool:pytest]\naddopts = -q\n\n"
        "[isort]\nprofile = black\n\n"
        "[mypy-demo.*]\nignore_missing_imports = True\n\n"
        "[pylint.master]\njobs = 1\n",
        encoding="utf-8",
    )
    (tmp_path / "tox.ini").write_text("[tox]\nenvlist = py312\n", encoding="utf-8")
    (tmp_path / ".flake8").write_text("[flake8]\nmax-line-length = 100\n", encoding="utf-8")
    (tmp_path / "noxfile.py").write_text("import nox\n", encoding="utf-8")
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "pytest").write_text("", encoding="utf-8")

    lines = environment_preflight(tmp_path)
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("Checkers configured by the project: available: pytest, flake8;")
    assert "not installed: isort, mypy, pylint, tox, nox (do not install; skip or note it)" in line
    assert len(f"- {line}") <= 200


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
    node_bin.mkdir(parents=True)
    (node_bin / "eslint").write_text("", encoding="utf-8")
    (node_bin / "tsc.cmd").write_text("", encoding="utf-8")
    (node_bin / "vitest").write_text("", encoding="utf-8")

    lines = environment_preflight(tmp_path)
    assert lines == [
        "Checkers configured by the project: available: tsc, eslint, vitest; "
        "not installed: prettier, jest (do not install; skip or note it)"
    ]


def test_preflight_missing_test_deps_monkeypatched_subprocess(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n\n"
        "[project.optional-dependencies]\n"
        "test = ['PyTest>=8', 'Pandas[excel]>=2.0', 'demo[dev]']\n"
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
    (tmp_path / "test-requirements.txt").write_text(
        "pandas>=2.0\nhttpx @ https://example.com/httpx.whl\n",
        encoding="utf-8",
    )
    venv_py = (
        tmp_path
        / ".venv"
        / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")
    )
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")

    calls: list[dict[str, object]] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        requested = json.loads(kwargs["input"].decode("utf-8"))
        installed = {"pytest", "ruff", "scipy", "httpx"}
        missing = [n for n in requested if n not in installed]
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(missing).encode("utf-8"), stderr=b""
        )

    monkeypatch.setattr(prompt_mod.subprocess, "run", fake_run)

    lines = environment_preflight(tmp_path)
    assert len(calls) == 1
    assert calls[0]["cmd"][0] == str(venv_py)
    assert calls[0]["kwargs"]["timeout"] <= 2.0
    requested = json.loads(calls[0]["kwargs"]["input"].decode("utf-8"))
    assert "sphinx" not in requested  # docs extra is not a test/dev group
    assert "demo" not in requested  # self-extra skipped
    assert "fake-other-os" not in requested  # inapplicable OS marker skipped
    assert "scikit-learn" in requested  # PEP 503 normalized + include-group resolved
    assert lines == [
        "Declared test dependencies not installed: pandas, scikit-learn, matplotlib, pillow "
        "(tests needing them will fail or skip; do not install unless the task asks)"
    ]


def test_preflight_missing_test_deps_with_fake_interpreter(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text(
        "[project.optional-dependencies]\ntest = ['pandas', 'matplotlib', 'pytest']\n",
        encoding="utf-8",
    )
    if platform.system() == "Windows":
        fake_py = tmp_path / ".venv" / "Scripts" / "python.cmd"
        fake_py.parent.mkdir(parents=True)
        fake_py.write_text('@echo ["pandas", "matplotlib"]\n', encoding="utf-8")
    else:
        fake_py = tmp_path / ".venv" / "bin" / "python"
        fake_py.parent.mkdir(parents=True)
        fake_py.write_text('#!/bin/sh\necho \'["pandas", "matplotlib"]\'\n', encoding="utf-8")
        fake_py.chmod(0o755)

    lines = environment_preflight(tmp_path)
    assert lines == [
        "Declared test dependencies not installed: pandas, matplotlib "
        "(tests needing them will fail or skip; do not install unless the task asks)"
    ]


def test_preflight_real_python_importlib_metadata_probe(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text(
        "[project.optional-dependencies]\n"
        "test = ['PyTest>=7', 'prompt-toolkit>=3', 'nonexistent_karox_dep_xyz>=1']\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(prompt_mod, "_project_python", lambda _ws: sys.executable)

    lines = environment_preflight(tmp_path)
    assert lines == [
        "Declared test dependencies not installed: nonexistent-karox-dep-xyz "
        "(tests needing them will fail or skip; do not install unless the task asks)"
    ]


def test_preflight_omits_deps_line_when_all_installed(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "requirements-dev.txt").write_text("pytest\nruff\n", encoding="utf-8")
    monkeypatch.setattr(prompt_mod, "_project_python", lambda _ws: sys.executable)
    monkeypatch.setattr(
        prompt_mod.subprocess,
        "run",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, stdout=b"[]\n", stderr=b""),
    )

    assert environment_preflight(tmp_path) == []


def test_preflight_timeout_and_errors_silently_omitted(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\n\n[project.optional-dependencies]\ntest = ['pandas']\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(prompt_mod, "_project_python", lambda _ws: sys.executable)

    def raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 2.0))

    monkeypatch.setattr(prompt_mod.subprocess, "run", raise_timeout)
    lines = environment_preflight(tmp_path)
    assert lines == [
        "Checkers configured by the project: not installed: ruff (do not install; skip or note it)"
    ]

    monkeypatch.setattr(
        prompt_mod.subprocess,
        "run",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 1, stdout=b"boom", stderr=b"err"),
    )
    assert len(environment_preflight(tmp_path)) == 1

    monkeypatch.setattr(
        prompt_mod.subprocess,
        "run",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, stdout=b"not-json", stderr=b""),
    )
    assert len(environment_preflight(tmp_path)) == 1


def test_preflight_survives_malformed_configs(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text("[tool.ruff\nbroken toml", encoding="utf-8")
    (tmp_path / "package.json").write_text("{broken json", encoding="utf-8")
    (tmp_path / "setup.cfg").write_text("no section header = 1\n", encoding="utf-8")
    assert environment_preflight(tmp_path) == []


def test_preflight_caps_at_8_items_and_200_chars_by_whole_items():
    many = [f"pkg{i}" for i in range(12)]
    line = _format_missing_deps_line(many)
    assert line is not None
    assert len(f"- {line}") <= 200
    assert "pkg0, pkg1, pkg2, pkg3, pkg4, pkg5, pkg6, pkg7, ..." in line
    assert "pkg8" not in line

    long_names = [f"very-long-package-name-{i:02d}" for i in range(8)]
    long_line = _format_missing_deps_line(long_names)
    assert long_line is not None
    assert len(f"- {long_line}") <= 200
    assert ", ... (" in long_line
    # Every listed item before ", ..." must be a complete package name
    body = long_line.split("Declared test dependencies not installed: ", 1)[1].split(" (", 1)[0]
    parts = [p.strip() for p in body.split(",")]
    assert parts[-1] == "..."
    assert all(p in long_names for p in parts[:-1])

    long_checkers_avail = [f"checker-a-{i:02d}" for i in range(8)]
    long_checkers_miss = [f"checker-m-{i:02d}" for i in range(8)]
    chk_line = _format_checkers_line(long_checkers_avail, long_checkers_miss)
    assert chk_line is not None
    assert len(f"- {chk_line}") <= 200
    assert ", ..." in chk_line


def test_preflight_stability_and_session_computes_once(tmp_path, monkeypatch):
    _clear_env_paths(monkeypatch)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\n[tool.mypy]\n\n[project.optional-dependencies]\ntest = ['pandas']\n",
        encoding="utf-8",
    )
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "ruff").write_text("", encoding="utf-8")
    monkeypatch.setattr(prompt_mod, "_project_python", lambda _ws: sys.executable)

    probe_calls = 0
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        nonlocal probe_calls
        if cmd and cmd[0] == sys.executable:
            probe_calls += 1
            return subprocess.CompletedProcess(cmd, 0, stdout=b'["pandas"]\n', stderr=b"")
        return real_run(cmd, **kw)

    monkeypatch.setattr(prompt_mod.subprocess, "run", fake_run)

    shell = detect_shell()
    first = environment_block(tmp_path, shell)
    second = environment_block(tmp_path, shell)
    assert first == second
    assert probe_calls == 2
    assert "- Checkers configured by the project: available: ruff; not installed: mypy" in first
    assert "- Declared test dependencies not installed: pandas (" in first

    probe_calls = 0
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
    assert probe_calls == 1
    assert len(provider.requests) == 2
    assert provider.requests[0].system == provider.requests[1].system
