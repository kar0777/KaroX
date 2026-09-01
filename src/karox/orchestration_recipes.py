"""Built-in orchestration recipes for common KaroX coding workflows."""

from __future__ import annotations

import dataclasses
from typing import Any

from .intelligence_pool import CAP_BROWSER, CAP_CODE, CAP_REASONING, CAP_TOOLS, CAP_VISION
from .orchestration_routing import (
    TASK_ARCHITECTURE,
    TASK_DISCOVERY,
    TASK_IMPLEMENTATION,
    TASK_REVIEW,
    TASK_SECURITY,
    TASK_TESTING,
    TASK_UI,
)


@dataclasses.dataclass(frozen=True)
class RecipeStep:
    step_id: str
    role: str
    task_class: str
    required_capabilities: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    independent_from: tuple[str, ...] = ()
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class OrchestrationRecipe:
    name: str
    description: str
    steps: tuple[RecipeStep, ...]

    def __post_init__(self) -> None:
        ids = [item.step_id for item in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("recipe step ids must be unique")
        known = set(ids)
        for step in self.steps:
            missing = set(step.depends_on).difference(known)
            if missing:
                raise ValueError(f"recipe step {step.step_id} depends on unknown steps: {sorted(missing)}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "steps": [item.to_dict() for item in self.steps],
        }


FEATURE = OrchestrationRecipe(
    name="feature",
    description="Plan, implement, verify and independently review a feature.",
    steps=(
        RecipeStep("scout", "scout", TASK_DISCOVERY, (CAP_REASONING, CAP_TOOLS)),
        RecipeStep("plan", "planner", TASK_ARCHITECTURE, (CAP_REASONING,), depends_on=("scout",)),
        RecipeStep("implement", "implementer", TASK_IMPLEMENTATION, (CAP_CODE, CAP_TOOLS), depends_on=("plan",)),
        RecipeStep("test", "tester", TASK_TESTING, (CAP_TOOLS,), depends_on=("implement",)),
        RecipeStep(
            "review",
            "reviewer",
            TASK_REVIEW,
            (CAP_REASONING, CAP_CODE),
            depends_on=("implement", "test"),
            independent_from=("implement",),
        ),
        RecipeStep(
            "ui",
            "ui",
            TASK_UI,
            (CAP_VISION, CAP_BROWSER),
            depends_on=("implement",),
            optional=True,
        ),
    ),
)

BUG_FIX = OrchestrationRecipe(
    name="bug-fix",
    description="Investigate, fix, regress and independently review a bug.",
    steps=(
        RecipeStep("investigate", "scout", TASK_DISCOVERY, (CAP_REASONING, CAP_TOOLS)),
        RecipeStep("implement", "implementer", TASK_IMPLEMENTATION, (CAP_CODE, CAP_TOOLS), depends_on=("investigate",)),
        RecipeStep("regression", "tester", TASK_TESTING, (CAP_TOOLS,), depends_on=("implement",)),
        RecipeStep(
            "review",
            "reviewer",
            TASK_REVIEW,
            (CAP_REASONING, CAP_CODE),
            depends_on=("implement", "regression"),
            independent_from=("implement",),
        ),
    ),
)

SECURITY = OrchestrationRecipe(
    name="security",
    description="Security-oriented discovery, implementation and independent security review.",
    steps=(
        RecipeStep("scout", "scout", TASK_DISCOVERY, (CAP_REASONING, CAP_TOOLS)),
        RecipeStep("security-plan", "security", TASK_SECURITY, (CAP_REASONING,), depends_on=("scout",)),
        RecipeStep("implement", "implementer", TASK_IMPLEMENTATION, (CAP_CODE, CAP_TOOLS), depends_on=("security-plan",)),
        RecipeStep("test", "tester", TASK_TESTING, (CAP_TOOLS,), depends_on=("implement",)),
        RecipeStep(
            "security-review",
            "security",
            TASK_SECURITY,
            (CAP_REASONING, CAP_CODE),
            depends_on=("implement", "test"),
            independent_from=("implement",),
        ),
    ),
)

LARGE_REFACTOR = OrchestrationRecipe(
    name="large-refactor",
    description="Architecture first, parallel bounded implementation slices, integration and review.",
    steps=(
        RecipeStep("map", "scout", TASK_DISCOVERY, (CAP_REASONING, CAP_TOOLS)),
        RecipeStep("architecture", "planner", TASK_ARCHITECTURE, (CAP_REASONING,), depends_on=("map",)),
        RecipeStep("impl-a", "implementer", TASK_IMPLEMENTATION, (CAP_CODE, CAP_TOOLS), depends_on=("architecture",)),
        RecipeStep("impl-b", "implementer", TASK_IMPLEMENTATION, (CAP_CODE, CAP_TOOLS), depends_on=("architecture",)),
        RecipeStep("impl-c", "implementer", TASK_IMPLEMENTATION, (CAP_CODE, CAP_TOOLS), depends_on=("architecture",)),
        RecipeStep("integration", "tester", TASK_TESTING, (CAP_TOOLS,), depends_on=("impl-a", "impl-b", "impl-c")),
        RecipeStep(
            "review",
            "reviewer",
            TASK_REVIEW,
            (CAP_REASONING, CAP_CODE),
            depends_on=("integration",),
            independent_from=("impl-a", "impl-b", "impl-c"),
        ),
    ),
)

RECIPES = {item.name: item for item in (FEATURE, BUG_FIX, SECURITY, LARGE_REFACTOR)}


def recipe(name: str) -> OrchestrationRecipe:
    try:
        return RECIPES[name]
    except KeyError as exc:
        raise ValueError(f"unknown orchestration recipe: {name}") from exc


__all__ = [
    "BUG_FIX",
    "FEATURE",
    "LARGE_REFACTOR",
    "OrchestrationRecipe",
    "RECIPES",
    "RecipeStep",
    "SECURITY",
    "recipe",
]
