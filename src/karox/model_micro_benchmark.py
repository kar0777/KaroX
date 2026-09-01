"""Small live frontend/Three.js corpus for provider-capacity constrained A/B runs.

The production benchmark in :mod:`karox.model_quality_benchmark` remains the
primary quality gate.  These cases exist only to obtain a live signal when a
free provider refuses long generations.  Tasks and checks are deterministic and
frozen before model output is inspected.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Iterable


FRONTEND_MICRO_TASK = """
Return ONLY one complete standalone index.html using HTML, CSS and vanilla JS.
Build a responsive Northstar pricing/analytics strip with:
- semantic header and main;
- exactly three plan cards marked data-plan-card;
- a button marked data-theme-toggle that toggles document.documentElement.dataset.theme between light and dark;
- CSS custom properties, :focus-visible, and an @media (max-width: 768px) rule;
- no external assets or frameworks;
- set window.__benchmarkReady = true after wiring interactions.
Keep the implementation compact but polished and accessible.
""".strip()


THREE_D_MICRO_TASK = """
Return ONLY one complete standalone index.html. Use Three.js ES modules from
unpkg.com three@0.160.0. Create a compact animated open-top aquarium scene with:
- PerspectiveCamera, WebGLRenderer and an animation loop;
- exactly five glass objects named glass-front, glass-back, glass-left, glass-right, glass-bottom and no glass-top;
- a translucent object named water-volume;
- a separate object named water-surface whose position/rotation/material changes over time;
- exactly three procedural fish root groups named clownfish-1, clownfish-2, clownfish-3, each with at least a body mesh and tail mesh;
- resize handling;
- expose the THREE.Scene as window.__aquariumScene and then set window.__benchmarkReady = true.
No external assets except the Three.js module. Keep the code deliberately compact.
""".strip()


@dataclasses.dataclass(frozen=True)
class MicroCheck:
    check_id: str
    passed: bool
    weight: int = 1


@dataclasses.dataclass(frozen=True)
class MicroEvaluation:
    kind: str
    checks: tuple[MicroCheck, ...]

    @property
    def passed_weight(self) -> int:
        return sum(c.weight for c in self.checks if c.passed)

    @property
    def total_weight(self) -> int:
        return sum(c.weight for c in self.checks)

    @property
    def score(self) -> float:
        return self.passed_weight / self.total_weight if self.total_weight else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "score": round(self.score, 4),
            "passed_weight": self.passed_weight,
            "total_weight": self.total_weight,
            "checks": [dataclasses.asdict(c) for c in self.checks],
        }


def _has(pattern: str, source: str) -> bool:
    return re.search(pattern, source, re.IGNORECASE | re.DOTALL) is not None


def _count(pattern: str, source: str) -> int:
    return len(re.findall(pattern, source, re.IGNORECASE | re.DOTALL))


def _frontend(source: str) -> Iterable[MicroCheck]:
    yield MicroCheck("doctype", _has(r"<!doctype\s+html", source))
    yield MicroCheck("northstar", "northstar" in source.lower())
    yield MicroCheck("semantic", _has(r"<header\b", source) and _has(r"<main\b", source))
    yield MicroCheck("three_cards", _count(r"data-plan-card(?:\s|=|>)", source) == 3, 2)
    yield MicroCheck("theme_button", "data-theme-toggle" in source and "dataset.theme" in source, 2)
    yield MicroCheck("tokens", _count(r"--[a-zA-Z][\w-]*\s*:", source) >= 4)
    yield MicroCheck("responsive", _has(r"@media[^\{]*max-width\s*:\s*768px", source))
    yield MicroCheck("focus", ":focus-visible" in source)
    yield MicroCheck("ready", "__benchmarkReady" in source, 2)
    yield MicroCheck(
        "no_external",
        not _has(r"<(?:script|link|img)[^>]+(?:src|href)=[\"']https?://", source),
        2,
    )


def _three_d(source: str) -> Iterable[MicroCheck]:
    yield MicroCheck("doctype", _has(r"<!doctype\s+html", source))
    yield MicroCheck("three", "three@0.160.0" in source and "WebGLRenderer" in source, 2)
    yield MicroCheck("camera", "PerspectiveCamera" in source)
    panels = ("glass-front", "glass-back", "glass-left", "glass-right", "glass-bottom")
    yield MicroCheck("panels", all(p in source for p in panels) and "glass-top" not in source, 3)
    yield MicroCheck("water_volume", "water-volume" in source, 2)
    yield MicroCheck("water_surface", "water-surface" in source and _has(r"(?:sin|cos|rotation|position|material)", source), 2)
    fish = ("clownfish-1", "clownfish-2", "clownfish-3")
    yield MicroCheck("three_fish", all(f in source for f in fish), 3)
    yield MicroCheck("fish_meshes", _count(r"new\s+THREE\.Mesh", source) >= 6, 2)
    yield MicroCheck("animation", "requestAnimationFrame" in source, 2)
    yield MicroCheck("resize", "resize" in source.lower() and "setSize" in source)
    yield MicroCheck("scene_exposed", "__aquariumScene" in source, 2)
    yield MicroCheck("ready", "__benchmarkReady" in source, 2)
    urls = re.findall(r"https?://[^\"'\s<>]+", source)
    yield MicroCheck("no_external", all("unpkg.com/three@0.160.0" in url for url in urls), 2)


def task_prompt(kind: str) -> str:
    if kind == "frontend":
        return FRONTEND_MICRO_TASK
    if kind == "3d":
        return THREE_D_MICRO_TASK
    raise ValueError("micro benchmark kind must be frontend or 3d")


def evaluate(kind: str, source: str) -> MicroEvaluation:
    if kind == "frontend":
        checks = tuple(_frontend(source))
    elif kind == "3d":
        checks = tuple(_three_d(source))
    else:
        raise ValueError("micro benchmark kind must be frontend or 3d")
    return MicroEvaluation(kind, checks)


def karo_system_prompt(kind: str) -> str:
    focus = "accessible UI contract" if kind == "frontend" else "scene object contract"
    return (
        "You are a KaroX implementation worker operating under a strict output budget. "
        "Spend as little hidden reasoning as possible; prioritize emitting the finished artifact. "
        "Treat every bullet as an acceptance test, implement the smallest complete solution, "
        f"and verify the {focus} mentally before finalizing. Return only HTML."
    )


__all__ = [
    "FRONTEND_MICRO_TASK",
    "THREE_D_MICRO_TASK",
    "MicroCheck",
    "MicroEvaluation",
    "evaluate",
    "karo_system_prompt",
    "task_prompt",
]
