"""Console entrypoint preserving legacy v5 commands and adding `karox agent`."""

from __future__ import annotations

import sys
from typing import Optional, Sequence


_ELLIPSIS_AGENT_SUBCOMMANDS = {
    "ellipsis",
    "attach",
    "status",
    "send",
    "stop",
    "result",
    "rollback",
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "agent":
        # `karox agent run` existed before this integration and keeps its legacy
        # meaning. Bare `karox agent`, options directly after it, and the new
        # lifecycle subcommands all select the Ellipsis local-workspace mode.
        if len(arguments) >= 2 and arguments[1] == "run":
            return _legacy(arguments)
        from .ellipsis_cli import main as ellipsis_main

        remainder = arguments[1:]
        if not remainder:
            return int(ellipsis_main(["ellipsis"]))
        if remainder[0] in _ELLIPSIS_AGENT_SUBCOMMANDS:
            return int(ellipsis_main(remainder))
        return int(ellipsis_main(["ellipsis", *remainder]))
    return _legacy(arguments)


def _legacy(arguments: Sequence[str]) -> int:
    from .cli import main as legacy_main

    previous = sys.argv
    try:
        sys.argv = [previous[0], *arguments]
        result = legacy_main()
    finally:
        sys.argv = previous
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
