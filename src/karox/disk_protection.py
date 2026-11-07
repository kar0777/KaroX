"""Disk protection: system roots, reparse boundaries and protected targets."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Sequence
from .paths import runtime_dir



class CleanupError(RuntimeError):
    """A cleanup scan/plan/apply boundary refused an unsafe or stale request."""

_WINDOWS_PROTECTED_NAMES = frozenset(
    {
        "$recycle.bin",
        "boot",
        "efi",
        "recovery",
        "system volume information",
    }
)

_WINDOWS_PROTECTED_FILES = frozenset(
    {
        "bootmgr",
        "hiberfil.sys",
        "pagefile.sys",
        "swapfile.sys",
    }
)

def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))

def _is_within(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((_norm(path), _norm(parent))) == _norm(parent)
    except (OSError, ValueError):
        return False

def is_drive_root(path: str | Path) -> bool:
    """Whether ``path`` is a filesystem root (``C:\\`` on Windows)."""

    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_dir():
        return False
    if os.name == "nt":
        if not resolved.anchor:
            return False
        return _norm(resolved) == _norm(Path(resolved.anchor))
    return resolved == Path(resolved.anchor or "/")

def _environment_system_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for name in (
        "SystemRoot",
        "windir",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "ProgramData",
        "ALLUSERSPROFILE",
    ):
        value = os.environ.get(name)
        if not value:
            continue
        with _suppress_os_errors():
            path = Path(value).expanduser().resolve(strict=False)
            if path not in roots:
                roots.append(path)
    return tuple(roots)

class _suppress_os_errors:
    """Tiny local context manager to keep environment discovery dependency-free."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, (OSError, RuntimeError, ValueError))

def workspace_system_reason(path: str | Path) -> Optional[str]:
    """Return a stable reason for a forbidden workspace, while allowing roots.

    A drive root is allowed because drive maintenance has a separate read-only
    model tool surface. A system *subdirectory* such as ``C:\\Windows`` is still
    refused as a workspace.
    """

    resolved = Path(path).expanduser().resolve(strict=True)
    if is_drive_root(resolved):
        return None
    for protected in _environment_system_roots():
        if _is_within(resolved, protected):
            return "windows_system_folder"
    if os.name == "nt":
        parts = [part.casefold() for part in resolved.parts]
        if any(part in _WINDOWS_PROTECTED_NAMES for part in parts):
            return "windows_system_folder"
    return None

def _karox_protected_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for value in (
        runtime_dir(),
        Path(sys.prefix),
        Path(__file__).resolve().parent,
    ):
        with _suppress_os_errors():
            path = Path(value).expanduser().resolve(strict=False)
            if path not in roots:
                roots.append(path)
    return tuple(roots)

def _is_reparse_boundary(path: Path) -> bool:
    """True for symlinks and Windows junctions/reparse directory boundaries."""

    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError:
        # If Windows cannot classify a reparse point reliably, fail closed.
        return True

def _validate_relative_target(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise CleanupError("cleanup target must be a non-empty relative path")
    normalized = relative.replace("\\", "/").strip().strip("/")
    candidate_rel = Path(normalized)
    if candidate_rel.is_absolute() or candidate_rel.drive:
        raise CleanupError("cleanup target must be relative to the selected workspace")
    if any(part in {"", ".", ".."} for part in candidate_rel.parts):
        raise CleanupError("cleanup target contains an unsafe path segment")
    cursor = root
    for part in candidate_rel.parts:
        cursor = cursor / part
        if _is_reparse_boundary(cursor):
            raise CleanupError(f"cleanup target crosses a symlink/junction: {relative}")
    if not cursor.exists():
        raise CleanupError(f"cleanup target no longer exists: {relative}")
    if not _is_within(cursor, root):
        raise CleanupError("cleanup target escapes the selected workspace")
    return cursor

def _protected_reason(
    root: Path,
    target: Path,
    *,
    protected_roots: Sequence[Path] = (),
) -> Optional[str]:
    if _norm(target) == _norm(root):
        return "workspace_root"
    for protected in _environment_system_roots():
        if _is_within(target, protected):
            return "windows_system_path"
    if os.name == "nt" and is_drive_root(root):
        try:
            relative = target.relative_to(root)
        except ValueError:
            return "outside_workspace"
        first = relative.parts[0].casefold() if relative.parts else ""
        if first in _WINDOWS_PROTECTED_NAMES:
            return "windows_system_path"
        if len(relative.parts) == 1 and first in _WINDOWS_PROTECTED_FILES:
            return "windows_system_file"
    for protected in _karox_protected_roots():
        if _is_within(target, protected):
            return "karox_runtime"
        if _is_within(protected, target):
            return "karox_runtime_parent"
    for protected in protected_roots:
        with _suppress_os_errors():
            project = Path(protected).expanduser().resolve(strict=False)
            if _norm(target) == _norm(project):
                return "approved_project_root"
            # A parent directory is just as destructive as deleting the project
            # itself. Descendants intentionally remain allowed so rebuildable
            # node_modules/.venv/build/cache content can still be cleaned.
            if _is_within(project, target):
                return "approved_project_parent"
    if any(part.casefold() == ".git" for part in target.parts):
        return "git_metadata"
    return None
