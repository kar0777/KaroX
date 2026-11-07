"""Skill, visual, disk and patch tools for the extended Core runtime."""

from __future__ import annotations

import hashlib
from typing import Any, Dict
from .core import (
    CoreError,
    InvalidCommand,
)
from .hot_worker import hot_worker_supervisor


VISUAL_CHECK_SUFFIXES = frozenset({".html", ".htm", ".svg"})


class CoreSkillToolsMixin:
    """Methods composed into ExtendedCoreRuntime; see core_tools.py."""

    def _repo_inspect(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        del deadline_seconds
        goal = self._required(arguments, "goal", str).strip()
        if not goal:
            raise InvalidCommand("repo.inspect goal must be a non-empty string")
        depth = arguments.get("depth", "focused")
        if depth not in {"focused", "standard", "deep"}:
            raise InvalidCommand(
                "repo.inspect depth must be focused, standard, or deep"
            )

        from .artifacts import ArtifactStore
        from .repo_context import RepositoryContextEngine

        namespace = "core-repo-inspect-" + hashlib.sha256(
            str(self.repository).encode("utf-8")
        ).hexdigest()[:16]
        engine = RepositoryContextEngine(
            self.repository,
            ArtifactStore(namespace),
            policy_profile=self.policy.profile.value,
        )
        return dict(engine.inspect(goal, str(depth)))

    def _require_skill_catalog(self) -> Any:
        catalog = self._skill_catalog
        if catalog is None:
            raise CoreError("skill recipes are not available in this run")
        return catalog

    def _skill_list(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        del arguments, deadline_seconds
        catalog = self._require_skill_catalog()
        return {
            "skills": [
                {
                    "name": item.name,
                    "description": item.description,
                    "when_to_use": item.when_to_use or "",
                    "source_kind": item.source_kind,
                }
                for item in catalog.discover()
            ]
        }

    def _skill_load(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        del deadline_seconds
        catalog = self._require_skill_catalog()
        content = catalog.load(self._required(arguments, "name", str))
        return {
            "name": content.metadata.name,
            "version": content.metadata.version,
            "source_kind": content.metadata.source_kind,
            "content_sha256": content.metadata.content_sha256,
            "instructions": content.instructions,
            # Names and sizes only: the recipe is the payload, and a reference
            # that is not needed this step must not be paid for.
            "files": [
                {"path": path, "bytes": len(value.encode("utf-8"))}
                for path, value in sorted(content.references.items())
            ],
        }

    def _skill_read(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        del deadline_seconds
        catalog = self._require_skill_catalog()
        name = self._required(arguments, "name", str)
        path = self._required(arguments, "path", str)
        text, digest = catalog.read_reference(name, path)
        return {
            "name": name,
            "path": path,
            "sha256": digest,
            "characters": len(text),
            "content": text,
        }

    def _visual_check(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        """One look at one page, judged in a context that is not this one.

        The page must be one this run wrote, so the path is confined exactly
        like every other repository read. A remote URL is deliberately not
        accepted: fetching an arbitrary host is a network capability the Build
        profile does not hold, and the page a check is about is the page in the
        working tree.
        """

        del deadline_seconds
        checker = self._visual_checker
        if checker is None:
            raise CoreError("visual checks are not available in this run")
        relative = self._required(arguments, "path", str)
        target = self.safe_path(relative)
        if not target.is_file():
            raise FileNotFoundError(target)
        if target.suffix.lower() not in VISUAL_CHECK_SUFFIXES:
            raise InvalidCommand(
                "visual_check looks at a rendered page or image; give an "
                ".html or .svg file"
            )
        goal = arguments.get("goal")
        if goal is not None and not isinstance(goal, str):
            raise InvalidCommand("goal must be a string")
        return dict(checker(target, goal=goal or ""))

    def _disk_scan(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        from .disk_maintenance import scan_cleanup_candidates

        max_candidates = arguments.get("max_candidates", 24)
        min_size_mb = arguments.get("min_size_mb", 25.0)
        scan_seconds = arguments.get("scan_seconds", min(8.0, deadline_seconds))
        return scan_cleanup_candidates(
            self.repository,
            max_candidates=int(max_candidates),
            min_size_mb=float(min_size_mb),
            scan_seconds=min(float(scan_seconds), float(deadline_seconds)),
            protected_roots=self._maintenance_protected_paths,
        )

    def _disk_plan_cleanup(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        del deadline_seconds
        from .disk_maintenance import create_cleanup_plan

        paths = arguments.get("paths")
        if not isinstance(paths, list) or not paths or not all(
            isinstance(item, str) for item in paths
        ):
            raise InvalidCommand("disk.plan_cleanup paths must be a non-empty string array")
        return create_cleanup_plan(
            self.repository,
            paths,
            protected_roots=self._maintenance_protected_paths,
        )

    def _apply_patch(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        """First-class unified-patch facade over the transactional repo worker."""
        result = hot_worker_supervisor().execute_repo(
            self,
            {"action": "apply_patch", "payload": dict(arguments)},
            deadline_seconds,
        )
        changed_files = result.get("changed_files")
        if result.get("dry_run") is True:
            result["changed"] = False
        elif isinstance(changed_files, list):
            result["changed"] = bool(changed_files)
        if isinstance(changed_files, list):
            if len(changed_files) == 1 and isinstance(changed_files[0], str):
                result["path"] = changed_files[0]
        return result

    def _prepare_apply_patch(self, arguments: Dict[str, Any], deadline_seconds: float) -> None:
        self._prepare_repo_command(
            {"action": "apply_patch", "payload": dict(arguments)}, deadline_seconds
        )
