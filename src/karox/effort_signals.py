"""Real task signals for AUTO effort: derived, deterministic, explainable.

The AUTO foundation in :mod:`karox.effort` scores :class:`TaskSignals`; this
module produces those signals from evidence that actually exists on the
machine -- the task text, the durable project map with its git co-change
evidence, and the current mode. It refuses to invent what it cannot observe:
no stored map simply means the dependency and churn signals stay at their
defaults, and the evidence trail says so instead of pretending.

Contract (mandate: AUTO real task signals; AUTO explains itself):

* Deterministic and local. String analysis plus one stored-map read; no
  model call, no network, no clock dependence. The same task text and map
  state always yield the same signals, which is what makes the
  recommendation testable and auditable.
* Prompt length is not a signal. A verbose but trivial request must not
  escalate; scoring keys off concrete file references, dependency evidence,
  and risk vocabulary -- never character count.
* Every triggered signal leaves one short evidence line so the CLI/TUI can
  show "Auto -> HIGH, reasons: ..." without guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from .effort import TaskSignals

# File-looking tokens: a real path mention is the strongest scope evidence a
# task statement can carry.
_PATH_TOKEN = re.compile(
    r"[\w\-./\\]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|rs|go|java|rb|kt|c|h|cpp|"
    r"md|rst|toml|cfg|ini|json|ya?ml|sql|sh|ps1|css|html)\b"
)
# Backticked identifiers may name a module by stem (`effort`, `map_service`).
_TICKED = re.compile(r"`([^`\n]{2,80})`")

# Vocabulary is bilingual by product contract (RU + EN UI). Substring match
# on casefolded text keeps Russian morphology manageable.
_AMBIGUITY = (
    "why", "sometimes", "somehow", "flaky", "randomly", "investigate",
    "figure out", "not sure", "doesn't work", "does not work", "broken",
    "странно", "иногда", "почему", "не работает", "разберись", "выясни",
    "непонятно", "сломал",
)
_RISK = (
    "auth", "security", "secret", "credential", "token", "keyring",
    "password", "payment", "billing", "encrypt", "permission", "capability",
    "bridge identity", "авторизац", "безопасн", "секрет", "парол",
    "платеж", "шифрован", "права доступа",
)
_MIGRATION = (
    "migration", "migrate", "alembic", "schema change", "data backfill",
    "миграц", "перенос схемы",
)
_TEST_COMPLEXITY = (
    "integration test", "e2e", "end-to-end", "race", "timeout", "flaky",
    "full suite", "coverage", "tui test", "интеграционн", "гонка",
    "покрытие", "весь набор",
)
_REGRESSION = (
    "again", "regress", "used to work", "re-broke", "came back", "снова",
    "опять", "регресс", "раньше работал", "вернулась",
)
_BREADTH = (
    "across", "everywhere", "all modules", "rename every", "refactor",
    "по всему", "везде", "все модули", "рефактор",
)

_RISKY_PATH_SEGMENTS = (
    "auth", "security", "secret", "credential", "keyring", "billing",
    "payment", "crypt", "permission", "policy",
)

_KNOWN_MODES = ("build", "plan", "ideate")


@dataclass(frozen=True)
class DerivedSignals:
    """The scored inputs plus the audit trail of where each one came from."""

    signals: TaskSignals
    evidence: Tuple[str, ...]


def _contains(text: str, needles: Tuple[str, ...]) -> Optional[str]:
    for needle in needles:
        if needle in text:
            return needle
    return None


def _load_stored_map(
    repository: Optional[Path], map_root: Optional[Path]
) -> tuple[Optional[Mapping[str, Any]], Optional[bool]]:
    """One guarded read of the durable map: (state, fresh) or (None, None)."""

    if repository is None:
        return None, None
    try:
        from .map_service import MapService

        service = MapService(Path(repository), root=map_root)
        state = service.load()
        if state is None:
            return None, None
        fresh = state.get("revision") == service.revision()
        validation = state.get("validation")
        if isinstance(validation, dict) and validation.get("state") == "STALE":
            fresh = False
        return state, fresh
    except Exception:
        # An unreadable map is the same as no map: signals stay honest
        # defaults rather than crashing effort resolution.
        return None, None


def _referenced_paths(
    text: str, implementation: Tuple[str, ...]
) -> Tuple[str, ...]:
    """Paths the task explicitly names, plus map modules named by stem."""

    found: list[str] = []
    for match in _PATH_TOKEN.finditer(text.replace("\\", "/")):
        token = match.group(0).strip("./")
        if token and token not in found:
            found.append(token)
    stems = {Path(item).stem: item for item in implementation}
    for ticked in _TICKED.findall(text):
        candidate = ticked.strip()
        mapped = stems.get(Path(candidate).stem)
        if mapped and mapped not in found:
            found.append(mapped)
    return tuple(found)


def _cochange_partners(
    references: Tuple[str, ...], git_evidence: Mapping[str, Any]
) -> Tuple[int, Tuple[str, ...]]:
    """Distinct co-change partners of the referenced files, from map data."""

    pairs = git_evidence.get("co_change_pairs")
    if not isinstance(pairs, list) or not references:
        return 0, ()
    normalized = {ref.replace("\\", "/") for ref in references}
    partners: set[str] = set()
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        paths = pair.get("paths")
        if not isinstance(paths, list) or len(paths) != 2:
            continue
        first, second = str(paths[0]), str(paths[1])
        for ref in normalized:
            if first.endswith(ref) or ref.endswith(first):
                partners.add(second)
            elif second.endswith(ref) or ref.endswith(second):
                partners.add(first)
    return len(partners), tuple(sorted(partners)[:6])


def _hot_reference(
    references: Tuple[str, ...], git_evidence: Mapping[str, Any]
) -> Optional[str]:
    hot = git_evidence.get("hot_files")
    if not isinstance(hot, list) or not references:
        return None
    normalized = {ref.replace("\\", "/") for ref in references}
    for entry in hot:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path", ""))
        commits = entry.get("commits")
        if not isinstance(commits, int) or commits < 3:
            continue
        for ref in normalized:
            if path.endswith(ref) or ref.endswith(path):
                return path
    return None


def derive_task_signals(
    task: str,
    *,
    repository: Optional[Path] = None,
    mode: Optional[str] = None,
    map_state: Optional[Mapping[str, Any]] = None,
    map_fresh: Optional[bool] = None,
    map_root: Optional[Path] = None,
) -> DerivedSignals:
    """Derive :class:`TaskSignals` from the task text, map, and mode.

    ``map_state``/``map_fresh`` allow a caller (or test) to inject an already
    loaded map; otherwise the durable map is read once through
    :class:`~karox.map_service.MapService`. Everything degrades honestly:
    with no repository and no map the result carries only text-derived
    signals and says so in the evidence.
    """

    text = (task or "").casefold()
    evidence: list[str] = []

    if map_state is None:
        map_state, map_fresh = _load_stored_map(repository, map_root)
    elif map_fresh is None:
        map_fresh = True

    implementation: Tuple[str, ...] = ()
    git_evidence: Mapping[str, Any] = {}
    if map_state is not None:
        stored_implementation = map_state.get("implementation")
        if isinstance(stored_implementation, list):
            implementation = tuple(
                str(item) for item in stored_implementation if str(item).strip()
            )
        second = map_state.get("second_pass")
        if isinstance(second, dict):
            extra = second.get("additional_change_points")
            if isinstance(extra, list):
                implementation += tuple(
                    str(item) for item in extra if str(item).strip()
                )
        raw_git = map_state.get("git_evidence")
        if isinstance(raw_git, dict):
            git_evidence = raw_git
    else:
        evidence.append(
            "no stored project map: dependency and churn signals unavailable"
        )

    references = _referenced_paths(task or "", implementation)
    if references:
        evidence.append(
            "task names " + ", ".join(references[:4])
            + ("..." if len(references) > 4 else "")
        )

    # -- ambiguity: vague vocabulary with no concrete anchor ----------------
    marker = _contains(text, _AMBIGUITY)
    ambiguity = bool(marker) and not references
    if ambiguity:
        evidence.append(f"ambiguous statement ({marker!r}) with no concrete file")

    # -- breadth: explicit references, cross-cutting vocabulary -------------
    files_likely_affected = len(references)
    breadth_marker = _contains(text, _BREADTH)
    if breadth_marker and implementation:
        files_likely_affected = max(files_likely_affected, len(implementation))
        evidence.append(
            f"cross-cutting change ({breadth_marker!r}): map shows "
            f"{len(implementation)} likely change points"
        )

    # -- dependency breadth: co-change partners from map git evidence -------
    dependency_breadth, partners = _cochange_partners(references, git_evidence)
    if dependency_breadth:
        evidence.append(
            f"{dependency_breadth} co-change partners in git history: "
            + ", ".join(partners)
        )

    # -- risk ---------------------------------------------------------------
    risk_marker = _contains(text, _RISK)
    risky_path = next(
        (
            ref
            for ref in references
            for segment in _RISKY_PATH_SEGMENTS
            if segment in ref.casefold()
        ),
        None,
    )
    risk_area = bool(risk_marker or risky_path)
    if risk_marker:
        evidence.append(f"risk vocabulary ({risk_marker!r})")
    elif risky_path:
        evidence.append(f"risky path referenced ({risky_path})")

    # -- migration ------------------------------------------------------------
    migration_marker = _contains(text, _MIGRATION)
    if migration_marker:
        evidence.append(f"migration involved ({migration_marker!r})")

    # -- test complexity ------------------------------------------------------
    test_refs = tuple(ref for ref in references if "test" in ref.casefold())
    test_marker = _contains(text, _TEST_COMPLEXITY)
    test_complexity = bool(test_marker) or len(test_refs) >= 2
    if test_marker:
        evidence.append(f"complex test surface ({test_marker!r})")
    elif test_complexity:
        evidence.append(f"{len(test_refs)} test files referenced")

    # -- regression history ---------------------------------------------------
    regression_marker = _contains(text, _REGRESSION)
    hot_path = _hot_reference(references, git_evidence)
    regression_history = bool(regression_marker or hot_path)
    if regression_marker:
        evidence.append(f"regression vocabulary ({regression_marker!r})")
    elif hot_path:
        evidence.append(f"referenced file has high recent churn ({hot_path})")

    # -- mode and map freshness ------------------------------------------------
    normalized_mode = (mode or "").strip().casefold() or None
    if normalized_mode is not None and normalized_mode not in _KNOWN_MODES:
        normalized_mode = None
    map_stale = bool(map_state is not None and map_fresh is False)
    if map_stale:
        evidence.append("stored project map is stale for the current revision")

    signals = TaskSignals(
        ambiguity=ambiguity,
        files_likely_affected=files_likely_affected,
        dependency_breadth=dependency_breadth,
        risk_area=risk_area,
        migration_involved=bool(migration_marker),
        test_complexity=test_complexity,
        regression_history=regression_history,
        mode=normalized_mode,
        map_stale=map_stale,
    )
    return DerivedSignals(signals=signals, evidence=tuple(evidence))
