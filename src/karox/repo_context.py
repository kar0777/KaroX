"""Deterministic repository context engine for high-level hosted inspection.

No model is called inside KaroX.  The engine combines ripgrep, Python AST,
repository paths, test/doc naming and import/call relationships, then stores the
full map as a session artifact while returning a compact ranked digest.
"""

from __future__ import annotations

from .repo_identity import RepoIdentityMixin
from .repo_search import RepoSearchMixin
from .repo_inspect import RepoInspectMixin
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

import hashlib
from pathlib import Path

from .artifacts import ArtifactStore
from .paths import runtime_dir

#: Vendored copies of this project under tools/, named after the version they
#: adapted. Component prefixes rather than names, because the name is the version.




































class RepositoryContextEngine(RepoIdentityMixin, RepoSearchMixin, RepoInspectMixin):
    def __init__(
        self,
        repository: Path,
        artifacts: ArtifactStore,
        *,
        policy_profile: str,
    ) -> None:
        self.repository = repository.expanduser().resolve(strict=True)
        self.artifacts = artifacts
        self.policy_profile = policy_profile
        self._git_directory = self._resolve_git_directory()
        self._common_git_directory = self._resolve_common_git_directory(self._git_directory)
        self.is_git_repository = self._git_directory is not None
        repo_key = hashlib.sha256(str(self.repository).encode("utf-8")).hexdigest()[:20]
        self.cache_root = (
            runtime_dir()
            / "vnext"
            / "repo-context-cache"
            / artifacts.session_id
            / repo_key
        )
        self.cache_root.mkdir(parents=True, exist_ok=True)





























