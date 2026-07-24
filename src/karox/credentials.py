"""Opaque provider credential references backed by the operating system.

KaroX deliberately keeps secret values out of its JSON configuration.  The
default backend is ``keyring``, which delegates to Windows Credential Manager,
macOS Keychain, or Linux Secret Service.  Backends that advertise themselves as
null, failing, or plaintext storage are rejected.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Optional, Protocol


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REFERENCE_PREFIX = "os-keyring:provider/"
_SERVICE = "KaroX/provider"


class CredentialError(RuntimeError):
    """A credential operation failed without exposing the credential value."""


@dataclass(frozen=True)
class CredentialReference:
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _NAME.fullmatch(self.name) is None:
            raise ValueError(
                "credential name must contain 1-128 safe alphanumeric characters"
            )

    def __str__(self) -> str:
        return f"{_REFERENCE_PREFIX}{self.name}"

    @classmethod
    def parse(cls, value: str) -> "CredentialReference":
        if not isinstance(value, str) or not value.startswith(_REFERENCE_PREFIX):
            raise ValueError("credential reference must use os-keyring:provider/<name>")
        return cls(value[len(_REFERENCE_PREFIX) :])


class CredentialBackend(Protocol):
    def set(self, service: str, account: str, secret: str) -> None: ...

    def get(self, service: str, account: str) -> Optional[str]: ...

    def delete(self, service: str, account: str) -> None: ...


class KeyringBackend:
    """Thin lazy wrapper around the platform ``keyring`` backend."""

    _UNSAFE_BACKEND_MARKERS = ("fail", "null", "plaintext")

    @classmethod
    def _module(cls):
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError as exc:
            raise CredentialError(
                "OS credential support is unavailable; install the project dependencies"
            ) from exc
        try:
            backend = keyring.get_keyring()
            identity = (
                f"{backend.__class__.__module__}.{backend.__class__.__name__}"
            ).lower()
            priority = float(getattr(backend, "priority", 0))
        except Exception as exc:
            raise CredentialError(
                f"cannot initialize the OS credential backend: {type(exc).__name__}"
            ) from exc
        if priority <= 0 or any(item in identity for item in cls._UNSAFE_BACKEND_MARKERS):
            raise CredentialError(
                "no secure OS credential backend is available; plaintext fallback is disabled"
            )
        return keyring

    def set(self, service: str, account: str, secret: str) -> None:
        self._module().set_password(service, account, secret)

    def get(self, service: str, account: str) -> Optional[str]:
        return self._module().get_password(service, account)

    def delete(self, service: str, account: str) -> None:
        keyring = self._module()
        try:
            keyring.delete_password(service, account)
        except keyring.errors.PasswordDeleteError as exc:
            raise CredentialError("credential does not exist") from exc


class CredentialStore:
    def __init__(self, backend: Optional[CredentialBackend] = None) -> None:
        self._backend = backend or KeyringBackend()

    @staticmethod
    def _secret(value: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("credential value must not be empty")
        if "\x00" in value or "\r" in value or "\n" in value:
            raise ValueError("credential value contains invalid control characters")
        if len(value) > 65536:
            raise ValueError("credential value exceeds 65536 characters")
        return value

    @staticmethod
    def fingerprint(secret: str) -> str:
        digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        return f"sha256:{digest[:12]}"

    def set(self, name: str, secret: str) -> dict[str, str]:
        reference = CredentialReference(name)
        value = self._secret(secret)
        try:
            self._backend.set(_SERVICE, reference.name, value)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(
                f"cannot write OS credential: {type(exc).__name__}"
            ) from exc
        return {"reference": str(reference), "fingerprint": self.fingerprint(value)}

    def resolve(self, reference: str | CredentialReference) -> str:
        parsed = (
            reference
            if isinstance(reference, CredentialReference)
            else CredentialReference.parse(reference)
        )
        try:
            value = self._backend.get(_SERVICE, parsed.name)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(
                f"cannot read OS credential: {type(exc).__name__}"
            ) from exc
        if not isinstance(value, str) or not value:
            raise CredentialError(f"credential reference does not exist: {parsed}")
        return self._secret(value)

    def delete(self, name: str) -> dict[str, str]:
        reference = CredentialReference(name)
        try:
            self._backend.delete(_SERVICE, reference.name)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(
                f"cannot delete OS credential: {type(exc).__name__}"
            ) from exc
        return {"reference": str(reference), "status": "deleted"}

    def accessor(self, reference: str) -> Callable[[], str]:
        parsed = CredentialReference.parse(reference)

        def access() -> str:
            return self.resolve(parsed)

        return access

    def doctor(self) -> dict[str, str]:
        """Verify that the configured backend can be initialized safely."""
        if isinstance(self._backend, KeyringBackend):
            self._backend._module()
        return {"status": "ok", "backend": "os-keyring"}
