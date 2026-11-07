"""Hosted-bridge command group for the KaroX command surface.

The lifecycle/configuration implementation remains in ``bridge_*`` and
``hosted_core``; this module owns only argument-to-call adaptation and output.
It is intentionally separate from ``cli.py`` so the command surface does not
carry the bridge lifecycle in the same module as the agent runtime.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Optional, Sequence

from .bridge_profiles import WEB_BRIDGE_PROFILES
from .cli_common import emit as _emit
from .cli_common import verification_argv as _verification_argv
from .cli_common import write as _write
from .models import AccessProfile

# The read-only catalogue is the launcher's default. Write mode adds only the
# concrete mutations a hosted coding session needs; push/publish/auth remain
# outside every bridge profile.
_BRIDGE_WRITE_TOOLS = (
    "karox.repo.write_file",
    "karox.repo.edit_file",
    "karox.repo.command",
    "karox.checks.run",
    "karox.checks.start",
    "karox.checks.status",
    "karox.checks.logs",
    "karox.checks.cancel",
    "karox.tests.run",
    "karox.git.commit",
)
# Developer commands need Full developer access (the elevated profile); a
# workspace-write session would only ever see them refused, so they are not
# advertised there.
_BRIDGE_DEVELOPER_TOOLS = (
    "karox.command.run",
    "karox.command.start",
    "karox.command.status",
    "karox.command.logs",
    "karox.command.cancel",
)
_BRIDGE_CHECK_TOOLS = frozenset(
    {
        "karox.checks.run",
        "karox.checks.start",
        "karox.checks.status",
        "karox.checks.logs",
        "karox.checks.cancel",
    }
)


def _bridge_access_profile(args: argparse.Namespace) -> AccessProfile:
    requested = getattr(args, "access_profile", None)
    if requested:
        return AccessProfile(requested)
    return AccessProfile.WORKSPACE_WRITE if args.write else AccessProfile.READ_ONLY


def _bridge_tools(
    selected: Sequence[str],
    *,
    write: bool,
    verification: Sequence[Sequence[str]],
    access_profile: Optional[AccessProfile] = None,
) -> tuple[str, ...]:
    from .bridge_launcher import DEFAULT_BRIDGE_TOOLS, core_bridge_tool_names

    tools = list(selected) or list(DEFAULT_BRIDGE_TOOLS)
    known = core_bridge_tool_names()
    elevated = access_profile == AccessProfile.ELEVATED
    if write or elevated:
        tools.extend(name for name in _BRIDGE_WRITE_TOOLS if name in known)
    if elevated:
        tools.extend(name for name in _BRIDGE_DEVELOPER_TOOLS if name in known)
    if not verification:
        tools = [name for name in tools if name not in _BRIDGE_CHECK_TOOLS]
    return tuple(dict.fromkeys(tools))


def _bridge_verification(args: argparse.Namespace) -> tuple[tuple[str, ...], ...]:
    return tuple(_verification_argv(item) for item in getattr(args, "verification_command", ()))


def _copy_secret_to_clipboard(secret: str) -> None:
    from .detached_process import windowless_flags

    commands: list[list[str]] = []
    if os.name == "nt":
        commands.append(["clip"])
    elif sys.platform == "darwin":
        commands.append(["pbcopy"])
    else:
        commands.append(["wl-copy"])
        commands.append(["xclip", "-selection", "clipboard"])
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                input=secret,
                text=True,
                capture_output=True,
                timeout=10.0,
                creationflags=windowless_flags() if os.name == "nt" else 0,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0:
            return
    raise ValueError("no clipboard command is available; use --json and the OS UI")


def _bridge_session_id_for(args: argparse.Namespace) -> str:
    from .bridge_profiles import (
        BridgeCredentialStore,
        saved_web_bridge_session_candidates,
    )

    if getattr(args, "saved", None):
        candidates = saved_web_bridge_session_candidates(args.saved)
        # A v5 profile may hold its approval password under the old target-profile
        # digest. Read the keyring by candidate identity; keep the current identity
        # when every candidate is absent so the existing missing-credential error
        # remains truthful.
        store = BridgeCredentialStore()
        for candidate in candidates:
            try:
                status = store.status(f"os-keyring:bridge/{candidate}")
            except Exception:
                continue
            if status.get("status") == "available":
                return candidate
        return candidates[0]
    session_id = getattr(args, "session_id", None)
    if not session_id:
        raise ValueError("--saved or --session-id is required")
    return str(session_id)


def handle_bridge(args: argparse.Namespace) -> int:
    from .bridge_profiles import BridgeProfileStore
    from .bridge_profiles import BridgeRegistry

    command = args.bridge_command
    if command == "list":
        _emit([item.to_dict() for item in BridgeRegistry().list()], json_output=args.json)
        return 0
    if command == "show":
        _emit(BridgeRegistry().get(args.profile).to_dict(), json_output=args.json)
        return 0
    if command == "doctor":
        payload = BridgeProfileStore().doctor()
        _emit(payload, json_output=args.json)
        return 0 if payload.get("status") in {None, "ok"} else 1
    if command == "saved":
        store = BridgeProfileStore()
        sub = args.bridge_saved_command
        if sub == "list":
            _emit([item.to_dict() for item in store.list()], json_output=args.json)
            return 0
        if sub == "show":
            _emit(store.get(args.name).to_dict(), json_output=args.json)
            return 0
        if sub == "delete":
            removed = store.delete(args.name)
            _emit(
                {"name": removed.name, "status": "deleted"},
                json_output=args.json,
            )
            return 0
        if sub == "validate":
            from .bridge_launcher import bridge_diagnostics, config_from_saved_profile

            config = config_from_saved_profile(args.name)
            _emit(bridge_diagnostics(config), json_output=args.json)
            return 0
        if sub != "create":
            raise ValueError(f"unsupported bridge saved command: {sub}")
        from .bridge_profiles import SavedBridgeProfile

        verification = _bridge_verification(args)
        repository = args.repository.expanduser().resolve(strict=True)
        access_profile = _bridge_access_profile(args)
        profile = SavedBridgeProfile(
            name=args.name,
            target_profile=args.target_profile,
            tools=_bridge_tools(
                args.tool,
                write=args.write,
                verification=verification,
                access_profile=access_profile,
            ),
            repository=str(repository),
            verification_commands=verification,
            deadline_seconds=args.deadline_seconds,
            tunnel=args.tunnel,
            public_url=args.public_url,
            language=args.language,
            access_profile=access_profile,
            port=args.port,
            tunnel_timeout_seconds=args.tunnel_timeout_seconds,
        )
        saved = store.put(profile, replace_existing=bool(args.replace))
        _emit(saved.to_dict(), json_output=args.json)
        return 0
    if command == "connect":
        from .bridge_launcher import (
            DEFAULT_BRIDGE_DEADLINE_SECONDS,
            WebBridgeConnectConfig,
            bridge_diagnostics,
            config_from_saved_profile,
            connect_bridge,
        )

        if args.saved:
            config = config_from_saved_profile(
                args.saved, tailscale=args.tailscale, session_id=args.session_id
            )
        else:
            if not args.profile:
                raise ValueError("bridge connect requires a profile or --saved")
            if args.profile not in WEB_BRIDGE_PROFILES:
                raise ValueError(f"unknown web bridge profile: {args.profile}")
            verification = _bridge_verification(args)
            access_profile = _bridge_access_profile(args)
            config = WebBridgeConnectConfig(
                profile=args.profile,
                repository=args.repository.expanduser().resolve(strict=True),
                port=args.port,
                tools=_bridge_tools(
                    args.tool,
                    write=args.write,
                    verification=verification,
                    access_profile=access_profile,
                ),
                session_id=args.session_id,
                access_profile=access_profile,
                tunnel=args.tunnel,
                public_url=args.public_url,
                tailscale=args.tailscale,
                tunnel_timeout_seconds=args.tunnel_timeout_seconds,
                deadline_seconds=args.deadline_seconds
                or DEFAULT_BRIDGE_DEADLINE_SECONDS,
                verification_commands=verification,
                language=args.language,
            )
        if args.diagnostics_only:
            _emit(bridge_diagnostics(config), json_output=args.json)
            return 0
        return connect_bridge(config)
    if command == "serve":
        from .bridge_launcher import DEFAULT_BRIDGE_TOOLS
        from .bridge_serve import serve_bridge

        if not args.tool:
            args.tool = list(DEFAULT_BRIDGE_TOOLS)
        return serve_bridge(args)
    if command in {"status", "start", "stop", "restart"}:
        from .bridge_launcher import (
            restart_saved_bridge,
            start_saved_bridge,
            status_bridge,
            stop_bridge,
        )

        functions = {
            "status": status_bridge,
            "start": start_saved_bridge,
            "stop": stop_bridge,
            "restart": restart_saved_bridge,
        }
        payload = functions[command](args.saved)
        _emit(payload, json_output=args.json)
        return 0 if payload.get("verdict") not in {"error", "foreign"} else 1
    if command == "oauth":
        from .bridge_profiles import BridgeCredentialStore

        session_id = _bridge_session_id_for(args)
        reference = f"os-keyring:bridge/{session_id}"
        store = BridgeCredentialStore()
        secret = store.resolve(reference)
        payload = {
            "session_id": session_id,
            "reference": reference,
            "fingerprint": store.fingerprint(secret),
            "status": "available",
        }
        if args.copy:
            _copy_secret_to_clipboard(secret)
            payload["copied"] = True
        _emit(payload, json_output=args.json)
        if not args.quiet and not args.json:
            _write("The approval password was not printed. Use --copy to place it on the clipboard.\n")
        return 0
    if command == "credential":
        from .bridge_profiles import BridgeCredentialStore

        store = BridgeCredentialStore()
        session_id = _bridge_session_id_for(args)
        reference = f"os-keyring:bridge/{session_id}"
        if args.bridge_credential_command in {"set", "rotate"}:
            payload = store.set(session_id)
            payload.pop("secret", None)
        elif args.bridge_credential_command == "revoke":
            store.delete(session_id)
            payload = {"reference": reference, "status": "revoked"}
        else:
            secret = store.resolve(reference)
            payload = {
                "reference": reference,
                "status": "available",
                "fingerprint": store.fingerprint(secret),
            }
            if args.bridge_credential_command == "copy" or getattr(args, "copy", False):
                _copy_secret_to_clipboard(secret)
                payload["copied"] = True
        _emit(payload, json_output=args.json)
        return 0
    raise ValueError(f"unknown bridge command: {command}")


__all__ = ["handle_bridge"]
