"""Pytest-only collection rules that keep the parallel suite honest.

This file is deliberately invisible to ``unittest``: the published test count and
CI's gate both run ``python -m unittest discover -s tests``, which never imports a
``conftest.py``. Nothing here changes what any test asserts.
"""

from __future__ import annotations

import os
from typing import Any

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
