"""Core's exception vocabulary and the tool descriptor.

These are the only names small modules (browser tools, the workspace worker,
patches, transactions) need from Core. Keeping them apart means importing
``karox.browser`` no longer pulls the wholeCoreRuntime stack in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from .models import Capability


class CoreError(RuntimeError):
    pass

def _path_label(value: object, limit: int = 120) -> str:
    """The offending value, single-line and short, for a rejection message.

    A path rejection that does not name the path it rejected cannot be acted on:
    the caller has to guess which of its arguments was wrong and usually guesses
    the same way again.
    """
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"

class InvalidPath(CoreError):
    pass

class InvalidCommand(CoreError):
    pass

class VerificationCommandNotApproved(InvalidCommand):
    """A checks.run argv is valid, but outside the user's verification allowlist."""

@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    capability: Capability
    mutates: bool
    input_schema: Dict[str, Any]
    additional_capabilities: tuple[Capability, ...] = ()
    external_schema: bool = False
    # A mutating tool normally replays its stored outcome for a repeated
    # idempotency key. A check must not: its answer describes a working tree that
    # may have changed since, and replaying it reports a stale pass as fresh.
    replayable: bool = True
