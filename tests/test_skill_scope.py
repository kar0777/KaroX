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
    assert "version: 1.7.0" in text
    assert "read the task's requirements" in text
    assert "lathegeometry" in text
    assert "depthtest: false" in text
    assert "before you finish" in text

SVG_SKILL = SKILL.parents[1] / "svg-illustration" / "SKILL.md"
#: The public drawing prompt this recipe is measured on; none of its nouns may
#: become part of the recipe.
SVG_FORBIDDEN = ("pelican", "bicycle", "bike", "pedal", "wheel", "spoke", "beak")


def test_svg_skill_stays_general() -> None:
    lowered = SVG_SKILL.read_text(encoding="utf-8").lower()
    present = [word for word in SVG_FORBIDDEN if word in lowered]
    assert not present, f"benchmark-specific terms leaked into the SVG skill: {present}"


def test_svg_skill_is_selected_for_a_drawing_task(tmp_path: Path) -> None:
    from karox.skill_runtime import SkillSession
    from karox.skills import SkillCatalog

    catalog = SkillCatalog(tmp_path, global_directory=tmp_path / "global")
    session = SkillSession(catalog, "s1", scratch_root=tmp_path / "scratch")
    names = [
        candidate.metadata.name
        for candidate in session.automatic_candidates("Draw a lighthouse logo as logo.svg")
    ]
    assert names == ["svg-illustration"]
    text = SVG_SKILL.read_text(encoding="utf-8").lower()
    assert "visual.check" in text
    assert "well-formed xml" in text
