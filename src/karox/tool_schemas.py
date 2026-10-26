"""Extended Core tool catalog: names, descriptions and JSON schemas.

Split from :mod:`karox.core_tools` so the runtime's handler logic reads as
logic: this module is data and the small conditional that adds skill and
visual-check tools when the runtime was given a catalogue or a checker.
"""

from __future__ import annotations

from .core_types import ToolDefinition
from .models import Capability


def additional_tool_definitions(
    *,
    max_edit_batch: int,
    max_plan_items: int,
    max_plan_item_chars: int,
    include_skill_tools: bool,
    include_visual_check: bool,
) -> dict[str, ToolDefinition]:
    """The extended runtime's extra tool definitions, keyed by Core name."""

    additional = {
        "repo.edit_file": ToolDefinition(
            "repo.edit_file",
            "Change a repository file. Pass edits=[{old,new}] to make several "
            "exact replacements in one call (applied atomically, order does "
            "not matter), or old_string/new_string for one replacement.",
            Capability.REPO_WRITE,
            True,
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "expected_occurrences": {"type": "number"},
                    "edits": {
                        "type": "array",
                        "maxItems": max_edit_batch,
                        "description": (
                            "Several replacements in one call. Either every "
                            "one of them is applied or none is."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "old": {"type": "string"},
                                "new": {"type": "string"},
                                "occurrence": {"type": ["string", "integer"]},
                            },
                            "required": ["old", "new"],
                            "additionalProperties": False,
                        },
                    },
                    "expected_sha256": {
                        "type": "string",
                        "description": (
                            "sha256 of the file as it was read. The edit is "
                            "refused if the file changed since then."
                        ),
                    },
                    "allow_secret_literal": {
                        "type": "boolean",
                        "description": (
                            "Write text that looks like a credential on "
                            "purpose, such as a secret-scanner fixture or a "
                            "documentation example. Recorded in the result."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        "repo.read_lines": ToolDefinition(
            "repo.read_lines",
            "Read a bounded line range of a UTF-8 repository file.",
            Capability.REPO_READ,
            False,
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {
                        "type": "number",
                        "minimum": 1,
                        "description": (
                            "First line to return, 1-based. Defaults to 1; "
                            "0 is refused."
                        ),
                    },
                    "count": {
                        "type": "number",
                        "minimum": 1,
                        "maximum": 1_500,
                        "description": (
                            "How many lines to return. Defaults to and is "
                            "capped at 1500."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        "repo.inspect": ToolDefinition(
            "repo.inspect",
            "Inspect the repository for one goal. Returns likely change points, "
            "relevant tests, dependency hints, bounded excerpts and, for one "
            "small safe change point, edit_context (full file + SHA; edit that "
            "path directly). Treat excerpts as read evidence. Use focused first; "
            "escalate only when it did not locate the target, tests or dependency.",
            Capability.REPO_READ,
            False,
            {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "What you need to locate or understand.",
                    },
                    "depth": {
                        "type": "string",
                        "enum": ["focused", "standard", "deep"],
                        "description": (
                            "Inspection breadth. Start with focused; escalate only "
                            "when the focused result is insufficient."
                        ),
                    },
                },
                "required": ["goal"],
                "additionalProperties": False,
            },
        ),
        "plan.update": ToolDefinition(
            "plan.update",
            "Record your working plan for this task. Send the whole list each "
            "time, 3-10 short items, and mark items done as you finish them.",
            Capability.REPO_READ,
            # It writes no file, but it does write session state that later
            # turns and the UI read back, so it goes through the same lease,
            # idempotency and record-save path as a mutation.
            True,
            {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "maxItems": max_plan_items,
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {
                                    "type": "string",
                                    "maxLength": max_plan_item_chars,
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["todo", "doing", "done"],
                                },
                            },
                            "required": ["text", "status"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["items"],
                "additionalProperties": False,
            },
        ),
        "disk.scan": ToolDefinition(
            "disk.scan",
            (
                "Scan the selected drive/workspace for bounded cleanup candidates using "
                "metadata only (names, sizes, categories, age/impact labels). Never reads "
                "file contents and never deletes anything."
            ),
            Capability.DISK_READ,
            False,
            {
                "type": "object",
                "properties": {
                    "max_candidates": {"type": "number"},
                    "min_size_mb": {"type": "number"},
                    "scan_seconds": {"type": "number"},
                },
                "additionalProperties": False,
            },
        ),
        "disk.plan_cleanup": ToolDefinition(
            "disk.plan_cleanup",
            (
                "Freeze an exact cleanup plan for selected relative paths. This only "
                "prepares a short impact preview; it cannot delete. The KaroX UI must "
                "obtain explicit user confirmation and apply the frozen plan separately."
            ),
            Capability.DISK_READ,
            False,
            {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 32,
                    }
                },
                "required": ["paths"],
                "additionalProperties": False,
            },
        ),
        "repo.apply_patch": ToolDefinition(
            "repo.apply_patch",
            (
                "Apply one strict unified diff atomically. Prefer this for a "
                "multi-site repair when exact old/new JSON replacements would be "
                "large; the whole transaction succeeds or fails."
            ),
            Capability.REPO_WRITE,
            True,
            {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": (
                            "Unified diff with ---/+++ file headers and @@ hunks. "
                            "Keep hunks narrow and include exact current context."
                        ),
                    },
                    "expected_sha256": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "dry_run": {"type": "boolean"},
                    "allow_secret_literal": {"type": "boolean"},
                },
                "required": ["patch"],
                "additionalProperties": False,
            },
            external_schema=True,
        ),
        "repo.command": ToolDefinition(
            "repo.command",
            "Apply an atomic patch or batch through the hot-reload worker.",
            Capability.REPO_WRITE,
            True,
            {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["apply_patch", "batch"],
                        "description": (
                            "apply_patch applies one unified diff; batch atomically stages "
                            "write/delete/move/mkdir operations"
                        ),
                    },
                    "payload": {
                        "type": "object",
                        "description": (
                            "apply_patch: {patch, expected_sha256?, dry_run?}. "
                            "batch: {operations, dry_run?} with "
                            "op=write|delete|move|mkdir and "
                            "path/content/source/destination."
                        ),
                        "properties": {
                            "patch": {"type": "string"},
                            "expected_sha256": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
                            "dry_run": {"type": "boolean"},
                            "allow_secret_literal": {"type": "boolean"},
                            "operations": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "op": {
                                            "type": "string",
                                            "enum": ["write", "delete", "move", "mkdir"],
                                        },
                                        "path": {"type": "string"},
                                        "content": {"type": "string"},
                                        "source": {"type": "string"},
                                        "destination": {"type": "string"},
                                        "expected_sha256": {"type": "string"},
                                        "allow_secret_literal": {"type": "boolean"},
                                        "missing_ok": {"type": "boolean"},
                                        "overwrite": {"type": "boolean"},
                                    },
                                    "required": ["op"],
                                    # The worker owns action-specific validation and
                                    # returns more precise unsupported-field errors.
                                    "additionalProperties": True,
                                },
                            },
                        },
                        # Keep this schema descriptive for MCP clients; the
                        # authoritative action-specific validator lives in
                        # workspace_worker.validate_repo_command().
                        "additionalProperties": True,
                    },
                },
                "required": ["action", "payload"],
                "additionalProperties": False,
            },
            external_schema=True,
        ),
        "dev.command": ToolDefinition(
            "dev.command",
            "Run a short repository-scoped developer command synchronously without a shell. Available only with hosted Full developer access. For a long build, model or benchmark run, start a durable job instead so a tunnel or reconnect interruption cannot strand the call.",
            Capability.DEV_COMMAND,
            True,
            {
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Executable and arguments. Shell syntax is not supported.",
                    },
                    "timeout_seconds": {"type": "number"},
                    "request_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
                        "description": (
                            "Optional execution generation for durable hosted command.run. "
                            "Reuse it to poll one run; change it to force a new run of identical argv."
                        ),
                    },
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
            (Capability.PROCESS_RUN,),
            replayable=False,
        ),
        "tests.run": ToolDefinition(
            "tests.run",
            "Run a short synchronous test verification: structured pytest for "
            "Python, the package test script for Node/Vite. Start a durable job "
            "for a long suite.",
            Capability.CHECKS_RUN,
            True,
            {
                "type": "object",
                "properties": {
                    "suite": {
                        "type": "string",
                        "enum": ["focused", "full", "split"],
                        "description": (
                            "focused runs only the requested targets, full "
                            "runs the whole suite, split runs part of it."
                        ),
                    },
                    "targets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Paths or node ids to run (focused). When omitted, "
                            "KaroX derives the test files that match this "
                            "session's changed modules."
                        ),
                    },
                    "split": {
                        "type": "number",
                        "description": "Number of parts to divide the suite into.",
                    },
                    "part": {
                        "type": "number",
                        "description": "Which part to run, 0-based (split).",
                    },
                    "timeout_seconds": {"type": "number"},
                },
                "additionalProperties": False,
            },
            (Capability.PROCESS_RUN,),
            replayable=False,
        ),
        "runtime.status": ToolDefinition(
            "runtime.status",
            "Read hot-worker generation, source digest, and reload health.",
            Capability.REPO_READ,
            False,
            {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        "git.log": ToolDefinition(
            "git.log",
            "Read recent commit history.",
            Capability.GIT_READ,
            False,
            {
                "type": "object",
                "properties": {"limit": {"type": "number"}},
                "additionalProperties": False,
            },
        ),
        "web.fetch": ToolDefinition(
            "web.fetch",
            (
                "Read one source file from a public code source (unpkg, "
                "jsdelivr, cdnjs, esm.sh, raw.githubusercontent) over HTTPS. "
                "Reuse proven code instead of re-deriving it and keep a one-line "
                "credit. Pass path to save into the repository; without it the "
                "text returns in the answer, shortened. Read-only: nothing is "
                "uploaded and no credentials are sent."
            ),
            Capability.CODE_FETCH,
            True,
            {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "https URL on a public code source.",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Repository-relative file to save the source to. "
                            "Omit to read it into the answer instead."
                        ),
                    },
                    "max_chars": {
                        "type": "number",
                        "minimum": 1,
                        "maximum": 40_000,
                        "description": (
                            "In-answer character limit. Defaults to and is "
                            "capped at 40000; saved files are not shortened."
                        ),
                    },
                    "allow_secret_literal": {
                        "type": "boolean",
                        "description": (
                            "Save text that looks like a credential on purpose, "
                            "such as a fixture or a documented example key. "
                            "Recorded in the result and the evidence."
                        ),
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            (Capability.REPO_WRITE,),
            replayable=False,
        ),
    }
    if include_skill_tools:
        additional["skill.list"] = ToolDefinition(
            "skill.list",
            (
                "List every skill recipe available to this run with its "
                "description and when to use it. Read-only."
            ),
            Capability.REPO_READ,
            False,
            {"type": "object", "properties": {}, "additionalProperties": False},
        )
        additional["skill.load"] = ToolDefinition(
            "skill.load",
            (
                "Read one skill recipe by name: its instructions and the "
                "names and sizes of its reference files, which skill.read "
                "opens one at a time. Read-only."
            ),
            Capability.REPO_READ,
            False,
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        )
        additional["skill.read"] = ToolDefinition(
            "skill.read",
            "Read one reference file of a skill, named as skill_list lists it. Read-only.",
            Capability.REPO_READ,
            False,
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["name", "path"],
                "additionalProperties": False,
            },
        )
    if include_visual_check:
        additional["visual.check"] = ToolDefinition(
            "visual.check",
            (
                "Look at a page this run wrote, in a context of its own: a "
                "warm browser renders it twice, the frames and the console "
                "go to a checker, and only the verdict comes back. Use it "
                "before finishing a visual task. The answer carries verdict "
                "pass, needs_work, or unavailable (no browser, or the "
                "per-task budget is spent) and at most five concrete "
                "issues. It is a bounded number of calls per task."
            ),
            Capability.REPO_READ,
            False,
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Repository-relative page to look at, for "
                            "example index.html. It must already be "
                            "written."
                        ),
                    },
                    "goal": {
                        "type": "string",
                        "description": (
                            "What the page is supposed to show, when it is "
                            "not already obvious from the task."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            # A check describes a page that may have changed since, so
            # replaying an earlier answer would report a stale verdict as a
            # fresh one -- the same reason checks.run is not replayable.
            replayable=False,
        )

    return additional
