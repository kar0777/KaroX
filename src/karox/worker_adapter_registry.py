"""In-process registry of guarded non-API orchestration worker adapters.

The registry stores callables only for the lifetime of the hosting KaroX process.
It deliberately has no JSON/CLI command field, subprocess launcher, cookie store,
or credential input.  An application that knows how to safely drive Claude Code,
Codex, a local model, Traycer, or another target registers a PromptTarget callable
under the same target_id that appears in IntelligencePool metadata.
"""

from __future__ import annotations

import threading
from typing import Iterable, Mapping, Optional

from .intelligence_pool import IntelligenceEndpoint, IntelligencePool, SOURCE_API
from .orchestrator import WorkerExecutor
from .worker_adapters import GuardedPromptExecutor, PromptTarget


class WorkerAdapterRegistryError(RuntimeError):
    pass


class WorkerAdapterRegistry:
    def __init__(self) -> None:
        self._targets: dict[str, PromptTarget] = {}
        self._lock = threading.RLock()

    def register(self, target_id: str, target: PromptTarget, *, replace: bool = False) -> None:
        if not isinstance(target_id, str) or not target_id or len(target_id) > 256:
            raise ValueError("worker adapter target_id must contain 1-256 characters")
        if not callable(target):
            raise ValueError("worker adapter target must be callable")
        with self._lock:
            if target_id in self._targets and not replace:
                raise WorkerAdapterRegistryError(f"worker adapter is already registered: {target_id}")
            self._targets[target_id] = target

    def unregister(self, target_id: str) -> bool:
        with self._lock:
            return self._targets.pop(target_id, None) is not None

    def target_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._targets))

    def get(self, target_id: str) -> PromptTarget:
        with self._lock:
            try:
                return self._targets[target_id]
            except KeyError as exc:
                raise WorkerAdapterRegistryError(
                    f"no guarded worker adapter is registered for target_id {target_id}"
                ) from exc

    def executor_for(self, endpoint: IntelligenceEndpoint) -> WorkerExecutor:
        if endpoint.source_kind == SOURCE_API:
            raise WorkerAdapterRegistryError(
                "API endpoints use NativeAgentExecutor, not WorkerAdapterRegistry"
            )
        if endpoint.target_id is None:
            raise WorkerAdapterRegistryError(
                f"endpoint has no target_id: {endpoint.endpoint_id}"
            )
        return GuardedPromptExecutor(self.get(endpoint.target_id))

    def build_executors(
        self,
        endpoints: Iterable[IntelligenceEndpoint],
        *,
        existing: Optional[Mapping[str, WorkerExecutor]] = None,
        strict: bool = True,
    ) -> dict[str, WorkerExecutor]:
        result = dict(existing or {})
        missing: list[str] = []
        for endpoint in endpoints:
            if endpoint.source_kind == SOURCE_API or endpoint.endpoint_id in result:
                continue
            try:
                result[endpoint.endpoint_id] = self.executor_for(endpoint)
            except WorkerAdapterRegistryError:
                missing.append(endpoint.endpoint_id)
        if strict and missing:
            raise WorkerAdapterRegistryError(
                "missing guarded worker adapters for endpoint(s): " + ", ".join(sorted(missing))
            )
        return result

    def build_for_pool(
        self,
        pool: IntelligencePool,
        *,
        existing: Optional[Mapping[str, WorkerExecutor]] = None,
        strict: bool = False,
    ) -> dict[str, WorkerExecutor]:
        return self.build_executors(
            pool.list(include_disabled=False), existing=existing, strict=strict
        )


__all__ = ["WorkerAdapterRegistry", "WorkerAdapterRegistryError"]
