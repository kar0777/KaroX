"""Outbound PromptQL Natural Language API client.

This module is the other half of the bridge contract: the CLI (or TUI) calls a
hosted agent -- PromptQL -- and receives its ``assistant_actions`` as evidence.
PromptQL is a hosted agent: it executes actions server-side against its own DDN
data sources and returns ``assistant_actions`` (``message``/``plan``/``code``/
``code_output``), not tool-calls for local execution.  KaroX therefore exposes it
as a dedicated ``target ask`` command instead of a provider in the tool-calling
routing loop -- mixing the two would be a semantic mismatch.

The contract below was verified against the official ``hasura/promptql-python-sdk``
source (``client.py``):

* ``POST {api_base_url}/query`` (the base URL must NOT include ``/query``).
* ``Authorization: Bearer {api_key}`` header.
* Default base URL: ``https://api.promptql.pro.hasura.io``.
* v2 body: ``{"ddn": {"build_version": ...} | {"build_id": ...},
  "interactions": [{"user_message": ...}], "stream": false, "timezone": ...}``.
* Non-streaming response: ``{"assistant_actions": [...], "modified_artifacts": [...]}``.

Only the documented v2 non-streaming path is implemented here.  v1 (``ddn_url``
plus explicit LLM config) and SSE streaming are deferred with explicit, honest
errors rather than half-built shims.  No live PromptQL run has been recorded;
the contract is exercised against a mocked HTTP transport in the test suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

from .credentials import CredentialStore
from .ecosystem import TARGET_PRESETS, EcosystemRegistry
from .providers import OpenAIChatCompletionsProvider
from .security import redact


DEFAULT_API_BASE_URL = "https://api.promptql.pro.hasura.io"
DEFAULT_TIMEZONE = "UTC"
_QUERY_PATH = "/query"


class PromptQLInvocationError(RuntimeError):
    """A PromptQL Natural Language API call failed."""


class PromptQLAccessDenied(PromptQLInvocationError, PermissionError):
    """PromptQL rejected the credential or the request was unauthorized."""


@dataclass(frozen=True)
class PromptQLTargetConfig:
    """Resolved, non-secret configuration for one PromptQL outbound target."""

    credential_ref: str
    api_base_url: str = DEFAULT_API_BASE_URL
    build_version: Optional[str] = None
    build_id: Optional[str] = None
    ddn_url: Optional[str] = None
    timezone: str = DEFAULT_TIMEZONE

    def __post_init__(self) -> None:
        if not isinstance(self.credential_ref, str) or not self.credential_ref:
            raise ValueError(
                "promptql target has no credential; run `karox credential set` "
                "and `karox target configure ... --credential-ref`"
            )
        base = self._normalized_base_url(self.api_base_url)
        object.__setattr__(self, "api_base_url", base)
        if not self.build_version and not self.build_id:
            if not self.ddn_url:
                raise ValueError(
                    "promptql target requires build_version or build_id "
                    "(v2 Natural Language API); v1 ddn_url mode is not yet supported"
                )
            raise ValueError(
                "v1 ddn_url mode is not yet supported; configure build_version "
                "or build_id for the v2 Natural Language API"
            )
        if self.build_version and self.build_id:
            raise ValueError(
                "promptql target must set build_version or build_id, not both"
            )
        if not isinstance(self.timezone, str) or not self.timezone.strip():
            raise ValueError("promptql target timezone must be a non-empty string")

    @staticmethod
    def _normalized_base_url(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("promptql api_base_url is required")
        parts = urlsplit(value.strip())
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "promptql api_base_url must be HTTP(S) without credentials, "
                "query, or fragment"
            )
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


@dataclass
class PromptQLResponse:
    """Redacted, parsed Natural Language API response."""

    assistant_actions: List[Dict[str, Any]]
    modified_artifacts: List[Dict[str, Any]]
    raw: Dict[str, Any]


class PromptQLNaturalLanguageClient:
    """Call the documented PromptQL Natural Language API (v2, non-streaming)."""

    def __init__(
        self,
        config: PromptQLTargetConfig,
        *,
        credential_accessor: Callable[[], str],
        timeout_seconds: float = 60.0,
    ) -> None:
        self._config = config
        self._accessor = credential_accessor
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0.1 <= float(timeout_seconds) <= 3600
        ):
            raise ValueError("promptql timeout must be between 0.1 and 3600 seconds")
        self.timeout_seconds = float(timeout_seconds)

    @classmethod
    def from_config(
        cls,
        config: PromptQLTargetConfig,
        *,
        credential_store: Optional[CredentialStore] = None,
        timeout_seconds: float = 60.0,
    ) -> "PromptQLNaturalLanguageClient":
        store = credential_store or CredentialStore()
        accessor = store.accessor(config.credential_ref)
        return cls(config, credential_accessor=accessor, timeout_seconds=timeout_seconds)

    def _endpoint(self) -> str:
        return f"{self._config.api_base_url}{_QUERY_PATH}"

    def _body(
        self, message: str, prior_interactions: Optional[Sequence[Dict[str, Any]]]
    ) -> Dict[str, Any]:
        ddn: Dict[str, Any]
        if self._config.build_version:
            ddn = {"build_version": self._config.build_version}
        else:
            ddn = {"build_id": self._config.build_id}
        interactions: List[Dict[str, Any]] = []
        if prior_interactions:
            for item in prior_interactions:
                if not isinstance(item, dict):
                    raise ValueError("prior interactions must be objects")
                interactions.append(dict(item))
        interactions.append({"user_message": str(message)})
        return {
            "ddn": ddn,
            "interactions": interactions,
            "stream": False,
            "timezone": self._config.timezone,
        }

    def ask(
        self,
        message: str,
        *,
        prior_interactions: Optional[Sequence[Dict[str, Any]]] = None,
        deadline_seconds: Optional[float] = None,
    ) -> PromptQLResponse:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("promptql ask message must be a non-empty string")
        endpoint = self._endpoint()
        if not OpenAIChatCompletionsProvider._credential_transport_is_secure(endpoint):
            raise ValueError(
                "promptql credentials require HTTPS or a loopback HTTP endpoint"
            )
        try:
            api_key = self._accessor()
        except Exception as exc:
            raise PromptQLAccessDenied(
                f"cannot resolve promptql credential: {type(exc).__name__}"
            ) from exc
        if not isinstance(api_key, str) or not api_key.strip():
            raise PromptQLAccessDenied("promptql credential accessor returned no key")
        if "\r" in api_key or "\n" in api_key:
            raise PromptQLAccessDenied("promptql credential contains invalid characters")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        payload = self._body(message, prior_interactions)
        timeout = (
            self.timeout_seconds
            if deadline_seconds is None
            else min(self.timeout_seconds, float(deadline_seconds))
        )
        try:
            with httpx.Client(follow_redirects=False) as client:
                response = client.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=timeout,
                )
        except httpx.TransportError as exc:
            raise PromptQLInvocationError(
                f"promptql transport failed: {type(exc).__name__}"
            ) from exc
        status = response.status_code
        if status in (401, 403):
            raise PromptQLAccessDenied(
                f"promptql rejected the credential (HTTP {status})"
            )
        if status >= 400:
            text = response.text
            raise PromptQLInvocationError(
                f"promptql returned HTTP {status}: {redact(text, secrets=(api_key,))[:1000]}"
            )
        try:
            body = response.json()
        except Exception as exc:
            raise PromptQLInvocationError(
                f"promptql response was not JSON: {type(exc).__name__}"
            ) from exc
        if not isinstance(body, dict):
            raise PromptQLInvocationError("promptql response body must be a JSON object")
        safe = redact(body, secrets=(api_key,))
        if not isinstance(safe, dict):
            safe = {"value": safe}
        actions = safe.get("assistant_actions")
        artifacts = safe.get("modified_artifacts")
        if not isinstance(actions, list):
            actions = []
        if not isinstance(artifacts, list):
            artifacts = []
        return PromptQLResponse(
            assistant_actions=list(actions),
            modified_artifacts=list(artifacts),
            raw=dict(safe),
        )


def load_promptql_target(config_dir: Path, item_id: str) -> PromptQLTargetConfig:
    """Read one configured PromptQL target from the ecosystem registry."""
    registry = EcosystemRegistry(
        config_dir / "vnext" / "targets.json", TARGET_PRESETS
    )
    record = registry.get(item_id)
    if record.preset_id != "promptql":
        raise ValueError(
            "target ask supports the promptql natural-language target only; "
            f"{item_id} is preset {record.preset_id!r}"
        )
    settings = record.settings
    return PromptQLTargetConfig(
        credential_ref=record.credential_ref or "",
        api_base_url=settings.get("api_base_url") or DEFAULT_API_BASE_URL,
        build_version=settings.get("build_version") or None,
        build_id=settings.get("build_id") or None,
        ddn_url=settings.get("ddn_url") or None,
        timezone=settings.get("timezone") or DEFAULT_TIMEZONE,
    )
