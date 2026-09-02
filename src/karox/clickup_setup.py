"""Auto-setup orchestrator for ClickUp preset connections.

The ClickUp preset is nearly automatic: the user picks ClickUp and presses one
button, and this module launches the real MCP bridge server, the tunnel, waits
for readiness, runs a genuine MCP handshake (initialize + tools/list), and
returns a ready-to-use connection card.  It reuses the bridge and tunnel
primitives the rest of the project already runs on -- it does not build a
second MCP server or a second tunnel.

The function is split into *seams* so the coordination logic is testable
without spawning the full ``karox bridge serve`` child (which needs a
configured repository and the tool runtime).  ``server_launcher`` and
``tunnel_launcher`` are callables; the defaults use the real launchers from
``web_bridge_launcher`` and ``bridge``, and the tests inject a launcher that
starts a genuine ``mcp`` SDK Streamable HTTP server on loopback, so the
handshake in the tests is real (initialize + tools/list over HTTP), not mocked.

Cancelling (Esc during setup) is honoured between steps: a ``cancellation``
callable returns ``True`` to abort, and any started server/tunnel is stopped
and the generated secret removed so no dangling processes or orphaned
credentials are left behind.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, cast
from unittest.mock import patch

from .connections import (
    ClickupDefaults,
    ConnectionConfigurationError,
    McpClientTarget,
    connection_registry,
    is_managed_streamable_bearer_target,
)

# Public step names the progress callback reports, in order.  Kept as constants
# so the TUI and the tests refer to the same labels.
STEP_CONFIG = "config"
STEP_SECRET = "secret"
STEP_SERVER = "server"
STEP_TUNNEL = "tunnel"
STEP_URL = "url"
STEP_HANDSHAKE = "handshake"


@dataclass
class ServerHandle:
    """A started local MCP bridge server, with a way to stop it cleanly.

    ``stop`` is the full cleanup: terminate the child *and* delete the generated
    credential.  ``stop_process`` terminates only the child and leaves the
    credential intact -- what a *saved* connection needs, because the saved card
    references that credential and must not be left pointing at a deleted
    secret.  Launchers that own no real process (the test seams) leave
    ``stop_process`` as ``None`` and it degrades to a no-op.
    """

    local_endpoint: str
    secret: str
    port: int
    credential_name: str
    stop: Callable[[], None]
    stop_process: Optional[Callable[[], None]] = None
    pid: Optional[int] = None


@dataclass
class TunnelHandle:
    """A started tunnel exposing a public URL, with a way to stop it."""

    public_url: str
    stop: Callable[[], None]
    pid: Optional[int] = None


@dataclass
class ClickupSetupOutcome:
    """The result of an auto-setup run, whether it succeeded or failed.

    On success ``target`` is the saved :class:`McpClientTarget` and
    ``public_endpoint`` the URL the user pastes into ClickUp.  On failure
    ``failure_kind`` is a short machine code and ``remediation`` a one-line
    action the user can take (``install_cloudflared`` / ``login_tailscale`` /
    ``retry`` / ``open_log`` / ``recreate_secret``), mirroring the spec's
    per-stage error actions.
    """

    success: bool
    target: Optional[McpClientTarget] = None
    public_endpoint: Optional[str] = None
    # The loopback URL of the same bridge. It is diagnostic-only evidence: a
    # local success never makes a public ClickUp connector ready.
    local_endpoint: Optional[str] = None
    handshake: Optional[Mapping[str, Any]] = None
    failure_kind: Optional[str] = None
    failure_detail: Optional[str] = None
    remediation: Optional[str] = None
    defaults: Optional[ClickupDefaults] = None
    # Shuts down the live bridge and tunnel a *successful* run left running, and
    # is the reason a second attempt does not inherit a stranger on its port.
    #
    # Success deliberately leaves both processes up: the connection only works
    # while they run.  But the bridge is started in its own process group and
    # survives the app that spawned it, so without a way to stop it every run
    # accumulated one more orphan holding one more port -- and the next run's
    # readiness probe then adopted that orphan and authenticated against its
    # secret, failing as an unexplained 401.  Keeping the credential is
    # deliberate: the saved card references it.
    stop: Optional[Callable[[], None]] = None
    runtime_id: Optional[str] = None


def _bridge_argv(
    *,
    profile: str,
    repository: Path,
    session_id: str,
    port: int,
    tools: tuple[str, ...] = (),
    verification_commands: tuple[tuple[str, ...], ...] = (),
) -> tuple[str, ...]:
    """Build the ``karox bridge serve`` child argv for a bearer profile.

    Mirrors :func:`karox.tui._bridge_launch` and
    :func:`karox.web_bridge_launcher._bridge_argv` so the auto-setup child is
    indistinguishable from a manually-launched bridge: same module, same
    ``--protocol mcp`` (ClickUp is Streamable HTTP), same ``--session-id`` and
    ``--credential`` (the server validates the bearer token against the
    ``os-keyring:bridge/<session_id>`` credential).  No second server is built.

    ``bridge serve`` requires at least one ``--tool`` (it exits with code 2,
    "requires at least one --tool or --server", otherwise), so the orchestrator
    passes the fixed guarded ClickUp developer tool set and verification
    allowlist.
    """
    argv: list[str] = [
        sys.executable,
        "-m",
        "karox.cli",
        "bridge",
        "serve",
        "--profile",
        profile,
        "--protocol",
        "mcp",
        "--repository",
        str(repository),
        "--session-id",
        session_id,
        "--credential",
        session_id,
        "--port",
        str(port),
    ]
    for tool in tools:
        argv.extend(("--tool", tool))
    for command in verification_commands:
        argv.extend(("--verification-command", json.dumps(list(command))))
    return tuple(argv)


# ClickUp is used as a coding-agent surface, not a four-tool status viewer. The
# bridge still runs under the workspace_write policy: every mutation remains
# repository-confined, audited, idempotent and subject to KaroX safety checks.
CLICKUP_DEVELOPER_TOOLS: tuple[str, ...] = (
    "karox.repo.read_file",
    "karox.repo.read_lines",
    "karox.repo.search",
    "karox.repo.list_files",
    "karox.repo.write_file",
    "karox.repo.edit_file",
    "karox.repo.command",
    "karox.git.status",
    "karox.git.diff",
    "karox.git.log",
    "karox.checks.run",
    "karox.tests.run",
    "karox.runtime.status",
)


def default_server_launcher(
    port: int,
    secret: str,
    *,
    repository: Path,
    credential_name: Optional[str] = None,
    session_dir: Optional[Path] = None,
    spawn: Optional[Callable[..., Any]] = None,
    reuse_existing_session: bool = False,
    bypass: bool = False,
) -> ServerHandle:
    """Start the real ``karox bridge serve`` child and wait for its port.

    Production seam: this is what runs when the user clicks "Connect
    automatically".  It creates a session (the bridge ``serve`` subcommand
    requires ``--session-id``), stores the generated secret in the bridge
    credential store (the server validates bearers against ``KaroX/bridge``),
    spawns the child, and waits for it to open ``port``.  ``spawn`` is a seam
    so a test can capture the argv without running a real subprocess.
    """
    import subprocess

    from .bridge import BridgeCredentialStore
    from .sessions import SessionStore

    # Lazy import to keep the launcher seam importable without the whole
    # web-bridge runtime on the path.
    from .web_bridge_launcher import (
        _child_options,
        _mirror_child_output,
        _port_is_available,
        _wait_for_bridge,
    )

    from .paths import session_dir as _session_dir

    # Re-check the port before binding.  ``_pick_free_port`` chose it from a
    # snapshot taken when the setup screen opened, and a bridge left running by
    # an earlier attempt can be holding it by now.  Without this check the
    # readiness probe below connects to *that* process, reports success, and the
    # handshake then authenticates against a stranger's secret -- surfacing as an
    # unexplained 401 that no amount of retrying fixes.  Fail with the port named
    # instead, so the remediation is obvious.
    if not _port_is_available(port):
        raise ConnectionConfigurationError(
            f"port {port} is already in use -- another KaroX bridge is most likely "
            f"still running from an earlier attempt; stop it, or set a different "
            f"port in Advanced"
        )

    # The shared Bypass mode maps onto the capability profile the session is
    # created with -- the same contract every other connection family uses.
    from types import SimpleNamespace

    from .access_mode import provider_access_profile

    session_profile = provider_access_profile(SimpleNamespace(bypass=bool(bypass)))
    sid = credential_name or f"clickup-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    store_dir = session_dir or _session_dir()
    sessions = SessionStore(store_dir)
    if reuse_existing_session:
        try:
            existing = sessions.load(sid)
        except Exception:
            existing = None
        if existing is None:
            sessions.create(
                repository,
                "ClickUp hosted bridge",
                session_profile,
                session_id=sid,
            )
        else:
            if existing.revoked:
                raise ConnectionConfigurationError(
                    "the saved ClickUp session has been revoked and cannot be restarted"
                )
            sessions.validate_repository(existing, repository)
    else:
        sessions.create(
            repository,
            "ClickUp hosted bridge",
            session_profile,
            session_id=sid,
        )
    # The bridge credential holds the secret the server will validate bearers
    # against.  ``set`` generates a fresh secret when one isn't passed, but we
    # already generated the connection secret; store that exact value so the
    # card and the server agree on one token.
    store = BridgeCredentialStore()
    store.set(sid, secret)

    argv = _bridge_argv(
        profile="generic-streamable-http",
        repository=repository,
        session_id=sid,
        port=port,
        tools=CLICKUP_DEVELOPER_TOOLS,
        verification_commands=((sys.executable, "-m", "pytest", "-q"),),
    )
    # The child's environment, mirroring ``launch_web_bridge`` exactly.
    #
    # ``PYTHONIOENCODING``/``PYTHONUTF8`` are the fix for a hard failure, not a
    # cosmetic one: this project's own repository path can contain non-ASCII
    # characters (``D:\проекты\KaroX-v5``), and ``bridge serve`` prints that path
    # on startup.  A child whose stdout is a pipe gets the host console code page
    # (cp1251/cp866 on a Russian-locale Windows install, cp1252 elsewhere), so
    # that print raises ``UnicodeEncodeError: 'charmap' codec can't encode
    # character`` and the bridge dies before it ever serves a request.  Forcing
    # UTF-8 on the child's side is the only fix that covers output whose
    # formatting this module does not control.
    #
    # ``KAROX_MCP_ALLOWED_HOSTS`` is inherited from the caller (the orchestrator
    # sets it around this call with the tunnel's hostname) so the public-URL
    # handshake is not rejected as DNS rebinding.
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"

    popen = spawn or subprocess.Popen
    process = popen(
        list(argv),
        cwd=str(repository),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_child_options(),
    )
    # Drain the pipe on a thread.  Two reasons, both load-bearing: an undrained
    # pipe fills its OS buffer and blocks the child mid-startup, and the drained
    # tail is what turns "exited with code 2" into the line that says why.
    output = _mirror_child_output(cast("subprocess.Popen[str]", process), name="clickup-bridge")
    try:
        _wait_for_bridge(
            cast("subprocess.Popen[str]", process), port, timeout_seconds=20.0, output=output
        )
    except Exception:
        _terminate(process)
        raise

    def stop_process() -> None:
        _terminate(process)

    def stop() -> None:
        stop_process()
        # A newly-created setup owns its credential until the connection is
        # saved. A restart reuses the credential already owned by the saved
        # card, so launch failure or cancellation must never delete it.
        if not reuse_existing_session:
            try:
                store.delete(sid)
            except Exception:
                pass

    return ServerHandle(
        local_endpoint=f"http://127.0.0.1:{port}/mcp",
        secret=secret,
        port=port,
        credential_name=sid,
        stop=stop,
        stop_process=stop_process,
        pid=getattr(process, "pid", None),
    )


def default_tunnel_launcher(
    tunnel_kind: str,
    port: int,
    *,
    https_port: int = 443,
) -> TunnelHandle:
    """Start the requested tunnel and return its public URL.

    Reuses ``start_cloudflare_quick_tunnel`` (ephemeral) and
    ``start_tailscale_foreground_funnel`` (stable) verbatim -- the same
    primitives ``/bridge`` uses in the TUI.  ``local`` and ``custom`` start no
    process; ``local`` returns the loopback origin so the handshake still runs.
    """
    if tunnel_kind == "local":
        return TunnelHandle(
            public_url=f"http://127.0.0.1:{port}", stop=lambda: None, pid=None
        )
    if tunnel_kind == "custom":
        raise ConnectionConfigurationError(
            "a custom tunnel URL must be supplied in Advanced; the auto flow has none"
        )
    from .web_bridge_launcher import (
        start_cloudflare_quick_tunnel,
        start_tailscale_foreground_funnel,
    )

    if tunnel_kind == "cloudflare":
        cf = start_cloudflare_quick_tunnel(port, timeout_seconds=30.0)
        return TunnelHandle(
            public_url=cf.public_url,
            stop=cf.stop,
            pid=getattr(cf.process, "pid", None),
        )
    if tunnel_kind == "tailscale":
        ts = start_tailscale_foreground_funnel(
            port, https_port=https_port, timeout_seconds=30.0
        )
        return TunnelHandle(
            public_url=ts.public_url,
            stop=ts.stop,
            pid=getattr(ts.process, "pid", None),
        )
    raise ConnectionConfigurationError(f"unknown tunnel: {tunnel_kind}")


def setup_clickup_connection(
    defaults: ClickupDefaults,
    *,
    name: str,
    repository: Path,
    supplied_secret: Optional[str] = None,
    server_launcher: Optional[Callable[..., ServerHandle]] = None,
    tunnel_launcher: Optional[Callable[[str, int], TunnelHandle]] = None,
    handshake: Optional[Callable[..., Mapping[str, Any]]] = None,
    on_progress: Optional[Callable[[str, str, Optional[str]], None]] = None,
    cancellation: Optional[Callable[[], bool]] = None,
    keep_public_pending: bool = False,
) -> ClickupSetupOutcome:
    """Run the full auto-setup for a ClickUp connection.

    Each step reports its state to ``on_progress(step, state, detail)`` where
    ``state`` is ``"running"`` / ``"ok"`` / ``"failed"``.  ``cancellation`` is
    polled between steps; a ``True`` aborts, stops anything started, removes
    the generated secret, and returns an ``cancelled`` outcome (no orphaned
    process or credential). By default the connection is saved only after the
    public handshake passes. ``keep_public_pending`` is reserved for the
    foreground ClickUp CLI: it keeps a locally verified bridge and tunnel alive
    so ClickUp can perform the authoritative external test, without labelling
    the connection publicly verified.
    """
    start_server = server_launcher or default_server_launcher
    start_tunnel = tunnel_launcher or default_tunnel_launcher
    run_handshake = handshake or _default_handshake

    def cancelled() -> bool:
        return bool(cancellation and cancellation())

    def step(step_id: str, state: str, detail: Optional[str] = None) -> None:
        if on_progress is not None:
            on_progress(step_id, state, detail)

    server: Optional[ServerHandle] = None
    tunnel: Optional[TunnelHandle] = None
    secret_value: Optional[str] = None

    try:
        # 1. Configuration (already resolved; just confirm invariants).
        step(STEP_CONFIG, "running")
        if defaults.transport != "streamable_http":
            step(STEP_CONFIG, "failed", "ClickUp currently supports Streamable HTTP MCP only")
            return ClickupSetupOutcome(
                success=False,
                failure_kind="invalid_config",
                failure_detail="ClickUp currently supports Streamable HTTP MCP only",
                remediation="reset Advanced settings to automatic",
                defaults=defaults,
            )
        if defaults.auth_scheme != "bearer":
            step(STEP_CONFIG, "failed", "ClickUp requires Authorization header bearer auth")
            return ClickupSetupOutcome(
                success=False,
                failure_kind="invalid_config",
                failure_detail="ClickUp requires Authorization header bearer auth",
                remediation="reset Advanced settings to automatic",
                defaults=defaults,
            )
        if defaults.tunnel == "custom":
            step(STEP_CONFIG, "failed", "custom tunnel setup is not available in this flow")
            return ClickupSetupOutcome(
                success=False,
                failure_kind="invalid_config",
                failure_detail="custom tunnel setup is not available in the automatic ClickUp flow",
                remediation="use tailscale, cloudflare, or local",
                defaults=defaults,
            )
        if defaults.url_stability == "stable" and defaults.tunnel != "tailscale":
            step(STEP_CONFIG, "failed", "stable URL needs tailscale/custom tunnel")
            return ClickupSetupOutcome(
                success=False,
                failure_kind="invalid_config",
                failure_detail="a stable URL requires the tailscale or custom tunnel",
                remediation="switch the tunnel or use a temporary URL in Advanced",
                defaults=defaults,
            )
        if cancelled():
            return ClickupSetupOutcome(success=False, failure_kind="cancelled", defaults=defaults)
        step(STEP_CONFIG, "ok", f"{defaults.transport} · {defaults.auth_scheme} · {defaults.tunnel}")

        # 2. Secret.  Secret material travels separately from the serializable
        # defaults object, so it cannot leak through overrides, diagnostics or
        # persisted configuration.  Generated and supplied secrets then follow
        # the same bridge-store and cleanup path.
        step(STEP_SECRET, "running")
        if defaults.secret_source == "paste":
            if not isinstance(supplied_secret, str) or not supplied_secret:
                step(STEP_SECRET, "failed", "a pasted secret is required but none was provided")
                return ClickupSetupOutcome(
                    success=False,
                    failure_kind="missing_secret",
                    failure_detail="no secret supplied",
                    remediation="enter a secret in Advanced or reset to automatic",
                    defaults=defaults,
                )
            if any(ch in supplied_secret for ch in ("\x00", "\r", "\n")):
                step(STEP_SECRET, "failed", "the supplied secret contains control characters")
                return ClickupSetupOutcome(
                    success=False,
                    failure_kind="invalid_secret",
                    failure_detail="the supplied secret contains invalid control characters",
                    remediation="enter a single-line secret",
                    defaults=defaults,
                )
            secret_value = supplied_secret
            step(STEP_SECRET, "ok", "secret accepted")
        else:
            secret_value = _generate_secret()
            step(STEP_SECRET, "ok", "secret generated")

        if cancelled():
            return ClickupSetupOutcome(success=False, failure_kind="cancelled", defaults=defaults)

        # 3. Tunnel first.  The bridge validates the ``Host`` header of every
        #    request against an allowlist (``KAROX_MCP_ALLOWED_HOSTS``); a
        #    tunnel host is not derivable from inside the listener, so it must
        #    be declared by the launcher.  Starting the tunnel first means we
        #    know its hostname before the bridge child is spawned, and we pass
        #    that hostname through the environment so the handshake over the
        #    public URL is not rejected as DNS rebinding (421 host_not_allowed).
        step(STEP_TUNNEL, "running")
        try:
            tunnel = start_tunnel(defaults.tunnel, defaults.port)
        except Exception as exc:
            step(STEP_TUNNEL, "failed", str(exc))
            remediation = _tunnel_remediation(defaults, exc)
            return ClickupSetupOutcome(
                success=False,
                failure_kind="tunnel_failed",
                failure_detail=str(exc),
                remediation=remediation,
                defaults=defaults,
            )
        step(STEP_TUNNEL, "ok", defaults.tunnel_reason)

        if cancelled():
            _cleanup(None, tunnel)
            return ClickupSetupOutcome(success=False, failure_kind="cancelled", defaults=defaults)

        # 4. MCP server.  Start the local bridge with the tunnel's hostname in
        #    the allowlist env var so the public-URL handshake is accepted.
        step(STEP_SERVER, "running")
        from urllib.parse import urlsplit

        tunnel_host = urlsplit(tunnel.public_url).hostname or ""
        # ``patch.dict`` sets the env var for the duration of the bridge spawn
        # so the child inherits the allowlist; it restores after.  When the
        # tunnel is local (loopback) the host is already allowed, so skip.
        env_patch = (
            patch.dict(os.environ, {"KAROX_MCP_ALLOWED_HOSTS": tunnel_host}, clear=False)
            if tunnel_host
            else nullcontext()
        )
        try:
            with env_patch:
                server = start_server(
                    defaults.port,
                    secret_value,
                    repository=repository,
                    credential_name=None,
                )
        except Exception as exc:  # pragma: no cover - launch errors are environment-specific
            step(STEP_SERVER, "failed", str(exc))
            _cleanup(None, tunnel)
            # A taken port is the one launch failure with a precise remedy, and
            # it is the common one: a bridge left running by an earlier attempt.
            # Naming it separately keeps the user from being sent to a log that
            # only repeats the sentence they already have.
            port_taken = "already in use" in str(exc).lower()
            return ClickupSetupOutcome(
                success=False,
                failure_kind="port_in_use" if port_taken else "server_failed",
                failure_detail=str(exc),
                remediation="change_port" if port_taken else "open_log",
                defaults=defaults,
            )
        step(STEP_SERVER, "ok", server.local_endpoint)

        if cancelled():
            _cleanup(server, tunnel)
            return ClickupSetupOutcome(success=False, failure_kind="cancelled", defaults=defaults)

        # 5. Public URL.  The MCP endpoint is the tunnel origin + /mcp.
        step(STEP_URL, "running")
        public_endpoint = tunnel.public_url.rstrip("/") + defaults.endpoint_path
        step(STEP_URL, "ok", public_endpoint)

        if cancelled():
            _cleanup(server, tunnel)
            return ClickupSetupOutcome(success=False, failure_kind="cancelled", defaults=defaults)

        # 6. Handshake. Real public initialize + tools/list. A fresh Cloudflare
        #    hostname gets a bounded propagation window. Loopback is diagnostic
        #    only and cannot make setup ready for ClickUp.
        step(STEP_HANDSHAKE, "running")
        handshake_target = _handshake_target(defaults, public_endpoint)
        result = run_resilient_handshake(
            handshake_target,
            public_endpoint=public_endpoint,
            local_endpoint=server.local_endpoint,
            secret=secret_value,
            run_handshake=run_handshake,
            public_retry_seconds=(
                45.0 if handshake is None and defaults.tunnel == "cloudflare" else 0.0
            ),
        )
        public_pending = result.get("state") == "public_pending"
        if result.get("state") != "ok" and not (
            keep_public_pending and public_pending
        ):
            step(STEP_HANDSHAKE, "failed", str(result.get("detail", "handshake failed")))
            _cleanup(server, tunnel)
            failure_kind = (
                "public_unreachable"
                if public_pending
                else result.get("failure_kind", "handshake_failed")
            )
            return ClickupSetupOutcome(
                success=False,
                failure_kind=failure_kind,
                failure_detail=str(result.get("detail", "")),
                remediation=(
                    "retry"
                    if failure_kind == "public_unreachable"
                    else _handshake_remediation(result)
                ),
                defaults=defaults,
            )
        step(
            STEP_HANDSHAKE,
            "pending" if public_pending else "ok",
            str(result.get("detail", "")),
        )

        # 7. Save after a successful public handshake, or keep an explicitly
        #    requested foreground public-pending bridge alive for ClickUp's own
        #    external connection test. The default remains strict.
        target = _save_target(
            defaults,
            name=name,
            public_url=tunnel.public_url,
            server=server,
            handshake=result,
        )
        # The saved connection now owns the credential. Runtime ownership moves
        # into the connection runtime manager, not the result-card closure, so
        # the normal Connections list can still inspect and stop it after the
        # card closes.
        if server is not None:
            server.stop = _process_only_stop(server)
        live_stop = _live_stop(server, tunnel)
        try:
            from .connection_runtime import connection_runtime_manager

            manager = connection_runtime_manager()
            runtime = manager.register(
                connection_id=target.connection_id,
                session_id=server.credential_name,
                tunnel=defaults.tunnel,
                local_endpoint=server.local_endpoint,
                public_endpoint=public_endpoint,
                bridge_pid=server.pid,
                tunnel_pid=tunnel.pid if tunnel is not None else None,
                stop=live_stop,
            )
        except Exception:
            # A connection that cannot be managed must not be left live or saved
            # as ready. Stop both processes and delete the saved metadata and
            # credential through the shared removal path.
            live_stop()
            try:
                from .connections import remove_connection

                remove_connection(target.connection_id)
            except Exception:
                pass
            raise

        def managed_stop() -> None:
            manager.stop(target.connection_id)

        return ClickupSetupOutcome(
            success=True,
            target=target,
            public_endpoint=public_endpoint,
            local_endpoint=server.local_endpoint,
            handshake=result,
            defaults=defaults,
            stop=managed_stop,
            runtime_id=runtime.runtime_id,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _cleanup(server, tunnel)
        return ClickupSetupOutcome(
            success=False,
            failure_kind="unexpected",
            failure_detail=str(exc),
            remediation="retry",
            defaults=defaults,
        )


def _saved_tailscale_https_candidates(target: McpClientTarget) -> tuple[int, ...]:
    """Return safe public Funnel listeners for a saved managed MCP target.

    ChatGPT/Claude own the conventional HTTPS 443 listener.  Generic saved MCP
    clients prefer the other Tailscale Funnel ports so they can run beside that
    bridge.  Once a public URL has been saved, its explicit listener is pinned on
    restart: silently moving a configured remote client to another port would be
    worse than a clear route-in-use failure.
    """

    if target.public_url:
        from urllib.parse import urlsplit

        parsed = urlsplit(target.public_url)
        return (parsed.port or 443,)
    if target.preset_id == "clickup":
        # Preserve the historical ClickUp behavior for existing setup flows.
        return (443,)
    preferred = 10000 if target.port != 8767 else 8443
    ordered = (preferred, 8443, 10000, 443)
    return tuple(dict.fromkeys(ordered))


def _start_saved_target_tunnel(
    target: McpClientTarget,
    tunnel_launcher: Optional[Callable[[str, int], TunnelHandle]],
) -> TunnelHandle:
    """Start only the tunnel owned by ``target``, preserving sibling bridges."""

    if tunnel_launcher is not None:
        # Test/custom seams keep their long-standing two-argument contract.
        return tunnel_launcher(target.tunnel, target.port)
    if target.tunnel != "tailscale":
        return default_tunnel_launcher(target.tunnel, target.port)

    last_error: Optional[Exception] = None
    candidates = _saved_tailscale_https_candidates(target)
    for index, https_port in enumerate(candidates):
        try:
            return default_tunnel_launcher(
                target.tunnel, target.port, https_port=https_port
            )
        except Exception as exc:
            last_error = exc
            detail = str(exc).lower()
            conflict = "route_in_use" in detail or "https_port_in_use" in detail
            # A saved URL is pinned to one listener.  Fallback is only for first
            # launch, where no remote client has been configured yet.
            if target.public_url or not conflict or index == len(candidates) - 1:
                raise
    assert last_error is not None
    raise last_error


def start_saved_clickup_connection(
    target: McpClientTarget,
    *,
    server_launcher: Optional[Callable[..., ServerHandle]] = None,
    tunnel_launcher: Optional[Callable[[str, int], TunnelHandle]] = None,
    handshake: Optional[Callable[..., Mapping[str, Any]]] = None,
    on_progress: Optional[Callable[[str, str, Optional[str]], None]] = None,
    cancellation: Optional[Callable[[], bool]] = None,
) -> ClickupSetupOutcome:
    """Start/restart one saved repository-bound Streamable HTTP bearer bridge.

    ClickUp, Generic MCP, Custom MCP, Web Agent and IDE presets share this exact
    guarded runtime path.  The saved bridge credential and repository-bound
    session are reused; no new connection ID or secret is created.  A temporary
    tunnel may produce a new public URL, which is persisted only after the real
    MCP handshake succeeds.
    """
    from dataclasses import replace

    from .connections import (
        _BRIDGE_REFERENCE_PREFIX,
        connection_registry,
        resolve_connection_secret,
    )
    from .connection_runtime import (
        ConnectionRuntimeError,
        connection_runtime_manager,
    )
    from .paths import session_dir as _session_dir
    from .sessions import SessionError, SessionStore

    def step(step_id: str, state: str, detail: Optional[str] = None) -> None:
        if on_progress is not None:
            on_progress(step_id, state, detail)

    def cancelled() -> bool:
        return bool(cancellation and cancellation())

    manager = connection_runtime_manager()
    current = manager.status(target.connection_id)
    if current["state"] in {"running", "degraded"}:
        return ClickupSetupOutcome(
            success=False,
            target=target,
            failure_kind="already_running",
            failure_detail="the saved connection is already running",
            remediation="stop it before restarting",
        )
    if current["state"] == "unmanaged_running":
        return ClickupSetupOutcome(
            success=False,
            target=target,
            failure_kind="unmanaged_running",
            failure_detail=(
                "a live runtime exists but is not owned by this KaroX process; "
                "it cannot be adopted or killed by PID alone"
            ),
            remediation="stop it from the KaroX process that owns it",
        )
    if not is_managed_streamable_bearer_target(target):
        return ClickupSetupOutcome(
            success=False,
            target=target,
            failure_kind="unsupported_preset",
            failure_detail=(
                "this saved connection is not a managed Streamable HTTP + Bearer "
                "KaroX bridge target"
            ),
        )
    credential_ref = target.credential_ref or ""
    if not credential_ref.startswith(_BRIDGE_REFERENCE_PREFIX):
        return ClickupSetupOutcome(
            success=False,
            target=target,
            failure_kind="invalid_credential",
            failure_detail="the saved MCP connection has no bridge credential",
            remediation="recreate the connection",
        )
    session_id = credential_ref[len(_BRIDGE_REFERENCE_PREFIX) :]
    try:
        sessions = SessionStore(_session_dir())
        session = sessions.load(session_id)
        if session.revoked:
            raise SessionError("session is revoked")
        repository = Path(session.repository).expanduser().resolve(strict=True)
        sessions.validate_repository(session, repository)
        secret = resolve_connection_secret(target)
    except Exception as exc:
        return ClickupSetupOutcome(
            success=False,
            target=target,
            failure_kind="saved_state_unavailable",
            failure_detail=f"cannot load saved MCP bridge state: {type(exc).__name__}",
            remediation="recreate the connection",
        )

    defaults = ClickupDefaults(
        transport=target.transport,
        auth_scheme=target.auth_scheme,
        tunnel=target.tunnel,
        port=target.port,
        endpoint_path=target.endpoint_path,
        url_stability=target.url_stability,
        secret_source="paste",
        tunnel_reason="saved connection restart",
        tunnel_action="none",
    )
    start_server = server_launcher or default_server_launcher
    run_handshake = handshake or _default_handshake
    server: Optional[ServerHandle] = None
    tunnel: Optional[TunnelHandle] = None

    try:
        step(STEP_CONFIG, "ok", "saved configuration loaded")
        if cancelled():
            return ClickupSetupOutcome(
                success=False,
                target=target,
                failure_kind="cancelled",
                defaults=defaults,
            )

        if server_launcher is None:
            from .web_bridge_launcher import _port_is_available

            if not _port_is_available(defaults.port):
                return ClickupSetupOutcome(
                    success=False,
                    target=target,
                    failure_kind="port_in_use",
                    failure_detail=f"local port {defaults.port} is already in use",
                    remediation="choose a different local port and retry",
                    defaults=defaults,
                )

        step(STEP_TUNNEL, "running")
        tunnel = _start_saved_target_tunnel(target, tunnel_launcher)
        step(STEP_TUNNEL, "ok", tunnel.public_url)
        if cancelled():
            _cleanup(None, tunnel)
            return ClickupSetupOutcome(
                success=False,
                target=target,
                failure_kind="cancelled",
                defaults=defaults,
            )

        from urllib.parse import urlsplit

        tunnel_host = urlsplit(tunnel.public_url).hostname or ""
        env_patch = (
            patch.dict(os.environ, {"KAROX_MCP_ALLOWED_HOSTS": tunnel_host}, clear=False)
            if tunnel_host
            else nullcontext()
        )
        step(STEP_SERVER, "running")
        with env_patch:
            server = start_server(
                defaults.port,
                secret,
                repository=repository,
                credential_name=session_id,
                reuse_existing_session=True,
            )
        step(STEP_SERVER, "ok", server.local_endpoint)
        if cancelled():
            _cleanup(server, tunnel)
            return ClickupSetupOutcome(
                success=False,
                target=target,
                failure_kind="cancelled",
                defaults=defaults,
            )

        public_endpoint = tunnel.public_url.rstrip("/") + defaults.endpoint_path
        step(STEP_URL, "ok", public_endpoint)
        step(STEP_HANDSHAKE, "running")
        result = run_resilient_handshake(
            target,
            public_endpoint=public_endpoint,
            local_endpoint=server.local_endpoint,
            secret=secret,
            run_handshake=run_handshake,
            public_retry_seconds=(
                45.0 if handshake is None and defaults.tunnel == "cloudflare" else 0.0
            ),
        )
        if result.get("state") != "ok":
            step(STEP_HANDSHAKE, "failed", str(result.get("detail", "")))
            _cleanup(server, tunnel)
            failure_kind = (
                "public_unreachable"
                if result.get("state") == "public_pending"
                else result.get("failure_kind", "handshake_failed")
            )
            return ClickupSetupOutcome(
                success=False,
                target=target,
                failure_kind=failure_kind,
                failure_detail=str(result.get("detail", "")),
                remediation=(
                    "retry"
                    if failure_kind == "public_unreachable"
                    else _handshake_remediation(result)
                ),
                defaults=defaults,
            )
        step(STEP_HANDSHAKE, "ok", str(result.get("detail", "")))

        updated = replace(
            target,
            public_url=tunnel.public_url,
            updated_at=time.time(),
        )
        connection_registry().put(updated)
        live_stop = _live_stop(server, tunnel)
        runtime = manager.register(
            connection_id=updated.connection_id,
            session_id=session_id,
            tunnel=updated.tunnel,
            local_endpoint=server.local_endpoint,
            public_endpoint=public_endpoint,
            bridge_pid=server.pid,
            tunnel_pid=tunnel.pid,
            stop=live_stop,
        )

        def managed_stop() -> None:
            manager.stop(updated.connection_id)

        return ClickupSetupOutcome(
            success=True,
            target=updated,
            public_endpoint=public_endpoint,
            local_endpoint=server.local_endpoint,
            handshake=result,
            defaults=defaults,
            stop=managed_stop,
            runtime_id=runtime.runtime_id,
        )
    except ConnectionRuntimeError as exc:
        _cleanup(server, tunnel)
        try:
            connection_registry().put(target)
        except Exception:
            pass
        return ClickupSetupOutcome(
            success=False,
            target=target,
            failure_kind="runtime_failed",
            failure_detail=str(exc),
            remediation="retry",
            defaults=defaults,
        )
    except Exception as exc:
        _cleanup(server, tunnel)
        return ClickupSetupOutcome(
            success=False,
            target=target,
            failure_kind="restart_failed",
            failure_detail=f"{type(exc).__name__}: {exc}",
            remediation="retry",
            defaults=defaults,
        )


# Public generic name for the controller and new callers.  Keep the historical
# ClickUp function as the compatibility entry point used by older tests/code.
start_saved_mcp_connection = start_saved_clickup_connection


def _generate_secret() -> str:
    """Generate a URL-safe bearer token, reusing the bridge generator."""
    import secrets

    return secrets.token_urlsafe(32)


def _default_handshake(target: McpClientTarget, *, endpoint_url: str, secret: str, timeout_seconds: float = 15.0) -> Mapping[str, Any]:
    """Run the real MCP handshake via the shared connection-test path."""
    from .connection_tests import test_mcp_client_target

    return test_mcp_client_target(
        target,
        endpoint_url=endpoint_url,
        secret=secret,
        timeout_seconds=timeout_seconds,
    )


# Failure kinds that may be retried while a fresh public hostname propagates.
# After retries, loopback may diagnose the local bridge, but it never proves the
# public URL ClickUp must call and therefore never makes setup successful.
#
# ``unauthorized`` / ``host_not_allowed`` are deliberately absent: those are
# verdicts about the credential or the ``Host`` header, so a loopback retry
# would either repeat them or -- worse for ``unauthorized`` -- hide a real
# secret mismatch behind a local success.
_LOCAL_FALLBACK_KINDS = frozenset(
    {"dns_failure", "network", "timeout", "handshake_error", "mcp_error"}
)


def run_resilient_handshake(
    target: McpClientTarget,
    *,
    public_endpoint: str,
    local_endpoint: Optional[str],
    secret: str,
    run_handshake: Optional[Callable[..., Mapping[str, Any]]] = None,
    public_retry_seconds: float = 0.0,
    retry_interval_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Mapping[str, Any]:
    """Verify the public endpoint, then use loopback only for diagnosis.

    Reachability failures may be retried for a bounded interval while a fresh
    Cloudflare hostname propagates.  A local success proves the bridge,
    credential and tool catalogue but returns ``public_pending``; callers must
    not save or advertise that state as a working ClickUp connector.  Auth and
    host-policy failures are never hidden by a local retry.
    """
    run_handshake = run_handshake or _default_handshake
    deadline = time.monotonic() + max(0.0, public_retry_seconds)
    while True:
        try:
            result = run_handshake(
                target,
                endpoint_url=public_endpoint,
                secret=secret,
                timeout_seconds=15.0,
            )
        except Exception as exc:
            result = {
                "state": "failed",
                "failure_kind": "handshake_error",
                "detail": str(exc),
            }
        if result.get("state") == "ok":
            return result
        if result.get("failure_kind") not in _LOCAL_FALLBACK_KINDS:
            return result
        if time.monotonic() >= deadline:
            break
        sleep(max(0.0, retry_interval_seconds))

    # Loopback is diagnostic evidence only. It may prove the bridge, credential
    # and tool catalogue, but cannot prove the public URL ClickUp must call.
    if not local_endpoint:
        return result
    try:
        local_result = run_handshake(
            target, endpoint_url=local_endpoint, secret=secret, timeout_seconds=15.0
        )
    except Exception:
        # Loopback also raised; the public verdict is the more user-actionable
        # one (it names the URL the user will actually paste), so return it.
        return result
    if local_result.get("state") == "ok":
        # Loopback proves the bridge, credential and tool catalogue, but it does
        # not prove that the public tunnel is reachable by ClickUp.
        return {
            **local_result,
            "state": "public_pending",
            "local_state": "ok",
            "public_state": "failed",
            "public_failure_kind": result.get("failure_kind", "network"),
            "detail": (
                str(local_result.get("detail", ""))
                + " — local bridge verified; public URL verification is pending"
            ).strip(),
        }
    return result



def _handshake_target(defaults: ClickupDefaults, public_endpoint: str) -> McpClientTarget:
    """Build a throwaway target for the handshake (not yet saved).

    The handshake only reads the target's ``auth_scheme``/``transport`` and the
    ``secret`` argument -- it does not resolve ``credential_ref`` -- but
    :class:`McpClientTarget` validates that an authenticated scheme carries a
    reference.  Use a dummy bridge reference so the throwaway passes validation
    without implying a real stored secret.
    """
    from .connections import _new_connection_id, _BRIDGE_REFERENCE_PREFIX

    return McpClientTarget(
        connection_id=_new_connection_id(),
        name="clickup-handshake",
        preset_id="clickup",
        transport=defaults.transport,
        endpoint_path=defaults.endpoint_path,
        auth_scheme=defaults.auth_scheme,
        tunnel=defaults.tunnel,
        runtime_profile="generic-streamable-http",
        url_stability=defaults.url_stability,
        public_url=None,
        header_name="",
        header_prefix="",
        description="",
        instructions="",
        credential_ref=f"{_BRIDGE_REFERENCE_PREFIX}clickup-handshake",
        credential_fingerprint=None,
        port=defaults.port,
        created_at=time.time(),
        updated_at=time.time(),
    )


def _save_target(
    defaults: ClickupDefaults,
    *,
    name: str,
    public_url: str,
    server: ServerHandle,
    handshake: Mapping[str, Any],
) -> McpClientTarget:
    """Persist the successful connection, referencing the bridge credential."""
    from .connections import _new_connection_id, _BRIDGE_REFERENCE_PREFIX, mcp_client_preset

    ref = f"{_BRIDGE_REFERENCE_PREFIX}{server.credential_name}"
    target = McpClientTarget(
        connection_id=_new_connection_id(),
        name=name,
        preset_id="clickup",
        transport=defaults.transport,
        endpoint_path=defaults.endpoint_path,
        auth_scheme=defaults.auth_scheme,
        tunnel=defaults.tunnel,
        runtime_profile="generic-streamable-http",
        url_stability=defaults.url_stability,
        public_url=public_url,
        header_name="",
        header_prefix="",
        description=mcp_client_preset("clickup").description,
        instructions=_clickup_instructions(defaults),
        credential_ref=ref,
        credential_fingerprint=None,
        port=defaults.port,
        created_at=time.time(),
        updated_at=time.time(),
    )
    connection_registry().put(target)
    return target


def _clickup_instructions(defaults: ClickupDefaults) -> str:
    """Ready-to-paste steps using ClickUp's exact current field labels."""
    temporary_note = (
        "\n9. This is a temporary Cloudflare URL. After restarting the bridge, "
        "create a new ClickUp connector with the new URL and secret."
        if defaults.url_stability == "temporary" and defaults.tunnel == "cloudflare"
        else ""
    )
    return (
        "1. Open ClickUp App Center.\n"
        "2. Open MCP Servers and choose Connect an MCP Server.\n"
        "3. Paste the generated KaroX URL ending in /mcp.\n"
        "4. In Authentication Method choose Authorization header (not OAuth).\n"
        "5. Paste the secret from KaroX; the token may be entered with or without "
        "the Bearer prefix.\n"
        "6. Save the connection.\n"
        "7. Run ClickUp's connection test.\n"
        "8. Keep KaroX and the bridge running while ClickUp uses the tools."
        + temporary_note
    )


def _tunnel_remediation(defaults: ClickupDefaults, exc: Exception) -> str:
    """Pick the one-button action for a tunnel failure."""
    msg = str(exc).lower()
    if "cloudflared" in msg or defaults.tunnel == "cloudflare":
        return "install_cloudflared"
    if "tailscale" in msg or "login" in msg:
        return "login_tailscale"
    return "retry"


def _handshake_remediation(result: Mapping[str, Any]) -> str:
    kind = result.get("failure_kind", "")
    if kind in {"unauthorized", "invalid_key"}:
        return "recreate_secret"
    if kind in {"timeout", "network", "dns_failure", "public_unreachable"}:
        return "retry"
    return "retry"


def _cleanup(server: Optional[ServerHandle], tunnel: Optional[TunnelHandle]) -> None:
    """Stop any started process and remove the generated secret (no orphans)."""
    if tunnel is not None:
        try:
            tunnel.stop()
        except Exception:
            pass
    if server is not None:
        try:
            server.stop()
        except Exception:
            pass


def _process_only_stop(server: ServerHandle) -> Callable[[], None]:
    """A stop() that terminates the server process but keeps the credential.

    Once the connection is saved the card owns the bridge credential, so
    stopping the live server must not delete the secret the card references.

    The returned callable is *not* invoked on the success path -- ClickUp needs
    the bridge listening -- it is what a later explicit "stop" uses.  It
    delegates to the handle's ``stop_process``, which the real launcher sets to
    a group-aware terminator; previously this returned an unconditional no-op,
    so an explicit stop left the child alive holding its port, and the next
    attempt's readiness probe then connected to that survivor and authenticated
    against its unrelated secret (an unexplained 401).
    """
    terminate = server.stop_process

    def stop() -> None:
        if terminate is not None:
            terminate()

    return stop


def _live_stop(
    server: Optional[ServerHandle], tunnel: Optional[TunnelHandle]
) -> Callable[[], None]:
    """Build the shutdown for a *saved* connection's live processes.

    Stops the tunnel and the bridge child while deliberately keeping the bridge
    credential, because the saved card references it.  This is what the outcome
    hands to the caller: without it the tunnel and the bridge stayed running with
    no handle to reach them, so every completed setup leaked a listener that kept
    holding its port -- and the next attempt's readiness probe adopted that
    survivor and authenticated against its unrelated secret.
    """
    process_stop = _process_only_stop(server) if server is not None else None

    def stop() -> None:
        if tunnel is not None:
            try:
                tunnel.stop()
            except Exception:
                pass
        if process_stop is not None:
            process_stop()

    return stop


def _terminate(process: Any) -> None:
    """Best-effort stop a bridge child and the whole tree it started.

    Delegates to the web-bridge launcher's group-aware stop, because
    :func:`_child_options` puts the child in its own process group: a bare
    ``terminate()`` then signals only the group leader and leaves anything it
    spawned holding the port.  Falls back to a plain terminate if the launcher
    module is unavailable, and swallows OS errors either way -- a stop that
    fails must not mask the error that prompted it.
    """
    try:
        from .web_bridge_launcher import _stop_process

        _stop_process(cast("Any", process))
        return
    except Exception:
        pass
    try:
        process.terminate()
    except Exception:
        pass


__all__ = [
    "ServerHandle",
    "TunnelHandle",
    "ClickupSetupOutcome",
    "CLICKUP_DEVELOPER_TOOLS",
    "setup_clickup_connection",
    "start_saved_clickup_connection",
    "run_resilient_handshake",
    "default_server_launcher",
    "default_tunnel_launcher",
    "STEP_CONFIG",
    "STEP_SECRET",
    "STEP_SERVER",
    "STEP_TUNNEL",
    "STEP_URL",
    "STEP_HANDSHAKE",
]
