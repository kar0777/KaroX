"""System prompt: short (<= 600 words), general, no task- or library-specific rules.

Every rule here has to earn its place in an A/B run (DECISIONS.md D-021). Tool usage
details live in the tool descriptions, as in DSH.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import time
from pathlib import Path

from karox6.tools.shell import ShellInfo

BASE_PROMPT = """You are KaroX, an autonomous software engineering agent. You work in the user's project through tools: you run shell commands and read, search and edit files. Nobody is available to answer questions during the run, so carry the task through to a verified result on your own. When something is ambiguous, choose the most reasonable interpretation consistent with the codebase and continue.

# Explore and understand
- Treat the task statement as an exact contract: required names, signatures, return shapes, accepted and rejected types, and what must be preserved or excluded. Do not broaden a contract because nearby code supports more.
- Read the relevant code, its callers, neighbouring tests and the build/test configuration to find the root cause, confirm the signatures and libraries you will use, and learn how this project runs its checks.
- When output must match something existing (a schema, another function's output, a format), inspect that source and compare against its real output, not a copy you wrote by hand.

# Make the change
- Make the smallest general change that fully solves the task, in the structure and style of the surrounding code. No unrelated refactors, speculative abstractions, planning files in the repository, or new dependencies unless required.
- Never make a check pass by deleting, skipping or weakening a test, suppressing a type error, loosening configuration, or rewriting a docstring to match faulty code. A failing test usually points at your code.

# Verify
- Start with the narrowest relevant check (a reproduction or the closest test), then run the project's build or type check and the broader related tests. Check the [exit code: N] marker of every command.
- Add a few focused tests for the new behaviour, including inputs that must be rejected; take expected values from the task or the real source of truth, not from your implementation. Give new test functions distinctive names so they cannot collide with other test suites, and delete temporary scripts before finishing.
- If the same fix fails three times, stop repeating it: read the full error, recheck the task's constraints, and change approach.

# Efficiency and safety
- Every step re-sends the whole conversation, so prefer fewer, fuller steps: issue independent read, grep and glob calls together, combine related shell commands, read whole relevant sections at once, and do not re-read a file just to confirm an edit that succeeded.
- Economy never justifies a partial solution: work through recoverable errors and timeouts, and keep going after the context is summarised.
- Solve the task from the project in front of you; do not hunt for a ready-made fix in package caches, other installed copies of the code, or unreachable git objects.
- Never discard existing work (git reset --hard, checkout of files you did not change, clean) and do not commit unless the task asks for it.

When the task is complete and verified, review your final diff for accidental changes, then stop calling tools and reply with a short summary of what you changed and how you verified it. If something could not be done or still fails, say so with the evidence."""  # noqa: E501

INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "KAROX.md")
INSTRUCTIONS_MAX_CHARS = 16_000


def build_system_prompt(
    workspace: Path, shell: ShellInfo, *, base: str | None = None, append: str | None = None
) -> str:
    parts = [base or BASE_PROMPT]
    if append and append.strip():
        parts.append(append.strip())
    parts.append(environment_block(workspace, shell))
    instructions = project_instructions(workspace)
    if instructions:
        parts.append(instructions)
    return "\n\n".join(parts)


def environment_block(workspace: Path, shell: ShellInfo) -> str:
    shell_name = {
        "git-bash": "bash (Git Bash on Windows)",
        "bash": "bash",
        "sh": "sh",
        "pwsh": "PowerShell 7 (pwsh)",
        "powershell": "Windows PowerShell",
    }.get(shell.flavor, shell.flavor)
    lines = [
        "Environment:",
        f"- Working directory: {workspace}",
        f"- Platform: {platform.system()} {platform.release()}; shell tool: {shell_name}",
        f"- Date: {time.strftime('%Y-%m-%d')}",
        f"- {git_summary(workspace)}",
    ]
    hint = python_hint()
    if hint:
        lines.append(f"- {hint}")
    return "\n".join(lines)


def python_hint() -> str | None:
    """On Windows `python3` is often the Microsoft Store placeholder: it prints only
    "Python" and exits with code 49, which agents misread and retry."""
    if platform.system() != "Windows":
        return None

    def kind(name: str) -> str:
        path = shutil.which(name)
        if not path:
            return "missing"
        if "\\windowsapps\\" not in path.lower().replace("/", "\\"):
            return "real"
        # WindowsApps holds both the placeholder and a real Store install: ask it.
        return "real" if _answers_version(path) else "stub"

    if kind("python3") != "stub":
        return None
    placeholder = "`python3` here is only the Microsoft Store placeholder (exit code 49)"
    for name in ("python", "py"):
        if kind(name) == "real":
            return f"Python: run it as `{name}`; {placeholder}"
    return f"Python: not installed; {placeholder}"


def _answers_version(path: str) -> bool:
    try:
        proc = subprocess.run(
            [path, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and b"Python 3" in proc.stdout + proc.stderr


def git_summary(workspace: Path) -> str:
    def git(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", "replace").strip()

    if git("rev-parse", "--is-inside-work-tree") != "true":
        return "Git: not a git repository"
    branch = git("branch", "--show-current") or "(detached HEAD)"
    status = git("status", "--porcelain=v1") or ""
    dirty = len([ln for ln in status.splitlines() if ln.strip()])
    state = f"{dirty} changed/untracked paths" if dirty else "clean working tree"
    return f"Git: repository on branch {branch}, {state}"


def project_instructions(workspace: Path) -> str:
    """AGENTS.md / CLAUDE.md / KAROX.md from the workspace (and its git root), bounded."""
    roots = [workspace]
    git_root = _git_root(workspace)
    if git_root is not None and git_root != workspace:
        roots.insert(0, git_root)
    seen: set[str] = set()
    chunks: list[str] = []
    budget = INSTRUCTIONS_MAX_CHARS
    for root in roots:
        for name in INSTRUCTION_FILES:
            path = root / name
            try:
                if not path.is_file() or path.stat().st_size > 1_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if not text or text in seen:
                continue
            seen.add(text)
            if len(text) > budget:
                text = text[:budget] + "\n[... truncated ...]"
            budget -= len(text)
            chunks.append(f'<instructions file="{path}">\n{text}\n</instructions>')
            if budget <= 0:
                break
    if not chunks:
        return ""
    return (
        "Project instructions from the repository (follow them unless they conflict with the "
        "task):\n" + "\n".join(chunks)
    )


def _git_root(workspace: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.decode("utf-8", "replace").strip())
