"""Hot-reload supervisor for stable developer-runtime commands.

The public MCP schemas remain fixed while implementation details live in a small
worker module group.  The bridge process and its tunnel stay alive; on source
changes the next command atomically loads a fresh generation.  A failed reload
never replaces the last known-good modules.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from importlib import _bootstrap
from pathlib import Path
from types import ModuleType
from typing import Any, Optional


_REQUIRED_ENTRY_POINTS = (
    "validate_repo_command",
    "execute_repo_command",
    "execute_tests",
    "execute_browser_command",
)
_DEFAULT_MODULE_GROUP = (
    "karox.workspace_transaction",
    "karox.unified_patch",
    # Desktop app actions are dispatched from workspace_worker but implemented
    # separately. Keep them in the same last-known-good generation so desktop
    # safety fixes never require recycling the persistent bridge.
    "karox.desktop_apps",
    "karox.workspace_worker",
)


@dataclass(frozen=True)
class WorkerSnapshot:
    generation: int
    reload_count: int
    loaded_at: float
    source_path: str
    source_sha256: str
    source_mtime_ns: int
    watched_sources: tuple[str, ...]
    last_reload_error: Optional[str]
    bridge_restart_required: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "reload_count": self.reload_count,
            "loaded_at": self.loaded_at,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            # Explicit name for the digest of the whole reloadable module group.
            # source_sha256 remains for backward compatibility.
            "generation_sha256": self.source_sha256,
            "source_mtime_ns": self.source_mtime_ns,
            "watched_sources": list(self.watched_sources),
            "last_reload_error": self.last_reload_error,
            "hot_reload": True,
            "bridge_restart_required": self.bridge_restart_required,
        }


class HotWorkerSupervisor:
    """Keep a last-known-good reloadable implementation module group."""

    MODULE_NAME = "karox.workspace_worker"
    MODULE_GROUP: Optional[tuple[str, ...]] = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._module: Optional[ModuleType] = None
        self._modules: dict[str, ModuleType] = {}
        self._signature: Optional[tuple[tuple[str, int, int, str], ...]] = None
        self._failed_signature: Optional[tuple[tuple[str, int, int, str], ...]] = None
        self._generation = 0
        self._reload_count = 0
        self._loaded_at = 0.0
        self._source_sha256 = ""
        self._source_path = ""
        self._source_mtime_ns = 0
        self._watched_sources: tuple[str, ...] = ()
        self._last_reload_error: Optional[str] = None
        self._supervisor_source_path = Path(__file__).resolve()
        try:
            self._supervisor_source_sha256 = self._digest(self._supervisor_source_path)
        except OSError:
            # If the supervisor source cannot be established, status must prefer
            # a conservative restart recommendation over a false all-clear.
            self._supervisor_source_sha256 = ""

    def _module_names(self) -> tuple[str, ...]:
        if self.MODULE_GROUP is not None:
            if not self.MODULE_GROUP or self.MODULE_GROUP[-1] != self.MODULE_NAME:
                raise RuntimeError("MODULE_GROUP must end with MODULE_NAME")
            return self.MODULE_GROUP
        if self.MODULE_NAME == "karox.workspace_worker":
            return _DEFAULT_MODULE_GROUP
        return (self.MODULE_NAME,)

    @staticmethod
    def _source(module: ModuleType) -> Path:
        value = getattr(module, "__file__", None)
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"reloadable module {module.__name__} has no source file")
        path = Path(value).resolve()
        if path.suffix == ".pyc" and path.with_suffix(".py").is_file():
            path = path.with_suffix(".py")
        if not path.is_file():
            raise RuntimeError(f"reloadable module source is missing: {path}")
        return path

    @staticmethod
    def _digest(path: Path) -> str:
        # Metadata-only caches miss same-size edits with restored mtime, even
        # with ctime on filesystems whose clock has not advanced between writes.
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _sources(
        self, modules: dict[str, ModuleType]
    ) -> dict[str, Path]:
        return {name: self._source(modules[name]) for name in self._module_names()}

    def _signature_for(
        self, sources: dict[str, Path]
    ) -> tuple[tuple[str, int, int, str], ...]:
        rows: list[tuple[str, int, int, str]] = []
        for name in self._module_names():
            path = sources[name]
            stat = path.stat()
            rows.append((name, stat.st_mtime_ns, stat.st_size, self._digest(path)))
        return tuple(rows)

    @staticmethod
    def _combined_digest(
        signature: tuple[tuple[str, int, int, str], ...]
    ) -> str:
        body = "\n".join(f"{name}:{digest}" for name, _mtime, _size, digest in signature)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def _bridge_restart_required(self) -> bool:
        """Return True when the non-reloadable supervisor source changed.

        The worker module group can update in-place, but this supervisor owns the
        composition of that group. A change to ``hot_worker.py`` therefore cannot
        safely self-apply; report the need for one durable child recycle instead
        of claiming hot reload covers it.
        """

        if not self._supervisor_source_sha256:
            return True
        try:
            current = self._digest(self._supervisor_source_path)
        except OSError:
            return True
        return current != self._supervisor_source_sha256

    def _update_snapshot(
        self,
        modules: dict[str, ModuleType],
        sources: dict[str, Path],
        signature: tuple[tuple[str, int, int, str], ...],
    ) -> None:
        self._modules = modules
        self._module = modules[self.MODULE_NAME]
        self._signature = signature
        self._loaded_at = time.time()
        worker_path = sources[self.MODULE_NAME]
        self._source_path = str(worker_path)
        self._source_mtime_ns = max(row[1] for row in signature)
        self._source_sha256 = self._combined_digest(signature)
        self._watched_sources = tuple(str(sources[name]) for name in self._module_names())
        self._last_reload_error = None
        self._failed_signature = None

    @staticmethod
    def _validate_worker(module: ModuleType) -> None:
        missing = [
            name
            for name in _REQUIRED_ENTRY_POINTS
            if not callable(getattr(module, name, None))
        ]
        if missing:
            raise RuntimeError(f"workspace worker misses entry points: {missing}")

    def _import_initial(self) -> ModuleType:
        modules = {
            name: importlib.import_module(name)
            for name in self._module_names()
        }
        self._validate_worker(modules[self.MODULE_NAME])
        sources = self._sources(modules)
        signature = self._signature_for(sources)
        self._generation = 1
        self._update_snapshot(modules, sources, signature)
        return modules[self.MODULE_NAME]

    @staticmethod
    def _candidate(name: str, path: Path) -> ModuleType:
        # A fresh spec is essential: changing _initializing on the old module's
        # spec would make known-good imports wait on the candidate too.
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None:
            raise RuntimeError(f"cannot create reloadable module spec: {name}")
        return importlib.util.module_from_spec(spec)

    def _load_generation(
        self,
        sources: dict[str, Path],
        signature: tuple[tuple[str, int, int, str], ...],
    ) -> dict[str, ModuleType]:
        names = self._module_names()
        contents = {name: sources[name].read_bytes() for name in names}
        expected = {name: digest for name, _mtime, _size, digest in signature}
        if any(hashlib.sha256(contents[name]).hexdigest() != expected[name] for name in names):
            raise RuntimeError("reloadable source changed while preparing generation")
        compiled = {
            name: compile(contents[name], str(sources[name]), "exec") for name in names
        }
        # Use the *same* per-module locks as ordinary imports. The global
        # _imp lock neither protects cached imports nor may be held over exec:
        # a dependency being imported in another thread can need it to finish.
        # importlib has no public API for these reentrant, deadlock-aware locks.
        with ExitStack() as locks:
            for name in names:
                locks.enter_context(_bootstrap._ModuleLockManager(name))  # type: ignore[attr-defined]
            previous = {name: sys.modules.get(name) for name in names}
            if any(module is None for module in previous.values()):
                raise RuntimeError("reloadable module group is incomplete in sys.modules")
            candidates = {
                name: self._candidate(name, sources[name]) for name in names
            }
            missing = object()
            parents = {
                name: sys.modules.get(name.rpartition(".")[0]) for name in names
            }
            attributes = {
                name: getattr(parents[name], name.rpartition(".")[2], missing) for name in names
            }
            try:
                for name in names:
                    candidate = candidates[name]
                    # Set this BEFORE inserting into sys.modules, just as
                    # importlib does. Cached import_module calls then wait and
                    # re-read sys.modules after success OR rollback. Keep it set
                    # until the entire generation has passed validation.
                    setattr(candidate.__spec__, "_initializing", True)
                    sys.modules[name] = candidate
                    exec(compiled[name], candidate.__dict__)
                    # `from package import dependency` bypasses module locks
                    # when this attribute exists. Keep the old, complete module
                    # there until exec finishes. Publish completed dependencies
                    # in order so later candidates also see their new versions.
                    parent = parents[name]
                    if parent is not None:
                        setattr(parent, name.rpartition(".")[2], candidate)
                self._validate_worker(candidates[self.MODULE_NAME])
            except BaseException:
                for name, module in previous.items():
                    if module is None:
                        sys.modules.pop(name, None)
                    else:
                        sys.modules[name] = module
                    parent = parents[name]
                    if parent is not None:
                        attribute = name.rpartition(".")[2]
                        if attributes[name] is missing:
                            if hasattr(parent, attribute):
                                delattr(parent, attribute)
                        else:
                            setattr(parent, attribute, attributes[name])
                raise
            finally:
                # Restore sys.modules first on failure, and only then unblock
                # importers. Never leave a candidate marked as initializing.
                for candidate in candidates.values():
                    setattr(candidate.__spec__, "_initializing", False)
        return candidates

    def module(self, *, force: bool = False) -> ModuleType:
        with self._lock:
            if self._module is None:
                return self._import_initial()
            signature = None
            sources: dict[str, Path] = {}
            try:
                sources = self._sources(self._modules)
                signature = self._signature_for(sources)
                if not force:
                    if self._combined_digest(signature) == self._source_sha256:
                        # A touch (or recovery to known-good bytes) is not a new
                        # implementation generation.
                        self._signature = signature
                        self._source_mtime_ns = max(row[1] for row in signature)
                        self._last_reload_error = None
                        self._failed_signature = None
                        return self._module
                    if signature == self._failed_signature:
                        return self._module
                importlib.invalidate_caches()
                candidates = self._load_generation(sources, signature)
            except Exception as exc:
                # Only cache compile failures backed by watched source bytes.
                # Imports/I/O can recover without changing this module group.
                self._failed_signature = (
                    signature
                    if isinstance(exc, SyntaxError)
                    and exc.filename in {str(path) for path in sources.values()}
                    else None
                )
                self._last_reload_error = f"{type(exc).__name__}: {exc}"
                if force:
                    raise RuntimeError(
                        "workspace worker reload failed; the last known-good "
                        "generation remains active: "
                        f"{self._last_reload_error}"
                    ) from exc
                # Normal commands continue on the last-known-good generation.
                # The error remains visible through runtime.status, while the
                # permanent MCP bridge, tunnel and browser stay usable.
                assert self._module is not None
                return self._module

            self._generation += 1
            self._reload_count += 1
            self._update_snapshot(candidates, sources, signature)
            assert self._module is not None
            return self._module

    def force_reload(self) -> dict[str, Any]:
        self.module(force=True)
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            # Status itself also checks for source changes, so it accurately
            # reports the generation a following command will use.
            self.module()
            return WorkerSnapshot(
                generation=self._generation,
                reload_count=self._reload_count,
                loaded_at=self._loaded_at,
                source_path=self._source_path,
                source_sha256=self._source_sha256,
                source_mtime_ns=self._source_mtime_ns,
                watched_sources=self._watched_sources,
                last_reload_error=self._last_reload_error,
                bridge_restart_required=self._bridge_restart_required(),
            ).to_dict()

    def validate_repo(self, arguments: dict[str, Any]) -> None:
        module = self.module()
        module.validate_repo_command(arguments)

    def execute_repo(
        self,
        runtime: Any,
        arguments: dict[str, Any],
        deadline_seconds: float,
    ) -> dict[str, Any]:
        module = self.module()
        return dict(module.execute_repo_command(runtime, arguments, deadline_seconds))

    def execute_tests(
        self,
        runtime: Any,
        arguments: dict[str, Any],
        deadline_seconds: float,
    ) -> dict[str, Any]:
        module = self.module()
        return dict(module.execute_tests(runtime, arguments, deadline_seconds))

    def execute_browser(
        self,
        runtime: Any,
        arguments: dict[str, Any],
        deadline_seconds: float,
    ) -> dict[str, Any]:
        module = self.module()
        return dict(
            module.execute_browser_command(runtime, arguments, deadline_seconds)
        )


_SUPERVISOR = HotWorkerSupervisor()


def hot_worker_supervisor() -> HotWorkerSupervisor:
    return _SUPERVISOR
