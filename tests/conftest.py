"""Pytest-only collection rules that keep the parallel suite honest.

This file is deliberately invisible to ``unittest``: the published test count and
CI's gate both run ``python -m unittest discover -s tests``, which never imports a
``conftest.py``. Nothing here changes what any test asserts.
"""

from __future__ import annotations

import os
import signal
import threading
import traceback
from typing import Any

import _path_setup
import pytest


# TEMPORARY CI diagnostic: the windows shard runs receive a KeyboardInterrupt
# mid-suite. Print the caller frame and live threads at delivery, then keep
# pytest's default handling.
def _diagnostic_sigint(signum: int, frame: Any) -> None:
    import sys
    import time

    print(
        f"\n[SIGINT] received={signum} pid={os.getpid()} "
        f"ppid={os.getppid()} at={time.strftime('%H:%M:%S')}\n",
        file=sys.__stderr__,
        flush=True,
    )
    for thread in threading.enumerate():
        print(f"[SIGINT] thread alive: {thread.name} daemon={thread.daemon}", file=sys.__stderr__, flush=True)
    traceback.print_stack(frame, file=sys.__stderr__)
    sys.__stderr__.flush()
    import signal as _signal  # noqa: F401  (default_int for symporarity)
    raise KeyboardInterrupt


def pytest_configure(config: Any) -> None:
    if "KAROX_CI_SIGINT_TRACE" not in os.environ:
        return
    signal.signal(signal.SIGINT, _diagnostic_sigint)


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


def _running_distributed() -> bool:
    # Set by pytest-xdist in every worker process; absent for `-n 0` and for a
    # plain serial run, which is exactly when the aggregator is meaningful.
    return bool(os.environ.get("PYTEST_XDIST_WORKER"))


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
