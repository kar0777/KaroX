"""Independent review before the run finishes (opt-in with ``--review``, off until A/B).

When the agent gives its final answer after changing files, a child session with a fresh
context checks the change against the task: it sees the user's own messages and the diff,
not the agent's reasoning (which is where a misread requirement got rationalized). It
works like a tester, not a critic: it states each requirement, writes small checks outside
the project and runs them, and reports a problem only with the `finding` tool, quoting the
command it ran and what that command printed. The harness keeps only findings whose
command really ran in the review and whose quoted output really appeared, so the agent is
sent back to work only on reproduced problems, never on opinions. Files the review
touched in the project are put back afterwards.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from karox6.tools.base import ToolContext, ToolResult

MAX_DIFF_CHARS = 40_000
MAX_SAVED_BYTES = 4_000_000
REVIEW_MAX_STEPS = 25
REVIEW_MAX_SECONDS = 300.0
# Reminders by step: rev1 reviewers found real failures but ran out of steps before
# reporting any of them.
REVIEW_STEP_NOTICES = {
    14: (
        "You have used 14 of 25 steps. Report every problem you have already shown with "
        "`finding` now; then check at most one or two more requirements."
    ),
    21: "Stop checking now: report what you have shown with `finding`, then reply.",
}

REVIEW_APPEND = (
    "You are an independent reviewer. Another agent changed this project for the task "
    "below and says it is done. You did not see its reasoning; judge only the code. Work "
    "like a tester:\n"
    "1. List every requirement the task states, especially exact ones: names, types, "
    "formats, return values, edge cases it names, and anything it says must NOT happen.\n"
    "2. Read the change and the code around it.\n"
    "3. For each requirement the change may not meet, write a small check (a script or a "
    "test) in a scratch directory OUTSIDE the project, e.g. under /tmp/karox-review/, and "
    "run it against the project code. Prefer inputs the task names and the boundaries "
    "around them. Never modify, create or delete files inside the project, and do not "
    "install anything. Look only at the project and your scratch directory: do not "
    "search the rest of the filesystem, read logs or shell history, or look for hidden "
    "tests; judge the code against the task text alone.\n"
    "4. For every requirement your check shows is NOT met, call the `finding` tool: the "
    "requirement as the task states it, the problem, the exact command you ran and the "
    "lines of its output that show the problem. Do not report style, missing comments, "
    "or suspicions you could not demonstrate with a command. Report each problem right "
    "after the check that shows it, before you check the next requirement: you have "
    "about 15 tool calls, so start with the requirements the change most likely misses "
    "(exact constraints, must-nots, edge cases the task names).\n"
    "When you are done, reply with one short line: how many requirements you checked and "
    "how many findings you reported."
)

NOTICE = (
    "An independent reviewer (fresh context; it did not see your reasoning) checked your "
    "change against the task and reproduced the problems below with commands it ran. For "
    "each: if the check is right, fix the code (and any test of yours that asserts the "
    "wrong behaviour); if the check itself is wrong, say why in one line. Then finish.\n\n"
)


@dataclass
class Finding:
    requirement: str
    problem: str
    command: str
    observed: str


@dataclass
class FindingTool:
    """The reviewer's report channel: one call per reproduced problem."""

    findings: list[Finding] = field(default_factory=list)
    name: str = "finding"
    description: str = (
        "Report one requirement of the task that the change does not meet, with the evidence: "
        "the exact command you ran (as you passed it to the shell tool) and the part of its "
        "output that shows the problem. Only for problems you reproduced."
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "requirement": {
                    "type": "string",
                    "description": "The requirement, quoted from the task.",
                },
                "problem": {"type": "string", "description": "What the code does instead."},
                "command": {
                    "type": "string",
                    "description": "The exact shell command you ran that shows it.",
                },
                "observed": {
                    "type": "string",
                    "description": "Lines copied from that command's output.",
                },
            },
            "required": ["requirement", "problem", "command", "observed"],
        }
    )
    read_only: bool = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        item = Finding(
            requirement=str(args.get("requirement") or "").strip(),
            problem=str(args.get("problem") or "").strip(),
            command=str(args.get("command") or "").strip(),
            observed=str(args.get("observed") or "").strip(),
        )
        if not (item.requirement and item.problem and item.command and item.observed):
            return ToolResult("Error: every field is required.", is_error=True)
        self.findings.append(item)
        return ToolResult("recorded")


def _norm(text: str) -> str:
    return " ".join(text.split())


def verified(findings: list[Finding], ran: list[tuple[str, str]]) -> list[Finding]:
    """Findings whose quoted output a command of the review really printed, that command
    being the one quoted (whitespace-insensitive). Reviewers quote one check out of a
    longer command line that ran several at once (rev2), so the quoted command counts as
    the one that ran when it is part of it, or shares most of its words."""
    out = []
    for item in findings:
        wanted = _norm(item.command)
        words = set(re.findall(r"\w+", wanted))
        outputs = [
            o
            for c, o in ran
            if wanted
            and (
                wanted in _norm(c)
                or (len(words) >= 3 and len(words & set(re.findall(r"\w+", c))) >= 0.8 * len(words))
            )
        ]
        if not outputs:
            continue
        lines = [_norm(line) for line in item.observed.splitlines() if len(line.strip()) >= 4]
        joined = [_norm(o) for o in outputs]
        if lines and any(line in o for line in lines for o in joined):
            out.append(item)
    return out


def notice(findings: list[Finding]) -> str:
    parts = []
    for i, item in enumerate(findings, 1):
        observed = item.observed if len(item.observed) <= 1500 else item.observed[:1500] + " ..."
        parts.append(
            f"{i}. Requirement: {item.requirement}\n   Problem: {item.problem}\n"
            f"   Check: {item.command}\n   Output:\n{observed}"
        )
    return NOTICE + "\n\n".join(parts)


def review_prompt(user_turns: list[str], diff: str, files: list[str]) -> str:
    task = "\n\n---\n\n".join(t.strip() for t in user_turns if t.strip())
    listed = "\n".join(f"- {f}" for f in files)
    body = diff if diff else "(no git diff available: read the changed files listed above)"
    if len(body) > MAX_DIFF_CHARS:
        body = body[:MAX_DIFF_CHARS] + "\n... (diff truncated: read the files for the rest)"
    return f"# Task\n\n{task}\n\n# Changed files\n\n{listed}\n\n# Change\n\n```diff\n{body}\n```"


def git_diff(workspace: Path, files: list[str]) -> str:
    """The change as a diff: tracked files against HEAD, new files in full."""
    try:
        tracked = subprocess.run(
            ["git", "diff", "HEAD", "--", *files],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if tracked.returncode != 0:
        return ""
    text = tracked.stdout.decode("utf-8", "replace")
    shown = set()
    for line in text.splitlines():
        if line.startswith("+++ b/"):
            shown.add(line[6:])
    for rel in files:
        path = workspace / rel
        if rel in shown or not path.is_file() or path.stat().st_size > 200_000:
            continue
        with contextlib.suppress(OSError, UnicodeDecodeError):
            added = path.read_text(encoding="utf-8")
            text += f"\n--- /dev/null\n+++ b/{rel}\n" + "".join(
                f"+{line}\n" for line in added.splitlines()
            )
    return text


@dataclass
class Guard:
    """Puts the project back as the agent left it after the review: the reviewer is told
    not to write there, this makes sure. Changed files are saved before the review; after
    it, a saved file whose content differs is restored, and a file the review created
    (seen by git, or among the saved names) is removed or checked out."""

    workspace: Path
    saved: dict[str, bytes | None] = field(default_factory=dict)
    before_dirty: set[str] | None = None

    def save(self, files: list[str], dirty: set[str] | None) -> None:
        self.before_dirty = dirty
        for rel in files:
            path = self.workspace / rel
            try:
                if path.is_file() and path.stat().st_size <= MAX_SAVED_BYTES:
                    self.saved[rel] = path.read_bytes()
                elif not path.exists():
                    self.saved[rel] = None
            except OSError:
                continue

    def restore(self, dirty_now: set[str] | None) -> list[str]:
        restored = []
        for rel, data in self.saved.items():
            path = self.workspace / rel
            with contextlib.suppress(OSError):
                if data is None:
                    if path.exists() and path.is_file():
                        path.unlink()
                        restored.append(rel)
                elif not path.is_file() or _digest(path.read_bytes()) != _digest(data):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    restored.append(rel)
        if self.before_dirty is not None and dirty_now is not None:
            for rel in sorted(dirty_now - self.before_dirty - set(self.saved)):
                path = self.workspace / rel
                if _tracked(self.workspace, rel):
                    with contextlib.suppress(OSError, subprocess.SubprocessError):
                        subprocess.run(
                            ["git", "checkout", "--", rel],
                            cwd=self.workspace,
                            stdin=subprocess.DEVNULL,
                            capture_output=True,
                            timeout=30,
                            check=False,
                        )
                        restored.append(rel)
                elif path.is_file():
                    with contextlib.suppress(OSError):
                        path.unlink()
                        restored.append(rel)
        return restored


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tracked(workspace: Path, rel: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", rel],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def scratch_dir() -> str:
    base = os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp"
    return os.path.join(base, "karox-review")
