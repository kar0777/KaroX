"""Engine tool catalog: domain gating and provider-tool wire helpers.

The full Core tool catalog is advertised except for large, narrow-domain
schemas that are gated on task keywords. This module owns that policy and the
two provider-tool shape helpers; the kernel keeps the language-server probe
because tests patch it there.
"""

from __future__ import annotations

from typing import Dict
import os

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

import os
import re
from typing import Optional
from .engine_provider_bridge import (  # noqa: E402
    FINISH_TOOL_NAME,
)
from .core import ToolDefinition
from .providers import (  # noqa: E402
    ProviderTool,
)


#: Tool schemas whose payload is large and whose domain is narrow. They are
#: advertised only when the task names that domain. The tool itself, its
#: capability gate and its handler stay in place, so a model that already knows
#: the name can still call it; an unrelated task simply stops paying ~3.4 KB of
#: JSON on every request. Measured on the ledger fixture, the full catalog cost
#: about 1.1k prompt tokens per call of which none were ever called.
_DOMAIN_TOOL_KEYWORDS: Dict[str, tuple[str, ...]] = {
    # web.fetch is not gated: it is one ~1.2 KB schema, and a repair task names
    # no domain word while its fix is often a known upstream change. Hidden on
    # swe-sympy-24213 (2026-11-05), the model spent ~25 KB of reasoning
    # reconstructing upstream code from memory ("web_fetch isn't in the tool
    # list"), where a fetch would have read it.
    "disk.scan": (
        "disk",
        "cleanup",
        "clean up",
        "free space",
        "storage",
        "cache",
        "prune",
        "temporary files",
    ),
    "disk.plan_cleanup": (
        "disk",
        "cleanup",
        "clean up",
        "free space",
        "storage",
        "cache",
        "prune",
        "temporary files",
    ),
    # The Safety section of the system prompt teaches the exact
    # exact `repo_command {"action": "batch", ...}` form for deleting a scratch file,
    # so the name stays callable while it is not advertised. An argument error
    # on that rare call is cheaper than 1.3 KB of batch schema on every repair
    # request.
    "repo.command": (
        "batch",
        "atomic",
        "rename",
        "mkdir",
        "move file",
        "move the file",
        "delete file",
        "delete the file",
        "remove file",
        "remove the file",
    ),
}


# The default surface is the handful of tools real runs use. 66 benchmark
# trajectories (2026-11-13, 4658 calls): checks_run, repo_search,
# repo_read_lines, repo_edit_file, repo_read_file, finish, git_log, git_diff,
# repo_write_file and repo_list_files made 98.6% of all calls; skill_*,
# runtime_status, lsp_diagnostics, plan_update, repo_inspect, repo_apply_patch,
# tests_run, git_status, web_fetch and browser_command together made 1.4%.
# web_fetch stays advertised (see above); skill_* follow the skill catalog.
# Top CLIs ship 6-10 tools (Claude Code, OpenCode V2, DeepSeek Harness); every
# extra schema is paid on every request and is one more wrong choice. Those
# tools stay callable (known_names) and come back when the task is about them.
_DOMAIN_TOOL_KEYWORDS.update({
    "runtime.status": ("runtime status", "hot reload", "reload the runtime"),
    "repo.inspect": ("architecture", "overview of the", "map of the"),
    "repo.apply_patch": ("apply the patch", "apply this patch", "unified diff", ".patch", ".diff"),
    "tests.run": ("focused tests",),
    "git.status": ("git status", "staged", "unstaged", "untracked", "stash"),
    "browser.command": (
        "html", "browser", "web page", "webpage", "page", "pages", "css", "javascript",
        "canvas", "website", "frontend", "front-end", "button", "click", "dom", "playwright",
        "game", "animation", "ui",
    ),
    "lsp.diagnostics": ("type error", "type errors", "type check", "typecheck", "mypy", "pyright", "diagnostics", "lsp"),
})
#: Tools shown only in the named modes (plan.update is a planning surface).
_MODE_TOOLS: Dict[str, frozenset[str]] = {"plan.update": frozenset({"plan"})}


def _tool_relevant_for_task(
    core_name: str, task: str, *, visual: bool = False, mode: Optional[str] = None
) -> bool:
    """Whether this tool's schema belongs on the request for this task."""

    modes = _MODE_TOOLS.get(core_name)
    if modes is not None:
        return (mode or "") in modes
    keywords = _DOMAIN_TOOL_KEYWORDS.get(core_name)
    if keywords is None:
        return True
    if visual and core_name in {"browser.command", "web.fetch", "disk.scan", "disk.plan_cleanup", "repo.command"}:
        # Image tasks commonly fetch a library or an asset and check a page.
        return True
    text = f"{task} {mode or ''}".lower()
    for word in keywords:
        # Word-ish boundaries: "asset" must not fire on "assets", and "url"
        # must not fire inside an unrelated identifier.
        if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text):
            return True
    return False

def _engine_provider_tool(alias: str, definition: ToolDefinition) -> ProviderTool:
    return ProviderTool(alias, definition.description, dict(definition.input_schema))

def _finish_provider_tool() -> ProviderTool:
    return ProviderTool(
        FINISH_TOOL_NAME,
        (
            'Submit the final answer and end the task. Arguments: {"answer": "<final answer text>"}. When the task asks '
            "for a file's contents in the reply (e.g. \"reply with the complete code in one code block\"), list that file "
            'in "answer_files": KaroX appends its exact bytes as a fenced code block, so never paste the file into answer; '
            "keep answer empty when the reply must contain only the code. A file the task only asks you to save or create "
            'stays out of the reply. A web deliverable may also carry "requirements": entries for the requirement IDs KaroX '
            "lists, quoting results you already have; the list is advisory and only an unmet entry keeps the run open. "
            "After an accepted finish you cannot continue working."
        ),
        {
            "type": "object",
            "properties": {
                "answer": {"type": "string", "description": (
                    "The final answer to report to the user. May be empty when answer_files carries the whole reply.")},
                "answer_files": {"type": "array", "items": {"type": "string"}, "description": (
                    "Only files whose complete contents the task asks to see in the reply (not files it only asks to "
                    "save); each is appended verbatim as one fenced code block after answer.")},
                "superseded_tests": {"type": "array", "description": (
                    "Existing tests that assert the old behaviour the task explicitly changes and now fail for that "
                    "reason; reason quotes the task sentence verbatim."), "items": {
                    "type": "object", "properties": {"test": {"type": "string"}, "reason": {"type": "string"}},
                    "required": ["test", "reason"]}},
                "requirements": {"type": "array", "description": (
                    "Requirement attestation for a web deliverable, one entry per listed requirement ID. status "
                    "verified: observed is text copied exactly from a browser_command, checks_run or visual_check "
                    "result received after your last edit. not_applicable: observed says why the line asks nothing "
                    "of the deliverable. unmet: the requirement is not met."), "items": {
                    "type": "object", "properties": {"id": {"type": "string"}, "status": {
                        "type": "string", "enum": ["verified", "not_applicable", "unmet"]}, "observed": {"type": "string"}},
                    "required": ["id", "status", "observed"]}},
            },
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
