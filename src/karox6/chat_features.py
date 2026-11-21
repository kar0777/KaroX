"""Chat features that are plain functions of their inputs: the change preview under an
edit, pictures attached to a message, the ``/init`` prompt and the ``/mcp`` screen.

Kept out of ``tui`` so each can be tested without a chat, and so ``tui`` stays the place
for the conversation flow only.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from karox6.core.messages import IMAGE_MAX_BYTES, IMAGE_TYPES
from karox6.tui_text import _plain_text, clip_cells

PREVIEW_LINES = 8  # changed lines shown under an edit; /diff shows everything
PREVIEW_WIDTH = 160

INIT_PROMPT = """Write an AGENTS.md file at the root of this project: the notes a coding agent needs before it changes anything here. Look at the project first (README, build and test configuration, the directory layout, a few representative source and test files), then write the file. Cover, briefly and only with what is true for this project:

1. Commands: how to install, build, lint, type-check and test, including how to run a single test. Take them from the project's own configuration and scripts; do not guess.
2. Layout: the main directories and what lives in each; the entry points.
3. Conventions that the code follows and a newcomer would get wrong: naming, error handling, formatting, how tests are organised, anything the linters enforce.
4. Rules of the house, if the repository states any (CONTRIBUTING, existing agent or editor rules such as CLAUDE.md, .cursor/rules, .github/copilot-instructions.md): fold them in.

Keep it under about 60 lines, as short bullet points; no generic advice that would fit any project. If AGENTS.md already exists, improve it in place: keep what is right, fix what is out of date, add what is missing. Do not change any other file. When done, say in two or three lines what the file now covers."""  # noqa: E501


# -- change preview -------------------------------------------------------------------


def diff_preview(
    name: str, arguments: str, meta: dict[str, Any], limit: int = PREVIEW_LINES
) -> list[tuple[str, str]]:
    """The lines an edit changed, as (kind, text) with kind ``-``, ``+`` or ``…``; text
    carries the line number when the tool reported where the change starts. Empty for
    other tools and for arguments that cannot be read."""
    base = name.rsplit("/", 1)[-1]
    if base != "edit":
        return []
    try:
        data = json.loads(arguments)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    old, new = data.get("old_string"), data.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        return []
    before, after = old.splitlines(), new.splitlines()
    start = meta.get("line")
    number = start if isinstance(start, int) and start > 0 else None
    out: list[tuple[str, str]] = []
    hidden = 0
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for tag, a1, a2, b1, b2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        rows = [("-", a1 + i, before[a1 + i]) for i in range(a2 - a1)]
        rows += [("+", b1 + i, after[b1 + i]) for i in range(b2 - b1)]
        for kind, offset, text in rows:
            if len(out) >= limit:
                hidden += 1
                continue
            label = f"{number + offset:>5} " if number is not None else "  "
            shown = clip_cells(_plain_text(text.expandtabs(4)), PREVIEW_WIDTH)
            out.append((kind, f"{label}{kind} {shown}"))
    if hidden:
        out.append(("…", f"  … {hidden} more changed line{'s' if hidden != 1 else ''} (/diff)"))
    return out


# -- pictures -------------------------------------------------------------------------

_IMAGE_TOKEN = re.compile(
    r"""(?:^|(?<=\s))@?(?:"([^"\n]+\.(?:png|jpe?g|gif|webp))"|'([^'\n]+\.(?:png|jpe?g|gif|webp))'"""
    r"""|((?:[A-Za-z]:[\\/]|[\\/~.]|[\w.-]+[\\/])?[^\s"']*\.(?:png|jpe?g|gif|webp)))(?=\s|$)""",
    re.I,
)


_ROOTED = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]|~|\.{1,2}[\\/])")  # what drag-and-drop types


@dataclass(slots=True)
class FoundImages:
    text: str  # the message with each attached picture's token replaced by [image N]
    paths: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def find_images(text: str, workspace: Path, already: int = 0) -> FoundImages:
    """Pictures the user named in a message: ``@shot.png``, a plain or quoted path (a file
    dragged into the terminal is typed as its path). Only files that exist count; a word
    that merely ends in .png stays as it is. ``already`` pictures are attached before
    these (their numbers come first)."""
    found = FoundImages(text)
    pieces: list[str] = []
    pos = 0
    seen: dict[str, int] = {}
    for match in _IMAGE_TOKEN.finditer(text):
        raw = match.group(1) or match.group(2) or match.group(3) or ""
        explicit = match.group(0).lstrip().startswith("@")
        quoted = match.group(3) is None
        if not explicit and not quoted and not _ROOTED.match(raw):
            continue  # a bare name in a sentence ("fix logo.png") is not an attachment
        path = _resolve_image(raw, workspace)
        if path is None:
            if explicit:
                found.warnings.append(f"skipped @{raw} (no such picture)")
            continue
        key = os.path.normcase(str(path))
        if key not in seen:
            try:
                size = path.stat().st_size
            except OSError as exc:
                found.warnings.append(f"skipped {raw} ({exc})")
                continue
            if size > IMAGE_MAX_BYTES:
                found.warnings.append(
                    f"skipped {raw} ({size / 1_048_576:.1f} MB; pictures up to "
                    f"{IMAGE_MAX_BYTES // 1_048_576} MB can be attached)"
                )
                continue
            if size == 0:
                found.warnings.append(f"skipped {raw} (empty file)")
                continue
            found.paths.append(path)
            seen[key] = already + len(found.paths)
        pieces.append(text[pos : match.start()])
        pieces.append(f"[image {seen[key]}]")
        pos = match.end()
    pieces.append(text[pos:])
    found.text = "".join(pieces)
    return found


def _resolve_image(raw: str, workspace: Path) -> Path | None:
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = workspace / path
        path = path.resolve()
        return path if path.is_file() else None
    except (OSError, ValueError, RuntimeError):
        return None


def store_image(session_dir: Path, source: Path, number: int) -> dict[str, str]:
    """Copy a picture into the session (so that every later request sends the same bytes,
    whatever happens to the original) and return its message attachment."""
    suffix = source.suffix.lower()
    target = session_dir / "images" / f"{number:03d}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    return {"path": str(target), "media_type": IMAGE_TYPES.get(suffix, "image/png")}


def image_label(path: Path) -> str:
    """``name (WxH, size)`` for the chat line that confirms an attachment."""
    size = path.stat().st_size
    shown = f"{size / 1024:.0f} KB" if size >= 1024 else f"{size} B"
    dims = ""
    if path.suffix.lower() == ".png":
        from karox6.tools.screen import ScreenError, png_size

        try:
            with path.open("rb") as stream:
                width, height = png_size(stream.read(32))
            dims = f"{width}x{height}, "
        except (OSError, ScreenError):
            dims = ""
    return f"{path.name} ({dims}{shown})"


# -- /mcp -----------------------------------------------------------------------------


@dataclass(slots=True)
class McpState:
    """What the chat knows about its MCP servers: the config file, the servers that are
    connected and why the others are not."""

    path: Path | None  # None: MCP is switched off (--no-mcp)
    handles: list[Any] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)


def mcp_report(
    state: McpState | None,
    paint: Callable[[str, str], str],
    glyph: dict[str, str],
    server: str = "",
) -> str:
    """The ``/mcp`` screen: every configured server with its state, or one server's tools."""
    from karox6.mcp.client import configured_servers, describe_server, tool_name

    if state is None:
        return "MCP servers are loaded with the first message; send one, then /mcp\n"
    if state.path is None:
        return "MCP servers are switched off for this chat (--no-mcp)\n"
    servers = configured_servers(state.path)
    connected = {h.name: h for h in state.handles if hasattr(h, "tools")}
    if not servers and not connected:
        example = '{"mcpServers": {"docs": {"command": "npx", "args": ["-y", "some-mcp-server"]}}}'
        return (
            f"no MCP servers configured\n"
            f"  add them to {state.path} and restart the chat, for example:\n"
            f"  {paint('dim', example)}\n"
            "  a server is either a local command (command, args, env) or a URL (url, headers)\n"
        )
    if server:
        handle = connected.get(server)
        if handle is None:
            reason = state.failures.get(server)
            known = ", ".join(servers) or "none"
            if reason is None and server not in servers:
                return f"no MCP server named {server}; configured: {known}\n"
            return f"{server}: not connected ({_plain_text(reason or 'unknown reason')})\n"
        lines = [paint("bold", f"{server}") + f"  {len(handle.tools)} tools"]
        for spec in handle.tools:
            about = " ".join(str(spec.get("description") or "").split())
            name = tool_name(server, str(spec.get("name")))
            lines.append(f"  {paint('cyan', name)}  {paint('dim', clip_cells(about, 100))}")
        return "\n".join(lines) + "\n"
    lines = [paint("bold", "MCP servers") + paint("dim", f"  ({state.path})")]
    for name in dict.fromkeys([*servers, *connected]):
        kind, where = describe_server(servers.get(name, {}))
        handle = connected.get(name)
        if handle is not None:
            mark = paint("green", glyph["ok"])
            restarts = f", restarted {handle.restarts}x" if handle.restarts else ""
            status = f"connected, {len(handle.tools)} tools{restarts}"
        elif servers.get(name, {}).get("disabled"):
            mark, status = paint("dim", "-"), "disabled in the config"
        else:
            mark = paint("red", glyph["fail"])
            status = "failed: " + clip_cells(_plain_text(state.failures.get(name, "unknown")), 120)
        lines.append(f"  {mark} {paint('bold', name)}  {status}")
        lines.append(paint("dim", f"      {kind}: {clip_cells(_plain_text(where), 110)}"))
    hint = "  /mcp NAME lists a server's tools; changes to the config apply on restart"
    lines.append(paint("dim", hint))
    return "\n".join(lines) + "\n"


# -- hints ----------------------------------------------------------------------------


def closest_command(name: str, commands: list[str]) -> str | None:
    """The command the user most likely meant (a typo, a prefix), if any."""
    if len(name) < 2:
        return None
    starts = [c for c in commands if c.startswith(name)]
    if len(starts) == 1:
        return starts[0]
    near = difflib.get_close_matches(name, commands, n=1, cutoff=0.6)
    return near[0] if near else None
