"""Deterministic frontend + Three.js benchmark used for live model A/B runs.

The task contracts are frozen before a model is called.  Static checks measure
source-level requirements; browser checks are emitted as a self-contained JS
probe that can be executed by Playwright/KaroX Browser against the generated
page.  Visual quality is deliberately kept outside the numeric score.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Iterable


FRONTEND_TASK = r"""
Create a polished production-style analytics dashboard as ONE standalone index.html.
Do not use frameworks, build tools, external fonts, images, icons, or other assets.
Use only HTML, CSS and vanilla JavaScript in the file.

Required behavior and structure:
1. Responsive from 360px mobile to 1440px desktop with no horizontal overflow.
2. Semantic <header>, <nav>, <main>, at least three <section> elements and <footer>.
3. Header contains product name "Northstar", desktop navigation and a mobile menu button.
4. Mobile menu button has aria-expanded and actually toggles the mobile navigation.
5. Theme button toggles document.documentElement.dataset.theme between dark and light.
6. Hero/overview area with a clear h1 and short explanatory text.
7. Exactly four metric cards marked data-metric-card.
8. Activity/data table marked data-activity-table with header plus at least five body rows.
9. A CSS-only or SVG inline chart/visualization; no external image requests.
10. Visible primary CTA and secondary action, with keyboard focus styles.
11. Use CSS custom properties for the design tokens and at least one @media query at <=768px.
12. Respect prefers-reduced-motion and use sensible accessible labels.
13. No console errors on load or when toggling menu/theme.
14. Set window.__benchmarkReady = true after initialization.

Design direction: restrained premium SaaS product, strong typography/spacing hierarchy,
not a neon AI dashboard, no glassmorphism overload. Return ONLY the complete HTML.
""".strip()


THREE_D_TASK = r"""
Create a polished interactive procedural 3D aquarium as ONE standalone index.html.
Use Three.js ES modules from unpkg.com three@0.160.0 plus OrbitControls. Do not load
models, textures, images, fonts, audio, or any other external assets.

Required scene/runtime contract:
1. Full-viewport renderer, PerspectiveCamera, OrbitControls with damping, resize handling.
2. A rectangular aquarium with an OPEN TOP and exactly five glass panels named:
   glass-front, glass-back, glass-left, glass-right, glass-bottom. There must be no glass-top.
3. A visible translucent water volume named water-volume inside the tank.
4. A separate water surface mesh named water-surface whose geometry/material visibly animates.
5. Exactly three procedural clownfish root objects named clownfish-1, clownfish-2, clownfish-3.
   Each fish must be constructed from multiple meshes and include orange body, white bands,
   dark edging/fins, eyes and a tail; no external fish model.
6. Fish must swim continuously on different paths and show body/tail motion, not remain static.
7. Include procedural rocks/substrate, aquatic plants and rising bubbles for scene richness.
8. Lighting must include ambient/hemisphere plus directional/point lighting and convincing depth.
9. Keep the UI overlay minimal and readable; include an orbit/zoom hint.
10. Animation loop must update controls, water, fish/plants/bubbles and render every frame.
11. Expose the actual THREE.Scene as window.__aquariumScene and set
    window.__benchmarkReady = true only after the scene has been fully constructed.
12. No console errors, page errors, or failed asset requests other than a possible transient
    CDN source-map request. Return ONLY the complete HTML.
""".strip()


@dataclasses.dataclass(frozen=True)
class Check:
    check_id: str
    description: str
    passed: bool
    weight: int = 1


@dataclasses.dataclass(frozen=True)
class BenchmarkEvaluation:
    kind: str
    checks: tuple[Check, ...]

    @property
    def passed_weight(self) -> int:
        return sum(item.weight for item in self.checks if item.passed)

    @property
    def total_weight(self) -> int:
        return sum(item.weight for item in self.checks)

    @property
    def score(self) -> float:
        return 0.0 if not self.total_weight else self.passed_weight / self.total_weight

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "score": round(self.score, 4),
            "passed_weight": self.passed_weight,
            "total_weight": self.total_weight,
            "checks": [dataclasses.asdict(item) for item in self.checks],
        }


def _contains(pattern: str, text: str, flags: int = re.IGNORECASE | re.DOTALL) -> bool:
    return re.search(pattern, text, flags) is not None


def _count(pattern: str, text: str, flags: int = re.IGNORECASE | re.DOTALL) -> int:
    return len(re.findall(pattern, text, flags))


def _frontend_checks(text: str) -> Iterable[Check]:
    yield Check("doctype", "HTML document", _contains(r"<!doctype\s+html", text))
    yield Check("viewport", "responsive viewport", _contains(r"name=[\"']viewport[\"']", text))
    yield Check(
        "semantic",
        "header/nav/main/sections/footer",
        all(_contains(fr"<{tag}\b", text) for tag in ("header", "nav", "main", "footer"))
        and _count(r"<section\b", text) >= 3,
        2,
    )
    yield Check("northstar", "Northstar product identity", "northstar" in text.lower())
    yield Check(
        "metric_cards",
        "exactly four metric cards",
        _count(r"data-metric-card(?:\s|=|>)", text) == 4,
        2,
    )
    yield Check(
        "activity_table",
        "activity table with five body rows",
        _contains(r"data-activity-table", text)
        and _count(r"<tr\b", text) >= 6,
        2,
    )
    yield Check("design_tokens", "CSS custom properties", _count(r"--[a-zA-Z][\w-]*\s*:", text) >= 6)
    yield Check(
        "mobile_media",
        "mobile media query <=768px",
        _contains(r"@media[^\{]*(?:max-width\s*:\s*(?:[1-7]\d\d|768)px)", text),
        2,
    )
    yield Check("reduced_motion", "prefers reduced motion", "prefers-reduced-motion" in text)
    yield Check(
        "menu_aria",
        "mobile menu aria-expanded",
        _contains(r"aria-expanded=[\"'](?:false|true)[\"']", text),
    )
    yield Check(
        "menu_toggle",
        "interactive menu toggle",
        _contains(r"aria-expanded", text)
        and _contains(r"addEventListener\s*\(\s*[\"']click", text),
        2,
    )
    yield Check(
        "theme_toggle",
        "dataset theme toggling",
        _contains(r"dataset\.theme", text)
        and _contains(r"(?:dark|light)", text),
        2,
    )
    yield Check(
        "chart",
        "inline chart/visualization",
        _contains(r"<svg\b|<canvas\b|class=[\"'][^\"']*(?:chart|spark|graph)", text),
    )
    yield Check("focus", "visible focus styles", _contains(r":focus(?:-visible)?", text))
    yield Check("ready", "benchmark ready flag", "__benchmarkReady" in text)
    yield Check(
        "no_external_assets",
        "no external assets",
        not _contains(r"<(?:img|link|script)[^>]+(?:src|href)=[\"']https?://", text),
        2,
    )


def _three_d_checks(text: str) -> Iterable[Check]:
    yield Check("doctype", "HTML document", _contains(r"<!doctype\s+html", text))
    yield Check("three", "Three.js module", "three@0.160.0" in text and "OrbitControls" in text, 2)
    yield Check("renderer", "WebGLRenderer", "WebGLRenderer" in text)
    yield Check("camera", "PerspectiveCamera", "PerspectiveCamera" in text)
    yield Check("controls", "OrbitControls damping", "OrbitControls" in text and "enableDamping" in text)
    glass_names = ("glass-front", "glass-back", "glass-left", "glass-right", "glass-bottom")
    yield Check(
        "five_glass_panels",
        "five named glass panels",
        all(name in text for name in glass_names) and "glass-top" not in text,
        3,
    )
    yield Check("water_volume", "water volume", "water-volume" in text, 2)
    yield Check(
        "water_surface",
        "separate animated water surface",
        "water-surface" in text and _contains(r"(?:position|vertices|attribute|material|uniform|rotation)[^\n]{0,120}(?:sin|cos|time|elapsed)", text),
        3,
    )
    fish_names = ("clownfish-1", "clownfish-2", "clownfish-3")
    literal_fish = all(name in text for name in fish_names) and not _contains(
        r"clownfish-(?:0|4|5|6|7|8|9|1\d)", text
    )
    configured_fish = bool(
        _contains(r"clownfish-\$\{[^}]*id[^}]*\}", text)
        and _contains(r"\{\s*id\s*:\s*1\b", text)
        and _contains(r"\{\s*id\s*:\s*2\b", text)
        and _contains(r"\{\s*id\s*:\s*3\b", text)
        and _count(r"new\s+[A-Za-z_$][\w$]*\s*\(\s*[^\n,]*\[\s*[012]\s*\]", text) >= 3
    )
    yield Check(
        "three_fish",
        "exactly three named/configured clownfish roots",
        literal_fish or configured_fish,
        3,
    )
    yield Check(
        "fish_anatomy",
        "procedural clownfish anatomy",
        all(word in text.lower() for word in ("white", "tail", "eye", "fin"))
        and _count(r"new\s+THREE\.(?:Mesh|Group)", text) >= 8,
        2,
    )
    yield Check(
        "fish_motion",
        "continuous fish motion",
        _contains(r"(?:sin|cos)\s*\(", text)
        and _contains(r"(?:clownfish|fish)[\s\S]{0,800}(?:position|rotation)", text),
        2,
    )
    yield Check(
        "environment",
        "rocks/plants/bubbles",
        all(word in text.lower() for word in ("rock", "plant", "bubble")),
        2,
    )
    yield Check(
        "lighting",
        "multiple light classes",
        sum(name in text for name in ("AmbientLight", "HemisphereLight", "DirectionalLight", "PointLight", "SpotLight")) >= 2,
    )
    yield Check("resize", "resize handler", "resize" in text.lower() and "setSize" in text)
    yield Check("animation", "animation loop", _contains(r"requestAnimationFrame\s*\(", text), 2)
    yield Check("scene_exposed", "actual scene exposed", "__aquariumScene" in text)
    yield Check("ready", "benchmark ready flag", "__benchmarkReady" in text)
    # Three.js CDN and OrbitControls are the only permitted external assets.
    urls = re.findall(r"https?://[^\"'\s<>]+", text)
    allowed = all("unpkg.com/three@0.160.0" in url for url in urls)
    yield Check("no_external_assets", "no external assets except Three.js", allowed, 2)


def evaluate_source(kind: str, source: str) -> BenchmarkEvaluation:
    if not isinstance(source, str):
        raise TypeError("benchmark source must be text")
    if kind == "frontend":
        checks = tuple(_frontend_checks(source))
    elif kind == "3d":
        checks = tuple(_three_d_checks(source))
    else:
        raise ValueError("benchmark kind must be frontend or 3d")
    return BenchmarkEvaluation(kind, checks)


def evaluate_file(kind: str, path: Path) -> BenchmarkEvaluation:
    return evaluate_source(kind, path.read_text(encoding="utf-8"))


def failed_check_ids(evaluation: BenchmarkEvaluation) -> tuple[str, ...]:
    return tuple(item.check_id for item in evaluation.checks if not item.passed)


def task_prompt(kind: str) -> str:
    if kind == "frontend":
        return FRONTEND_TASK
    if kind == "3d":
        return THREE_D_TASK
    raise ValueError("benchmark kind must be frontend or 3d")


def karo_system_prompt(kind: str) -> str:
    focus = (
        "responsive semantics, interaction and runtime accessibility"
        if kind == "frontend"
        else "scene correctness, actual object construction and animation/runtime stability"
    )
    return (
        "You are operating as a KaroX implementation worker. Complexity stays inside the agent: "
        "return a finished artifact, not a tutorial. Treat every numbered requirement as an acceptance "
        "test. Before finalizing, mentally verify the generated file against each requirement, fix any "
        f"contradiction, and prefer deterministic code over decorative complexity. Focus on {focus}. "
        "Do not claim verification you did not perform. Return only the requested complete HTML."
    )


def repair_prompt(kind: str, source: str, failures: Iterable[str]) -> str:
    failure_list = ", ".join(failures)
    return (
        task_prompt(kind)
        + "\n\nA deterministic KaroX acceptance pass found these failing check IDs: "
        + failure_list
        + ". Repair ONLY as needed to satisfy them while preserving already-correct behavior. "
        + "Return the entire corrected HTML, not a diff.\n\nCURRENT HTML:\n"
        + source
    )


__all__ = [
    "BenchmarkEvaluation",
    "Check",
    "FRONTEND_TASK",
    "THREE_D_TASK",
    "evaluate_file",
    "evaluate_source",
    "failed_check_ids",
    "karo_system_prompt",
    "repair_prompt",
    "task_prompt",
]
