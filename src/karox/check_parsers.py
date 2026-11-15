"""Runner-output parsers: turn a raw check log into Failure records.

Read-only and heuristic; parsing never decides that a run passed."""

from __future__ import annotations

from dataclasses import dataclass

import hashlib
import re
from pathlib import Path, PureWindowsPath
from typing import Optional, Sequence
from urllib.parse import unquote


FAILURE_LIMIT = 8

MESSAGE_LIMIT = 220

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_WHITESPACE = re.compile(r"\s+")

_LOCATION = re.compile(
    r"(?P<path>(?:[A-Za-z]:[\\/]|file://)?[^\s:()\[\]{}]+)"
    r":(?P<line>\d+)(?::(?P<column>\d+))?"
)

_TEST_RUNNERS = (
    "pytest",
    "unittest",
    "jest",
    "vitest",
    "node-test",
    "go-test",
    "cargo-test",
)

_LINT_RUNNERS = ("ruff", "mypy", "tsc", "cargo-build")

@dataclass(frozen=True)
class Failure:
    """One distinct failure, with the place a repair has to look at."""

    runner: str
    path: str = ""
    line: Optional[int] = None
    column: Optional[int] = None
    test_id: str = ""
    code: str = ""
    message: str = ""
    count: int = 1
    fingerprint: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "runner": self.runner,
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "test_id": self.test_id,
            "code": self.code,
            "message": self.message,
            "count": self.count,
            "fingerprint": self.fingerprint,
        }

    def label(self) -> str:
        """Where the failure is, in the order a reader scans it."""
        place = self.path or ""
        if place and self.line is not None:
            place = f"{place}:{self.line}"
        test_id = self.test_id
        if "::" in test_id:
            # The file is already printed; the node's test name is the part
            # that differs between failures in that file.
            test_id = test_id.rsplit("::", 1)[-1]
        parts = [part for part in (place, test_id) if part]
        return " · ".join(parts) or self.runner

    def display_code(self) -> str:
        """Only lint/build codes add information; FAILED is already the label."""
        if self.runner in _LINT_RUNNERS or self.runner.startswith("cargo"):
            return self.code
        return ""

def _combined(stdout: str, stderr: str) -> str:
    # A runner that prints its report to stderr is common (Jest, cargo). The
    # map must see both streams or it silently reports "no failures" for a red
    # run -- the exact false clean the evidence chain exists to refuse.
    first = stdout if isinstance(stdout, str) else ""
    second = stderr if isinstance(stderr, str) else ""
    if first.strip() and second.strip():
        return f"{first}\n{second}"
    return first or second

def _clean(text: str) -> str:
    # Windows runners print CRLF. Section headers are line-anchored, so a bare
    # `$` never matches before the `\r` and the whole FAILURES block would be
    # invisible on the platform this product runs on most.
    return _ANSI.sub("", text or "").replace("\r\n", "\n").replace("\r", "\n")

def _message(text: str, *, limit: int = MESSAGE_LIMIT) -> str:
    cleaned = _WHITESPACE.sub(" ", _clean(text)).strip()
    return cleaned[:limit]

def _int_or_none(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None

def _positive_count(value: object) -> int:
    count = _int_or_none(value)
    return count if count is not None and count > 0 else 1

_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")

def _relative(path: str, root: Optional[Path | str]) -> str:
    raw = (path or "").strip().strip("\"'`")
    if raw.startswith("file://"):
        raw = unquote(raw[len("file://") :])
        if re.match(r"^/[A-Za-z]:", raw):
            # file:///D:/work/x.py -- the leading slash is URL syntax.
            raw = raw[1:]
    if root is not None:
        try:
            base = Path(root).expanduser().resolve(strict=False)
            candidate = Path(raw)
            if candidate.is_absolute():
                return candidate.resolve(strict=False).relative_to(base).as_posix()
        except (OSError, ValueError):
            pass
        if _DRIVE_PATH.match(raw) and _DRIVE_PATH.match(str(root)):
            # A Windows log read on another system (or a root given in Windows
            # form): the native Path above does not see a drive path as
            # absolute there, so compare the two as Windows paths.
            try:
                return (
                    PureWindowsPath(raw).relative_to(PureWindowsPath(str(root))).as_posix()
                )
            except ValueError:
                pass
    return raw.replace("\\", "/")

def _fingerprint(runner: str, path: str, line: Optional[int], test_id: str, message: str) -> str:
    material = "\0".join(
        (runner, path, "" if line is None else str(line), test_id, message)
    )
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:12]

def _row(
    runner: str,
    *,
    path: str = "",
    line: object = None,
    column: object = None,
    test_id: str = "",
    code: str = "",
    message: str = "",
) -> dict[str, object]:
    return {
        "runner": runner,
        "path": path,
        "line": _int_or_none(line),
        "column": _int_or_none(column),
        "test_id": test_id,
        "code": code,
        "message": _message(message),
    }

def _collapse(
    raw: Sequence[dict[str, object]], root: Optional[Path | str]
) -> tuple[list[Failure], int, int, list[str]]:
    """Deduplicate by identity while keeping the order a reader expects."""

    ordered = sorted(
        enumerate(raw),
        key=lambda item: (_priority(str(item[1].get("runner") or "")), item[0]),
    )
    by_fingerprint: dict[str, Failure] = {}
    order: list[str] = []
    for _, row in ordered:
        runner = str(row.get("runner") or "")
        path = _relative(str(row.get("path") or ""), root)
        line = _int_or_none(row.get("line"))
        test_id = str(row.get("test_id") or "")
        message = str(row.get("message") or "")
        fingerprint = _fingerprint(runner, path, line, test_id, message)
        previous = by_fingerprint.get(fingerprint)
        if previous is None:
            by_fingerprint[fingerprint] = Failure(
                runner=runner,
                path=path,
                line=line,
                column=_int_or_none(row.get("column")),
                test_id=test_id,
                code=str(row.get("code") or ""),
                message=message,
                count=1,
                fingerprint=fingerprint,
            )
            order.append(fingerprint)
        else:
            by_fingerprint[fingerprint] = Failure(
                runner=previous.runner,
                path=previous.path,
                line=previous.line,
                column=previous.column,
                test_id=previous.test_id,
                code=previous.code,
                message=previous.message,
                count=previous.count + 1,
                fingerprint=previous.fingerprint,
            )
    failures = [by_fingerprint[key] for key in order]
    total = len(failures)
    repeats = sum(failure.count - 1 for failure in failures)
    # The rendered list is capped, but repeat detection must see every distinct
    # failure: two rounds that differ only in the ninth failure are not the
    # same set, and calling them identical would mislabel real progress.
    return (
        failures[:FAILURE_LIMIT],
        total,
        repeats,
        [failure.fingerprint for failure in failures],
    )

def _priority(runner: str) -> int:
    if runner in _TEST_RUNNERS:
        return 0
    return 1

def _report(
    failures: Sequence[Failure], total: int, repeats: int, all_fingerprints: Sequence[str]
) -> dict[str, object]:
    return {
        "failures": [failure.to_dict() for failure in failures],
        "total": total,
        "repeats": repeats,
        "runners": sorted({failure.runner for failure in failures}),
        # Every distinct identity, not only the rendered eight. Repeat
        # detection compares these; the list is bounded by the log size.
        "fingerprints": list(all_fingerprints),
    }

_PYTEST_SUMMARY = re.compile(r"^(?P<kind>FAILED|ERROR) (?P<rest>.+)$", re.M)

_PYTEST_SECTION = re.compile(r"^_{3,} (?P<name>.+?) _{3,}$", re.M)

_PYTEST_LOCATION = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+)(?::(?P<column>\d+))?:(?: (?P<msg>.*))?$", re.M
)

def _pytest(text: str) -> list[dict[str, object]]:
    sections = _pytest_sections(text)
    rows: list[dict[str, object]] = []
    summaries = list(_PYTEST_SUMMARY.finditer(text))
    # Sections come in summary order: two files' same-named tests take one each.
    used: set[int] = set()
    for match in summaries:
        node, reason = _split_node_reason(match.group("rest").strip())
        if not node or (".py" not in node and "::" not in node):
            # unittest's own `FAILED (failures=1, errors=1)` summary uses the
            # same word; only a path or a node id is a pytest failure.
            continue
        node_path = node.split("::", 1)[0]
        tail = node.split("::")[-1] if "::" in node else ""
        # pytest titles a method's block "Class.method": two classes with a
        # same-named test used to share the first class's block.
        qualified = ".".join(node.split("::")[1:]) if "::" in node else ""
        section = (
            _pytest_section_for(qualified, sections, used) if "." in qualified else None
        ) or _pytest_section_for(tail, sections, used)
        if "::" not in node:
            # A collection error (a SyntaxError or ImportError after an edit):
            # its "ERROR collecting <path>" block holds the line that broke.
            section = _pytest_section_for(f"ERROR collecting {node_path}", sections, used)
        if section is not None:
            used.add(id(section))
        # The deepest traceback frame may live in the module under test, not
        # in the test file. Its path and line must travel together: pairing the
        # node's test file with the source file's line sends the repair to a
        # line that does not exist in the file it names.
        path = section[1] if section is not None and section[1] else node_path
        line = section[2] if section is not None else None
        column = section[3] if section is not None else None
        message = reason or (section[4] if section is not None else "") or "failed"
        rows.append(
            _row(
                "pytest",
                path=path,
                line=line,
                column=column,
                test_id=node,
                code=match.group("kind"),
                message=message,
            )
        )
    if rows:
        return rows
    # No short summary (for example pytest stopped with -x and no summary
    # block): the failure sections still name the test and the deepest frame.
    for name, path, line, column, message in sections:
        rows.append(
            _row(
                "pytest",
                path=path,
                line=line,
                column=column,
                test_id=name,
                code="FAILED",
                message=message or "failed",
            )
        )
    return rows

def _pytest_sections(
    text: str,
) -> list[tuple[str, str, Optional[int], Optional[int], str]]:
    marks = list(_PYTEST_SECTION.finditer(text))
    result: list[tuple[str, str, Optional[int], Optional[int], str]] = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        block = text[mark.end() : end]
        locations = list(_PYTEST_LOCATION.finditer(block))
        # pytest prints the traceback outermost-first, so the last location is
        # the frame that actually raised -- the place a repair must edit.
        location = locations[-1] if locations else None
        name = mark.group("name").strip()
        if location is None:
            result.append((name, "", None, None, ""))
            continue
        message = (location.group("msg") or "").strip()
        if not message:
            # A call line (`path:9:`) carries the frame but not the error; the
            # error line above it is more useful than an empty message.
            message = _first_error_line(block)
        result.append(
            (
                name,
                location.group("path"),
                int(location.group("line")),
                _int_or_none(int(location.group("column")) if location.group("column") else None),
                message,
            )
        )
    return result

def _pytest_section_for(
    tail: str,
    sections: Sequence[tuple[str, str, Optional[int], Optional[int], str]],
    used: Optional[set[int]] = None,
) -> Optional[tuple[str, str, Optional[int], Optional[int], str]]:
    if not tail:
        return None
    if used:
        sections = [section for section in sections if id(section) not in used] or sections
    # Exact first, then a whole trailing name, then a substring: a bare
    # substring pass let "test_parse" take the section of "test_parse_long".
    for accept in (
        lambda name: name == tail,
        lambda name: name.endswith(("." + tail, "::" + tail, " " + tail)),
        lambda name: tail in name,
    ):
        for section in sections:
            if accept(section[0]):
                return section
    return None

def _first_error_line(block: str) -> str:
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("E "):
            return stripped[2:].strip()
    return ""

def _split_node_reason(rest: str) -> tuple[str, str]:
    """Split `nodeid - reason` without cutting a parametrised node id.

    pytest node ids may contain spaces and even ` - ` inside `[...]`, so the
    separator only counts at bracket depth zero.
    """

    depth = 0
    for index, character in enumerate(rest):
        if character == "[":
            depth += 1
        elif character == "]":
            depth = max(0, depth - 1)
        elif depth == 0 and rest.startswith(" - ", index):
            return rest[:index].strip(), rest[index + 3 :].strip()
    return rest.strip(), ""

# A subTest adds " (i=0)" or " [msg] (i=0)" after the qualified name.
_UNITTEST_HEADER = re.compile(
    r"^(?P<kind>FAIL|ERROR): (?P<test>\S+) \((?P<qual>[\w.]+)\)(?: .*)?$", re.M
)

# "in <module>" / "in <lambda>": an import-time error raises outside a function.
_UNITTEST_FILE = re.compile(
    r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)\s*$', re.M
)

_UNITTEST_MESSAGE = re.compile(
    r"^(?P<exc>[A-Za-z_][\w.]*(?:Error|Exception|Failure))(?::\s*(?P<msg>.*))?$"
)

def _unittest(text: str) -> list[dict[str, object]]:
    marks = list(_UNITTEST_HEADER.finditer(text))
    rows: list[dict[str, object]] = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        block = text[mark.end() : end]
        frames = list(_UNITTEST_FILE.finditer(block))
        location = frames[-1] if frames else None
        message = ""
        for line in reversed(block.splitlines()):
            match = _UNITTEST_MESSAGE.match(line.strip())
            if match:
                message = (
                    f"{match.group('exc')}: {match.group('msg')}"
                    if match.group("msg")
                    else match.group("exc")
                )
                break
        rows.append(
            _row(
                "unittest",
                path=location.group("path") if location else "",
                line=location.group("line") if location else None,
                test_id=mark.group("qual"),
                code=mark.group("kind"),
                message=message or mark.group("kind"),
            )
        )
    return rows

_JEST_BULLET = re.compile(r"^\s*●\s+(?P<test>\S.*?)\s*$", re.M)

_VITEST_BULLET = re.compile(
    r"^\s*[×✕✗]\s+(?P<test>\S.*?)(?:\s+\(?\d+(?:\.\d+)?\s*ms\)?)?\s*$", re.M
)

_VITEST_FILE = re.compile(
    r"^\s*[❯>]\s+(?P<path>[^\s(]+\.(?:test|spec)\.[cm]?[jt]sx?)\b.*$", re.M
)

_JEST_MESSAGE = re.compile(
    r"^\s*(?P<msg>(?:expect\(.*|Expected:.*|Received:.*|"
    r"[A-Za-z_$][\w.$]*(?:Error|Exception)(?::.*)?|"
    r"Cannot .+|Unable to .+|.*\.(?:toBe|toEqual|toMatch)\w*\(.*))$"
)

def _jest_vitest(text: str) -> list[dict[str, object]]:
    rows = _merge_by_test([*_jest_blocks(text), *_vitest_blocks(text)])
    fail_lines = _fail_lines(text)
    if not fail_lines:
        return _drop_file_level_duplicates(rows)
    if not rows:
        return fail_lines
    # A FAIL line names the file a bullet could not locate. Bullets carry the
    # message, so a FAIL line is only a separate failure when no bullet already
    # names that test -- otherwise one failure would be counted twice with two
    # different messages and defeat repeat detection.
    keys = {_test_key(str(row.get("test_id") or "")): row for row in rows if row.get("test_id")}
    for line in fail_lines:
        key = _test_key(str(line.get("test_id") or ""))
        if key and key in keys:
            target = keys[key]
            if not target.get("path") and line.get("path"):
                target["path"] = line["path"]
            continue
        rows.append(line)
    return _drop_file_level_duplicates(rows)

def _merge_by_test(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """One failure per test, even when two reporters describe it differently.

    Jest prints `✕ name` and `● suite › name`; Vitest prints `× suite > name`
    and `FAIL path > suite > name`. They are the same failure. The first row
    keeps its place, and the others only fill what it lacked: a location, a
    column, or a real message in place of the filler "failed".
    """

    merged: list[dict[str, object]] = []
    by_key: dict[str, dict[str, object]] = {}
    for row in rows:
        key = _test_key(str(row.get("test_id") or ""))
        if not key:
            merged.append(row)
            continue
        target = by_key.get(key)
        if target is None:
            by_key[key] = row
            merged.append(row)
            continue
        for field in ("path", "line", "column"):
            if not target.get(field) and row.get(field):
                target[field] = row[field]
        if str(target.get("message") or "") in ("", "failed") and row.get("message"):
            target["message"] = row["message"]
    return merged

def _drop_file_level_duplicates(
    rows: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """A `FAIL path` line is not a second failure when its tests are listed."""

    covered = {str(row.get("path") or "") for row in rows if row.get("test_id")}
    return [
        row
        for row in rows
        if row.get("test_id") or str(row.get("path") or "") not in covered
    ]

_FAIL_LINE = re.compile(
    r"^\s*FAIL\s+(?P<path>[^\s>›]+)(?:\s*[>›]\s*(?P<test>.+?))?\s*$", re.M
)

def _fail_lines(text: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for match in _FAIL_LINE.finditer(text):
        test = (match.group("test") or "").strip()
        rows.append(
            _row(
                "vitest",
                path=match.group("path"),
                test_id=test,
                message="" if test else "test file failed",
            )
        )
    return rows

def _test_key(name: str) -> str:
    """The last segment of `suite > test`, so two reporters agree on a name."""
    cleaned = name.strip()
    for separator in ("›", ">"):
        if separator in cleaned:
            cleaned = cleaned.rsplit(separator, 1)[-1].strip()
    return cleaned

def _jest_blocks(text: str) -> list[dict[str, object]]:
    marks = list(_JEST_BULLET.finditer(text))
    rows: list[dict[str, object]] = []
    for index, mark in enumerate(marks):
        test = mark.group("test").strip()
        if not test or test.lower().startswith(("console", "stderr", "stdout")):
            continue
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        block = text[mark.end() : end]
        location = _last_location(block)
        message = ""
        for line in block.splitlines():
            match = _JEST_MESSAGE.match(line)
            if match:
                message = match.group("msg")
                break
        if not message:
            message = _first_error_line(block) or "failed"
        # `Test suite failed to run` has no suite name; the location still
        # points at the file that could not load.
        test_id = "" if test.lower() == "test suite failed to run" else test
        rows.append(
            _row(
                "jest",
                path=location[0] if location else "",
                line=location[1] if location else None,
                column=location[2] if location else None,
                test_id=test_id,
                message=message,
            )
        )
    return rows

def _vitest_blocks(text: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    file_marks = list(_VITEST_FILE.finditer(text))
    bullets = list(_VITEST_BULLET.finditer(text))
    current = ""
    file_index = -1
    for mark in bullets:
        # One forward pass: the previous nested scan was quadratic on a log
        # with many tests, which a red suite can produce.
        while (
            file_index + 1 < len(file_marks)
            and file_marks[file_index + 1].start() < mark.start()
        ):
            file_index += 1
            current = file_marks[file_index].group("path")
        following = text[mark.end() : mark.end() + 400]
        message = ""
        for line in following.splitlines()[:3]:
            stripped = line.strip()
            if stripped.startswith("→"):
                message = stripped[1:].strip()
                break
            if stripped.startswith(("AssertionError", "Error")):
                message = stripped
                break
        rows.append(
            _row(
                "vitest",
                path=current,
                test_id=mark.group("test").strip(),
                message=message or "failed",
            )
        )
    return rows

def _last_location(block: str) -> Optional[tuple[str, int, Optional[int]]]:
    found: Optional[tuple[str, int, Optional[int]]] = None
    for match in _LOCATION.finditer(block):
        path = match.group("path")
        if path.startswith("node:") or path.startswith("internal/"):
            continue
        found = (
            path,
            int(match.group("line")),
            _int_or_none(int(match.group("column")) if match.group("column") else None),
        )
    return found

_NODE_SPLIT = re.compile(r"^✖ failing tests:\s*$", re.M)

_NODE_TEST_AT = re.compile(r"^test at (?P<path>\S+):(?P<line>\d+):(?P<col>\d+)\s*$")

_NODE_FAILED = re.compile(r"^✖ (?P<test>.+?)(?:\s+\(\d+(?:\.\d+)?\s*ms\))?\s*$")

_NODE_MESSAGE = re.compile(
    r"^\s*(?P<msg>[A-Za-z_$][\w.$]*(?:Error|Exception)(?: \[[^\]]+\])?:.*)$"
)

def _node_test(text: str) -> list[dict[str, object]]:
    split = _NODE_SPLIT.search(text)
    if split is None:
        return []
    lines = text[split.end() :].splitlines()
    rows: list[dict[str, object]] = []
    index = 0
    while index < len(lines):
        location = _NODE_TEST_AT.match(lines[index].strip())
        if location is None:
            index += 1
            continue
        index += 1
        if index >= len(lines):
            break
        named = _NODE_FAILED.match(lines[index].strip())
        if named is None:
            continue
        index += 1
        message = ""
        for line in lines[index : index + 6]:
            match = _NODE_MESSAGE.match(line)
            if match:
                message = match.group("msg")
                break
        rows.append(
            _row(
                "node-test",
                path=_relative(location.group("path"), None),
                line=int(location.group("line")),
                column=int(location.group("col")),
                test_id=named.group("test").strip(),
                message=message or "failed",
            )
        )
    return rows

_GO_FAILED = re.compile(r"^--- FAIL: (?P<test>\S+) \([\d.]+s\)\s*$")  # TestParse/empty: a subtest

_GO_LOCATION = re.compile(
    r"^\s+(?P<path>[^\s:]+\.go):(?P<line>\d+):(?P<msg>.*)$"
)

_GO_PANIC = re.compile(r"^panic: (?P<msg>.*)$", re.M)

_GO_PANIC_LOCATION = re.compile(r"^(?P<path>[^\s:]+\.go):(?P<line>\d+)", re.M)

def _go_test(text: str) -> list[dict[str, object]]:
    lines = text.splitlines()
    rows: list[dict[str, object]] = []
    index = 0
    while index < len(lines):
        named = _GO_FAILED.match(lines[index].strip())
        if named is None:
            index += 1
            continue
        test = named.group("test")
        index += 1
        path = ""
        line: Optional[int] = None
        message = ""
        while index < len(lines) and not lines[index].strip().startswith("--- "):
            location = _GO_LOCATION.match(lines[index])
            if location is not None and not path:
                path = location.group("path")
                line = int(location.group("line"))
                message = location.group("msg").strip()
            elif message and lines[index].strip().startswith("panic:"):
                message = lines[index].strip()
            index += 1
        following = _GO_FAILED.match(lines[index].strip()) if index < len(lines) else None
        if not path and following is not None and following.group("test").startswith(test + "/"):
            continue  # the parent of failed subtests: each subtest has its own row
        rows.append(
            _row(
                "go-test",
                path=path,
                line=line,
                test_id=test,
                message=message or "failed",
            )
        )
    for match in _GO_PANIC.finditer(text):
        following = text[match.end() : match.end() + 200].lstrip("\n")
        location = _GO_PANIC_LOCATION.match(following)
        message = match.group("msg").strip() or "panic"
        # A panic inside a failed test is already that test's message; listing
        # it again would turn one failure into two and defeat repeat detection.
        if any(message in str(row.get("message") or "") for row in rows):
            continue
        rows.append(
            _row(
                "go-test",
                path=location.group("path") if location else "",
                line=location.group("line") if location else None,
                message=message,
            )
        )
    return rows

_CARGO_FAILED = re.compile(r"^test (?P<test>[\w:]+) \.\.\. FAILED\s*$", re.M)

_CARGO_BLOCK = re.compile(r"^---- (?P<test>[\w:]+) stdout ----\s*$", re.M)

_CARGO_PANIC = re.compile(
    r"panicked at (?P<path>[^:\n]+):(?P<line>\d+):(?P<column>\d+):\s*$", re.M
)

_CARGO_ERROR = re.compile(r"^error(?:\[(?P<code>E\d+)\])?: (?P<msg>.+)$", re.M)

_CARGO_ARROW = re.compile(
    r"^\s*-->\s+(?P<path>[^:\n]+):(?P<line>\d+):(?P<column>\d+)\s*$", re.M
)

def _cargo_test(text: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    blocks = list(_CARGO_BLOCK.finditer(text))
    for index, mark in enumerate(blocks):
        end = blocks[index + 1].start() if index + 1 < len(blocks) else len(text)
        block = text[mark.end() : end]
        panic = _CARGO_PANIC.search(block)
        message = ""
        if panic is not None:
            tail = block[panic.end() :].lstrip("\n")
            message = tail.splitlines()[0] if tail.splitlines() else ""
        rows.append(
            _row(
                "cargo-test",
                path=panic.group("path") if panic else "",
                line=panic.group("line") if panic else None,
                column=panic.group("column") if panic else None,
                test_id=mark.group("test"),
                message=message or "panicked",
            )
        )
    for match in _CARGO_FAILED.finditer(text):
        test = match.group("test")
        if any(row.get("test_id") == test for row in rows):
            continue
        rows.append(_row("cargo-test", test_id=test, message="failed"))
    # A compile error stops cargo before any test runs; the arrow line names
    # the exact source position, which is what the repair needs.
    for match in _CARGO_ERROR.finditer(text):
        following = text[match.end() : match.end() + 600]
        arrow = _CARGO_ARROW.search(following)
        if arrow is None:
            # `error: test failed, to rerun pass ...` is the run summary, not
            # a second compile failure. A real build error always carries an
            # arrow with its source position; without one there is nothing to
            # repair at a line, and counting it would double the failure.
            continue
        rows.append(
            _row(
                "cargo-build",
                path=arrow.group("path"),
                line=arrow.group("line"),
                column=arrow.group("column"),
                code=match.group("code") or "",
                message=match.group("msg").strip(),
            )
        )
    return rows

_RUFF = re.compile(
    r"^(?P<path>[^\s:]+\.py):(?P<line>\d+):(?P<column>\d+): (?P<code>[A-Z]+\d+) (?P<msg>.+)$",
    re.M,
)

_RUFF_FIXABLE = re.compile(r"^\[\*\]\s*")

_RUFF_FORMAT = re.compile(r"^Would reformat: (?P<path>.+)$", re.M)

_MYPY = re.compile(
    r"^(?P<path>[^\s:]+):(?P<line>\d+): error: (?P<msg>.+?)(?:\s+\[(?P<code>[\w-]+)\])?$",
    re.M,
)

_TSC = re.compile(
    r"^(?P<path>[^\s(]+)\((?P<line>\d+),(?P<column>\d+)\): error (?P<code>TS\d+): (?P<msg>.+)$",
    re.M,
)

def _lint(text: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for match in _RUFF.finditer(text):
        rows.append(
            _row(
                "ruff",
                path=match.group("path"),
                line=match.group("line"),
                column=match.group("column"),
                code=match.group("code"),
                message=_RUFF_FIXABLE.sub("", match.group("msg")),
            )
        )
    for match in _RUFF_FORMAT.finditer(text):
        rows.append(
            _row("ruff", path=match.group("path").strip(), code="format", message="would reformat")
        )
    for match in _MYPY.finditer(text):
        rows.append(
            _row(
                "mypy",
                path=match.group("path"),
                line=match.group("line"),
                code=match.group("code") or "",
                message=match.group("msg"),
            )
        )
    for match in _TSC.finditer(text):
        rows.append(
            _row(
                "tsc",
                path=match.group("path"),
                line=match.group("line"),
                column=match.group("column"),
                code=match.group("code"),
                message=match.group("msg"),
            )
        )
    return rows
