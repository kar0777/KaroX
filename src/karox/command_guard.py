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
"""

from __future__ import annotations

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

    if _contains_inline_remote_action(argv):
        return "inline code contains a blocked publish/auth/deploy/git-push action"
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


__all__ = ["DeveloperCommandBlocked", "validate_developer_command_argv"]
