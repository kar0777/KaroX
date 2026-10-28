"""The shipped 3D skill is a technique guide, not a benchmark recipe.

The aquarium comparison is allowed to have its own bench-side recipe. If any
of that scene's nouns or counts leaks back into the built-in skill, this test
fails before the package is built.
"""

from __future__ import annotations

from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "src" / "karox" / "builtin_skills" / "web-3d-scene" / "SKILL.md"
FORBIDDEN = (
    "aquarium",
    "clownfish",
    "fish",
    "bubble",
    "tank",
    "hood",
    "sand",
)


def test_web_3d_skill_stays_general() -> None:
    text = SKILL.read_text(encoding="utf-8")
    lowered = text.lower()
    present = [word for word in FORBIDDEN if word in lowered]
    assert not present, f"benchmark-specific terms leaked into the shipped skill: {present}"


def test_web_3d_skill_keeps_its_technique_and_scope() -> None:
    text = SKILL.read_text(encoding="utf-8").lower()
    assert "version: 1.6.0" in text
    assert "read the task's requirements" in text
    assert "lathegeometry" in text
    assert "depthtest: false" in text
    assert "before you finish" in text