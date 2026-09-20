"""Deterministic project fact map: local, cheap, evidence-backed onboarding.

`karox init` and the hosted bootstrap both need the same answer: what is this
project, how is it built, where are its tests, and what instructions did its
authors already write down. This module computes that answer locally with no
model call, records the source files each fact came from (path + sha256), and
refreshes incrementally: a changed file invalidates only the facts it backs.

The semantic map is deliberately somewhere else: a strong model may later
summarize THIS compressed structure, instead of reading the repository raw.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

FACT_MAP_SCHEMA_VERSION = 1

# Instruction files other agent runtimes already understand. Read, never
# copied: their presence and content belong to the project's authors.
INSTRUCTION_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "CLAUDE.local.md",
    "GEMINI.md",
    ".cursorrules",
    ".windsurfrules",
    "README.md",
    "README_RU.md",
)

_CONFIG_FILES = (
    "pyproject.toml",
    "package.json",
    "requirements.txt",
    "Cargo.toml",
    "go.mod",
    "Makefile",
    "setup.py",
    "setup.cfg",
)

_LANGUAGE_EXTENSIONS = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".rb": "ruby",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".sh": "shell",
    ".ps1": "powershell",
    ".md": "markdown",
    ".toml": "config",
    ".yaml": "config",
    ".yml": "config",
    ".json": "config",
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
    }
)

_MAX_FILES_SCANNED = 20000


# These files affect config-derived facts even though they are not parsed.
_CONFIG_DEPENDENCIES = frozenset({"uv.lock", "poetry.lock", "pnpm-lock.yaml", "yarn.lock"})


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_source(path: Path) -> tuple[str, str]:
    """Hash and decode the same read, keeping evidence and facts consistent."""
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    # Match read_text's universal-newline and replacement-decoding behavior,
    # but decode the bytes already hashed instead of reopening the source.
    with io.TextIOWrapper(io.BytesIO(content), encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    return digest, text


@dataclasses.dataclass(frozen=True)
class SourceFact:
    """One fact plus the evidence file it was derived from."""

    value: Any
    source_path: str
    source_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class ProjectFactMap:
    """Compute, persist, and incrementally refresh a project's fact map."""

    def __init__(self, repository: Path, storage: Path) -> None:
        self.repository = Path(repository).expanduser().resolve(strict=True)
        self.storage = Path(storage)
        self.storage.parent.mkdir(parents=True, exist_ok=True)

    # -- building ----------------------------------------------------------

    def build(self) -> dict[str, Any]:
        languages: dict[str, int] = {}
        top_dirs: dict[str, int] = {}
        test_files = 0
        scanned = 0
        for root, dirs, files in os.walk(self.repository):
            dirs[:] = [item for item in dirs if item not in _SKIP_DIRS]
            relative_root = Path(root).relative_to(self.repository)
            for name in files:
                scanned += 1
                if scanned > _MAX_FILES_SCANNED:
                    break
                suffix = Path(name).suffix.lower()
                language = _LANGUAGE_EXTENSIONS.get(suffix)
                if language:
                    languages[language] = languages.get(language, 0) + 1
                anchor = (
                    relative_root.parts[0]
                    if relative_root.parts
                    else "."
                )
                top_dirs[anchor] = top_dirs.get(anchor, 0) + 1
                lowered = name.lower()
                if lowered.startswith("test_") or lowered.endswith("_test.py"):
                    test_files += 1
            if scanned > _MAX_FILES_SCANNED:
                break

        facts: dict[str, Any] = {
            "schema_version": FACT_MAP_SCHEMA_VERSION,
            "repository": str(self.repository),
            "generated_at": time.time(),
            "files_scanned": scanned,
            "languages": dict(
                sorted(languages.items(), key=lambda item: -item[1])[:10]
            ),
            "important_dirs": dict(
                sorted(top_dirs.items(), key=lambda item: -item[1])[:12]
            ),
            "test_files": test_files,
            "sources": {},
            "project": {},
            "instructions": {},
            "git": self._git_facts(),
        }
        self._apply_config_facts(facts)
        self._apply_instruction_facts(facts)
        self._save(facts)
        return facts

    # -- incremental refresh -------------------------------------------------

    def refresh(self, changed_files: Iterable[str]) -> dict[str, Any]:
        """Refresh only the facts whose source files changed.

        Unknown or missing maps rebuild fully; a change that touches no fact
        source returns the stored map untouched (cheap no-op).
        """

        stored = self.load()
        if stored is None:
            return self.build()
        changed = {str(item).replace("\\", "/") for item in changed_files}
        sources = stored.get("sources", {})
        # Include absent sources: a newly-created instruction or lockfile is
        # not in the previous map's evidence, but still invalidates its facts.
        affected = changed.intersection(
            set(sources) | set(_CONFIG_FILES) | set(INSTRUCTION_FILES) | _CONFIG_DEPENDENCIES
        )
        code_changed = any(
            Path(item).suffix.lower() in _LANGUAGE_EXTENSIONS for item in changed
        )
        if not affected and not code_changed:
            return stored
        # Config or instruction sources changed -> selective refresh; code
        # volume changes -> full rebuild (histograms are whole-tree facts).
        if code_changed:
            return self.build()
        self._apply_config_facts(stored)
        self._apply_instruction_facts(stored)
        stored["generated_at"] = time.time()
        self._save(stored)
        return stored

    def load(self) -> Optional[dict[str, Any]]:
        if not self.storage.exists():
            return None
        try:
            payload = json.loads(self.storage.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != FACT_MAP_SCHEMA_VERSION:
            return None
        if payload.get("repository") != str(self.repository):
            return None
        return payload

    def summary(self, *, budget_chars: int = 900) -> str:
        """A compact prompt-ready digest of the fact map."""

        facts = self.load()
        if facts is None:
            return ""
        project = facts.get("project", {})
        lines = ["Project fact map (local, deterministic):"]
        name = project.get("name", {}).get("value")
        if name:
            lines.append(f"- project: {name}")
        languages = ", ".join(list(facts.get("languages", {}))[:5])
        if languages:
            lines.append(f"- languages: {languages}")
        for label in ("build_commands", "test_command", "package_manager", "entrypoints"):
            item = project.get(label)
            if item and item.get("value"):
                lines.append(f"- {label}: {json.dumps(item['value'], ensure_ascii=False)}")
        instructions = [
            path for path, meta in facts.get("instructions", {}).items()
            if meta.get("present")
        ]
        if instructions:
            lines.append(f"- instructions: {', '.join(sorted(instructions))}")
        text = "\n".join(lines)
        return text[:budget_chars]

    # -- fact extraction -----------------------------------------------------

    def _record_source(self, facts: dict[str, Any], relative: str, digest: str) -> None:
        facts.setdefault("sources", {})[relative] = digest

    def _apply_config_facts(self, facts: dict[str, Any]) -> None:
        # Re-extract this small section from scratch so deleted fields and
        # setdefault-based precedence cannot retain facts from an older source.
        project: dict[str, Any] = {}
        facts["project"] = project
        for name in _CONFIG_FILES:
            facts.setdefault("sources", {}).pop(name, None)
            path = self.repository / name
            if not path.is_file():
                continue
            # Hash-only sources stay streamed; parsed sources share one read
            # between their digest and extracted facts.
            if name in {"pyproject.toml", "package.json"}:
                digest, text = _read_source(path)
            else:
                digest, text = _sha256_file(path), ""
            self._record_source(facts, name, digest)
            if name == "pyproject.toml":
                self._facts_from_pyproject(project, path, digest, text)
            elif name == "package.json":
                self._facts_from_package_json(project, path, digest, text)
            elif name == "go.mod":
                project["project_type"] = SourceFact("go", name, digest).to_dict()
            elif name == "Cargo.toml":
                project["project_type"] = SourceFact("rust", name, digest).to_dict()

    def _facts_from_pyproject(
        self, project: dict[str, Any], path: Path, digest: str, text: str
    ) -> None:
        relative = path.name
        project["project_type"] = SourceFact("python", relative, digest).to_dict()
        name_match = re.search(r'(?m)^name\s*=\s*"([^"]+)"', text)
        if name_match:
            project["name"] = SourceFact(name_match.group(1), relative, digest).to_dict()
        if "[build-system]" in text:
            project["build_commands"] = SourceFact(
                ["python -m build --wheel"], relative, digest
            ).to_dict()
        if re.search(r"(?m)^\[tool\.pytest", text) or (self.repository / "tests").is_dir():
            project["test_command"] = SourceFact(
                "python -m pytest", relative, digest
            ).to_dict()
        manager = "pip"
        if (self.repository / "uv.lock").is_file():
            manager = "uv"
        elif (self.repository / "poetry.lock").is_file():
            manager = "poetry"
        project["package_manager"] = SourceFact(manager, relative, digest).to_dict()
        scripts = re.search(
            r"(?ms)^\[project\.scripts\]\s*(.*?)(?=^\[|\Z)", text
        )
        if scripts:
            entrypoints = re.findall(r"(?m)^([A-Za-z0-9_-]+)\s*=", scripts.group(1))
            if entrypoints:
                project["entrypoints"] = SourceFact(
                    entrypoints[:10], relative, digest
                ).to_dict()

    def _facts_from_package_json(
        self, project: dict[str, Any], path: Path, digest: str, text: str
    ) -> None:
        relative = path.name
        try:
            payload = json.loads(text)
        except ValueError:
            return
        project.setdefault(
            "project_type", SourceFact("node", relative, digest).to_dict()
        )
        if isinstance(payload.get("name"), str):
            project.setdefault(
                "name", SourceFact(payload["name"], relative, digest).to_dict()
            )
        scripts = payload.get("scripts")
        if isinstance(scripts, dict) and scripts:
            project["build_commands"] = SourceFact(
                [f"npm run {key}" for key in list(scripts)[:8]], relative, digest
            ).to_dict()
        manager = "npm"
        if (self.repository / "pnpm-lock.yaml").is_file():
            manager = "pnpm"
        elif (self.repository / "yarn.lock").is_file():
            manager = "yarn"
        project.setdefault(
            "package_manager", SourceFact(manager, relative, digest).to_dict()
        )

    def _apply_instruction_facts(self, facts: dict[str, Any]) -> None:
        instructions: dict[str, Any] = facts.setdefault("instructions", {})
        for name in INSTRUCTION_FILES:
            facts.setdefault("sources", {}).pop(name, None)
            path = self.repository / name
            if not path.is_file():
                instructions[name] = {"present": False}
                continue
            digest, text = _read_source(path)
            self._record_source(facts, name, digest)
            instructions[name] = {
                "present": True,
                "bytes": len(text.encode("utf-8")),
                "sha256": digest,
                "head": text[:400],
            }

    def _git_facts(self) -> dict[str, Any]:
        head = self.repository / ".git" / "HEAD"
        branch: Optional[str] = None
        if head.is_file():
            content = head.read_text(encoding="utf-8", errors="replace").strip()
            if content.startswith("ref: refs/heads/"):
                branch = content[len("ref: refs/heads/"):]
        return {"branch": branch, "has_git": head.is_file()}

    def _save(self, facts: Mapping[str, Any]) -> None:
        temporary = self.storage.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(facts, ensure_ascii=False, sort_keys=True, indent=1),
            encoding="utf-8",
        )
        os.replace(temporary, self.storage)
