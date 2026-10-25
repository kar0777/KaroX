"""Durable Plan and Concept artifacts left behind by agent modes.

A Plan or Ideate run\'s deliverable is a document, not a diff. Storing it only
in the transcript makes recovery a replay problem, so the final answer of such
a run is written to disk under the runtime directory -- one file per run,
plain markdown, no TTL. The repository itself is deliberately not the home:
``CoreRuntime.safe_path`` blocks ``.karox`` inside a project, and an agent
that may not mutate production code must not need a write grant to leave its
plan behind.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import runtime_dir

# The sections a durable artifact is expected to cover. These are the product
# contract for the Plan and Ideate modes, not a style suggestion: a plan that
# names no rollback or acceptance criteria is not a plan Build can start from.
PLAN_SECTIONS: tuple[str, ...] = (
    "Goal",
    "Current state",
    "Architecture",
    "Approach",
    "Alternatives considered",
    "Files/modules affected",
    "Implementation stages",
    "Tests",
    "Risks",
    "Migration",
    "Rollback",
    "Acceptance criteria",
)

CONCEPT_SECTIONS: tuple[str, ...] = (
    "Title",
    "Problem",
    "Opportunity",
    "Evidence",
    "User value",
    "Possible approaches",
    "Trade-offs",
    "Recommended direction",
    "Risks",
    "Open questions",
    "Acceptance idea",
)

_KIND_SECTIONS: dict[str, tuple[str, ...]] = {
    "plan": PLAN_SECTIONS,
    "concept": CONCEPT_SECTIONS,
}

_SESSION_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _sections(kind: str) -> tuple[str, ...]:
    sections = _KIND_SECTIONS.get(kind)
    if sections is None:
        raise ValueError(f"unknown mode artifact kind: {kind!r}")
    return sections


def artifact_skeleton(kind: str) -> str:
    """The section skeleton an artifact of ``kind`` is expected to fill."""

    return "".join(f"## {name}\n\n" for name in _sections(kind))


def missing_sections(kind: str, content: str) -> tuple[str, ...]:
    """Which expected sections the document never mentions.

    The match is deliberately loose (case-insensitive substring): a model may
    write "Risks and mitigations" and mean the Risks section. This is a
    report, not a gate -- the artifact is kept either way, and the honest gap
    is recorded next to it instead of being papered over.
    """

    lowered = content.lower()
    return tuple(s for s in _sections(kind) if s.lower() not in lowered)


def mode_artifacts_dir(session_id: str, *, root: Path | None = None) -> Path:
    """Where one session\'s durable mode artifacts live."""

    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise ValueError("mode artifacts require a well-formed session id")
    base = (
        Path(root)
        if root is not None
        else runtime_dir() / "vnext" / "mode_artifacts"
    )
    return base / session_id


def save_mode_artifact(
    *,
    session_id: str,
    kind: str,
    task: str,
    content: str,
    root: Path | None = None,
) -> dict[str, Any]:
    """Write one durable artifact and return honest metadata about it."""

    sections = _sections(kind)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("a mode artifact requires non-empty content")
    directory = mode_artifacts_dir(session_id, root=root)
    directory.mkdir(parents=True, exist_ok=True)
    created = datetime.now(timezone.utc)
    stamp = created.strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"{kind}-{stamp}.md"
    counter = 1
    while path.exists():
        counter += 1
        path = directory / f"{kind}-{stamp}-{counter}.md"
    task_line = " ".join(task.split()) if isinstance(task, str) else ""
    header = (
        f"# {kind.capitalize()} artifact\n\n"
        f"- session: {session_id}\n"
        f"- created: {created.isoformat()}\n"
        f"- task: {task_line}\n\n"
    )
    document = header + content.rstrip() + "\n"
    path.write_text(document, encoding="utf-8", newline="\n")
    data = document.encode("utf-8")
    return {
        "kind": kind,
        "path": str(path),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "created_at": created.isoformat(),
        "missing_sections": list(missing_sections(kind, content)),
        "expected_sections": list(sections),
    }
