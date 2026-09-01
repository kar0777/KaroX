"""Safe, bounded disk-cleanup planning with a user-owned apply boundary.

The model is deliberately not given a delete primitive.  It may inspect compact
metadata with :func:`scan_cleanup_candidates` and freeze an exact plan with
:func:`create_cleanup_plan`.  Only the human-facing TUI/CLI confirmation path
calls :func:`apply_cleanup_plan` afterwards.

That split is load-bearing:

* a model cannot manufacture ``confirmed=True`` in a tool call because apply is
  not a model tool;
* the plan records an exact metadata fingerprint and expires quickly, so a path
  that changed after the user reviewed it is refused rather than deleted;
* Windows/system/KaroX runtime locations are blocked even when the selected
  workspace is a whole drive such as ``C:\\``;
* scan results contain names, sizes, categories and impact labels, never file
  contents, which keeps both privacy exposure and token use bounded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

from .paths import runtime_dir

PLAN_SCHEMA_VERSION = 1
PLAN_TTL_SECONDS = 20 * 60
MAX_PLAN_TARGETS = 32
MAX_SCAN_CANDIDATES = 40
MAX_SCAN_DIRECTORIES = 30_000
MAX_SNAPSHOT_ENTRIES = 1_000_000
DEFAULT_SCAN_SECONDS = 8.0
DEFAULT_MIN_CANDIDATE_BYTES = 25 * 1024 * 1024
_PLAN_ID = re.compile(r"[0-9a-f]{32}\Z")

# Whole OS/application roots are never cleanup targets.  User-local application
# caches (for example Chrome's Cache under AppData) are intentionally *not* in
# this list and are handled by the impact classifier instead.
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


class CleanupError(RuntimeError):
    """A cleanup scan/plan/apply boundary refused an unsafe or stale request."""


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


@dataclass(frozen=True)
class CleanupPlan:
    schema_version: int
    plan_id: str
    root: str
    created_at: float
    expires_at: float
    targets: tuple[CleanupTarget, ...]
    total_bytes: int
    file_count: int
    directory_count: int
    affected_apps: tuple[str, ...]
    code_projects: tuple[str, ...]
    has_user_data: bool
    irreversible: bool = True
    confirmation_required: bool = True

    def to_dict(self, *, public: bool = False) -> dict[str, Any]:
        payload = asdict(self)
        payload["targets"] = [asdict(item) for item in self.targets]
        if public:
            # Summarize the *whole* frozen set before truncating target details;
            # otherwise a risky ninth target could disappear from the warning.
            risks = {item.data_risk for item in self.targets}
            payload["risk_level"] = (
                "personal_or_code"
                if risks.intersection({"user_data", "code"})
                else "application_data"
                if "application_data" in risks
                else "rebuildable"
            )
            payload["target_count"] = len(self.targets)
            # Fingerprints are useful only to the apply boundary. Keeping them
            # out of model-facing output removes noise without weakening safety.
            for item in payload["targets"]:
                item.pop("fingerprint", None)
            payload["targets"] = payload["targets"][:8]
            payload["targets_truncated"] = len(self.targets) > 8
        return payload


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


def cleanup_plan_risk(plan: Mapping[str, Any]) -> str:
    """Strongest user-facing data class in a public cleanup plan."""

    declared = str(plan.get("risk_level") or "")
    if declared in {"rebuildable", "application_data", "personal_or_code"}:
        return declared
    raw_targets = plan.get("targets")
    if not isinstance(raw_targets, Sequence) or isinstance(raw_targets, (str, bytes)):
        return "personal_or_code"
    risks = {
        str(item.get("data_risk") or "user_data")
        for item in raw_targets
        if isinstance(item, Mapping)
    }
    if "user_data" in risks or "code" in risks:
        return "personal_or_code"
    if "application_data" in risks:
        return "application_data"
    return "rebuildable"


def cleanup_impact_preview(plan: Mapping[str, Any], *, language: str = "ru") -> str:
    """Render the frozen deletion impact in four short, model-free lines."""

    english = language == "en"
    raw_targets = plan.get("targets")
    try:
        target_count = max(0, int(plan.get("target_count") or 0))
    except (TypeError, ValueError, OverflowError):
        target_count = 0
    if not target_count:
        target_count = (
            len(raw_targets)
            if isinstance(raw_targets, Sequence) and not isinstance(raw_targets, (str, bytes))
            else 0
        )
    try:
        file_count = max(0, int(plan.get("file_count") or 0))
    except (TypeError, ValueError, OverflowError):
        file_count = 0
    apps = [str(item) for item in (plan.get("affected_apps") or ()) if str(item).strip()]
    projects = [str(item) for item in (plan.get("code_projects") or ()) if str(item).strip()]
    risk = cleanup_plan_risk(plan)

    lines = [
        (
            f"Delete: {_human_bytes(plan.get('total_bytes'))} · {file_count} files · {target_count} targets"
            if english
            else f"Удалится: {_human_bytes(plan.get('total_bytes'))} · {file_count} файлов · {target_count} целей"
        )
    ]
    affected: list[str] = []
    if apps:
        affected.append(("apps: " if english else "приложения: ") + ", ".join(apps[:5]))
    if projects:
        affected.append(("projects: " if english else "проекты: ") + ", ".join(projects[:4]))
    if affected:
        lines.append(("Affects: " if english else "Затронет: ") + " · ".join(affected))
    risk_words = {
        "rebuildable": (
            "Risk: rebuildable caches/dependencies",
            "Риск: восстанавливаемые кэши/зависимости",
        ),
        "application_data": (
            "Risk: application data; local app state may reset",
            "Риск: данные приложений; локальное состояние может сброситься",
        ),
        "personal_or_code": (
            "Risk: code or personal data",
            "Риск: код или пользовательские данные",
        ),
    }[risk]
    lines.append(risk_words[0 if english else 1])
    lines.append("Permanent. Continue?" if english else "Необратимо. Продолжить?")
    return "\n".join(lines)


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


def _plans_dir() -> Path:
    path = runtime_dir() / "cleanup_plans"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _plan_path(plan_id: str) -> Path:
    if not isinstance(plan_id, str) or _PLAN_ID.fullmatch(plan_id) is None:
        raise CleanupError("invalid cleanup plan id")
    return _plans_dir() / f"{plan_id}.json"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def create_cleanup_plan(
    root: str | Path,
    paths: Sequence[str],
    *,
    protected_roots: Sequence[str | Path] = (),
    ttl_seconds: float = PLAN_TTL_SECONDS,
) -> dict[str, Any]:
    """Freeze exact deletion metadata; this function never deletes anything."""

    workspace = Path(root).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise CleanupError("cleanup workspace is not a directory")
    if not isinstance(paths, Sequence) or isinstance(paths, (str, bytes)) or not paths:
        raise CleanupError("cleanup plan requires at least one target")
    if len(paths) > MAX_PLAN_TARGETS:
        raise CleanupError(f"cleanup plan supports at most {MAX_PLAN_TARGETS} targets")
    protected = tuple(Path(item).expanduser().resolve(strict=False) for item in protected_roots)
    deadline = time.monotonic() + 30.0
    targets: list[CleanupTarget] = []
    seen: set[str] = set()
    for raw in paths:
        target = _validate_relative_target(workspace, str(raw))
        key = _norm(target)
        if key in seen:
            continue
        seen.add(key)
        reason = _protected_reason(workspace, target, protected_roots=protected)
        if reason is not None:
            raise CleanupError(f"cleanup target is protected ({reason}): {raw}")
        snapshot = _snapshot_target(target, deadline=deadline)
        category, apps, code_project, data_risk = _classify_target(target, workspace)
        targets.append(
            CleanupTarget(
                path=target.relative_to(workspace).as_posix(),
                kind=snapshot.kind,
                size_bytes=snapshot.size_bytes,
                file_count=snapshot.file_count,
                directory_count=snapshot.directory_count,
                fingerprint=snapshot.fingerprint,
                category=category,
                affected_apps=apps,
                code_project=code_project,
                data_risk=data_risk,
            )
        )
    if not targets:
        raise CleanupError("cleanup plan contains no distinct targets")
    # A parent target already includes a child target. Refusing overlap keeps the
    # action digest obvious and avoids a plan that succeeds or fails depending on
    # deletion order.
    absolute_targets = [workspace / item.path for item in targets]
    for index, left in enumerate(absolute_targets):
        for right in absolute_targets[index + 1 :]:
            if _is_within(left, right) or _is_within(right, left):
                raise CleanupError("cleanup plan contains overlapping targets")

    now = time.time()
    try:
        ttl = min(max(float(ttl_seconds), 60.0), PLAN_TTL_SECONDS)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CleanupError("cleanup plan TTL must be a number") from exc
    apps = tuple(dict.fromkeys(app for item in targets for app in item.affected_apps))
    projects = tuple(dict.fromkeys(item.code_project for item in targets if item.code_project))
    plan = CleanupPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        plan_id=uuid.uuid4().hex,
        root=str(workspace),
        created_at=now,
        expires_at=now + ttl,
        targets=tuple(targets),
        total_bytes=sum(item.size_bytes for item in targets),
        file_count=sum(item.file_count for item in targets),
        directory_count=sum(item.directory_count for item in targets),
        affected_apps=apps,
        code_projects=projects,
        has_user_data=any(item.data_risk in {"user_data", "application_data"} for item in targets),
    )
    _atomic_json(_plan_path(plan.plan_id), plan.to_dict(public=False))
    return plan.to_dict(public=True)


def _plan_from_payload(payload: dict[str, Any]) -> CleanupPlan:
    if int(payload.get("schema_version", -1)) != PLAN_SCHEMA_VERSION:
        raise CleanupError("unsupported cleanup plan version")
    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list):
        raise CleanupError("cleanup plan targets are invalid")
    try:
        targets = tuple(CleanupTarget(**dict(item)) for item in raw_targets if isinstance(item, dict))
        return CleanupPlan(
            schema_version=PLAN_SCHEMA_VERSION,
            plan_id=str(payload["plan_id"]),
            root=str(payload["root"]),
            created_at=float(payload["created_at"]),
            expires_at=float(payload["expires_at"]),
            targets=targets,
            total_bytes=int(payload["total_bytes"]),
            file_count=int(payload["file_count"]),
            directory_count=int(payload["directory_count"]),
            affected_apps=tuple(str(item) for item in payload.get("affected_apps", ())),
            code_projects=tuple(str(item) for item in payload.get("code_projects", ())),
            has_user_data=bool(payload.get("has_user_data")),
            irreversible=bool(payload.get("irreversible", True)),
            confirmation_required=bool(payload.get("confirmation_required", True)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CleanupError("cleanup plan is malformed") from exc


def load_cleanup_plan(plan_id: str, *, public: bool = False) -> dict[str, Any]:
    path = _plan_path(plan_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CleanupError("cleanup plan does not exist or already completed") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CleanupError("cleanup plan cannot be read") from exc
    if not isinstance(payload, dict):
        raise CleanupError("cleanup plan is malformed")
    return _plan_from_payload(payload).to_dict(public=public)


def _remove_tree(path: Path) -> None:
    def make_writable(func: Any, target: str, exc: BaseException) -> None:
        del exc
        with _suppress_os_errors():
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        func(target)

    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    # Python 3.13 supports onexc; onerror remains for compatibility with older
    # runtimes used by downstream packagers. Resolve dynamically because older
    # typeshed versions do not yet expose the 3.13 keyword even when the runtime
    # does, and diagnostics should not fail solely on a typing-version mismatch.
    rmtree: Any = shutil.rmtree
    try:
        rmtree(path, onexc=make_writable)
    except TypeError:  # pragma: no cover - compatibility path
        def onerror(func: Any, target: str, info: Any) -> None:
            make_writable(func, target, info[1])
        rmtree(path, onerror=onerror)


def _append_audit(payload: dict[str, Any]) -> None:
    path = runtime_dir() / "cleanup_audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")


def apply_cleanup_plan(
    plan_id: str,
    *,
    root: str | Path,
    confirmed_by_user: bool,
    protected_roots: Sequence[str | Path] = (),
) -> dict[str, Any]:
    """Apply one frozen plan after an explicit human-facing confirmation."""

    if confirmed_by_user is not True:
        raise CleanupError("cleanup apply requires explicit user confirmation")
    stored = load_cleanup_plan(plan_id, public=False)
    plan = _plan_from_payload(stored)
    workspace = Path(root).expanduser().resolve(strict=True)
    if _norm(workspace) != _norm(Path(plan.root)):
        raise CleanupError("cleanup plan belongs to a different workspace")
    if time.time() > plan.expires_at:
        with _suppress_os_errors():
            _plan_path(plan.plan_id).unlink()
        raise CleanupError("cleanup plan expired; scan and review again")
    protected = tuple(Path(item).expanduser().resolve(strict=False) for item in protected_roots)

    # Revalidate every target *before* moving the first one. This is the safety
    # property the user reviewed: same target set, same metadata fingerprint.
    validated: list[tuple[CleanupTarget, Path]] = []
    for item in plan.targets:
        target = _validate_relative_target(workspace, item.path)
        reason = _protected_reason(workspace, target, protected_roots=protected)
        if reason is not None:
            raise CleanupError(f"cleanup target became protected ({reason}): {item.path}")
        snapshot = _snapshot_target(target)
        if snapshot.fingerprint != item.fingerprint:
            raise CleanupError(f"cleanup target changed after preview: {item.path}")
        validated.append((item, target))

    free_before = int(shutil.disk_usage(workspace).free)
    staging_root = workspace / ".karox-cleanup-staging" / plan.plan_id
    if staging_root.exists():
        raise CleanupError("cleanup staging path already exists")
    staging_root.mkdir(parents=True, exist_ok=False)
    moved: list[tuple[Path, Path]] = []
    try:
        for index, (item, target) in enumerate(validated):
            destination = staging_root / f"{index:03d}-{target.name}"
            os.replace(target, destination)
            moved.append((target, destination))
    except Exception as exc:
        rollback_errors: list[str] = []
        for original, staged in reversed(moved):
            try:
                os.replace(staged, original)
            except OSError as rollback_exc:
                rollback_errors.append(f"{original}: {type(rollback_exc).__name__}")
        with _suppress_os_errors():
            staging_root.rmdir()
            staging_root.parent.rmdir()
        detail = ""
        if rollback_errors:
            detail = "; rollback incomplete: " + ", ".join(rollback_errors[:5])
        raise CleanupError("could not stage every target for deletion" + detail) from exc

    removed = 0
    cleanup_failures: list[str] = []
    for _original, staged in moved:
        try:
            _remove_tree(staged)
            removed += 1
        except Exception as exc:
            cleanup_failures.append(f"{staged.name}: {type(exc).__name__}")
    with _suppress_os_errors():
        staging_root.rmdir()
        staging_root.parent.rmdir()
    free_after = int(shutil.disk_usage(workspace).free)

    status = "completed" if not cleanup_failures else "partial"
    audit = {
        "timestamp": time.time(),
        "plan_id": plan.plan_id,
        "root": str(workspace),
        "status": status,
        "target_count": len(plan.targets),
        "removed_target_count": removed,
        "planned_bytes": plan.total_bytes,
        "free_space_delta_bytes": free_after - free_before,
        "affected_apps": list(plan.affected_apps),
        "code_project_count": len(plan.code_projects),
        "has_user_data": plan.has_user_data,
        "failures": cleanup_failures[:10],
    }
    _append_audit(audit)
    with _suppress_os_errors():
        _plan_path(plan.plan_id).unlink()
    return {
        **audit,
        "deleted_paths": [item.path for item in plan.targets[:8]],
        "deleted_paths_truncated": len(plan.targets) > 8,
        "quarantine_remaining": bool(cleanup_failures),
    }


__all__ = [
    "CleanupError",
    "CleanupPlan",
    "CleanupTarget",
    "DEFAULT_MIN_CANDIDATE_BYTES",
    "MAX_PLAN_TARGETS",
    "MAX_SCAN_CANDIDATES",
    "PLAN_TTL_SECONDS",
    "apply_cleanup_plan",
    "cleanup_impact_preview",
    "cleanup_plan_risk",
    "create_cleanup_plan",
    "is_drive_root",
    "load_cleanup_plan",
    "scan_cleanup_candidates",
    "workspace_system_reason",
]
