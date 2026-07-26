"""Secure metadata discovery and lazy loading for untrusted Skills."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Mapping, Optional, Sequence

import yaml

from .models import Capability, Origin, OriginKind
from .paths import config_dir
from .policy import CapabilityPolicy


class SkillError(RuntimeError):
    """Raised when Skill metadata or content fails closed validation."""


class SkillPermission(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_VERSION = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_DECLARATIVE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$")
_KNOWN_AGENT_TOOLS = frozenset(
    {
        "repo_read_file",
        "repo_write_file",
        "repo_list_files",
        "checks_run",
        "git_status",
        "git_diff",
    }
)


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    description: str
    version: str
    author: Optional[str]
    tags: tuple[str, ...]
    compatible_project_types: tuple[str, ...]
    required_tools: tuple[str, ...]
    optional_mcp: tuple[str, ...]
    requested_capabilities: tuple[Capability, ...]
    files: tuple[str, ...]
    validation_rules: tuple[str, ...]
    source_kind: str
    source_root: Path
    directory: Path
    manifest_path: Path
    metadata_sha256: str
    content_sha256: Optional[str]
    _manifest_identity: _FileIdentity

    @property
    def identity(self) -> str:
        return f"{self.name}@{self.version}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "identity": self.identity,
            "author": self.author,
            "tags": list(self.tags),
            "compatible_project_types": list(self.compatible_project_types),
            "required_tools": list(self.required_tools),
            "optional_mcp": list(self.optional_mcp),
            "permissions": [item.value for item in self.requested_capabilities],
            "files": list(self.files),
            "validation_rules": list(self.validation_rules),
            "source_kind": self.source_kind,
            "source_root": str(self.source_root),
            "directory": str(self.directory),
            "manifest_path": str(self.manifest_path),
            "metadata_sha256": self.metadata_sha256,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class SkillContent:
    metadata: SkillMetadata
    instructions: str
    references: Mapping[str, str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metadata": self.metadata.to_dict(),
            "instructions": self.instructions,
            "references": dict(self.references),
        }


@dataclass(frozen=True)
class SkillDiagnostic:
    status: str
    source_kind: str
    path: str
    reason: str
    name: Optional[str] = None
    selected_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "status": self.status,
            "source_kind": self.source_kind,
            "path": self.path,
            "reason": self.reason,
        }
        if self.name is not None:
            value["name"] = self.name
        if self.selected_path is not None:
            value["selected_path"] = self.selected_path
        return value


class _StrictSafeLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise SkillError("YAML aliases are not allowed in Skill metadata")
        return super().compose_node(parent, index)


def _construct_mapping(
    loader: _StrictSafeLoader, node: yaml.MappingNode, deep: bool = False
) -> Dict[Any, Any]:
    loader.flatten_mapping(node)
    result: Dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise SkillError("Skill metadata mapping keys must be scalar") from exc
        if duplicate:
            raise SkillError(f"duplicate Skill metadata key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _is_link_or_reparse(path: Path) -> bool:
    metadata = path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _identity_from_stat(metadata: os.stat_result) -> _FileIdentity:
    # On Windows a path stat taken immediately after creation can expose a
    # stale creation/change timestamp while fstat already reports the final
    # value.  The remaining fields plus mtime are stable across both views;
    # POSIX keeps ctime as an additional replacement/change signal.
    changed_ns = int(metadata.st_ctime_ns) if os.name != "nt" else 0
    return _FileIdentity(
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        mode=int(metadata.st_mode),
        size=int(metadata.st_size),
        modified_ns=int(metadata.st_mtime_ns),
        changed_ns=changed_ns,
    )


def _identity(path: Path) -> _FileIdentity:
    return _identity_from_stat(path.lstat())


def _validate_name(value: Any) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise SkillError(
            "Skill name must be a lowercase safe identifier using letters, "
            "digits, '-' or '_'"
        )
    if len(value) > 100:
        raise SkillError("Skill name exceeds 100 characters")
    return value


def _validate_version(value: Any) -> str:
    if value is None:
        return "0.0.0"
    if not isinstance(value, str) or len(value) > 100:
        raise SkillError("Skill version must be a valid semantic version")
    match = _VERSION.fullmatch(value)
    if match is None:
        raise SkillError("Skill version must be a valid semantic version")
    prerelease = match.group(4)
    if prerelease is not None and any(
        item.isdigit() and len(item) > 1 and item.startswith("0")
        for item in prerelease.split(".")
    ):
        raise SkillError("Skill version must be a valid semantic version")
    return value


def _metadata_value(
    value: Mapping[str, Any],
    nested: Mapping[str, Any],
    canonical: str,
    *aliases: str,
) -> Any:
    candidates: list[tuple[str, Any]] = []
    for key in (canonical, *aliases):
        if key in value and value[key] is not None:
            candidates.append((key, value[key]))
        if key in nested and nested[key] is not None:
            candidates.append((f"metadata.{key}", nested[key]))
    if not candidates:
        return None
    selected = candidates[0][1]
    if any(candidate != selected for _, candidate in candidates[1:]):
        fields = ", ".join(name for name, _ in candidates)
        raise SkillError(f"conflicting Skill {canonical} fields: {fields}")
    return selected


def _validate_optional_text(value: Any, label: str, limit: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SkillError(f"Skill {label} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > limit or any(
        ord(character) < 32 for character in normalized
    ):
        raise SkillError(f"Skill {label} must contain 1-{limit} printable characters")
    return normalized


def _validate_string_list(
    value: Any,
    label: str,
    *,
    max_items: int,
    max_length: int,
    identifiers: bool = False,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SkillError(f"Skill {label} must be a list")
    if len(value) > max_items:
        raise SkillError(f"Skill {label} declares more than {max_items} entries")
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise SkillError(f"Skill {label} entries must be strings")
        item = raw.strip()
        if (
            not item
            or len(item) > max_length
            or any(ord(character) < 32 for character in item)
            or (identifiers and _DECLARATIVE_IDENTIFIER.fullmatch(item) is None)
        ):
            qualifier = " safe identifier" if identifiers else " printable"
            raise SkillError(
                f"Skill {label} entries must contain 1-{max_length}{qualifier} characters"
            )
        if item in result:
            raise SkillError(f"duplicate Skill {label} entry: {item}")
        result.append(item)
    return tuple(result)


def _validate_reference(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 300:
        raise SkillError("Skill file references must contain 1-300 characters")
    if "\\" in value or "\x00" in value:
        raise SkillError(f"unsafe Skill file reference: {value!r}")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise SkillError(f"unsafe Skill file reference: {value!r}")
    normalized = posix.as_posix()
    if normalized == "SKILL.md":
        raise SkillError("SKILL.md cannot reference itself")
    return normalized


def _bounded_frontmatter(path: Path, limit: int) -> tuple[bytes, int, _FileIdentity]:
    before = _identity(path)
    if not stat.S_ISREG(before.mode):
        raise SkillError(f"Skill manifest is not a regular file: {path}")
    if _is_link_or_reparse(path):
        raise SkillError(f"Skill manifest cannot be a link or reparse point: {path}")
    consumed = 0
    pieces: list[bytes] = []
    try:
        with path.open("rb") as handle:
            opened = _identity_from_stat(os.fstat(handle.fileno()))
            if opened != before:
                raise SkillError(f"Skill manifest changed before it was read: {path}")
            opening = handle.readline(limit + 1)
            consumed += len(opening)
            if len(opening) > limit or opening.rstrip(b"\r\n") != b"---":
                raise SkillError("SKILL.md must begin with YAML frontmatter")
            pieces.append(opening)
            while True:
                remaining = limit - consumed
                if remaining < 0:
                    raise SkillError("Skill metadata exceeds the size limit")
                line = handle.readline(remaining + 1)
                if not line:
                    raise SkillError("Skill metadata has no closing delimiter")
                consumed += len(line)
                if consumed > limit:
                    raise SkillError("Skill metadata exceeds the size limit")
                pieces.append(line)
                if line.rstrip(b"\r\n") in {b"---", b"..."}:
                    offset = handle.tell()
                    break
    except OSError as exc:
        raise SkillError(f"cannot read Skill metadata {path}: {exc}") from exc
    after = _identity(path)
    if before != after:
        raise SkillError(f"Skill manifest changed while metadata was read: {path}")
    return b"".join(pieces), offset, after


def _frontmatter_payload(prefix: bytes) -> bytes:
    lines = prefix.splitlines(keepends=True)
    if len(lines) < 2:
        raise SkillError("Skill metadata is incomplete")
    return b"".join(lines[1:-1])


def _parse_yaml(prefix: bytes) -> Dict[str, Any]:
    try:
        text = _frontmatter_payload(prefix).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SkillError("Skill metadata must be valid UTF-8") from exc
    try:
        value = yaml.load(text, Loader=_StrictSafeLoader)
    except SkillError:
        raise
    except yaml.YAMLError as exc:
        raise SkillError(f"invalid Skill YAML metadata: {exc}") from exc
    if not isinstance(value, dict):
        raise SkillError("Skill metadata must be a YAML mapping")
    if any(not isinstance(key, str) for key in value):
        raise SkillError("Skill metadata keys must be strings")
    return value


def _read_bounded(path: Path, limit: int) -> tuple[bytes, _FileIdentity]:
    before = _identity(path)
    if not stat.S_ISREG(before.mode) or _is_link_or_reparse(path):
        raise SkillError(f"Skill content must be a regular non-link file: {path}")
    if before.size > limit:
        raise SkillError(f"Skill file exceeds the {limit}-byte limit: {path}")
    try:
        with path.open("rb") as handle:
            opened = _identity_from_stat(os.fstat(handle.fileno()))
            if opened != before:
                raise SkillError(f"Skill file changed before it was read: {path}")
            value = handle.read(limit + 1)
    except OSError as exc:
        raise SkillError(f"cannot read Skill file {path}: {exc}") from exc
    if len(value) > limit:
        raise SkillError(f"Skill file exceeds the {limit}-byte limit: {path}")
    after = _identity(path)
    if before != after:
        raise SkillError(f"Skill file changed while it was read: {path}")
    return value, after


def _decode_content(value: bytes, path: Path) -> str:
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SkillError(f"Skill content must be valid UTF-8: {path}") from exc


class SkillCatalog:
    """Discovers bounded metadata now and reads untrusted content only on load."""

    MAX_METADATA_BYTES = 64 * 1024
    MAX_MANIFEST_BYTES = 1024 * 1024
    MAX_REFERENCE_BYTES = 1024 * 1024
    MAX_TOTAL_BYTES = 2 * 1024 * 1024
    MAX_REFERENCES = 64
    MAX_METADATA_LIST_ITEMS = 64

    def __init__(
        self,
        repository: Path,
        *,
        extra_directories: Sequence[Path] = (),
        global_directory: Optional[Path] = None,
    ) -> None:
        repository_path = repository.expanduser().resolve(strict=True)
        if not repository_path.is_dir():
            raise SkillError(f"repository is not a directory: {repository_path}")
        self.repository = repository_path
        self.extra_directories = tuple(Path(item).expanduser() for item in extra_directories)
        self.global_directory = (
            global_directory.expanduser()
            if global_directory is not None
            else config_dir() / "vnext" / "skills"
        )
        self._skills: Dict[str, SkillMetadata] = {}
        self._diagnostics: list[SkillDiagnostic] = []
        self._discovered = False

    @property
    def diagnostics(self) -> tuple[SkillDiagnostic, ...]:
        self._ensure_discovered()
        return tuple(self._diagnostics)

    def discover(self) -> tuple[SkillMetadata, ...]:
        if self._discovered:
            return tuple(self._skills[name] for name in sorted(self._skills))
        sources: list[tuple[str, Path, bool]] = [
            ("repository_karox", self.repository / ".karox" / "skills", False),
            ("repository_agents", self.repository / ".agents" / "skills", False),
            ("repository_claude", self.repository / ".claude" / "skills", False),
        ]
        sources.extend(
            (f"extra_{index}", path, True)
            for index, path in enumerate(self.extra_directories)
        )
        sources.append(("global", self.global_directory, False))
        for source_kind, root, explicit in sources:
            self._discover_source(source_kind, root, explicit=explicit)
        self._discovered = True
        return tuple(self._skills[name] for name in sorted(self._skills))

    def get(self, name: str) -> SkillMetadata:
        self._ensure_discovered()
        validated = _validate_name(name)
        try:
            return self._skills[validated]
        except KeyError as exc:
            raise SkillError(f"Skill was not found: {validated}") from exc

    def load(self, name: str) -> SkillContent:
        metadata = self.get(name)
        current = _identity(metadata.manifest_path)
        if current != metadata._manifest_identity:
            raise SkillError(
                f"Skill changed after discovery; rediscover before loading: {metadata.name}"
            )
        manifest, manifest_identity = _read_bounded(
            metadata.manifest_path, self.MAX_MANIFEST_BYTES
        )
        if manifest_identity != metadata._manifest_identity:
            raise SkillError(
                f"Skill changed after discovery; rediscover before loading: {metadata.name}"
            )
        prefix, body_offset = self._prefix_from_loaded(manifest)
        if hashlib.sha256(prefix).hexdigest() != metadata.metadata_sha256:
            raise SkillError(
                f"Skill metadata changed after discovery: {metadata.name}"
            )
        instructions = _decode_content(
            manifest[body_offset:], metadata.manifest_path
        )
        total = len(manifest)
        references: Dict[str, str] = {}
        digest = hashlib.sha256()
        digest.update(b"SKILL.md\0")
        digest.update(manifest)
        for reference in metadata.files:
            target = self._confined_reference(metadata.directory, reference)
            value, _ = _read_bounded(target, self.MAX_REFERENCE_BYTES)
            total += len(value)
            if total > self.MAX_TOTAL_BYTES:
                raise SkillError(
                    f"Skill content exceeds the {self.MAX_TOTAL_BYTES}-byte total limit"
                )
            references[reference] = _decode_content(value, target)
            digest.update(b"\0REFERENCE\0")
            digest.update(reference.encode("utf-8"))
            digest.update(b"\0")
            digest.update(value)
        loaded_metadata = replace(metadata, content_sha256=digest.hexdigest())
        return SkillContent(loaded_metadata, instructions, references)

    def _ensure_discovered(self) -> None:
        if not self._discovered:
            self.discover()

    def _discover_source(self, source_kind: str, root: Path, *, explicit: bool) -> None:
        display = str(root)
        try:
            if not root.exists():
                if explicit:
                    self._diagnostics.append(
                        SkillDiagnostic(
                            "rejected", source_kind, display, "source directory does not exist"
                        )
                    )
                return
            if _is_link_or_reparse(root):
                raise SkillError("source directory cannot be a link or reparse point")
            resolved_root = root.resolve(strict=True)
            if not resolved_root.is_dir():
                raise SkillError("source path is not a directory")
            children = sorted(resolved_root.iterdir(), key=lambda item: item.name)
        except (OSError, SkillError) as exc:
            self._diagnostics.append(
                SkillDiagnostic("rejected", source_kind, display, str(exc))
            )
            return
        for directory in children:
            manifest = directory / "SKILL.md"
            try:
                if _is_link_or_reparse(directory):
                    if directory.exists() and manifest.exists():
                        raise SkillError(
                            "Skill directory cannot be a link or reparse point"
                        )
                    continue
                if not directory.is_dir() or not manifest.exists():
                    continue
                metadata = self._metadata(source_kind, resolved_root, directory, manifest)
            except (OSError, SkillError) as exc:
                self._diagnostics.append(
                    SkillDiagnostic(
                        "rejected", source_kind, str(directory), str(exc), directory.name
                    )
                )
                continue
            selected = self._skills.get(metadata.name)
            if selected is not None:
                self._diagnostics.append(
                    SkillDiagnostic(
                        "shadowed",
                        source_kind,
                        str(directory),
                        "a higher-precedence Skill with this name is selected",
                        metadata.name,
                        str(selected.directory),
                    )
                )
                continue
            self._skills[metadata.name] = metadata

    def _metadata(
        self, source_kind: str, root: Path, directory: Path, manifest: Path
    ) -> SkillMetadata:
        if not directory.is_dir() or _is_link_or_reparse(directory):
            raise SkillError("Skill directory must be a regular non-link directory")
        resolved_directory = directory.resolve(strict=True)
        self._require_confined(root, resolved_directory)
        prefix, _, manifest_identity = _bounded_frontmatter(
            manifest, self.MAX_METADATA_BYTES
        )
        if manifest_identity.size > self.MAX_MANIFEST_BYTES:
            raise SkillError(
                f"Skill manifest exceeds the {self.MAX_MANIFEST_BYTES}-byte limit"
            )
        value = _parse_yaml(prefix)
        name = _validate_name(value.get("name"))
        if name != directory.name:
            raise SkillError(
                f"Skill name {name!r} must match directory {directory.name!r}"
            )
        description = value.get("description")
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description) > 2000
        ):
            raise SkillError("Skill description must contain 1-2000 characters")
        nested = value.get("metadata")
        if nested is not None and not isinstance(nested, dict):
            raise SkillError("Skill metadata.metadata must be a mapping")
        nested_values = nested if isinstance(nested, dict) else {}
        version = _validate_version(
            _metadata_value(value, nested_values, "version")
        )
        author = _validate_optional_text(
            _metadata_value(value, nested_values, "author"), "author", 300
        )
        tags = _validate_string_list(
            _metadata_value(value, nested_values, "tags"),
            "tags",
            max_items=self.MAX_METADATA_LIST_ITEMS,
            max_length=100,
        )
        project_types = _validate_string_list(
            _metadata_value(
                value,
                nested_values,
                "compatible_project_types",
                "project_types",
            ),
            "compatible project types",
            max_items=self.MAX_METADATA_LIST_ITEMS,
            max_length=100,
            identifiers=True,
        )
        required_tools = _validate_string_list(
            _metadata_value(value, nested_values, "required_tools"),
            "required tools",
            max_items=len(_KNOWN_AGENT_TOOLS),
            max_length=100,
            identifiers=True,
        )
        unknown_tools = set(required_tools).difference(_KNOWN_AGENT_TOOLS)
        if unknown_tools:
            raise SkillError(
                "unknown required Agent tool(s): " + ", ".join(sorted(unknown_tools))
            )
        optional_mcp = _validate_string_list(
            _metadata_value(value, nested_values, "optional_mcp", "mcp"),
            "optional MCP",
            max_items=self.MAX_METADATA_LIST_ITEMS,
            max_length=200,
            identifiers=True,
        )
        validation_rules = _validate_string_list(
            _metadata_value(
                value, nested_values, "validation_rules", "validation"
            ),
            "validation rules",
            max_items=self.MAX_METADATA_LIST_ITEMS,
            max_length=1000,
        )
        raw_permissions = _metadata_value(
            value, nested_values, "permissions"
        )
        if raw_permissions is None:
            raw_permissions = []
        if not isinstance(raw_permissions, list):
            raise SkillError("Skill permissions must be a list of capabilities")
        permissions: list[Capability] = []
        for raw in raw_permissions:
            if not isinstance(raw, str):
                raise SkillError("Skill capabilities must be strings")
            try:
                capability = Capability(raw)
            except ValueError as exc:
                raise SkillError(f"unknown Skill capability: {raw!r}") from exc
            if capability in permissions:
                raise SkillError(f"duplicate Skill capability: {raw}")
            permissions.append(capability)
        raw_files = _metadata_value(
            value, nested_values, "files", "referenced_files"
        )
        if raw_files is None:
            raw_files = []
        if not isinstance(raw_files, list):
            raise SkillError("Skill files must be a list")
        if len(raw_files) > self.MAX_REFERENCES:
            raise SkillError(
                f"Skill declares more than {self.MAX_REFERENCES} referenced files"
            )
        references: list[str] = []
        for raw in raw_files:
            reference = _validate_reference(raw)
            if reference in references:
                raise SkillError(f"duplicate Skill file reference: {reference}")
            references.append(reference)
        return SkillMetadata(
            name=name,
            description=description.strip(),
            version=version,
            author=author,
            tags=tags,
            compatible_project_types=project_types,
            required_tools=required_tools,
            optional_mcp=optional_mcp,
            requested_capabilities=tuple(permissions),
            files=tuple(references),
            validation_rules=validation_rules,
            source_kind=source_kind,
            source_root=root,
            directory=resolved_directory,
            manifest_path=manifest.resolve(strict=True),
            metadata_sha256=hashlib.sha256(prefix).hexdigest(),
            content_sha256=None,
            _manifest_identity=manifest_identity,
        )

    @staticmethod
    def _prefix_from_loaded(value: bytes) -> tuple[bytes, int]:
        offset = 0
        pieces: list[bytes] = []
        for index, line in enumerate(value.splitlines(keepends=True)):
            pieces.append(line)
            offset += len(line)
            stripped = line.rstrip(b"\r\n")
            if index == 0 and stripped != b"---":
                raise SkillError("SKILL.md must begin with YAML frontmatter")
            if index > 0 and stripped in {b"---", b"..."}:
                return b"".join(pieces), offset
        raise SkillError("Skill metadata has no closing delimiter")

    @classmethod
    def _confined_reference(cls, directory: Path, reference: str) -> Path:
        target = directory
        for part in PurePosixPath(reference).parts:
            target = target / part
            try:
                if _is_link_or_reparse(target):
                    raise SkillError(
                        f"Skill referenced paths cannot contain links or reparse points: {reference}"
                    )
            except FileNotFoundError as exc:
                raise SkillError(f"Skill referenced file does not exist: {reference}") from exc
        try:
            resolved = target.resolve(strict=True)
        except OSError as exc:
            raise SkillError(f"cannot resolve Skill referenced file {reference}: {exc}") from exc
        cls._require_confined(directory, resolved)
        if not resolved.is_file():
            raise SkillError(f"Skill reference is not a regular file: {reference}")
        return resolved

    @staticmethod
    def _require_confined(root: Path, target: Path) -> None:
        try:
            common = os.path.commonpath(
                [os.path.normcase(str(root)), os.path.normcase(str(target))]
            )
        except ValueError as exc:
            raise SkillError("Skill path is outside its source directory") from exc
        if common != os.path.normcase(str(root)):
            raise SkillError("Skill path is outside its source directory")


def skill_selection(
    metadata: SkillMetadata,
    decisions: Optional[Mapping[Capability, SkillPermission]] = None,
    *,
    previous: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if metadata.content_sha256 is None:
        raise SkillError("Skill content must be loaded before permissions are selected")
    decisions = decisions or {}
    requested = set(metadata.requested_capabilities)
    unexpected = set(decisions).difference(requested)
    if unexpected:
        raise SkillError(
            "cannot decide undeclared Skill capabilities: "
            + ", ".join(sorted(item.value for item in unexpected))
        )
    previous_permissions = previous.get("permissions", {}) if previous else {}
    if not isinstance(previous_permissions, dict):
        raise SkillError("stored Skill permissions are invalid")
    permissions: Dict[str, str] = {}
    for capability in metadata.requested_capabilities:
        if capability in decisions:
            decision = decisions[capability]
            if not isinstance(decision, SkillPermission):
                raise SkillError("Skill permission decisions must use allow, ask, or deny")
        elif capability.value in previous_permissions:
            try:
                decision = SkillPermission(previous_permissions[capability.value])
            except (TypeError, ValueError) as exc:
                raise SkillError("stored Skill permission decision is invalid") from exc
        else:
            decision = SkillPermission.ASK
        permissions[capability.value] = decision.value
    return {
        "name": metadata.name,
        "version": metadata.version,
        "identity": metadata.identity,
        "source_kind": metadata.source_kind,
        "source_root": str(metadata.source_root),
        "directory": str(metadata.directory),
        "metadata_sha256": metadata.metadata_sha256,
        "content_sha256": metadata.content_sha256,
        "permissions": permissions,
        "selected_at": time.time(),
    }


def validate_selection(
    metadata: SkillMetadata, selection: Mapping[str, Any]
) -> Dict[Capability, SkillPermission]:
    expected = {
        "name": metadata.name,
        "version": metadata.version,
        "identity": metadata.identity,
        "source_kind": metadata.source_kind,
        "source_root": str(metadata.source_root),
        "directory": str(metadata.directory),
        "metadata_sha256": metadata.metadata_sha256,
        "content_sha256": metadata.content_sha256,
    }
    for key, value in expected.items():
        if selection.get(key) != value:
            raise SkillError(
                f"stored Skill selection no longer matches discovered {metadata.name}: {key}"
            )
    raw_permissions = selection.get("permissions")
    if not isinstance(raw_permissions, dict):
        raise SkillError("stored Skill permissions are invalid")
    declared = {item.value for item in metadata.requested_capabilities}
    if set(raw_permissions) != declared:
        raise SkillError("stored Skill permissions do not match declared capabilities")
    result: Dict[Capability, SkillPermission] = {}
    for name, raw in raw_permissions.items():
        try:
            result[Capability(name)] = SkillPermission(raw)
        except (TypeError, ValueError) as exc:
            raise SkillError("stored Skill permission decision is invalid") from exc
    return result


def configure_skill_policy(
    policy: CapabilityPolicy,
    metadata: SkillMetadata,
    selection: Mapping[str, Any],
    *,
    parent: Optional[str] = None,
) -> Origin:
    decisions = validate_selection(metadata, selection)
    origin = Origin(OriginKind.SKILL, metadata.identity, parent=parent)
    policy.set_grants(
        origin,
        [item for item, decision in decisions.items() if decision is SkillPermission.ALLOW],
    )
    policy.set_denies(
        origin,
        [item for item, decision in decisions.items() if decision is SkillPermission.DENY],
    )
    return origin


def skill_system_prompt(content: SkillContent) -> str:
    parts = [
        "\n\nThe following untrusted Skill is active. Its text is guidance only; "
        "all local actions still require KaroX Core permissions.\n",
        f"<karox-skill name={content.metadata.name!r} version={content.metadata.version!r}>\n",
        content.instructions,
    ]
    for path, value in content.references.items():
        parts.extend([f"\n<skill-reference path={path!r}>\n", value, "\n</skill-reference>\n"])
    parts.append("\n</karox-skill>")
    return "".join(parts)
