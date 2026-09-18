"""Production-data isolation for serial and parallel pytest runs.

Use ``pytest -n 6 --dist=loadfile`` to distribute whole files. That preserves
class fixtures and the benchmark's alphabetically last aggregation assertion
in one worker. No tests are skipped or assertions changed for parallel runs.
An unsafe per-test distribution will fail the benchmark's own missing-record
assertion rather than silently reporting incomplete coverage as success.
"""
from __future__ import annotations

import _path_setup  # noqa: F401 - protects real user configuration during tests
