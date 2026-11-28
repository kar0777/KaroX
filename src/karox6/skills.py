"""Skills: folders with a ``SKILL.md`` (the open Agent Skills format Claude Code, Codex and
OpenCode read) that teach the agent a way of working, loaded only when a task needs one.

A skill is a directory ``<name>/SKILL.md``::

    ---
    name: release-notes
    description: Write release notes from the git log. Use when asked for a changelog.
    ---
    Instructions in Markdown; files next to SKILL.md (scripts, templates) may be named.

KaroX lists every skill's name and description in the system prompt (bounded); the body
is read with the ``skill`` tool when the model decides a skill fits, or sent with the
user's message by ``/<name>`` in the chat. Where skills are looked for, first one wins:

- the project (and its git root): ``.karox/skills``, ``.agents/skills``, ``.claude/skills``;
- plugins (``<KaroX home>/plugins/<plugin>/skills``, see karox6.plugins);
- the user: ``<KaroX home>/skills``, ``~/.agents/skills``, ``~/.claude/skills``.

Off with ``skills = false`` in config.toml (or KAROX_SKILLS=0).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from karox6.tools.base import ToolContext, ToolResult

FILE_NAME = "SKILL.md"
PROJECT_DIRS = (".karox/skills", ".agents/skills", ".claude/skills")
MAX_SKILLS = 200  # listed in the prompt
LIST_MAX_CHARS = 12_000  # the whole listing
DESCRIPTION_MAX_CHARS = 300  # one skill's line
BODY_MAX_CHARS = 60_000  # what the skill tool returns
MAX_FILE_BYTES = 1_000_000
_NAME = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    path: Path  # the SKILL.md file (a command: its .md file)
    source: str  # project | plugin:<name> | user
    kind: str = "skill"  # skill (the model may load it) | command (only /<name> sends it)

    @property
    def folder(self) -> Path:
        return self.path.parent

    def body(self) -> str:
        """The instructions without the front matter."""
        text = self.path.read_text(encoding="utf-8", errors="replace")
        return split_front_matter(text)[1].strip()


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """``---``-fenced YAML front matter (the simple subset skills use: ``key: value``,
    quoted values, ``|`` / ``>`` blocks, indented continuation lines) and the rest."""
    text = text.lstrip("﻿")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    # the closing fence starts the line: an indented "---" belongs to a block value
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    if end is None:
        return {}, text
    fields: dict[str, str] = {}
    key: str | None = None
    block: str | None = None  # "|" or ">" while a block scalar is open
    collected: list[str] = []

    def close() -> None:
        nonlocal key, block, collected
        if key is not None:
            joined = "\n".join(collected) if block == "|" else " ".join(collected)
            value = joined.strip()
            if not block:
                value = _unquote(value)
            fields[key] = value
        key, block, collected = None, None, []

    for line in lines[1:end]:
        top = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if top and not line[:1].isspace():
            close()
            key, value = top.group(1).lower(), top.group(2).strip()
            if value[:1] in ("|", ">"):
                block = value[:1]
            elif value:
                collected = [value]
            continue
        if key is not None and (line[:1].isspace() or not line.strip()):
            collected.append(line.strip())
    close()
    return fields, "\n".join(lines[end + 1 :])


_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/"}


def _unquote(value: str) -> str:
    """A YAML scalar on one line: quoted (with its escapes) or plain; a ``# comment`` after
    it is dropped."""
    if value[:1] == '"':
        out, i = [], 1
        while i < len(value):
            ch = value[i]
            if ch == "\\" and i + 1 < len(value):
                out.append(_ESCAPES.get(value[i + 1], "\\" + value[i + 1]))
                i += 2
                continue
            if ch == '"':
                return "".join(out)
            out.append(ch)
            i += 1
        return value  # never closed: as written
    if value[:1] == "'":
        out, i = [], 1
        while i < len(value):
            if value[i] == "'":
                if value[i + 1 : i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                return "".join(out)
            out.append(value[i])
            i += 1
        return value
    return re.sub(r"\s+#.*$", "", value)


def _read_skill(path: Path, source: str) -> Skill | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fields, _ = split_front_matter(text)
    name = (fields.get("name") or path.parent.name).strip().lower()
    description = " ".join((fields.get("description") or "").split())
    if not _NAME.match(name) or not description:
        return None  # the format requires both; a folder without them is not a skill
    return Skill(name, description, path, source)


def _scan(root: Path, source: str) -> list[Skill]:
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    found = []
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        skill = _read_skill(entry / FILE_NAME, source)
        if skill is not None:
            found.append(skill)
    return found


COMMAND_DIRS = (".karox/commands", ".claude/commands")


def _read_command(path: Path, name: str, source: str) -> Skill | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fields, body = split_front_matter(text)
    first = next((ln.strip().lstrip("#").strip() for ln in body.splitlines() if ln.strip()), "")
    description = " ".join((fields.get("description") or first).split())[:DESCRIPTION_MAX_CHARS]
    name = name.lower()
    if not _NAME.match(name):
        return None
    return Skill(name, description or "(no description)", path, source, kind="command")


def _scan_commands(root: Path, source: str) -> list[Skill]:
    """``name.md`` -> /name; ``group/name.md`` -> /group:name."""
    found: list[Skill] = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_file() and entry.suffix.lower() == ".md":
            command = _read_command(entry, entry.stem, source)
            if command is not None:
                found.append(command)
        elif entry.is_dir():
            try:
                inner = sorted(entry.glob("*.md"), key=lambda p: p.name.lower())
            except OSError:
                continue
            for path in inner:
                command = _read_command(path, f"{entry.name}:{path.stem}", source)
                if command is not None:
                    found.append(command)
    return found


def command_roots(
    workspace: Path, home: Path | None, *, git_root: Path | None = None
) -> list[tuple[Path, str]]:
    roots: list[tuple[Path, str]] = []
    for base in dict.fromkeys(p for p in (workspace, git_root) if p is not None):
        roots += [(base / d, "project") for d in COMMAND_DIRS]
    if home is not None:
        from karox6.plugins import plugin_dirs

        roots += [(plugin / "commands", f"plugin:{plugin.name}") for plugin in plugin_dirs(home)]
        roots.append((home / "commands", "user"))
    roots.append((Path(os.path.expanduser("~")) / ".claude" / "commands", "user"))
    return roots


def skill_roots(
    workspace: Path, home: Path | None, *, git_root: Path | None = None
) -> list[tuple[Path, str]]:
    roots: list[tuple[Path, str]] = []
    for base in dict.fromkeys(p for p in (workspace, git_root) if p is not None):
        roots += [(base / d, "project") for d in PROJECT_DIRS]
    if home is not None:
        from karox6.plugins import plugin_dirs

        roots += [(plugin / "skills", f"plugin:{plugin.name}") for plugin in plugin_dirs(home)]
        roots.append((home / "skills", "user"))
    user = Path(os.path.expanduser("~"))
    roots += [(user / ".agents" / "skills", "user"), (user / ".claude" / "skills", "user")]
    return roots


def discover(
    workspace: Path,
    home: Path | None,
    *,
    git_root: Path | None = None,
    commands: bool = False,
) -> list[Skill]:
    """Every skill KaroX can use here (with ``commands``: the slash commands too), the
    first of each name winning (skills before commands of the same name)."""
    seen: dict[str, Skill] = {}
    done: set[str] = set()
    roots = [(r, s, _scan) for r, s in skill_roots(workspace, home, git_root=git_root)]
    if commands:
        roots += [
            (r, s, _scan_commands) for r, s in command_roots(workspace, home, git_root=git_root)
        ]
    # the project before plugins before the user, whether skill or command (stable: a
    # skill before a command of the same scope)
    roots.sort(key=lambda item: _SCOPE_RANK.get(item[1].split(":", 1)[0], 3))
    for root, source, scan in roots:
        key = os.path.normcase(str(root))
        if key in done:
            continue
        done.add(key)
        for skill in scan(root, source):
            seen.setdefault(skill.name, skill)
    return list(seen.values())


_SCOPE_RANK = {"project": 0, "plugin": 1, "user": 2}


def enabled(setting: Any = None) -> bool:
    wanted = os.environ.get("KAROX_SKILLS", "")
    if wanted in ("0", "1"):
        return wanted == "1"
    return setting is not False


def listing(skills: list[Skill]) -> str:
    """The system prompt's part: what each skill is for, so the model knows when to load
    one (never the bodies)."""
    skills = [s for s in skills if s.kind == "skill"]
    if not skills:
        return ""
    lines: list[str] = []
    budget = LIST_MAX_CHARS
    for skill in skills[:MAX_SKILLS]:
        text = skill.description
        if len(text) > DESCRIPTION_MAX_CHARS:
            text = text[: DESCRIPTION_MAX_CHARS - 1].rstrip() + "…"
        line = f"- {skill.name}: {text}"
        if len(line) > budget:
            break
        budget -= len(line) + 1
        lines.append(line)
    left = len(skills) - len(lines)
    more = f"\n({left} more skills: the skill tool lists them all)" if left else ""
    return (
        "Skills: instructions for particular kinds of work. When the task matches a "
        "skill's description, load it with the skill tool before you start and follow it; "
        "otherwise ignore them.\n" + "\n".join(lines) + more
    )


def expand(skill: Skill, arguments: str = "") -> str:
    """What the model gets for a skill: its folder (for the files it names) and body."""
    try:
        body = skill.body()
    except OSError as exc:
        return f"Error: cannot read the skill {skill.name}: {exc}"
    if len(body) > BODY_MAX_CHARS:
        body = body[:BODY_MAX_CHARS] + "\n[... truncated ...]"
    if "$ARGUMENTS" in body:
        body = body.replace("$ARGUMENTS", arguments)
        arguments = ""
    if skill.kind == "command":
        # the user's own prompt, sent as they wrote it: wrapped like a skill, the model
        # went looking for a skill of that name
        return body + (f"\n\n{arguments.strip()}" if arguments.strip() else "")
    parts = [
        f'<skill name="{skill.name}" folder="{skill.folder.as_posix()}">',
        "Files this skill names are relative to its folder.",
        "",
        body,
        "</skill>",
    ]
    if arguments.strip():
        parts.append(f"\nArguments: {arguments.strip()}")
    return "\n".join(parts)


class SkillTool:
    name = "skill"
    description = (
        "Load a skill listed in the system prompt: returns its instructions (and its folder "
        "for the files they name). Call it before starting work a skill's description "
        "matches. Without a name it lists every skill."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The skill's name."},
            "arguments": {"type": "string", "description": "Optional input for the skill."},
        },
    }
    read_only = True

    def __init__(self, skills: list[Skill]) -> None:
        self.skills = {s.name: s for s in skills if s.kind == "skill"}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name") or "").strip().lower().lstrip("/")
        if not name:
            rows = [
                f"- {s.name} ({s.source}): {s.description[:DESCRIPTION_MAX_CHARS]}"
                for s in self.skills.values()
            ]
            text = "\n".join(rows)
            if len(text) > BODY_MAX_CHARS:
                text = text[:BODY_MAX_CHARS] + "\n[... more skills not shown ...]"
            return ToolResult(text or "No skills are installed.")
        skill = self.skills.get(name)
        if skill is None:
            close = [n for n in self.skills if name in n or n in name][:5]
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            return ToolResult(f"Error: no skill named {name!r}.{hint}", is_error=True)
        text = expand(skill, str(args.get("arguments") or ""))
        return ToolResult(text, is_error=text.startswith("Error:"), meta={"skill": name})
