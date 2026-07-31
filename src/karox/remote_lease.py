"""Short-lived Ellipsis-to-KaroX credential leases.

The bearer value and remote URL live only in the operating-system keyring.
Durable JSON contains binding metadata and a fingerprint, never either value.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from .bridge import BridgeCredentialStore
from .credentials import CredentialBackend, CredentialError, KeyringBackend
from .managed_process_cleanup import stop_session_browser, stop_session_processes
from .paths import runtime_dir


_CONNECTION_SERVICE = "KaroX/ellipsis-connection"


class EllipsisLeaseError(RuntimeError):
    """A lease is missing, expired, revoked, or bound to another session."""


@dataclass(frozen=True)
class EllipsisCredentialLease:
    schema_version: int
    credential_name: str
    fingerprint: str
    local_session_id: str
    ellipsis_session_id: str
    repository_digest: str
    access_profile: str
    issued_at: float
    expires_at: float
    revoked_at: Optional[float] = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None and time.time() < self.expires_at

    def public_dict(self) -> dict[str, Any]:
        return {**asdict(self), "active": self.active}


def repository_digest(repository: Path) -> str:
    resolved = repository.expanduser().resolve(strict=True)
    return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()


class EllipsisLeaseStore:
    """Credential value in keyring, binding and expiry in restrictive JSON."""

    def __init__(
        self,
        root: Optional[Path] = None,
        *,
        backend: Optional[CredentialBackend] = None,
    ) -> None:
        self.root = (root or runtime_dir() / "vnext" / "ellipsis" / "leases").resolve()
        self._credentials = BridgeCredentialStore(backend)

    def path(self, credential_name: str) -> Path:
        if (
            not credential_name
            or len(credential_name) > 64
            or any(
                not (character.isalnum() or character in "._-")
                for character in credential_name
            )
        ):
            raise ValueError("credential lease name is malformed")
        return self.root / f"{credential_name}.json"

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def mint(
        self,
        *,
        credential_name: str,
        local_session_id: str,
        ellipsis_session_id: str,
        repository: Path,
        access_profile: str,
        ttl_seconds: float,
    ) -> tuple[EllipsisCredentialLease, str]:
        if not 60.0 <= float(ttl_seconds) <= 86_400.0:
            raise ValueError("credential TTL must be between 60 and 86400 seconds")
        if not local_session_id or not ellipsis_session_id:
            raise ValueError("credential lease requires both session IDs")
        issued = time.time()
        generated = self._credentials.set(credential_name)
        secret = generated.get("secret")
        if not isinstance(secret, str) or not secret:
            raise EllipsisLeaseError("credential generator returned no secret")
        lease = EllipsisCredentialLease(
            schema_version=1,
            credential_name=credential_name,
            fingerprint=self._credentials.fingerprint(secret),
            local_session_id=local_session_id,
            ellipsis_session_id=ellipsis_session_id,
            repository_digest=repository_digest(repository),
            access_profile=access_profile,
            issued_at=issued,
            expires_at=issued + float(ttl_seconds),
        )
        try:
            self._write_json(self.path(credential_name), asdict(lease))
        except Exception:
            try:
                self._credentials.delete(credential_name)
            except Exception:
                pass
            raise
        return lease, secret

    def load(self, credential_name: str) -> EllipsisCredentialLease:
        path = self.path(credential_name)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise EllipsisLeaseError("credential lease does not exist") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise EllipsisLeaseError("credential lease metadata is unreadable") from exc
        if not isinstance(payload, dict):
            raise EllipsisLeaseError("credential lease metadata is malformed")
        try:
            return EllipsisCredentialLease(**payload)
        except (TypeError, ValueError) as exc:
            raise EllipsisLeaseError("credential lease metadata is malformed") from exc

    def validate(
        self,
        credential_name: str,
        *,
        local_session_id: Optional[str] = None,
        ellipsis_session_id: Optional[str] = None,
        repository: Optional[Path] = None,
        access_profile: Optional[str] = None,
    ) -> EllipsisCredentialLease:
        lease = self.load(credential_name)
        if lease.revoked_at is not None:
            raise EllipsisLeaseError("credential lease has been revoked")
        if time.time() >= lease.expires_at:
            raise EllipsisLeaseError("credential lease has expired")
        checks = (
            ("local session", local_session_id, lease.local_session_id),
            ("Ellipsis session", ellipsis_session_id, lease.ellipsis_session_id),
            ("access profile", access_profile, lease.access_profile),
        )
        for label, expected, actual in checks:
            if expected is not None and expected != actual:
                raise EllipsisLeaseError(f"credential lease is bound to another {label}")
        if repository is not None and repository_digest(repository) != lease.repository_digest:
            raise EllipsisLeaseError("credential lease is bound to another repository")
        return lease

    def resolve(
        self,
        credential_name: str,
        *,
        local_session_id: Optional[str] = None,
        repository: Optional[Path] = None,
        access_profile: Optional[str] = None,
    ) -> str:
        self.validate(
            credential_name,
            local_session_id=local_session_id,
            repository=repository,
            access_profile=access_profile,
        )
        try:
            return self._credentials.resolve(f"os-keyring:bridge/{credential_name}")
        except CredentialError as exc:
            raise EllipsisLeaseError("credential value is unavailable") from exc

    def revoke(self, credential_name: str) -> EllipsisCredentialLease:
        lease = self.load(credential_name)
        revoked = EllipsisCredentialLease(
            **{
                **asdict(lease),
                "revoked_at": lease.revoked_at or time.time(),
            }
        )
        self._write_json(self.path(credential_name), asdict(revoked))
        try:
            self._credentials.delete(credential_name)
        except Exception:
            pass
        # Revocation ends the remote agent's authority. Any process started under
        # that authority is stopped as part of the same lifecycle transition.
        stop_session_processes(lease.local_session_id)
        # The browser session is in-process on the bridge, but call the hook
        # anyway so the lifecycle is symmetric and a future out-of-process
        # browser would be covered here without a second edit.
        stop_session_browser(lease.local_session_id)
        return revoked


class EllipsisConnectionStore:
    """OS-keyring storage for the URL needed by detach/attach."""

    def __init__(self, backend: Optional[CredentialBackend] = None) -> None:
        self._backend = backend or KeyringBackend()

    @staticmethod
    def _account(local_session_id: str) -> str:
        if (
            not local_session_id
            or len(local_session_id) > 128
            or any(
                not (character.isalnum() or character in "._-")
                for character in local_session_id
            )
        ):
            raise ValueError("local session ID is malformed")
        return local_session_id

    def set(self, local_session_id: str, remote_url: str) -> None:
        account = self._account(local_session_id)
        if (
            not isinstance(remote_url, str)
            or not remote_url.startswith("https://")
            or any(character in remote_url for character in "\r\n\0")
        ):
            raise ValueError("remote URL must be a valid HTTPS URL")
        try:
            self._backend.set(_CONNECTION_SERVICE, account, remote_url)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(
                f"cannot store Ellipsis connection data: {type(exc).__name__}"
            ) from exc

    def get(self, local_session_id: str) -> str:
        account = self._account(local_session_id)
        try:
            value = self._backend.get(_CONNECTION_SERVICE, account)
        except CredentialError as exc:
            raise EllipsisLeaseError(
                "Ellipsis connection data is unavailable"
            ) from exc
        except Exception as exc:
            raise EllipsisLeaseError(
                f"cannot read Ellipsis connection data: {type(exc).__name__}"
            ) from exc
        if not isinstance(value, str) or not value.startswith("https://"):
            raise EllipsisLeaseError("Ellipsis connection data does not exist")
        return value

    def delete(self, local_session_id: str) -> None:
        account = self._account(local_session_id)
        try:
            self._backend.delete(_CONNECTION_SERVICE, account)
        except Exception:
            pass
