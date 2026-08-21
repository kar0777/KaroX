"""Grouped activity over the typed agent event stream.

The typed protocol already reports every low-level fact that happens during a
run: a tool started, a file was read, a check finished. Shown raw, that feed
is tool-call spam -- motion without meaning at agent speed. This module folds
the chronological event stream into a short list of *activities*: what the
agent was doing (investigating, implementing, verifying, driving the browser),
for how many files, with which outcome.

Three rules carried over from the activity-line layer, because they are what
make the surface trustworthy:

* **Containment.** Nothing free-form reaches a renderer from here. A group is
  a kind from the catalog below plus integers and identifier-keyed reasons; no
  branch interpolates a string taken from a tool payload. Raw detail stays in
  Session Detail and ``--json``, on demand.
* **Honesty.** Every number is a count of events that actually happened. There
  is no percentage progress, because nothing here knows the denominator, and
  no fabricated narration -- only phases the kernel proclaimed, counts, and
  pass/fail outcomes.
* **Visibility of trouble.** A warning or an error is never folded away. It
  stays a line of its own in every rendering, and the group that carried it
  reports attention rather than success.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

__all__ = [
    "GROUP_BROWSER",
    "GROUP_IMPLEMENTING",
    "GROUP_INVESTIGATING",
    "GROUP_VERIFYING",
    "GROUP_WORKING",
    "STATUS_ATTENTION",
    "STATUS_DONE",
    "STATUS_RUNNING",
    "ActivityGroup",
    "ActivityStream",
    "render_group_lines",
    "render_stream_lines",
]


# --------------------------------------------------------------------------- #
# Catalog: every word a renderer may print                                     #
# --------------------------------------------------------------------------- #

GROUP_INVESTIGATING = "investigating"
GROUP_IMPLEMENTING = "implementing"
GROUP_VERIFYING = "verifying"
GROUP_BROWSER = "browser"
# The fail-soft destination: an activity nobody classified still has to say
# something true, and "working" is true of every one of them.
GROUP_WORKING = "working"

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ATTENTION = "attention"

_GROUP_WORDS: Dict[str, Tuple[str, str]] = {
    GROUP_INVESTIGATING: ("Изучает код", "Investigating"),
    GROUP_IMPLEMENTING: ("Вносит изменения", "Implementing"),
    GROUP_VERIFYING: ("Проверяет", "Verifying"),
    GROUP_BROWSER: ("Браузер", "Browser"),
    GROUP_WORKING: ("Работает", "Working"),
}

# The canonical dotted Core names the kernel publishes. A name absent from
# this table is not an error and is not printed -- it resolves to
# GROUP_WORKING, so a new tool never leaks its identifier into the interface
# while somebody gets around to classifying it.
_TOOL_GROUPS: Dict[str, str] = {
    "repo.read_file": GROUP_INVESTIGATING,
    "repo.read_lines": GROUP_INVESTIGATING,
    "repo.list_files": GROUP_INVESTIGATING,
    "repo.search": GROUP_INVESTIGATING,
    "repo.inspect": GROUP_INVESTIGATING,
    "git.status": GROUP_INVESTIGATING,
    "git.diff": GROUP_INVESTIGATING,
    "git.log": GROUP_INVESTIGATING,
    "repo.write_file": GROUP_IMPLEMENTING,
    "repo.edit_file": GROUP_IMPLEMENTING,
    "repo.command": GROUP_IMPLEMENTING,
    "git.commit": GROUP_IMPLEMENTING,
    "checks.run": GROUP_VERIFYING,
    "checks.run_affected": GROUP_VERIFYING,
    "tests.run": GROUP_VERIFYING,
}

# The kernel's proclaimed phases. Unknown phases fall to GROUP_WORKING rather
# than printing the identifier at somebody.
_PHASE_GROUPS: Dict[str, str] = {
    "execution": GROUP_IMPLEMENTING,
    "verification": GROUP_VERIFYING,
}

# Reasons the kernel is known to publish on WARNING/ERROR events. An unknown
# identifier renders as the generic word, never as itself: this layer exists
# to keep internal vocabulary off the screen.
_WARNING_WORDS: Dict[str, Tuple[str, str]] = {
    "repeated_refusal": (
        "модель повторно отказалась",
        "the model refused repeatedly",
    ),
    "tool_error": ("инструмент вернул ошибку", "a tool call failed"),
}
_GENERIC_WARNING = ("предупреждение", "warning")
_GENERIC_ERROR = ("ошибка выполнения", "run error")

_BULLET = "●"
_INDENT = "  "


def _russian_plural(count: int, one: str, few: str, many: str) -> str:
    if 11 <= count % 100 <= 14:
        return many
    remainder = count % 10
    if remainder == 1:
        return one
    if 2 <= remainder <= 4:
        return few
    return many


def _files_words(count: int, english: bool) -> str:
    if english:
        return f"{count} file" if count == 1 else f"{count} files"
    return f"{count} {_russian_plural(count, 'файл', 'файла', 'файлов')}"


def _identifier(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _tool_group(name: Any) -> str:
    tool = _identifier(name)
    if tool.startswith("browser."):
        return GROUP_BROWSER
    return _TOOL_GROUPS.get(tool, GROUP_WORKING)


# --------------------------------------------------------------------------- #
# The grouped model                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ActivityGroup:
    """One activity a person can read at a glance: a kind, counts, an outcome.

    Every field is an identifier from the catalogs above or an integer counted
    from events that actually happened. Free text from tool payloads has no
    field to arrive in, which is the containment argument in type form.
    """

    kind: str
    status: str
    phase: str = ""
    actions: int = 0
    failures: int = 0
    searches: int = 0
    files_read: int = 0
    files_changed: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    artifacts: int = 0
    warnings: Tuple[str, ...] = ()
    errors: Tuple[str, ...] = ()


class _OpenGroup:
    """The mutable accumulator behind one :class:`ActivityGroup`."""

    def __init__(self, kind: str, phase: str = "") -> None:
        self.kind = kind
        self.phase = phase
        self.actions = 0
        self.failures = 0
        self.searches = 0
        self.read_paths: set[str] = set()
        self.changed_paths: set[str] = set()
        self.checks_passed = 0
        self.checks_failed = 0
        self.artifacts = 0
        self.warnings: List[str] = []
        self.errors: List[str] = []

    def snapshot(self, status: str) -> ActivityGroup:
        if status != STATUS_RUNNING and (
            self.failures or self.errors or self.warnings or self.checks_failed
        ):
            status = STATUS_ATTENTION
        return ActivityGroup(
            kind=self.kind,
            status=status,
            phase=self.phase,
            actions=self.actions,
            failures=self.failures,
            searches=self.searches,
            files_read=len(self.read_paths),
            files_changed=len(self.changed_paths),
            checks_passed=self.checks_passed,
            checks_failed=self.checks_failed,
            artifacts=self.artifacts,
            warnings=tuple(self.warnings),
            errors=tuple(self.errors),
        )

    def is_empty(self) -> bool:
        return not (
            self.actions
            or self.searches
            or self.read_paths
            or self.changed_paths
            or self.checks_passed
            or self.checks_failed
            or self.artifacts
            or self.warnings
            or self.errors
        )


class ActivityStream:
    """Fold typed transcript events into chronological activity groups.

    Feed it the ``(kind, payload)`` pairs the transcript shadow publishes, in
    sequence order. Closed groups accumulate; :meth:`pop_closed` hands them to
    an incremental renderer exactly once, and :meth:`groups` always returns
    the whole chronology including the live group.
    """

    def __init__(self) -> None:
        self._closed: List[ActivityGroup] = []
        self._unread: List[ActivityGroup] = []
        self._open: Optional[_OpenGroup] = None
        self.finished_status: str = ""

    # ------------------------------------------------------------------ #
    # Feeding                                                             #
    # ------------------------------------------------------------------ #

    def feed(self, kind: Any, payload: Mapping[str, Any]) -> None:
        """Consume one typed event. Unknown kinds contribute nothing."""

        name = _identifier(kind)
        if name in ("ToolCallStarted", "ToolCallCompleted"):
            group = self._ensure(_tool_group(payload.get("tool")))
            if name == "ToolCallCompleted":
                group.actions += 1
                if payload.get("ok") is False:
                    group.failures += 1
                elif _identifier(payload.get("tool")) == "repo.search":
                    group.searches += 1
        elif name == "AgentPhaseChanged":
            phase = _identifier(payload.get("phase"))
            self._close()
            self._open = _OpenGroup(
                _PHASE_GROUPS.get(phase, GROUP_WORKING), phase=phase
            )
        elif name == "FileRead":
            path = _identifier(payload.get("path"))
            group = self._fold(GROUP_INVESTIGATING)
            if path:
                group.read_paths.add(path)
        elif name == "FileEdited":
            path = _identifier(payload.get("path"))
            group = self._fold(GROUP_IMPLEMENTING)
            if path:
                group.changed_paths.add(path)
        elif name == "TestRunCompleted":
            group = self._fold(GROUP_VERIFYING)
            if payload.get("ok") is False:
                group.checks_failed += 1
            else:
                group.checks_passed += 1
        elif name == "ArtifactCreated":
            self._fold(GROUP_WORKING).artifacts += 1
        elif name == "AgentWarning":
            self._fold(GROUP_WORKING).warnings.append(
                _identifier(payload.get("reason"))
            )
        elif name == "AgentErrorEvent":
            self._fold(GROUP_WORKING).errors.append(
                _identifier(payload.get("reason"))
            )
        elif name == "SessionStateChanged":
            self.finished_status = _identifier(payload.get("status"))
            self._close()

    def finish(self) -> None:
        """The run ended; whatever group is still open is now an outcome."""

        self._close()

    # ------------------------------------------------------------------ #
    # Reading                                                             #
    # ------------------------------------------------------------------ #

    def groups(self) -> Tuple[ActivityGroup, ...]:
        result = list(self._closed)
        if self._open is not None and not self._open.is_empty():
            result.append(self._open.snapshot(STATUS_RUNNING))
        return tuple(result)

    def pop_closed(self) -> Tuple[ActivityGroup, ...]:
        """Groups closed since the previous call, each returned exactly once."""

        unread = tuple(self._unread)
        self._unread.clear()
        return unread

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    def _ensure(self, family: str) -> _OpenGroup:
        """The open group of ``family``, closing a different one first.

        Chronological correctness over tidiness: a read after an edit opens a
        new investigating group rather than inflating the earlier one, so the
        order a person reads is the order things happened.
        """

        if self._open is None:
            self._open = _OpenGroup(family)
        elif self._open.kind != family:
            self._close()
            self._open = _OpenGroup(family)
        return self._open

    def _fold(self, family: str) -> _OpenGroup:
        """The open group if any, else a new one of the natural ``family``.

        Derived facts (a file read, a check finished) follow the tool call
        that produced them, so they land in the group that call opened even
        when the classifications differ -- a phase-anchored group absorbs the
        facts produced inside that phase.
        """

        if self._open is None:
            self._open = _OpenGroup(family)
        return self._open

    def _close(self) -> None:
        if self._open is None:
            return
        if not self._open.is_empty():
            snapshot = self._open.snapshot(STATUS_DONE)
            self._closed.append(snapshot)
            self._unread.append(snapshot)
        self._open = None


# --------------------------------------------------------------------------- #
# Rendering: catalog words and integers, nothing else                          #
# --------------------------------------------------------------------------- #


def _reason_line(reason: str, english: bool, generic: Tuple[str, str]) -> str:
    words = _WARNING_WORDS.get(reason, generic)
    return words[1] if english else words[0]


def render_group_lines(group: ActivityGroup, english: bool) -> List[str]:
    """One header plus one line per fact worth a row of the conversation."""

    words = _GROUP_WORDS.get(group.kind, _GROUP_WORDS[GROUP_WORKING])
    lines = [f"{_BULLET} {words[1] if english else words[0]}"]
    if group.searches:
        if english:
            plural = "search" if group.searches == 1 else "searches"
            lines.append(f"{_INDENT}Ran {group.searches} {plural}")
        else:
            noun = _russian_plural(group.searches, "поиск", "поиска", "поисков")
            lines.append(f"{_INDENT}Выполнил {group.searches} {noun}")
    if group.files_read:
        count = _files_words(group.files_read, english)
        lines.append(
            f"{_INDENT}Read {count}" if english else f"{_INDENT}Прочитал {count}"
        )
    if group.files_changed:
        count = _files_words(group.files_changed, english)
        lines.append(
            f"{_INDENT}{count} changed" if english else f"{_INDENT}Изменил {count}"
        )
    if group.checks_passed:
        if english:
            plural = "check" if group.checks_passed == 1 else "checks"
            lines.append(f"{_INDENT}✓ {group.checks_passed} {plural} passed")
        else:
            noun = _russian_plural(
                group.checks_passed, "проверка", "проверки", "проверок"
            )
            verb = "прошла" if noun == "проверка" else "прошли"
            lines.append(f"{_INDENT}✓ {group.checks_passed} {noun} {verb}")
    if group.checks_failed:
        if english:
            plural = "check" if group.checks_failed == 1 else "checks"
            lines.append(f"{_INDENT}✗ {group.checks_failed} {plural} failed")
        else:
            noun = _russian_plural(
                group.checks_failed, "проверка", "проверки", "проверок"
            )
            lines.append(f"{_INDENT}✗ {group.checks_failed} {noun} с ошибкой")
    if group.artifacts:
        if english:
            plural = "artifact" if group.artifacts == 1 else "artifacts"
            lines.append(f"{_INDENT}{group.artifacts} {plural} saved")
        else:
            noun = _russian_plural(
                group.artifacts, "артефакт", "артефакта", "артефактов"
            )
            lines.append(f"{_INDENT}Сохранено {group.artifacts} {noun}")
    if group.failures:
        if english:
            plural = "action" if group.failures == 1 else "actions"
            lines.append(f"{_INDENT}! {group.failures} {plural} failed")
        else:
            noun = _russian_plural(
                group.failures, "действие", "действия", "действий"
            )
            lines.append(f"{_INDENT}! {group.failures} {noun} с ошибкой")
    for reason in group.warnings:
        lines.append(f"{_INDENT}! {_reason_line(reason, english, _GENERIC_WARNING)}")
    for reason in group.errors:
        lines.append(f"{_INDENT}✗ {_reason_line(reason, english, _GENERIC_ERROR)}")
    return lines


def render_stream_lines(stream: ActivityStream, english: bool) -> List[str]:
    """The whole chronology, for a summary surface or a test's eyes."""

    lines: List[str] = []
    for group in stream.groups():
        lines.extend(render_group_lines(group, english))
    return lines
