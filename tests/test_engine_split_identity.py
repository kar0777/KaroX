"""Pin the engine-agent identity across the mixin split.

The vendored engine serializes the concrete class's ``__module__`` into its
trajectory, and the product report names ``karox.engine_kernel.EngineAgent``.
The deep refactor moves the methods into a mixin, but the concrete class must
stay in ``engine_kernel`` so that durable artifact does not change.
"""

from __future__ import annotations

from karox import engine_kernel
from karox.engine_agent import _EngineAgentMixin
from karox.engine_kernel import EngineAgent


def test_engine_agent_class_stays_in_the_kernel_module() -> None:
    assert EngineAgent.__module__ == "karox.engine_kernel"
    assert EngineAgent.__name__ == "EngineAgent"
    assert issubclass(EngineAgent, _EngineAgentMixin)


def test_engine_identity_still_names_the_public_class() -> None:
    identity = engine_kernel._ENGINE_IDENTITY
    assert identity["name"] == "karo-core"
    assert identity["implementation"] == "karox.engine_kernel.EngineAgent"
