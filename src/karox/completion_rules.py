"""Shared completion predicates: requirements, docs, test-edit rules, diagnostics and identity."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess
from typing import Any, Optional
from .requirements import ledger_digest
from .web_check import diagnostic_pages, is_scratch_artifact



_TEXT = {".md", ".rst"}

NO_PRODUCER = "no approved producer for "

_DOC_DIRS = {"docs", "doc", "documentation"}

_ROOT_DOC_PREFIXES = (
    "readme", "changelog", "contributing", "license", "security",
    "support", "code_of_conduct", "quickstart", "release_notes",
)

_BEHAVIOR = re.compile(r"\b(?:implement|bugfix|refactor|runtime|behavior|behaviour|execute|launch|bugs?|crash(?:es|ed)?|raises?|exceptions?|regression|traceback|\w+Error)\b|исправ.*ошиб|поведен|запуск", re.I)

_TEST_CHANGE_REQUEST = re.compile(
    r"\b(?:add|write|create|update|modify|change|extend|adjust|revise)\w*\b"
    r".{0,80}(?<![-_.])\b(?:tests?|test\s+cases?|spec\s+(?:files?|tests?)|specs\s+for)\b"
    r"|(?:добав\w*|напиш\w*|созда\w*|обнов\w*|измен\w*|расшир\w*|допол\w*)"
    r".{0,80}(?:тест\w*|спек\w*)",
    re.I,
)

_NEGATED_TEST_CHANGE = re.compile(
    r"(?:\b(?:(?:do|does|should|must|may|will|would|can|could)\s+not|"
    r"can't|cannot|don't|dont|never|without|avoid)\b"
    r".{0,45}\b(?:add(?:ing)?|writ(?:e|ing)|creat(?:e|ing)|updat(?:e|ing)|"
    r"modify(?:ing)?|chang(?:e|ing)|extend(?:ing)?)\w*\b"
    r".{0,45}\b(?:tests?|test\s+cases?|specs?)\b)"
    r"|(?:(?:\bне\b|\bбез\b).{0,45}"
    r"(?:добав\w*|пис\w*|созда\w*|обнов\w*|измен\w*|меня\w*|расшир\w*|допол\w*)"
    r".{0,45}(?:тест\w*|спек\w*))",
    re.I,
)

_SENTENCE_BREAK = re.compile(r"(?<=[.!?;])\s+|\n+")

#: The matched verb is a noun ("the change", "all changes", "an update").
_VERB_AS_NOUN = re.compile(
    r"(?i)\b(?:the|a|an|all|any|this|that|these|those|its|my|our|their|of|no|recent|latest)\s*$"
)
#: The test noun belongs to the reporter ("my test") or names a suite that broke.
_NOT_A_REQUEST = re.compile(
    r"(?i)\b(?:my|our)\s+(?:\w+\s+)?tests?\b|\bbroke\b|\bbreaks\b|\bfails?\b|\bfailing\b"
)
_ALREADY_DONE = re.compile(
    r"(?i)\balready\b.{0,40}\b(?:taken care|changed|modified|updated|written|added|done)\b"
    r"|\btaken care of\b"
)
#: Words after which the tests are the yardstick, not the object of the verb:
#: "update the implementation so that the existing tests pass", "update the
#: code based on tests", "change X without breaking tests". Such a sentence
#: owed a test edit, and lost the test ban of the sentence before it.
_TESTS_AS_CRITERION = re.compile(
    r"\b(?:so|such\s+that|until|based\s+on|according\s+to|against|to\s+make|to\s+get|"
    r"making|in\s+order\s+to|keep(?:s|ing)?|without)\b",
    re.I,
)

_CONDITIONAL_REQUEST = re.compile(
    r"^\s*(?:[-*+]|\d+[.)])?\s*\[[ xX]?\]"
    r"|\b(?:when|where|if)\s+(?:applicable|appropriate|needed|necessary|possible|relevant)\b"
    r"|\b(?:if|when)\s+(?:it\s+)?(?:applies|makes\s+sense)\b"
    r"|(?:при\s+необходимости|если\s+(?:нужно|необходимо|применимо))",
    re.I,
)

_TESTS_OUT_OF_SCOPE = re.compile(
    r"(?:\b(?:not|never)\b\s+(?:(?:in|to|into|inside|within)\s+)?"
    r"(?:the\s+)?(?:tests?|test\s+files?|specs?)\b"
    r"|(?:rather|instead)\s+(?:than|of)\s+(?:in\s+|inside\s+|within\s+)?"
    r"(?:the\s+)?(?:tests?|test\s+files?|specs?)\b"
    r"|outside\s+(?:of\s+)?(?:the\s+)?(?:tests?|test\s+files?)\b)",
    re.I,
)

_TEST_EXECUTION_IN_MATCH = re.compile(
    r"\b(?:run|execute|rerun|re-run)\w*\b|(?:запуст\w*|выполн\w*)",
    re.I,
)

def diagnostic_paths(core: Any, record: Any) -> frozenset[str]:
    """Created pages that nothing delivers (``web_check.diagnostic_pages``).

    Like a ``_probe.html``, such a page is the run's own instrument: editing it
    changes nothing the user opens, so it neither owes proof nor makes the
    deliverable's proof stale. Fail-closed: any error marks nothing.
    """

    try:
        created = list(getattr(record, "created_files", None) or ())
        created.extend(
            path for path in _created_then_deleted(core, record) if path not in created
        )
        if not created:
            return frozenset()
        named = " ".join([str(record.task or ""), *_active_requirement_texts(record)])
        return frozenset(diagnostic_pages(Path(core.repository), created, named))
    except Exception:  # noqa: BLE001 - the stricter answer is always safe
        return frozenset()

def _created_then_deleted(core: Any, record: Any) -> list[str]:
    """Files this run created and has since deleted.

    ``created_files`` forgets a file once the run deletes it, and the run is
    told to delete its own probes as soon as they have answered. A measured 3D
    run deleted its ``self-test.html``; the page stopped being diagnostic at
    that moment, entered the identity as a deleted file and made every proof
    and every attestation stale. The transcript still shows the first write
    created the file (no previous bytes), so a file that is gone again was
    never the user's and is not part of what the user receives.
    """

    first: dict[str, bool] = {}
    for entry in record.provider_history:
        if not isinstance(entry, dict):
            continue
        result = entry.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            continue
        data = result.get("data")
        if not isinstance(data, dict) or data.get("dry_run") is True:
            continue
        name = entry.get("core_name")
        if name == "repo.write_file":
            operations = [data]
        elif name in {"repo.command", "repo.apply_patch"}:
            operations = [op for op in data.get("operations") or () if isinstance(op, dict)]
        else:
            continue
        for operation in operations:
            path = operation.get("path")
            if isinstance(path, str) and operation.get("changed") is True:
                first.setdefault(path, "previous_sha256" in operation
                                 and operation.get("previous_sha256") is None)
    gone: list[str] = []
    for path, created in first.items():
        if not created or path not in record.changed_files:
            continue
        try:
            if core.safe_path(path).exists():
                continue
        except Exception:  # noqa: BLE001 - an unresolvable path stays bound
            continue
        gone.append(path)
    return gone

def _deliverable(path: str, diagnostic: frozenset[str]) -> bool:
    return not is_scratch_artifact(path) and Path(path).as_posix() not in diagnostic

def _active_requirement_texts(record: Any) -> list[str]:
    entries = [
        entry
        for entry in record.requirements
        if isinstance(entry, dict) and entry.get("status", "active") == "active"
    ]
    texts = [
        str(entry.get("text", "")).strip()
        for entry in entries
        if str(entry.get("text", "")).strip()
    ]
    return texts or [str(record.task)]

def _explicit_test_change_required(record: Any) -> bool:
    for text in _active_requirement_texts(record):
        # Judge one sentence at a time. A scope sentence must not borrow the
        # word "tests" from the next sentence, and a negated sentence must not
        # swallow a following positive request.
        for sentence in _SENTENCE_BREAK.split(text):
            if _TESTS_OUT_OF_SCOPE.search(sentence) or _CONDITIONAL_REQUEST.search(sentence):
                continue
            cleaned = _NEGATED_TEST_CHANGE.sub("", sentence)
            for match in _TEST_CHANGE_REQUEST.finditer(cleaned):
                # "change code, then run tests" is verification work, not a request
                # to edit tests. The broad matcher deliberately understands varied
                # phrasing, so reject matches whose path to the test noun crosses
                # an execution verb.
                if _TEST_EXECUTION_IN_MATCH.search(match.group(0)):
                    continue
                # Audit 2026-11-13: the SWE-bench preamble ("all changes to
                # any of the test files ... already taken care of") and bug
                # reports ("the change in 3.1 broke the test suite", "a
                # fixture in my test") read as requests, so a correct source
                # fix was held for an unedited test and lost its test ban.
                if _VERB_AS_NOUN.search(cleaned[: match.start()]):
                    continue
                if _NOT_A_REQUEST.search(match.group(0)) or _ALREADY_DONE.search(sentence):
                    continue
                if _TESTS_AS_CRITERION.search(match.group(0)) and not any(
                    # "add a test so that ..." and "... so it parses, and add
                    # tests" still ask for one: some clause holds verb and noun.
                    _TEST_CHANGE_REQUEST.search(clause)
                    for clause in _TESTS_AS_CRITERION.split(match.group(0))
                ):
                    continue
                return True
    return False

_FORBIDS_TEST_EDITS = re.compile(
    r"(?:\b(?:(?:do|does|should|must|may|will|can|could)\s+not|can't|cannot|"
    r"don't|dont|never|avoid)\b"
    r".{0,30}\b(?:modify(?:ing)?|chang(?:e|ing)|edit(?:ing)?|touch(?:ing)?|"
    r"alter(?:ing)?|updat(?:e|ing)|rewrit(?:e|ing))\b"
    r".{0,40}\b(?:tests|test\s+files?|test\s+suites?|test\s+cases?|"
    r"existing\s+tests?|unit\s+tests?)\b)"
    r"|(?:\bне\b.{0,30}(?:измен\w*|меня\w*|редактир\w*|трога\w*|прав\w*)"
    r".{0,40}тест\w*)",
    re.I,
)

def _test_edit_ban(record: Any) -> str:
    """The task sentence that keeps the existing tests unchanged, or "".

    A sandbox SWE run (2026-11-04, swe-pytest-10356) copied the upstream
    regression test into ``testing/test_mark.py`` two steps before finishing,
    although the task said "Do not modify tests" and "Changes should be made
    in the source tree, not in the test files". The hidden grader restores that
    file, so the edit proved nothing and broke the task's rules. A task that
    also asks for a test change is ambiguous and keeps its old handling.
    """

    if _explicit_test_change_required(record):
        return ""
    texts = [str(getattr(record, "task", "") or ""), *_active_requirement_texts(record)]
    for text in texts:
        for sentence in _SENTENCE_BREAK.split(text):
            if _FORBIDS_TEST_EDITS.search(sentence) or _TESTS_OUT_OF_SCOPE.search(sentence):
                return " ".join(sentence.split())
    return ""

def _changed_existing_tests(core: Any, record: Any) -> list[str]:
    """Committed test files that now hold other bytes than at the run's start.

    ``changed_files`` remembers every file the run ever wrote, so the starting
    bytes come from the first mutation's ``previous_sha256``: a test file the
    run restored to its original content is not reported. Only files tracked
    at ``HEAD`` count: a ``__pycache__/test_*.pyc`` left by a probe and then
    cleaned up is not the user's test. A file without a recorded starting
    digest, one this run created, or any file outside Git is never reported.
    """

    created = {str(item) for item in (getattr(record, "created_files", None) or ())}
    candidates = [
        path
        for path in dict.fromkeys(record.changed_files)
        if isinstance(path, str)
        and _is_test_path(path)
        and path not in created
        and "__pycache__" not in Path(path).parts
    ]
    candidates = [path for path in candidates if _tracked_at_head(core, path)]
    if not candidates:
        return []
    original: dict[str, Optional[str]] = {}
    for entry in record.provider_history:
        if not isinstance(entry, dict):
            continue
        result = entry.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            continue
        data = result.get("data")
        if not isinstance(data, dict) or data.get("dry_run") is True:
            continue
        name = entry.get("core_name")
        if name in {"repo.write_file", "repo.edit_file", "web.fetch"}:
            operations = [data]
        elif name in {"repo.command", "repo.apply_patch"}:
            operations = [op for op in data.get("operations") or () if isinstance(op, dict)]
        else:
            continue
        for operation in operations:
            path = operation.get("path")
            if (
                isinstance(path, str)
                and path in candidates
                and path not in original
                and "previous_sha256" in operation
            ):
                original[path] = operation.get("previous_sha256")
    changed = []
    for path in candidates:
        before = original.get(path)
        if not isinstance(before, str):
            continue
        try:
            target = core.safe_path(path)
            now = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        except Exception:  # noqa: BLE001 - an unreadable file is not evidence of an edit
            continue
        if now != before:
            changed.append(path)
    return changed

def _tracked_at_head(core: Any, relative: str) -> bool:
    """Whether ``relative`` is committed at ``HEAD``; any failure says no.

    ``./`` keeps the path relative to the workspace, which may sit below the
    Git root.
    """

    try:
        tracked = subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:./{Path(relative).as_posix()}"],
            cwd=str(core.repository), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:  # noqa: BLE001 - no Git, no claim about the user's tests
        return False
    return tracked.returncode == 0

def _is_test_path(relative: str) -> bool:
    path = Path(relative)
    parts = {part.lower() for part in path.parts[:-1]}
    if parts & {"test", "tests", "spec", "specs", "__tests__"}:
        return True
    name = path.name.lower()
    stem = path.stem.lower()
    if name in {"tests.py", "conftest.py"}:
        return True
    return (
        name.startswith(("test_", "test-", "spec_", "spec-"))
        or stem.endswith(("_test", "_spec"))
        or ".test." in name
        or ".spec." in name
    )

def is_doc(relative: str) -> bool:
    path = Path(relative)
    if path.suffix.lower() not in _TEXT:
        return False
    parts = path.parts
    if parts and parts[0].lower() in _DOC_DIRS:
        return True
    if len(parts) != 1:
        return False
    stem = path.stem.lower()
    # Root markdown is not automatically documentation. Agent instructions,
    # prompt resources and skill files often live there too and affect runtime
    # behavior. Only conventional human-facing documentation names get the
    # cheap docs-only path.
    return any(stem.startswith(prefix) for prefix in _ROOT_DOC_PREFIXES)

def docs_only(record: Any) -> bool:
    return bool(record.changed_files) and all(is_doc(p) for p in record.changed_files) and not _BEHAVIOR.search(record.task)

def identity(core: Any, record: Any) -> dict[str, Any]:
    """Hash only known task inputs (reads + changes), not generated tree noise.

    Missing files have a stable deletion marker. Unreadable/outside paths fail
    closed. Checks also compare before/after identities to reject concurrent edits.
    Scratch artifacts are not task inputs: deleting a probe after a passing
    visual check must not make the deliverable's proof stale. Neither are the
    run's own unreferenced harness pages (``diagnostic_paths``).
    """
    diagnostic = diagnostic_paths(core, record)
    paths = {p for p in record.changed_files if _deliverable(p, diagnostic)}
    for requirement in record.requirements:
        if (
            not isinstance(requirement, dict)
            or requirement.get("status", "active") != "active"
        ):
            continue
        source_path = requirement.get("source_path")
        if (
            isinstance(source_path, str)
            and source_path
            and _deliverable(source_path, diagnostic)
        ):
            paths.add(source_path)
    for entry in record.provider_history:
        if entry.get("core_name") in {"repo.read_file", "repo.read_lines"}:
            result = entry.get("result") or {}
            path = (result.get("data") or {}).get("path")
            if result.get("ok") and isinstance(path, str) and _deliverable(path, diagnostic):
                paths.add(path)
    files = {}
    for relative in sorted(paths):
        try:
            path = core.safe_path(relative)
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
        except Exception:
            return {}
    return {"files": files, "requirements": ledger_digest(record.requirements), "task": record.task}
