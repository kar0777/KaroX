"""KaroX Pack SDK: manifest, lifecycle, and permissions.

A Pack is a versioned, declarative extension for a development domain.  It can
contribute Core-registered tools, Skills, MCP server declarations, project
detectors, commands, templates, tests, validators, and health checks.  It cannot
bypass Core or session policy, run arbitrary setup scripts, request unrestricted
environment, or request a provider credential.

The manifest is a strict ``karox-pack.toml``: unknown fields are rejected, paths
must remain inside the Pack, and content hashes are verified.  Installed Pack
copies are immutable; enable/disable grants only a user-approved subset.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

# tomllib entered the standard library in 3.11, but this package declares 3.10 as
# its floor and both installers accept it. cli.py imports this module at the top
# level, so an unconditional `import tomllib` made every karox invocation on 3.10
# die with an ImportError before argparse ran.
if sys.version_info >= (3, 11):  # pragma: no cover - selected by interpreter
    import tomllib
else:  # pragma: no cover - selected by interpreter
    import tomli as tomllib

# The registry lock has to hold across processes -- two `karox pack` invocations,
# not two threads -- so it is taken on a file descriptor by the OS. The two
# platforms expose that through different modules and neither is importable on
# the other. The test is written against `sys.platform` rather than `os.name`
# because a type checker narrows on the former, and otherwise reports the whole
# unreachable branch as errors on each platform in turn.
if sys.platform == "win32":  # pragma: no cover - selected by platform
    import msvcrt

    def _try_lock(descriptor: int) -> None:
        # Locks one byte at the current offset; the byte need not exist. Windows
        # refuses immediately rather than waiting, which is what the retry loop
        # in _exclusive_file_lock wants.
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)

    def _unlock(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - selected by platform
    import fcntl

    def _try_lock(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


PACK_MANIFEST_NAME = "karox-pack.toml"
PACK_MANIFEST_VERSION = 1
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_REFERENCE_BYTES = 1024 * 1024
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SAFE_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SAFE_CAPABILITY = re.compile(r"^[a-z][a-z0-9._-]{0,62}$")
_SAFE_TOOL = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


def _runtime_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _compatible_karox(requirement: str) -> bool:
    from . import __version__

    # A release component can carry a suffix -- 5.0.0.dev0 compares as 5, 0, 0 --
    # so only its leading digits count. A component with no digits at all is
    # reported rather than dereferenced: the old expression assumed the match
    # always succeeded and would have raised AttributeError from inside a
    # generator, naming neither the version nor the pack.
    release: list[str] = []
    for component in __version__.split(".")[:3]:
        digits = re.match(r"\d+", component)
        if digits is None:
            raise PackError(f"KaroX version is not comparable: {__version__!r}")
        release.append(digits.group(0))
    current_major = int(release[0])
    if re.fullmatch(r"\d+\.x", requirement):
        return int(requirement.split(".", 1)[0]) == current_major
    if _SAFE_VERSION.fullmatch(requirement):
        return ".".join(release) == requirement
    raise PackManifestError(
        "pack karox_version must be an exact semantic version or a major.x range"
    )


class PackError(RuntimeError):
    pass


class PackManifestError(PackError):
    pass


class PackConfigurationError(PackError):
    pass


class PackAccessDenied(PackError, PermissionError):
    pass


def _reject_nan(value: str) -> float:
    raise ValueError(f"non-finite number is not allowed: {value}")


def _strict_json_hash(value: Any) -> str:
    import json

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PackTool:
    name: str
    capability: str
    description: str = ""
    mutates: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _SAFE_TOOL.fullmatch(self.name) is None:
            raise PackManifestError("pack tool name must contain 1-128 safe characters")
        if not isinstance(self.capability, str) or _SAFE_CAPABILITY.fullmatch(self.capability) is None:
            raise PackManifestError("pack tool capability is invalid")
        if not isinstance(self.description, str):
            raise PackManifestError("pack tool description must be a string")
        if not isinstance(self.mutates, bool):
            raise PackManifestError("pack tool mutates must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PackMcpDeclaration:
    """An MCP server a Pack requests; never auto-granted."""

    server_id: str
    namespace: str
    transport: str
    # Read-only declarations only; a Pack cannot ship secret command lines or
    # URLs.  Real McpServerRecord creation requires an explicit user approval.
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.server_id, str) or _SAFE_NAME.fullmatch(self.server_id) is None:
            raise PackManifestError("pack MCP server id is invalid")
        if not isinstance(self.namespace, str) or _SAFE_NAME.fullmatch(self.namespace) is None:
            raise PackManifestError("pack MCP namespace is invalid")
        if self.transport not in {"stdio", "streamable_http"}:
            raise PackManifestError("pack MCP transport must be stdio or streamable_http")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PackManifest:
    """Strict, validated ``karox-pack.toml`` manifest."""

    name: str
    version: str
    description: str
    authors: tuple[str, ...]
    license: str
    karox_version: str
    platforms: tuple[str, ...]
    tools: tuple[PackTool, ...]
    skills: tuple[str, ...]
    mcp_declarations: tuple[PackMcpDeclaration, ...]
    detectors: tuple[str, ...]
    commands: tuple[str, ...]
    templates: tuple[str, ...]
    tests: tuple[str, ...]
    health_checks: tuple[str, ...]
    permissions: tuple[str, ...]
    manifest_version: int = PACK_MANIFEST_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _SAFE_NAME.fullmatch(self.name) is None:
            raise PackManifestError("pack name must be lowercase kebab (1-63 chars)")
        if not isinstance(self.version, str) or _SAFE_VERSION.fullmatch(self.version) is None:
            raise PackManifestError("pack version must be a semantic x.y.z")
        if not isinstance(self.description, str) or not self.description.strip():
            raise PackManifestError("pack description must be non-empty")
        if not isinstance(self.authors, tuple) or not self.authors or not all(
            isinstance(a, str) and a for a in self.authors
        ):
            raise PackManifestError("pack authors must be a non-empty list of strings")
        if not isinstance(self.license, str) or not self.license.strip():
            raise PackManifestError("pack license must be non-empty")
        if not isinstance(self.karox_version, str) or not self.karox_version.strip():
            raise PackManifestError("pack karox_version must be non-empty")
        if not isinstance(self.platforms, tuple) or not self.platforms:
            raise PackManifestError("pack platforms must be non-empty")
        for platform in self.platforms:
            if platform not in {"windows", "linux", "macos"}:
                raise PackManifestError(f"unsupported platform: {platform}")
        for attr, label in (
            (self.tools, "tools"),
            (self.skills, "skills"),
            (self.mcp_declarations, "mcp"),
            (self.detectors, "detectors"),
            (self.commands, "commands"),
            (self.templates, "templates"),
            (self.tests, "tests"),
            (self.health_checks, "health_checks"),
            (self.permissions, "permissions"),
        ):
            if not isinstance(attr, tuple):
                raise PackManifestError(f"pack {label} must be a list")
        for permission in self.permissions:
            if permission not in {
                "network", "process.run", "browser.read", "browser.input",
                "desktop.input",
            }:
                raise PackManifestError(f"unsupported pack permission: {permission}")
        if "network" in self.permissions and "process.run" not in self.permissions:
            # A Pack may only request network together with process.run, so the
            # capability boundary stays explicit rather than implicit.
            raise PackManifestError("network permission requires process.run")

    @property
    def identity(self) -> str:
        return f"{self.name}@{self.version}"

    def requested_capabilities(self) -> frozenset[str]:
        caps = {tool.capability for tool in self.tools}
        caps.update(self.permissions)
        return frozenset(caps)

    def requested_permissions(self) -> frozenset[str]:
        """Install-time permissions (process.run, network, ...), not tool caps.

        Tool capabilities are re-authorized by Core policy on every call; the
        install boundary only gates the elevated *permissions* a Pack requests.
        """
        return frozenset(self.permissions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "authors": list(self.authors),
            "license": self.license,
            "karox_version": self.karox_version,
            "platforms": list(self.platforms),
            "tools": [t.to_dict() for t in self.tools],
            "skills": list(self.skills),
            "mcp_declarations": [m.to_dict() for m in self.mcp_declarations],
            "detectors": list(self.detectors),
            "commands": list(self.commands),
            "templates": list(self.templates),
            "tests": list(self.tests),
            "health_checks": list(self.health_checks),
            "permissions": list(self.permissions),
        }


def _read_toml(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise PackManifestError("pack manifest exceeds 256 KiB")
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PackManifestError(f"cannot parse pack manifest: {exc}") from exc


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise PackManifestError(f"pack {label} must be an array")
    return value


def parse_pack_manifest(path: Path) -> PackManifest:
    """Parse and validate a ``karox-pack.toml`` against the strict schema."""
    if not isinstance(path, Path) or not path.is_file():
        raise PackManifestError(f"pack manifest not found: {path}")
    data = _read_toml(path)
    if not isinstance(data, dict):
        raise PackManifestError("pack manifest must be a table")
    version = data.get("manifest_version", PACK_MANIFEST_VERSION)
    if version != PACK_MANIFEST_VERSION:
        raise PackManifestError(f"unsupported pack manifest version: {version!r}")
    allowed = {
        "manifest_version", "name", "version", "description", "authors", "license",
        "karox_version", "platforms", "tools", "skills", "mcp", "detectors",
        "commands", "templates", "tests", "health_checks", "permissions",
    }
    unknown = set(data).difference(allowed)
    if unknown:
        raise PackManifestError(f"unknown pack manifest fields: {sorted(unknown)}")
    for required in ("name", "version", "description", "authors", "license", "karox_version", "platforms"):
        if required not in data:
            raise PackManifestError(f"pack manifest missing field: {required}")
    raw_tools = _require_list(data.get("tools", []), "tools")
    tools_list: list[PackTool] = []
    for item in raw_tools:
        if not isinstance(item, dict):
            raise PackManifestError("pack tools entries must be tables")
        unknown_tool = set(item).difference({"name", "capability", "description", "mutates"})
        if unknown_tool:
            raise PackManifestError(f"unknown pack tool fields: {sorted(unknown_tool)}")
        # Absence is reported as absence. PackTool's own checks reject the None
        # that a missing key produced, but they described it as a malformed value
        # -- "pack tool name must contain 1-128 safe characters" for a manifest
        # that never mentioned a name.
        for field in ("name", "capability"):
            if field not in item:
                raise PackManifestError(f"pack tool entry missing field: {field}")
        tools_list.append(
            PackTool(
                item["name"],
                item["capability"],
                item.get("description", ""),
                item.get("mutates", False),
            )
        )
    tools = tuple(tools_list)
    skills = tuple(_require_list(data.get("skills", []), "skills"))
    raw_mcp = _require_list(data.get("mcp", []), "mcp")
    mcp_list: list[PackMcpDeclaration] = []
    for item in raw_mcp:
        if not isinstance(item, dict):
            raise PackManifestError("pack MCP entries must be tables")
        unknown_mcp = set(item).difference(
            {"server_id", "namespace", "transport", "description"}
        )
        if unknown_mcp:
            raise PackManifestError(f"unknown pack MCP fields: {sorted(unknown_mcp)}")
        for field in ("server_id", "namespace", "transport"):
            if field not in item:
                raise PackManifestError(f"pack MCP entry missing field: {field}")
        mcp_list.append(
            PackMcpDeclaration(
                item["server_id"],
                item["namespace"],
                item["transport"],
                item.get("description", ""),
            )
        )
    mcp = tuple(mcp_list)
    return PackManifest(
        name=data["name"],
        version=data["version"],
        description=data["description"],
        authors=tuple(data["authors"]),
        license=data["license"],
        karox_version=data["karox_version"],
        platforms=tuple(data["platforms"]),
        tools=tools,
        skills=skills,
        mcp_declarations=mcp,
        detectors=tuple(_require_list(data.get("detectors", []), "detectors")),
        commands=tuple(_require_list(data.get("commands", []), "commands")),
        templates=tuple(_require_list(data.get("templates", []), "templates")),
        tests=tuple(_require_list(data.get("tests", []), "tests")),
        health_checks=tuple(_require_list(data.get("health_checks", []), "health_checks")),
        permissions=tuple(_require_list(data.get("permissions", []), "permissions")),
    )


def _validate_pack_paths(root: Path, manifest: PackManifest) -> None:
    """Every referenced path must stay inside the Pack directory."""
    root = root.resolve()
    for attr in (
        manifest.skills, manifest.detectors, manifest.commands, manifest.templates,
        manifest.tests, manifest.health_checks,
    ):
        for relative in attr:
            if not isinstance(relative, str) or not relative.strip():
                raise PackManifestError("pack reference must be a non-empty string")
            candidate = (root / relative).resolve(strict=False)
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise PackManifestError(
                    f"pack reference escapes the pack directory: {relative}"
                ) from exc
            if any(part == ".." for part in Path(relative).parts):
                raise PackManifestError(f"pack reference must not traverse: {relative}")


def _content_hashes(root: Path, manifest: PackManifest) -> Dict[str, str]:
    """Compute sha256 over every referenced file; reject missing files."""
    root = root.resolve()
    hashes: Dict[str, str] = {}
    for attr in (
        manifest.skills, manifest.detectors, manifest.commands, manifest.templates,
        manifest.tests, manifest.health_checks,
    ):
        for relative in attr:
            target = root / relative
            try:
                details = target.lstat()
            except FileNotFoundError as exc:
                raise PackManifestError(f"pack references missing file: {relative}") from exc
            attributes = getattr(details, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if stat.S_ISLNK(details.st_mode) or (reparse and attributes & reparse):
                raise PackManifestError(f"pack reference cannot be a link: {relative}")
            if not target.is_file():
                raise PackManifestError(f"pack references missing file: {relative}")
            raw = target.read_bytes()
            if len(raw) > _MAX_REFERENCE_BYTES:
                raise PackManifestError(f"pack file exceeds 1 MiB: {relative}")
            hashes[relative] = hashlib.sha256(raw).hexdigest()
    return hashes


@dataclass(frozen=True)
class InstalledPack:
    """An immutable installed Pack copy with a verified manifest and hashes."""

    name: str
    version: str
    install_path: str
    manifest_sha256: str
    content_hashes: Dict[str, str]
    enabled: bool = False

    @property
    def identity(self) -> str:
        return f"{self.name}@{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    import json
    import os
    import uuid

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


# Windows refuses a delete or a rename with a sharing violation for as long as
# any other process holds a handle on the file, and a virus scanner or the search
# indexer opening a freshly written pack is enough to cause one. It clears in
# milliseconds, so the operation is retried rather than reported to the user as a
# failure they can do nothing about: `karox pack remove` was returning "cannot
# remove installed pack" for a condition that had already passed.
_FS_RETRY_SECONDS = 2.0
_FS_RETRY_DELAY = 0.05


def _clear_read_only(target: Path) -> None:
    """Drop the read-only attribute Windows will not delete through.

    Not transient, but it presents as the same error and costs nothing to undo on
    a retry we are already making.
    """
    entries: Iterable[Path]
    if target.is_dir():
        entries = (target, *target.rglob("*"))
    else:
        entries = (target,)
    for entry in entries:
        try:
            entry.chmod(entry.stat().st_mode | stat.S_IWRITE)
        except OSError:
            pass


def _retry_transient_fs(operation: Any, target: Path) -> None:
    """Run a destructive filesystem operation, tolerating a passing lock."""
    deadline = time.monotonic() + _FS_RETRY_SECONDS
    while True:
        try:
            operation()
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            _clear_read_only(target)
            time.sleep(_FS_RETRY_DELAY)


def _remove_tree(target: Path) -> None:
    _retry_transient_fs(lambda: shutil.rmtree(target), target)


def _replace_path(source: Path, destination: Path) -> None:
    _retry_transient_fs(lambda: os.replace(source, destination), source)


@contextlib.contextmanager
def _exclusive_file_lock(path: Path, *, timeout: float = 30.0) -> Iterator[None]:
    """Hold an OS-level exclusive lock on ``path`` for the duration of the block.

    ``_atomic_json`` already makes each write all-or-nothing, but every registry
    mutation is a read-modify-write spanning two calls. Two ``karox pack``
    processes could therefore both read the same state and whichever saved second
    would silently drop the other's pack -- a lost install, or a resurrected
    removal. Serialising the whole read-modify-write is the only fix; a lock in
    process memory would not see the other process at all.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    # Opened without truncation on purpose: the lock carries no content, and
    # truncating would race with whoever currently holds it.
    descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:
            try:
                _try_lock(descriptor)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise PackConfigurationError(
                        f"timed out after {timeout:g}s waiting for the pack registry lock; "
                        "another karox process may be installing or removing a pack"
                    ) from None
                time.sleep(0.02)
        try:
            yield
        finally:
            _unlock(descriptor)
    finally:
        os.close(descriptor)


def _verification_complaint(report: Dict[str, Any]) -> str:
    """Say, in one clause, why a verification report is not ``ok``."""
    if not report.get("installed"):
        return "its install directory is missing"
    if not report.get("manifest_present"):
        return "its manifest is missing"
    if report.get("error"):
        return f"its manifest no longer parses: {report['error']}"
    problems = []
    if not report.get("manifest_matches", True):
        problems.append("its manifest changed since install")
    if report.get("missing_files"):
        problems.append("declared files are missing: " + ", ".join(sorted(report["missing_files"])))
    if report.get("modified_files"):
        problems.append("declared files were modified: " + ", ".join(sorted(report["modified_files"])))
    return "; ".join(problems) or "it failed verification"


class PackRegistry:
    """Manages installed immutable Packs and their enabled state."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "packs.json"
        self.lock_path = self.root / "packs.json.lock"
        self._lock_depth = 0

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialise a read-modify-write against other processes.

        Re-entrant within one process: both platform locks are held per
        descriptor, so a mutator that called another locked method would refuse
        or block against itself. Nesting therefore reuses the outer hold.
        """
        if self._lock_depth:
            yield
            return
        with _exclusive_file_lock(self.lock_path):
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1

    def _load(self) -> Dict[str, InstalledPack]:
        if not self.state_path.exists():
            return {}
        import json

        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"), parse_constant=_reject_nan)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise PackConfigurationError(f"cannot read pack registry: {exc}") from exc
        if not isinstance(data, dict) or set(data) != {"schema_version", "packs"}:
            raise PackConfigurationError("pack registry has invalid top-level fields")
        if data.get("schema_version") != PACK_MANIFEST_VERSION:
            raise PackConfigurationError("pack registry has an unsupported schema")
        packs = data.get("packs")
        if not isinstance(packs, list):
            raise PackConfigurationError("pack registry packs must be a list")
        result: Dict[str, InstalledPack] = {}
        for item in packs:
            if not isinstance(item, dict):
                raise PackConfigurationError("pack registry entry must be an object")
            pack = InstalledPack(
                item["name"], item["version"], item["install_path"],
                item["manifest_sha256"], dict(item.get("content_hashes", {})),
                bool(item.get("enabled", False)),
            )
            if pack.identity in result:
                raise PackConfigurationError(f"duplicate installed pack: {pack.identity}")
            result[pack.identity] = pack
        return result

    def _save(self, packs: Dict[str, InstalledPack]) -> None:
        _atomic_json(
            self.state_path,
            {
                "schema_version": PACK_MANIFEST_VERSION,
                "packs": [
                    item.to_dict()
                    for item in sorted(packs.values(), key=lambda p: p.identity)
                ],
            },
        )

    def list(self) -> List[InstalledPack]:
        # One read, not two. Taking the keys from one snapshot and the values
        # from another let a concurrent install or removal land in between, and
        # the second lookup then raised KeyError out of a read-only command.
        packs = self._load()
        return [packs[key] for key in sorted(packs)]

    def get(self, identity: str) -> InstalledPack:
        try:
            return self._load()[identity]
        except KeyError as exc:
            raise PackConfigurationError(f"pack is not installed: {identity}") from exc

    def install(self, source: Path, *, approved_permissions: Optional[Iterable[str]] = None) -> InstalledPack:
        # The lock spans the collision check as well as the write: without it two
        # installs of the same name could both find the name free and both stage
        # into the tree.
        with self._locked():
            return self._install_locked(source, approved_permissions=approved_permissions)

    def _install_locked(
        self, source: Path, *, approved_permissions: Optional[Iterable[str]] = None
    ) -> InstalledPack:
        source = source.expanduser().resolve(strict=True)
        if not source.is_dir():
            raise PackConfigurationError(f"pack source is not a directory: {source}")
        manifest_path = source / PACK_MANIFEST_NAME
        manifest = parse_pack_manifest(manifest_path)
        if not _compatible_karox(manifest.karox_version):
            raise PackConfigurationError(
                f"pack requires incompatible KaroX version: {manifest.karox_version}"
            )
        current_platform = _runtime_platform()
        if current_platform not in manifest.platforms:
            raise PackConfigurationError(
                f"pack does not support the current platform: {current_platform}"
            )
        _validate_pack_paths(source, manifest)
        hashes = _content_hashes(source, manifest)
        manifest_sha = _strict_json_hash(manifest.to_dict())
        # Validate approved permissions: every requested permission must be
        # explicitly approved by the user; no auto-grant.
        approved = frozenset(approved_permissions or ())
        requested = manifest.requested_permissions()
        if requested and not requested.issubset(approved):
            missing = sorted(requested.difference(approved))
            raise PackAccessDenied(f"pack requests unapproved permissions: {missing}")
        # Namespace collision check across installed packs.
        existing = self._load()
        for pack in existing.values():
            if pack.name == manifest.name:
                raise PackConfigurationError(
                    f"pack name already installed: {manifest.name}@{pack.version}"
                )
        install_dir = self.root / "installed" / manifest.name / manifest.version
        if install_dir.exists():
            raise PackConfigurationError(f"pack install path already exists: {install_dir}")
        # Atomic staging: copy into a temp dir then rename.
        staging = install_dir.with_name(f".{install_dir.name}.staging")
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        declared = [PACK_MANIFEST_NAME, *sorted(hashes)]
        for relative in declared:
            entry = source / relative
            dest = staging / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, dest, follow_symlinks=False)
        # Re-validate the staged copy before activation.
        staged_manifest = parse_pack_manifest(staging / PACK_MANIFEST_NAME)
        if _strict_json_hash(staged_manifest.to_dict()) != manifest_sha:
            shutil.rmtree(staging, ignore_errors=True)
            raise PackConfigurationError("staged manifest diverged from source")
        staged_hashes = _content_hashes(staging, staged_manifest)
        if staged_hashes != hashes:
            shutil.rmtree(staging, ignore_errors=True)
            raise PackConfigurationError("staged Pack content diverged from source")
        # Same sharing-violation window as removal: the files were written a
        # moment ago, which is exactly when a scanner opens them.
        _replace_path(staging, install_dir)
        pack = InstalledPack(
            manifest.name, manifest.version, str(install_dir),
            manifest_sha, hashes, enabled=False,
        )
        existing[pack.identity] = pack
        self._save(existing)
        return pack

    def enable(self, identity: str) -> InstalledPack:
        with self._locked():
            packs = self._load()
            try:
                pack = packs[identity]
            except KeyError as exc:
                raise PackConfigurationError(f"pack is not installed: {identity}") from exc
            if pack.enabled:
                return pack
            # Install-time verification is not enough. Enabling is the moment a
            # Pack's tools become callable, and the install directory is ordinary
            # files on disk that anything could have edited since -- so the
            # hashes recorded at install are re-checked here, against the same
            # rules `doctor` reports. Previously a Pack whose code had been
            # swapped out was activated without a word.
            report = self._verify(pack)
            if report["status"] != "ok":
                raise PackConfigurationError(
                    f"refusing to enable {identity}: {_verification_complaint(report)}"
                )
            pack = InstalledPack(
                pack.name, pack.version, pack.install_path, pack.manifest_sha256,
                pack.content_hashes, enabled=True,
            )
            packs[identity] = pack
            self._save(packs)
            return pack

    def disable(self, identity: str) -> InstalledPack:
        with self._locked():
            packs = self._load()
            try:
                pack = packs[identity]
            except KeyError as exc:
                raise PackConfigurationError(f"pack is not installed: {identity}") from exc
            if not pack.enabled:
                return pack
            pack = InstalledPack(
                pack.name, pack.version, pack.install_path, pack.manifest_sha256,
                pack.content_hashes, enabled=False,
            )
            packs[identity] = pack
            self._save(packs)
            return pack

    def remove(self, identity: str) -> InstalledPack:
        with self._locked():
            packs = self._load()
            try:
                pack = packs[identity]
            except KeyError as exc:
                raise PackConfigurationError(f"pack is not installed: {identity}") from exc
            if pack.enabled:
                raise PackConfigurationError("cannot remove an enabled pack; disable it first")
            install_path = Path(pack.install_path)
            if install_path.exists():
                try:
                    _remove_tree(install_path)
                except OSError as exc:
                    raise PackConfigurationError(
                        f"cannot remove installed pack {identity}: {exc}"
                    ) from exc
            packs.pop(identity)
            self._save(packs)
            return pack

    def doctor(self, identity: str) -> dict[str, Any]:
        """Verify an installed Pack without mutating the machine."""
        return self._verify(self.get(identity))

    def _verify(self, pack: InstalledPack) -> dict[str, Any]:
        """Check a Pack on disk against what was recorded when it was installed.

        Shared with ``enable`` so the two can never disagree about what counts as
        an intact Pack.
        """
        install_path = Path(pack.install_path)
        result: dict[str, Any] = {
            "identity": pack.identity,
            "installed": install_path.is_dir(),
            "manifest_present": (install_path / PACK_MANIFEST_NAME).is_file(),
        }
        if not result["installed"] or not result["manifest_present"]:
            result["status"] = "broken"
            return result
        try:
            manifest = parse_pack_manifest(install_path / PACK_MANIFEST_NAME)
        except PackManifestError as exc:
            result["status"] = "broken"
            result["error"] = str(exc)
            return result
        result["manifest_sha256"] = _strict_json_hash(manifest.to_dict())
        result["manifest_matches"] = result["manifest_sha256"] == pack.manifest_sha256
        # Re-check content hashes.
        missing = [
            relative for relative, expected in pack.content_hashes.items()
            if not (install_path / relative).is_file()
        ]
        modified = [
            relative
            for relative, expected in pack.content_hashes.items()
            if (install_path / relative).is_file()
            and hashlib.sha256((install_path / relative).read_bytes()).hexdigest()
            != expected
        ]
        result["missing_files"] = missing
        result["modified_files"] = modified
        result["status"] = (
            "ok"
            if result["manifest_matches"] and not missing and not modified
            else "broken"
        )
        return result


def create_pack_template(target: Path, *, name: str, description: str) -> Path:
    """Generate a minimal, testable sample pack at ``target``."""
    target = target.expanduser().resolve()
    if target.exists():
        raise PackConfigurationError(f"pack template target already exists: {target}")
    safe_name = name.lower()
    if not _SAFE_NAME.fullmatch(safe_name):
        raise PackConfigurationError("pack name must be lowercase kebab (1-63 chars)")
    target.mkdir(parents=True)
    description_literal = json.dumps(description, ensure_ascii=False)
    manifest = f'''manifest_version = {PACK_MANIFEST_VERSION}
name = "{safe_name}"
version = "0.1.0"
description = {description_literal}
authors = ["KaroX"]
license = "MIT"
karox_version = "5.x"
platforms = ["windows", "linux", "macos"]
skills = ["skills/SKILL.md"]
detectors = ["detectors/generic.txt"]
commands = []
templates = []
tests = ["tests/pack_test.txt"]
health_checks = []
permissions = []

[[tools]]
name = "project_metadata"
capability = "repo.read"
description = "Read project metadata for the {safe_name} pack."
mutates = false
'''
    (target / PACK_MANIFEST_NAME).write_text(manifest, encoding="utf-8")
    skills_dir = target / "skills"
    skills_dir.mkdir()
    (skills_dir / "SKILL.md").write_text(
        f"---\nname: {safe_name}-skill\ndescription: Sample skill for the {safe_name} pack.\nversion: 0.1.0\n---\nFollow the {safe_name} pack guidance.\n",
        encoding="utf-8",
    )
    detectors_dir = target / "detectors"
    detectors_dir.mkdir()
    (detectors_dir / "generic.txt").write_text(
        f"{safe_name} detector marker\n", encoding="utf-8",
    )
    tests_dir = target / "tests"
    tests_dir.mkdir()
    (tests_dir / "pack_test.txt").write_text(
        f"{safe_name} pack test harness marker\n", encoding="utf-8",
    )
    return target
