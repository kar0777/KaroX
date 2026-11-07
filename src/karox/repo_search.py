"""Repository file listing, grep/ripgrep, Python symbols and import hints."""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from .security import redact
from .repo_support import (  # noqa: F401
    REPO_INSPECT_SCHEMA_VERSION as REPO_INSPECT_SCHEMA_VERSION,
    _TEXT_SUFFIXES as _TEXT_SUFFIXES,
    _SYMBOL_SUFFIXES as _SYMBOL_SUFFIXES,
    _IGNORED_STATUS_COMPONENTS as _IGNORED_STATUS_COMPONENTS,
    _IGNORED_STATUS_PREFIXES as _IGNORED_STATUS_PREFIXES,
    _ignored_component as _ignored_component,
    _GENERIC_SYMBOL as _GENERIC_SYMBOL,
    _STOPWORDS as _STOPWORDS,
    InspectBudget as InspectBudget,
    _BUDGETS as _BUDGETS,
    _EDIT_CONTEXT_MAX_BYTES as _EDIT_CONTEXT_MAX_BYTES,
    _EDIT_CONTEXT_MAX_LINES as _EDIT_CONTEXT_MAX_LINES,
    _per_file_match_limit as _per_file_match_limit,
    _identifier_tokens as _identifier_tokens,
    _path_relevance as _path_relevance,
    _content_search_tokens as _content_search_tokens,
    _raw_match_limit as _raw_match_limit,
    _select_diverse_matches as _select_diverse_matches,
    _run as _run,
    _tokens as _tokens,
    _safe_relative as _safe_relative,
    _status_entries as _status_entries,
    _read_lines as _read_lines,
    _json_atomic as _json_atomic,
)


class RepoSearchMixin:
    """Methods composed into RepositoryContextEngine; see repo_context.py."""

    def _files(self, budget: InspectBudget) -> list[str]:
        if not self.is_git_repository:
            files: list[str] = []
            stack = [self.repository]
            max_candidates = max(1_000, budget.max_files * 50)
            considered = 0
            while stack and considered < max_candidates:
                current = stack.pop()
                try:
                    entries = sorted(
                        os.scandir(current), key=lambda item: os.path.normcase(item.name)
                    )
                except OSError:
                    continue
                child_directories: list[Path] = []
                for entry in entries:
                    if considered >= max_candidates:
                        break
                    child = Path(entry.path)
                    try:
                        relative = child.relative_to(self.repository).as_posix()
                        is_symlink = entry.is_symlink()
                        is_directory = entry.is_dir(follow_symlinks=False) and not is_symlink
                    except (OSError, ValueError):
                        continue
                    if any(
                        _ignored_component(part)
                        or part.lower().endswith(".egg-info")
                        for part in Path(relative).parts
                    ):
                        continue
                    considered += 1
                    if is_directory:
                        child_directories.append(child)
                        continue
                    if is_symlink or child.suffix.lower() not in _TEXT_SUFFIXES:
                        continue
                    try:
                        if entry.stat(follow_symlinks=False).st_size > budget.max_file_bytes:
                            continue
                    except OSError:
                        continue
                    files.append(relative)
                stack.extend(reversed(child_directories))
            return sorted(dict.fromkeys(files))
        listed = self._git("ls-files", "-co", "--exclude-standard")
        git_files: list[str] = []
        for raw in listed.splitlines():
            git_relative = _safe_relative(self.repository, raw)
            if git_relative is None:
                continue
            path = self.repository / git_relative
            if path.suffix.lower() not in _TEXT_SUFFIXES or not path.is_file():
                continue
            try:
                if path.stat().st_size > budget.max_file_bytes:
                    continue
            except OSError:
                continue
            git_files.append(git_relative)
        return sorted(dict.fromkeys(git_files))

    def _git_grep_search(
        self,
        tokens: tuple[str, ...],
        budget: InspectBudget,
    ) -> Optional[list[dict[str, Any]]]:
        """Use Git to shortlist token-matching files when ripgrep is unavailable."""
        if not self.is_git_repository:
            return None
        if not tokens:
            return []
        argv = [
            "git",
            "-C",
            str(self.repository),
            "grep",
            "--untracked",
            "-I",
            "-l",
            "-z",
            "--full-name",
            "-F",
            "-i",
        ]
        for token in tokens:
            argv.extend(("-e", token))
        argv.append("--")
        completed = _run(
            self.repository,
            argv,
            timeout=45,
            allow_failure=True,
        )
        if completed.returncode == 1:
            return []
        if completed.returncode != 0:
            return None
        raw_candidates = (
            completed.stdout.split("\x00")
            if "\x00" in completed.stdout
            else completed.stdout.splitlines()
        )
        expression = re.compile(
            "|".join(re.escape(token) for token in _content_search_tokens(tokens)),
            re.IGNORECASE,
        )
        matches: list[dict[str, Any]] = []
        seen: set[str] = set()
        per_file_limit = _per_file_match_limit(budget)
        raw_limit = _raw_match_limit(budget)
        identifier_tokens = _identifier_tokens(tokens)
        for raw in raw_candidates:
            relative = _safe_relative(self.repository, raw.strip())
            if relative is None or relative in seen:
                continue
            seen.add(relative)
            path = self.repository / relative
            file_matches = 0
            for line_number, line in enumerate(
                _read_lines(path, budget.max_file_bytes), start=1
            ):
                if not expression.search(line):
                    continue
                lowered_line = line.lower()
                identifier_hit = any(token in lowered_line for token in identifier_tokens)
                if file_matches >= per_file_limit and not identifier_hit:
                    continue
                matches.append(
                    {
                        "path": relative,
                        "line": line_number,
                        "text": str(redact(line))[:1000],
                    }
                )
                file_matches += 1
                if len(matches) >= raw_limit:
                    return _select_diverse_matches(matches, tokens, budget)
        return _select_diverse_matches(matches, tokens, budget)

    def _targeted_identifier_matches(
        self,
        matches: Iterable[Mapping[str, Any]],
        tokens: tuple[str, ...],
        budget: InspectBudget,
    ) -> list[dict[str, Any]]:
        """Recover late exact identifiers from already relevant files only.

        A second repository-wide search roughly doubled search latency on large
        Windows worktrees. The broad pass already tells us which files are
        semantically relevant, so exact snake/dotted/hyphen identifiers are
        refined inside a small ranked subset instead.
        """
        identifiers = _identifier_tokens(tokens)
        if not identifiers:
            return []
        groups: dict[str, dict[str, Any]] = {}
        for item in matches:
            path = str(item.get("path", ""))
            if not path:
                continue
            group = groups.setdefault(path, {"coverage": set(), "count": 0})
            group["count"] = int(group["count"]) + 1
            lowered = str(item.get("text", "")).lower()
            coverage = group["coverage"]
            if isinstance(coverage, set):
                coverage.update(token for token in tokens if token in lowered)

        source_roots = ("src/", "lib/", "app/", "pkg/", "cmd/", "internal/", "packages/")

        def priority(item: tuple[str, dict[str, Any]]) -> tuple[float, str]:
            path, data = item
            lowered = path.lower()
            coverage = data.get("coverage")
            coverage_count = len(coverage) if isinstance(coverage, set) else 0
            score = sum(token in lowered for token in tokens) * 50.0
            score += coverage_count * 18.0 + min(int(data.get("count", 0)), 8)
            if path.startswith(source_roots):
                score += 36.0
            elif path.startswith("tests/"):
                score += 28.0
            if Path(path).suffix.lower() in _SYMBOL_SUFFIXES:
                score += 8.0
            return (-score, path)

        limit = min(80, max(20, budget.max_files * 2))
        candidates = [path for path, _data in sorted(groups.items(), key=priority)[:limit]]
        existing = {
            (str(item.get("path", "")), int(item.get("line", 0) or 0))
            for item in matches
        }

        def scan(relative: str) -> list[dict[str, Any]]:
            found: list[dict[str, Any]] = []
            for line_number, line in enumerate(
                _read_lines(self.repository / relative, budget.max_file_bytes), start=1
            ):
                lowered = line.lower()
                if not any(token in lowered for token in identifiers):
                    continue
                if (relative, line_number) in existing:
                    continue
                found.append(
                    {
                        "path": relative,
                        "line": line_number,
                        "text": str(redact(line))[:1000],
                    }
                )
                if len(found) >= 4:
                    break
            return found

        refined: list[dict[str, Any]] = []
        workers = min(8, max(1, len(candidates)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for found in pool.map(scan, candidates):
                refined.extend(found)
        return refined

    def _ripgrep(
        self,
        tokens: tuple[str, ...],
        budget: InspectBudget,
    ) -> list[dict[str, Any]]:
        if not tokens:
            return []
        rg = shutil.which("rg")
        if rg is None:
            git_matches = self._git_grep_search(tokens, budget)
            if git_matches is not None:
                return git_matches
            return self._python_search(tokens, budget)
        expression = "|".join(re.escape(token) for token in _content_search_tokens(tokens))
        completed = _run(
            self.repository,
            [
                rg,
                "--json",
                "--ignore-case",
                "--line-number",
                "--no-heading",
                "--max-count",
                str(_per_file_match_limit(budget)),
                "--glob",
                "!.git/**",
                "-e",
                expression,
                ".",
            ],
            timeout=45,
            allow_failure=True,
        )
        matches: list[dict[str, Any]] = []
        per_file_limit = _per_file_match_limit(budget)
        raw_limit = _raw_match_limit(budget)
        identifier_tokens = _identifier_tokens(tokens)
        per_file_counts: dict[str, int] = {}
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            path_text = ((data.get("path") or {}).get("text"))
            relative = _safe_relative(self.repository, str(path_text or ""))
            if relative is None:
                continue
            line_number = data.get("line_number")
            text = ((data.get("lines") or {}).get("text"))
            if not isinstance(line_number, int) or not isinstance(text, str):
                continue
            lowered_text = text.lower()
            identifier_hit = any(token in lowered_text for token in identifier_tokens)
            if per_file_counts.get(relative, 0) >= per_file_limit and not identifier_hit:
                continue
            matches.append(
                {
                    "path": relative,
                    "line": line_number,
                    "text": str(redact(text.rstrip("\r\n")))[:1000],
                }
            )
            per_file_counts[relative] = per_file_counts.get(relative, 0) + 1
            if len(matches) >= raw_limit:
                break

        if identifier_tokens:
            matches.extend(self._targeted_identifier_matches(matches, tokens, budget))
        return _select_diverse_matches(matches, tokens, budget)

    def _python_search(
        self,
        tokens: tuple[str, ...],
        budget: InspectBudget,
    ) -> list[dict[str, Any]]:
        expression = re.compile(
            "|".join(re.escape(token) for token in _content_search_tokens(tokens)),
            re.IGNORECASE,
        )
        matches: list[dict[str, Any]] = []
        per_file_limit = _per_file_match_limit(budget)
        raw_limit = _raw_match_limit(budget)
        identifier_tokens = _identifier_tokens(tokens)
        for relative in self._files(budget):
            file_matches = 0
            for line_number, line in enumerate(
                _read_lines(self.repository / relative, budget.max_file_bytes),
                start=1,
            ):
                if expression.search(line):
                    lowered_line = line.lower()
                    identifier_hit = any(token in lowered_line for token in identifier_tokens)
                    if file_matches >= per_file_limit and not identifier_hit:
                        continue
                    matches.append(
                        {"path": relative, "line": line_number, "text": str(redact(line))[:1000]}
                    )
                    file_matches += 1
                    if len(matches) >= raw_limit:
                        return _select_diverse_matches(matches, tokens, budget)
        return _select_diverse_matches(matches, tokens, budget)

    def _symbol_candidate_files(
        self,
        files: Iterable[str],
        matches: Iterable[Mapping[str, Any]],
        tokens: tuple[str, ...],
        budget: InspectBudget,
    ) -> list[str]:
        """Choose code files for symbol extraction before doing any AST work.

        Search results can be dominated by prose files that repeat generic task
        words. Symbols only exist in code, so parsing the first alphabetic match
        list wasted the focused budget and often produced zero definitions.
        """

        file_set = set(files)
        counts: dict[str, int] = {}
        coverage: dict[str, set[str]] = {}
        for item in matches:
            path = str(item.get("path", ""))
            if path not in file_set or Path(path).suffix.lower() not in _SYMBOL_SUFFIXES:
                continue
            counts[path] = counts.get(path, 0) + 1
            text = str(item.get("text", "")).lower()
            coverage.setdefault(path, set()).update(
                token for token in tokens if token in text or token in path.lower()
            )

        candidates = list(counts)
        if not candidates:
            candidates = [
                path
                for path in file_set
                if Path(path).suffix.lower() in _SYMBOL_SUFFIXES
                and any(token in path.lower() for token in tokens)
            ]

        preferred_roots = ("src/", "lib/", "app/", "pkg/", "cmd/", "internal/", "packages/")

        def priority(path: str) -> tuple[float, str]:
            path_hits = _path_relevance(path, tokens)
            score = path_hits * 30 + len(coverage.get(path, ())) * 10
            score += min(counts.get(path, 0), 8) * 2
            if path.startswith(preferred_roots):
                score += 36
            return (-float(score), path)

        return sorted(candidates, key=priority)[: budget.max_files]

    def _python_symbols(self, files: Iterable[str], tokens: tuple[str, ...]) -> dict[str, Any]:
        wanted = {token.lower().split(".")[-1] for token in tokens}
        definitions: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        imports: list[dict[str, Any]] = []
        types: list[dict[str, Any]] = []
        for relative in files:
            path = self.repository / relative
            if Path(relative).suffix.lower() not in {".py", ".pyi"}:
                for line_number, line in enumerate(_read_lines(path, 2_000_000), start=1):
                    for match in _GENERIC_SYMBOL.finditer(line):
                        name = match.group("name")
                        keyword = match.group("kind")
                        lowered_name = name.lower()
                        entry = {
                            "path": relative,
                            "line": line_number,
                            "name": name,
                            "kind": "type" if keyword in {"class", "interface", "type", "struct", "enum", "trait"} else "function",
                            "goal_match": (
                                not wanted
                                or lowered_name in wanted
                                or any(token in lowered_name for token in wanted)
                            ),
                        }
                        definitions.append(entry)
                        if entry["kind"] == "type":
                            types.append(entry)
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=relative)
            except (OSError, SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    lowered_name = node.name.lower()
                    entry = {
                        "path": relative,
                        "line": int(getattr(node, "lineno", 0)),
                        "name": node.name,
                        "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                        "goal_match": (
                            not wanted
                            or lowered_name in wanted
                            or any(token in lowered_name for token in wanted)
                        ),
                    }
                    definitions.append(entry)
                    if isinstance(node, ast.ClassDef):
                        types.append(entry)
                elif isinstance(node, ast.Call):
                    name = ""
                    if isinstance(node.func, ast.Name):
                        name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        name = node.func.attr
                    if name:
                        calls.append(
                            {
                                "path": relative,
                                "line": int(getattr(node, "lineno", 0)),
                                "name": name,
                                "goal_match": not wanted or name.lower() in wanted,
                            }
                        )
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append({"path": relative, "module": alias.name, "line": node.lineno})
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    imports.append({"path": relative, "module": module, "line": node.lineno})
        return {
            "definitions": definitions[:500],
            "calls": calls[:1000],
            "types": types[:300],
            "imports": imports[:1500],
        }

    def _lightweight_imports(
        self, files: Iterable[str], *, max_file_bytes: int
    ) -> list[dict[str, Any]]:
        """Index Python imports cheaply, independent of goal-ranked AST files.

        The fast path is one ``git grep`` over import lines. That keeps reverse
        caller discovery without reopening hundreds of files from Python. A
        bounded threaded scanner remains as a portability fallback when Git grep
        is unavailable or rejects the query.
        """

        python_files = [
            path for path in files if Path(path).suffix.lower() in {".py", ".pyi"}
        ]
        if not python_files:
            return []
        allowed = set(python_files)
        from_pattern = re.compile(r"^\s*from\s+([.A-Za-z_][.A-Za-z0-9_]*)\s+import\b")
        import_pattern = re.compile(r"^\s*import\s+(.+)$")

        def parse(relative: str, line_number: int, line: str) -> list[dict[str, Any]]:
            if relative not in allowed:
                return []
            match = from_pattern.match(line)
            if match is not None:
                module = match.group(1).strip().strip(".")
                return (
                    [{"path": relative, "module": module, "line": line_number}]
                    if module
                    else []
                )
            match = import_pattern.match(line)
            if match is None:
                return []
            result: list[dict[str, Any]] = []
            for raw in match.group(1).split(","):
                module = raw.strip().split()[0].strip().strip(".") if raw.strip() else ""
                if module:
                    result.append(
                        {"path": relative, "module": module, "line": line_number}
                    )
            return result

        collected: list[dict[str, Any]] = []
        indexed = False
        rg = shutil.which("rg")
        if rg is not None:
            completed = _run(
                self.repository,
                [
                    rg,
                    "--json",
                    "--line-number",
                    "--no-heading",
                    "--glob",
                    "*.py",
                    "--glob",
                    "*.pyi",
                    "-e",
                    r"^\s*(from\s+\S+\s+import|import\s+)",
                    ".",
                ],
                timeout=20.0,
                allow_failure=True,
            )
            if completed.returncode in {0, 1}:
                indexed = True
                for raw in completed.stdout.splitlines():
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") != "match":
                        continue
                    data = event.get("data") or {}
                    path_text = ((data.get("path") or {}).get("text"))
                    relative = _safe_relative(self.repository, str(path_text or ""))
                    line_number = data.get("line_number")
                    line = ((data.get("lines") or {}).get("text"))
                    if (
                        relative is None
                        or not isinstance(line_number, int)
                        or not isinstance(line, str)
                    ):
                        continue
                    collected.extend(parse(relative, line_number, line.rstrip("\r\n")))
                    if len(collected) >= 5_000:
                        break

        if not indexed:
            completed = _run(
                self.repository,
                [
                    "git",
                    "-C",
                    str(self.repository),
                    "grep",
                    "--untracked",
                    "-I",
                    "-n",
                    "-E",
                    r"^[[:space:]]*(from[[:space:]]+[^[:space:]]+[[:space:]]+import|import[[:space:]]+)",
                    "--",
                    "*.py",
                    "*.pyi",
                ],
                timeout=20.0,
                allow_failure=True,
            )
            if completed.returncode in {0, 1}:
                indexed = True
                for raw in completed.stdout.splitlines():
                    relative, separator, rest = raw.partition(":")
                    if not separator:
                        continue
                    raw_line, separator, line = rest.partition(":")
                    if not separator:
                        continue
                    try:
                        line_number = int(raw_line)
                    except ValueError:
                        continue
                    collected.extend(parse(relative, line_number, line))
                    if len(collected) >= 5_000:
                        break

        if not indexed:
            def scan(relative: str) -> list[dict[str, Any]]:
                entries: list[dict[str, Any]] = []
                for line_number, line in enumerate(
                    _read_lines(self.repository / relative, max_file_bytes), start=1
                ):
                    entries.extend(parse(relative, line_number, line))
                return entries

            workers = min(8, max(1, len(python_files)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for entries in pool.map(scan, python_files):
                    collected.extend(entries)
                    if len(collected) >= 5_000:
                        break

        deduplicated: list[dict[str, Any]] = []
        seen: set[tuple[str, str, int]] = set()
        for item in collected:
            import_key = (
                str(item["path"]),
                str(item["module"]),
                int(item["line"]),
            )
            if import_key in seen:
                continue
            seen.add(import_key)
            deduplicated.append(item)
            if len(deduplicated) >= 5_000:
                break
        return deduplicated

    def _resolve_import_target(self, importer: str, module: str) -> Optional[str]:
        """Resolve a Python import to a repository file when it is local.

        AST ``ImportFrom`` records intentionally stay lightweight and do not keep
        the relative-import level, so sibling resolution is tried before the
        common source roots. Missing/external modules simply produce no edge.
        """
        cleaned = module.strip().strip(".")
        if not cleaned:
            return None
        module_parts = tuple(part for part in cleaned.split(".") if part)
        if not module_parts:
            return None
        importer_parent = (self.repository / importer).parent
        relative = Path(*module_parts)
        candidates = [
            importer_parent / relative.with_suffix(".py"),
            importer_parent / relative / "__init__.py",
            self.repository / relative.with_suffix(".py"),
            self.repository / relative / "__init__.py",
        ]
        for source_root in ("src", "lib", "app", "pkg", "packages"):
            candidates.extend(
                [
                    self.repository / source_root / relative.with_suffix(".py"),
                    self.repository / source_root / relative / "__init__.py",
                ]
            )
        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
                return candidate.resolve().relative_to(self.repository).as_posix()
            except (OSError, ValueError):
                continue
        return None

    def _dependency_hints(
        self,
        imports: Iterable[Mapping[str, Any]],
        root_paths: Iterable[str],
    ) -> dict[str, Any]:
        """Return one-hop local dependency/caller paths around ranked roots."""
        roots = {str(path) for path in root_paths if isinstance(path, str) and path}
        implementation: list[str] = []
        tests: list[str] = []
        edges = 0

        def add(path: str) -> None:
            if path in roots:
                return
            target = tests if path.startswith("tests/") else implementation
            if path not in target:
                target.append(path)

        for item in imports:
            importer = str(item.get("path", ""))
            module = str(item.get("module", ""))
            if not importer or not module:
                continue
            target = self._resolve_import_target(importer, module)
            if target is None:
                continue
            if importer in roots:
                add(target)
                edges += 1
            if target in roots:
                add(importer)
                edges += 1
        return {
            "implementation": implementation[:12],
            "tests": tests[:8],
            "edge_count": edges,
        }
