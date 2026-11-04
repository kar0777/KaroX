"""Global safety guard for user-authorized developer commands.

Full/elevated access intentionally permits broad *local* development commands,
but it does not remove KaroX's product invariants: the agent may not publish,
deploy/release, authenticate, or push to remote Git.  Keep this guard dependency
light so both synchronous Core commands and detached durable jobs use the exact
same decision before spawning a process.

This is a command boundary, not a malware sandbox.  It rejects shell wrappers and
known remote-side-effect CLIs so a model cannot hide a blocked action in an
opaque command string. Repository code is still subject to the normal KaroX
permission and verification boundaries.

Broad process termination is rejected too: ``taskkill /IM python.exe``,
``pkill``/``killall`` and ``kill -1`` stop unrelated user programs and KaroX
itself.  Killing one process by PID stays allowed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence


class DeveloperCommandBlocked(ValueError):
    """The requested developer argv crosses a global KaroX safety invariant."""


_SHELL_EXECUTABLES = frozenset(
    {"bash", "cmd", "dash", "fish", "nu", "powershell", "pwsh", "sh", "wsl", "zsh"}
)
_REMOTE_TRANSPORT_EXECUTABLES = frozenset({"scp", "sftp", "ssh"})
_SAFE_GIT_READ_SUBCOMMANDS = frozenset(
    {
        "blame",
        "branch",
        "cat-file",
        "describe",
        "diff",
        "for-each-ref",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "merge-base",
        "name-rev",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "show-ref",
        "status",
    }
)


def _basename(value: str) -> str:
    executable = value.strip().replace("\\", "/").rsplit("/", 1)[-1].casefold()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if executable.endswith(suffix):
            executable = executable[: -len(suffix)]
            break
    return executable


def _words(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(item.strip().casefold() for item in values if item.strip())


def _git_subcommand(argv: Sequence[str]) -> str | None:
    """Return Git's subcommand while skipping common global option/value pairs."""

    options_with_value = {
        "-c",
        "-C",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
    }
    index = 1
    while index < len(argv):
        raw = argv[index].strip()
        lowered = raw.casefold()
        if raw in options_with_value or lowered in {item.casefold() for item in options_with_value}:
            index += 2
            continue
        if lowered.startswith(
            ("--config-env=", "--exec-path=", "--git-dir=", "--namespace=", "--super-prefix=", "--work-tree=")
        ):
            index += 1
            continue
        if lowered.startswith("-"):
            index += 1
            continue
        return lowered
    return None


_KILL_GUIDANCE = (
    "terminate one process by PID instead (taskkill /PID <pid> /T /F or kill <pid>), "
    "or cancel a job KaroX started with dev.command_cancel"
)


def _protected_pids() -> frozenset[int]:
    """PIDs whose termination would kill KaroX itself (this process and its parent)."""

    pids = {os.getpid()}
    try:
        parent = os.getppid()
    except (AttributeError, OSError):  # pragma: no cover - platform dependent
        parent = 0
    if parent > 1:
        pids.add(parent)
    return frozenset(pids)


def _parse_pid(value: str) -> int | None:
    text = value.strip()
    if text.startswith("+"):
        text = text[1:]
    if text.startswith("-"):
        body = text[1:]
        return -int(body) if body.isdigit() else None
    return int(text) if text.isdigit() else None


def _taskkill_reason(argv: Sequence[str]) -> str | None:
    """``taskkill`` may only target explicit PIDs: image names and filters are broad."""

    flags = [item.strip().casefold() for item in argv[1:]]
    if any(flag in {"/?", "-?", "/h", "-h"} for flag in flags):
        return None
    if any(flag in {"/s", "-s", "/u", "-u", "/p", "-p"} for flag in flags):
        return "remote-system taskkill (/S, /U, /P) is blocked by the local-first command boundary"
    if any(flag.lstrip("/-").split(":", 1)[0] in {"im", "fi"} for flag in flags if flag[:1] in "/-"):
        return (
            "killing processes by image name or filter is blocked because it also terminates "
            "unrelated processes (including KaroX itself); " + _KILL_GUIDANCE
        )
    pids: list[int] = []
    for index, flag in enumerate(flags):
        if flag in {"/pid", "-pid"} and index + 1 < len(flags):
            pid = _parse_pid(flags[index + 1])
            if pid is None:
                return "taskkill /PID needs a numeric process id; " + _KILL_GUIDANCE
            pids.append(pid)
    if not pids:
        return "taskkill without an explicit /PID is blocked; " + _KILL_GUIDANCE
    return _protected_pid_reason(pids)


def _kill_reason(argv: Sequence[str]) -> str | None:
    """POSIX ``kill``: positive PIDs only, never process groups or ``-1`` (everything)."""

    values = [item.strip() for item in argv[1:] if item.strip()]
    if any(item in {"-l", "-L", "--list", "--help", "-h"} or item.startswith("--list=") for item in values):
        return None
    pids: list[int] = []
    signal_seen = False
    index = 0
    end_of_options = False
    while index < len(values):
        item = values[index]
        if not end_of_options and item == "--":
            end_of_options = True
            index += 1
            continue
        if not end_of_options and item in {"-s", "-n", "--signal"}:
            signal_seen = True
            index += 2
            continue
        if not end_of_options and item.startswith("--signal="):
            signal_seen = True
            index += 1
            continue
        if not end_of_options and item.startswith("-") and not signal_seen:
            # The first dash word is the signal (``-9``, ``-KILL``, ``-SIGTERM``).
            signal_seen = True
            index += 1
            continue
        pid = _parse_pid(item)
        if pid is None:
            if item.startswith("%"):
                return "shell job specs are not processes KaroX can target; " + _KILL_GUIDANCE
            return "kill accepts numeric PIDs only here; " + _KILL_GUIDANCE
        pids.append(pid)
        index += 1
    if any(pid <= 0 for pid in pids):
        return (
            "kill with 0, -1 or a negative PID signals whole process groups (or every process); "
            + _KILL_GUIDANCE
        )
    return _protected_pid_reason(pids)


def _protected_pid_reason(pids: Sequence[int]) -> str | None:
    protected = _protected_pids()
    if any(pid in protected for pid in pids):
        return "this command would terminate the running KaroX process itself"
    return None


def _process_kill_reason(argv: Sequence[str]) -> str | None:
    """Reject broad, name-based process termination; allow PID-targeted kills."""

    executable = _basename(argv[0])
    words = _words(argv[1:])
    if executable == "taskkill":
        return _taskkill_reason(argv)
    if executable in {"pkill", "killall", "skill", "slay"}:
        return "killing processes by name/pattern is blocked; " + _KILL_GUIDANCE
    if executable == "kill":
        return _kill_reason(argv)
    if executable in {"wmic"} and "process" in words and (
        {"delete", "terminate"}.intersection(words) or any("terminate" in item for item in words)
    ):
        return "WMIC process termination is blocked; " + _KILL_GUIDANCE
    return None


def process_kill_block_reason(argv: Sequence[str]) -> str | None:
    """Public check shared by every KaroX process boundary (dev commands and checks)."""

    values = tuple(argv)
    if not values or not isinstance(values[0], str):
        return None
    return _process_kill_reason(values)


def _contains_inline_remote_action(argv: Sequence[str]) -> bool:
    """Catch obvious attempts to hide a forbidden command in Python/Node eval text."""

    if len(argv) < 3:
        return False
    executable = _basename(argv[0])
    inline_flags = {"-c"} if executable.startswith("python") else {"-e", "--eval"}
    if executable not in {"node", "nodejs"} and not executable.startswith("python"):
        return False
    for index, item in enumerate(argv[1:-1], start=1):
        if item.casefold() not in inline_flags:
            continue
        payload = argv[index + 1].casefold()
        patterns = (
            r"\bgit\s+push\b",
            r"\b(?:npm|pnpm|yarn|bun|cargo|poetry|twine|uv|hatch|flit)\b.{0,80}\bpublish\b",
            r"\b(?:vercel|netlify|fly|railway|firebase|wrangler|gcloud)\b.{0,80}\bdeploy\b",
            r"\b(?:gh|docker|npm|gcloud|az|vercel|netlify|hf|huggingface-cli)\b.{0,80}\b(?:auth|login|logout)\b",
            r"\btaskkill\b.{0,80}[/-](?:im|fi)\b",
            r"\b(?:pkill|killall)\b",
            # PowerShell's by-name forms, spawned from inline code: any
            # Stop-Process that does not start with -Id, and Get-Process piped
            # into Stop-Process.
            r"\b(?:stop-process|spps)\b(?![\s'\",]+-id\b)",
            r"\bget-process\b.{0,160}\|\s*(?:stop-process|spps|kill)\b",
            # psutil sweeps: iterate every process, kill the ones that match.
            r"\bprocess_iter\b.{0,400}\.(?:kill|terminate)\s*\(",
        )
        return any(re.search(pattern, payload, flags=re.DOTALL) for pattern in patterns)
    return False


def _blocked_reason(argv: Sequence[str]) -> str | None:
    executable = _basename(argv[0])
    words = _words(argv[1:])
    word_set = set(words[:12])

    if executable in _SHELL_EXECUTABLES:
        return (
            "shell wrappers are blocked for hosted developer commands because they can hide "
            "publish/auth/deploy/git-push actions; invoke the local executable directly"
        )
    if executable in _REMOTE_TRANSPORT_EXECUTABLES:
        return "remote SSH/SCP/SFTP actions are blocked by the local-first command boundary"
    if executable == "git":
        subcommand = _git_subcommand(argv)
        if subcommand not in _SAFE_GIT_READ_SUBCOMMANDS:
            return (
                "Git developer commands are read-only; use KaroX guarded local Git tools "
                "for commits and remote Git push remains blocked"
            )

    # Package/registry publishing and account mutations.
    if executable in {"npm", "npm-cli", "pnpm", "bun"}:
        if word_set.intersection({"publish", "unpublish", "login", "logout", "adduser"}):
            return "package publishing and registry authentication are blocked"
        if "token" in word_set or "owner" in word_set:
            return "package registry credential/account mutations are blocked"
    if executable == "yarn":
        if word_set.intersection({"publish", "login", "logout"}) or (
            len(words) >= 2 and words[0] == "npm" and words[1] in {"publish", "login", "logout"}
        ):
            return "package publishing and registry authentication are blocked"
    if executable in {"twine", "uv", "poetry", "hatch", "flit", "cargo"}:
        if word_set.intersection({"publish", "upload", "login", "logout", "owner", "yank"}):
            return "package publishing and registry authentication are blocked"
    if executable in {"dotnet", "nuget"} and word_set.intersection({"push", "delete"}):
        return "NuGet publishing/removal is blocked"

    # Hosting/deployment/release CLIs. Read-only/status commands remain usable.
    deploy_words = {"deploy", "publish", "release"}
    if executable in {"vercel", "netlify", "fly", "flyctl", "railway", "firebase", "wrangler"}:
        if word_set.intersection(deploy_words | {"login", "logout", "link", "init"}):
            return "deployment/release/authentication actions are blocked"
    if executable == "gcloud":
        if word_set.intersection({"auth", "deploy", "login", "revoke"}):
            return "cloud deployment/authentication actions are blocked"
    if executable in {"az", "azure"} and word_set.intersection({"login", "logout", "deployment", "deploy"}):
        return "cloud deployment/authentication actions are blocked"
    if executable == "aws" and word_set.intersection({"configure", "deploy", "publish"}):
        return "cloud deployment/authentication actions are blocked"
    if executable in {"kubectl", "helm"} and word_set.intersection(
        {"apply", "create", "delete", "patch", "replace", "rollout", "upgrade", "install", "uninstall"}
    ):
        return "cluster deployment/mutation actions are blocked"
    if executable in {"terraform", "tofu", "pulumi"} and word_set.intersection(
        {"apply", "destroy", "import", "up", "refresh"}
    ):
        return "infrastructure deployment/mutation actions are blocked"

    # Service/account CLIs with remote mutations or authentication.
    if executable == "gh":
        if not words:
            return None
        if words[0] in {"auth", "release"}:
            return "GitHub authentication/release actions are blocked"
        if words[0] == "api":
            return "arbitrary GitHub API calls are blocked in developer commands"
        if words[0] in {"pr", "issue", "repo"} and word_set.intersection(
            {"create", "merge", "close", "reopen", "edit", "delete", "fork", "archive", "rename", "review"}
        ):
            return "remote GitHub mutations are blocked"
    if executable == "docker" and word_set.intersection({"login", "logout", "push"}):
        return "container registry authentication/publishing is blocked"
    if executable in {"hf", "huggingface-cli"} and word_set.intersection(
        {"login", "logout", "upload", "repo", "delete-cache"}
    ):
        return "model-hub authentication/publishing mutations are blocked"

    kill_reason = _process_kill_reason(argv)
    if kill_reason is not None:
        return kill_reason

    if _contains_inline_remote_action(argv):
        return "inline code contains a blocked publish/auth/deploy/git-push/process-kill action"
    return None


def validate_developer_command_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Validate and return an argv safe for KaroX's elevated local-dev boundary."""

    values = tuple(argv)
    if not values or len(values) > 100 or not all(isinstance(item, str) for item in values):
        raise DeveloperCommandBlocked("developer argv must contain 1-100 strings")
    if any(not item or "\x00" in item or len(item) > 10_000 for item in values):
        raise DeveloperCommandBlocked("developer argv contains an invalid value")
    reason = _blocked_reason(values)
    if reason is not None:
        raise DeveloperCommandBlocked(reason)
    return values


__all__ = [
    "DeveloperCommandBlocked",
    "process_kill_block_reason",
    "validate_developer_command_argv",
]
