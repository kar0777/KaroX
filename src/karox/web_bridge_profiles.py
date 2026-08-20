"""Secret-free saved profiles for repeatable managed web-bridge launches."""

from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from .browser_access import BrowserAccessPolicy
from .hosted_bridge import (
    DEFAULT_HOSTED_DEADLINE_SECONDS,
    KNOWN_HOSTED_TOOL_NAMES,
)
from .hosted_tools_runtime import ManagedServerProfile
from .models import AccessProfile
from .paths import config_dir
from .project_registry import ProjectRegistry, ProjectRegistryError


_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MCP_SERVER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TARGET_PROFILES = {"chatgpt-web", "claude-web", "notion", "hyperagent-web", "adapt"}
_TUNNELS = {"cloudflare", "tailscale", "custom"}
_LANGUAGES = {"en", "ru"}
_STORE_VERSION = 1


class WebBridgeProfileError(ValueError):
    """A saved bridge profile is missing, malformed, or unsafe to use."""


def default_web_bridge_profile_path() -> Path:
    return config_dir() / "vnext" / "web-bridge-profiles.json"


def _profile_name(value: str) -> str:
    name = str(value or "").strip()
    if not _PROFILE_NAME.fullmatch(name):
        raise WebBridgeProfileError(
            "saved bridge profile names must use 1-64 letters, digits, dots, "
            "underscores, or hyphens"
        )
    return name


def _https_origin(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    origin = value.strip().rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise WebBridgeProfileError(
            "saved custom public URL must be an HTTPS origin without path, query, "
            "fragment, or user information"
        )
    return origin


def _json_boolean(value: dict[str, Any], name: str, default: bool = False) -> bool:
    raw = value.get(name, default)
    if not isinstance(raw, bool):
        raise WebBridgeProfileError(f"saved {name} must be boolean")
    return raw


def _json_string_tuple(value: dict[str, Any], name: str) -> tuple[str, ...]:
    raw = value.get(name, [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise WebBridgeProfileError(f"saved {name} must be a string list")
    return tuple(raw)


def _command(value: Any) -> tuple[str, ...]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or len(value) > 100
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise WebBridgeProfileError(
            "saved verification commands must contain 1-100 non-empty strings"
        )
    return tuple(value)


def _server_profile(value: Any) -> dict[str, Any]:
    """Validate one server-profile dict persisted in a saved bridge profile.

    The dict is the secret-free public form from
    :meth:`ManagedServerProfile.to_public_dict`.  It is rebuilt into a
    :class:`ManagedServerProfile` on load (which re-validates), so this only
    guards against malformed JSON.
    """
    if not isinstance(value, dict):
        raise WebBridgeProfileError("saved server profile must be an object")
    allowed = {"name", "argv", "env_keys", "env_allowlist", "host_hint", "ready_url"}
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise WebBridgeProfileError(
            "saved server profile has unknown fields: " + ", ".join(unexpected)
        )
    argv = value.get("argv")
    if (
        not isinstance(argv, (list, tuple))
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise WebBridgeProfileError(
            "saved server profile argv must contain non-empty strings"
        )
    # Bound to locals before the isinstance test: repeating ``value.get(...)``
    # inside the condition and the body describes two separate reads of untrusted
    # JSON, so neither the type checker nor a reader can rely on them agreeing.
    raw_env_keys = value.get("env_keys")
    env_keys = raw_env_keys if isinstance(raw_env_keys, list) else []
    raw_allowlist = value.get("env_allowlist")
    if raw_allowlist is not None and not isinstance(raw_allowlist, list):
        # Without this, a saved profile carrying a number here reached
        # ``frozenset(... for item in 5)`` and left the process as a bare
        # TypeError, past the WebBridgeProfileError contract this function
        # promises for every malformed field.
        raise WebBridgeProfileError("saved server profile env_allowlist must be a list")

    # Reconstruct the runtime object so the full validation (env keys, host,
    # allowlist) runs once on load, not only when the profile is used.
    try:
        ManagedServerProfile(
            name=str(value.get("name", "")),
            argv=tuple(str(item) for item in argv),
            env={str(k): "" for k in env_keys},
            env_allowlist=frozenset(str(item) for item in (raw_allowlist or [])),
            host_hint=str(value.get("host_hint", "127.0.0.1")),
            ready_url=value.get("ready_url"),
        )
    except (TypeError, ValueError) as exc:
        raise WebBridgeProfileError(f"saved server profile is invalid: {exc}") from exc
    return dict(value)


@dataclass(frozen=True)
class SavedWebBridgeProfile:
    """Only non-secret launch policy; credentials remain in the OS keyring."""

    name: str
    target_profile: str
    tools: tuple[str, ...]
    mcp_servers: tuple[str, ...] = ()
    repository: Optional[str] = None
    projects: tuple[dict[str, str], ...] = ()
    default_project_id: Optional[str] = None
    verification_commands: tuple[tuple[str, ...], ...] = ()
    # Server profiles are secret-free recipes (argv + env keys + env allowlist),
    # so they are safe to persist.  Stored as plain dicts and rebuilt into
    # ``ManagedServerProfile`` on load to keep the dataclass free of runtime
    # objects in the JSON store.
    server_profiles: tuple[dict[str, Any], ...] = ()
    browser_external_https: bool = False
    browser_allowed_domains: tuple[str, ...] = ()
    browser_denied_domains: tuple[str, ...] = ()
    browser_headed: bool = False
    browser_user_takeover: bool = False
    browser_network_inspection: bool = False
    browser_payment_confirmation: bool = False
    browser_allowed_emails: tuple[str, ...] = ()
    browser_credential_refs: tuple[str, ...] = ()
    deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS
    tunnel: str = "tailscale"
    public_url: Optional[str] = None
    language: str = "en"
    access_profile: AccessProfile = AccessProfile.READ_ONLY
    port: int = 8765
    tunnel_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _profile_name(self.name))
        if self.target_profile not in _TARGET_PROFILES:
            raise WebBridgeProfileError(
                "saved target profile must be chatgpt-web, claude-web, notion, hyperagent-web, or adapt"
            )
        if not self.tools or len(set(self.tools)) != len(self.tools):
            raise WebBridgeProfileError("saved bridge tools must be non-empty and unique")
        unknown = sorted(set(self.tools) - set(KNOWN_HOSTED_TOOL_NAMES))
        if unknown:
            raise WebBridgeProfileError(
                "saved bridge profile contains unknown tools: " + ", ".join(unknown)
            )
        if len(set(self.mcp_servers)) != len(self.mcp_servers) or not all(
            isinstance(item, str) and _MCP_SERVER_ID.fullmatch(item)
            for item in self.mcp_servers
        ):
            raise WebBridgeProfileError(
                "saved MCP server IDs must be unique safe identifiers"
            )
        commands = tuple(_command(value) for value in self.verification_commands)
        object.__setattr__(self, "verification_commands", commands)
        if "karox.checks.run" in self.tools and not commands:
            raise WebBridgeProfileError(
                "a profile exposing karox.checks.run needs at least one approved "
                "verification command"
            )
        profiles = tuple(_server_profile(value) for value in self.server_profiles)
        object.__setattr__(self, "server_profiles", profiles)
        if "karox.dev_server.start" in self.tools and not profiles:
            raise WebBridgeProfileError(
                "a profile exposing karox.dev_server.start needs at least one "
                "approved server profile"
            )
        seen: set[str] = set()
        for profile in profiles:
            if profile["name"] in seen:
                raise WebBridgeProfileError(
                    f"duplicate saved server profile: {profile['name']}"
                )
            seen.add(profile["name"])
        try:
            browser_policy = BrowserAccessPolicy(
                session_id=f"saved-{self.name}",
                localhost=True,
                external_https=self.browser_external_https,
                allowed_domains=tuple(self.browser_allowed_domains),
                denied_domains=tuple(self.browser_denied_domains),
                headed=self.browser_headed,
                user_takeover=self.browser_user_takeover,
                network_inspection=self.browser_network_inspection,
                payment_confirmation=self.browser_payment_confirmation,
                allowed_emails=tuple(self.browser_allowed_emails),
                allowed_credential_refs=tuple(self.browser_credential_refs),
            )
        except ValueError as exc:
            raise WebBridgeProfileError(f"saved browser policy is invalid: {exc}") from exc
        object.__setattr__(self, "browser_allowed_domains", browser_policy.allowed_domains)
        object.__setattr__(self, "browser_denied_domains", browser_policy.denied_domains)
        object.__setattr__(self, "browser_allowed_emails", browser_policy.allowed_emails)
        object.__setattr__(
            self,
            "browser_credential_refs",
            browser_policy.allowed_credential_refs,
        )
        if self.repository is not None and not str(self.repository).strip():
            raise WebBridgeProfileError("saved repository path must not be empty")
        try:
            project_registry = ProjectRegistry.from_profile(
                repository=self.repository,
                projects=self.projects,
                default_project_id=self.default_project_id,
            )
        except (ProjectRegistryError, TypeError) as exc:
            raise WebBridgeProfileError(f"saved project registry is invalid: {exc}") from exc
        object.__setattr__(self, "projects", tuple(project_registry.to_payload()))
        object.__setattr__(self, "default_project_id", project_registry.default_project_id)
        anchor = (
            project_registry.entry_for_path(self.repository)
            if self.repository is not None
            else project_registry.default
        )
        if anchor is not None:
            # The legacy field remains the durable session anchor. The logical
            # default may change independently via default_project_id.
            object.__setattr__(self, "repository", anchor.path)
        if self.tunnel not in _TUNNELS:
            raise WebBridgeProfileError(
                "saved bridge tunnel must be cloudflare, tailscale, or custom"
            )
        origin = _https_origin(self.public_url)
        object.__setattr__(self, "public_url", origin)
        if self.tunnel == "custom" and origin is None:
            raise WebBridgeProfileError("saved custom tunnel requires a public URL")
        if self.tunnel != "custom" and origin is not None:
            raise WebBridgeProfileError(
                "saved public URL is only valid with the custom tunnel"
            )
        if self.language not in _LANGUAGES:
            raise WebBridgeProfileError("saved bridge language must be en or ru")
        if not isinstance(self.access_profile, AccessProfile):
            try:
                profile = AccessProfile(str(self.access_profile))
            except ValueError as exc:
                raise WebBridgeProfileError("saved access profile is invalid") from exc
            object.__setattr__(self, "access_profile", profile)
        if self.browser_external_https and self.access_profile == AccessProfile.READ_ONLY:
            raise WebBridgeProfileError(
                "saved external browser profile requires browser_control, workspace_write, or elevated access"
            )
        if not 1 <= int(self.port) <= 65_535:
            raise WebBridgeProfileError("saved bridge port must be between 1 and 65535")
        if not 0.1 <= float(self.deadline_seconds) <= 3600.0:
            raise WebBridgeProfileError(
                "saved bridge deadline must be between 0.1 and 3600 seconds"
            )
        if not 1.0 <= float(self.tunnel_timeout_seconds) <= 300.0:
            raise WebBridgeProfileError(
                "saved tunnel timeout must be between 1 and 300 seconds"
            )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["access_profile"] = self.access_profile.value
        value["tools"] = list(self.tools)
        value["mcp_servers"] = list(self.mcp_servers)
        value["projects"] = [dict(item) for item in self.projects]
        value["verification_commands"] = [
            list(command) for command in self.verification_commands
        ]
        value["browser_allowed_domains"] = list(self.browser_allowed_domains)
        value["browser_denied_domains"] = list(self.browser_denied_domains)
        value["browser_allowed_emails"] = list(self.browser_allowed_emails)
        value["browser_credential_refs"] = list(self.browser_credential_refs)
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "SavedWebBridgeProfile":
        if not isinstance(value, dict):
            raise WebBridgeProfileError("saved bridge profile must be a JSON object")
        allowed = {
            "name",
            "target_profile",
            "tools",
            "mcp_servers",
            "repository",
            "projects",
            "default_project_id",
            "verification_commands",
            "server_profiles",
            "browser_external_https",
            "browser_allowed_domains",
            "browser_denied_domains",
            "browser_headed",
            "browser_user_takeover",
            "browser_network_inspection",
            "browser_payment_confirmation",
            "browser_allowed_emails",
            "browser_credential_refs",
            "deadline_seconds",
            "tunnel",
            "public_url",
            "language",
            "access_profile",
            "port",
            "tunnel_timeout_seconds",
        }
        unexpected = sorted(set(value) - allowed)
        if unexpected:
            raise WebBridgeProfileError(
                "saved bridge profile contains unknown fields: "
                + ", ".join(unexpected)
            )
        try:
            tools = tuple(value["tools"])
            raw_projects = value.get("projects", [])
            if not isinstance(raw_projects, list):
                raise WebBridgeProfileError("saved projects must be a project list")
            projects = tuple(raw_projects)
            commands = tuple(
                _command(command) for command in value.get("verification_commands", [])
            )
            server_profiles = tuple(
                _server_profile(item)
                for item in value.get("server_profiles", [])
            )
            access_profile = AccessProfile(
                value.get("access_profile", AccessProfile.READ_ONLY.value)
            )
            return cls(
                name=value["name"],
                target_profile=value["target_profile"],
                tools=tools,
                mcp_servers=_json_string_tuple(value, "mcp_servers"),
                repository=value.get("repository"),
                projects=projects,
                default_project_id=value.get("default_project_id"),
                verification_commands=commands,
                server_profiles=server_profiles,
                browser_external_https=_json_boolean(value, "browser_external_https"),
                browser_allowed_domains=_json_string_tuple(
                    value, "browser_allowed_domains"
                ),
                browser_denied_domains=_json_string_tuple(
                    value, "browser_denied_domains"
                ),
                browser_headed=_json_boolean(value, "browser_headed"),
                browser_user_takeover=_json_boolean(value, "browser_user_takeover"),
                browser_network_inspection=_json_boolean(
                    value, "browser_network_inspection"
                ),
                browser_payment_confirmation=_json_boolean(
                    value, "browser_payment_confirmation"
                ),
                browser_allowed_emails=_json_string_tuple(
                    value, "browser_allowed_emails"
                ),
                browser_credential_refs=_json_string_tuple(
                    value, "browser_credential_refs"
                ),
                deadline_seconds=float(
                    value.get("deadline_seconds", DEFAULT_HOSTED_DEADLINE_SECONDS)
                ),
                tunnel=value.get("tunnel", "tailscale"),
                public_url=value.get("public_url"),
                language=value.get("language", "en"),
                access_profile=access_profile,
                port=int(value.get("port", 8765)),
                tunnel_timeout_seconds=float(value.get("tunnel_timeout_seconds", 30.0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, WebBridgeProfileError):
                raise
            raise WebBridgeProfileError(
                "saved bridge profile is missing a required or correctly typed field"
            ) from exc


class WebBridgeProfileStore:
    """Atomic JSON registry for secret-free bridge launch profiles."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = (path or default_web_bridge_profile_path()).expanduser().resolve()

    def _load(self) -> dict[str, SavedWebBridgeProfile]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WebBridgeProfileError(
                f"cannot read saved bridge profiles from {self.path}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("version") != _STORE_VERSION:
            raise WebBridgeProfileError("saved bridge profile store has an unknown format")
        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, list):
            raise WebBridgeProfileError("saved bridge profile store has no profile list")
        profiles: dict[str, SavedWebBridgeProfile] = {}
        for raw in raw_profiles:
            profile = SavedWebBridgeProfile.from_dict(raw)
            if profile.name in profiles:
                raise WebBridgeProfileError(
                    f"duplicate saved bridge profile: {profile.name}"
                )
            profiles[profile.name] = profile
        return profiles

    def _write(self, profiles: dict[str, SavedWebBridgeProfile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _STORE_VERSION,
            "profiles": [profiles[name].to_dict() for name in sorted(profiles)],
        }
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        descriptor = os.open(
            str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            with contextlib.suppress(OSError):
                os.chmod(self.path, 0o600)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()

    def list(self) -> tuple[SavedWebBridgeProfile, ...]:
        profiles = self._load()
        return tuple(profiles[name] for name in sorted(profiles))

    def get(self, name: str) -> SavedWebBridgeProfile:
        normalized = _profile_name(name)
        profiles = self._load()
        try:
            return profiles[normalized]
        except KeyError as exc:
            raise WebBridgeProfileError(
                f"saved bridge profile does not exist: {normalized}"
            ) from exc

    def put(
        self, profile: SavedWebBridgeProfile, *, replace_existing: bool = True
    ) -> SavedWebBridgeProfile:
        profiles = self._load()
        if not replace_existing and profile.name in profiles:
            raise WebBridgeProfileError(
                f"saved bridge profile already exists: {profile.name}"
            )
        profiles[profile.name] = profile
        self._write(profiles)
        return profile

    def delete(self, name: str) -> SavedWebBridgeProfile:
        normalized = _profile_name(name)
        profiles = self._load()
        try:
            removed = profiles.pop(normalized)
        except KeyError as exc:
            raise WebBridgeProfileError(
                f"saved bridge profile does not exist: {normalized}"
            ) from exc
        self._write(profiles)
        return removed

    def doctor(self) -> dict[str, Any]:
        try:
            profiles = self.list()
        except WebBridgeProfileError as exc:
            # Doctor is the recovery surface. Normal list/get/start paths stay
            # strict, but diagnostics must remain usable when a saved project
            # was moved or deleted and one profile can no longer canonicalize.
            return {
                "status": "degraded",
                "path": str(self.path),
                "secret_fields": [],
                "profile_count": None,
                "profiles": [],
                "error": str(exc)[:400],
            }
        return {
            "status": "ok",
            "path": str(self.path),
            "secret_fields": [],
            "profile_count": len(profiles),
            "profiles": [profile.name for profile in profiles],
        }
