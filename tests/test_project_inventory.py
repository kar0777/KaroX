import json

from karox6.core.prompt import project_inventory


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
