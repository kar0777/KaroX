"""Opaque provider credential references.

KaroX deliberately keeps secret values out of its JSON configuration.  Two
reference schemes exist, and both name a secret without holding one:

``os-keyring:provider/<name>``
    The default.  Delegates to Windows Credential Manager, macOS Keychain, or
    Linux Secret Service.  Backends that advertise themselves as null, failing,
    or plaintext storage are rejected.

``env:KAROX_PROVIDER_<NAME>_API_KEY``
    For hosts that have no OS keyring at all — CI runners and containers, where
    the Secret Service is simply absent.  The process environment is not
    OS-protected storage, and :meth:`CredentialStore.doctor` says so rather than
    reporting it as a keyring.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass
from typing import Callable, Optional, Protocol


KEYRING_SCHEME = "os-keyring"
ENVIRONMENT_SCHEME = "env"
# Every scheme that names a credential. Anything that promises to carry no
# credential reference -- the handoff document above all -- checks against this
# rather than against a hand-copied literal that a new scheme silently escapes.
CREDENTIAL_REFERENCE_SCHEMES: tuple[str, ...] = (KEYRING_SCHEME, ENVIRONMENT_SCHEME)

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Pinned to KaroX's own namespace: an attacker who can edit the provider
# registry must not be able to point the resolver at an unrelated secret in the
# environment (AWS_SECRET_ACCESS_KEY, GITHUB_TOKEN) and have KaroX send it to a
# base URL of their choosing.
_ENVIRONMENT_NAME = re.compile(r"^KAROX_PROVIDER_[A-Z0-9]+(?:_[A-Z0-9]+)*_API_KEY$")
_REFERENCE_PREFIX = "os-keyring:provider/"
_ENVIRONMENT_PREFIX = "env:"
_SERVICE = "KaroX/provider"
_SCHEME_HELP = (
    "credential reference must use os-keyring:provider/<name> or "
    "env:KAROX_PROVIDER_<NAME>_API_KEY"
)


class CredentialError(RuntimeError):
    """A credential operation failed without exposing the credential value."""


@dataclass(frozen=True)
class CredentialReference:
    name: str
    scheme: str = KEYRING_SCHEME

    def __post_init__(self) -> None:
        if self.scheme == KEYRING_SCHEME:
            if not isinstance(self.name, str) or _NAME.fullmatch(self.name) is None:
                raise ValueError(
                    "credential name must contain 1-128 safe alphanumeric characters"
                )
        elif self.scheme == ENVIRONMENT_SCHEME:
            if (
                not isinstance(self.name, str)
                or len(self.name) > 128
                or _ENVIRONMENT_NAME.fullmatch(self.name) is None
            ):
                raise ValueError(
                    "environment credential must be named "
                    "KAROX_PROVIDER_<NAME>_API_KEY"
                )
        else:
            raise ValueError(f"unsupported credential scheme: {self.scheme!r}")

    def __str__(self) -> str:
        if self.scheme == ENVIRONMENT_SCHEME:
            return f"{_ENVIRONMENT_PREFIX}{self.name}"
        return f"{_REFERENCE_PREFIX}{self.name}"

    @classmethod
    def parse(cls, value: str) -> "CredentialReference":
        if isinstance(value, str):
            if value.startswith(_REFERENCE_PREFIX):
                return cls(value[len(_REFERENCE_PREFIX) :])
            if value.startswith(_ENVIRONMENT_PREFIX):
                return cls(value[len(_ENVIRONMENT_PREFIX) :], ENVIRONMENT_SCHEME)
        raise ValueError(_SCHEME_HELP)


class CredentialBackend(Protocol):
    def set(self, service: str, account: str, secret: str) -> None: ...

    def get(self, service: str, account: str) -> Optional[str]: ...

    def delete(self, service: str, account: str) -> None: ...


class KeyringBackend:
    """Thin lazy wrapper around the platform ``keyring`` backend."""

    # No chaining backend may silently delegate writes to keyrings.alt/plaintext.
    _OS_BACKENDS = {
        "keyring.backends.Windows.WinVaultKeyring",
        "keyring.backends.macOS.Keyring",
    }

    @classmethod
    def _check_test_isolation(cls) -> None:
        if (
            os.environ.get("KAROX_TEST_ISOLATION", "").strip() == "1"
            and os.environ.get("KAROX_TEST_ALLOW_REAL_KEYRING", "").strip() != "1"
        ):
            # tests/_path_setup.py arms this flag for every test process. A
            # test that reaches the real OS keyring can pollute or delete a
            # developer's production credentials, so refuse by default; a
            # live-conformance test may opt in explicitly.
            raise CredentialError(
                "test isolation is active: the real OS keyring is disabled in "
                "tests. Inject a fake CredentialBackend, or set "
                "KAROX_TEST_ALLOW_REAL_KEYRING=1 to opt in explicitly."
            )

    @classmethod
    def _module(cls):
        """Select and check an OS backend; retained name for existing doctors."""
        cls._check_test_isolation()
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError as exc:
            # Reading a secret must never install software: this path runs
            # lazily, mid-run, from a credential accessor, and a package
            # manager reaching the network there is neither expected nor
            # auditable by the user who only asked for a model call.
            raise CredentialError(
                "OS credential support is unavailable. Install it with "
                "'python -m pip install keyring', or reference the key from the "
                "environment instead: env:KAROX_PROVIDER_<NAME>_API_KEY. "
                "For secure headless bridge storage, run 'karox credential setup'."
            ) from exc
        if sys.platform.startswith("linux"):
            from .credential_session import secure_linux_backend

            try:
                return secure_linux_backend()
            except CredentialError:
                raise
            except Exception as exc:
                raise CredentialError(
                    "cannot initialize Secret Service: "
                    + type(exc).__name__
                    + ". Run 'karox credential setup' for recovery."
                ) from None
        try:
            backend = keyring.get_keyring()
            identity = f"{backend.__class__.__module__}.{backend.__class__.__name__}"
            priority = float(getattr(backend, "priority", 0))
        except Exception as exc:
            raise CredentialError(
                f"cannot initialize the OS credential backend: {type(exc).__name__}"
            ) from exc
        if priority <= 0 or identity not in cls._OS_BACKENDS:
            raise CredentialError(
                "no secure OS credential backend is available; plaintext fallback "
                "is disabled. Install a Secret Service (Debian/Ubuntu: "
                "sudo apt install gnome-keyring dbus-user-session) or reference the "
                "key from the environment: env:KAROX_PROVIDER_<NAME>_API_KEY. "
                "For the guided headless flow, run 'karox credential setup'."
            )
        return backend

    def set(self, service: str, account: str, secret: str) -> None:
        try:
            self._module().set_password(service, account, secret)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError("cannot write OS credential: " + type(exc).__name__) from None

    def get(self, service: str, account: str) -> Optional[str]:
        try:
            return self._module().get_password(service, account)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError("cannot read OS credential: " + type(exc).__name__) from None

    def delete(self, service: str, account: str) -> None:
        backend = self._module()
        from keyring.errors import PasswordDeleteError

        try:
            backend.delete_password(service, account)
        except PasswordDeleteError:
            raise CredentialError("credential does not exist") from None
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError("cannot delete OS credential: " + type(exc).__name__) from None


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
        if parsed.scheme == ENVIRONMENT_SCHEME:
            return self._environment_secret(parsed)
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

    @staticmethod
    def _environment_secret(reference: CredentialReference) -> str:
        raw_value = os.environ.get(reference.name)
        # A trailing newline is the classic CI mistake (`KEY=$(cat key.txt)`)
        # and would otherwise be rejected as a control character.
        value = raw_value.strip() if isinstance(raw_value, str) else ""
        if not value:
            raise CredentialError(
                f"credential reference does not exist: {reference}"
            )
        try:
            return CredentialStore._secret(value)
        except ValueError as exc:
            raise CredentialError(
                f"environment credential is unusable: {exc}"
            ) from exc

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

    def doctor(
        self, reference: str | CredentialReference | None = None
    ) -> dict[str, str]:
        """Report how a credential is protected, without reading its value."""
        parsed = (
            reference
            if isinstance(reference, CredentialReference) or reference is None
            else CredentialReference.parse(reference)
        )
        if parsed is not None and parsed.scheme == ENVIRONMENT_SCHEME:
            return {
                # Any process the user runs can read this variable, so calling
                # it "os-keyring" would overstate what protects the key.
                "status": "ok" if os.environ.get(parsed.name, "").strip() else "missing",
                "backend": "environment",
                "protection": "process-environment",
                "reference": str(parsed),
            }
        if isinstance(self._backend, KeyringBackend):
            self._backend._module()
            if sys.platform.startswith("linux"):
                return {
                    "status": "ok", "backend": "os-keyring", "protection": "os-protected",
                    "verification": "unlocked-collection-metadata-only",
                }
        return {"status": "ok", "backend": "os-keyring", "protection": "os-protected"}
