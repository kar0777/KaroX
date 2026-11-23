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
import tempfile
from collections.abc import Sequence
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


@dataclass(frozen=True, slots=True)
class _Token:
    kind: Literal["word", "sep", "redir"]
    value: str


_REDIR_OPS = ("&>>", "&>", "<<<", "<<-", "<<", "<&", "<>", ">>", ">&", ">|", "<", ">")
_FD_REDIR_OPS = ("<<<", "<<-", "<<", "<&", "<>", ">>", ">&", ">|", "<", ">")
_PWSH_ALL_REDIR_OPS = (">>", ">&", ">")
_OUTPUT_REDIR_OPS = {">", ">>", ">|", "<>", "&>", "&>>", ">&"}


def _match_prefix(text: str, pos: int, prefixes: tuple[str, ...]) -> str | None:
    for op in prefixes:
        if text.startswith(op, pos):
            return op
    return None


def _tokenize(text: str, shell: str = "bash") -> list[_Token]:
    tokens: list[_Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r":
            i += 1
            continue
        if ch == "\n":
            tokens.append(_Token("sep", "\n"))
            i += 1
            continue
        if text.startswith(("&&", "||"), i):
            tokens.append(_Token("sep", text[i : i + 2]))
            i += 2
            continue
        if shell == "bash" and text.startswith("|&", i):
            tokens.append(_Token("sep", "|"))
            i += 2
            continue
        if op := _match_prefix(text, i, _REDIR_OPS):
            tokens.append(_Token("redir", op))
            i += len(op)
            continue
        if ch in ";|&()":
            tokens.append(_Token("sep", ch))
            i += 1
            continue
        after_redir = bool(tokens) and tokens[-1].kind == "redir"
        if not after_redir and ch.isdigit():
            j = i
            while j < n and text[j].isdigit():
                j += 1
            if op := _match_prefix(text, j, _FD_REDIR_OPS):
                tokens.append(_Token("redir", op))
                i = j + len(op)
                continue
        if (
            not after_redir
            and shell == "pwsh"
            and ch == "*"
            and (op := _match_prefix(text, i + 1, _PWSH_ALL_REDIR_OPS))
        ):
            tokens.append(_Token("redir", op))
            i += 1 + len(op)
            continue
        buf: list[str] = []
        while i < n:
            c = text[i]
            if c in " \t\r\n;|&()<>":
                break
            if c in "'\"":
                i += 1
                end = text.find(c, i)
                if end < 0:
                    buf.append(text[i:])
                    i = n
                else:
                    buf.append(text[i:end])
                    i = end + 1
            else:
                buf.append(c)
                i += 1
        tokens.append(_Token("word", "".join(buf)))
    return tokens


def _split_redirections(segment: list[_Token]) -> tuple[list[str], list[str], list[str]]:
    """Separate command arguments from redirection operators and their targets.

    Returns ``(args, output_targets, herestrings)``.
    """
    args: list[str] = []
    outputs: list[str] = []
    herestrings: list[str] = []
    i = 0
    while i < len(segment):
        tok = segment[i]
        if tok.kind == "redir":
            if i + 1 < len(segment) and segment[i + 1].kind == "word":
                target = segment[i + 1].value
                if tok.value in _OUTPUT_REDIR_OPS:
                    outputs.append(target)
                elif tok.value == "<<<":
                    herestrings.append(target)
                i += 2
            else:
                i += 1
        else:
            args.append(tok.value)
            i += 1
    return args, outputs, herestrings


def _substitutions(text: str, shell: str) -> list[str]:
    """Bodies of ``$(...)`` and backtick substitutions that the shell would execute.

    Single quotes are literal only outside double quotes, and ``$(...)`` may nest.
    """
    bash = shell == "bash"
    found: list[str] = []
    i, n, double = 0, len(text), False
    while i < n:
        char = text[i]
        if (char == "\\" and bash) or (char == "`" and not bash):
            i += 2  # an escaped character (PowerShell escapes with a backtick)
            continue
        if char == "'" and not double:
            end = text.find("'", i + 1)
            i = n if end < 0 else end + 1
            continue
        if char == '"':
            double = not double
        elif char == "`":
            end = text.find("`", i + 1)
            end = n if end < 0 else end
            found.append(text[i + 1 : end])
            i = end + 1
            continue
        elif text.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            found.append(text[i + 2 : j - 1 if depth == 0 else n])
            i = j
            continue
        i += 1
    return found


_VARIABLE = re.compile(r"\$\{[A-Za-z_][A-Za-z_0-9]*\}|\$[A-Za-z_][A-Za-z_0-9]*")


def _path(value: str, cwd: str, home: str, *, unset_empty: bool = False) -> str | None:
    """Resolve a target path; None when it depends on unknown variables.

    ``unset_empty`` resolves it as if every unknown variable were unset (empty).
    """
    value = value.strip("'\"").replace("\\", "/")
    value = re.sub(r"^\$\{HOME\}|^\$HOME(?=/|$)|^~(?=/|$)", lambda _: home, value, flags=re.I)
    value = re.sub(r"^\$\{PWD\}|^\$PWD(?=/|$)", lambda _: cwd, value, flags=re.I)
    value = re.sub(
        r"^\$\{TMPDIR\}|^\$TMPDIR(?=/|$)",
        lambda _: tempfile.gettempdir().replace("\\", "/"),
        value,
        flags=re.I,
    )
    if unset_empty and "${" not in _VARIABLE.sub("", value):  # ${X:-y}, ${X:?}: not empty
        value = _VARIABLE.sub("", value)
        if not value:
            return None
    if "$" in value or "%" in value:
        return None
    # Git Bash drive notation and native Windows paths share a representation.
    value = re.sub(r"^/([a-zA-Z])(?=/|$)", lambda m: m.group(1).lower() + ":", value)
    cwd = re.sub(r"^/([a-zA-Z])(?=/|$)", lambda m: m.group(1).lower() + ":", cwd.replace("\\", "/"))
    if re.match(r"^[a-zA-Z]:", value) or re.match(r"^[a-zA-Z]:", cwd):
        return ntpath.normcase(ntpath.normpath(ntpath.join(cwd, value))).replace("\\", "/")
    # normpath keeps a leading "//"; for rm it is the root all the same.
    return re.sub(r"^//+", "/", posixpath.normpath(posixpath.join(cwd, value)))


def _glob_base(path: str) -> str:
    return (path[:-2] or "/") if path.endswith("/*") else path


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
        for inner in _substitutions(text, shell):
            found = self.scan(inner, shell, cwd, depth + 1) if inner.strip() else None
            if found:
                return found
        tokens = _tokenize(text, shell)
        segments: list[tuple[list[_Token], str]] = []
        part: list[_Token] = []
        separator = ""
        for token in tokens:
            if token.kind == "sep":
                if part:
                    segments.append((part, separator))
                    part = []
                separator = token.value
            else:
                part.append(token)
        if part:
            segments.append((part, separator))
        producer: list[str] = []
        current = cwd
        for raw, sep in segments:
            if sep != "|":
                producer = []
            args, outputs, herestrings = _split_redirections(raw)
            found = self.simple(
                args, shell, current, depth, producer, outputs=outputs, herestrings=herestrings
            )
            if found:
                return found
            if args and _basename(args[0]) == "cd" and len(args) > 1:
                current = _path(args[1], current, self.home) or current
            producer = args
        return None

    def simple(
        self,
        args: list[str],
        shell: str,
        cwd: str,
        depth: int,
        producer: list[str],
        *,
        outputs: Sequence[str] = (),
        herestrings: Sequence[str] = (),
    ) -> DangerMatch | None:
        if not args:
            if any(_DEVICE.match(t) for t in outputs):
                return _match("redirect.block-device", "disk device")
            return None
        args = args[:]
        while args and _ASSIGN.match(args[0]):
            args.pop(0)
        if not args:
            if any(_DEVICE.match(t) for t in outputs):
                return _match("redirect.block-device", "disk device")
            return None
        if shell == "bash" and "\\" in args[0] and depth < 8:
            # Unquoted, bash drops the backslashes (r\m is rm); quoted, it is a Windows path.
            unescaped = [args[0].replace("\\", ""), *args[1:]]
            found = self.simple(
                unescaped, shell, cwd, depth + 1, producer, outputs=outputs, herestrings=herestrings
            )
            if found:
                return found
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
            return (
                self.simple(
                    args, shell, cwd, depth + 1, [], outputs=outputs, herestrings=herestrings
                )
                if depth < 8
                else None
            )
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
                if not (recursive and force):
                    continue
                if path is None:
                    # An unset variable expands to nothing: `rm -rf $DIR/` deletes `/`.
                    empty = _path(target, cwd, self.home, unset_empty=True)
                    if empty and _root(_glob_base(empty), self.home):
                        return _match("rm.recursive-force-root-or-home", target)
                    continue
                stripped = target.replace("\\", "/").rstrip("/")
                base = _glob_base(path)
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
        if any(_DEVICE.match(t) for t in outputs) or (
            name == "tee" and any(_DEVICE.match(a) for a in args[1:])
        ):
            return _match("redirect.block-device", "disk device")
        if name == "git" and "push" in args[1:]:
            index = args.index("push")
            if any(
                a in {"--force", "--force-with-lease", "--force-if-includes", "--mirror"}
                or (a.startswith("-") and not a.startswith("--") and "f" in a[1:])  # -f, -uf
                or a.startswith("--force-with-lease=")
                or a.startswith("+")
                for a in args[index + 1 :]
            ):
                return _match("git.push-force", "remote repository")
        if name in _DATABASES and (
            _SQL.search(" ".join(args[1:]))
            or _SQL.search(" ".join(herestrings))
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
