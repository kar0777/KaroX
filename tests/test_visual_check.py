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


def test_dark_bottom_is_a_3d_scene_problem_not_a_2d_page_problem() -> None:
    """A 2D board with a dark band below it is not an unlit 3D floor.

    web-snake passed 17/17 while the checker reported the page's bottom quarter
    as "almost black" on every look; the visual budget ran out on a background
    band. The measurement only means a missing floor or broken lighting when the
    page actually renders a 3D scene.
    """

    measured = _measured()
    measured["dark_bottom_share"] = 0.84
    measured["bottom_sampled"] = 200

    two_d = render_problems(
        "snake.html",
        page_errors=(),
        console_errors=(),
        measured=measured,
        probe={**_probe(), "paints_with_three": False},
        resize={"ok": True},
    )
    assert not any("bottom quarter" in item for item in two_d["problems"])

    three_d = render_problems(
        "scene.html",
        page_errors=(),
        console_errors=(),
        measured=measured,
        probe={**_probe(), "paints_with_three": True},
        resize={"ok": True},
    )
    assert any("bottom quarter" in item for item in three_d["problems"])


def test_a_dark_floor_with_visible_lane_structure_is_not_a_missing_floor() -> None:
    """A neon game's floor is dark by design; lane lines make it readable.

    The 3D runner's ready frame was 74-83% dark in the bottom quarter while the
    grid, lane lines and rails were plainly visible. Brightness alone called
    that a missing floor and spent the visual budget on a design choice.
    """

    measured = _measured()
    measured["dark_bottom_share"] = 0.84
    measured["bottom_sampled"] = 200
    measured["bottom_structured_share"] = 0.12

    result = render_problems(
        "runner.html",
        page_errors=(),
        console_errors=(),
        measured=measured,
        probe=_probe(),
        resize={"ok": True},
    )

    assert not any("bottom quarter" in item for item in result["problems"])


def test_the_visual_checker_prompt_scopes_games_to_required_facts() -> None:
    from karox.prompts import visual_checker_text

    text = visual_checker_text()

    assert "For a game or an interactive scene" in text
    assert "every element the goal requires to be visible" in text
    assert "do not demand a particular silhouette" in text
    assert "are notes, not a failed scene" in text


def test_identical_frames_are_not_a_defect_for_a_static_chart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A static chart is allowed to hold still.

    The deterministic two-frame probe used to add "nothing moved" to every
    verdict. The vision model turned that into a mandatory pulsing animation,
    and the temperature chart spent six checks adding motion nobody asked for
    and stopped unverified although its grader passed.
    """

    observation = _observation(resize_ok=True)
    observation["frames"] = [b"same-frame", b"same-frame"]
    monkeypatch.setattr(visual_check, "observe_page", lambda *args, **kwargs: observation)
    checker = VisualChecker(
        provider=_FakeProvider('{"verdict": "pass", "issues": []}'),  # type: ignore[arg-type]
        model="test-model",
        artifacts=tmp_path / "visual",
        vision=True,
        goal="Global Temperature Anomalies — interactive SVG chart. Moving the pointer off a point hides the tooltip.",
    )

    result = checker(Path("chart.html"))

    assert result["verdict"] == "pass"
    assert not any("identical" in str(item) for item in result["issues"])


def test_identical_frames_do_not_create_a_motion_requirement_for_a_game(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A game at rest is not a broken animation.

    Snake sits in `ready` until the player presses a key. The deterministic
    two-frame diff used to say "nothing moved"; the checker then demanded a
    pulsing food dot the task never asked for, and the run spent all six looks
    on it.
    """

    observation = _observation(resize_ok=True)
    observation["frames"] = [b"same-frame", b"same-frame"]
    monkeypatch.setattr(visual_check, "observe_page", lambda *args, **kwargs: observation)
    checker = VisualChecker(
        provider=_FakeProvider('{"verdict": "pass", "issues": []}'),  # type: ignore[arg-type]
        model="test-model",
        artifacts=tmp_path / "visual",
        vision=True,
        goal="Canvas Snake game with a visible animation and a deterministic hook.",
    )

    result = checker(Path("snake.html"))

    assert result["verdict"] == "pass"
    assert not any("identical" in str(item) for item in result["issues"])


def test_a_resumed_checker_does_not_overwrite_earlier_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two checker instances (a resume) must keep their own evidence.

    The call counter restarts at one when a session resumes, so without a token
    the first frames of the resumed run replaced the frames the earlier verdict
    was made from -- exactly the evidence a regression would be judged against.
    """

    monkeypatch.setattr(
        visual_check,
        "observe_page",
        lambda *args, **kwargs: _observation(resize_ok=True),
    )

    def look() -> list[str]:
        checker = VisualChecker(
            provider=_FakeProvider('{"verdict": "pass", "issues": []}'),  # type: ignore[arg-type]
            model="test-model",
            artifacts=tmp_path / "visual",
            vision=True,
            goal=LONG_GOAL,
        )
        return list(checker(Path("aquarium-test.html"))["frames"])

    first = look()
    second = look()

    assert len(set(first) | set(second)) == 4
    assert all(Path(item).is_file() for item in first + second)

def test_measured_problems_keep_their_slots_in_a_bounded_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken resize must not be crowded out by five model observations.

    The aquarium run measured a broken resize on every check while the model
    reported five variants of a bubble problem; the bounded verdict kept the
    model's list and dropped the measured fact, so the resize stayed broken.
    """

    reported = [f"model observation {index}" for index in range(5)]
    provider = _FakeProvider(
        '{"verdict": "needs_work", "issues": ' + str(reported).replace("'", '"') + "}"
    )
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

    result = checker(Path("aquarium-test.html"))

    assert len(result["issues"]) == 5
    assert any("viewport changed to 1000x700" in item for item in result["issues"])
    assert result["issues"][0].startswith("aquarium-test.html")


class _SequenceProvider:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.requests: list[object] = []

    def complete(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else '{"verdict": "pass", "issues": []}'
        return ModelResponse(
            content=answer,
            tool_calls=(),
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )


def test_the_second_look_remembers_the_first_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A returning reviewer is not a stranger with a fresh memory.

    The aquarium run was told "no bubbles" on four consecutive checks while the
    frames showed a few; each look started from zero, so the model got the same
    sentence instead of "still missing" or "now visible but sparse".
    """

    provider = _SequenceProvider(
        [
            '{"verdict": "needs_work", "issues": ["the bubble stream is absent"]}',
            '{"verdict": "pass", "issues": []}',
        ]
    )
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

    checker(Path("aquarium-test.html"))
    checker(Path("aquarium-test.html"))

    second = provider.requests[1].messages[-1].content  # type: ignore[attr-defined]
    assert "the bubble stream is absent" in second
    assert "whether these frames now show it fixed" in second


def test_a_page_fixed_after_the_budget_gets_one_closing_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last check may judge the fix, not the version before it.

    The aquarium run used its sixth check on a page with a runtime error, fixed
    the error, and then got only `limit`: the artifact passed the external
    grader, but no verdict ever saw the fixed bytes. One closing look per page
    solves that without reopening the budget.
    """

    provider = _SequenceProvider(
        [
            '{"verdict": "needs_work", "issues": ["bubbles are absent"]}',
            '{"verdict": "pass", "issues": []}',
        ]
    )
    monkeypatch.setattr(
        visual_check,
        "observe_page",
        lambda *args, **kwargs: _observation(resize_ok=True),
    )
    page = tmp_path / "aquarium-test.html"
    page.write_text("<html>v1</html>", encoding="utf-8")
    checker = VisualChecker(
        provider=provider,  # type: ignore[arg-type]
        model="test-model",
        artifacts=tmp_path / "visual",
        vision=True,
        goal=LONG_GOAL,
        max_calls=1,
    )

    first = checker(page)
    unchanged = checker(page)
    page.write_text("<html>v2 fixed</html>", encoding="utf-8")
    closing = checker(page)
    page.write_text("<html>v3 another edit</html>", encoding="utf-8")
    exhausted = checker(page)

    assert first["verdict"] == "needs_work"
    assert unchanged["method"] == "limit"
    assert closing["verdict"] == "pass"
    assert closing["closing"] is True
    assert closing["checks_left"] == 0
    assert exhausted["method"] == "limit"
