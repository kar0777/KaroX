"""Tailscale Funnel route planning/apply/stop."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from .bridge_tailscale_status import (
    TailscaleError,
    find_tailscale,
    query_tailscale_status,
    query_funnel_ownership,
    classify_funnel_failure,
)
from .bridge_tailscale_gui import (
    TailscaleLaunchPlan,
    ensure_tailscale_ready,
)


def _normalize_funnel_mount_path(mount_path: str) -> str:
    if not isinstance(mount_path, str) or not mount_path.startswith("/"):
        raise ValueError("Tailscale Funnel mount path must start with /")
    if "?" in mount_path or "#" in mount_path or "//" in mount_path:
        raise ValueError("Tailscale Funnel mount path is invalid")
    normalized = mount_path.rstrip("/") or "/"
    if any(part in {".", ".."} for part in normalized.split("/")):
        raise ValueError("Tailscale Funnel mount path is invalid")
    return normalized

def tailscale_funnel_argv(
    executable: str,
    port: int,
    *,
    https_port: int = 443,
    mount_path: str = "/",
) -> tuple[str, ...]:
    if not 1 <= port <= 65_535:
        raise ValueError("Tailscale Funnel target port must be between 1 and 65535")
    if https_port not in {443, 8443, 10000, 10001}:
        raise ValueError("Tailscale Funnel HTTPS port must be 443, 8443, 10000, or 10001")
    normalized_path = _normalize_funnel_mount_path(mount_path)
    values = [executable, "funnel", "--yes", f"--https={https_port}"]
    if normalized_path != "/":
        values.append(f"--set-path={normalized_path}")
    values.append(f"http://127.0.0.1:{port}")
    return tuple(values)

def prepare_tailscale_funnel(
    port: int,
    *,
    https_port: int = 443,
    mount_path: str = "/",
    executable: Optional[str] = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    emit: Optional[Callable[[str], None]] = None,
    restart_service: bool = False,
    elevated_run: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
) -> TailscaleLaunchPlan:
    """Fail before mutation unless this process can own a new Funnel route.

    If Tailscale is installed but only transiently not ready -- the daemon is
    still starting, which is the state a freshly-booted Windows machine reports
    -- one ``tailscale up`` is run and readiness is polled before giving up.
    A login gate is reported with its auth URL instead of blocking on a browser
    the launch path never opens.
    """
    if https_port not in {443, 8443, 10000, 10001}:
        raise TailscaleError(
            "invalid_https_port: Funnel supports 443, 8443, 10000, or 10001"
        )
    try:
        normalized_path = _normalize_funnel_mount_path(mount_path)
    except ValueError as exc:
        raise TailscaleError(f"invalid_mount_path: {exc}") from exc
    status = ensure_tailscale_ready(
        executable,
        run=run,
        emit=emit,
        restart_service=restart_service,
        elevated_run=elevated_run,
    )
    if not status.get("ready"):
        auth_url = status.get("auth_url")
        suffix = f" — sign in at {auth_url}" if auth_url else ""
        raise TailscaleError(f"{status.get('code')}: {status.get('detail')}{suffix}")
    resolved = status.get("executable")
    public_url = status.get("public_url")
    if not isinstance(resolved, str) or not isinstance(public_url, str):
        raise TailscaleError("hostname_unavailable: stable Tailscale URL is unavailable")
    ownership = query_funnel_ownership(resolved, run=run)
    if not ownership.get("known"):
        raise TailscaleError(
            "ownership_unknown: cannot prove that starting Funnel will preserve "
            "existing Tailscale Serve/Funnel routes"
        )
    if ownership.get("active"):
        routes = inventory_tailscale_routes(resolved, run=run)
        if not routes:
            # Something is active but we cannot parse it into individual
            # routes. Fail-safe: refuse rather than risk clearing a foreign
            # route we could not identify.
            raise TailscaleError(
                "route_in_use: an existing Tailscale Serve/Funnel route is "
                "active but its details cannot be parsed; KaroX refuses to "
                "replace it. Run `karox bridge doctor`."
            )
        # Tailscale can host multiple path mounts on one HTTPS listener. Only
        # the route KaroX is about to own -- the requested path -- can conflict
        # with this launcher. Sibling paths must be preserved.
        desired_routes = [
            route
            for route in routes
            if route.public_port == https_port
            and (route.path.rstrip("/") or "/") == normalized_path
        ]
        foreign = [route for route in desired_routes if route.local_port != port]
        stale_owned = [route for route in desired_routes if route.local_port == port]
        if foreign:
            descriptions = ", ".join(
                f"{route.host}:{route.public_port}{route.path}->{route.local_target}"
                for route in foreign
            )
            raise TailscaleError(
                f"route_in_use: HTTPS {https_port} is already owned by another "
                f"Tailscale route ({descriptions})"
            )
        if stale_owned:
            if emit is not None:
                emit(
                    "Clearing the stale KaroX Funnel listener "
                    f"HTTPS {https_port} -> localhost:{port}…"
                )
            cleanup_argv = [resolved, "funnel", f"--https={https_port}"]
            if normalized_path != "/":
                cleanup_argv.append(f"--set-path={normalized_path}")
            cleanup_argv.extend((f"http://127.0.0.1:{port}", "off"))
            try:
                result = run(
                    cleanup_argv,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise TailscaleError(
                    "stale_route_clear_failed: could not clear stale owned "
                    f"route: {type(exc).__name__}"
                ) from exc
            if result.returncode != 0:
                raise TailscaleError(
                    "stale_route_clear_failed: Tailscale refused the exact route cleanup"
                )
            remaining = inventory_tailscale_routes(resolved, run=run)
            if any(
                route.public_port == https_port
                and (route.path.rstrip("/") or "/") == normalized_path
                and route.local_port == port
                for route in remaining
            ):
                raise TailscaleError(
                    "route_in_use: stale route could not be cleared; "
                    "run `karox bridge doctor`"
                )
        # Routes on the other supported HTTPS ports are intentionally preserved.
    published_url = public_url.rstrip("/")
    if https_port != 443:
        published_url = f"{published_url}:{https_port}"
    return TailscaleLaunchPlan(
        executable=resolved,
        public_url=published_url,
        argv=tailscale_funnel_argv(
            resolved, port, https_port=https_port, mount_path=normalized_path
        ),
        ownership=ownership,
    )

def inventory_tailscale_routes(
    executable: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[Any]:
    from .tailscale_routes import inventory_tailscale_routes as inventory

    return inventory(executable, run=run)

@dataclass(frozen=True)
class TailscaleFunnel:
    """A daemon-managed Funnel route owned by one bridge profile."""

    executable: str
    public_url: str
    port: int
    https_port: int = 443
    mount_path: str = "/"
    lifecycle: str = "daemon_background"

    def to_dict(self) -> dict[str, Any]:
        return {
            "executable": self.executable,
            "public_url": self.public_url,
            "port": self.port,
            "https_port": self.https_port,
            "mount_path": self.mount_path,
            "lifecycle": self.lifecycle,
        }

def background_funnel_argv(
    executable: str,
    port: int,
    *,
    https_port: int = 443,
    mount_path: str = "/",
) -> list[str]:
    """The daemon configuration command for one exact KaroX route."""
    if not 1 <= port <= 65_535:
        raise ValueError("Tailscale Funnel target port must be between 1 and 65535")
    if https_port not in {443, 8443, 10000, 10001}:
        raise ValueError("Tailscale Funnel HTTPS port must be 443, 8443, 10000, or 10001")
    normalized_path = _normalize_funnel_mount_path(mount_path)
    argv = [
        executable,
        "funnel",
        "--bg",
        "--yes",
        f"--https={https_port}",
    ]
    if normalized_path != "/":
        argv.append(f"--set-path={normalized_path}")
    argv.append(f"http://127.0.0.1:{port}")
    return argv

def _matching_background_funnel_routes(
    executable: str,
    *,
    public_url: str,
    port: int,
    https_port: int = 443,
    mount_path: str = "/",
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[list[Any], list[Any]]:
    """Split exact-mount routes into owned (same local port) and foreign."""
    host = urlsplit(public_url).hostname or ""
    normalized_path = mount_path.rstrip("/") or "/"
    routes = inventory_tailscale_routes(executable, run=run)
    same_mount = [
        route
        for route in routes
        if route.public_port == https_port
        and (not route.host or route.host == host)
        and (route.path.rstrip("/") or "/") == normalized_path
    ]
    owned = [route for route in same_mount if route.local_port == port]
    foreign = [route for route in same_mount if route.local_port != port]
    return owned, foreign

def _run_kwargs(timeout_seconds: float) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "check": False,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": max(1.0, float(timeout_seconds)),
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs

def _apply_background_funnel(
    executable: str,
    *,
    public_url: str,
    port: int,
    https_port: int = 443,
    mount_path: str = "/",
    timeout_seconds: float,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    expected_stale_ports: frozenset[int] = frozenset(),
) -> None:
    """Configure the exact owned route and prove the daemon applied it."""
    try:
        result = run(
            background_funnel_argv(
                executable,
                port,
                https_port=https_port,
                mount_path=mount_path,
            ),
            **_run_kwargs(timeout_seconds),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TailscaleError(
            f"cannot configure background Tailscale Funnel: {type(exc).__name__}"
        ) from exc
    detail = "\n".join(
        value.strip()
        for value in (result.stdout or "", result.stderr or "")
        if value and value.strip()
    )
    if result.returncode != 0:
        failure = classify_funnel_failure(detail)
        raise TailscaleError(f"{failure['code']}: {failure['detail']}")

    # `--bg` stores configuration in tailscaled. Do not trust the CLI exit code
    # alone: verify that the daemon reports the exact host/port route KaroX owns.
    deadline = time.monotonic() + min(max(float(timeout_seconds), 1.0), 15.0)
    while time.monotonic() < deadline:
        owned, others = _matching_background_funnel_routes(
            executable,
            public_url=public_url,
            port=port,
            https_port=https_port,
            mount_path=mount_path,
            run=run,
        )
        # During an owned retarget the daemon may briefly still report the
        # route at the previous KaroX-owned port. That route is stale, not
        # foreign: wait for the daemon to converge instead of refusing.
        stale = [route for route in others if route.local_port in expected_stale_ports]
        foreign = [
            route for route in others if route.local_port not in expected_stale_ports
        ]
        if foreign:
            raise TailscaleError(
                "route_in_use: background Funnel configuration introduced or "
                "encountered a foreign Tailscale route; refusing to claim ownership"
            )
        if owned and not stale:
            return
        time.sleep(0.2)
    raise TailscaleError(
        "funnel_not_applied: background Tailscale Funnel was accepted but the "
        "owned route did not appear in Tailscale status"
    )

def find_owned_funnel(
    port: int,
    *,
    https_port: int = 443,
    mount_path: str = "/",
    executable: Optional[str] = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Optional[TailscaleFunnel]:
    """Return the exact owned route if it is already live, else ``None``.

    Read-only. A foreign route on the same mount raises, because starting the
    bridge would otherwise have to replace it.
    """
    status = query_tailscale_status(executable, run=run)
    if not status.get("ready"):
        return None
    resolved = status.get("executable")
    public_url = status.get("public_url")
    if not isinstance(resolved, str) or not isinstance(public_url, str):
        return None
    published_url = public_url.rstrip("/")
    if https_port != 443:
        published_url = f"{published_url}:{https_port}"
    owned, foreign = _matching_background_funnel_routes(
        resolved,
        public_url=published_url,
        port=port,
        https_port=https_port,
        mount_path=mount_path,
        run=run,
    )
    if foreign:
        descriptions = ", ".join(
            f"{route.host}:{route.public_port}{route.path}->{route.local_target}"
            for route in foreign
        )
        raise TailscaleError(
            f"route_in_use: HTTPS {https_port} path {mount_path} is owned by a "
            f"foreign Tailscale route ({descriptions})"
        )
    if owned:
        return TailscaleFunnel(
            resolved,
            published_url,
            port,
            https_port=https_port,
            mount_path=mount_path,
        )
    return None

def start_tailscale_funnel(
    port: int,
    *,
    https_port: int = 443,
    mount_path: str = "/",
    executable: Optional[str] = None,
    timeout_seconds: float = 30.0,
    emit: Optional[Callable[[str], None]] = None,
    restart_service: bool = False,
    elevated_run: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> TailscaleFunnel:
    """Create a daemon-managed, persistent Funnel with proven route ownership."""
    try:
        plan = prepare_tailscale_funnel(
            port,
            https_port=https_port,
            mount_path=mount_path,
            executable=executable,
            run=run,
            emit=emit,
            restart_service=restart_service,
            elevated_run=elevated_run,
        )
    except TailscaleError:
        raise
    _apply_background_funnel(
        plan.executable,
        public_url=plan.public_url,
        port=port,
        https_port=https_port,
        mount_path=mount_path,
        timeout_seconds=timeout_seconds,
        run=run,
    )
    if emit is not None:
        emit(f"Background Funnel is ready: {plan.public_url}")
    return TailscaleFunnel(
        plan.executable,
        plan.public_url,
        port,
        https_port=https_port,
        mount_path=mount_path,
    )

def ensure_tailscale_funnel(
    port: int,
    *,
    https_port: int = 443,
    mount_path: str = "/",
    executable: Optional[str] = None,
    timeout_seconds: float = 30.0,
    emit: Optional[Callable[[str], None]] = None,
    restart_service: bool = False,
    elevated_run: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> TailscaleFunnel:
    """Reuse the exact owned route when it is live, otherwise create it.

    Reuse is what keeps the public ``*.ts.net`` origin (and therefore every
    OAuth registration bound to it) stable across bridge restarts.
    """
    existing = find_owned_funnel(
        port,
        https_port=https_port,
        mount_path=mount_path,
        executable=executable,
        run=run,
    )
    if existing is not None:
        if emit is not None:
            emit(f"Reusing the existing Funnel route: {existing.public_url}")
        return existing
    return start_tailscale_funnel(
        port,
        https_port=https_port,
        mount_path=mount_path,
        executable=executable,
        timeout_seconds=timeout_seconds,
        emit=emit,
        restart_service=restart_service,
        elevated_run=elevated_run,
        run=run,
    )

def refresh_tailscale_funnel(
    tunnel: TailscaleFunnel,
    *,
    timeout_seconds: float = 30.0,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> TailscaleFunnel:
    """Reapply only the daemon route already proven to belong to this bridge."""
    owned, foreign = _matching_background_funnel_routes(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=tunnel.port,
        https_port=tunnel.https_port,
        mount_path=tunnel.mount_path,
        run=run,
    )
    if foreign:
        raise TailscaleError(
            "route_in_use: Tailscale route recovery found a foreign route; "
            "refusing to mutate Funnel"
        )
    # If the route disappeared entirely, reapplying is also safe: its hostname
    # and local target are fixed by the already-proven Tailscale identity and
    # bridge port.
    _apply_background_funnel(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=tunnel.port,
        https_port=tunnel.https_port,
        mount_path=tunnel.mount_path,
        timeout_seconds=timeout_seconds,
        run=run,
    )
    return tunnel

def retarget_tailscale_funnel(
    tunnel: TailscaleFunnel,
    new_port: int,
    *,
    timeout_seconds: float = 30.0,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> TailscaleFunnel:
    """Repoint the owned daemon Funnel route at a new local backend port.

    The stable public hostname is the bridge's identity: it never changes here,
    and no bridge credential is read, written, or rotated. A same-listener route
    that matches neither the current nor the requested port belongs to another
    program, so the retarget is refused before any mutation.
    """
    requested_port = int(new_port)
    if requested_port == tunnel.port:
        return refresh_tailscale_funnel(
            tunnel, timeout_seconds=timeout_seconds, run=run
        )
    _owned, others = _matching_background_funnel_routes(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=tunnel.port,
        https_port=tunnel.https_port,
        mount_path=tunnel.mount_path,
        run=run,
    )
    foreign = [route for route in others if route.local_port != requested_port]
    if foreign:
        raise TailscaleError(
            "route_in_use: Tailscale retarget found a route owned by neither the "
            "current nor the requested bridge port; refusing to mutate a foreign route"
        )
    _apply_background_funnel(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=requested_port,
        https_port=tunnel.https_port,
        mount_path=tunnel.mount_path,
        timeout_seconds=timeout_seconds,
        run=run,
        expected_stale_ports=frozenset({tunnel.port}),
    )
    return TailscaleFunnel(
        tunnel.executable,
        tunnel.public_url,
        requested_port,
        https_port=tunnel.https_port,
        mount_path=tunnel.mount_path,
    )

def stop_tailscale_funnel(
    tunnel: TailscaleFunnel,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """Turn off only the exact background Funnel route still owned by KaroX."""
    owned, foreign = _matching_background_funnel_routes(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=tunnel.port,
        https_port=tunnel.https_port,
        mount_path=tunnel.mount_path,
        run=run,
    )
    if not owned:
        # No KaroX-owned route to stop. Report success only when nothing
        # foreign is occupying the same public endpoint; claiming a clean stop
        # while another program still serves it would hide a live exposure.
        return not foreign
    if foreign:
        # Fail closed during cleanup. Leaving a stale proxy to a closed localhost
        # port is safer than deleting another program's Tailscale route.
        return False
    cleanup_argv = [
        tunnel.executable,
        "funnel",
        f"--https={tunnel.https_port}",
    ]
    if tunnel.mount_path != "/":
        # Tailscale path routes are daemon config entries: this exact form
        # removes only the selected path and preserves the root handler.
        cleanup_argv.extend((f"--set-path={tunnel.mount_path}", "off"))
    else:
        cleanup_argv.extend((f"http://127.0.0.1:{tunnel.port}", "off"))
    try:
        result = run(cleanup_argv, **_run_kwargs(15))
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    remaining, _foreign = _matching_background_funnel_routes(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=tunnel.port,
        https_port=tunnel.https_port,
        mount_path=tunnel.mount_path,
        run=run,
    )
    return not remaining

def stop_tailscale_route_for_port(
    port: int,
    *,
    https_port: int = 443,
    executable: Optional[str] = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """Stop the unique route whose local target is ``port``, or refuse.

    Used only when the owner is already dead and its watchdog record is gone:
    the port is KaroX's own proven identity at that point. Multiple matching
    routes are ambiguous and are left untouched.
    """
    resolved = find_tailscale(executable)
    if resolved is None:
        return False
    routes = [
        route
        for route in inventory_tailscale_routes(resolved, run=run)
        if route.public_port == https_port and route.local_port == port
    ]
    if len(routes) != 1:
        return False
    route = routes[0]
    host = route.host
    public_url = f"https://{host}"
    if https_port != 443:
        public_url = f"{public_url}:{https_port}"
    return stop_tailscale_funnel(
        TailscaleFunnel(resolved, public_url, port, https_port, route.path),
        run=run,
    )

TailscaleBackgroundFunnel = TailscaleFunnel

start_tailscale_background_funnel = start_tailscale_funnel

refresh_tailscale_background_funnel = refresh_tailscale_funnel

retarget_tailscale_background_funnel = retarget_tailscale_funnel

stop_tailscale_background_funnel = stop_tailscale_funnel


def funnel_status(
    tunnel: TailscaleFunnel,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Report exact-route ownership without mutating anything."""
    owned, foreign = _matching_background_funnel_routes(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=tunnel.port,
        https_port=tunnel.https_port,
        mount_path=tunnel.mount_path,
        run=run,
    )
    return {
        "owned": bool(owned),
        "foreign": [route.to_dict() for route in foreign],
        "public_url": tunnel.public_url,
        "port": tunnel.port,
        "https_port": tunnel.https_port,
        "mount_path": tunnel.mount_path,
    }
