"""Data-only custom orchestration recipe registry.

A recipe describes roles/dependencies/capabilities only. It cannot contain Python,
argv, URLs, credentials, or executable hooks. Execution remains in registered
KaroX worker adapters.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

from .intelligence_pool import CAPABILITIES, ROLE_KINDS
from .orchestration_recipes import RECIPES, OrchestrationRecipe, RecipeStep
from .orchestration_routing import TASK_CLASSES
from .paths import config_dir


_SCHEMA_VERSION = 1
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class RecipeRegistryError(RuntimeError):
    pass


def _recipe_from_dict(value: Mapping[str, Any]) -> OrchestrationRecipe:
    name = value.get("name")
    description = value.get("description")
    raw_steps = value.get("steps")
    if not isinstance(name, str) or _SAFE.fullmatch(name) is None:
        raise ValueError("recipe name must contain 1-64 safe characters")
    if name in RECIPES:
        raise ValueError("custom recipe cannot shadow a built-in recipe")
    if not isinstance(description, str) or not description.strip() or len(description) > 1000:
        raise ValueError("recipe description must contain 1-1000 characters")
    if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > 50:
        raise ValueError("recipe steps must be a non-empty array with at most 50 entries")
    steps: list[RecipeStep] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, Mapping):
            raise ValueError(f"recipe steps[{index}] must be an object")
        allowed = {
            "step_id",
            "role",
            "task_class",
            "required_capabilities",
            "depends_on",
            "independent_from",
            "optional",
        }
        unknown = set(raw).difference(allowed)
        if unknown:
            raise ValueError(f"recipe steps[{index}] contains unsupported fields: {sorted(unknown)}")
        step_id = raw.get("step_id")
        role = raw.get("role")
        task_class = raw.get("task_class")
        if not isinstance(step_id, str) or _SAFE.fullmatch(step_id) is None:
            raise ValueError(f"recipe steps[{index}].step_id is invalid")
        if role not in ROLE_KINDS:
            raise ValueError(f"recipe steps[{index}].role is unsupported")
        if task_class not in TASK_CLASSES:
            raise ValueError(f"recipe steps[{index}].task_class is unsupported")
        capabilities = raw.get("required_capabilities", [])
        depends = raw.get("depends_on", [])
        independent = raw.get("independent_from", [])
        optional = raw.get("optional", False)
        if not isinstance(capabilities, list) or not all(item in CAPABILITIES for item in capabilities):
            raise ValueError(f"recipe steps[{index}].required_capabilities is invalid")
        for label, rows in (("depends_on", depends), ("independent_from", independent)):
            if not isinstance(rows, list) or not all(isinstance(item, str) and _SAFE.fullmatch(item) for item in rows):
                raise ValueError(f"recipe steps[{index}].{label} is invalid")
        if not isinstance(optional, bool):
            raise ValueError(f"recipe steps[{index}].optional must be boolean")
        steps.append(
            RecipeStep(
                step_id=step_id,
                role=role,
                task_class=task_class,
                required_capabilities=tuple(dict.fromkeys(capabilities)),
                depends_on=tuple(dict.fromkeys(depends)),
                independent_from=tuple(dict.fromkeys(independent)),
                optional=optional,
            )
        )
    return OrchestrationRecipe(name=name, description=description.strip(), steps=tuple(steps))


class RecipeRegistry:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = (path or (config_dir() / "vnext" / "orchestration-recipes.json")).expanduser().resolve()

    def _load_custom(self) -> dict[str, OrchestrationRecipe]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecipeRegistryError(f"cannot read orchestration recipes: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            raise RecipeRegistryError("orchestration recipes have unsupported schema")
        raw = payload.get("recipes")
        if not isinstance(raw, list):
            raise RecipeRegistryError("orchestration recipes must be an array")
        try:
            recipes = [_recipe_from_dict(item) for item in raw if isinstance(item, Mapping)]
        except (TypeError, ValueError) as exc:
            raise RecipeRegistryError(f"orchestration recipes are invalid: {exc}") from exc
        return {item.name: item for item in recipes}

    def _save(self, recipes: Mapping[str, OrchestrationRecipe]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temp = Path(raw)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {
                        "schema_version": _SCHEMA_VERSION,
                        "recipes": [recipes[key].to_dict() for key in sorted(recipes)],
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def list(self) -> list[OrchestrationRecipe]:
        custom = self._load_custom()
        merged = dict(RECIPES)
        merged.update(custom)
        return [merged[key] for key in sorted(merged)]

    def get(self, name: str) -> OrchestrationRecipe:
        if name in RECIPES:
            return RECIPES[name]
        custom = self._load_custom()
        try:
            return custom[name]
        except KeyError as exc:
            raise RecipeRegistryError(f"orchestration recipe does not exist: {name}") from exc

    def put_dict(self, value: Mapping[str, Any]) -> OrchestrationRecipe:
        recipe = _recipe_from_dict(value)
        custom = self._load_custom()
        custom[recipe.name] = recipe
        self._save(custom)
        return recipe

    def put_file(self, path: Path) -> OrchestrationRecipe:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecipeRegistryError(f"cannot read recipe file: {exc}") from exc
        if not isinstance(value, Mapping):
            raise RecipeRegistryError("recipe file must contain one JSON object")
        return self.put_dict(value)

    def remove(self, name: str) -> OrchestrationRecipe:
        if name in RECIPES:
            raise RecipeRegistryError("built-in orchestration recipes cannot be removed")
        custom = self._load_custom()
        try:
            removed = custom.pop(name)
        except KeyError as exc:
            raise RecipeRegistryError(f"custom orchestration recipe does not exist: {name}") from exc
        self._save(custom)
        return removed


__all__ = ["RecipeRegistry", "RecipeRegistryError"]
