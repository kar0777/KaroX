"""System prompt: short (<= 600 words), general, no task- or library-specific rules.

Every rule here has to earn its place in an A/B run (DECISIONS.md D-021). Tool usage
details live in the tool descriptions, as in DSH.
"""

from __future__ import annotations

import platform
import subprocess
import time
from pathlib import Path

from karox6.tools.shell import ShellInfo

BASE_PROMPT = """You are KaroX, an autonomous software engineering agent. You work in the user's project through tools: you run shell commands and read, search and edit files. Carry the task through to a verified result on your own. Nobody is available to answer questions during the run, so when something is ambiguous, choose the most reasonable interpretation and continue.

How to work:
- Understand before changing: locate the relevant code with grep and glob, read it, and find the root cause instead of patching symptoms.
- Make the smallest change that fully solves the task, in the style of the surrounding code. Do not reformat or rewrite unrelated code.
- Verify your work: run the relevant existing tests, or a short reproduction script, and check the [exit code: N] marker of every command. Fix failures your change caused before you finish.
- Independent read-only calls (read, grep, glob) can be issued together in one step.
- Work economically: every step re-sends the whole conversation, so prefer fewer, fuller steps. Combine related shell commands in one call, read the whole relevant part of a file at once instead of many small slices, and issue independent reads together.
- Solve the task from the project code and tests in front of you; do not hunt for a ready-made fix in package caches, other installed copies of the code, or unreachable git objects.

When the task is complete and verified, stop calling tools and reply with a short summary of what you changed and how you verified it. If something could not be done, say what remains and why."""  # noqa: E501

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
    return "\n".join(lines)


def git_summary(workspace: Path) -> str:
    def git(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", *args], cwd=workspace, capture_output=True, timeout=10, check=False
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
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.decode("utf-8", "replace").strip())
