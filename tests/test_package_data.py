"""Runtime package data must be real, non-empty and still declared.

`docs/DEVELOPMENT.md` used to require `prompts/profiles/*.md`, but this release
line has no such source: bridge launch profiles are runtime JSON, not packaged
prompt files. These checks pin the resources that do exist and fail if a docs
edit starts requiring a directory the package does not ship, without making a
future real `profiles/` addition impossible.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "karox"

NONEMPTY_FILES = (
    "prompts/visual_checker.md",
    "tui/theme.tcss",
    "engine/LICENSE.md",
    "engine/NOTICE.md",
    "data/NOTICE.md",
    "data/models_dev.json.gz",
)


def test_required_package_data_is_present_and_nonempty() -> None:
    for relative in NONEMPTY_FILES:
        path = PACKAGE / relative
        assert path.is_file(), f"missing package data: {relative}"
        assert path.stat().st_size > 0, f"empty package data: {relative}"


def test_engine_typing_marker_is_present() -> None:
    # `py.typed` is a marker file and is legitimately empty.
    assert (PACKAGE / "engine" / "py.typed").is_file()


def test_every_builtin_skill_ships_a_nonempty_skill_md() -> None:
    skills = sorted((PACKAGE / "builtin_skills").glob("*/SKILL.md"))
    assert len(skills) == 8, [path.parent.name for path in skills]
    for path in skills:
        assert path.stat().st_size > 0, path


def test_pyproject_package_data_still_declares_the_runtime_resources() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for fragment in (
        '"karox.tui" = ["*.tcss"]',
        '"karox.prompts" = ["*.md"]',
        '"karox.builtin_skills" = ["*/SKILL.md", "*/*.md"]',
        '"karox.engine" = ["py.typed", "LICENSE.md", "NOTICE.md"]',
        '"karox.data" = ["*.json.gz", "NOTICE.md"]',
    ):
        assert fragment in text, fragment


def test_docs_do_not_require_a_missing_profiles_source() -> None:
    text = (ROOT / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")
    section = text[text.index("## Wheel contents") :]
    section = section[: section.find("\n## ", 1)]
    required = [
        line
        for line in section.splitlines()
        if line.lstrip().startswith("- ") and "prompts/profiles" in line
    ]
    if not required:
        return
    profiles = PACKAGE / "prompts" / "profiles"
    assert profiles.is_dir(), (
        "docs/DEVELOPMENT.md requires prompts/profiles; add the source or remove the requirement"
    )
    assert list(profiles.glob("*.md")), (
        "docs/DEVELOPMENT.md requires prompts/profiles/*.md but none exist"
    )
