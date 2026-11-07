"""Relevance ranking, excerpts, edit context and the inspect entrypoint."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Iterable, Mapping, Optional
from .security import contains_credential, redact
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


class RepoInspectMixin:
    """Methods composed into RepositoryContextEngine; see repo_context.py."""

    def _rank_files(
        self,
        files: Iterable[str],
        matches: Iterable[Mapping[str, Any]],
        symbols: Mapping[str, Any],
        tokens: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        scores: dict[str, float] = {path: 0.0 for path in files}
        reasons: dict[str, list[str]] = {path: [] for path in files}
        for path in files:
            path_hits = _path_relevance(path, tokens)
            if path_hits:
                scores[path] += path_hits * 20
                reasons[path].append(f"path-token-hits={path_hits}")
            if path.startswith("tests/") or "/test_" in path or path.endswith("_test.py"):
                scores[path] += 1.5
            # invalid typographic literal preserved in comment: path.startswith("docs/") or path.lower().endswith(“.md"):
            if path.startswith("docs/") or path.lower().endswith(".md"):
                scores[path] += 1.0
        match_counts: dict[str, int] = {}
        token_coverage: dict[str, set[str]] = {}
        identifier_tokens = tuple(
            token for token in tokens if any(marker in token for marker in ("_", ".", "-"))
        )
        identifier_coverage: dict[str, set[str]] = {}
        for item in matches:
            path = str(item.get("path", ""))
            if path in scores:
                match_counts[path] = match_counts.get(path, 0) + 1
                lowered_text = str(item.get("text", "")).lower()
                token_coverage.setdefault(path, set()).update(
                    token for token in tokens if token in lowered_text
                )
                identifier_coverage.setdefault(path, set()).update(
                    token for token in identifier_tokens if token in lowered_text
                )
        for path, count in match_counts.items():
            scores[path] += min(count, 8) * 1.5
            coverage = len(token_coverage.get(path, ()))
            identifier_hits = len(identifier_coverage.get(path, ()))
            scores[path] += coverage * 8
            scores[path] += identifier_hits * 36
            reasons[path].append(f"content-matches={count}")
            if coverage:
                reasons[path].append(f"goal-token-coverage={coverage}")
            if identifier_hits:
                reasons[path].append(f"identifier-token-coverage={identifier_hits}")
        for key, weight in (("definitions", 12), ("calls", 5), ("types", 8)):
            matched_per_file: dict[str, int] = {}
            for item in symbols.get(key, []):
                path = str(item.get("path", ""))
                if path in scores and item.get("goal_match") is True:
                    matched_per_file[path] = matched_per_file.get(path, 0) + 1
            for path, count in matched_per_file.items():
                scores[path] += min(count, 4) * weight
                reasons[path].append(f"{key}={count}")
        for path, score in scores.items():
            if score > 0 and path.startswith(
                ("src/", "lib/", "app/", "pkg/", "cmd/", "internal/", "packages/")
            ):
                scores[path] += 36
                reasons[path].append("primary-source")
        ranked: list[dict[str, Any]] = [
            {"path": path, "score": round(score, 3), "reasons": sorted(set(reasons[path]))}
            for path, score in scores.items()
            if score > 0
        ]
        ranked.sort(key=lambda item: (-float(item["score"]), str(item["path"])))
        return ranked

    def _excerpts(
        self,
        matches: list[dict[str, Any]],
        ranked: list[dict[str, Any]],
        budget: InspectBudget,
    ) -> list[dict[str, Any]]:
        rank_order = {str(item["path"]): index for index, item in enumerate(ranked)}
        ordered = sorted(
            matches,
            key=lambda item: (
                rank_order.get(str(item["path"]), 999999),
                int(item["line"]),
            ),
        )
        excerpts: list[dict[str, Any]] = []
        file_lines: dict[str, list[str]] = {}
        emitted_end: dict[str, int] = {}
        window = budget.context_lines * 2 + 1
        remaining_lines = budget.max_excerpts * window
        for match in ordered:
            path = str(match["path"])
            line_number = int(match["line"])
            if path not in file_lines:
                file_lines[path] = _read_lines(self.repository / path, budget.max_file_bytes)
            lines = file_lines[path]
            if not lines or not 1 <= line_number <= len(lines):
                continue
            start = max(1, line_number - budget.context_lines, emitted_end.get(path, 0) + 1)
            end = min(len(lines), line_number + budget.context_lines)
            if start > end:
                continue
            end = min(end, start + remaining_lines - 1)
            if end < line_number and line_number > emitted_end.get(path, 0):
                # The line budget cannot reach the match that justified this
                # excerpt: emitting the window anyway would spend context on a
                # reason the reader never gets to see, and nothing in the result
                # would say the line was cut. Skip it and let a later match with
                # a shorter window use what is left.
                continue
            previous = excerpts[-1] if excerpts else None
            merge = (previous is not None and previous["path"] == path
                     and previous["end"] + 1 == start
                     and end - previous["start"] + 1 <= window * 4)
            if not merge and len(excerpts) >= budget.max_excerpts:
                break
            content = [
                {"line": index, "text": str(redact(lines[index - 1]))[:2000]}
                for index in range(start, end + 1)
            ]
            if merge and previous is not None:
                previous["end"] = end
                previous["lines"].extend(content)
            else:
                excerpts.append({"path": path, "start": start, "end": end, "lines": content})
            emitted_end[path] = end
            remaining_lines -= len(content)
            if remaining_lines <= 0:
                break
        return excerpts

    def _edit_context(
        self,
        change_points: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """Return exact content for one small, safe top change-point."""

        if not change_points:
            return None
        relative = str(change_points[0].get("path", "")).strip()
        if not relative:
            return None
        path = self.repository / relative
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        if len(raw) > _EDIT_CONTEXT_MAX_BYTES:
            return None
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if len(content.splitlines()) > _EDIT_CONTEXT_MAX_LINES:
            return None
        # Exact edit context must never embed credential-like content.
        # Keep using the guarded repo.read_file path when the shared security
        # detector finds anything that would be unsafe to place in inspect.
        if contains_credential(content):
            return None
        return {
            "path": relative,
            "content": content,
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "complete_file": True,
        }

    def inspect(
        self,
        goal: str,
        depth: str = "focused",
        *,
        include_dependency_hints: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 10_000:
            raise ValueError("repo.inspect goal must be a non-empty string up to 10000 characters")
        if depth not in _BUDGETS:
            raise ValueError("repo.inspect depth must be focused, standard, or deep")
        budget = _BUDGETS[depth]
        normalized_goal = goal.strip()
        revision = self._compact_content_revision_identity()
        key = self._cache_key(
            normalized_goal,
            depth,
            revision,
            include_dependency_hints=include_dependency_hints,
        )
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        started = time.perf_counter()
        tokens = _tokens(normalized_goal)
        files_started = time.perf_counter()
        files = self._files(budget)
        files_ms = (time.perf_counter() - files_started) * 1000
        search_started = time.perf_counter()
        matches = self._ripgrep(tokens, budget)
        search_ms = (time.perf_counter() - search_started) * 1000
        candidate_files = self._symbol_candidate_files(files, matches, tokens, budget)
        if not candidate_files:
            candidate_files = [
                path
                for path in files
                if any(token in path.lower() for token in tokens)
            ][: budget.max_files]
        symbols_started = time.perf_counter()
        symbols = self._python_symbols(candidate_files, tokens)
        # Ordinary inspection stays fast. A broad caller/dependency index is
        # built only when an explicit recursive/research experiment asks for it;
        # the default project map must not pay several seconds for unused edges.
        if include_dependency_hints:
            import_edges = self._lightweight_imports(
                files, max_file_bytes=budget.max_file_bytes
            )
            merged_imports: list[dict[str, Any]] = []
            seen_imports: set[tuple[str, str, int]] = set()
            for item in [*symbols.get("imports", []), *import_edges]:
                path = str(item.get("path", ""))
                module = str(item.get("module", ""))
                line = int(item.get("line", 0) or 0)
                import_key = (path, module, line)
                if not path or not module or import_key in seen_imports:
                    continue
                seen_imports.add(import_key)
                merged_imports.append({"path": path, "module": module, "line": line})
                if len(merged_imports) >= 5_000:
                    break
            symbols["imports"] = merged_imports
        symbols_ms = (time.perf_counter() - symbols_started) * 1000
        rank_started = time.perf_counter()
        ranked = self._rank_files(files, matches, symbols, tokens)[: budget.max_files]
        excerpts = self._excerpts(matches, ranked, budget)
        tests = [item for item in ranked if str(item["path"]).startswith("tests/")][:20]
        docs = [
            item
            for item in ranked
            if str(item["path"]).startswith("docs/") and item.get("reasons")
        ][:20]
        preferred_source_roots = (
            "src/", "lib/", "app/", "pkg/", "cmd/", "internal/", "packages/"
        )
        change_candidates = [
            item
            for item in ranked
            if not str(item["path"]).startswith(("tests/", "docs/", "benchmarks/"))
            and not str(item["path"]).lower().endswith((".md", ".rst", ".txt"))
        ]
        change_points = sorted(
            change_candidates,
            key=lambda item: (
                0 if str(item["path"]).startswith(preferred_source_roots) else 1,
                -float(item["score"]),
                str(item["path"]),
            ),
        )[:20]
        edit_context = self._edit_context(change_points)
        dependency_hints = self._dependency_hints(
            symbols["imports"],
            (str(item["path"]) for item in change_points[:6]),
        )
        rank_ms = (time.perf_counter() - rank_started) * 1000
        full: dict[str, Any] = {
            "schema_version": REPO_INSPECT_SCHEMA_VERSION,
            "goal": normalized_goal,
            "depth": depth,
            "tokens": tokens,
            "repository_revision": revision,
            "ranked_files": ranked,
            "matches": matches,
            "symbols": symbols,
            "dependency_map": {
                "imports": symbols["imports"],
                "calls": symbols["calls"],
            },
            "dependency_hints": dependency_hints,
            "tests": tests,
            "docs": docs,
            "likely_change_points": change_points,
            "excerpts": excerpts,
            "edit_context": edit_context,
            "metrics": {
                "files_considered": len(files),
                "candidate_files": len(candidate_files),
                "matches": len(matches),
                "files_ms": round(files_ms, 3),
                "search_ms": round(search_ms, 3),
                "symbols_ms": round(symbols_ms, 3),
                "rank_ms": round(rank_ms, 3),
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            },
        }
        encoded = json.dumps(redact(full), ensure_ascii=False, sort_keys=True).encode("utf-8")
        record = self.artifacts.put(
            encoded,
            name="repo-inspect.json",
            mime="application/json",
        )
        result = {
            "ok": True,
            "schema_version": REPO_INSPECT_SCHEMA_VERSION,
            "goal": normalized_goal,
            "depth": depth,
            "cache_hit": False,
            "summary": {
                "files_considered": len(files),
                "matches": len(matches),
                "definitions": len(symbols["definitions"]),
                "callers": len(symbols["calls"]),
                "tests": len(tests),
                "docs": len(docs),
                "duration_ms": full["metrics"]["duration_ms"],
            },
            "important_findings": ranked[:12],
            "likely_change_points": change_points[:10],
            "relevant_tests": tests[:10],
            "relevant_docs": docs[:10],
            "dependency_hints": dependency_hints,
            "excerpts": excerpts[:10],
            "edit_context": edit_context,
            "artifact_id": record.artifact_id,
            "available_sections": [
                "ranked_files",
                "matches",
                "symbols.definitions",
                "symbols.calls",
                "symbols.types",
                "dependency_map",
                "dependency_hints",
                "tests",
                "docs",
                "likely_change_points",
                "excerpts",
                "edit_context",
                "metrics",
            ],
            "content_hash": record.sha256,
            "total_size": record.size,
            "persistence_policy": record.persistence_policy,
            "expires_at": record.expires_at,
        }
        self._cache_put(key, result)
        return result
