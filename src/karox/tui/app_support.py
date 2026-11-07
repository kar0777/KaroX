"""TUI app constants, run state and small preference helpers."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from ..python_tests import python_test_command
from ..detached_process import windowless_flags
from . import prefs


EFFORT_LEVELS = ("auto", "low", "medium", "high", "extra-high", "ultra")

MODES = ("build", "plan", "ideate")

LANGUAGES = (("ru", "Русский"), ("en", "English"))

_REFUSED_WHILE_RUNNING = frozenset({"connect", "model", "sessions", "new", "mcp", "clear"})

@dataclass
class RunState:
    """Live facts about the current or latest run, for the status line.

    Every field is derived from a typed stream event or the final report --
    nothing here is inferred from human-readable text.
    """

    state: str = "idle"  # idle | running | stopping | done | failed | stopped
    step: int = 0
    phase: str = ""
    started_at: float = 0.0
    prompt_tokens: int = 0
    #: Tokens this run has spent so far, for the working line.
    tokens: int = 0
    context_fraction: float | None = None
    reads: int = 0
    edits: int = 0
    tests_ok: int = 0
    tests_failed: int = 0
    mcp_calls: int = 0
    skill_calls: int = 0
    inspections: int = 0
    files: list[str] = field(default_factory=list)

    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        return max(0.0, time.monotonic() - self.started_at)

def _display_path(path: Path) -> str:
    """The folder the way a shell prompt shows it: under ``~`` when it can."""

    try:
        home = Path.home().resolve()
        relative = path.relative_to(home)
    except (OSError, RuntimeError, ValueError):
        return str(path)
    return "~" if not relative.parts else str(Path("~") / relative)

def _cost_text(costs: dict[str, float]) -> str:
    def amount(value: float) -> str:
        return f"{value:.4f}" if value < 0.01 else f"{value:.2f}"

    return " · ".join(
        f"${amount(value)}" if currency.upper() == "USD" else f"{currency} {amount(value)}"
        for currency, value in sorted(costs.items())
    )

def _save_preference(**changes: object) -> None:
    try:
        prefs.save_preferences(**changes)
    except OSError:
        # A read-only config directory must not take a running chat down.
        pass

def _git_branch(repository: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.0,
            creationflags=windowless_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""

def _detect_verification(repository: Path) -> tuple[tuple[str, ...], ...]:
    """The same bounded discovery the runtime performs, without its web import."""

    commands: list[tuple[str, ...]] = []
    package = repository / "package.json"
    if package.is_file():
        try:
            payload = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        scripts = payload.get("scripts") if isinstance(payload, dict) else None
        if isinstance(scripts, dict):
            for name in ("test", "verify", "typecheck", "lint", "build"):
                raw = scripts.get(name)
                if not isinstance(raw, str):
                    continue
                if any(marker in raw for marker in ("--fix", "--write", " deploy", " publish")):
                    continue
                command = ("npm", "test") if name == "test" else ("npm", "run", name)
                if command not in commands:
                    commands.append(command)
    python_command = python_test_command(repository)
    if python_command is not None:
        commands.append(python_command)
    if (repository / "Cargo.toml").is_file():
        commands.append(("cargo", "test"))
    if (repository / "go.mod").is_file():
        commands.append(("go", "test", "./..."))
    return tuple(commands) or (("git", "diff", "--check"),)
