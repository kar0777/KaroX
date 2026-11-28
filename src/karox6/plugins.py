"""Plugins: a folder that bundles skills, slash commands and MCP servers.

``<KaroX home>/plugins/<name>/`` may hold (the layout of a Claude Code plugin works)::

    skills/<skill>/SKILL.md      skills (karox6.skills)
    commands/<command>.md        slash commands: /<command> in the chat
    .mcp.json or mcp.json        MCP servers ({"mcpServers": {...}}), started with the chat
    .claude-plugin/plugin.json   or plugin.json: name, description, version (optional)

``${CLAUDE_PLUGIN_ROOT}`` / ``${KAROX_PLUGIN_ROOT}`` in a server's command, arguments,
environment or URL become the plugin's folder. ``karox6 plugin install SOURCE`` copies a
local folder or clones a git repository there; ``disable`` / ``enable`` / ``remove`` /
``list`` manage them. A disabled plugin keeps a ``.disabled`` file in its folder.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DIR_NAME = "plugins"
DISABLED = ".disabled"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ROOT_VARS = ("${CLAUDE_PLUGIN_ROOT}", "${KAROX_PLUGIN_ROOT}")


class PluginError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Plugin:
    name: str
    folder: Path
    enabled: bool
    description: str = ""
    version: str = ""

    def parts(self) -> list[str]:
        """What it brings: "3 skills", "1 command", "MCP"."""
        out = []
        skills = _count(self.folder / "skills", "*/SKILL.md")
        commands = _count(self.folder / "commands", "**/*.md")
        if skills:
            out.append(f"{skills} skill{'s' if skills != 1 else ''}")
        if commands:
            out.append(f"{commands} command{'s' if commands != 1 else ''}")
        if mcp_file(self.folder) is not None:
            out.append("MCP")
        return out


def _count(root: Path, pattern: str) -> int:
    try:
        return sum(1 for _ in root.glob(pattern))
    except OSError:
        return 0


def root(home: Path) -> Path:
    return home / DIR_NAME


def _manifest(folder: Path) -> dict[str, Any]:
    for path in (folder / ".claude-plugin" / "plugin.json", folder / "plugin.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def plugins(home: Path) -> list[Plugin]:
    try:
        entries = sorted(root(home).iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    out = []
    for folder in entries:
        if folder.name.startswith(".") or not folder.is_dir():
            continue
        data = _manifest(folder)
        out.append(
            Plugin(
                folder.name,
                folder,
                not (folder / DISABLED).exists(),
                str(data.get("description") or ""),
                str(data.get("version") or ""),
            )
        )
    return out


def plugin_dirs(home: Path) -> list[Path]:
    """Folders of the enabled plugins (sorted by name)."""
    return [p.folder for p in plugins(home) if p.enabled]


def mcp_file(folder: Path) -> Path | None:
    for name in (".mcp.json", "mcp.json"):
        if (folder / name).is_file():
            return folder / name
    return None


def _substitute(value: Any, folder: str) -> Any:
    if isinstance(value, str):
        for var in _ROOT_VARS:
            value = value.replace(var, folder)
        return value
    if isinstance(value, list):
        return [_substitute(v, folder) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, folder) for k, v in value.items()}
    return value


def mcp_servers(home: Path, warn: Any = None) -> dict[str, Any]:
    """The MCP servers the enabled plugins declare (the first plugin of a server name
    wins; the user's own mcp.json wins over every plugin)."""
    servers: dict[str, Any] = {}
    for folder in plugin_dirs(home):
        path = mcp_file(folder)
        if path is None:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            found = data.get("mcpServers") or data.get("servers") or {}
            if not isinstance(found, dict):
                raise ValueError("'mcpServers' must be an object")
        except (OSError, ValueError, AttributeError) as exc:
            if warn is not None:
                warn(f"karox: plugin {folder.name}: {path.name} skipped: {exc}\n")
            continue
        for name, spec in found.items():
            if isinstance(spec, dict):
                servers.setdefault(str(name), _substitute(spec, folder.as_posix()))
    return servers


def _looks_remote(source: str) -> bool:
    return bool(re.match(r"^(?:https?://|git@|ssh://|git://)", source)) or source.endswith(".git")


def install(home: Path, source: str, *, name: str | None = None) -> Plugin:
    """Copy a local plugin folder or clone a git repository into the plugins folder."""
    source = source.strip()
    if not source:
        raise PluginError("give a folder or a git URL")
    remote = _looks_remote(source)
    if not remote and not Path(source).expanduser().is_dir():
        raise PluginError(f"{source}: no such folder (or give a git URL)")
    guess = re.sub(r"\.git$", "", source.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1])
    target_name = name or guess
    if not _NAME.match(target_name):
        raise PluginError(f"{target_name!r} is not a usable plugin name (give --name)")
    target = root(home) / target_name
    if target.exists():
        raise PluginError(f"a plugin named {target_name} is installed (remove it first)")
    here = Path(source).expanduser().resolve()
    if not remote and root(home).resolve().is_relative_to(here):
        raise PluginError(f"{source} holds the plugins folder itself: copy a plugin folder")
    root(home).mkdir(parents=True, exist_ok=True)
    # Everything happens in a folder of this install only (hidden: never listed), which is
    # the only thing removed on failure; the finished plugin is renamed into place.
    staging = Path(tempfile.mkdtemp(dir=root(home), prefix=".install-"))
    try:
        fresh = staging / target_name
        if remote:
            _clone(source, fresh)
        else:
            shutil.copytree(here, fresh, ignore=shutil.ignore_patterns(".git"))
        if not Plugin(target_name, fresh, True).parts():
            raise PluginError(
                f"{source} has no skills/, commands/ or MCP config: not a plugin (nothing kept)"
            )
        try:
            os.rename(fresh, target)  # fails if the name was taken meanwhile
        except OSError as exc:
            raise PluginError(f"cannot install as {target_name}: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    found = next((p for p in plugins(home) if p.name == target_name), None)
    if found is None:  # pragma: no cover - just renamed into place
        raise PluginError("the plugin folder disappeared")
    return found


def _clone(source: str, into: Path) -> None:
    git = shutil.which("git")
    if git is None:
        raise PluginError("git is needed to install from a URL")
    try:
        done = subprocess.run(
            [git, "clone", "--depth", "1", "--", source, str(into)],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PluginError(f"git clone failed: {exc}") from exc
    if done.returncode != 0:
        raise PluginError(f"git clone failed: {(done.stderr or done.stdout).strip()[-400:]}")


def _find(home: Path, name: str) -> Plugin:
    found = next((p for p in plugins(home) if p.name.lower() == name.strip().lower()), None)
    if found is None:
        raise PluginError(f"no plugin named {name!r}")
    return found


def set_enabled(home: Path, name: str, enabled: bool) -> Plugin:
    plugin = _find(home, name)
    marker = plugin.folder / DISABLED
    if enabled:
        marker.unlink(missing_ok=True)
    else:
        marker.write_text("disabled by `karox6 plugin disable`\n", encoding="utf-8")
    return _find(home, name)


def remove(home: Path, name: str) -> Plugin:
    plugin = _find(home, name)
    shutil.rmtree(plugin.folder)
    return plugin
