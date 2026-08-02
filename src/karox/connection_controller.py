"""One orchestration surface for saved MCP-client connections.

Presentation code must not independently coordinate the connection registry,
credential namespaces, live wire tests, and managed runtime ownership. Doing so
made the CLI and TUI disagree about which URL was testable, which keyring held a
ClickUp token, and whether a connection could be safely removed.

``ConnectionController`` is the unified Connections surface. It covers the
operations that have a real runtime contract: list/get/status, secret
resolution, test, start, restart, stop, and remove.

Start and restart go through :func:`karox.launch_support.launch_support`, a
capability check that refuses a saved configuration no launcher can actually
serve, instead of failing later inside a child process with an opaque error.
Adoption of a runtime this process did not originally own is allowed only when
:mod:`karox.connection_runtime` proves its strong process identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol

from .connections import (
    ConnectionCredentialStore,
    ConnectionError,
    ConnectionRegistry,
    McpClientTarget,
    connection_registry,
    connection_test_endpoint,
    remove_connection,
    resolve_connection_secret,
)
from .connection_runtime import (
    ConnectionRuntimeError,
    ConnectionRuntimeManager,
    connection_runtime_manager,
)
from .launch_support import (
    BLOCKER_AUTH_UNSUPPORTED,
    BLOCKER_CREDENTIAL_MISSING,
    BLOCKER_CUSTOM_TUNNEL_NEEDS_URL,
    BLOCKER_LAUNCHER_UNAVAILABLE,
    BLOCKER_OAUTH_NEEDS_STABLE_URL,
    BLOCKER_STABLE_URL_UNAVAILABLE,
    BLOCKER_TRANSPORT_UNSUPPORTED,
    BLOCKER_TUNNEL_UNSUPPORTED,
    LAUNCHABLE_AUTH_SCHEMES,
    LAUNCHABLE_TRANSPORTS,
    MANAGED_TUNNELS,
    TEMPORARY_URL_TUNNELS,
    ConnectionLaunchResult,
    LaunchSupport,
    launch_support as assess_launch_support,
)


class ConnectionTester(Protocol):
    def __call__(
        self,
        target: McpClientTarget,
        *,
        endpoint_url: str,
        secret: str,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class ConnectionRemover(Protocol):
    def __call__(
        self,
        connection_id: str,
        *,
        registry: ConnectionRegistry,
        credentials: Optional[ConnectionCredentialStore] = None,
    ) -> McpClientTarget: ...


class ConnectionLauncher(Protocol):
    def __call__(self, target: McpClientTarget) -> Any: ...


@dataclass(frozen=True)
class ConnectionState:
    """Desired configuration plus its current safe runtime observation."""

    target: McpClientTarget
    runtime: Mapping[str, Any]
    endpoint: Optional[str]

    @property
    def connection_id(self) -> str:
        return self.target.connection_id

    @property
    def state(self) -> str:
        return str(self.runtime.get("state") or "runtime_error")

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection_id": self.target.connection_id,
            "target": self.target.to_dict(),
            "runtime": dict(self.runtime),
            "endpoint": self.endpoint or "",
        }


@dataclass(frozen=True)
class ManagedConnectionLaunch:
    """A normalized start/restart result with safe runtime evidence."""

    status: str
    result: ConnectionLaunchResult
    runtime: Mapping[str, Any]
    support: LaunchSupport
    stopped_previous: bool = False

    @property
    def target(self) -> Optional[McpClientTarget]:
        return self.result.target

    @property
    def success(self) -> bool:
        return self.result.success

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "result": self.result.to_dict(),
            "runtime": dict(self.runtime),
            "support": self.support.to_dict(),
            "stopped_previous": self.stopped_previous,
        }


class ConnectionLaunchError(ConnectionError):
    """A start or restart was refused before or during managed launch."""

    def __init__(
        self,
        message: str,
        *,
        blockers: tuple[str, ...] = (),
        remediation: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.blockers = blockers
        self.remediation = remediation


class ConnectionController:
    """Coordinate registry, credentials, verifier, launcher, and runtime safely."""

    def __init__(
        self,
        *,
        registry: ConnectionRegistry,
        runtime_manager: ConnectionRuntimeManager,
        tester: ConnectionTester,
        secret_resolver: Callable[[McpClientTarget], str],
        remover: ConnectionRemover,
        launcher: Optional[ConnectionLauncher] = None,
        credentials: Optional[ConnectionCredentialStore] = None,
    ) -> None:
        self.registry = registry
        self.runtime_manager = runtime_manager
        self._tester = tester
        self._secret_resolver = secret_resolver
        self._remover = remover
        self._launcher = launcher
        self.credentials = credentials or ConnectionCredentialStore()

    def _state_for(self, target: McpClientTarget) -> ConnectionState:
        runtime = self.runtime_manager.status(target.connection_id)
        return ConnectionState(
            target=target,
            runtime=dict(runtime),
            endpoint=connection_test_endpoint(target),
        )

    def list(self) -> list[ConnectionState]:
        return [self._state_for(target) for target in self.registry.list()]

    def get(self, connection_id: str) -> ConnectionState:
        return self._state_for(self.registry.get(connection_id))

    def status(self, connection_id: str) -> dict[str, Any]:
        return dict(self.get(connection_id).runtime)

    def secret(self, connection_id: str) -> str:
        target = self.registry.get(connection_id)
        if not target.credential_ref:
            return ""
        return self._secret_resolver(target)

    def launch_support(self, connection_id: str) -> LaunchSupport:
        target = self.registry.get(connection_id)
        credential_available: Optional[bool]
        if target.auth_scheme == "none":
            credential_available = True
        elif not target.credential_ref:
            credential_available = False
        else:
            try:
                credential_available = bool(self._secret_resolver(target))
            except Exception:
                credential_available = False
        return assess_launch_support(
            target,
            credential_available=credential_available,
        )

    def test(
        self,
        connection_id: str,
        *,
        endpoint_url: Optional[str] = None,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        target = self.registry.get(connection_id)
        endpoint = endpoint_url or connection_test_endpoint(target)
        if not endpoint:
            raise ConnectionError(
                "no endpoint URL is known for this connection yet: start its "
                "bridge, or provide an endpoint override"
            )
        secret = self._secret_resolver(target) if target.credential_ref else ""
        result = self._tester(
            target,
            endpoint_url=endpoint,
            secret=secret,
            timeout_seconds=timeout_seconds,
        )
        return {
            "connection_id": target.connection_id,
            "url": endpoint,
            **dict(result),
        }

    def _require_launch_support(self, connection_id: str) -> LaunchSupport:
        support = self.launch_support(connection_id)
        if not support.supported:
            raise ConnectionLaunchError(
                "saved connection cannot be launched by the current runtime: "
                + ", ".join(support.blockers),
                blockers=support.blockers,
            )
        if self._launcher is None:
            raise ConnectionLaunchError(
                "no managed connection launcher is configured",
                blockers=(BLOCKER_LAUNCHER_UNAVAILABLE,),
            )
        return support

    def _launch_target(
        self,
        target: McpClientTarget,
        *,
        support: LaunchSupport,
        status: str,
        stopped_previous: bool,
    ) -> ManagedConnectionLaunch:
        assert self._launcher is not None
        result = ConnectionLaunchResult.coerce(self._launcher(target))
        if not result.success:
            kind = result.failure_kind or "launch_failed"
            detail = result.failure_detail or "managed connection launch failed"
            raise ConnectionLaunchError(
                f"{kind}: {detail}",
                blockers=(kind,),
                remediation=result.remediation,
            )
        launched_target = result.target or self.registry.get(target.connection_id)
        runtime = dict(self.runtime_manager.status(launched_target.connection_id))
        return ManagedConnectionLaunch(
            status=status,
            result=ConnectionLaunchResult(
                success=True,
                target=launched_target,
                public_endpoint=result.public_endpoint,
                local_endpoint=result.local_endpoint,
                runtime_id=result.runtime_id,
                raw=result.raw,
            ),
            runtime=runtime,
            support=support,
            stopped_previous=stopped_previous,
        )

    def start(self, connection_id: str) -> ManagedConnectionLaunch:
        state = self.get(connection_id)
        current = state.state
        support = self._require_launch_support(connection_id)
        if current == "running":
            return ManagedConnectionLaunch(
                status="already_running",
                result=ConnectionLaunchResult(
                    success=True,
                    target=state.target,
                    public_endpoint=state.endpoint,
                    runtime_id=(
                        str(state.runtime.get("runtime_id"))
                        if state.runtime.get("runtime_id")
                        else None
                    ),
                ),
                runtime=dict(state.runtime),
                support=support,
            )
        if current == "degraded":
            raise ConnectionLaunchError(
                "the connection runtime is degraded; use restart so KaroX can "
                "stop the proven process tree before launching a replacement"
            )
        if current == "unmanaged_running":
            raise ConnectionRuntimeError(
                "the connection has a live runtime whose identity is not proven; "
                "refusing to launch a second copy"
            )
        return self._launch_target(
            state.target,
            support=support,
            status="started",
            stopped_previous=False,
        )

    def restart(self, connection_id: str) -> ManagedConnectionLaunch:
        state = self.get(connection_id)
        support = self._require_launch_support(connection_id)
        if state.state == "unmanaged_running":
            raise ConnectionRuntimeError(
                "the connection has a live runtime whose identity is not proven; "
                "refusing an unsafe restart"
            )
        stopped_previous = False
        if state.state in {"running", "degraded"}:
            self.runtime_manager.stop(connection_id)
            stopped_previous = True
        target = self.registry.get(connection_id)
        return self._launch_target(
            target,
            support=support,
            status="restarted" if stopped_previous else "started",
            stopped_previous=stopped_previous,
        )

    def stop(self, connection_id: str) -> dict[str, Any]:
        # Resolve the config first so status/stop cannot operate on an orphaned
        # runtime ID that has no user-visible saved connection.
        self.registry.get(connection_id)
        return dict(self.runtime_manager.stop(connection_id))

    def remove(self, connection_id: str) -> dict[str, Any]:
        # Resolve the saved config first so a removal cannot act on an orphaned
        # runtime ID that has no user-visible connection behind it.
        self.registry.get(connection_id)
        runtime = self.runtime_manager.status(connection_id)
        state = str(runtime.get("state") or "runtime_error")
        if state in {"running", "degraded"}:
            self.runtime_manager.stop(connection_id)
        elif state == "unmanaged_running":
            raise ConnectionRuntimeError(
                "the connection still has an unmanaged live process; stop it "
                "from the KaroX process that owns it before removing"
            )
        removed = self._remover(
            connection_id,
            registry=self.registry,
            credentials=self.credentials,
        )
        self.runtime_manager.forget(connection_id)
        return {
            "connection_id": removed.connection_id,
            "name": removed.name,
            "status": "removed",
        }


def connection_controller() -> ConnectionController:
    """Build the process-local controller with late-bound patchable dependencies."""

    def tester(
        target: McpClientTarget,
        *,
        endpoint_url: str,
        secret: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        from .connection_tests import test_mcp_client_target

        return test_mcp_client_target(
            target,
            endpoint_url=endpoint_url,
            secret=secret,
            timeout_seconds=timeout_seconds,
        )

    def launcher(target: McpClientTarget) -> Any:
        from .clickup_setup import start_saved_clickup_connection

        return start_saved_clickup_connection(target)

    return ConnectionController(
        registry=connection_registry(),
        runtime_manager=connection_runtime_manager(),
        tester=tester,
        secret_resolver=lambda target: resolve_connection_secret(target),
        remover=remove_connection,
        launcher=launcher,
        credentials=ConnectionCredentialStore(),
    )


__all__ = [
    "ConnectionController",
    "ConnectionState",
    "ManagedConnectionLaunch",
    "ConnectionLaunchError",
    "connection_controller",
    "BLOCKER_TRANSPORT_UNSUPPORTED",
    "BLOCKER_TUNNEL_UNSUPPORTED",
    "BLOCKER_CUSTOM_TUNNEL_NEEDS_URL",
    "BLOCKER_STABLE_URL_UNAVAILABLE",
    "BLOCKER_OAUTH_NEEDS_STABLE_URL",
    "BLOCKER_CREDENTIAL_MISSING",
    "BLOCKER_AUTH_UNSUPPORTED",
    "BLOCKER_LAUNCHER_UNAVAILABLE",
    "LAUNCHABLE_TRANSPORTS",
    "MANAGED_TUNNELS",
    "TEMPORARY_URL_TUNNELS",
    "LAUNCHABLE_AUTH_SCHEMES",
]
