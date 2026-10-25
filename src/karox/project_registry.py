"""User-approved multi-project registry for durable hosted connections.

A saved web bridge may expose more than one local repository, but only paths
that the user explicitly added to its profile belong to this registry. Runtime
tool calls resolve a project ID through this object instead of accepting an
arbitrary filesystem root from the model.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

MAX_PROJECT_ID_LENGTH = 64
_PROJECT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class ProjectRegistryError(ValueError):
    """A project allowlist entry or lookup is invalid."""


def validate_project_id(project_id: str) -> str:
    if not isinstance(project_id, str):
        raise ProjectRegistryError("project_id must be a string")
    value = project_id.strip()
    if not _PROJECT_ID_RE.fullmatch(value) or ".." in value:
        raise ProjectRegistryError(
            "project_id must be a safe 1..64 character identifier"
        )
    return value


def _canonical_directory(path: str | Path) -> Path:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectRegistryError(f"project path does not exist: {path}") from exc
    if not resolved.is_dir():
        raise ProjectRegistryError(f"project path is not a directory: {resolved}")
    return resolved


def _path_key(path: str | Path) -> str:
    resolved = _canonical_directory(path)
    return os.path.normcase(str(resolved))


def _generated_project_id(path: Path) -> str:
    raw = path.name or "project"
    ascii_slug = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    if not ascii_slug:
        ascii_slug = "project"
    ascii_slug = ascii_slug[:48].rstrip("-._") or "project"
    digest = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()[:8]
    return validate_project_id(f"{ascii_slug}-{digest}")


@dataclass(frozen=True)
class ProjectEntry:
    project_id: str
    path: str
    label: str = ""

    def __post_init__(self) -> None:
        project_id = validate_project_id(self.project_id)
        path = _canonical_directory(self.path)
        label = str(self.label or path.name or project_id).strip()
        if not label or len(label) > 128 or any(ord(ch) < 32 for ch in label):
            raise ProjectRegistryError("project label must be 1..128 printable characters")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "path", str(path))
        object.__setattr__(self, "label", label)

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | "ProjectEntry") -> "ProjectEntry":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ProjectRegistryError("project entry must be an object")
        unknown = set(value).difference({"project_id", "path", "label"})
        if unknown:
            raise ProjectRegistryError(
                "project entry contains unknown fields: " + ", ".join(sorted(unknown))
            )
        try:
            return cls(
                project_id=str(value["project_id"]),
                path=str(value["path"]),
                label=str(value.get("label", "")),
            )
        except KeyError as exc:
            raise ProjectRegistryError("project entry requires project_id and path") from exc

    def to_dict(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "path": self.path,
            "label": self.label,
        }


@dataclass(frozen=True)
class ProjectRegistry:
    projects: tuple[ProjectEntry, ...] = ()
    default_project_id: Optional[str] = None

    def __post_init__(self) -> None:
        entries = tuple(ProjectEntry.from_value(item) for item in self.projects)
        ids: set[str] = set()
        paths: set[str] = set()
        for entry in entries:
            if entry.project_id in ids:
                raise ProjectRegistryError(f"duplicate project_id: {entry.project_id}")
            key = _path_key(entry.path)
            if key in paths:
                raise ProjectRegistryError("duplicate canonical project path")
            ids.add(entry.project_id)
            paths.add(key)
        default = self.default_project_id
        if entries:
            default = validate_project_id(default or entries[0].project_id)
            if default not in ids:
                raise ProjectRegistryError("default_project_id is not in the project registry")
        elif default is not None:
            raise ProjectRegistryError("an empty project registry cannot have a default project")
        object.__setattr__(self, "projects", entries)
        object.__setattr__(self, "default_project_id", default)

    @classmethod
    def from_profile(
        cls,
        *,
        repository: Optional[str] = None,
        projects: Iterable[Mapping[str, Any] | ProjectEntry] = (),
        default_project_id: Optional[str] = None,
    ) -> "ProjectRegistry":
        entries = [ProjectEntry.from_value(item) for item in projects]
        legacy_path: Optional[Path] = None
        if repository is not None and str(repository).strip():
            legacy_path = _canonical_directory(str(repository).strip())
            legacy_key = os.path.normcase(str(legacy_path))
            matching = next(
                (
                    item
                    for item in entries
                    if os.path.normcase(str(_canonical_directory(item.path))) == legacy_key
                ),
                None,
            )
            if matching is None:
                matching = ProjectEntry(
                    project_id=_generated_project_id(legacy_path),
                    path=str(legacy_path),
                    label=legacy_path.name or "Project",
                )
                entries.insert(0, matching)
            # A legacy profile had no separate default and therefore migrates to
            # its repository. Once a registry/default exists, though, repository
            # remains the durable session anchor while default_project_id may be
            # changed without rebinding or restarting that saved connection.
            if default_project_id is None:
                default_project_id = matching.project_id
        if default_project_id is None and entries:
            default_project_id = entries[0].project_id
        return cls(tuple(entries), default_project_id)

    @classmethod
    def single(cls, repository: str | Path) -> "ProjectRegistry":
        path = _canonical_directory(repository)
        entry = ProjectEntry(
            project_id=_generated_project_id(path),
            path=str(path),
            label=path.name or "Project",
        )
        return cls((entry,), entry.project_id)

    def to_payload(self) -> list[dict[str, str]]:
        return [entry.to_dict() for entry in self.projects]

    @property
    def default(self) -> Optional[ProjectEntry]:
        if self.default_project_id is None:
            return None
        return self.get(self.default_project_id)

    def get(self, project_id: str) -> ProjectEntry:
        wanted = validate_project_id(project_id)
        for entry in self.projects:
            if entry.project_id == wanted:
                return entry
        raise ProjectRegistryError(f"project is not approved: {wanted}")

    def resolve(self, project_id: Optional[str] = None) -> Path:
        selected = project_id or self.default_project_id
        if selected is None:
            raise ProjectRegistryError("no project is configured")
        entry = self.get(selected)
        current = _canonical_directory(entry.path)
        if os.path.normcase(str(current)) != os.path.normcase(entry.path):
            raise ProjectRegistryError("approved project path no longer resolves to its saved target")
        return current

    def entry_for_path(self, path: str | Path) -> Optional[ProjectEntry]:
        key = _path_key(path)
        for entry in self.projects:
            if _path_key(entry.path) == key:
                return entry
        return None

    def add(
        self,
        path: str | Path,
        *,
        project_id: Optional[str] = None,
        label: Optional[str] = None,
        make_default: bool = False,
    ) -> "ProjectRegistry":
        resolved = _canonical_directory(path)
        if self.entry_for_path(resolved) is not None:
            raise ProjectRegistryError("duplicate canonical project path")
        identifier = validate_project_id(project_id) if project_id else _generated_project_id(resolved)
        if any(item.project_id == identifier for item in self.projects):
            raise ProjectRegistryError(f"duplicate project_id: {identifier}")
        entry = ProjectEntry(
            project_id=identifier,
            path=str(resolved),
            label=label or resolved.name or identifier,
        )
        default = identifier if make_default or not self.projects else self.default_project_id
        return ProjectRegistry((*self.projects, entry), default)

    def remove(self, project_id: str) -> "ProjectRegistry":
        wanted = validate_project_id(project_id)
        remaining = tuple(item for item in self.projects if item.project_id != wanted)
        if len(remaining) == len(self.projects):
            raise ProjectRegistryError(f"project is not approved: {wanted}")
        if not remaining:
            return ProjectRegistry()
        default = self.default_project_id
        if default == wanted:
            default = remaining[0].project_id
        return ProjectRegistry(remaining, default)

    def with_default(self, project_id: str) -> "ProjectRegistry":
        wanted = self.get(project_id).project_id
        return ProjectRegistry(self.projects, wanted)
