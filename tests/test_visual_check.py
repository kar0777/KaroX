"""The visual checker sees the whole checklist, not a 2 KB prefix.

The aquarium run called visual_check six times and still shipped a flat water
surface and a canvas that did not follow the viewport. The checker had been
shown the first 2000 characters of an 8 KB brief, so the fish, gravel, plants,
lighting and camera requirements were invisible; it also had no deterministic
resize probe. These tests pin both fixes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from karox import visual_check
from karox.providers import ModelResponse
from karox.visual_check import VisualChecker, goal_brief
from karox.web_check import render_problems

LATE_REQUIREMENT = "Exactly three clownfish, each with three white bands."
LONG_GOAL = ("Background and tank description. " * 200) + LATE_REQUIREMENT


def _measured() -> dict:
    return {
        "sampled": 100,
        "structured": 20,
        "background_like": 5,
        "tones": 10,
        "dominant_color": "#123456",
        "dominant_share": 0.2,
        "canvas_rect": {"x": 0, "y": 0, "width": 1280, "height": 800},
        "luma_mean": 100,
        "luma_std": 20,
        "dark_bottom_share": 0.1,
        "bottom_sampled": 10,
    }


def _probe() -> dict:
    return {
        "canvases": 1,
        "canvas_rect": {"x": 0, "y": 0, "width": 1280, "height": 800},
        "paints_with_three": True,
    }


def _observation(*, resize_ok: bool = False) -> dict:
    return {
        "frames": [b"frame-a", b"frame-b"],
        "measured": _measured(),
        "probe": _probe(),
        "page_errors": [],
        "console_errors": [],
        "resize": {
            "ok": resize_ok,
            "target": {"width": 1000, "height": 700},
            "after": {
                "canvas": {
                    "clientWidth": 1000 if resize_ok else 1280,
                    "clientHeight": 700 if resize_ok else 800,
                }
            },
        },
    }


class _FakeProvider:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.requests: list[object] = []

    def complete(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return ModelResponse(
            content=self.answer,
            tool_calls=(),
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )


def test_goal_brief_keeps_the_requirements_after_two_kilobytes() -> None:
    brief = goal_brief(LONG_GOAL)

    assert LATE_REQUIREMENT in brief
    assert len(LONG_GOAL) > 2000


def test_goal_brief_stays_bounded_for_a_pathological_brief() -> None:
    brief = goal_brief("x" * 13_000 + "TAIL")

    assert len(brief) < 12_100
    assert "TAIL" not in brief


def test_render_problems_names_a_canvas_that_ignores_resize() -> None:
    judged = render_problems(
        "aquarium.html",
        measured=_measured(),
        probe=_probe(),
        resize=_observation(resize_ok=False)["resize"],
    )

    assert any("viewport changed to 1000x700" in item for item in judged["problems"])
    assert judged["resize"]["ok"] is False


def test_visual_checker_uses_the_full_task_and_forces_resize_needs_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeProvider('{"verdict": "pass", "issues": []}')
    monkeypatch.setattr(
        visual_check, "observe_page", lambda *args, **kwargs: _observation()
    )
    checker = VisualChecker(
        provider=provider,  # type: ignore[arg-type]
        model="test-model",
        artifacts=tmp_path / "visual",
        vision=True,
        goal=LONG_GOAL,
    )

    result = checker(Path("aquarium-test.html"), goal="check the aquarium")

    assert result["verdict"] == "needs_work"
    assert any("viewport changed to 1000x700" in item for item in result["issues"])
    sent = provider.requests[0].messages[-1].content  # type: ignore[attr-defined]
    assert LATE_REQUIREMENT in sent
    assert "check the aquarium" in sent
    assert result["resize"]["ok"] is False
    assert result["checks_left"] == 5


def test_visual_checker_can_still_pass_a_clean_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeProvider('{"verdict": "pass", "issues": []}')
    monkeypatch.setattr(
        visual_check,
        "observe_page",
        lambda *args, **kwargs: _observation(resize_ok=True),
    )
    checker = VisualChecker(
        provider=provider,  # type: ignore[arg-type]
        model="test-model",
        artifacts=tmp_path / "visual",
        vision=True,
        goal=LONG_GOAL,
    )

    result = checker(Path("aquarium-test.html"))

    assert result["verdict"] == "pass"
    assert result["issues"] == []