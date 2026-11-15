"""Destructive command detection adapted from claude-code-safety-net.

https://github.com/kenryu42/claude-code-safety-net, MIT, Copyright (c) 2026 J Liew.
Subset: rm.recursive-force-root-or-home, rm.git-metadata,
rm.recursive-force-cwd-self, rm.recursive-force-outside-cwd,
find.delete-git-metadata, find.delete-root-or-home, dd.device-write,
mkfs.device, disk.erase, redirect.block-device, git.push-force,
powershell.remove-item-root-or-home, powershell.remove-item-git-metadata,
powershell.remove-item-cwd-self, cmd.recursive-delete-root-or-home and
cmd.recursive-delete-git-metadata. sql.drop-database is KaroX's own addition.
"""

from __future__ import annotations

import ntpath
import posixpath
import re
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class DangerMatch:
    rule: str
    label: str
    reason: str


_SYSTEM = {
    "/usr",
    "/etc",
    "/bin",
    "/var",
    "/opt",
    "/lib",
    "/boot",
    "/sys",
    "/proc",
    "/dev",
    "/root",
    "/home",
    "/Users",
    "/System",
    "/Library",
}
_DEVICE = re.compile(r"^/dev/(?:sd|nvme|hd|vd|xvd|mmcblk|disk)[^/]*$", re.I)
_SQL = re.compile(r"\bdrop\s+(?:database|schema)\b|\bdropDatabase\s*\(", re.I)
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
_DATABASES = {"psql", "mysql", "mariadb", "sqlcmd", "clickhouse-client", "mongosh", "mongo"}


def _match(rule: str, target: str) -> DangerMatch:
    label = rule.replace(".", ": ").replace("-", " ")
    return DangerMatch(rule, label, f"The command may cause catastrophic damage to {target!r}.")


def _basename(value: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return re.sub(r"\.(?:exe|cmd|bat)$", "", name)


def _tokens(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.escape = ""
    lexer.commenters = ""
    try:
        tokens = list(lexer)
        result: list[str] = []
        for token in tokens:
            if re.fullmatch(r"[;&|()\n]+", token):
                result.extend(re.findall(r"&&|\|\||[;&|()\n]", token))
            else:
                result.append(token)
        return result
    except ValueError:
        return re.findall(r"&&|\|\||>>|[;&|()<>]|[^\s;&|()<>]+", text)


def _path(value: str, cwd: str, home: str) -> str | None:
    value = value.strip("'\"").replace("\\", "/")
    value = re.sub(r"^\$\{HOME\}|^\$HOME(?=/|$)|^~(?=/|$)", lambda _: home, value, flags=re.I)
    value = re.sub(r"^\$\{PWD\}|^\$PWD(?=/|$)", lambda _: cwd, value, flags=re.I)
    value = re.sub(
        r"^\$\{TMPDIR\}|^\$TMPDIR(?=/|$)",
        lambda _: tempfile.gettempdir().replace("\\", "/"),
        value,
        flags=re.I,
    )
    if "$" in value or "%" in value:
        return None
    # Git Bash drive notation and native Windows paths share a representation.
    value = re.sub(r"^/([a-zA-Z])(?=/|$)", lambda m: m.group(1).lower() + ":", value)
    cwd = re.sub(r"^/([a-zA-Z])(?=/|$)", lambda m: m.group(1).lower() + ":", cwd.replace("\\", "/"))
    if re.match(r"^[a-zA-Z]:", value) or re.match(r"^[a-zA-Z]:", cwd):
        return ntpath.normcase(ntpath.normpath(ntpath.join(cwd, value))).replace("\\", "/")
    return posixpath.normpath(posixpath.join(cwd, value))


def _within(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def _root(path: str, home: str) -> bool:
    return (
        path == "/"
        or bool(re.fullmatch(r"[a-z]:/?", path, re.I))
        or path == home
        or path in _SYSTEM
    )


def _git(path: str) -> bool:
    return path.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1].lower() == ".git"


def _flags(args: list[str]) -> tuple[bool, bool]:
    recursive = any(
        a.lower() in {"--recursive", "-recurse", "-r"}
        or (a.startswith("-") and not a.startswith("--") and "r" in a[1:].lower())
        for a in args
    )
    force = any(
        a.lower() in {"--force", "-force"}
        or (a.startswith("-") and not a.startswith("--") and "f" in a[1:].lower())
        for a in args
    )
    return recursive, force


class _Checker:
    def __init__(self, cwd: Path, workspace: Path, home: Path) -> None:
        self.cwd = str(cwd).replace("\\", "/")
        self.home = _path(str(home), self.cwd, str(home)) or str(home)
        self.workspace = _path(str(workspace), self.cwd, self.home) or str(workspace)

    def scan(self, text: str, shell: str, cwd: str, depth: int = 0) -> DangerMatch | None:
        if depth > 8:
            return None
        # Single-quoted text is literal; command substitution in double quotes executes.
        for substitution in re.finditer(r"'[^']*'|\$\(([^()]*)\)|`([^`]*)`", text):
            inner = substitution.group(1) or substitution.group(2)
            if inner:
                found = self.scan(inner, shell, cwd, depth + 1)
                if found:
                    return found
        tokens = _tokens(text)
        segments: list[tuple[list[str], str]] = []
        part: list[str] = []
        separator = ""
        for token in tokens:
            if token in {";", "&&", "||", "|", "&", "(", ")", "\n"}:
                if part:
                    segments.append((part, separator))
                    part = []
                separator = token
            else:
                part.append(token)
        if part:
            segments.append((part, separator))
        producer: list[str] = []
        current = cwd
        for args, sep in segments:
            if sep != "|":
                producer = []
            found = self.simple(args, shell, current, depth, producer)
            if found:
                return found
            if args and _basename(args[0]) == "cd" and len(args) > 1:
                current = _path(args[1], current, self.home) or current
            producer = args
        return None

    def simple(
        self, args: list[str], shell: str, cwd: str, depth: int, producer: list[str]
    ) -> DangerMatch | None:
        if not args:
            return None
        args = args[:]
        while args and _ASSIGN.match(args[0]):
            args.pop(0)
        if not args:
            return None
        name = _basename(args[0])
        if name in {
            "sudo",
            "doas",
            "env",
            "command",
            "builtin",
            "exec",
            "nice",
            "nohup",
            "time",
            "timeout",
            "xargs",
        }:
            index = 1
            while index < len(args):
                arg = args[index]
                if _ASSIGN.match(arg):
                    index += 1
                elif arg in {
                    "-u",
                    "--user",
                    "-g",
                    "--group",
                    "-n",
                    "-P",
                    "-I",
                    "-d",
                    "--max-args",
                    "--max-procs",
                    "--signal",
                    "-s",
                    "-k",
                    "--kill-after",
                }:
                    index += 2
                elif arg.startswith("-"):
                    index += 1
                else:
                    break
            if name == "timeout" and index < len(args):
                index += 1
            if (
                name == "xargs"
                and index < len(args)
                and producer
                and _basename(producer[0]) in {"echo", "printf"}
            ):
                targets = [
                    target
                    for target in producer[1:]
                    if (path := _path(target, cwd, self.home)) and _root(path, self.home)
                ]
                args = [*args[index:], *targets]
            else:
                args = args[index:]
            return self.simple(args, shell, cwd, depth + 1, []) if depth < 8 else None
        if name in {"bash", "sh", "zsh", "dash", "ksh", "pwsh", "powershell", "cmd"}:
            for i, arg in enumerate(args[1:], 1):
                if arg.lower() in {"-c", "-lc", "-command", "/c"} and i + 1 < len(args):
                    inner_shell = (
                        "pwsh"
                        if name in {"pwsh", "powershell"}
                        else "cmd"
                        if name == "cmd"
                        else "bash"
                    )
                    return self.scan(" ".join(args[i + 1 :]), inner_shell, cwd, depth + 1)
        if name == "eval":
            return self.scan(" ".join(args[1:]), shell, cwd, depth + 1)
        if name == "find":
            destructive = "-delete" in args or any(
                args[i] in {"-exec", "-execdir"}
                and i + 1 < len(args)
                and _basename(args[i + 1]) == "rm"
                for i in range(len(args))
            )
            if destructive:
                for target in args[1:]:
                    if target.startswith("-"):
                        break
                    path = _path(target, cwd, self.home)
                    if _git(target):
                        return _match("find.delete-git-metadata", target)
                    if path and _root(path, self.home):
                        return _match("find.delete-root-or-home", target)
            for i, arg in enumerate(args):
                if arg in {"-exec", "-execdir"}:
                    command = [arg for arg in args[i + 1 :] if arg not in {"\\", ";", "+"}]
                    found = self.simple(command, shell, cwd, depth + 1, []) if depth < 8 else None
                    if found:
                        return found
        if name == "rm" and shell == "bash":
            recursive, force = _flags(args[1:])
            for target in args[1:]:
                if target.startswith("-"):
                    continue
                path = _path(target, cwd, self.home)
                if path is not None and (recursive or force) and _git(target):
                    return _match("rm.git-metadata", target)
                if not (recursive and force) or path is None:
                    continue
                stripped = target.replace("\\", "/").rstrip("/")
                base = (path[:-2] or "/") if path.endswith("/*") else path
                if _root(base, self.home):
                    return _match("rm.recursive-force-root-or-home", target)
                if stripped in {".", "*", "./*", "..", "$PWD", "${PWD}"} or path == self.workspace:
                    return _match("rm.recursive-force-cwd-self", target)
                # Judge the resolved path, however it was spelled (~/x, $HOME/x, ../x, /x).
                temps = ["/tmp", "/var/tmp", _path(tempfile.gettempdir(), cwd, self.home) or ""]
                if not _within(path, self.workspace) and not any(
                    t and _within(path, t) for t in temps
                ):
                    return _match("rm.recursive-force-outside-cwd", target)
        if shell == "pwsh" and name in {"remove-item", "rm", "del", "rd", "rmdir", "ri"}:
            recursive = any(a.lower() in {"-recurse", "-r"} for a in args)
            for target in args[1:]:
                if target.startswith("-"):
                    continue
                path = _path(target, cwd, self.home)
                if path is not None and _git(target):
                    return _match("powershell.remove-item-git-metadata", target)
                special = target.lower() in {"$home", "${home}", "$env:userprofile", "~"}
                if recursive and (
                    special
                    or (
                        path
                        and (_root(path, self.home) or path.lower() in {"c:/windows", "c:/users"})
                    )
                ):
                    return _match("powershell.remove-item-root-or-home", target)
                if recursive and (
                    target.replace("\\", "/").rstrip("/") in {".", "*", "./*"}
                    or path == self.workspace
                ):
                    return _match("powershell.remove-item-cwd-self", target)
        if (
            shell == "cmd"
            and name in {"rmdir", "rd", "del"}
            and any(a.lower() == "/s" for a in args)
        ):
            for target in args[1:]:
                if target.lower() in {"/s", "/q", "/f"}:
                    continue
                if _git(target) and name != "del":
                    return _match("cmd.recursive-delete-git-metadata", target)
                path = _path(target, cwd, self.home)
                if target.lower() == "%userprofile%" or (path and _root(path, self.home)):
                    return _match("cmd.recursive-delete-root-or-home", target)
        if name == "dd" and any(a.startswith("of=") and _DEVICE.match(a[3:]) for a in args):
            return _match("dd.device-write", "disk device")
        if (
            name == "mkfs"
            or name.startswith("mkfs.")
            or name in {"mke2fs", "mkswap", "wipefs", "fdisk", "sfdisk", "parted", "sgdisk"}
        ) and any(a.startswith("/dev/") for a in args[1:]):
            return _match("mkfs.device", "device")
        if (
            (
                name == "diskutil"
                and any(
                    a.lower()
                    in {"erasedisk", "erasevolume", "zerodisk", "randomdisk", "secureerase"}
                    for a in args[1:]
                )
            )
            or (name == "format" and any(re.fullmatch(r"[a-z]:", a, re.I) for a in args[1:]))
            or name in {"diskpart", "format-volume", "clear-disk", "initialize-disk"}
        ):
            return _match("disk.erase", "disk")
        if any(
            args[i] in {">", ">>"} and i + 1 < len(args) and _DEVICE.match(args[i + 1])
            for i in range(len(args))
        ) or (name == "tee" and any(_DEVICE.match(a) for a in args[1:])):
            return _match("redirect.block-device", "disk device")
        if name == "git" and "push" in args[1:]:
            index = args.index("push")
            if any(
                a in {"--force", "-f", "--force-with-lease", "--force-if-includes", "--mirror"}
                or a.startswith("--force-with-lease=")
                or a.startswith("+")
                for a in args[index + 1 :]
            ):
                return _match("git.push-force", "remote repository")
        if name in _DATABASES and (
            _SQL.search(" ".join(args[1:]))
            or (
                producer
                and _basename(producer[0]) in {"echo", "printf"}
                and _SQL.search(" ".join(producer[1:]))
            )
        ):
            return _match("sql.drop-database", "database or schema")
        return None


def check_command(
    command: str,
    *,
    shell: Literal["bash", "pwsh"] = "bash",
    cwd: Path,
    workspace: Path | None = None,
    home: Path | None = None,
) -> DangerMatch | None:
    """Return the first matching catastrophic rule, or None."""
    checker = _Checker(
        cwd, workspace if workspace is not None else cwd, home if home is not None else Path.home()
    )
    return checker.scan(command, shell, checker.cwd)
