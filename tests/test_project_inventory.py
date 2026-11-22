import json

from karox6.core import prompt as prompt_mod
from karox6.core.prompt import (
    CHECKER_INVENTORY_MAX_CHARS,
    COMMON_CHECKERS,
    checker_inventory,
    environment_block,
    project_inventory,
)
from karox6.tools.shell import detect_shell


def test_inventory_lists_declared_project_facts(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    scripts = {"test": "vitest", "build": "tsc", "typecheck": "tsc --noEmit"}
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "go.mod").write_text("module example.com/thing\n\ngo 1.22\n")
    (tmp_path / "Makefile").write_text(
        ".PHONY: test\ntest:\n\tgo test ./...\nVAR := 1\nlint: test\n"
    )
    items = project_inventory(tmp_path)
    assert items == [
        "Python project files: pyproject.toml; pytest configured",
        "Project virtualenv: .venv/bin/python",
        "package.json (lockfile: pnpm); scripts: test, build, typecheck",
        "Go module: example.com/thing",
        "Makefile targets: test, lint",
    ]


def test_inventory_is_empty_for_a_bare_directory_and_survives_bad_json(tmp_path):
    assert project_inventory(tmp_path) == []
    (tmp_path / "package.json").write_text("{not json")
    assert project_inventory(tmp_path) == ["package.json"]


def test_inventory_caps_long_lists(tmp_path):
    scripts = {f"s{i}": "x" for i in range(20)}
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}))
    (line,) = project_inventory(tmp_path, max_items=5)
    assert line.endswith("s0, s1, s2, s3, s4 (+15 more)")


def test_inventory_bounds_long_names(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"x" * 100_000: "y"}}))
    (line,) = project_inventory(tmp_path)
    assert len(line) < 500


def test_checker_inventory_reports_present_and_absent_tools(tmp_path, monkeypatch):
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts" / "pytest.exe").write_text("")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "ruff").write_text("")
    on_path = {"go": "/usr/local/bin/go", "node": "/usr/bin/node"}
    monkeypatch.setattr(prompt_mod.shutil, "which", on_path.get)

    line = checker_inventory(tmp_path)
    assert "\n" not in line
    assert len(line) <= CHECKER_INVENTORY_MAX_CHARS
    available_part, missing_part = line.split("; not found on PATH/venv: ")
    assert available_part == "Checkers found: pytest (.venv), ruff (.venv), node, go"
    assert missing_part.endswith("(do not install checkers)")
    for absent in ("mypy", "pyright", "flake8", "npx", "tsc", "eslint", "cargo"):
        assert absent in missing_part
    # Stable across calls (prefix caching)
    assert checker_inventory(tmp_path) == line
    env = environment_block(tmp_path, detect_shell())
    assert f"- {line}" in env
    assert environment_block(tmp_path, detect_shell()) == env


def test_checker_inventory_handles_all_absent_and_all_present(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_mod.shutil, "which", lambda _name: None)
    none_line = checker_inventory(tmp_path)
    assert none_line.startswith("Checkers found: none; not found on PATH/venv: ")
    assert all(tool in none_line for tool in COMMON_CHECKERS)
    assert len(none_line) <= CHECKER_INVENTORY_MAX_CHARS

    monkeypatch.setattr(prompt_mod.shutil, "which", lambda name: f"/bin/{name}")
    all_line = checker_inventory(tmp_path)
    assert "not found" not in all_line
    assert all(tool in all_line for tool in COMMON_CHECKERS)
    assert len(all_line) <= CHECKER_INVENTORY_MAX_CHARS


def test_checker_inventory_finds_local_node_tools(tmp_path, monkeypatch):
    (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
    (tmp_path / "node_modules" / ".bin" / "tsc.cmd").write_text("")
    (tmp_path / "node_modules" / ".bin" / "eslint").write_text("")
    monkeypatch.setattr(prompt_mod.shutil, "which", lambda _name: None)
    line = checker_inventory(tmp_path)
    assert "tsc (node_modules)" in line
    assert "eslint (node_modules)" in line
    assert "tsc," not in line.split("not found")[1]
