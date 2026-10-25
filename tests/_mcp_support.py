"""Isolation and cleanup helpers shared by the MCP client and CLI tests.

Only loopback/test-local resources are used: every KaroX state directory is a
caller-supplied temporary directory and every MCP server is a local child
process or a loopback HTTP listener.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import psutil


TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SRC = ROOT / "src"
ECHO_SERVER = TESTS_DIR / "_mcp_echo_server.py"
HTTP_SERVER = TESTS_DIR / "_mcp_http_server.py"

"""Every spelling KaroX accepts for a path override, highest precedence first.

``paths.py`` prefers the ``KAROX_VNEXT_*`` names and falls back to the shorter
aliases, so a test that sets only an alias can be silently redirected by an
inherited ``KAROX_VNEXT_*`` -- to another run's directory, or to the developer's
real configuration.  Isolation therefore has to name every spelling.
"""
_CONFIG_OVERRIDES = ("KAROX_VNEXT_CONFIG_DIR", "KAROX_CONFIG_DIR")
_RUNTIME_OVERRIDES = ("KAROX_VNEXT_RUNTIME_DIR", "KAROX_RUNTIME_DIR")
_LEGACY_OVERRIDES = ("KAROX_LEGACY_CONFIG_DIR",)
_ISOLATION_OVERRIDES = (
    "KAROX_TEST_ISOLATION",
    "KAROX_TEST_FORBIDDEN_DIRS",
    "KAROX_TEST_SANDBOX_DIR",
)


def isolate_karox_environment(
    monkeypatch: Any,
    *,
    config_dir: Path | str,
    runtime_dir: Path | str,
) -> None:
    """Point every configuration spelling at the caller's temporary roots."""

    for name in (
        _CONFIG_OVERRIDES + _RUNTIME_OVERRIDES + _LEGACY_OVERRIDES + _ISOLATION_OVERRIDES
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KAROX_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(runtime_dir))


def cli_json(
    argv: Sequence[str],
) -> tuple[int, Any | None, str]:
    """Run the real CLI entry point and decode its JSON stdout.

    Returns ``(exit_code, payload, stderr)``.  A non-zero exit leaves the
    payload as ``None`` so a caller can assert on the recorded error instead of
    trying to parse empty output.
    """

    from karox import cli

    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(list(argv))
    text = stdout.getvalue()
    payload = json.loads(text) if text.strip() and code == 0 else None
    return code, payload, stderr.getvalue()


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def recorded_pids(paths: Iterable[Path]) -> set[int]:
    pids: set[int] = set()
    for path in paths:
        for line in read_lines(path):
            try:
                pids.add(int(line.strip()))
            except ValueError:
                continue
    return pids


def terminate_recorded_processes(
    paths: Iterable[Path], *, timeout: float = 5.0
) -> None:
    """Terminate any fixture server a failed transport left behind.

    ``mcp``'s stdio transport normally reaps its child on context exit, but a
    test that fails mid-call can unwind before that happens.  On Windows the
    orphan keeps the temporary directory locked, so teardown has to be
    deterministic rather than hopeful.
    """

    deadline = time.monotonic() + timeout
    for pid in recorded_pids(paths):
        if pid == os.getpid():
            continue
        try:
            process = psutil.Process(pid)
        except psutil.Error:
            continue
        try:
            process.terminate()
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except psutil.TimeoutExpired:
            with contextlib.suppress(psutil.Error):
                process.kill()
                process.wait(timeout=2.0)
        except psutil.Error:
            continue


def terminate_server_processes(script: Path, *, timeout: float = 5.0) -> None:
    """Terminate any orphan running one of our fixture server scripts.

    Matching on the absolute fixture path is safe: no unrelated host process
    runs this repository's throwaway test server.  This is the backstop for a
    transport that unwound before reaping its child, which is the case that
    leaves Windows temporary directories locked.
    """

    needle = os.path.normcase(str(script))
    orphans: list[Any] = []
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = process.info.get("cmdline") or []
        except psutil.Error:
            continue
        if not any(needle in os.path.normcase(str(part)) for part in cmdline):
            continue
        # Every xdist worker runs the same fixture script. Killing by path
        # alone killed another worker's live server and produced transport
        # errors in unrelated tests; only reap a child of this process (or an
        # orphan whose parent is gone, which is the lock-leak case).
        try:
            parent = process.ppid()
        except psutil.Error:
            continue
        if parent != os.getpid() and psutil.pid_exists(parent):
            continue
        orphans.append(process)
    deadline = time.monotonic() + timeout
    for process in orphans:
        try:
            process.terminate()
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except psutil.TimeoutExpired:
            with contextlib.suppress(psutil.Error):
                process.kill()
                process.wait(timeout=2.0)
        except psutil.Error:
            continue


@contextlib.contextmanager
def child_process_cleanup(
    paths: Iterable[Path] = (), scripts: Iterable[Path] = ()
) -> Iterator[None]:
    """Terminate recorded or orphaned fixture children on the way out."""

    recorded = list(paths)
    fixtures = list(scripts)
    try:
        yield
    finally:
        terminate_recorded_processes(recorded)
        for script in fixtures:
            terminate_server_processes(script)
        # Windows refuses to remove a directory while a just-killed child still
        # holds it as its working directory; the handle clears in milliseconds.
        time.sleep(0.05)