"""`karox` command line entry point.

Imports stay inside the functions: `karox --version` must not pay for the
agent stack (startup budget 300 ms, checked in CI by scripts/perf_check.py).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def _parser() -> argparse.ArgumentParser:
    from karox6 import __version__

    parser = argparse.ArgumentParser(prog="karox", description="KaroX 6 coding agent")
    parser.add_argument("--version", action="version", version=f"karox {__version__}")
    sub = parser.add_subparsers(dest="command")

    debug = sub.add_parser("debug", help="diagnostics for developers")
    debug_sub = debug.add_subparsers(dest="debug_command", required=True)
    debug_sub.add_parser("rss", help="load the headless stack and print resident memory (MiB)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "debug" and args.debug_command == "rss":
        return _debug_rss()
    _parser().print_help()
    return 0


def _debug_rss() -> int:
    """Import everything a headless run imports, then report RSS."""
    import karox6.stack  # noqa: F401  (side effect: imports the headless stack)
    from karox6.sysinfo import rss_mb

    sys.stdout.write(f"{rss_mb():.1f}\n")
    return 0
