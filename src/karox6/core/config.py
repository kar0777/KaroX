"""Configuration shared by the agent loop and its entry points."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from karox6.core.messages import Price
from karox6.providers.base import RetryPolicy
from karox6.settings import default_home


@dataclass(slots=True)
class SessionConfig:
    model: str
    provider_name: str = "openai-chat"
    workspace: Path = field(default_factory=Path.cwd)
    home: Path = field(default_factory=default_home)
    max_steps: int | None = None
    max_seconds: float | None = None
    max_cost_usd: float | None = None
    max_output_tokens: int | None = None
    context_window: int | None = None  # what the served model accepts (None: unknown)
    context_limit: int = 400_000  # the user's working budget; the smaller of the two counts
    compact_at_tokens: int | None = None  # default: derived from the effective window
    temperature: float | None = None
    reasoning_effort: str | None = None
    price: Price | None = None
    apply_patch: bool = False
    replay_reasoning: str = "turn"  # all | turn | none
    loop_hard_stop: int = 20
    snapshot_on_finish: bool = True  # copy changed files to submissions/NN (grading)
    max_parallel: int = 10
    stream_deltas: bool = False
    extra_body: dict[str, Any] = field(default_factory=dict)
    system_prompt: str | None = None  # replaces the base prompt (environment is still added)
    system_append: str | None = None  # appended after the base prompt (A/B variants, users)
    subagents: bool = False  # offer the `agent` tool (M5, off until it wins an A/B)
    code_mode: bool = False  # offer `run_code` (M5, off until it wins an A/B)
    skills: bool = False  # list installed skills and offer the `skill` tool (CLI: on)
    hooks: bool = False  # run the user's and plugins' hooks (karox6.hooks; CLI: on)
    copy_notes: bool = False  # edit lists other copies of replaced code (off until A/B)
    command_wait_cap_s: int | None = None  # foreground wait cap per command (off until A/B)
    wrap_up_steps: int | None = None  # remind about long runs (off: batch B1 lost its A/B)
    deadline_notice_s: int | None = None  # warn this long before max_seconds (off until A/B)
    review: bool = False  # independent review before finishing (core/review.py; off until A/B)
    finish_gate: bool = False  # pre-finish self-check once per user task (opt-in)
    progress_guard: bool = True  # wrap-up notice after prolonged steps without progress
    step_notices: dict[int, str] = field(default_factory=dict)  # step -> notice (the review)
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    def effective_window(self, learned: int | None = None) -> int:
        """Tokens the conversation may use: the user's limit, capped by the model's window
        when it is known and by what a context-window error taught the session."""
        known = [v for v in (self.context_window, self.context_limit, learned) if v]
        return min(known) if known else 400_000

    def compaction_threshold(self, learned: int | None = None) -> int:
        if self.compact_at_tokens:
            return self.compact_at_tokens
        window = self.effective_window(learned)
        reserve = (self.max_output_tokens or 32_000) + 16_000
        # A small window (local models) still compacts below its size.
        return max(int(min(window * 0.8, window - reserve)), min(8_000, window // 2))
