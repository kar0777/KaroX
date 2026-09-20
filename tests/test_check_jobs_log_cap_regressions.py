"""Batching must preserve per-line eviction and use the real display redactor."""

from __future__ import annotations

import io
import threading
from pathlib import Path
from unittest import mock

import pytest

from _support import SRC  # noqa: F401
from karox import check_jobs, security


class _ShortReads(io.StringIO):
    def __init__(self, text: str, limit: int) -> None:
        super().__init__(text)
        self.limit = limit

    def read(self, size: int = -1) -> str:
        return super().read(min(size, self.limit))


def _per_line_capture(stream: io.StringIO, output: Path) -> int:
    """Pre-batching reader; the unchanged capped writer is the eviction oracle."""
    pending = ""
    evictions = 0
    mode = "r+b" if output.exists() else "w+b"
    with output.open(mode) as handle:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            pending += chunk
            lines = pending.splitlines(keepends=True)
            if lines and not lines[-1].endswith(("\n", "\r")):
                pending = lines.pop()
            else:
                pending = ""
            for line in lines:
                evictions += check_jobs._write_capped_log(
                    handle, str(security.redact(line)).encode("utf-8", errors="replace")
                )
            if len(pending) > 8192:
                flush, pending = pending[:-512], pending[-512:]
                evictions += check_jobs._write_capped_log(
                    handle, str(security.redact(flush)).encode("utf-8", errors="replace")
                )
        if pending:
            evictions += check_jobs._write_capped_log(
                handle, str(security.redact(pending)).encode("utf-8", errors="replace")
            )
    return evictions


def _fixture(long_lines: bool) -> tuple[str, bytes, tuple[str, ...]]:
    # Synthetic pattern fixtures, assembled so repository redaction cannot
    # silently turn the test input into an already-redacted marker.
    canaries = (
        "ghp_" + "A" * 24,
        "github_pat_" + "B" * 24,
        "sk-" + "C" * 24,
        "Bearer " + "D" * 24,
    )
    credentials = " ".join(canaries)
    if long_lines:
        # Force pending[:-512] flushes, with credentials safely at line starts.
        text = "".join(
            f"row {index} {credentials} " + "." * (20000 + index) + "\r\n"
            for index in range(6)
        )
    else:
        text = "".join(
            f"row {index:04} 雪 café \udcff {credentials}\r\n" for index in range(400)
        )
    text += f"1 passed {credentials}"  # unterminated final output
    expected = text
    for canary in canaries:
        assert security.redact(canary) == "[REDACTED]"
        expected = expected.replace(canary, "[REDACTED]")
    return text, expected.encode("utf-8", errors="replace"), canaries


@pytest.mark.parametrize("limit", [17, 257, 4096])
@pytest.mark.parametrize("long_lines", [False, True], ids=["short-lines", "forced-long-lines"])
@pytest.mark.parametrize("cap,trim", [(128, 64), (1024, 512), (16384, 8192)])
@pytest.mark.parametrize("existing", [False, True])
def test_multiple_evictions_match_per_line_bytes(
    tmp_path: Path, limit: int, long_lines: bool, cap: int, trim: int, existing: bool
) -> None:
    assert check_jobs.redact is security.redact
    text, _, canaries = _fixture(long_lines)
    actual, reference = tmp_path / "actual.log", tmp_path / "reference.log"
    if existing:
        # Existing files are opened r+b at offset zero, not at EOF. Also cover
        # already-oversized files, which are only trimmed on the next write.
        initial = b"old log\n" * (cap // 8 + 3)
        actual.write_bytes(initial)
        reference.write_bytes(initial)
    truncated = [False]
    stop = threading.Event()
    stop.set()  # EOF, not cancellation, must deliver the unterminated summary.
    with (
        mock.patch.object(check_jobs, "_MAX_LOG_BYTES", cap),
        mock.patch.object(check_jobs, "_LOG_TRIM_TO_BYTES", trim),
    ):
        retained_at_eviction: list[bytes] = []
        write = check_jobs._write_capped_log

        def record_eviction(handle, data):
            result = write(handle, data)
            if result:
                # Reads from a separate handle cannot move the writer's cursor.
                retained_at_eviction.append(Path(handle.name).read_bytes())
            return result

        with mock.patch.object(check_jobs, "_write_capped_log", side_effect=record_eviction):
            evictions = _per_line_capture(_ShortReads(text, limit), reference)
            expected_evictions = retained_at_eviction[:]
            retained_at_eviction.clear()
            check_jobs._append_redacted(_ShortReads(text, limit), actual, stop, truncated)
    # Every crossing must leave exactly the same retained file, not just the
    # final suffix after later evictions have hidden an earlier difference.
    assert len(retained_at_eviction) == evictions
    assert retained_at_eviction == expected_evictions
    assert evictions >= 2, "fixture must cross the cap repeatedly"
    assert truncated == [True]
    assert actual.read_bytes() == reference.read_bytes()
    assert len(actual.read_bytes()) <= cap
    assert actual.read_bytes().endswith(b"1 passed " + b" ".join([b"[REDACTED]"] * 4))
    for canary in canaries:
        assert canary.encode() not in actual.read_bytes()


@pytest.mark.parametrize("limit", [1, 17, 4096])
@pytest.mark.parametrize("long_lines", [False, True], ids=["short-lines", "forced-long-lines"])
def test_uncapped_capture_has_exact_real_redacted_bytes(
    tmp_path: Path, limit: int, long_lines: bool
) -> None:
    assert check_jobs.redact is security.redact
    text, expected, canaries = _fixture(long_lines)
    actual, reference = tmp_path / "actual.log", tmp_path / "reference.log"
    truncated = [False]
    with (
        mock.patch.object(check_jobs, "_MAX_LOG_BYTES", len(expected) + 1),
        mock.patch.object(check_jobs, "_LOG_TRIM_TO_BYTES", len(expected) // 2),
    ):
        assert _per_line_capture(_ShortReads(text, limit), reference) == 0
        check_jobs._append_redacted(_ShortReads(text, limit), actual, threading.Event(), truncated)
    assert actual.read_bytes() == reference.read_bytes() == expected
    assert truncated == [False]
    for canary in canaries:
        assert canary.encode() not in actual.read_bytes()


@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.parametrize("already_truncated", [False, True])
def test_exact_cap_and_one_byte_crossing(
    tmp_path: Path, extra: int, already_truncated: bool
) -> None:
    path = tmp_path / "actual.log"
    initial = b"old-prefix-0123456789\n"
    path.write_bytes(initial)
    text = "one\ntwo\nthree\n"
    raw = text.encode()
    cap = len(initial) + len(raw) - extra
    trim = 16
    truncated = [already_truncated]
    with (
        mock.patch.object(check_jobs, "_MAX_LOG_BYTES", cap),
        mock.patch.object(check_jobs, "_LOG_TRIM_TO_BYTES", trim),
        mock.patch.object(check_jobs, "_write_capped_log", wraps=check_jobs._write_capped_log) as writes,
    ):
        check_jobs._append_redacted(io.StringIO(text), path, threading.Event(), truncated)
    if extra:
        # First two lines still fit. Only the third triggers the original trim;
        # the retained budget must use its size, not the aggregate chunk size.
        expected = (initial + b"one\ntwo\n")[-(trim - len(b"three\n")):] + b"three\n"
        assert writes.call_args_list == [
            mock.call(mock.ANY, b"one\n"),
            mock.call(mock.ANY, b"two\n"),
            mock.call(mock.ANY, b"three\n"),
        ]
    else:
        expected = initial + raw
        assert writes.call_args_list == [mock.call(mock.ANY, raw)]
    assert path.read_bytes() == expected
    assert truncated == [already_truncated or bool(extra)]


def test_real_redaction_still_batches_safely_below_cap(tmp_path: Path) -> None:
    assert check_jobs.redact is security.redact
    canary = "sk-" + "C" * 24
    text = f"value {canary}\n" * 100
    expected = b"value [REDACTED]\n" * 100
    path = tmp_path / "actual.log"
    truncated = [False]
    with (
        mock.patch.object(check_jobs, "_MAX_LOG_BYTES", len(expected)),
        mock.patch.object(check_jobs, "_LOG_TRIM_TO_BYTES", len(expected) // 2),
        mock.patch.object(check_jobs, "_write_capped_log", wraps=check_jobs._write_capped_log) as writes,
    ):
        check_jobs._append_redacted(io.StringIO(text), path, threading.Event(), truncated)
    assert path.read_bytes() == expected
    assert truncated == [False]
    assert writes.call_count == 1
