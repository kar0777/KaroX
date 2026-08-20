"""OS-keyring storage for credentials used by KaroX-managed browser sessions.

Browser login values are intentionally isolated from provider, MCP, and bridge
credentials.  The public configuration stores only an opaque reference such as
``os-keyring:browser/gmail-test``.  The username and password live together in
the operating-system keyring (Windows Credential Manager, macOS Keychain, or
Linux Secret Service) and are resolved only inside trusted browser code.

This module deliberately has no environment/plaintext fallback. Browser
credentials are interactive account secrets and must fail closed when a secure
OS keyring is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, Optional

from .credentials import CredentialBackend, CredentialError, KeyringBackend


_REFERENCE_PREFIX = "os-keyring:browser/"
_SERVICE = "KaroX/browser"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_STORE_VERSION = 1
_MAX_VALUE_LENGTH = 65_536


class BrowserCredentialError(CredentialError):
    """A browser-credential operation failed without exposing secret values."""


@dataclass(frozen=True)
class BrowserCredentialReference:
    """Opaque public reference to one browser login bundle."""

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _NAME.fullmatch(self.name) is None:
            raise ValueError(
                "browser credential name must contain 1-128 safe alphanumeric characters"
            )

    def __str__(self) -> str:
        return f"{_REFERENCE_PREFIX}{self.name}"

    @classmethod
    def parse(cls, value: str) -> "BrowserCredentialReference":
        if isinstance(value, str) and value.startswith(_REFERENCE_PREFIX):
            return cls(value[len(_REFERENCE_PREFIX) :])
        raise ValueError(
            "browser credential reference must use os-keyring:browser/<name>"
        )


class BrowserCredentialStore:
    """Secure store for a browser username/password pair.

    Public methods used by UI/configuration code return only references,
    fingerprints, and status. Raw fields are available solely through
    :meth:`resolve_field`, intended for local browser input code immediately
    before filling an owned page element.
    """

    def __init__(self, backend: Optional[CredentialBackend] = None) -> None:
        self._backend = backend or KeyringBackend()

    @staticmethod
    def _value(value: str, field: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"browser credential {field} must not be empty")
        if "\x00" in value or "\r" in value or "\n" in value:
            raise ValueError(
                f"browser credential {field} contains invalid control characters"
            )
        if len(value) > _MAX_VALUE_LENGTH:
            raise ValueError(f"browser credential {field} exceeds {_MAX_VALUE_LENGTH} characters")
        return value

    @staticmethod
    def _fingerprint(payload: str) -> str:
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"sha256:{digest[:12]}"

    @staticmethod
    def _encode(username: str, password: str) -> str:
        return json.dumps(
            {
                "version": _STORE_VERSION,
                "username": username,
                "password": password,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _decode(payload: str) -> dict[str, str]:
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise BrowserCredentialError(
                "browser credential stored in the OS keyring has an invalid format"
            ) from exc
        if not isinstance(value, dict) or value.get("version") != _STORE_VERSION:
            raise BrowserCredentialError(
                "browser credential stored in the OS keyring has an unknown format"
            )
        username = value.get("username")
        password = value.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise BrowserCredentialError(
                "browser credential stored in the OS keyring is unusable"
            )
        try:
            return {
                "username": BrowserCredentialStore._value(username, "username"),
                "password": BrowserCredentialStore._value(password, "password"),
            }
        except ValueError as exc:
            raise BrowserCredentialError(
                "browser credential stored in the OS keyring is unusable"
            ) from exc

    def _prepare(self, name: str, *, username: str, password: str) -> tuple[BrowserCredentialReference, str]:
        reference = BrowserCredentialReference(name)
        safe_username = self._value(username, "username")
        safe_password = self._value(password, "password")
        return reference, self._encode(safe_username, safe_password)

    def validate_bundle(self, name: str, *, username: str, password: str) -> dict[str, str]:
        """Validate a bundle and return only metadata without touching keyring."""

        reference, payload = self._prepare(name, username=username, password=password)
        return {
            "reference": str(reference),
            "fingerprint": self._fingerprint(payload),
            "backend": "os-keyring",
        }

    def set(self, name: str, *, username: str, password: str) -> dict[str, str]:
        """Store one login bundle and return only secret-free metadata."""

        reference, payload = self._prepare(name, username=username, password=password)
        try:
            self._backend.set(_SERVICE, reference.name, payload)
        except CredentialError:
            raise
        except Exception as exc:
            raise BrowserCredentialError(
                f"cannot write browser OS credential: {type(exc).__name__}"
            ) from exc
        return {
            "reference": str(reference),
            "fingerprint": self._fingerprint(payload),
            "backend": "os-keyring",
        }

    def _resolve(self, reference: str | BrowserCredentialReference) -> dict[str, str]:
        parsed = (
            reference
            if isinstance(reference, BrowserCredentialReference)
            else BrowserCredentialReference.parse(reference)
        )
        try:
            payload = self._backend.get(_SERVICE, parsed.name)
        except CredentialError:
            raise
        except Exception as exc:
            raise BrowserCredentialError(
                f"cannot read browser OS credential: {type(exc).__name__}"
            ) from exc
        if not isinstance(payload, str) or not payload:
            raise BrowserCredentialError(
                f"browser credential reference does not exist: {parsed}"
            )
        return self._decode(payload)

    def resolve_field(
        self,
        reference: str | BrowserCredentialReference,
        field: Literal["username", "password"],
    ) -> str:
        """Resolve exactly one field for immediate local browser injection."""

        if field not in {"username", "password"}:
            raise ValueError("browser credential field must be username or password")
        return self._resolve(reference)[field]

    def delete(self, name: str) -> dict[str, str]:
        reference = BrowserCredentialReference(name)
        try:
            self._backend.delete(_SERVICE, reference.name)
        except CredentialError:
            raise
        except Exception as exc:
            raise BrowserCredentialError(
                f"cannot delete browser OS credential: {type(exc).__name__}"
            ) from exc
        return {"reference": str(reference), "status": "deleted"}

    def doctor(self) -> dict[str, str]:
        """Report protection without reading a credential value."""

        if isinstance(self._backend, KeyringBackend):
            self._backend._module()
        return {
            "status": "ok",
            "backend": "os-keyring",
            "protection": "os-protected",
            "scope": "browser",
        }


__all__ = [
    "BrowserCredentialError",
    "BrowserCredentialReference",
    "BrowserCredentialStore",
]
