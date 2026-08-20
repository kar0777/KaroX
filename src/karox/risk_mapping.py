"""Translate a Core command into the action the RiskEngine classifies.

:class:`~karox.core.CoreRuntime` is the one place every agent reaches the
machine, whatever it arrived as: an API model, a sponsor API, ChatGPT Web,
Claude Web, an MCP client, an external coding agent or a KaroX subagent. That
makes it the only correct place to apply Smart Stop, and this module is the
adapter that lets it.

Capability policy and risk are different questions and both are needed:

* :mod:`karox.policy` answers *may this origin ever do this kind of thing*;
* :mod:`karox.risk_engine` answers *is this specific instance dangerous now*.

A profile can legitimately hold ``repo.write``. Deleting four hundred files
with it is still not something a model gets to decide alone.

The mapping is deliberately pessimistic. An unmapped command name produces an
unknown action kind, which the engine classifies as high risk, so a tool added
later cannot slip past Smart Stop just by not being listed here yet.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from .models import CoreCommand
from .risk_engine import RiskAction

# Core tool name -> risk action kind. The right-hand side is the vocabulary in
# karox.risk_engine, not a second taxonomy.
COMMAND_RISK_KINDS: Mapping[str, str] = {
    "repo.read_file": "repo.read",
    "repo.read_lines": "repo.read",
    "repo.list_files": "repo.list",
    "repo.search": "repo.search",
    "repo.write_file": "repo.write",
    "repo.edit_file": "repo.write",
    "repo.delete_file": "repo.delete",
    "repo.command": "repo.write",
    "git.status": "git.read",
    "git.diff": "git.read",
    "git.log": "git.read",
    "git.commit": "git.commit",
    "git.push": "git.push",
    "checks.run": "checks.run",
    "tests.run": "tests.run",
    "dev.command": "process.run_dev",
    "process.run": "process.run_unknown",
    "runtime.status": "status.read",
    "bridge.diagnostics": "diagnostics.read",
    "browser.command": "browser.input",
    "artifact.get": "repo.read",
    "artifact.read_image": "repo.read",
}

# Sub-actions of the single stable ``browser.command`` surface. Adding an
# action must not require a client reconnect, so the risk of one browser call
# is decided from its payload rather than from a separate tool name.
BROWSER_ACTION_RISK_KINDS: Mapping[str, str] = {
    "open": "browser.input",
    "tabs": "browser.read",
    "new_tab": "browser.input",
    "switch_tab": "browser.input",
    "close_tab": "browser.input",
    "snapshot": "browser.snapshot",
    "snapshot_diff": "browser.snapshot",
    "find_text": "browser.read",
    "get_text": "browser.read",
    "get_form_state": "browser.read",
    "click": "browser.input",
    "fill": "browser.input",
    "select": "browser.input",
    "press": "browser.input",
    "scroll": "browser.input",
    "hover": "browser.input",
    "wait_for": "browser.read",
    "wait_navigation": "browser.read",
    "wait_network_idle": "browser.read",
    "dialog": "browser.input",
    "iframe": "browser.read",
    "screenshot": "browser.screenshot",
    "screenshot_region": "browser.screenshot",
    "console": "browser.read",
    "network_failures": "browser.read",
    "network_requests": "browser.read",
    "download_start": "browser.input",
    "download_status": "browser.read",
    "upload": "browser.upload",
    "request_user_takeover": "browser.read",
    "resume_after_user_takeover": "browser.read",
    "close": "browser.input",
}

# Git subcommands that are never allowed to happen on a model's say-so. The
# guarded surface refuses most of these outright; classifying them here means a
# path that ever reaches the runtime still stops.
GIT_SUBCOMMAND_RISK_KINDS: Mapping[str, str] = {
    "push": "git.push",
    "reset": "git.reset_hard",
    "clean": "git.clean",
    "rebase": "git.rebase",
    "tag": "git.tag_delete",
    "filter-branch": "git.history_rewrite",
}

#: Payload keys that carry the paths a command will touch.
_PATH_KEYS = ("path", "paths", "file", "files", "target", "targets")


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Sequence):
        return tuple(item for item in value if isinstance(item, str) and item)
    return ()


def extract_paths(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """Collect every repository path a command payload refers to.

    Batch payloads nest their paths one level down, under ``operations``,
    ``edits`` or ``files``. Missing those would let a fifty-file batch look
    like a single bounded edit, which is the exact case Smart Stop exists for.
    """

    found: list[str] = []
    for key in _PATH_KEYS:
        found.extend(_strings(arguments.get(key)))
    for container in ("operations", "edits", "files", "patches", "changes"):
        items = arguments.get(container)
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            continue
        for item in items:
            if isinstance(item, str):
                found.append(item)
            elif isinstance(item, Mapping):
                for key in _PATH_KEYS:
                    found.extend(_strings(item.get(key)))
    # Preserve order while removing duplicates so a preview reads naturally.
    return tuple(dict.fromkeys(found))


def _count_operations(arguments: Mapping[str, Any]) -> tuple[int, int, int]:
    """Return (deletes, creates, modifies) declared by a batch payload."""

    deletes = creates = modifies = 0
    for container in ("operations", "edits", "changes"):
        items = arguments.get(container)
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            operation = str(item.get("operation") or item.get("action") or "").lower()
            if operation in {"delete", "remove", "unlink"}:
                deletes += 1
            elif operation in {"create", "add", "write_new"}:
                creates += 1
            else:
                modifies += 1
    return deletes, creates, modifies


def _browser_kind(arguments: Mapping[str, Any]) -> str:
    action = str(arguments.get("action") or arguments.get("command") or "").lower()
    if not action:
        # A browser call that does not say what it does cannot be assumed safe.
        return "browser.input"
    return BROWSER_ACTION_RISK_KINDS.get(action, "browser.input")


def _process_kind(arguments: Mapping[str, Any]) -> str:
    argv = _strings(arguments.get("argv")) or _strings(arguments.get("command"))
    if not argv:
        return "process.run_unknown"
    executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    if executable == "git":
        for token in argv[1:]:
            lowered = token.lower()
            if lowered in GIT_SUBCOMMAND_RISK_KINDS:
                if lowered == "reset" and not any(
                    item.lower() == "--hard" for item in argv
                ):
                    # A soft or mixed reset does not destroy working-tree work.
                    return "git.read"
                if lowered == "tag" and not any(
                    item.lower() in {"-d", "--delete"} for item in argv
                ):
                    return "process.run_unknown"
                if lowered == "push" and any(
                    item.lower() in {"-f", "--force", "--force-with-lease"}
                    for item in argv
                ):
                    return "git.force_push"
                return GIT_SUBCOMMAND_RISK_KINDS[lowered]
    if executable in {"pip", "pip3", "npm", "pnpm", "yarn", "uv"}:
        for token in argv[1:]:
            lowered = token.lower()
            if lowered == "publish":
                return "package.publish"
            if lowered in {"install", "add"}:
                return "package.install"
    if executable == "twine":
        return "package.publish"
    return "process.run_unknown"


def risk_kind_for(name: str, arguments: Mapping[str, Any]) -> str:
    """Return the risk vocabulary kind for one Core command.

    An unmapped name is returned unchanged so the engine sees an unknown kind
    and classifies it as high, rather than silently defaulting to safe.
    """

    if name == "browser.command":
        return _browser_kind(arguments)
    if name == "process.run":
        return _process_kind(arguments)
    mapped = COMMAND_RISK_KINDS.get(name)
    if mapped is not None:
        return mapped
    if name.startswith("mcp."):
        # An outbound MCP call leaves KaroX and can have side effects the
        # runtime cannot see, so it is a bounded mutation, never a read.
        return "mcp.call"
    return name


def action_for_command(
    command: CoreCommand,
    *,
    repository_file_count: Optional[int] = None,
    reversible_by_checkpoint: bool = False,
) -> RiskAction:
    """Build the :class:`RiskAction` for one Core command.

    ``repository_file_count`` lets the engine judge a change as a share of the
    project. Without it a hundred-file edit in a huge repository and the same
    edit in a ten-file repository would look identical.
    """

    arguments = dict(command.arguments)
    kind = risk_kind_for(command.name, arguments)
    paths = extract_paths(arguments)
    deletes, creates, modifies = _count_operations(arguments)

    if not (deletes or creates or modifies):
        # A single-target command declares its shape through its name.
        if kind == "repo.delete":
            deletes = len(paths) or 1
        elif kind in {"repo.write", "repo.create", "repo.move"}:
            modifies = len(paths) or 1

    recursive = bool(
        arguments.get("recursive")
        or arguments.get("recurse")
        or any(item.endswith(("/**", "/*")) for item in paths)
    )

    return RiskAction(
        kind=kind,
        session_id=command.session_id,
        summary=command.name,
        source=command.origin.key,
        target=paths[0] if len(paths) == 1 else "",
        paths=paths,
        delete_count=deletes,
        create_count=creates,
        modify_count=modifies,
        repository_file_count=repository_file_count,
        recursive=recursive,
        reversible_by_checkpoint=reversible_by_checkpoint,
        details={"command": command.name},
    )


__all__ = [
    "BROWSER_ACTION_RISK_KINDS",
    "COMMAND_RISK_KINDS",
    "GIT_SUBCOMMAND_RISK_KINDS",
    "action_for_command",
    "extract_paths",
    "risk_kind_for",
]
