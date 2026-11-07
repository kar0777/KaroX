"""Core support types and constants shared by the runtime mixins."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Optional, Sequence
from .core_process import (
    WILDCARD_ARGUMENT,
    is_plain_argument,
)
from .core_types import (
    CoreError,
)


WRITE_MODE_CREATE = "create"

WRITE_MODE_APPEND = "append"

@dataclass(frozen=True)
class VerificationRule:
    """One user-approved check command.

    An entry whose last element is ``*`` is a prefix rule: the literal prefix
    must match position for position and every remaining argument must satisfy
    :func:`karox.core_process.is_plain_argument`.  Any other entry keeps the exact-argv meaning it
    had before prefix rules existed.
    """

    prefix: tuple[str, ...]
    wildcard: bool

    @classmethod
    def parse(cls, entry: Iterable[str]) -> "VerificationRule":
        values = tuple(entry)
        if not values or not all(
            isinstance(item, str) and item for item in values
        ):
            raise CoreError(
                "a verification command must contain non-empty strings"
            )
        wildcard = values[-1] == WILDCARD_ARGUMENT
        prefix = values[:-1] if wildcard else values
        if WILDCARD_ARGUMENT in prefix:
            raise CoreError(
                f"{WILDCARD_ARGUMENT!r} is only meaningful as the last argument "
                "of a verification command"
            )
        # Without a literal prefix the rule would approve every executable, which
        # is the one thing this allowlist exists to prevent.
        if not prefix:
            raise CoreError("a verification command must name an executable")
        return cls(prefix, wildcard)

    def as_tuple(self) -> tuple[str, ...]:
        return self.prefix + ((WILDCARD_ARGUMENT,) if self.wildcard else ())

    def matches(self, argv: Sequence[str]) -> bool:
        if not self.wildcard:
            return tuple(argv) == self.prefix
        if tuple(argv[: len(self.prefix)]) != self.prefix:
            return False
        return all(is_plain_argument(item) for item in argv[len(self.prefix) :])

@lru_cache(maxsize=1)
def _ripgrep_executable() -> Optional[str]:
    """Resolve ripgrep once per process; PATH is stable for a running bridge."""
    return shutil.which("rg")
