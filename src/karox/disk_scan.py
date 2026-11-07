"""Disk scan: bounded candidate classification and repository markers."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from .disk_protection import (
    CleanupError,
    _norm,
    _is_within,
    is_drive_root,
    _suppress_os_errors,
    _is_reparse_boundary,
    _protected_reason,
)


_CANDIDATE_DIRECTORY_NAMES: dict[str, tuple[str, str]] = {
    "node_modules": ("project_dependencies", "Node.js"),
    ".venv": ("project_environment", "Python"),
    "venv": ("project_environment", "Python"),
    "__pycache__": ("project_cache", "Python"),
    ".pytest_cache": ("project_cache", "Python/pytest"),
    ".mypy_cache": ("project_cache", "Python/mypy"),
    ".ruff_cache": ("project_cache", "Python/Ruff"),
    ".next": ("build_artifacts", "Next.js"),
    "dist": ("build_artifacts", "project build"),
    "build": ("build_artifacts", "project build"),
    "target": ("build_artifacts", "Rust/Java build"),
    "cache": ("application_cache", "application"),
    "code cache": ("application_cache", "application"),
    "gpucache": ("application_cache", "application"),
    "npm-cache": ("package_cache", "npm"),
}

_APP_MARKERS: tuple[tuple[str, str], ...] = (
    ("google/chrome", "Chrome"),
    ("microsoft/edge", "Edge"),
    ("bravesoftware/brave-browser", "Brave"),
    ("mozilla/firefox", "Firefox"),
    ("discord", "Discord"),
    ("slack", "Slack"),
    ("spotify", "Spotify"),
    ("microsoft/vscode", "VS Code"),
    ("/code/", "VS Code"),
    ("npm-cache", "npm"),
    ("pnpm", "pnpm"),
    ("yarn", "Yarn"),
    ("pip/cache", "pip"),
    (".gradle/caches", "Gradle"),
    (".nuget/packages", "NuGet"),
)


@dataclass(frozen=True)
class TargetSnapshot:
    kind: str
    size_bytes: int
    file_count: int
    directory_count: int
    entry_count: int
    latest_mtime_ns: int
    fingerprint: str

@dataclass(frozen=True)
class CleanupTarget:
    path: str
    kind: str
    size_bytes: int
    file_count: int
    directory_count: int
    fingerprint: str
    category: str
    affected_apps: tuple[str, ...]
    code_project: str = ""
    data_risk: str = "rebuildable"

def _human_bytes(value: Any) -> str:
    try:
        size = max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        size = 0
    amount = float(size)
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    unit = units[0]
    for current in units:
        unit = current
        if amount < 1024.0 or current == units[-1]:
            break
        amount /= 1024.0
    return f"{int(amount)} {unit}" if unit == "Б" else f"{amount:.1f} {unit}"

_PROJECT_MARKERS = (
    ".git",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
)

_REBUILDABLE_CATEGORIES = frozenset(
    {
        "temporary_files",
        "application_cache",
        "package_cache",
        "project_dependencies",
        "project_environment",
        "project_cache",
        "build_artifacts",
    }
)

def _snapshot_target(path: Path, *, deadline: Optional[float] = None) -> TargetSnapshot:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("cleanup metadata scan reached its time budget")
    if _is_reparse_boundary(path):
        raise CleanupError(f"cleanup target is a symlink/junction: {path}")
    try:
        info = path.stat()
    except OSError as exc:
        raise CleanupError(f"cannot inspect cleanup target: {path}") from exc
    if path.is_file():
        # Windows mode bits are a projection and can vary without a content
        # change. Size + file mtime still catch ordinary edits while keeping the
        # metadata-only contract.
        payload = f"f\0{info.st_size}\0{info.st_mtime_ns}"
        return TargetSnapshot(
            kind="file",
            size_bytes=int(info.st_size),
            file_count=1,
            directory_count=0,
            entry_count=1,
            latest_mtime_ns=int(info.st_mtime_ns),
            fingerprint=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        )
    if not path.is_dir():
        raise CleanupError(f"cleanup target is not a regular file or directory: {path}")

    digest = hashlib.sha256()
    files = directories = entries = size = 0
    latest = int(info.st_mtime_ns)
    stack: list[tuple[Path, str]] = [(path, "")]
    while stack:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("cleanup metadata scan reached its time budget")
        current, prefix = stack.pop()
        try:
            children = list(os.scandir(current))
        except OSError as exc:
            raise CleanupError(f"cannot inspect directory contents: {current}") from exc
        children.sort(key=lambda item: item.name.casefold())
        for child in children:
            entries += 1
            if entries > MAX_SNAPSHOT_ENTRIES:
                raise CleanupError(
                    f"cleanup target contains more than {MAX_SNAPSHOT_ENTRIES} entries; "
                    "select narrower folders"
                )
            relative = f"{prefix}/{child.name}" if prefix else child.name
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise CleanupError(f"cannot inspect cleanup entry: {child.path}") from exc
            latest = max(latest, int(child_stat.st_mtime_ns))
            if _is_reparse_boundary(Path(child.path)):
                raise CleanupError(
                    f"cleanup target contains a symlink/junction: {child.path}"
                )
            if child.is_dir(follow_symlinks=False):
                directories += 1
                # Directory mtimes are not a content contract on Windows and can
                # change as a side effect of metadata/enumeration. Child names
                # still make add/remove/rename drift visible.
                digest.update(f"d\0{relative}\n".encode("utf-8", "surrogatepass"))
                stack.append((Path(child.path), relative))
                continue
            if child.is_file(follow_symlinks=False):
                files += 1
                size += int(child_stat.st_size)
                digest.update(
                    f"f\0{relative}\0{child_stat.st_size}\0{child_stat.st_mtime_ns}\n".encode(
                        "utf-8", "surrogatepass"
                    )
                )
                continue
            raise CleanupError(f"unsupported filesystem entry in cleanup target: {child.path}")
    return TargetSnapshot(
        kind="directory",
        size_bytes=size,
        file_count=files,
        directory_count=directories,
        entry_count=entries,
        latest_mtime_ns=latest,
        fingerprint=digest.hexdigest(),
    )

def _project_root(path: Path, root: Path) -> Optional[Path]:
    cursor = path if path.is_dir() else path.parent
    while _is_within(cursor, root):
        # A drive root is a maintenance scope, not a code project, even if an
        # unrelated project marker happens to exist at its top level.
        if _norm(cursor) == _norm(root) and is_drive_root(root):
            break
        for marker in _PROJECT_MARKERS:
            if (cursor / marker).exists():
                return cursor
        if _norm(cursor) == _norm(root):
            break
        cursor = cursor.parent
    return None

def _affected_apps(path: Path, category_app: str = "") -> tuple[str, ...]:
    normalized = "/" + str(path).replace("\\", "/").casefold() + "/"
    apps: list[str] = []
    if category_app and category_app not in {"application", "project build"}:
        apps.append(category_app)
    for marker, label in _APP_MARKERS:
        if marker.casefold() in normalized and label not in apps:
            apps.append(label)
    return tuple(apps[:8])

def _classify_target(path: Path, root: Path) -> tuple[str, tuple[str, ...], str, str]:
    name = path.name.casefold()
    category = "user_data"
    category_app = ""
    if name in _CANDIDATE_DIRECTORY_NAMES:
        category, category_app = _CANDIDATE_DIRECTORY_NAMES[name]
    normalized = str(path).replace("\\", "/").casefold()
    # A specific rebuildable category wins over the location of its parent.
    # A project living under %TEMP% still has ``node_modules`` dependencies,
    # not anonymous temporary user data. The known Temp directory itself is
    # still a cleanup candidate when a drive root is scanned.
    if category == "user_data":
        with _suppress_os_errors():
            temp_root = Path(tempfile.gettempdir()).expanduser().resolve(strict=False)
            if _is_within(path, temp_root):
                category = "temporary_files"
    if "pip/cache" in normalized:
        category, category_app = "package_cache", "pip"
    elif ".gradle/caches" in normalized:
        category, category_app = "package_cache", "Gradle"
    elif ".nuget/packages" in normalized:
        category, category_app = "package_cache", "NuGet"
    elif "pnpm" in normalized and ("store" in normalized or "cache" in normalized):
        category, category_app = "package_cache", "pnpm"
    elif "yarn" in normalized and "cache" in normalized:
        category, category_app = "package_cache", "Yarn"

    project = _project_root(path, root)
    code_project = ""
    if project is not None:
        try:
            code_project = project.relative_to(root).as_posix() or "."
        except ValueError:
            code_project = project.name
    if path.is_file() and path.suffix.casefold() in {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".rs",
        ".go",
        ".java",
        ".cs",
        ".cpp",
        ".c",
        ".h",
    }:
        category = "code_file"
    if project is not None and _norm(path) == _norm(project):
        category = "code_project"

    if category in _REBUILDABLE_CATEGORIES:
        data_risk = "rebuildable"
    elif category in {"code_file", "code_project"} or code_project:
        data_risk = "code"
    elif "appdata" in normalized:
        data_risk = "application_data"
    else:
        data_risk = "user_data"
    return category, _affected_apps(path, category_app), code_project, data_risk

def _candidate_payload(root: Path, path: Path, snapshot: TargetSnapshot) -> dict[str, Any]:
    category, apps, code_project, data_risk = _classify_target(path, root)
    return {
        "path": path.relative_to(root).as_posix(),
        "kind": snapshot.kind,
        "size_bytes": snapshot.size_bytes,
        "file_count": snapshot.file_count,
        "directory_count": snapshot.directory_count,
        "category": category,
        "affected_apps": list(apps),
        "code_project": code_project or None,
        "data_risk": data_risk,
    }

def _known_candidate_paths(root: Path) -> Iterator[Path]:
    seen: set[str] = set()

    def emit(value: str | Path) -> Iterator[Path]:
        path = Path(value).expanduser()
        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return
        if not path.is_dir() or not _is_within(path, root):
            return
        key = _norm(path)
        if key in seen:
            return
        seen.add(key)
        yield path

    for item in (tempfile.gettempdir(), os.environ.get("TEMP", ""), os.environ.get("TMP", "")):
        if item:
            yield from emit(item)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = Path(local)
        for relative in (
            "Temp",
            "npm-cache",
            "pip/Cache",
            "pnpm/store",
            "Yarn/Cache",
            "Google/Chrome/User Data/Default/Cache",
            "Google/Chrome/User Data/Default/Code Cache",
            "Microsoft/Edge/User Data/Default/Cache",
            "Microsoft/Edge/User Data/Default/Code Cache",
            "BraveSoftware/Brave-Browser/User Data/Default/Cache",
            "Discord/Cache",
            "Discord/Code Cache",
            "Discord/GPUCache",
        ):
            yield from emit(base / relative)
    home = Path.home()
    for relative in (".cache", ".gradle/caches", ".nuget/packages"):
        yield from emit(home / relative)

MAX_SCAN_CANDIDATES = 40

MAX_SCAN_DIRECTORIES = 30_000

MAX_SNAPSHOT_ENTRIES = 1_000_000

DEFAULT_SCAN_SECONDS = 8.0

DEFAULT_MIN_CANDIDATE_BYTES = 25 * 1024 * 1024


def scan_cleanup_candidates(
    root: str | Path,
    *,
    max_candidates: int = MAX_SCAN_CANDIDATES,
    min_size_mb: float = 25.0,
    scan_seconds: float = DEFAULT_SCAN_SECONDS,
    protected_roots: Sequence[str | Path] = (),
) -> dict[str, Any]:
    """Return bounded metadata-only cleanup candidates under ``root``."""

    workspace = Path(root).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise CleanupError("cleanup workspace is not a directory")
    if not isinstance(max_candidates, int) or isinstance(max_candidates, bool):
        raise CleanupError("max_candidates must be an integer")
    max_candidates = max(1, min(max_candidates, MAX_SCAN_CANDIDATES))
    try:
        min_bytes = max(0, int(float(min_size_mb) * 1024 * 1024))
        seconds = min(max(float(scan_seconds), 0.5), 30.0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CleanupError("scan limits must be finite numbers") from exc
    deadline = time.monotonic() + seconds
    protected = tuple(Path(item).expanduser().resolve(strict=False) for item in protected_roots)
    found: dict[str, dict[str, Any]] = {}
    truncated = False

    def consider(path: Path) -> None:
        nonlocal truncated
        if len(found) >= max_candidates * 3 or time.monotonic() >= deadline:
            truncated = True
            return
        reason = _protected_reason(workspace, path, protected_roots=protected)
        if reason is not None:
            return
        key = _norm(path)
        if key in found:
            return
        try:
            snapshot = _snapshot_target(path, deadline=deadline)
        except TimeoutError:
            truncated = True
            return
        except CleanupError:
            return
        if snapshot.size_bytes < min_bytes:
            return
        found[key] = _candidate_payload(workspace, path, snapshot)

    for known in _known_candidate_paths(workspace):
        consider(known)
        if time.monotonic() >= deadline:
            truncated = True
            break

    visited = 0
    if time.monotonic() < deadline:
        try:
            walker = os.walk(workspace, topdown=True, followlinks=False)
            for current_raw, dirs, _files in walker:
                visited += 1
                if visited > MAX_SCAN_DIRECTORIES or time.monotonic() >= deadline:
                    truncated = True
                    break
                current = Path(current_raw)
                kept: list[str] = []
                for dirname in dirs:
                    child = current / dirname
                    if _is_reparse_boundary(child):
                        continue
                    protected_reason = _protected_reason(
                        workspace, child, protected_roots=protected
                    )
                    if protected_reason is not None:
                        # A saved project root is protected from being proposed as
                        # one giant deletion, but walking into it is desirable:
                        # its node_modules/.venv/build caches are often the safest
                        # and largest cleanup candidates on a developer machine.
                        if protected_reason in {
                            "approved_project_root",
                            "approved_project_parent",
                            "karox_runtime_parent",
                        }:
                            kept.append(dirname)
                        continue
                    if dirname.casefold() in _CANDIDATE_DIRECTORY_NAMES:
                        consider(child)
                        # The candidate is already measured recursively; walking
                        # into it would count thousands of dependency/cache dirs
                        # a second time and waste the scan budget.
                        continue
                    kept.append(dirname)
                dirs[:] = kept
        except OSError:
            truncated = True

    candidates = sorted(
        found.values(), key=lambda item: int(item.get("size_bytes") or 0), reverse=True
    )[:max_candidates]
    usage = shutil.disk_usage(workspace)
    return {
        "root": str(workspace),
        "drive_root": is_drive_root(workspace),
        "disk_total_bytes": int(usage.total),
        "disk_free_bytes": int(usage.free),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "scan_truncated": truncated or len(found) > len(candidates),
        "metadata_only": True,
        "content_read": False,
    }

