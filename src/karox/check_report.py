"""Turn a raw check log into the short list of failures a repair needs.

A test runner states the cause of a failure once and then repeats it: the
FAILED line, the full traceback, the short summary, the final count. A model
that reads only the clipped ends of a long log can miss all of it, and a repair
turn built from the tail of a traceback can spend a whole edit on the wrong
file. This module extracts the failures themselves -- runner, test id, file,
line, code, a one-line message -- so the same information survives the elision
that keeps a turn affordable.

It is deliberately read-only and heuristic. Parsing never decides whether a run
passed: a check that exited zero is never turned into a failure here. The map
describes a failure the exit code already established, so an empty map means
"this log named no failure in a known format", never "the run was green".

Supported shapes: pytest, unittest, Jest/Vitest, node:test, go test, cargo test
(including its compile errors), ruff, mypy and tsc. Unknown output yields an
empty map rather than a guess; a fabricated failure sends a repair at a file
that never failed, which is worse than showing the raw tail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .check_parsers import (  # noqa: F401
    FAILURE_LIMIT as FAILURE_LIMIT,
    MESSAGE_LIMIT as MESSAGE_LIMIT,
    _ANSI as _ANSI,
    _WHITESPACE as _WHITESPACE,
    _LOCATION as _LOCATION,
    _TEST_RUNNERS as _TEST_RUNNERS,
    _LINT_RUNNERS as _LINT_RUNNERS,
    Failure as Failure,
    _combined as _combined,
    _clean as _clean,
    _message as _message,
    _relative as _relative,
    _fingerprint as _fingerprint,
    _row as _row,
    _collapse as _collapse,
    _priority as _priority,
    _report as _report,
    _pytest as _pytest,
    _pytest_sections as _pytest_sections,
    _pytest_section_for as _pytest_section_for,
    _first_error_line as _first_error_line,
    _split_node_reason as _split_node_reason,
    _unittest as _unittest,
    _jest_vitest as _jest_vitest,
    _merge_by_test as _merge_by_test,
    _drop_file_level_duplicates as _drop_file_level_duplicates,
    _fail_lines as _fail_lines,
    _test_key as _test_key,
    _jest_blocks as _jest_blocks,
    _vitest_blocks as _vitest_blocks,
    _last_location as _last_location,
    _node_test as _node_test,
    _go_test as _go_test,
    _cargo_test as _cargo_test,
    _lint as _lint,
    _int_or_none as _int_or_none,
    _positive_count as _positive_count,
    _DRIVE_PATH as _DRIVE_PATH,
    _PYTEST_SUMMARY as _PYTEST_SUMMARY,
    _PYTEST_SECTION as _PYTEST_SECTION,
    _PYTEST_LOCATION as _PYTEST_LOCATION,
    _UNITTEST_HEADER as _UNITTEST_HEADER,
    _UNITTEST_FILE as _UNITTEST_FILE,
    _UNITTEST_MESSAGE as _UNITTEST_MESSAGE,
    _JEST_BULLET as _JEST_BULLET,
    _VITEST_BULLET as _VITEST_BULLET,
    _VITEST_FILE as _VITEST_FILE,
    _JEST_MESSAGE as _JEST_MESSAGE,
    _FAIL_LINE as _FAIL_LINE,
    _NODE_SPLIT as _NODE_SPLIT,
    _NODE_TEST_AT as _NODE_TEST_AT,
    _NODE_FAILED as _NODE_FAILED,
    _NODE_MESSAGE as _NODE_MESSAGE,
    _GO_FAILED as _GO_FAILED,
    _GO_LOCATION as _GO_LOCATION,
    _GO_PANIC as _GO_PANIC,
    _GO_PANIC_LOCATION as _GO_PANIC_LOCATION,
    _CARGO_FAILED as _CARGO_FAILED,
    _CARGO_BLOCK as _CARGO_BLOCK,
    _CARGO_PANIC as _CARGO_PANIC,
    _CARGO_ERROR as _CARGO_ERROR,
    _CARGO_ARROW as _CARGO_ARROW,
    _RUFF as _RUFF,
    _RUFF_FIXABLE as _RUFF_FIXABLE,
    _RUFF_FORMAT as _RUFF_FORMAT,
    _MYPY as _MYPY,
    _TSC as _TSC,
)
from .check_targets import (  # noqa: F401
    MAX_TEST_FILES as MAX_TEST_FILES,
    MAX_TEST_TARGETS as MAX_TEST_TARGETS,
    MAX_IMPORT_BYTES as MAX_IMPORT_BYTES,
    is_test_path as is_test_path,
    select_test_targets as select_test_targets,
    _closeness as _closeness,
    _repository_test_files as _repository_test_files,
    _changed_stems as _changed_stems,
    _imports_of as _imports_of,
    _imports_touch as _imports_touch,
    render_targets as render_targets,
    rerun_targets as rerun_targets,
    _TEST_FILE as _TEST_FILE,
    _TEST_DIRS as _TEST_DIRS,
    _SKIP_DIRS as _SKIP_DIRS,
    _IMPORT as _IMPORT,
    _IMPORT_CACHE as _IMPORT_CACHE,
    _IMPORT_CACHE_LIMIT as _IMPORT_CACHE_LIMIT,
)

__all__ = [
    "Failure",
    "analyze",
    "render_failure_map",
    "render_targets",
    "fingerprints",
    "select_test_targets",
    "rerun_targets",
    "is_test_path",
]

RENDER_LIMIT = 1_600


def analyze(
    stdout: str = "",
    stderr: str = "",
    *,
    root: Optional[Path | str] = None,
) -> dict[str, object]:
    """The structured failure map of one finished check.

    ``root`` makes absolute traceback paths repository-relative, which keeps a
    fingerprint stable across runs in a fresh checkout. The report is JSON
    shaped because it travels inside the persisted tool result.
    """

    text = _clean(_combined(stdout, stderr))
    if not text.strip():
        return _report([], 0, 0, [])
    raw: list[dict[str, object]] = []
    raw.extend(_pytest(text))
    raw.extend(_unittest(text))
    raw.extend(_jest_vitest(text))
    raw.extend(_node_test(text))
    raw.extend(_go_test(text))
    raw.extend(_cargo_test(text))
    raw.extend(_lint(text))
    failures, total, repeats, all_fingerprints = _collapse(raw, root)
    return _report(failures, total, repeats, all_fingerprints)


def fingerprints(report: dict[str, object]) -> tuple[str, ...]:
    """The distinct failure identities, for comparing two repair rounds."""
    if not isinstance(report, dict):
        return ()
    stored = report.get("fingerprints")
    if isinstance(stored, list):
        return tuple(str(item) for item in stored if item)
    rows = report.get("failures")
    if not isinstance(rows, list):
        return ()
    return tuple(
        str(row.get("fingerprint") or "")
        for row in rows
        if isinstance(row, dict) and row.get("fingerprint")
    )


def render_failure_map(
    report: Optional[dict[str, object]], *, max_chars: int = RENDER_LIMIT
) -> str:
    """The compact block a model reads before the raw log.

    Nothing is invented: every line is one parsed failure with its own place.
    The block is bounded, and the count of unlisted failures stays visible so a
    reader knows the log holds more than this.
    """

    if not isinstance(report, dict):
        return ""
    rows = report.get("failures")
    total = report.get("total")
    if not isinstance(rows, list) or not rows or not isinstance(total, int) or total <= 0:
        return ""
    raw_repeats = report.get("repeats")
    repeats = (
        raw_repeats
        if isinstance(raw_repeats, int) and not isinstance(raw_repeats, bool)
        else 0
    )
    header = f"[KaroX failure map · {total} distinct"
    if repeats:
        header += f" · {repeats} repeated"
    header += "]"
    lines = [header]
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        failure = Failure(
            runner=str(row.get("runner") or ""),
            path=str(row.get("path") or ""),
            line=_int_or_none(row.get("line")),
            column=_int_or_none(row.get("column")),
            test_id=str(row.get("test_id") or ""),
            code=str(row.get("code") or ""),
            message=str(row.get("message") or ""),
            count=_positive_count(row.get("count")),
        )
        line = f"{index}. "
        if failure.runner in _LINT_RUNNERS:
            line += f"[{failure.runner}] "
        line += failure.label()
        if failure.display_code():
            line += f" {failure.display_code()}"
        if failure.message:
            line += f" — {failure.message}"
        if failure.count > 1:
            line += f" (x{failure.count})"
        lines.append(line)
    hidden = total - len(rows)
    if hidden > 0:
        lines.append(
            f"… {hidden} more not listed; rerun one target to see its detail"
        )
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[: max_chars - 40].rstrip() + "\n… [failure map truncated]"
    return body


# --------------------------------------------------------------------------- #
# Normalisation                                                               #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# pytest                                                                      #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# unittest                                                                    #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Jest / Vitest                                                               #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# node:test                                                                   #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# go test                                                                     #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# cargo test / cargo build                                                    #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Linters and type checkers                                                   #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Impacted test targets                                                       #
# --------------------------------------------------------------------------- #

#: Directory names that hold tests in the layouts pytest collects: a top-level
#: ``tests/`` tree and the package-local ``pkg/sub/tests/`` of sympy, numpy or
#: astropy. A focused-run contract that knew only the first refused every
#: sympy test file and cost the run two turns before it fell back.
#: Directories a repository-wide test search never enters: environments,
#: build output and caches hold copies of tests that are not the project's.


#: A generated or vendored test file above this size is not scanned for
#: imports; reading thousands of them whole was the only unbounded work in the
#: focused-target path.
