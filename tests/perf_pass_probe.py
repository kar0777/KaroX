"""Part 2 performance pass probe.

Run explicitly (never collected by the default suite):

    python -m pytest tests/perf_pass_probe.py

Every number here is measured in this process or in a real child
interpreter on this machine. Anything that needs live provider access,
project session state, a browser, or an interactive terminal is recorded
as UNAVAILABLE with the reason instead of an invented baseline.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

from karox.cache_scheduler import CacheAwareScheduler
from karox.context_compiler import ContextCompiler, ContextItem
from karox.cost_intelligence import CacheKey
from karox.evidence_packets import Fidelity
from karox.evidence_packets import tests_packet as _tests_packet
from karox.tool_universe import select_families

_REPORT = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "agent_throughput"
    / "latest_part2_perf_pass.json"
)


def _child_seconds(code: str, runs: int = 3) -> list[float]:
    durations: list[float] = []
    for _ in range(runs):
        started = time.perf_counter()
        completed = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        elapsed = time.perf_counter() - started
        assert completed.returncode == 0, completed.stderr[-500:]
        durations.append(round(elapsed, 4))
    return durations


def _context_items(count: int) -> list[ContextItem]:
    items: list[ContextItem] = []
    body = "x" * 500
    for index in range(count):
        if index % 10 == 7:
            # Same identity repeated: the older result is provably stale.
            items.append(
                ContextItem(
                    index=index,
                    role="tool",
                    content=f"{body}-{index % 20}",
                    tool_call_id=f"call-{index}",
                    tool_name="karox.repo.read_file",
                    tool_arguments='{"path": "src/karox/cli.py"}',
                )
            )
        elif index % 10 == 8:
            # Byte-identical duplicate tool result: reuse marker path.
            items.append(
                ContextItem(
                    index=index,
                    role="tool",
                    content=body,
                    tool_call_id=f"call-{index}",
                    tool_name="karox.git.status",
                    tool_arguments="{}",
                )
            )
        else:
            items.append(
                ContextItem(
                    index=index,
                    role="assistant" if index % 2 else "user",
                    content=f"{body}-{index}",
                )
            )
    return items


def test_perf_pass_measurements_write_report() -> None:
    report: dict[str, object] = {
        "schema": "part2-perf-pass-v1",
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }

    interpreter = _child_seconds("pass")
    cold_import = _child_seconds("import karox.cli")
    report["interpreter_boot_seconds"] = {
        "runs": interpreter,
        "median": statistics.median(interpreter),
        "label": "MEASURED",
    }
    report["cold_import_karox_cli_seconds"] = {
        "runs": cold_import,
        "median": statistics.median(cold_import),
        "label": "MEASURED",
        "note": "child interpreter, includes interpreter boot",
    }

    compiler = ContextCompiler()
    items = _context_items(400)
    durations_ms: list[float] = []
    compiled = compiler.compile(items)
    for _ in range(5):
        started = time.perf_counter()
        compiled = compiler.compile(items)
        durations_ms.append(round((time.perf_counter() - started) * 1000, 3))
    report["context_compiler_compile_400_items_ms"] = {
        "runs": durations_ms,
        "median": statistics.median(durations_ms),
        "chars_in": compiled.stats.chars_in,
        "label": "MEASURED",
    }

    scheduler = CacheAwareScheduler()
    stable = CacheKey(key="k-stable", prefix_hash="a" * 16, token_estimate=4000)
    changed = CacheKey(key="k-changed", prefix_hash="b" * 16, token_estimate=4000)
    started = time.perf_counter()
    previous = None
    for index in range(2000):
        current = stable if index % 3 else changed
        scheduler.decide(
            prefix_key=current,
            previous_key=previous,
            dynamic_chars=1200,
        )
        previous = current
    decide_us = (time.perf_counter() - started) / 2000 * 1_000_000
    report["cache_scheduler_decide_avg_us"] = {
        "value": round(decide_us, 2),
        "iterations": 2000,
        "label": "MEASURED",
    }

    texts = (
        "fix the failing test and restart the dev server",
        "открой браузер и проверь страницу логина",
        "summarize recent git history",
    )
    started = time.perf_counter()
    for index in range(1000):
        select_families(texts[index % 3])
    select_us = (time.perf_counter() - started) / 1000 * 1_000_000
    report["tool_universe_select_avg_us"] = {
        "value": round(select_us, 2),
        "iterations": 1000,
        "label": "MEASURED",
    }

    packet = _tests_packet(
        exit_code=1,
        passed=120,
        failed=2,
        errors=0,
        duration_seconds=8.4,
        primary_failure="tests/test_x.py::test_y",
        detail=tuple(f"line {index}" for index in range(40)),
    )
    started = time.perf_counter()
    for _ in range(1000):
        packet.render(Fidelity.AUTO)
    render_us = (time.perf_counter() - started) / 1000 * 1_000_000
    report["evidence_packet_render_avg_us"] = {
        "value": round(render_us, 2),
        "iterations": 1000,
        "label": "MEASURED",
    }

    report["unavailable"] = {
        "model_discovery": "needs a provider key in the host OS keyring",
        "project_map_cold_warm": "needs real project session state",
        "memory_recall": "needs the session-scoped memory store",
        "browser_flow": "needs the managed headed browser",
        "service_start_restart": "needs an approved server profile",
        "tui_rendering": "needs an interactive terminal",
        "usage_aggregation": "needs live persisted usage events",
    }

    _REPORT.parent.mkdir(parents=True, exist_ok=True)
    _REPORT.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    assert report["cold_import_karox_cli_seconds"]["median"] > 0  # type: ignore[index]
    assert compiled.stats.chars_in > 0
