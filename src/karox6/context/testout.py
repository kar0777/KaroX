"""Condense a long test-runner log before the model reads it.

A pytest / unittest / jest / go test / cargo test run prints hundreds of progress and
"passed" lines, and the model pays for every one of them on every later request. What a fix
needs is small: the final counts, each failure with its assertion message and the file:line
of the failing test, collection errors, and a short count of warnings. This keeps exactly
that. The full raw output stays in the log file the shell tool already writes; the caller
names that file next to the condensed text. A format that is not recognised is left alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

#: Below this size a log is returned as it is: condensing it saves nothing.
CONDENSE_THRESHOLD_CHARS = 4_000
#: What the condensed text aims for. Failure blocks are shrunk to fit.
CONDENSED_TARGET_CHARS = 8_000
#: Many failures can need more than the target; past this, later failures are listed by
#: name only (the log file has the rest).
CONDENSED_HARD_CHARS = 16_000
#: Warning entries kept from a warnings summary; the rest is counted.
WARNINGS_KEPT = 3
#: Lines kept from one summary-like section (the pytest short summary, the header).
SECTION_LINES_KEPT = 80
LINE_CHARS_KEPT = 300
MIN_BLOCK_CHARS = 500
#: A condensed text must be at least this much shorter than the original to be used.
MIN_GAIN = 0.9

_MARKER = re.compile(r"^\[exit code:.*\]\s*$")
_CODE = re.compile(r"^\s*>\s")
_MSG = re.compile(
    r"^(E\s|\s*>\s|\s*(Expected|Received|expect\(|assert\w*|AssertionError|left:|right:|"
    r"thread '|panic:)|\s*[\w.]*(Error|Exception|Failure)\b)"
)
_LOC = re.compile(r"[\w./\\-]+\.\w+:\d+|File \".+\", line \d+")
_CAPTURED = re.compile(r"^-{3,} Captured .*-{3,}\s*$")


@dataclass(frozen=True, slots=True)
class Condensed:
    text: str
    original_chars: int


@dataclass(slots=True)
class _Seg:
    lines: list[str]
    fail: bool = False


@dataclass(slots=True)
class _Parsed:
    segs: list[_Seg] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)

    def keep(self, lines: list[str]) -> None:
        lines = _trim_blank(lines)
        if lines:
            self.segs.append(_Seg(lines))

    def fail(self, lines: list[str]) -> None:
        lines = _trim_blank(lines)
        if lines:
            self.segs.append(_Seg(lines, fail=True))

    def drop(self, what: str, count: int = 1) -> None:
        self.dropped[what] = self.dropped.get(what, 0) + count


def condense_test_output(
    text: str,
    *,
    threshold: int = CONDENSE_THRESHOLD_CHARS,
    target: int = CONDENSED_TARGET_CHARS,
) -> Condensed | None:
    """The condensed text of a recognised test run, or None (too short, unknown format, or
    nothing worth dropping)."""
    if len(text) <= threshold:
        return None
    parser = _detect(text)
    if parser is None:
        return None
    lines = text.replace("\r\n", "\n").split("\n")
    marker: list[str] = []
    while lines and (not lines[-1].strip() or _MARKER.match(lines[-1])):
        last = lines.pop()
        if last.strip():
            marker.insert(0, last)
    parsed = parser(lines)
    body = _render(parsed, target)
    if not body.strip():
        return None
    head = _note(parsed.dropped, len(text))
    out = "\n".join([*([head] if head else []), body, *marker])
    if len(out) >= len(text) * MIN_GAIN:
        return None
    return Condensed(out, len(text))


def condensed_notice(original_chars: int, kept_chars: int, path: Path | None) -> str:
    where = str(path) if path is not None else "(unavailable)"
    return (
        f"(Test output condensed: {original_chars} chars -> {kept_chars}. Passed, progress and "
        f"repeated lines dropped; every failure kept. Full output saved to: {where}. "
        "Use read with offset/limit, or grep this path to search within it.)"
    )


# -- detection ---------------------------------------------------------------------------

_PY_FINAL = re.compile(
    r"^(=+ )?(\d+ (failed|passed|skipped|deselected|xfailed|xpassed|errors?|warnings?|rerun)"
    r"|no tests ran)\b.* in [\d.]+s\b.*$",
    re.M,
)
_JEST = re.compile(r"^Tests:\s+.*\btotal\b", re.M)
_JEST_SUITES = re.compile(r"^Test Suites:", re.M)
_CARGO = re.compile(r"^test result: (ok|FAILED)\.", re.M)
_UNITTEST = re.compile(r"^Ran \d+ tests? in [\d.]+s\s*$", re.M)
_GO = re.compile(
    r"^((ok|FAIL)\s+\S+\s+([\d.]+s|\(cached\)|\[build failed\]|\[setup failed\])"
    r"|--- (FAIL|PASS): Test\w*)",
    re.M,
)


def _detect(text: str) -> Callable[[list[str]], _Parsed] | None:
    if re.search(r"^=+ test session starts =+\s*$", text, re.M) or _PY_FINAL.search(text):
        return _parse_pytest
    if _JEST.search(text) and _JEST_SUITES.search(text):
        return _parse_jest
    if _CARGO.search(text):
        return _parse_cargo
    if _UNITTEST.search(text):
        return _parse_unittest
    if _GO.search(text):
        return _parse_go
    return None


# -- pytest ------------------------------------------------------------------------------

_PY_SECTION = re.compile(r"^={3,} (.+?) ={3,}\s*$")
_PY_TITLE = re.compile(r"^_{2,} (.+?) _{2,}\s*$")
_PY_PROGRESS = re.compile(r"^(\S+\.py\s+)?[.FEsxXuUR]+(\s+\[\s*\d+%\])?\s*$")
_PY_PASSED = re.compile(
    r"^(\[gw\d+\]\s+(\[\s*\d+%\]\s+)?(PASSED|SKIPPED|XFAIL|XPASS)\b.*"
    r"|\S+::\S.*\s(PASSED|SKIPPED|XFAIL|XPASS)\b.*)$"
)


def _parse_pytest(lines: list[str]) -> _Parsed:
    out = _Parsed()
    section = "pre"
    buf: list[str] = []
    block: list[str] | None = None

    def flush() -> None:
        nonlocal block
        if block is not None:
            out.fail(block)
            block = None
        if section == "warn":
            _keep_warnings(buf, out)
        elif section in ("pre", "header", "short", "post"):
            out.keep(_cap_lines(buf))
        buf.clear()

    for line in lines:
        match = _PY_SECTION.match(line)
        if match:
            flush()
            title = match.group(1).strip()
            if title.startswith("test session starts"):
                section = "header"
            elif title in ("FAILURES", "ERRORS"):
                section = "fail"
            elif "warnings summary" in title:
                section = "warn"
            elif "short test summary info" in title:
                section = "short"
                buf.append(line)
            elif _PY_FINAL.match(line) or title.startswith("no tests ran"):
                section = "post"
                buf.append(line)
            else:
                section = "other"
                out.drop("lines of other sections")
            continue
        if section == "fail":
            title_match = _PY_TITLE.match(line)
            if title_match:
                if block is not None:
                    out.fail(block)
                block = [line]
            elif block is not None:
                block.append(line)
            continue
        if section == "other":
            out.drop("lines of other sections")
            continue
        if section in ("pre", "header"):
            if _PY_PROGRESS.match(line) and line.strip():
                out.drop("progress lines")
                continue
            if _PY_PASSED.match(line):
                out.drop("passed/skipped lines")
                continue
            if section == "header" and line.startswith(("plugins:", "cachedir:")):
                continue
        buf.append(line)
    flush()
    return out


def _keep_warnings(buf: list[str], out: _Parsed) -> None:
    entries: list[list[str]] = [[]]
    for line in buf:
        if line.startswith("-- Docs:"):
            continue
        if line.strip():
            entries[-1].append(line)
        elif entries[-1]:
            entries.append([])
    entries = [e for e in entries if e]
    for entry in entries[:WARNINGS_KEPT]:
        out.keep(["warnings:" if entry is entries[0] else "", *_cap_lines(entry, 6)])
    if len(entries) > WARNINGS_KEPT:
        out.drop("warnings", len(entries) - WARNINGS_KEPT)


# -- unittest ----------------------------------------------------------------------------

_UT_RULE = re.compile(r"^={40,}\s*$")
_UT_DASH = re.compile(r"^-{40,}\s*$")
_UT_TITLE = re.compile(r"^(FAIL|ERROR): ")
_UT_PROGRESS = re.compile(r"^[.FEsxu]+\s*$")
_UT_VERBOSE = re.compile(
    r"^\S.* \.\.\. (ok|skipped\b.*|FAIL|ERROR|expected failure|unexpected success)$"
)


def _parse_unittest(lines: list[str]) -> _Parsed:
    out = _Parsed()
    pre: list[str] = []
    block: list[str] | None = None
    tail: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if _UT_RULE.match(line) and _UT_TITLE.match(nxt):
            if block is not None:
                out.fail(block)
            block = []
            i += 1
            continue
        if _UT_DASH.match(line) and re.match(r"^Ran \d+ tests? in ", nxt):
            if block is not None:
                out.fail(block)
                block = None
            tail = lines[i + 1 :]
            break
        if block is not None:
            if not (_UT_DASH.match(line) and len(block) == 1):
                block.append(line)
        elif _UT_PROGRESS.match(line) and line.strip():
            out.drop("progress lines")
        elif _UT_VERBOSE.match(line):
            out.drop("per-test lines")
        else:
            pre.append(line)
        i += 1
    if block is not None:
        out.fail(block)
    head = _trim_blank(pre)
    if head:
        out.segs.insert(0, _Seg(head))
    out.keep(tail)
    return out


# -- jest --------------------------------------------------------------------------------

_JEST_FILE = re.compile(r"^(PASS|FAIL)\s+\S")
_JEST_OK = re.compile(r"^\s+(✓|✔|√|○)\s")
_JEST_START = re.compile(r"^\s*● ")
_JEST_CONSOLE = re.compile(r"^\s{2}console\.\w+\s*$")


def _parse_jest(lines: list[str]) -> _Parsed:
    out = _Parsed()
    block: list[str] | None = None
    noise = False
    failed_files: set[str] = set()

    def close() -> None:
        nonlocal block
        if block is not None:
            out.fail(block)
            block = None

    for line in lines:
        indented = line.startswith(" ") or not line.strip()
        if _JEST_START.match(line):
            close()
            noise = False
            if line.strip().startswith("● Console"):
                noise = True
                out.drop("console output blocks")
            else:
                block = [line]
            continue
        if _JEST_CONSOLE.match(line):
            close()
            noise = True
            out.drop("console output blocks")
            continue
        if indented and (block is not None or noise):
            if block is not None and not noise:
                block.append(line)
            continue
        close()
        noise = False
        if _JEST_FILE.match(line) and line.startswith("PASS"):
            out.drop("passing test files")
        elif _JEST_OK.match(line):
            out.drop("passed test lines")
        elif line.strip() and not line.startswith("Summary of all failing tests"):
            if _JEST_FILE.match(line):
                if line in failed_files:
                    continue
                failed_files.add(line)
            out.keep([line])
    close()
    _merge_adjacent_keeps(out)
    return out


# -- go test -----------------------------------------------------------------------------

_GO_CONTROL = re.compile(r"^=== (RUN|PAUSE|CONT|NAME)\b")
_GO_PASS = re.compile(r"^\s*--- (PASS|SKIP|BENCH):")
_GO_FAIL = re.compile(r"^--- FAIL:")
_GO_OK_PKG = re.compile(r"^(ok|\?)\s+\S+\s")
_GO_FAIL_PKG = re.compile(r"^(FAIL|exit status)\b")
_GO_OKS_KEPT = 5


def _go_continues(block: list[str], line: str) -> bool:
    if block[0].startswith("--- FAIL:"):
        return line.startswith((" ", "\t"))
    return not (
        _GO_FAIL_PKG.match(line)
        or _GO_OK_PKG.match(line)
        or _GO_CONTROL.match(line)
        or line.startswith(("--- ", "# "))
    )


def _parse_go(lines: list[str]) -> _Parsed:
    out = _Parsed()
    pending: list[str] = []
    block: list[str] | None = None
    ok_lines: list[str] = []
    summary: list[str] = []

    def close() -> None:
        nonlocal block
        if block is not None:
            out.fail(block)
            block = None

    for line in lines:
        if block is not None:
            if _GO_PASS.match(line) and line.startswith((" ", "\t")):
                out.drop("passed subtests")
                continue
            if _go_continues(block, line):
                block.append(line)
                continue
            close()
        if _GO_CONTROL.match(line):
            if any(_LOC.search(p) for p in pending):
                out.fail(pending)
            elif pending:
                out.drop("output lines of passing tests", len(pending))
            pending = []
            out.drop("=== lines")
            continue
        if _GO_PASS.match(line):
            out.drop("output lines of passing tests", len(pending))
            out.drop("passed tests")
            pending = []
            continue
        if _GO_FAIL.match(line):
            block = [line, *pending]
            pending = []
            continue
        if line.startswith(("panic:", "# ", "fatal error:")):
            pending = []
            block = [line]
            continue
        if _GO_OK_PKG.match(line):
            ok_lines.append(line)
            continue
        if _GO_FAIL_PKG.match(line) or line.startswith("coverage:"):
            summary.append(line)
            continue
        if line.strip() in ("PASS", "") or line.startswith("go: downloading"):
            continue
        pending.append(line)
    close()
    if pending and any(_LOC.search(p) for p in pending):
        out.fail(pending)
    elif pending:
        out.keep(_cap_lines(pending))
    for line in ok_lines[:_GO_OKS_KEPT]:
        summary.append(line)
    if len(ok_lines) > _GO_OKS_KEPT:
        out.drop("passing packages", len(ok_lines) - _GO_OKS_KEPT)
    out.keep(summary)
    return out


# -- cargo test --------------------------------------------------------------------------

_CARGO_NOISE = re.compile(
    r"^\s*(Compiling|Downloading|Downloaded|Fresh|Updating|Locking|Blocking|Finished|Running|"
    r"Doc-tests|Adding|Checking|Packaging)\b|^running \d+ tests?\s*$|^note: run with `RUST_"
)
_CARGO_OK = re.compile(r"^test .* \.\.\. (ok|ignored.*)$")
_CARGO_FAILED = re.compile(r"^test .* \.\.\. FAILED$")
_CARGO_STDOUT = re.compile(r"^---- (.+) (stdout|stderr) ----\s*$")
_CARGO_DIAG = re.compile(r"^(error|warning)(\[\w+\])?: ")


def _parse_cargo(lines: list[str]) -> _Parsed:
    out = _Parsed()
    block: list[str] | None = None
    failed_lines: list[str] = []
    names: list[str] = []
    has_list = False
    mode = ""
    other: list[str] = []

    def close() -> None:
        nonlocal block, mode
        if block is not None:
            if mode == "warning":
                out.drop("compiler warnings")
            else:
                out.fail(block)
            block = None
        mode = ""

    for line in lines:
        if mode == "flist":
            if line.startswith(" ") and line.strip():
                names.append(line)
                continue
            if not line.strip():
                continue
            if names:
                has_list = True
                out.keep(["failures:", *names])
            names = []
            mode = ""
        stdout = _CARGO_STDOUT.match(line)
        if stdout:
            close()
            block, mode = [line], "stdout"
            continue
        if line.startswith("failures:"):
            close()
            mode = "flist"
            continue
        if line.startswith("test result:"):
            close()
            out.keep([line])
            continue
        if mode == "stdout":
            assert block is not None
            if not line.startswith("note: run with"):
                block.append(line)
            continue
        diag = _CARGO_DIAG.match(line)
        if diag and not line.startswith(("error: test failed", "error: could not", "warning: `")):
            close()
            block = [line]
            mode = "warning" if line.startswith("warning") else "error"
            continue
        if mode in ("warning", "error"):
            assert block is not None
            if line.strip():
                block.append(line)
                continue
            close()
            continue
        if _CARGO_NOISE.match(line):
            out.drop("build/progress lines")
        elif _CARGO_OK.match(line):
            out.drop("passed tests")
        elif _CARGO_FAILED.match(line):
            failed_lines.append(line)
        elif line.startswith("warning: `"):
            out.drop("compiler warnings")
        else:
            other.append(line)
    close()
    if mode == "flist" and names:
        has_list = True
        out.keep(["failures:", *names])
    if failed_lines and not has_list:
        out.keep(failed_lines)
    out.keep(_cap_lines(other))
    return out


# -- rendering ---------------------------------------------------------------------------


def _trim_blank(lines: list[str]) -> list[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return [line.rstrip() for line in lines[start:end]]


def _cap_lines(lines: list[str], limit: int = SECTION_LINES_KEPT) -> list[str]:
    lines = [_clip(line) for line in lines]
    if len([line for line in lines if line.strip()]) <= limit:
        return lines
    kept = lines[: limit * 2 // 3]
    rest = lines[-(limit // 3) :]
    return [*kept, f"[... {len(lines) - len(kept) - len(rest)} lines omitted]", *rest]


def _clip(line: str, limit: int = LINE_CHARS_KEPT) -> str:
    return line if len(line) <= limit else line[:limit] + f"... [{len(line) - limit} chars cut]"


def _merge_adjacent_keeps(parsed: _Parsed) -> None:
    merged: list[_Seg] = []
    for seg in parsed.segs:
        if merged and not seg.fail and not merged[-1].fail:
            merged[-1].lines = [*merged[-1].lines, *seg.lines]
        else:
            merged.append(seg)
    parsed.segs = merged


def _size(lines: list[str]) -> int:
    return sum(len(line) + 1 for line in lines)


def _squeeze_captured(lines: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(lines):
        out.append(lines[i])
        if not _CAPTURED.match(lines[i]):
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not _CAPTURED.match(lines[j]):
            j += 1
        body = lines[i + 1 : j]
        if len(body) > 12:
            body = [*body[:4], f"[... {len(body) - 10} captured lines omitted]", *body[-6:]]
        out.extend(body)
        i = j
    return out


# (context lines around a message line, message lines from the start, from the end,
#  trailing lines of the block, characters kept per line): each level keeps less than the
#  one before. The last level is the floor: title, first message line, last file:line.
_LEVELS = (
    (2, 12, 4, 6, 300),
    (1, 6, 2, 4, 240),
    (0, 3, 1, 2, 200),
    (0, 1, 0, 0, 200),
)


def _select(lines: list[str], levels: tuple[int, int, int, int, int]) -> list[str]:
    context, head, tail, tail_lines, width = levels
    picked: set[int] = {0}
    msgs = [i for i, line in enumerate(lines) if i and _MSG.match(line)]
    codes = [i for i in msgs if _CODE.match(lines[i])]
    asserts = [i for i in msgs if not _CODE.match(lines[i])]
    chosen = asserts[:head] + (asserts[-tail:] if tail else []) + codes[-2:]
    for i in chosen:
        picked.update(range(max(i - context, 1), min(i + context, len(lines) - 1) + 1))
    locs = [i for i, line in enumerate(lines) if i and _LOC.search(line)]
    if locs:
        picked.add(locs[0])
        picked.update(locs[-(tail_lines // 2 + 1) :])
    if tail_lines:
        picked.update(range(max(len(lines) - tail_lines, 1), len(lines)))
    out: list[str] = []
    previous = -1
    for i in sorted(picked):
        if previous >= 0 and i > previous + 1:
            out.append(f"    [... {i - previous - 1} lines omitted]")
        if lines[i].strip() or (out and out[-1].strip()):
            out.append(_clip(lines[i], width))
        previous = i
    if previous < len(lines) - 1:
        out.append(f"    [... {len(lines) - 1 - previous} lines omitted]")
    return out


def _shrink_block(lines: list[str], cap: int) -> list[str]:
    lines = _squeeze_captured(lines)
    if _size(lines) <= cap:
        return lines
    picked = lines
    for level in _LEVELS:
        picked = _select(lines, level)
        if _size(picked) <= cap:
            break
    return picked


def _allocate(sizes: list[int], budget: int) -> list[int]:
    """A cap per block: small blocks keep their size, big ones share what is left."""
    caps = [0] * len(sizes)
    remaining = budget
    order = sorted(range(len(sizes)), key=lambda k: sizes[k])
    for done, k in enumerate(order):
        share = max(remaining // (len(order) - done), MIN_BLOCK_CHARS)
        caps[k] = min(sizes[k], share)
        remaining -= caps[k]
    return caps


def _one_line(lines: list[str]) -> list[str]:
    """A failure as one line: its title and the file:line where it failed."""
    loc = next((line.strip() for line in reversed(lines[1:]) if _LOC.search(line)), "")
    return [_clip(f"{lines[0].strip()} | {loc}" if loc else lines[0], 200)]


def _signature(lines: list[str]) -> tuple[str, str]:
    msgs = [line.strip() for line in lines[1:] if _MSG.match(line) and not _CODE.match(line)]
    loc = next((line.strip() for line in reversed(lines[1:]) if _LOC.search(line)), "")
    return (msgs[0] if msgs else ""), loc


def _render(parsed: _Parsed, target: int) -> str:
    seen: set[tuple[str, ...]] = set()
    fails: list[_Seg] = []
    for seg in parsed.segs:
        if not seg.fail:
            continue
        key = tuple(line.strip() for line in seg.lines)
        if key in seen:
            seg.lines = []
            parsed.drop("repeated failure blocks")
        else:
            seen.add(key)
            fails.append(seg)
    for seg in parsed.segs:
        if not seg.fail:
            seg.lines = _cap_lines(seg.lines)
    keep_size = sum(_size(seg.lines) for seg in parsed.segs if not seg.fail)
    budget = max(target - keep_size - 300, MIN_BLOCK_CHARS)
    caps = _allocate([_size(seg.lines) for seg in fails], budget)
    for seg, cap in zip(fails, caps, strict=True):
        seg.lines = _shrink_block(seg.lines, cap)
    total = keep_size + sum(_size(seg.lines) + 1 for seg in fails)
    if total > target:
        first: dict[str, str] = {}
        for seg in fails:
            msg, loc = _signature(seg.lines)
            if msg and msg in first and loc:
                seg.lines = [seg.lines[0], f"    same error as {first[msg]!r}", f"    {loc}"]
                parsed.drop("failure blocks with an error already shown")
            elif msg:
                first[msg] = seg.lines[0].strip()[:80]
        total = keep_size + sum(_size(seg.lines) + 1 for seg in fails)
    for label, compact in (
        ("failures shortened to title and file:line", _one_line),
        ("failures shortened to title only", lambda lines: [_clip(lines[0].split(" | ")[0], 120)]),
    ):
        for seg in reversed(fails):
            if total <= CONDENSED_HARD_CHARS:
                break
            shorter = compact(seg.lines)
            if _size(shorter) < _size(seg.lines):
                total -= _size(seg.lines) - _size(shorter)
                seg.lines = shorter
                parsed.drop(label)
    if total > CONDENSED_HARD_CHARS:
        for seg in parsed.segs:
            if not seg.fail and len(seg.lines) > SECTION_LINES_KEPT // 3:
                seg.lines = _cap_lines(seg.lines, SECTION_LINES_KEPT // 3)
    out: list[str] = []
    for seg in parsed.segs:
        if not seg.lines:
            continue
        if out and out[-1].strip():
            out.append("")
        out.extend(seg.lines)
    return "\n".join(out)


def _note(dropped: dict[str, int], original: int) -> str:
    parts = [f"{count} {what}" for what, count in dropped.items() if count]
    if not parts:
        return ""
    return f"[test output condensed from {original} chars; dropped " + ", ".join(parts) + "]"
