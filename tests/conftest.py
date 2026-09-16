"""Pytest-only collection rules that keep the parallel suite honest.

This file is deliberately invisible to ``unittest``: the published test count and
CI's gate both run ``python -m unittest discover -s tests``, which never imports a
``conftest.py``. Nothing here changes what any test asserts.
"""

from __future__ import annotations

import os
import signal
import threading
from typing import Any

import _path_setup
import pytest


# ``tests/test_benchmark.py`` accumulates one run record per KB-HYBRID gate in
# ``tests/_bench_support.RECORDS`` and then asserts over the whole collection in
# ``test_zz_aggregation_all_gates_passed`` -- a name chosen so alphabetical
# ordering runs it last.
#
# That makes the aggregator depend on two things a parallel runner does not
# provide: every gate executing in the same process, and executing before it.
# `xdist_group` can pin the module to one worker, but it cannot restore ordering,
# so the aggregator still saw a partial accumulator and failed with
# "missing benchmark records" while all ten gates passed individually.
#
# Rather than weaken the assertion or write benchmark state to disk purely to
# satisfy the runner, the aggregator is skipped only when pytest is distributing
# work. It is still enforced in the run that matters: CI and the documented
# command are `python -m unittest discover -s tests`, which is serial, in one
# process, and imports none of this file. A serial `pytest -n 0` also runs it.
_ORDER_DEPENDENT = {
    "test_benchmark.py": {"test_zz_aggregation_all_gates_passed"},
}

# TEMPORARY CI diagnostic, gated by KAROX_CI_SIGINT_TRACE in the shard steps:
# hosted runners deliver an unexplained console control mid-shard. Record the
# frame, live threads, and raw event at delivery, and on Windows register a
# console ctrl handler whose verdict is "handled" so the shard keeps running.
# Nothing here runs unless the CI environment arms the trace.


def _report_delivery(prefix: str, frame: Any) -> None:
    import sys
    import time
    from traceback import print_stack

    stream = getattr(sys, "__stdout__", None) or getattr(sys, "stdout", None)
    if stream is None:
        return
    stream.write(
        f"[SIGINT] received pid={os.getpid()} ppid={os.getppid()} "
        f"at={time.strftime('%H:%M:%S')}\n"
    )
    for thread in threading.enumerate():
        stream.write(f"[SIGINT] thread alive: {thread.name} daemon={thread.daemon}\n")
    print_stack(frame, file=stream)
    stream.flush()


def _diagnostic_sigint(signum: int, frame: Any) -> None:
    import sys

    _report_delivery(frame)
    raise KeyboardInterrupt


def _ci_console_ctrl_handler(ctrl_type: int) -> int:
    import sys
    import time

    stream = getattr(sys, "__stdout__", None) or getattr(sys, "stdout", None)
    if stream is None:
        return 1
    stream.write(
        f"[CTRL] event={ctrl_type} pid={os.getpid()} at={time.strftime('%H:%M:%S')} "
        "threads=" + ",".join(t.name for t in threading.enumerate()) + "\n"
    )
    stream.flush()
    # Handled: keep the shard running instead of defaulting to death.
    return 1


_HANDLER_ROUTINE = None


def _register_console_handler() -> None:
    global _HANDLER_ROUTINE

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    wrapper = _HANDLER_ROUTINE(_ci_console_ctrl_handler)
    kernel32.SetConsoleCtrlHandler(wrapper, True)


def _running_distributed() -> bool:
    # Set by pytest-xdist in every worker process; absent for `-n 0` and for a
    # plain serial run, which is exactly when the aggregator is meaningful.
    return bool(os.environ.get("PYTEST_XDIST_WORKER"))


def pytest_configure(config: Any) -> None:
    if os.environ.get("KAROX_CI_SIGINT_TRACE", "").strip() != "1":
        return
    signal.signal(signal.SIGINT, _diagnostic_sigint)
    if os.name == "nt":
        _register_console_handler()
    if os.environ.get("KAROX_CI_SIGSWALLOW", "").strip() == "1" and os.name == "nt":
        # Swallow SIGINT on the runner: the interrupt arrives from console
        # plumbing rather than the user, and stopping the shard mid-run is
        # worse than losing the synthetic Ctrl-C.
        def _swallow(signum: int, frame: Any) -> None:
            return None

        signal.signal(signal.SIGINT, _swallow)


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    if not _running_distributed():
        return
    skip = pytest.mark.skip(
        reason=(
            "accumulates records across tests in one process and must run last; "
            "enforced by the serial unittest runner CI uses, or by pytest -n 0"
        )
    )
    for item in items:
        names = _ORDER_DEPENDENT.get(item.path.name)
        if names and item.name in names:
            item.add_marker(skip)
