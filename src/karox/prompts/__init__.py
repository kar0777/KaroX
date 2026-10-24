"""The v7 request contract, assembled from reviewed text files.

Why the text lives in ``.md`` files: the v6 contract is one Python string
literal, and a string literal is what scripted rewrites of this repository have
damaged before (doubled newlines, invisible in a diff of Python source). A
prompt is prose; a reviewer diffs it as prose.

What this module owns:

* the text of ``core.md`` and the five model profiles, read from the installed
  package so a wheel carries them;
* :func:`select_profile_names` -- which profiles a model's measured capability
  flags earn, never guessed from a model's name;
* :func:`build_system_prompt` -- one ordered assembly, byte-stable for a given
  ``(version, capabilities, environment, catalog)`` tuple so the provider can
  cache the prefix between steps.

What it deliberately does not own: the v6 contract. It stays in
``karox.agent`` as ``SYSTEM_PROMPT`` and is used unchanged, because a second
copy of the same words in a second file is exactly how two prompts drift apart.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Mapping, Optional

#: Every version a caller may ask for. ``v6`` is the default and its text is
#: owned by ``karox.agent``; ``v7`` is assembled by this module.
PROMPT_VERSIONS: tuple[str, ...] = ("v6", "v7")

#: Versions :func:`build_system_prompt` can build. ``v6`` is absent on purpose:
#: the caller keeps using its own text so exactly one copy of those words exists.
BUILDABLE_PROMPT_VERSIONS: tuple[str, ...] = ("v7",)

DEFAULT_PROMPT_VERSION = "v6"

CORE_FILE = "core.md"
VISUAL_CHECKER_FILE = "visual_checker.md"
PROFILES_DIRECTORY = "profiles"

#: The profile files, in the order the specification lists them. Section order
#: is part of the contract: a reordered prompt is a different prompt.
PROFILE_NAMES: tuple[str, ...] = (
    "parallel_tools",
    "single_tool",
    "reasoning",
    "small_context",
    "weak_follower",
)

#: A model below this window gets the small-context profile.
SMALL_CONTEXT_WINDOW = 64_000

#: Token budgets from the v7 specification, and the divisor it names for
#: counting them without a tokenizer. KaroX has its own estimate
#: (``ContextBudget.chars_per_token``, 3.0), which is deliberately more
#: pessimistic; the budgets here are the specification's and are checked with
#: its own divisor so a test measures what the specification asked about.
TOKEN_CHARS = 4
CORE_BUDGET_TOKENS = 1_100
PROFILE_BUDGET_TOKENS = 150
CATALOG_BUDGET_TOKENS = 1_500

#: Families whose thinking mode is part of the model's own name. This is the
#: fallback the specification allows when a registry has no measured flag; it
#: never overrides a measured value, and a miss costs one sentence of guidance
#: rather than a malformed request.
_THINKING_NAME_HINTS = (
    re.compile(r"thinking", re.IGNORECASE),
    re.compile(r"reasoning", re.IGNORECASE),
    re.compile(r"deepseek-r|(^|[-_/])r1($|[-_/])", re.IGNORECASE),
)


def estimated_tokens(text: str) -> int:
    """The specification's conservative token estimate for prompt text."""

    return len(text) // TOKEN_CHARS


def normalize_prompt_version(value: object) -> str:
    """The validated prompt version, or the default when nothing was asked for."""

    if value is None:
        return DEFAULT_PROMPT_VERSION
    if not isinstance(value, str) or not value.strip():
        raise ValueError("prompt version must be a non-empty string")
    normalized = value.strip().lower()
    if normalized not in PROMPT_VERSIONS:
        raise ValueError(
            f"prompt version must be one of {', '.join(PROMPT_VERSIONS)}; got {value!r}"
        )
    return normalized


def _read_text(relative_path: str) -> str:
    """Read one prompt file from the installed package.

    Trailing newlines are stripped: the assembly joins sections with its own
    blank line, so a file that ends with one newline and a file that ends with
    three must produce the same prompt.
    """

    resource = resources.files("karox.prompts").joinpath(relative_path)
    return resource.read_text(encoding="utf-8").rstrip("\n")


def core_text() -> str:
    """The model-independent part of the v7 contract."""

    return _read_text(CORE_FILE)


def profile_text(name: str) -> str:
    """One capability profile's text. The name is validated, never interpolated."""

    if name not in PROFILE_NAMES:
        raise ValueError(f"unknown prompt profile: {name!r}")
    return _read_text(f"{PROFILES_DIRECTORY}/{name}.md")


def visual_checker_text() -> str:
    """The separate prompt the visual checker answers with, without task history."""

    return _read_text(VISUAL_CHECKER_FILE)


def _flag(value: object) -> Optional[bool]:
    """A tri-state capability flag, where an unrecognised value is 'unknown'.

    Never raises: a registry written by a newer or older version must not stop a
    run, and 'unknown' is the honest reading of a value this version does not
    understand.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def _window(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


@dataclass(frozen=True)
class ModelCapabilities:
    """What the prompt may assume about the model it is written for.

    ``None`` means unmeasured. An unmeasured flag never invents a profile: the
    wrong profile costs a failed request, a missing one costs a sentence.
    """

    parallel_tool_calls: Optional[bool] = None
    reasoning: Optional[bool] = None
    weak_follower: bool = False
    context_window: Optional[int] = None
    vision: Optional[bool] = None

    @classmethod
    def from_contract(
        cls,
        contract: Optional[Mapping[str, Any]] = None,
        *,
        context_window: object = None,
        vision: object = None,
    ) -> "ModelCapabilities":
        """Build from a provider's capability map plus registry facts.

        ``contract`` is a provider's capability map (``parallel_tool_calls``,
        ``vision_images``, ...), the same one the preset table publishes.
        ``vision`` is what a model record states, and it wins when it states
        anything: a registry that says ``unknown`` is not an answer, so the
        provider's measured value is read instead of being shadowed by silence.
        """

        values = dict(contract or {})
        stated_vision = _flag(vision)
        return cls(
            parallel_tool_calls=_flag(values.get("parallel_tool_calls")),
            reasoning=_flag(values.get("reasoning")),
            weak_follower=_flag(values.get("weak_follower")) is True,
            context_window=_window(context_window),
            vision=(
                stated_vision
                if stated_vision is not None
                else _flag(values.get("vision_images"))
            ),
        )

    def with_name_fallback(self, model_id: str) -> "ModelCapabilities":
        """Fill only the unmeasured flags a model's own name makes obvious."""

        reasoning = self.reasoning
        if reasoning is None and isinstance(model_id, str):
            reasoning = any(hint.search(model_id) for hint in _THINKING_NAME_HINTS)
        return ModelCapabilities(
            parallel_tool_calls=self.parallel_tool_calls,
            reasoning=reasoning,
            weak_follower=self.weak_follower,
            context_window=self.context_window,
            vision=self.vision,
        )

    def profile_names(self) -> tuple[str, ...]:
        """The profiles these flags earn, in the specification's order."""

        names = [select_profile_names(self)]
        if self.reasoning:
            names.append("reasoning")
        if self.context_window is not None and self.context_window < SMALL_CONTEXT_WINDOW:
            names.append("small_context")
        if self.weak_follower:
            names.append("weak_follower")
        return tuple(names)

    def to_dict(self) -> dict[str, Any]:
        return {
            "parallel_tool_calls": self.parallel_tool_calls,
            "reasoning": self.reasoning,
            "weak_follower": self.weak_follower,
            "context_window": self.context_window,
            "vision": self.vision,
        }


def select_profile_names(capabilities: ModelCapabilities) -> str:
    """The tool-call profile: one call at a time only when that was measured.

    An unmeasured provider gets the batching profile, because the product's own
    constitution already asks for independent reads in one response; only a
    provider measured to break on several calls in one response is told
    otherwise.
    """

    return "single_tool" if capabilities.parallel_tool_calls is False else "parallel_tools"


@dataclass(frozen=True)
class BuiltPrompt:
    """One assembled prompt, with the evidence a run record needs."""

    version: str
    text: str
    sections: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ()
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def tokens(self) -> int:
        return estimated_tokens(self.text)

    def to_dict(self) -> dict[str, Any]:
        """What a session record stores about the prompt it ran with.

        The text itself is deliberately absent: a session file is not a prompt
        archive, and a prompt that changed can be identified by its digest.
        """

        return {
            "version": self.version,
            "sha256": self.sha256,
            "characters": len(self.text),
            "estimated_tokens": self.tokens,
            "sections": list(self.sections),
            "profiles": list(self.profiles),
            "capabilities": self.capabilities.to_dict(),
        }


def build_system_prompt(
    version: str,
    *,
    capabilities: Optional[ModelCapabilities] = None,
    environment: str = "",
    catalog: str = "",
) -> BuiltPrompt:
    """Assemble the prompt for one version, in the specification's fixed order.

    Order: ``core``, then the capability profiles, then the environment block,
    then the skills catalog. Project instructions (``AGENTS.md`` and similar)
    are appended by the caller's composition, which is why they are not a
    parameter here.

    ``v6`` raises: its text is owned by ``karox.agent``. Building a second copy
    here would be the drift this module's docstring warns about.
    """

    normalized = normalize_prompt_version(version)
    if normalized not in BUILDABLE_PROMPT_VERSIONS:
        raise ValueError(
            f"prompt version {normalized!r} is not built here; "
            "karox.agent owns the v6 contract and callers use it directly"
        )
    resolved = capabilities or ModelCapabilities()
    parts = [core_text()]
    sections = ["core"]
    profiles = resolved.profile_names()
    for name in profiles:
        parts.append(profile_text(name))
    if profiles:
        sections.append("profiles")
    environment_block = environment.strip("\n")
    if environment_block:
        parts.append(environment_block)
        sections.append("environment")
    catalog_block = catalog.strip("\n")
    if catalog_block:
        parts.append(catalog_block)
        sections.append("catalog")
    return BuiltPrompt(
        version=normalized,
        text="\n\n".join(parts) + "\n",
        sections=tuple(sections),
        profiles=tuple(profiles),
        capabilities=resolved,
    )


def prompt_budget_report() -> dict[str, dict[str, int]]:
    """The measured size of every prompt file against its budget.

    Exposed for the budget test and for diagnostics: a prompt that silently
    grows is a cost paid on every step of every run.
    """

    report: dict[str, dict[str, int]] = {}
    core = core_text()
    report[CORE_FILE] = {
        "characters": len(core),
        "tokens": estimated_tokens(core),
        "budget": CORE_BUDGET_TOKENS,
    }
    for name in PROFILE_NAMES:
        text = profile_text(name)
        report[f"{PROFILES_DIRECTORY}/{name}.md"] = {
            "characters": len(text),
            "tokens": estimated_tokens(text),
            "budget": PROFILE_BUDGET_TOKENS,
        }
    return report
