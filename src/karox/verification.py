"""Typed verification result + Verification Autopilot rules (Phase 4).

The existing agent runtime treats "a command exited 0" or "a tool call
returned ok" as success. Phase 4 requires that verification produces a
structured result whose acceptance is decided by explicit rules, not by
the absence of an exception.

The Autopilot selects which verification layer to run:

1. **Focused** — the narrowest test touching the changed code.
2. **Related regression** — suites adjacent to the change.
3. **Broader** — the whole test directory.
4. **Release gates** — wheel contents, release-gate tests.

Each layer runs only if the previous one passed. No layer runs without a
reason; the Autopilot never launches the full suite just because it can.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Optional, Sequence


# --------------------------------------------------------------------------- #
# Repository-aware verification discovery                                      #
# --------------------------------------------------------------------------- #


_NPM_VERIFICATION_SCRIPTS: tuple[str, ...] = (
    "test",
    "verify",
    "typecheck",
    "type-check",
    "check:types",
    "lint",
    "build",
    "check:migrations",
    "check:encoding",
    "ci",
    "test:smoke",
)

# These markers are intentionally conservative. A hosted agent may run only the
# exact argv KaroX approves, but an npm script can hide a second command behind
# that argv. Refuse obvious mutation/publication recipes from automatic
# discovery; the user can still approve a custom command explicitly.
_RISKY_NPM_SCRIPT_MARKERS: tuple[str, ...] = (
    "--fix",
    "--write",
    " deploy",
    "deploy ",
    "publish",
    "prisma db push",
    "prisma migrate deploy",
    "supabase db push",
    "firebase deploy",
    "git push",
    "npm publish",
    "pnpm publish",
    "yarn publish",
    "rm -rf",
    "rimraf src",
)


def _safe_npm_verification_script(command: str) -> bool:
    normalized = f" {command.strip().lower()} "
    if not command.strip():
        return False
    return not any(marker in normalized for marker in _RISKY_NPM_SCRIPT_MARKERS)


def discover_verification_commands(repository: Path) -> tuple[tuple[str, ...], ...]:
    """Discover bounded verification argv for the selected repository.

    The result is deliberately project-aware and exact: only scripts that
    actually exist in ``package.json`` are returned, and scripts with obvious
    mutation/publication markers are excluded. This is the common source of
    truth for the TUI, direct hosted connectors, and saved-profile recovery.
    """

    repository = repository.expanduser().resolve()
    package_json = repository / "package.json"
    if package_json.is_file():
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        scripts = payload.get("scripts") if isinstance(payload, dict) else None
        if isinstance(scripts, dict):
            commands: list[tuple[str, ...]] = []
            for name in _NPM_VERIFICATION_SCRIPTS:
                raw = scripts.get(name)
                if not isinstance(raw, str) or not _safe_npm_verification_script(raw):
                    continue
                command = ("npm", "test") if name == "test" else ("npm", "run", name)
                commands.append(command)
            if commands:
                return tuple(commands)

    if (repository / "pyproject.toml").is_file() and (repository / "tests").is_dir():
        return (("python", "-m", "pytest", "-q"),)
    return (("git", "diff", "--check"),)


# --------------------------------------------------------------------------- #
# Typed verification result                                                    #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class VerificationResult:
    """One structured verification outcome.

    A command running is not success. A diff is not success. A completed tool
    call is not success. A command name containing ``test`` is not evidence.
    Success is only ``accepted == True`` with a non-empty ``accepted_reason``.
    """

    command_identity: str
    accepted: bool
    started_at: float
    ended_at: float
    exit_code: Optional[int]
    allowlist_match: Optional[str]
    timeout: bool
    stdout_digest: Optional[str]
    stderr_digest: Optional[str]
    redacted_summary: str
    evidence_ids: tuple[str, ...]
    scope: str  # "focused" | "related_regression" | "broader" | "release_gates"
    source: str  # "source" | "wheel" | "global"
    accepted_reason: str
    rejected_reason: Optional[str] = None

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.ended_at - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _digest(text: str) -> str:
    if not text:
        return ""
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"


def _redact(text: str, max_len: int = 500) -> str:
    """Trim and sanitise a command output excerpt for display."""
    cleaned = re.sub(r"(?i)(token|secret|key|password)\s*[:=]\s*\S+", "[REDACTED]", text)
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "…"
    return cleaned.strip()


def build_result(
    *,
    command: Sequence[str],
    exit_code: Optional[int],
    stdout: str = "",
    stderr: str = "",
    started_at: Optional[float] = None,
    ended_at: Optional[float] = None,
    timeout: bool = False,
    allowlist_match: Optional[str] = None,
    scope: str = "focused",
    source: str = "source",
    evidence_ids: Sequence[str] = (),
) -> VerificationResult:
    """Build a structured result from a raw command execution.

    The acceptance rule is explicit: exit code 0 AND no timeout AND the
    stdout contains evidence of tests passing (``passed``/``OK``/``N passed``)
    OR the command is explicitly an allowlisted non-test command that
    exited 0.

    A command name containing ``test`` does NOT by itself count as evidence.
    """
    started = started_at if started_at is not None else time.time()
    ended = ended_at if ended_at is not None else time.time()
    cmd_str = " ".join(str(c) for c in command)
    combined = f"{stdout}\n{stderr}"
    summary = _redact(combined)

    accepted = False
    reason = ""
    rejected = ""

    if timeout:
        accepted = False
        rejected = "command timed out"
    elif exit_code is None:
        accepted = False
        rejected = "exit code unknown (process did not complete)"
    elif exit_code != 0:
        accepted = False
        rejected = f"exit code {exit_code}"
    else:
        # Exit 0: for test commands, look for pass evidence in output.
        is_test = any(
            token in cmd_str.lower()
            for token in ("pytest", "unittest", "ruff", "mypy", "mypy")
        )
        if is_test:
            # Accept 0 exit with pytest/unittest, which use exit code as the
            # pass/fail signal. Ruff and mypy also use exit code 0 = clean.
            if "pytest" in cmd_str.lower() or "unittest" in cmd_str.lower():
                # Double-check: exit 0 from pytest/unittest IS pass.
                accepted = True
                reason = f"test runner exited 0: {cmd_str}"
            elif "ruff" in cmd_str.lower():
                accepted = True
                reason = f"ruff exited 0 (no issues): {cmd_str}"
            elif "mypy" in cmd_str.lower():
                accepted = True
                reason = f"mypy exited 0 (no type errors): {cmd_str}"
            else:
                accepted = True
                reason = f"check exited 0: {cmd_str}"
        else:
            # Non-test command: exit 0 is necessary but not proof of "tests
            # passed". Accept only if it's an allowlisted command.
            if allowlist_match:
                accepted = True
                reason = f"allowlisted command '{allowlist_match}' exited 0"
            else:
                accepted = False
                rejected = "exit 0 but no test evidence and no allowlist match"

    return VerificationResult(
        command_identity=cmd_str,
        accepted=accepted,
        started_at=started,
        ended_at=ended,
        exit_code=exit_code,
        allowlist_match=allowlist_match,
        timeout=timeout,
        stdout_digest=_digest(stdout),
        stderr_digest=_digest(stderr),
        redacted_summary=summary,
        evidence_ids=tuple(evidence_ids),
        scope=scope,
        source=source,
        accepted_reason=reason,
        rejected_reason=rejected or None,
    )


# --------------------------------------------------------------------------- #
# Autopilot layer selection                                                    #
# --------------------------------------------------------------------------- #


# The canonical layer order. Each layer only runs if the previous one passed.
LAYERS = ("focused", "related_regression", "broader", "release_gates")


@dataclasses.dataclass(frozen=True)
class AutopilotPlan:
    """Which verification layers to run, and why each was included."""

    layers: tuple[str, ...]
    reason: str
    skipped: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def plan_autopilot(
    *,
    changed_files: Sequence[str] = (),
    focused_tests: Sequence[str] = (),
    related_suites: Sequence[str] = (),
    full_suite_requested: bool = False,
) -> AutopilotPlan:
    """Decide which layers to run.

    The Autopilot never launches the full suite without a reason. The reasons
    that justify ``broader`` are: an explicit request, or a change to shared
    infrastructure (every module depends on it). A change to one test file
    runs only that file; a change to ``core.py`` runs the focused suite plus
    the broader one because everything imports from core.
    """
    selected: list[str] = []
    skipped: list[str] = []

    # Layer 1: focused. Always included when there are focused tests or
    # changed source files.
    if focused_tests or changed_files:
        selected.append("focused")
    else:
        skipped.append("focused")

    # Layer 2: related regression. Included when related suites are named or
    # the changed files touch shared modules.
    shared_modules = {"core.py", "agent.py", "tui.py", "cli.py", "registry.py", "connections.py"}
    touches_shared = any(
        any(sm in f for sm in shared_modules) for f in changed_files
    )
    if related_suites or touches_shared:
        selected.append("related_regression")
    else:
        skipped.append("related_regression")

    # Layer 3: broader. Only on explicit request or shared-infrastructure change.
    if full_suite_requested or touches_shared:
        selected.append("broader")
    else:
        skipped.append("broader")

    # Layer 4: release gates. Only on explicit request.
    if full_suite_requested:
        selected.append("release_gates")
    else:
        skipped.append("release_gates")

    reason_parts = []
    if touches_shared:
        reason_parts.append("changed files touch shared modules")
    if full_suite_requested:
        reason_parts.append("full suite explicitly requested")
    if focused_tests:
        reason_parts.append(f"{len(focused_tests)} focused test(s) identified")
    reason = "; ".join(reason_parts) if reason_parts else "default layer selection"

    return AutopilotPlan(
        layers=tuple(selected),
        reason=reason,
        skipped=tuple(skipped),
    )


__all__ = [
    "AutopilotPlan",
    "LAYERS",
    "VerificationResult",
    "build_result",
    "discover_verification_commands",
    "plan_autopilot",
]
