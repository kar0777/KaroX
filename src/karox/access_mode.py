"""Common Bypass (access mode) contract for every KaroX connection family.

RU: "Bypass rezhim" -- minimum vnutrennikh ogranichenij KaroX dlya avtonomnoj
razrabotki.  EN: "Bypass mode" -- minimal KaroX restrictions for autonomous
development.

Bypass is an opt-in, per-connection mode expressed entirely through the
permission architecture that already exists: the saved record's
``AccessProfile`` plus the ``CapabilityPolicy`` layer that enforces it at
runtime.  ON selects the highest existing developer capability profile
(``AccessProfile.ELEVATED``); OFF returns to the normal protected Project
access contract (``AccessProfile.WORKSPACE_WRITE``).  No second permission
architecture, no parallel store.

What Bypass is NOT:

* it does not touch OS / provider / platform security;
* it does not disable credential isolation or the keyring boundary;
* it does not skip confirmation of irreversible external actions;
* it does not mean automatic git push, publishing, or payments;
* it never rotates secrets, connection identity, or bridge URLs.

Hyperagent's earlier "Full developer access" toggle is this very mode; its
canonical saved-profile builder lives here now and Hyperagent is the first
user of the shared implementation.  A record written before this mode
existed reads as OFF, and a legacy Hyperagent profile that already carries
the full elevated contract reads as ON -- no migration rewrite, no schema
version bump.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

__all__ = [
    "BYPASS_TOOL_NAMES",
    "build_saved_profile_bypass",
    "provider_access_profile",
    "provider_bypass_enabled",
    "saved_profile_bypass_enabled",
    "session_access_profile",
    "set_provider_bypass",
]

# The tool names a coding connection gains under Bypass beyond its normal
# project-access catalogue.  They are appended when the mode is first enabled
# and then kept stable across ON/OFF transitions: several clients cache the
# MCP tool catalogue for the lifetime of a thread, so only the runtime
# permission profile may change on a toggle.
BYPASS_TOOL_NAMES: tuple[str, ...] = (
    "karox.command.run",
    "karox.command.start",
    "karox.command.status",
    "karox.command.logs",
    "karox.command.cancel",
    "karox.git.commit",
)


def saved_profile_bypass_enabled(profile: Any) -> bool:
    """Whether a saved web-bridge profile explicitly enables Bypass.

    New profiles persist a dedicated ``bypass`` bit so Advanced/elevated access
    can exist without silently authorizing destructive deletion. ``from_dict``
    preserves legacy elevated profiles as Bypass during migration.
    """

    try:
        if hasattr(profile, "bypass"):
            return bool(getattr(profile, "bypass"))
        from .models import AccessProfile
        return getattr(profile, "access_profile", None) == AccessProfile.ELEVATED
    except Exception:
        return False


def build_saved_profile_bypass(profile: Any, enabled: bool) -> Any:
    """Return the canonical saved profile for Bypass ON/OFF.

    The returned record keeps its identity: name, target, repository,
    projects, port, tunnel, public URL, and credential references are never
    rewritten, so flipping the mode cannot rotate a secret, change a bridge
    URL, or spawn a duplicate.  Hyperagent additionally opts into its
    historical full-developer browser contract; every other target keeps its
    browser policy exactly as configured.
    """

    if getattr(profile, "target_profile", "") == "hyperagent-web":
        return _build_hyperagent_bypass(profile, enabled)

    from .models import AccessProfile

    kwargs: dict[str, Any] = {
        "access_profile": (
            AccessProfile.ELEVATED if enabled else AccessProfile.WORKSPACE_WRITE
        ),
        "bypass": bool(enabled),
    }
    if enabled:
        tools = list(getattr(profile, "tools", ()) or ())
        extended = False
        for name in BYPASS_TOOL_NAMES:
            if name not in tools:
                tools.append(name)
                extended = True
        if extended:
            kwargs["tools"] = tuple(dict.fromkeys(tools))
    return replace(profile, **kwargs)


def _build_hyperagent_bypass(profile: Any, enabled: bool) -> Any:
    """The canonical Hyperagent profile for Full access / Bypass ON-OFF.

    ON deliberately means one coherent trusted developer contract rather than
    a pile of independent checkboxes: elevated repo/process/check
    capabilities, the whole coding tool surface, Git commit, an unrestricted
    developer command runner, and a headed browser that may navigate public
    HTTPS with network inspection and takeover.  OFF returns to the normal
    protected Project access contract.
    """

    repository_raw = str(getattr(profile, "repository", "") or "").strip()
    if not repository_raw:
        raise ValueError("Hyperagent profile has no repository")
    repository = Path(repository_raw).expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise ValueError("Hyperagent repository is not a directory")

    from .hosted_tools_runtime import server_profiles_for_repository
    from .models import AccessProfile
    from .verification import discover_verification_commands
    from .web_bridge_launcher import (
        CHECKS_RUN_TOOL,
        DEFAULT_WEB_TOOLS,
        EXTERNAL_BROWSER_TOOL_NAMES,
        WRITE_WEB_TOOLS,
    )

    verification = discover_verification_commands(repository)
    if not verification:
        verification = tuple(getattr(profile, "verification_commands", ()) or ())
    runtime_profiles = server_profiles_for_repository(repository)
    server_profiles = tuple(item.to_public_dict() for item in runtime_profiles)

    tools = list(DEFAULT_WEB_TOOLS)
    tools.extend(WRITE_WEB_TOOLS)
    if verification:
        tools.append(CHECKS_RUN_TOOL)
    if not server_profiles:
        tools = [
            item
            for item in tools
            if item not in {
                "karox.dev_server.start",
                "karox.dev_server.stop",
                "karox.dev_server.restart",
            }
        ]
    # Hyperagent may cache the MCP tool catalogue for the lifetime of a thread.
    # Keep the advertised tool names stable across Project access <-> Bypass
    # and change only the runtime permission/profile.  Otherwise flipping the
    # switch can leave a perfectly healthy elevated bridge behind a stale
    # client catalogue that still contains the smaller Project-access set.
    tools.extend(BYPASS_TOOL_NAMES)
    tools.extend(EXTERNAL_BROWSER_TOOL_NAMES)

    return replace(
        profile,
        tools=tuple(dict.fromkeys(tools)),
        verification_commands=tuple(verification),
        server_profiles=server_profiles,
        access_profile=(
            AccessProfile.ELEVATED if enabled else AccessProfile.WORKSPACE_WRITE
        ),
        bypass=bool(enabled),
        browser_external_https=bool(enabled),
        # Empty allowlist + external_https=True means any public HTTPS host
        # that passes KaroX DNS/private-network checks: the intended normal
        # browser.
        browser_allowed_domains=(),
        browser_headed=bool(enabled),
        browser_user_takeover=bool(enabled),
        browser_network_inspection=bool(enabled),
        # Spending/subscription confirmation remains a separate explicit gate.
        browser_payment_confirmation=False,
    )


def provider_bypass_enabled(record: Any) -> bool:
    """Whether an API/provider connection requests Bypass for new sessions."""

    return bool(getattr(record, "bypass", False))


def set_provider_bypass(record: Any, enabled: bool) -> Any:
    """Return the provider record with the persisted Bypass preference.

    The provider record only carries the preference; enforcement happens when
    a KaroX agent session is created for that provider.  Provider HTTP auth,
    transport configuration, and credential storage never change here.
    """

    if bool(getattr(record, "bypass", False)) == bool(enabled):
        return record
    return replace(record, bypass=bool(enabled))


def provider_access_profile(record: Any, base: Any = None) -> Any:
    """The ``AccessProfile`` a new agent session for this provider gets.

    Bypass ON maps to the existing elevated developer contract; OFF keeps the
    caller's normal profile (or the protected Project access default).
    """

    from .models import AccessProfile

    if provider_bypass_enabled(record):
        return AccessProfile.ELEVATED
    if base is not None:
        return base
    return AccessProfile.WORKSPACE_WRITE


def session_access_profile(records: Any, base: Any = None) -> Any:
    """The profile for a session that may be served by several providers.

    A routed session can fall back from its primary provider to another one
    mid-run, and the session's profile is fixed when the session is created.
    The narrowest safe rule is therefore unanimity: the session is elevated
    only when *every* provider that could serve it has Bypass ON.  One
    fallback route left in normal mode keeps the whole session protected
    rather than quietly lending it elevated capabilities.

    An empty set of records (direct ``--base-url`` mode, which persists no
    provider record and therefore has nowhere to store the preference) is not
    unanimous consent and keeps the normal profile.
    """

    from .models import AccessProfile

    items = list(records)
    if items and all(provider_bypass_enabled(item) for item in items):
        return AccessProfile.ELEVATED
    if base is not None:
        return base
    return AccessProfile.WORKSPACE_WRITE
