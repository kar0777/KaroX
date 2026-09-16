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

import os
import subprocess
from pathlib import Path
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
    "disk.scan": "disk.read",
    "disk.plan_cleanup": "disk.read",
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
    "lsp.diagnostics": "diagnostics.read",
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


def _payload_sources(arguments: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Top-level arguments plus one stable nested ``payload`` object."""

    nested = arguments.get("payload")
    return (
        (arguments, nested)
        if isinstance(nested, Mapping)
        else (arguments,)
    )


def extract_paths(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """Collect every repository path a command payload refers to.

    ``repo.command`` nests its batch and unified-patch data under ``payload``.
    Missing that level used to classify a deletion batch as one ordinary write.
    Paths from patch headers are included too, but ``/dev/null`` is an operation
    marker rather than a filesystem path and is therefore omitted.
    """

    found: list[str] = []
    for source in _payload_sources(arguments):
        for key in _PATH_KEYS:
            found.extend(_strings(source.get(key)))
        for container in ("operations", "edits", "files", "patches", "changes"):
            items = source.get(container)
            if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
                continue
            for item in items:
                if isinstance(item, str):
                    found.append(item)
                elif isinstance(item, Mapping):
                    for key in _PATH_KEYS:
                        found.extend(_strings(item.get(key)))
        patch = source.get("patch")
        if isinstance(patch, str):
            for line in patch.splitlines():
                if not line.startswith(("--- ", "+++ ")):
                    continue
                value = line[4:].split("\t", 1)[0].strip()
                if value == "/dev/null":
                    continue
                if value.startswith(("a/", "b/")):
                    value = value[2:]
                if value:
                    found.append(value)
    # Preserve order while removing duplicates so a preview reads naturally.
    return tuple(dict.fromkeys(found))


def _count_operations(arguments: Mapping[str, Any]) -> tuple[int, int, int]:
    """Return (deletes, creates, modifies) declared by a mutation payload."""

    deletes = creates = modifies = 0
    for source in _payload_sources(arguments):
        for container in ("operations", "edits", "changes"):
            items = source.get(container)
            if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
                continue
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                operation = str(
                    item.get("op")
                    or item.get("operation")
                    or item.get("action")
                    or ""
                ).lower()
                if operation in {"delete", "remove", "unlink"}:
                    deletes += 1
                elif operation in {"create", "add", "write_new"}:
                    creates += 1
                else:
                    modifies += 1
        patch = source.get("patch")
        if isinstance(patch, str):
            deletes += sum(1 for line in patch.splitlines() if line.strip() == "+++ /dev/null")
            creates += sum(1 for line in patch.splitlines() if line.strip() == "--- /dev/null")
    return deletes, creates, modifies


def _delete_paths(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """Return only the targets that the payload actually deletes.

    ``extract_paths`` is intentionally broader because it powers audit/preview
    for the whole transaction. Consequence classification needs the narrower
    set: a batch that writes ``src/index.py`` and removes ``build/cache`` should
    be judged as a normal write plus a rebuildable deletion, not as if the
    source file itself were being deleted.
    """

    found: list[str] = []
    for source in _payload_sources(arguments):
        for container in ("operations", "edits", "changes"):
            items = source.get(container)
            if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
                continue
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                operation = str(
                    item.get("op")
                    or item.get("operation")
                    or item.get("action")
                    or ""
                ).lower()
                if operation not in {"delete", "remove", "unlink"}:
                    continue
                for key in _PATH_KEYS:
                    found.extend(_strings(item.get(key)))
        patch = source.get("patch")
        if isinstance(patch, str):
            previous: str | None = None
            for line in patch.splitlines():
                if line.startswith("--- "):
                    value = line[4:].split("\t", 1)[0].strip()
                    if value.startswith("a/"):
                        value = value[2:]
                    previous = "" if value == "/dev/null" else value
                    continue
                if line.startswith("+++ ") and line[4:].split("\t", 1)[0].strip() == "/dev/null":
                    if previous:
                        found.append(previous)
                if line.startswith("+++ "):
                    previous = None
    return tuple(dict.fromkeys(item for item in found if item))


def _repo_command_kind(arguments: Mapping[str, Any]) -> str:
    """A repo.command containing any deletion is a delete, not a generic write."""

    deletes, _creates, _modifies = _count_operations(arguments)
    return "repo.delete" if deletes else "repo.write"


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


def _process_delete_paths(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract concrete targets from direct filesystem-deletion argv.

    This intentionally handles only command shapes whose path arguments are
    structurally obvious. Inline shell/Python programs remain opaque and are
    therefore left for confirmation rather than guessed from source text.
    """

    argv = _strings(arguments.get("argv")) or _strings(arguments.get("command"))
    if not argv:
        return ()
    executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    direct = {"rm", "rmdir", "del", "erase", "remove-item"}
    if executable not in direct:
        return ()

    # Keep the flag list narrow and executable-specific. A leading slash is a
    # perfectly valid Unix absolute path, so slash-prefixed values are skipped
    # only for the Windows commands that actually use /S, /Q, ... switches.
    windows_switches = {
        "/s",
        "/q",
        "/f",
        "/a",
        "/p",
    }
    powershell_switches = {
        "-force",
        "-recurse",
        "-confirm",
        "-whatif",
        "-verbose",
        "-erroraction",
        "-ea",
        "-literalpath",
        "-path",
    }
    paths: list[str] = []
    skip_next = False
    for index, token in enumerate(argv[1:]):
        if skip_next:
            skip_next = False
            continue
        lowered = token.casefold()
        if executable in {"del", "erase", "rmdir"} and lowered in windows_switches:
            continue
        if executable == "remove-item" and lowered in powershell_switches:
            # -Path/-LiteralPath consume the following token *as* a path; all
            # other switches are flags. Preserve the consumed path below.
            if lowered in {"-path", "-literalpath"} and index + 2 <= len(argv) - 1:
                candidate = argv[index + 2]
                if candidate:
                    paths.append(candidate)
                skip_next = True
            continue
        if executable == "rm" and token.startswith("-"):
            continue
        if token:
            paths.append(token)
    return tuple(dict.fromkeys(paths))


def _outside_repository(paths: Sequence[str], repository: Path | None) -> bool:
    if repository is None:
        return False
    repo = repository.expanduser().resolve(strict=False)
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_absolute() and not path.drive:
            continue
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(repo)
        except (OSError, RuntimeError, ValueError):
            return True
    return False


def command_requests_deletion(name: str, arguments: Mapping[str, Any]) -> bool:
    """Whether a model command explicitly asks to destroy filesystem content.

    This is intentionally narrower than "might cause a package manager to
    replace files". Normal development must keep working. The gate catches the
    operations where deletion itself is the requested action: repo batch/patch
    deletes, direct remove commands, destructive Git cleanup, and obvious inline
    filesystem-deletion scripts.
    """

    if name == "repo.delete_file":
        return True
    if name == "repo.command":
        return _repo_command_kind(arguments) == "repo.delete"
    if name not in {"dev.command", "process.run"}:
        return False
    argv = _strings(arguments.get("argv")) or _strings(arguments.get("command"))
    if not argv:
        return False
    executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    if executable in {"rm", "rmdir", "del", "erase", "remove-item"}:
        return True
    lowered_args = [item.casefold() for item in argv[1:]]
    if executable == "git":
        if "clean" in lowered_args or "rm" in lowered_args:
            return True
        return "reset" in lowered_args and "--hard" in lowered_args
    joined = " ".join(lowered_args)
    if executable in {"powershell", "pwsh"}:
        return any(
            marker in joined
            for marker in ("remove-item", " rm ", " del ", " erase ", " rmdir ")
        )
    if executable in {"cmd", "sh", "bash", "zsh"}:
        padded = f" {joined} "
        return any(
            marker in padded
            for marker in (" rm ", " rmdir ", " del ", " erase ", " git clean ")
        )
    if executable.startswith("python"):
        return any(
            marker in joined
            for marker in (
                "os.remove(",
                "os.unlink(",
                ".unlink(",
                "shutil.rmtree(",
                "pathlib.path.rmdir(",
                "apply_cleanup_plan(",
            )
        )
    return False


def risk_kind_for(name: str, arguments: Mapping[str, Any]) -> str:
    """Return the risk vocabulary kind for one Core command.

    An unmapped name is returned unchanged so the engine sees an unknown kind
    and classifies it as high, rather than silently defaulting to safe.
    """

    if name == "browser.command":
        return _browser_kind(arguments)
    if name in {"process.run", "dev.command"}:
        # Full developer access changes *capability*, not the consequence of
        # the argv. A git push, package publish or destructive Git command must
        # keep its semantic kind even when it travels through dev.command.
        return _process_kind(arguments)
    if name == "repo.command":
        return _repo_command_kind(arguments)
    mapped = COMMAND_RISK_KINDS.get(name)
    if mapped is not None:
        return mapped
    if name.startswith("mcp."):
        # An outbound MCP call leaves KaroX and can have side effects the
        # runtime cannot see, so it is a bounded mutation, never a read.
        return "mcp.call"
    return name


def _git_head_identity(repository: Path | None) -> str:
    """Return the exact commit a push would send, or ``unborn`` fail-closed identity."""

    if repository is None:
        return "unborn"
    kwargs: dict[str, Any] = {
        "cwd": repository,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "check": False,
        "timeout": 5.0,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return "unborn"
    head = completed.stdout.strip()
    if completed.returncode == 0 and len(head) == 40:
        return head
    return "unborn"


def action_for_command(
    command: CoreCommand,
    *,
    repository: Path | None = None,
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
    deletion_requested = command_requests_deletion(command.name, arguments)
    paths = extract_paths(arguments)
    deletion_paths = _delete_paths(arguments)
    if deletion_requested and not deletion_paths:
        deletion_paths = _process_delete_paths(arguments)
    if deletion_requested and not paths:
        paths = deletion_paths
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
    details: dict[str, Any] = {
        "command": command.name,
        "deletion_requested": deletion_requested,
        "deletion_paths": list(deletion_paths),
    }
    if kind == "git.push":
        # A push approval is for one exact commit as well as one destination.
        # Both the hosted preflight and Core execution call this mapper, so a
        # HEAD change after the human says yes changes the action digest and the
        # one-shot confirmation can no longer be redeemed.
        details.update(
            {
                "remote": arguments.get("remote"),
                "branch": arguments.get("branch"),
                "head": _git_head_identity(repository),
            }
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
        outside_repository=_outside_repository(paths, repository),
        reversible_by_checkpoint=reversible_by_checkpoint,
        details=details,
    )


__all__ = [
    "BROWSER_ACTION_RISK_KINDS",
    "COMMAND_RISK_KINDS",
    "GIT_SUBCOMMAND_RISK_KINDS",
    "action_for_command",
    "command_requests_deletion",
    "extract_paths",
    "risk_kind_for",
]
