"""Project Map: durable, effort-contracted, incrementally refreshed.

The engine room behind ``/map``. It composes substrate that already exists
rather than duplicating it: :class:`ProjectFactMap` owns deterministic
source-hashed facts, :class:`RepositoryContextEngine` owns depth-budgeted goal
inspection with a content-revision cache, and the effort ladder owns how deep
a map each level is entitled to (``EffortBudget.map_depth`` /
``git_history_depth``). What was missing is the product layer this module
adds: one durable per-repository map document with an honest status, preview
estimates grounded in measured history instead of invented timings,
incremental refresh with cold/warm/single-file measurements, and a compact
digest the agent runtime consumes without receiving a project dump.

The map is a project fact, not a chat fact: state lives under the runtime
directory keyed by repository path, and the inspection cache session is a
stable well-known id so warm map builds survive across chats and TUI runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .artifacts import ArtifactStore
from .effort import AUTO_EFFORT, EFFORT_LEVELS, budget_for, normalize_effort
from .paths import runtime_dir
from .project_map import ProjectFactMap
from .repo_context import RepositoryContextEngine

MAP_SCHEMA_VERSION = 1

# The map ladder deliberately shares the effort vocabulary: a person who
# learned /effort has already learned /map, and the budget table is the one
# source of truth for what each level may spend.
MAP_LEVELS: tuple[str, ...] = EFFORT_LEVELS

# Engine cache/artifacts session: stable on purpose (see module docstring).
MAP_SESSION_ID = "project-map"

_ENGINE_DEPTHS: dict[str, Optional[str]] = {
    "low": None,
    "medium": "focused",
    "high": "standard",
    "extra-high": "deep",
    "ultra": "deep",
}

# Honest fallback duration bands (seconds) for a level that has never been
# measured on this machine. Ranges, not points: inventing exact timings is
# exactly what the preview contract forbids.
_FALLBACK_RANGES: dict[str, tuple[float, float]] = {
    "low": (0.1, 5.0),
    "medium": (1.0, 30.0),
    "high": (3.0, 60.0),
    "extra-high": (8.0, 150.0),
    "ultra": (15.0, 300.0),
}

# Estimated deep-inspection file ranges per level. Estimates by construction,
# labeled as such in the preview.
_INSPECTION_RANGES: dict[str, tuple[int, int]] = {
    "low": (0, 0),
    "medium": (5, 40),
    "high": (10, 80),
    "extra-high": (20, 160),
    "ultra": (30, 320),
}

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".idea",
        ".vscode",
        ".karox",
    }
)

_MAX_STRUCTURE_FILES = 5000
_COMMIT_LINE = re.compile(r"[0-9a-f]{40}")


def normalize_map_level(value: object) -> str:
    """Canonical map level, sharing the effort ladder's aliases.

    AUTO is an effort concept, not a map concept: a map build is an explicit
    action, so asking for an AUTO map is refused rather than guessed.
    """

    resolved = normalize_effort(value)
    if resolved == AUTO_EFFORT:
        raise ValueError(
            "map level must be one of " + ", ".join(MAP_LEVELS)
        )
    return resolved


def default_map_level(effort_level: Optional[str]) -> str:
    """The map level the current effort choice is entitled to.

    This is the real contract, not a convention: the budget table's
    ``map_depth`` field decides. AUTO and unknown spellings fall back to
    medium, the level whose cost is safe to spend without being asked.
    """

    try:
        if effort_level and effort_level != AUTO_EFFORT:
            return budget_for(normalize_effort(effort_level)).map_depth
    except ValueError:
        pass
    return "medium"


@dataclass(frozen=True)
class MapLevelContract:
    """What one map level actually does. Derived, never hand-tuned per call."""

    level: str
    engine_depth: Optional[str]
    dependency_hints: bool
    git_history_commits: int
    validates_sources: bool
    passes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "engine_depth": self.engine_depth,
            "dependency_hints": self.dependency_hints,
            "git_history_commits": self.git_history_commits,
            "validates_sources": self.validates_sources,
            "passes": self.passes,
        }


def level_contract(level: object) -> MapLevelContract:
    """Resolve a level into its enforceable map contract.

    Git history depth comes from the effort budget table -- the same numbers
    the agent runtime is entitled to -- so ``/map ultra`` cannot silently
    promise more Git evidence than an ultra run would be allowed to use.
    """

    resolved = normalize_map_level(level)
    budget = budget_for(resolved)
    return MapLevelContract(
        level=resolved,
        engine_depth=_ENGINE_DEPTHS[resolved],
        dependency_hints=resolved in {"high", "extra-high", "ultra"},
        git_history_commits=budget.git_history_depth,
        validates_sources=resolved in {"extra-high", "ultra"},
        passes=2 if resolved == "ultra" else 1,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MapService:
    """Build, refresh, measure, and serve one repository's durable map."""

    def __init__(
        self,
        repository: Path,
        *,
        root: Optional[Path] = None,
        session_id: str = MAP_SESSION_ID,
        memory_root: Optional[Path] = None,
    ) -> None:
        self.repository = Path(repository).expanduser().resolve(strict=True)
        base = (
            Path(root)
            if root is not None
            else runtime_dir() / "vnext" / "project_maps"
        )
        repo_key = hashlib.sha256(
            str(self.repository).encode("utf-8")
        ).hexdigest()[:20]
        self.directory = base / repo_key
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state_path = self.directory / "map.json"
        self.fact_map = ProjectFactMap(
            self.repository, self.directory / "facts.json"
        )
        self.session_id = session_id
        self.memory_root = memory_root

    # -- storage -----------------------------------------------------------

    def load(self) -> Optional[dict[str, Any]]:
        if not self.state_path.exists():
            return None
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != MAP_SCHEMA_VERSION:
            return None
        if payload.get("repository") != str(self.repository):
            return None
        return payload

    def _save(self, state: dict[str, Any]) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    # -- identity ----------------------------------------------------------

    def _git(self, *arguments: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.repository), *arguments],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                ),
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if completed.returncode != 0:
            return ""
        return (completed.stdout or "").strip()

    def _structure_identity(self) -> str:
        entries: list[str] = []
        for index, (relative, size) in enumerate(self._iter_files()):
            if index >= _MAX_STRUCTURE_FILES:
                break
            entries.append(f"{relative}:{size}")
        digest = hashlib.sha256(
            "\n".join(sorted(entries)).encode("utf-8")
        ).hexdigest()[:24]
        return f"structure:{digest}"

    def _iter_files(self) -> "list[tuple[str, int]]":
        found: list[tuple[str, int]] = []
        for dirpath, dirnames, filenames in os.walk(self.repository):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                relative = path.relative_to(self.repository).as_posix()
                found.append((relative, size))
                if len(found) >= _MAX_STRUCTURE_FILES:
                    return found
        return found

    def revision(self) -> str:
        """Cheap content identity for freshness decisions.

        With Git: HEAD plus a digest of the porcelain status, so a dirty tree
        is a different identity from a clean one. Without Git: a bounded
        path+size structural identity -- same-size in-place edits are
        invisible to it, so a no-git repository only ever loses warm reuse,
        never the correctness of a fresh build.
        """

        head = self._git("rev-parse", "--verify", "HEAD")
        if head:
            dirty = self._git("status", "--porcelain")
            suffix = (
                hashlib.sha256(dirty.encode("utf-8")).hexdigest()[:12]
                if dirty
                else "clean"
            )
            return f"{head[:16]}+{suffix}"
        return self._structure_identity()

    def _changed_files(
        self, previous: Optional[dict[str, Any]]
    ) -> Optional[list[str]]:
        """Files changed since the stored map, or None when unknowable."""

        if previous is None:
            return None
        stored_head = str(previous.get("git_head") or "")
        head = self._git("rev-parse", "--verify", "HEAD")
        if not head or not stored_head:
            return None
        changed: set[str] = set()
        if head != stored_head:
            diff = self._git("diff", "--name-only", f"{stored_head}..{head}")
            if not diff:
                # Unrelated histories or a rewritten branch: claiming "no
                # changes" here would be a lie, so the caller rebuilds.
                return None
            changed.update(
                line.strip().replace("\\", "/")
                for line in diff.splitlines()
                if line.strip()
            )
        for line in self._git("status", "--porcelain").splitlines():
            item = line[3:].strip().strip('"')
            if " -> " in item:
                item = item.split(" -> ", 1)[1]
            if item:
                changed.add(item.replace("\\", "/"))
        return sorted(changed)

    # -- evidence ----------------------------------------------------------

    def _git_cochange(self, commits: int) -> dict[str, Any]:
        if commits <= 0:
            return {"enabled": False}
        raw = self._git(
            "log", "--name-only", f"--max-count={commits}", "--pretty=format:%H"
        )
        if not raw:
            return {"enabled": False}
        file_counts: dict[str, int] = {}
        pair_counts: dict[tuple[str, str], int] = {}
        current: list[str] = []

        def flush() -> None:
            bounded = current[:20]
            for index, first in enumerate(bounded):
                file_counts[first] = file_counts.get(first, 0) + 1
                for second in bounded[index + 1 :]:
                    key = (first, second) if first < second else (second, first)
                    pair_counts[key] = pair_counts.get(key, 0) + 1

        for line in raw.splitlines():
            stripped = line.strip()
            if _COMMIT_LINE.fullmatch(stripped):
                flush()
                current = []
            elif stripped:
                current.append(stripped.replace("\\", "/"))
        flush()
        hot_files = [
            {"path": path, "commits": count}
            for path, count in sorted(
                file_counts.items(), key=lambda kv: (-kv[1], kv[0])
            )[:10]
        ]
        pairs = [
            {"paths": list(key), "count": count}
            for key, count in sorted(
                pair_counts.items(), key=lambda kv: (-kv[1], kv[0])
            )[:10]
        ]
        return {
            "enabled": True,
            "commits_scanned": commits,
            "hot_files": hot_files,
            "co_change_pairs": pairs,
        }

    def _validate_sources(self, facts: dict[str, Any]) -> dict[str, Any]:
        """VALID -> STALE fact validation against current file hashes."""

        sources = facts.get("sources", {})
        stale: list[str] = []
        missing: list[str] = []
        valid = 0
        if isinstance(sources, dict):
            for relative, digest in sorted(sources.items()):
                path = self.repository / str(relative)
                if not path.is_file():
                    missing.append(str(relative))
                    continue
                try:
                    current = _sha256_file(path)
                except OSError:
                    missing.append(str(relative))
                    continue
                if current == digest:
                    valid += 1
                else:
                    stale.append(str(relative))
        state = "VALID" if not stale and not missing else "STALE"
        return {"state": state, "valid": valid, "stale": stale, "missing": missing}

    # -- building ----------------------------------------------------------

    def _engine(self) -> RepositoryContextEngine:
        return RepositoryContextEngine(
            self.repository,
            ArtifactStore(self.session_id),
            policy_profile="workspace_write",
        )

    @staticmethod
    def _paths(items: Any, limit: int) -> list[str]:
        found: list[str] = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    path = item.get("path")
                    if isinstance(path, str) and path:
                        found.append(path)
                if len(found) >= limit:
                    break
        return found

    def build(
        self, level: object, *, goal: Optional[str] = None
    ) -> dict[str, Any]:
        """Build or refresh the durable map at ``level`` and measure it."""

        contract = level_contract(level)
        previous = self.load()
        started = time.perf_counter()
        current_revision = self.revision()
        warm = bool(
            previous
            and previous.get("revision") == current_revision
            and previous.get("level") == contract.level
        )
        changed = self._changed_files(previous)
        facts = (
            self.fact_map.refresh(changed)
            if changed is not None
            else self.fact_map.build()
        )

        inspection_summary: Optional[dict[str, Any]] = None
        implementation: list[str] = []
        tests: list[str] = []
        docs: list[str] = []
        second_pass: Optional[dict[str, Any]] = None
        if contract.engine_depth is not None:
            goal_text = (goal or "").strip() or (
                "project overview architecture entrypoints tests "
                f"for {self.repository.name}"
            )
            try:
                engine = self._engine()
                inspection = engine.inspect(
                    goal_text,
                    contract.engine_depth,
                    include_dependency_hints=contract.dependency_hints,
                )
                implementation = self._paths(
                    inspection.get("likely_change_points"), 12
                )
                tests = self._paths(inspection.get("tests"), 8)
                docs = self._paths(inspection.get("docs"), 4)
                metrics = inspection.get("metrics")
                inspection_summary = {
                    "goal": goal_text,
                    "depth": contract.engine_depth,
                    "metrics": metrics if isinstance(metrics, dict) else {},
                }
                if contract.passes > 1 and implementation:
                    second_goal = " ".join(
                        Path(item).stem for item in implementation[:6]
                    )
                    second = engine.inspect(
                        second_goal or goal_text,
                        "deep",
                        include_dependency_hints=True,
                    )
                    second_metrics = second.get("metrics")
                    second_pass = {
                        "goal": second_goal or goal_text,
                        "metrics": (
                            second_metrics
                            if isinstance(second_metrics, dict)
                            else {}
                        ),
                        "additional_change_points": [
                            path
                            for path in self._paths(
                                second.get("likely_change_points"), 12
                            )
                            if path not in implementation
                        ][:8],
                    }

            except Exception as exc:
                # A repository the engine cannot inspect (no usable git,
                # no ripgrep, a permission wall) still deserves its
                # structural map. The gap is recorded, not papered over.
                inspection_summary = {
                    "goal": goal_text,
                    "depth": contract.engine_depth,
                    "error": type(exc).__name__,
                }
        git_evidence = self._git_cochange(contract.git_history_commits)
        validation = (
            self._validate_sources(facts)
            if contract.validates_sources
            else None
        )
        if contract.passes > 1 and contract.validates_sources:
            # Multi-pass validation: the second inspection pass may have taken
            # real time; re-checking keeps the recorded state honest at save.
            validation = self._validate_sources(facts)

        # Map refresh participates in memory invalidation: source-backed
        # memory entries are re-hashed against the repository the map just
        # walked, so stale architecture memory cannot stay authoritative.
        memory_invalidation: Optional[dict[str, Any]] = None
        try:
            from .memory import KaroXMemory
            from .paths import session_dir

            memory_home = (
                self.memory_root
                if self.memory_root is not None
                else session_dir() / "memory"
            )
            if memory_home.exists():
                memory_invalidation = KaroXMemory(
                    memory_home
                ).revalidate_sources(repository=self.repository)
        except Exception as exc:
            memory_invalidation = {"error": type(exc).__name__}

        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        measurements: dict[str, Any] = (
            dict(previous.get("measurements", {})) if previous else {}
        )
        bucket = dict(measurements.get(contract.level, {}))
        kind = "warm_ms" if warm else "cold_ms"
        values = list(bucket.get(kind, []))
        values.append(duration_ms)
        bucket[kind] = values[-5:]
        if changed is not None and len(changed) == 1:
            single = list(bucket.get("single_file_ms", []))
            single.append(duration_ms)
            bucket["single_file_ms"] = single[-5:]
        measurements[contract.level] = bucket

        top_dirs = facts.get("important_dirs")
        state: dict[str, Any] = {
            "schema_version": MAP_SCHEMA_VERSION,
            "repository": str(self.repository),
            "level": contract.level,
            "contract": contract.to_dict(),
            "built_at": time.time(),
            "duration_ms": duration_ms,
            "warm": warm,
            "revision": current_revision,
            "git_head": self._git("rev-parse", "--verify", "HEAD"),
            "files_scanned": facts.get("files_scanned", 0),
            "regions": top_dirs if isinstance(top_dirs, dict) else {},
            "implementation": implementation,
            "tests": tests,
            "docs": docs,
            "inspection": inspection_summary,
            "second_pass": second_pass,
            "git_evidence": git_evidence,
            "validation": validation,
            "memory_invalidation": memory_invalidation,
            "changed_files_seen": changed if changed is not None else None,
            "measurements": measurements,
        }
        self._save(state)
        return state

    # -- reading -----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        stored = self.load()
        current = self.revision()
        result: dict[str, Any] = {
            "exists": stored is not None,
            "repository": str(self.repository),
            "revision_current": current,
        }
        if stored is not None:
            built_at = float(stored.get("built_at") or 0.0)
            result.update(
                {
                    "level": stored.get("level"),
                    "built_at": built_at,
                    "age_seconds": round(max(0.0, time.time() - built_at), 1),
                    "duration_ms": stored.get("duration_ms"),
                    "warm": stored.get("warm"),
                    "fresh": stored.get("revision") == current,
                    "files_scanned": stored.get("files_scanned"),
                    "validation": stored.get("validation"),
                    "measurements": stored.get("measurements", {}),
                }
            )
        facts = self.fact_map.load()
        if facts is not None:
            result["sources"] = self._validate_sources(facts)
        return result

    def preview(self, level: object) -> dict[str, Any]:
        """Honest cost estimate for building the map at ``level``."""

        contract = level_contract(level)
        facts = self.fact_map.load()
        if facts is not None:
            files = int(facts.get("files_scanned", 0) or 0)
            files_basis = "measured"
        else:
            files = len(self._iter_files())
            files_basis = "counted"
        stored = self.load()
        measured: list[float] = []
        if stored is not None:
            bucket = stored.get("measurements", {}).get(contract.level, {})
            for kind in ("cold_ms", "warm_ms"):
                for value in bucket.get(kind, []):
                    if isinstance(value, (int, float)):
                        measured.append(float(value))
        if measured:
            low = round(min(measured) * 0.8 / 1000, 1)
            high = round(max(measured) * 1.5 / 1000, 1)
            duration = {
                "seconds": [max(low, 0.1), max(high, 0.2)],
                "basis": "measured",
            }
        else:
            fallback = _FALLBACK_RANGES[contract.level]
            duration = {
                "seconds": [fallback[0], fallback[1]],
                "basis": "estimate",
            }
        inspect_low, inspect_high = _INSPECTION_RANGES[contract.level]
        return {
            "repository": str(self.repository),
            "level": contract.level,
            "contract": contract.to_dict(),
            "files_indexed": files,
            "files_basis": files_basis,
            "deep_inspection_files": [
                min(files, inspect_low),
                min(files, inspect_high),
            ],
            "semantic_analysis": contract.engine_depth is not None,
            "git_history": contract.git_history_commits > 0,
            "git_history_commits": contract.git_history_commits,
            "duration_range": duration,
        }

    def digest(self, *, budget_chars: int = 1200) -> str:
        """Compact prompt-ready digest: the map without the project dump."""

        parts: list[str] = []
        summary = self.fact_map.summary(budget_chars=600)
        if summary:
            parts.append(summary)
        stored = self.load()
        if stored is not None:
            lines = [
                (
                    f"Map (level {stored.get('level')}, "
                    f"revision {str(stored.get('revision'))[:16]}):"
                )
            ]
            implementation = stored.get("implementation")
            if isinstance(implementation, list) and implementation:
                lines.append(
                    "- likely change points: "
                    + ", ".join(str(item) for item in implementation[:8])
                )
            tests = stored.get("tests")
            if isinstance(tests, list) and tests:
                lines.append(
                    "- tests: " + ", ".join(str(item) for item in tests[:5])
                )
            regions = stored.get("regions")
            if isinstance(regions, dict) and regions:
                lines.append(
                    "- regions: " + ", ".join(list(regions)[:8])
                )
            validation = stored.get("validation")
            if isinstance(validation, dict):
                lines.append(
                    f"- fact validation: {validation.get('state')} "
                    f"({len(validation.get('stale', []))} stale)"
                )
            parts.append("\n".join(lines))
        return "\n\n".join(parts)[:budget_chars]


def stored_map_digest(
    repository: Path,
    *,
    root: Optional[Path] = None,
    budget_chars: int = 1200,
) -> Optional[tuple[str, dict[str, Any]]]:
    """The stored map's digest and honest metadata, or None without a map.

    Cheap by contract: no engine run, no fact rebuild -- one state read plus
    a revision probe. This is the hook ``discover_project_context`` uses so
    every agent run starts from the durable map when one exists.
    """

    try:
        service = MapService(repository, root=root)
    except (OSError, ValueError):
        return None
    stored = service.load()
    if stored is None:
        return None
    current = service.revision()
    fresh = stored.get("revision") == current
    built_at = float(stored.get("built_at") or 0.0)
    meta: dict[str, Any] = {
        "enabled": True,
        "level": stored.get("level"),
        "fresh": fresh,
        "age_seconds": round(max(0.0, time.time() - built_at), 1),
    }
    validation = stored.get("validation")
    if isinstance(validation, dict):
        meta["validation_state"] = validation.get("state")
    text = service.digest(budget_chars=budget_chars)
    if not text:
        return None
    freshness = "fresh" if fresh else "stale revision"
    rendered = (
        f"Stored project map (level {stored.get('level')}, {freshness}):\n"
        f"{text}"
    )
    return rendered, meta


_MAP_LEVEL_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "en": {"low": "quick structure scan", "medium": "standard project overview", "high": "deeper dependency map", "extra-high": "deep map with source validation", "ultra": "maximum depth with a second validation pass"},
    "ru": {"low": "быстрый обзор структуры", "medium": "обычная карта проекта", "high": "глубже по зависимостям", "extra-high": "глубокая карта с проверкой источников", "ultra": "максимальная глубина со вторым проходом проверки"},
}


def _map_level_description(level: object, language: str) -> str:
    key = str(level or "medium")
    return _MAP_LEVEL_DESCRIPTIONS["ru" if language == "ru" else "en"].get(key, key)


def render_status(status: dict[str, Any], language: str = "en", *, details: bool = False) -> str:
    """Human-first /map status; internal provenance is opt-in via ``details``."""
    russian = language == "ru"
    if not status.get("exists"):
        return "Карта проекта: ещё не построена." if russian else "Project map: not built yet."
    level = status.get("level")
    fresh = bool(status.get("fresh"))
    files = status.get("files_scanned")
    state = (("готова" if fresh else "нужно обновить") if russian else ("ready" if fresh else "needs refresh"))
    lines = [f"Карта проекта: {state} · {level} · {files} файлов" if russian else f"Project map: {state} · {level} · {files} files", _map_level_description(level, language)]
    if not details:
        return "\n".join(lines)
    age = status.get("age_seconds"); duration = status.get("duration_ms")
    lines.append(f"Технические детали: возраст {age} с · построена за {duration} мс" if russian else f"Technical details: age {age}s · built in {duration} ms")
    validation = status.get("validation")
    if isinstance(validation, dict):
        stale = len(validation.get("stale", []))
        lines.append(f"Проверка фактов: {validation.get('state')} · устаревших {stale}" if russian else f"Fact validation: {validation.get('state')} · {stale} stale")
    sources = status.get("sources")
    if isinstance(sources, dict):
        stale_sources = sources.get("stale", [])
        lines.append((f"Источники: {sources.get('state')}" + (f" · устарели: {', '.join(stale_sources[:5])}" if stale_sources else "")) if russian else (f"Sources: {sources.get('state')}" + (f" · stale: {', '.join(stale_sources[:5])}" if stale_sources else "")))
    return "\n".join(lines)


def render_preview(preview: dict[str, Any], language: str = "en", *, details: bool = False) -> str:
    """Human-first map estimate; engine internals are hidden unless requested."""
    russian = language == "ru"
    level = preview.get("level"); files = preview.get("files_indexed")
    duration = preview.get("duration_range", {})
    seconds = duration.get("seconds", [0, 0]) if isinstance(duration, dict) else [0, 0]
    if not isinstance(seconds, list) or len(seconds) < 2:
        seconds = [0, 0]
    basis = duration.get("basis", "estimate") if isinstance(duration, dict) else "estimate"
    basis_text = (("по измерениям на этом ПК" if basis == "measured" else "оценка") if russian else ("measured on this machine" if basis == "measured" else "estimate"))
    lines = [f"Карта проекта: {level}" if russian else f"Project map preview: {level}", _map_level_description(level, language), f"Охват: {files} файлов" if russian else f"Scope: {files} files", f"Ожидаемое время: {seconds[0]}–{seconds[1]} с · {basis_text}" if russian else f"Expected time: {seconds[0]}–{seconds[1]}s · {basis_text}"]
    if not details:
        return "\n".join(lines)
    deep = preview.get("deep_inspection_files", [0, 0])
    lines.extend([f"Проект: {preview.get('repository')}" if russian else f"Project: {preview.get('repository')}", f"Глубокая инспекция: {deep[0]}–{deep[1]} файлов" if russian else f"Deep inspection: {deep[0]}–{deep[1]} files", ("Семантический анализ: " + ("да" if preview.get("semantic_analysis") else "нет")) if russian else ("Semantic analysis: " + ("yes" if preview.get("semantic_analysis") else "no")), ("История Git: " + (f"да, {preview.get('git_history_commits')} коммитов" if preview.get("git_history") else "нет")) if russian else ("Git history: " + (f"yes, {preview.get('git_history_commits')} commits" if preview.get("git_history") else "no"))])
    return "\n".join(lines)
