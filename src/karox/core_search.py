"""Repository search: ripgrep, the small-tree fallback, git grep and LSP.

Split from :mod:`karox.core`; the group shares only the class constants and
the process/Git methods CoreRuntime already provides.
"""

from __future__ import annotations

import json
import math
import os
import re
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core_types import CoreError, InvalidCommand, InvalidPath, _path_label


class SearchToolsMixin:
    """Search methods mixed into :class:`karox.core.CoreRuntime`."""

    def _search_ripgrep(
        self,
        *,
        query: str,
        use_regex: bool,
        case_sensitive: bool,
        pattern: str,
        limit: int,
        deadline_seconds: float,
    ) -> Optional[Dict[str, Any]]:
        """Use ripgrep for repository search when available.

        The legacy Python scanner is intentionally kept as a portability fallback,
        but opening and decoding up to 2,000 files serially is far too expensive
        for large workspaces with generated or untracked trees.  Ripgrep provides
        the same bounded line-oriented result much more efficiently while staying
        inside the selected repository and without invoking a shell.
        """
        rg = self._ripgrep_path
        if rg is None:
            return None
        argv = [
            rg,
            "--json",
            "--line-number",
            "--no-heading",
            "--hidden",
            "--no-ignore",
            "--glob",
            "!.git/**",
            "--glob",
            "!.karox/**",
        ]
        # Repository-wide search should inspect source, not generated copies.
        # Keep explicit user globs authoritative, but for the default scope skip
        # dependency/build/runtime directories consistently across backends.
        if pattern == "**/*":
            for ignored in sorted(
                str(item) for item in getattr(self, "SEARCH_IGNORED_DIRECTORY_NAMES", ())
            ):
                argv.extend(["--glob", f"!**/{ignored}/**"])
            for suffix in getattr(self, "SEARCH_IGNORED_DIRECTORY_SUFFIXES", ()):
                argv.extend(["--glob", f"!**/*{suffix}/**"])
            for prefix in getattr(self, "SEARCH_IGNORED_DIRECTORY_PREFIXES", ()):
                argv.extend(["--glob", f"!**/{prefix}*/**"])
        argv.extend(
            [
                "--max-filesize",
                str(self.MAX_FILE_BYTES),
                # A Windows checkout is full of CRLF files, and without this
                # ripgrep treats the carriage return as line content: ``$`` then
                # anchors after it, so a pattern like ``elit $`` reports no match
                # in a file whose lines end exactly that way. The model distrusts
                # a correct belief and spends turns proving it again. With --crlf
                # the anchor means what the caller wrote, and the line text no
                # longer carries the "\r" into a result either.
                "--crlf",
            ]
        )
        if not case_sensitive:
            argv.append("--ignore-case")
        if not use_regex:
            argv.append("--fixed-strings")
        if pattern != "**/*":
            argv.extend(["--glob", pattern])
        argv.extend(["--", query, "."])

        result = self._run(argv, deadline_seconds, redact_output=False)
        exit_code = result.get("exit_code")
        if exit_code not in {0, 1, None}:
            # Preserve portability and unusual glob behaviour by falling back to
            # the established Python implementation if ripgrep rejects the run.
            return None

        matches: List[Dict[str, Any]] = []
        files_scanned = 0
        stats_matches: Optional[int] = None
        for raw_line in str(result.get("stdout", "")).splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            if event_type == "summary":
                stats = data.get("stats")
                if isinstance(stats, dict):
                    searches = stats.get("searches")
                    total_matches = stats.get("matches")
                    if isinstance(searches, int):
                        files_scanned = searches
                    if isinstance(total_matches, int):
                        stats_matches = total_matches
                continue
            if event_type != "match" or len(matches) >= limit:
                continue
            path_data = data.get("path")
            lines_data = data.get("lines")
            if not isinstance(path_data, dict) or not isinstance(lines_data, dict):
                continue
            path_text = path_data.get("text")
            line_text = lines_data.get("text")
            line_number = data.get("line_number")
            if not isinstance(path_text, str) or not isinstance(line_text, str):
                continue
            if not isinstance(line_number, int):
                continue
            normalized_path = path_text.replace("\\", "/")
            if normalized_path.startswith("./"):
                normalized_path = normalized_path[2:]
            try:
                safe = self.safe_path(normalized_path)
                relative = safe.relative_to(self.repository).as_posix()
            except (InvalidPath, ValueError):
                continue
            line = line_text.rstrip("\r\n")
            encoded = line.encode("utf-8")
            clipped = len(encoded) > self.MAX_SEARCH_LINE_BYTES
            if clipped:
                line = encoded[: self.MAX_SEARCH_LINE_BYTES].decode(
                    "utf-8", errors="ignore"
                )
            matches.append(
                {
                    "path": relative,
                    "line": line_number,
                    "text": line,
                    "clipped": clipped,
                }
            )

        truncated = bool(result.get("stdout_truncated")) or bool(result.get("timed_out"))
        if len(matches) >= limit:
            truncated = True
        if stats_matches is not None and stats_matches > len(matches):
            truncated = True
        return {
            "query": query,
            "regex": use_regex,
            "case_sensitive": case_sensitive,
            "files_scanned": files_scanned,
            "files_skipped": 0,
            "match_count": len(matches),
            "matches": matches,
            "truncated": truncated,
            "backend": "ripgrep",
        }

    def _search_small_tree(
        self,
        *,
        query: str,
        use_regex: bool,
        case_sensitive: bool,
        pattern: str,
        matcher: re.Pattern[str],
        limit: int,
    ) -> Optional[Dict[str, Any]]:
        """Search a proven-small default tree without spawning a process.

        This fast path is deliberately conservative. It is used only for the
        default repository-wide pattern after ripgrep is unavailable. If the
        tree exceeds a small bounded probe, contains a symlink, or cannot be
        scanned unambiguously, return ``None`` so the established git/Python
        backends retain their existing semantics.
        """
        if pattern != "**/*":
            return None

        candidates: List[tuple[str, Path]] = []
        ignored_directory_names = frozenset(
            str(item).lower()
            for item in getattr(self, "SEARCH_IGNORED_DIRECTORY_NAMES", ())
        )
        ignored_directory_suffixes = tuple(
            str(item).lower()
            for item in getattr(self, "SEARCH_IGNORED_DIRECTORY_SUFFIXES", ())
        )
        stack: List[Path] = [self.repository]
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as iterator:
                    entries = list(iterator)
            except OSError:
                return None

            directories: List[Path] = []
            for entry in entries:
                if directory == self.repository and entry.name.lower() in {".git", ".karox"}:
                    continue
                path = Path(entry.path)
                try:
                    if entry.is_symlink():
                        return None
                    if entry.is_dir(follow_symlinks=False):
                        lowered_name = entry.name.lower()
                        if (
                            lowered_name in ignored_directory_names
                            or any(
                                lowered_name.endswith(suffix)
                                for suffix in ignored_directory_suffixes
                            )
                        ):
                            continue
                        # Windows junctions/reparse points can leave the selected
                        # repository without being reported as ordinary symlinks.
                        # Ambiguous traversal always falls back to the established
                        # safe_path-backed native/Python search path.
                        is_junction = getattr(path, "is_junction", None)
                        if callable(is_junction) and is_junction():
                            return None
                        directories.append(path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    return None
                try:
                    relative = path.relative_to(self.repository).as_posix()
                except ValueError:
                    return None
                candidates.append((relative, path))
                if len(candidates) > self.SMALL_SEARCH_FILE_PROBE_LIMIT:
                    return None
            directories.sort(key=lambda item: item.as_posix(), reverse=True)
            stack.extend(directories)

        ordered = sorted(candidates, key=lambda item: item[0])

        def scan_candidate(
            candidate: tuple[str, Path],
        ) -> tuple[bool, bool, List[Dict[str, Any]]]:
            relative, path = candidate
            try:
                # Re-check immediately before the read so a file replaced by a
                # symlink after enumeration cannot silently use this fast path.
                if path.is_symlink():
                    return True, False, []
                if path.stat().st_size > self.MAX_FILE_BYTES:
                    return False, True, []
                text = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                return False, True, []
            file_matches: List[Dict[str, Any]] = []
            for number, line in enumerate(text.splitlines(), start=1):
                if not matcher.search(line):
                    continue
                encoded = line.encode("utf-8")
                clipped = len(encoded) > self.MAX_SEARCH_LINE_BYTES
                if clipped:
                    line = encoded[: self.MAX_SEARCH_LINE_BYTES].decode(
                        "utf-8", errors="ignore"
                    )
                file_matches.append(
                    {
                        "path": relative,
                        "line": number,
                        "text": line,
                        "clipped": clipped,
                    }
                )
                # The public result is globally capped at ``limit``. Keeping at
                # most that many matches per file bounds worker result memory;
                # reaching the global cap is already reported as truncated by
                # the established search contract.
                if len(file_matches) >= limit:
                    break
            return False, False, file_matches

        matches: List[Dict[str, Any]] = []
        scanned = 0
        skipped = 0
        truncated = False
        worker_count = min(8, len(ordered))
        if worker_count:
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="karox-search",
            ) as executor:
                pending: List[
                    Future[tuple[bool, bool, List[Dict[str, Any]]]]
                ] = []
                next_index = 0
                while next_index < worker_count:
                    pending.append(executor.submit(scan_candidate, ordered[next_index]))
                    next_index += 1

                while pending:
                    future = pending.pop(0)
                    try:
                        ambiguous, file_skipped, file_matches = future.result()
                    except Exception:
                        for item in pending:
                            item.cancel()
                        return None
                    if ambiguous:
                        for item in pending:
                            item.cancel()
                        return None
                    if file_skipped:
                        skipped += 1
                    else:
                        scanned += 1
                    remaining = limit - len(matches)
                    if len(file_matches) >= remaining:
                        matches.extend(file_matches[:remaining])
                        truncated = True
                        for item in pending:
                            item.cancel()
                        break
                    matches.extend(file_matches)
                    if next_index < len(ordered):
                        pending.append(executor.submit(scan_candidate, ordered[next_index]))
                        next_index += 1

        return {
            "query": query,
            "regex": use_regex,
            "case_sensitive": case_sensitive,
            "files_scanned": scanned,
            "files_skipped": skipped,
            "match_count": len(matches),
            "matches": matches,
            "truncated": truncated,
            "backend": "python",
        }

    def _search_git_grep(
        self,
        *,
        query: str,
        use_regex: bool,
        case_sensitive: bool,
        pattern: str,
        matcher: re.Pattern[str],
        limit: int,
        deadline_seconds: float,
    ) -> Optional[Dict[str, Any]]:
        """Use Git's native grep to shortlist matching files for literal search.

        Git is already a required dependency for a KaroX repository, unlike
        ripgrep.  For the common default literal search we let ``git grep`` scan
        tracked and untracked files natively, then read only the matching files
        in Python so the public line/text/clipping contract remains identical to
        the legacy scanner.  Regex and explicit-glob searches deliberately stay
        on the established Python fallback to preserve their exact semantics.
        """
        if use_regex or pattern != "**/*":
            return None

        arguments = [
            "grep",
            "--untracked",
            "--no-exclude-standard",
            "-I",
            "-l",
            "-z",
            "--full-name",
            "-F",
        ]
        if not case_sensitive:
            arguments.append("-i")
        arguments.extend(["-e", query, "--"])
        result = self._git(arguments, deadline_seconds, redact_output=False)
        exit_code = result.get("exit_code")
        if exit_code not in {0, 1, None}:
            return None

        raw_output = str(result.get("stdout", ""))
        raw_candidates = (
            raw_output.split("\x00") if "\x00" in raw_output else raw_output.splitlines()
        )
        candidates: List[str] = []
        ignored_names = {
            str(item).lower()
            for item in getattr(self, "SEARCH_IGNORED_DIRECTORY_NAMES", ())
        }
        ignored_suffixes = tuple(
            str(item).lower()
            for item in getattr(self, "SEARCH_IGNORED_DIRECTORY_SUFFIXES", ())
        )
        ignored_prefixes = tuple(
            str(item).lower()
            for item in getattr(self, "SEARCH_IGNORED_DIRECTORY_PREFIXES", ())
        )
        for raw in raw_candidates:
            relative = raw.strip().replace("\\", "/")
            if not relative:
                continue
            try:
                safe = self.safe_path(relative)
                normalized = safe.relative_to(self.repository).as_posix()
            except (InvalidPath, ValueError):
                continue
            if normalized.startswith(".karox/"):
                continue
            parts = [part.lower() for part in Path(normalized).parts[:-1]]
            if any(
                part in ignored_names
                or any(part.endswith(suffix) for suffix in ignored_suffixes)
                or any(part.startswith(prefix) for prefix in ignored_prefixes)
                for part in parts
            ):
                continue
            candidates.append(normalized)
            if len(candidates) >= self.MAX_SEARCH_FILES:
                break

        matches: List[Dict[str, Any]] = []
        scanned = 0
        skipped = 0
        truncated = bool(result.get("stdout_truncated")) or bool(result.get("timed_out"))
        if len(candidates) >= self.MAX_SEARCH_FILES:
            truncated = True
        for relative in candidates:
            if len(matches) >= limit:
                truncated = True
                break
            try:
                path = self.safe_path(relative)
                if path.stat().st_size > self.MAX_FILE_BYTES:
                    skipped += 1
                    continue
                text = path.read_bytes().decode("utf-8")
            except (InvalidPath, OSError, UnicodeDecodeError):
                skipped += 1
                continue
            scanned += 1
            for number, line in enumerate(text.splitlines(), start=1):
                if not matcher.search(line):
                    continue
                encoded = line.encode("utf-8")
                clipped = len(encoded) > self.MAX_SEARCH_LINE_BYTES
                if clipped:
                    line = encoded[: self.MAX_SEARCH_LINE_BYTES].decode(
                        "utf-8", errors="ignore"
                    )
                matches.append(
                    {
                        "path": relative,
                        "line": number,
                        "text": line,
                        "clipped": clipped,
                    }
                )
                if len(matches) >= limit:
                    truncated = True
                    break

        return {
            "query": query,
            "regex": use_regex,
            "case_sensitive": case_sensitive,
            "files_scanned": scanned,
            "files_skipped": skipped,
            "match_count": len(matches),
            "matches": matches,
            "truncated": truncated,
            "backend": "git-grep",
        }

    def _lsp_diagnostics(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        path = self._required(arguments, "path", str)
        if not path or len(path) > 1000 or "\x00" in path:
            raise InvalidCommand("diagnostic path must contain 1-1000 characters")
        requested = arguments.get("timeout_seconds", min(8.0, deadline_seconds))
        if isinstance(requested, bool) or not isinstance(requested, (int, float)):
            raise InvalidCommand("diagnostic timeout must be a number")
        if not math.isfinite(float(requested)):
            raise InvalidCommand("diagnostic timeout must be between 1 and 30 seconds")
        # Out-of-range values are clamped, not refused: a refusal cost a model
        # turn to learn a bound the runtime can apply itself.
        requested = min(30.0, max(1.0, float(requested)))
        timeout = min(float(requested), max(1.0, float(deadline_seconds)))
        from .lsp_diagnostics import LspDiagnostics, LspDiagnosticsError

        try:
            return LspDiagnostics(self.repository).diagnose(path, timeout_seconds=timeout)
        except LspDiagnosticsError as exc:
            raise CoreError(str(exc)) from exc

    def _search_scope_pattern(self, scope: Any) -> Any:
        """Map repo.search `path` to a glob; invalid values fall through to the
        same repository-relative validation as `pattern`."""
        if scope is None:
            return "**/*"
        if not isinstance(scope, str):
            raise InvalidCommand("path must be string")
        cleaned = scope.replace("\\", "/").strip()
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        cleaned = cleaned.rstrip("/")
        if cleaned in {"", "."}:
            return "**/*"
        if any(ch in cleaned for ch in "*?["):
            return cleaned
        try:
            if self.safe_path(cleaned).is_file():
                return cleaned
        except Exception:
            return cleaned  # rejected below with the standard message
        return cleaned + "/**/*"

    def _search(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        query = self._required(arguments, "query", str)
        if not query or len(query) > 1_000 or "\x00" in query:
            raise InvalidCommand("query must contain 1-1000 characters")
        use_regex = arguments.get("regex", False)
        if not isinstance(use_regex, bool):
            raise InvalidCommand("regex must be boolean")
        case_sensitive = arguments.get("case_sensitive", False)
        if not isinstance(case_sensitive, bool):
            raise InvalidCommand("case_sensitive must be boolean")
        requested = arguments.get("max_results", self.MAX_SEARCH_RESULTS)
        if isinstance(requested, bool) or not isinstance(requested, (int, float)):
            raise InvalidCommand("max_results must be number")
        if not math.isfinite(float(requested)) or float(requested) < 1:
            raise InvalidCommand("max_results must be a positive number")
        limit = min(int(requested), self.MAX_SEARCH_RESULTS)
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            matcher = re.compile(query if use_regex else re.escape(query), flags)
        except re.error as exc:
            raise InvalidCommand(f"invalid regular expression: {exc}") from exc

        pattern = arguments.get("pattern")
        if pattern is None:
            # Models scope searches with `path` far more often than with a glob
            # (swer5-k3: 13 rejected calls in one run). Treat it as the glob it means.
            pattern = self._search_scope_pattern(arguments.get("path"))
        raw_pattern = Path(pattern) if isinstance(pattern, str) else None
        if (
            raw_pattern is None
            or not pattern
            or len(pattern) > 1000
            or "\x00" in pattern
            or raw_pattern.is_absolute()
            or raw_pattern.drive
            or ".." in raw_pattern.parts
        ):
            raise InvalidCommand(
                "pattern must be repository-relative: " + _path_label(str(pattern))
            )

        fast = self._search_ripgrep(
            query=query,
            use_regex=use_regex,
            case_sensitive=case_sensitive,
            pattern=pattern,
            limit=limit,
            deadline_seconds=deadline_seconds,
        )
        if fast is not None:
            return fast

        fast = self._search_small_tree(
            query=query,
            use_regex=use_regex,
            case_sensitive=case_sensitive,
            pattern=pattern,
            matcher=matcher,
            limit=limit,
        )
        if fast is not None:
            return fast

        fast = self._search_git_grep(
            query=query,
            use_regex=use_regex,
            case_sensitive=case_sensitive,
            pattern=pattern,
            matcher=matcher,
            limit=limit,
            deadline_seconds=deadline_seconds,
        )
        if fast is not None:
            return fast

        listing = self._list_files({"pattern": pattern}, deadline_seconds)
        candidates = listing["files"][: self.MAX_SEARCH_FILES]
        truncated = bool(listing["truncated"]) or len(listing["files"]) > len(
            candidates
        )
        matches: List[Dict[str, Any]] = []
        scanned = 0
        skipped = 0
        for relative in candidates:
            if len(matches) >= limit:
                truncated = True
                break
            try:
                path = self.safe_path(relative)
                if path.stat().st_size > self.MAX_FILE_BYTES:
                    skipped += 1
                    continue
                text = path.read_bytes().decode("utf-8")
            except (InvalidPath, OSError, UnicodeDecodeError):
                skipped += 1
                continue
            scanned += 1
            for number, line in enumerate(text.splitlines(), start=1):
                if not matcher.search(line):
                    continue
                encoded = line.encode("utf-8")
                clipped = len(encoded) > self.MAX_SEARCH_LINE_BYTES
                if clipped:
                    line = encoded[: self.MAX_SEARCH_LINE_BYTES].decode(
                        "utf-8", errors="ignore"
                    )
                matches.append(
                    {
                        "path": relative,
                        "line": number,
                        # A matched line is repository content a caller may quote
                        # back, so it follows the same byte-faithful rule as a
                        # read rather than the display rule used for audit rows.
                        "text": line,
                        "clipped": clipped,
                    }
                )
                if len(matches) >= limit:
                    truncated = True
                    break
        return {
            "query": query,
            "regex": use_regex,
            "case_sensitive": case_sensitive,
            "files_scanned": scanned,
            "files_skipped": skipped,
            "match_count": len(matches),
            "matches": matches,
            "truncated": truncated,
            "backend": "python",
        }
