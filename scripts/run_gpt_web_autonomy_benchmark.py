"""Run the isolated GPT Web autonomy benchmark and write a redacted JSON artifact."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from karox.autonomy_benchmark import write_benchmark_artifact  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run ten isolated baseline/optimized autonomy tasks. This produces a "
            "server-side and deterministic simulated-client benchmark, not a model benchmark."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "scratch" / "gpt_web_autonomy_benchmark.json",
        help="Destination JSON artifact (default: scratch/gpt_web_autonomy_benchmark.json)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = write_benchmark_artifact(args.output)
    comparison = result["simulated_client"]["comparison"]
    baseline = result["simulated_client"]["baseline"]["aggregate"]
    optimized = result["simulated_client"]["optimized"]["aggregate"]
    print(f"output={args.output.expanduser().resolve()}")
    print(f"tasks={result['server_side']['tasks_executed']}")
    print(f"baseline_success={baseline['task_success_rate']:.3f}")
    print(f"optimized_success={optimized['task_success_rate']:.3f}")
    print(
        "median_tool_call_reduction="
        f"{comparison['median_tool_call_reduction']:.3f}"
    )
    print(
        "median_inline_context_reduction="
        f"{comparison['median_inline_context_reduction']:.3f}"
    )
    print(f"all_targets_met={comparison['all_targets_met']}")
    print("paired_chatgpt_web=not_run_user_gate")
    return 0 if comparison["all_targets_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
