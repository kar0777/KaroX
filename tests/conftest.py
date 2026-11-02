"""Make the release package, not an installed one, the import target."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

# A running bridge exports these to every process it starts, so a suite run
# through the bridge's own command tools would otherwise test the live bridge's
# contract (an extra diagnostics tool, extra allowed hosts) instead of the
# defaults the tests describe. Tests that need them set them explicitly.
_BRIDGE_PROCESS_ENVIRONMENT = (
    "KAROX_BRIDGE_DIAGNOSTICS_JSON",
    "KAROX_MCP_ALLOWED_HOSTS",
)


@pytest.fixture(autouse=True)
def _no_inherited_bridge_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _BRIDGE_PROCESS_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
