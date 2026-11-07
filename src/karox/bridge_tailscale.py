"""Tailscale readiness, route ownership, and stable Funnel lifecycle.

KaroX publishes a managed bridge on the device's stable ``*.ts.net`` hostname.
The public route is daemon-managed (``tailscale funnel --bg``), so a restart of
the bridge owner can reuse the same public origin instead of reissuing a new
URL. Every mutation here first proves which individual route KaroX owns and
refuses to touch a foreign one.

``find_tailscale``, ``query_tailscale_status``, ``prepare_tailscale_funnel``,
``start_tailscale_funnel`` and ``stop_tailscale_funnel`` are the public surface;
the helpers keep the ownership checks testable without launching Tailscale.
"""

from __future__ import annotations

from .bridge_tailscale_status import (  # noqa: F401
    _TS_URL as _TS_URL,
    _ENABLE_URL as _ENABLE_URL,
    TailscaleError as TailscaleError,
    find_tailscale as find_tailscale,
    _mapping as _mapping,
    _text as _text,
    parse_tailscale_status as parse_tailscale_status,
    query_tailscale_status as query_tailscale_status,
    _serve_payload_has_routes as _serve_payload_has_routes,
    query_funnel_ownership as query_funnel_ownership,
    classify_funnel_failure as classify_funnel_failure,
    tailscale_doctor as tailscale_doctor,
)
from .bridge_tailscale_gui import (  # noqa: F401
    TailscaleLaunchPlan as TailscaleLaunchPlan,
    _TAILSCALE_AUTOFIX_CODES as _TAILSCALE_AUTOFIX_CODES,
    _tailscale_auto_up_disabled as _tailscale_auto_up_disabled,
    find_tailscale_gui as find_tailscale_gui,
    _same_windows_session as _same_windows_session,
    tailscale_gui_pids as tailscale_gui_pids,
    launch_tailscale_gui as launch_tailscale_gui,
    _tailscale_gui_launch_disabled as _tailscale_gui_launch_disabled,
    _bring_tailscale_up as _bring_tailscale_up,
    _tailscale_service_restart_disabled as _tailscale_service_restart_disabled,
    _is_windows_admin as _is_windows_admin,
    _elevated_windows_service_restart_argv as _elevated_windows_service_restart_argv,
    tailscale_service_fix_hint as tailscale_service_fix_hint,
    restart_tailscale_service as restart_tailscale_service,
    _run_sc as _run_sc,
    ensure_tailscale_ready as ensure_tailscale_ready,
    _poll_tailscale_ready as _poll_tailscale_ready,
)
from .bridge_tailscale_funnel import (  # noqa: F401
    funnel_status as funnel_status,
    _normalize_funnel_mount_path as _normalize_funnel_mount_path,
    tailscale_funnel_argv as tailscale_funnel_argv,
    prepare_tailscale_funnel as prepare_tailscale_funnel,
    inventory_tailscale_routes as inventory_tailscale_routes,
    TailscaleFunnel as TailscaleFunnel,
    background_funnel_argv as background_funnel_argv,
    _matching_background_funnel_routes as _matching_background_funnel_routes,
    _run_kwargs as _run_kwargs,
    _apply_background_funnel as _apply_background_funnel,
    find_owned_funnel as find_owned_funnel,
    start_tailscale_funnel as start_tailscale_funnel,
    ensure_tailscale_funnel as ensure_tailscale_funnel,
    refresh_tailscale_funnel as refresh_tailscale_funnel,
    retarget_tailscale_funnel as retarget_tailscale_funnel,
    stop_tailscale_funnel as stop_tailscale_funnel,
    stop_tailscale_route_for_port as stop_tailscale_route_for_port,
    TailscaleBackgroundFunnel as TailscaleBackgroundFunnel,
    start_tailscale_background_funnel as start_tailscale_background_funnel,
    refresh_tailscale_background_funnel as refresh_tailscale_background_funnel,
    retarget_tailscale_background_funnel as retarget_tailscale_background_funnel,
    stop_tailscale_background_funnel as stop_tailscale_background_funnel,
)































# Tailscale backend states an unattended ``tailscale up`` can recover without a
# human in a browser: the daemon is still booting (NoState/starting/stopped), or
# it is up but lost its MagicDNS name. ``login_required`` and
# ``machine_approval_required`` need a person (a browser login or an admin's
# approval), so those are reported with their auth URL rather than blocked on.


































# --------------------------------------------------------------------------- #
# Stable daemon-owned Funnel lifecycle                                         #
# --------------------------------------------------------------------------- #


# Imports are local so a host without the route module never pays for it during
# a plain status query, and tests can monkeypatch route inventory cleanly.




























# KaroX 5 spellings retained for callers that still use the background-funnel
# names. The implementation is the same daemon-owned route.


__all__ = [
    "TailscaleBackgroundFunnel",
    "TailscaleError",
    "TailscaleFunnel",
    "TailscaleLaunchPlan",
    "background_funnel_argv",
    "classify_funnel_failure",
    "ensure_tailscale_funnel",
    "ensure_tailscale_ready",
    "find_owned_funnel",
    "find_tailscale",
    "find_tailscale_gui",
    "funnel_status",
    "launch_tailscale_gui",
    "parse_tailscale_status",
    "prepare_tailscale_funnel",
    "query_funnel_ownership",
    "query_tailscale_status",
    "refresh_tailscale_background_funnel",
    "refresh_tailscale_funnel",
    "restart_tailscale_service",
    "retarget_tailscale_background_funnel",
    "retarget_tailscale_funnel",
    "start_tailscale_background_funnel",
    "start_tailscale_funnel",
    "stop_tailscale_background_funnel",
    "stop_tailscale_funnel",
    "stop_tailscale_route_for_port",
    "tailscale_doctor",
    "tailscale_funnel_argv",
    "tailscale_gui_pids",
    "tailscale_service_fix_hint",
]