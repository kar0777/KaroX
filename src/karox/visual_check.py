"""A verdict about a rendered page, produced in a context that is not the run's.

Why this exists: showing the model its own frames helped (the run that looked at
its page produced banded clownfish where the run that only read a verdict did
not), but every frame also entered the main conversation and stayed there --
pictures are the most expensive tokens in a transcript, and they were re-sent on
every later step.

So the frames go to a context that does not accumulate: a page is observed in
the warm browser, the observations and two pictures go to a checker, and only
its verdict comes back. When no model can look -- the registry says the model
has no vision, or this machine cannot render -- the verdict comes from the
measurements the render check already takes, and says so instead of pretending.
"""

from __future__ import annotations

import json
import pathlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from .providers import (
    ImageAttachment,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderError,
)
from .web_check import observe_page, render_problems

#: How many times one task may ask for a look. The budget is per task, not per
#: step: a check that can be called on every edit becomes a habit, and the point
#: is a verdict before finishing, not a stream of them.
CHECKS_PER_TASK = 6
# Checker/model failures are not quality judgments. Permit a tiny bounded retry
# allowance without stealing slots from real PASS/NEEDS_WORK verdicts.
UNAVAILABLE_RETRY_ALLOWANCE = 2

#: What the verdict may cost in the main conversation.
VERDICT_BUDGET_TOKENS = 400
TOKEN_CHARS = 4
ISSUE_LIMIT = 5
ISSUE_WORDS = 20
CONSOLE_LIMIT = 3
CONSOLE_CHARS = 200

#: How many characters of the task the checker is shown. The checker judges
#: against explicit mandatory requirements, so a 2k prefix used to hide most of
#: an 8k brief: the aquarium run's fish, gravel, plants, lighting and camera
#: sections all sat beyond the cut. Twelve kilobytes covers a normal brief
#: whole while keeping a pathological one bounded.
GOAL_CHARS = 12_000

CHECKER_DEADLINE_SECONDS = 120.0
#: The checker's own output ceiling is generous on purpose, and it is not the
#: budget that matters: the verdict that enters the run's conversation is capped
#: separately. This one was 500 until a real page proved why that is wrong -- a
#: thinking model spends tokens before its JSON, the answer was cut off mid
#: sentence, and a page that was fine fell back to the measurements because the
#: verdict never arrived.
CHECKER_MAX_OUTPUT_TOKENS = 3500

#: Task shapes that ask for something a viewer has to look at. The tool is only
#: offered for these: a verdict about a rendered page is noise on a task about a
#: parser, and offering a tool that is never useful spends a model step.
_TASK_SIGNALS = (
    re.compile(r"\bhtml\b", re.IGNORECASE),
    re.compile(r"\bcanvas\b", re.IGNORECASE),
    re.compile(r"three(\.js)?\b", re.IGNORECASE),
    re.compile(r"\bwebgl\b", re.IGNORECASE),
    re.compile(r"\bscene\b", re.IGNORECASE),
    re.compile(r"\bpage\b", re.IGNORECASE),
    re.compile(r"\bscreen\b", re.IGNORECASE),
    re.compile(r"\brender(?:s|ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\banimat", re.IGNORECASE),
    re.compile(r"\bvisual", re.IGNORECASE),
    re.compile(r"\bdashboard\b", re.IGNORECASE),
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def is_visual_task(text: object) -> bool:
    """Whether a task asks for something that has to be looked at."""

    return isinstance(text, str) and any(
        signal.search(text) for signal in _TASK_SIGNALS
    )


def goal_brief(text: object, *, limit: int = GOAL_CHARS) -> str:
    """The goal as the checker sees it: bounded, and never the whole brief."""

    if not isinstance(text, str):
        return ""
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "\n…[the brief is longer; judge what the frames show]"


def parse_verdict(text: object) -> Optional[tuple[str, list[str]]]:
    """Read the checker's JSON out of its answer, or ``None`` when there is none.

    Tolerant about the wrapping and strict about the content: a model that
    answers inside a code fence or after a sentence is understood, but an answer
    with no verdict is refused rather than guessed, because a guessed verdict is
    the one failure mode this whole path exists to prevent.
    """

    if not isinstance(text, str) or not text.strip():
        return None
    for candidate in (text, *_JSON_OBJECT.findall(text)):
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        verdict = value.get("verdict")
        if verdict not in {"pass", "needs_work"}:
            continue
        raw_issues = value.get("issues")
        issues: list[str] = []
        if isinstance(raw_issues, list):
            for item in raw_issues:
                if isinstance(item, str) and item.strip():
                    issues.append(item.strip())
        if verdict == "needs_work" and not issues:
            # "needs_work" with nothing to fix is not a verdict the model can
            # act on, and treating it as a pass would hide a real objection.
            issues = ["the checker asked for more work without naming a problem"]
        return verdict, issues
    return None


def bound_verdict(payload: dict[str, Any]) -> dict[str, Any]:
    """Trim a verdict to what the main conversation may pay for.

    Issues are capped first, console lines second: a checker that reports more
    than five problems has stopped being specific, and the first ones are the
    ones it thought were worst.
    """

    result = dict(payload)
    raw_issues = result.get("issues")
    issues = [str(item)[:200] for item in raw_issues] if isinstance(raw_issues, list) else []
    result["issues"] = issues[:ISSUE_LIMIT]
    dropped = len(issues) - len(result["issues"])
    raw_console = result.get("console_errors")
    console = (
        [str(item)[:CONSOLE_CHARS] for item in raw_console]
        if isinstance(raw_console, list)
        else []
    )
    result["console_errors"] = console[:CONSOLE_LIMIT]
    while (
        len(json.dumps(result, ensure_ascii=False)) // TOKEN_CHARS > VERDICT_BUDGET_TOKENS
        and result["issues"]
    ):
        result["issues"] = result["issues"][:-1]
        dropped += 1
    if dropped:
        result["issues_truncated"] = dropped
    return result


@dataclass
class VisualCheckTally:
    """What the checker spent, kept apart from the run's own usage."""

    calls: int = 0
    vision_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    skipped: int = 0
    unavailable: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "vision_calls": self.vision_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "skipped": self.skipped,
            "unavailable": self.unavailable,
        }


@dataclass
class VisualChecker:
    """Observes a page and returns a verdict the main agent can act on.

    ``provider`` and ``model`` are the ones this run is already using: a check
    that silently switched model would be measuring something else. ``vision``
    comes from the registry and is never assumed -- with no vision the
    measurements decide, and the verdict says ``method: heuristics``.
    """

    provider: Provider
    model: str
    artifacts: pathlib.Path
    vision: bool = False
    goal: str = ""
    criteria: tuple[str, ...] = ()
    max_calls: int = CHECKS_PER_TASK
    settle_ms: int = 2000
    record_usage: Optional[Callable[[ModelResponse], None]] = None
    tally: VisualCheckTally = field(default_factory=VisualCheckTally)
    _calls: int = 0
    _quality_calls: int = 0
    # A resumed session builds a new checker, so the per-instance call counter
    # restarts at one. Without a token in the filename, the resumed run's first
    # frames overwrite the frames that earned the earlier verdict, and the
    # evidence for "what passed last time" is lost exactly when a regression
    # would need it.
    _run_token: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    #: What this checker told the model last time, per page. Without it the
    #: second look is as amnesiac as the first: the aquarium run was told "no
    #: bubbles" four times while the frames showed a few, and the model kept
    #: rewriting the same emitter. A returning reviewer can say "still missing"
    #: or "now visible, but sparse" instead.
    _previous_issues: dict[str, list[str]] = field(default_factory=dict)

    def remaining(self) -> int:
        return max(0, self.max_calls - self._quality_calls)

    def _quality_verdict(
        self, payload: dict[str, Any], *, page: pathlib.Path | None = None
    ) -> dict[str, Any]:
        """Consume one real visual-quality slot and return a bounded verdict."""

        self._quality_calls += 1
        result = dict(payload)
        result["checks_left"] = self.remaining()
        if page is not None:
            issues = result.get("issues")
            if isinstance(issues, list):
                self._previous_issues[str(page)] = [str(item) for item in issues][:ISSUE_LIMIT]
        return bound_verdict(result)

    def _unavailable_verdict(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record a checker/transport miss without spending a quality slot."""

        self.tally.unavailable += 1
        result = dict(payload)
        result["checks_left"] = self.remaining()
        return bound_verdict(result)

    def __call__(
        self,
        page: pathlib.Path,
        *,
        goal: str = "",
        criteria: Sequence[str] = (),
    ) -> dict[str, Any]:
        """One look at one page. Never raises: a broken check is not a verdict."""

        if (
            self._quality_calls >= self.max_calls
            or self._calls >= self.max_calls + UNAVAILABLE_RETRY_ALLOWANCE
        ):
            return {
                "verdict": "unavailable",
                "method": "limit",
                "issues": [
                    f"visual_check produced {self._quality_calls} usable verdicts "
                    f"across {self._calls} attempts; its bounded budget is exhausted"
                ],
                "checks_left": self.remaining(),
            }
        self._calls += 1
        self.tally.calls += 1
        # The full task owns the requirements; a caller's goal is a focus for
        # this look, not a replacement for the brief. The old `goal or self.goal`
        # let a terse "check the aquarium" hide the fish, gravel, lighting and
        # camera sections of an 8 KB prompt from the checker.
        base_goal = (self.goal or "").strip()
        focus = (goal or "").strip()
        effective_goal = base_goal or focus
        if base_goal and focus and focus not in base_goal:
            effective_goal = f"{base_goal}\n\nThis check's focus:\n{focus}"
        observation = observe_page(
            page, settle_ms=self.settle_ms, check_resize=True
        )
        if "frames" not in observation:
            self.tally.skipped += 1
            return self._unavailable_verdict(
                {
                    "verdict": "unavailable",
                    "method": "unavailable",
                    "issues": [
                        "the page could not be rendered here: "
                        + str(
                            observation.get("skipped")
                            or observation.get("error")
                            or "unknown reason"
                        )
                    ],
                }
            )
        frames = list(observation["frames"])
        measured = observation.get("measured") or {}
        probe = observation.get("probe") or {}
        judged = render_problems(
            page.name,
            page_errors=observation.get("page_errors") or (),
            console_errors=observation.get("console_errors") or (),
            measured=measured,
            probe=probe,
            resize=observation.get("resize"),
        )
        issues = list(judged.get("problems") or [])
        resize = judged.get("resize")
        if len(frames) > 1 and frames[0] == frames[1]:
            issues.append(
                f"{page.name}: the two frames taken {round(self.settle_ms / 1000, 1)}s "
                "apart are identical, so nothing in the page moved while it was watched"
            )
        saved = self._save_frames(page, frames)
        console = list(judged.get("console_errors") or [])
        page_errors = list(judged.get("page_errors") or [])
        if not self.vision:
            return self._quality_verdict(
                {
                    "verdict": "needs_work" if issues else "pass",
                    "method": "heuristics",
                    "issues": issues,
                    "console_errors": console,
                    "page_errors": page_errors,
                    "frames": saved,
                },
                page=page,
            )
        answer, finish_reason = self._ask(
            frames,
            page,
            goal=effective_goal,
            criteria=criteria or self.criteria,
            issues=issues,
            console=console,
            previous=self._previous_issues.get(str(page), ()),
        )
        if answer is None:
            if issues:
                return self._quality_verdict(
                    {
                        "verdict": "needs_work",
                        "method": "heuristics",
                        "issues": issues + ["the checker model gave no usable verdict"],
                        "console_errors": console,
                        "page_errors": page_errors,
                        "frames": saved,
                    },
                    page=page,
                )
            return self._unavailable_verdict(
                {
                    "verdict": "unavailable",
                    "method": "vision_unavailable",
                    "issues": ["the checker model gave no usable verdict"],
                    "console_errors": console,
                    "page_errors": page_errors,
                    "frames": saved,
                }
            )
        parsed = parse_verdict(answer)
        if parsed is None:
            # A cut-off answer is named as cut off: "answered without a verdict"
            # would send a reader looking for a model that ignores instructions
            # when the real cause was the ceiling.
            reason = (
                "the checker's answer was cut off at the output limit before it "
                "contained a verdict"
                if str(finish_reason or "").lower() == "length"
                else "the checker model answered without a verdict"
            )
            if issues:
                return self._quality_verdict(
                    {
                        "verdict": "needs_work",
                        "method": "heuristics",
                        "issues": issues + [reason],
                        "console_errors": console,
                        "page_errors": page_errors,
                        "frames": saved,
                    },
                    page=page,
                )
            return self._unavailable_verdict(
                {
                    "verdict": "unavailable",
                    "method": "vision_unavailable",
                    "issues": [reason],
                    "console_errors": console,
                    "page_errors": page_errors,
                    "frames": saved,
                }
            )
        verdict, reported = parsed
        # A model judgment can never clear an observed runtime exception or a
        # measured render problem: the model may simply not see it in a frame.
        if issues or observation.get("page_errors") or observation.get("console_errors"):
            verdict = "needs_work"
        # Measured problems go first. A model can report five variants of one
        # visible defect and push a broken resize — measured on every check —
        # out of the bounded verdict, and the aquarium run then never fixed it.
        merged = list(issues) + [item for item in reported if item not in issues]
        return self._quality_verdict(
            {
                "verdict": verdict,
                "method": "vision",
                "issues": merged,
                "console_errors": console,
                "page_errors": page_errors,
                "resize": resize,
                "frames": saved,
            },
            page=page,
        )

    def _save_frames(self, page: pathlib.Path, frames: Sequence[bytes]) -> list[str]:
        """Write the pictures beside the session, as artifacts of the run.

        The main conversation gets their names, not their bytes: the whole point
        of the checker is that frames stop being re-sent on every later step.
        """

        saved: list[str] = []
        directory = self.artifacts
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stem = f"{page.stem}-{self._run_token}-{self.tally.calls}"
            for index, payload in enumerate(frames, start=1):
                target = directory / f"{stem}-{index}.png"
                target.write_bytes(payload)
                saved.append(str(target))
        except OSError:
            # A frame that cannot be kept is a missing artifact, never a failed
            # check: the verdict was computed from the bytes either way.
            return saved
        return saved

    def _ask(
        self,
        frames: Sequence[bytes],
        page: pathlib.Path,
        *,
        goal: str,
        criteria: Sequence[str],
        issues: Sequence[str],
        console: Sequence[str],
        previous: Sequence[str] = (),
    ) -> tuple[Optional[str], Optional[str]]:
        """One checker call, without any of this run's history.

        Returns the answer and why it stopped: an answer that hit the model's
        output ceiling is a different fact from one that simply ignored the
        format, and the fallback text says which happened.
        """

        lines = [f"Page: {page.name}"]
        brief = goal_brief(goal or self.goal)
        if brief:
            lines.append(f"Goal:\n{brief}")
        if criteria:
            lines.append("Visual criteria:\n" + "\n".join(f"- {item}" for item in criteria))
        if issues:
            lines.append(
                "The render check measured:\n" + "\n".join(f"- {item}" for item in issues)
            )
        if console:
            lines.append("Console errors:\n" + "\n".join(f"- {item}" for item in console))
        if previous:
            lines.append(
                "Your previous review of this page reported:\n"
                + "\n".join(f"- {str(item)[:200]}" for item in previous[:ISSUE_LIMIT])
                + "\nFor each, say whether these frames now show it fixed. If it "
                "is still present, keep it as an issue; if it is fixed, do not "
                "repeat it."
            )
        lines.append(
            "Two frames follow: the first after the page settled, the second "
            f"about {round(self.settle_ms / 1000, 1)} seconds later."
        )
        request = ModelRequest(
            model=self.model,
            messages=(
                ModelMessage(role="system", content=_checker_prompt()),
                ModelMessage(
                    role="user",
                    content="\n\n".join(lines),
                    images=tuple(
                        ImageAttachment(mime="image/png", data=payload)
                        for payload in frames
                    ),
                ),
            ),
            max_output_tokens=CHECKER_MAX_OUTPUT_TOKENS,
            # This is a constrained visual classification, not another coding
            # turn. R10b spent thousands of tokens thinking before a tiny JSON
            # verdict and repeatedly hit the checker ceiling. Low reasoning keeps
            # the intelligence for image judgment while making JSON arrive early.
            reasoning_effort="low",
            deadline_seconds=CHECKER_DEADLINE_SECONDS,
        )
        try:
            response = self.provider.complete(request)
        except ProviderError:
            return None, None
        except Exception:
            return None, None
        self.tally.vision_calls += 1
        usage = response.usage if isinstance(response.usage, Mapping) else {}
        self.tally.input_tokens += _count(usage.get("prompt_tokens"))
        self.tally.output_tokens += _count(usage.get("completion_tokens"))
        if self.record_usage is not None:
            try:
                self.record_usage(response)
            except Exception:
                # Accounting must never be able to lose a verdict.
                pass
        return response.content, response.finish_reason


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _checker_prompt() -> str:
    """The checker's own prompt, read from the package like every other prompt."""

    from .prompts import visual_checker_text

    return visual_checker_text()
