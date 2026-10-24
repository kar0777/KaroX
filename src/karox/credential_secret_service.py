"""Noninteractive access to an already-verified Linux Secret Service collection.

Imported only on Linux, after keyring test isolation and session checks. This
adapter is deliberately not discoverable as an automatic keyring fallback.
"""

from __future__ import annotations

from typing import Any

from jeepney import HeaderFields  # type: ignore[import-not-found,import-untyped]
from keyring.backends.SecretService import Keyring
import secretstorage  # type: ignore[import-not-found]

from .credentials import CredentialError

_RECOVERY = "Run 'karox credential setup' for consent-guided recovery."


class _PinnedConnection:
    """Pin requests to the probed owner; a restart must fail, not activate/UI."""

    def __init__(self, connection: Any, owner: str) -> None:
        self.connection = connection
        self.owner = owner

    def send_and_get_reply(self, message: Any) -> Any:
        if message.header.fields.get(HeaderFields.destination) == "org.freedesktop.secrets":
            message.header.fields[HeaderFields.destination] = self.owner
        return self.connection.send_and_get_reply(message, timeout=5.0)

    def filter(self, *args: Any, **kwargs: Any) -> Any:
        # SecretStorage uses this before executing a service-requested prompt.
        # Do not hang a background process or acquire consent implicitly.
        raise CredentialError("OS credential storage requires interactive approval. " + _RECOVERY)

    def close(self) -> None:
        self.connection.close()


class ExistingCollectionKeyring(Keyring):
    # A single class (not one registered subclass per secret read). Only KaroX's
    # checked selector constructs it; keyring discovery must never select it.
    priority = 0

    def __init__(self, address: str = "", collection: str = "", owner: str = "") -> None:
        super().__init__()
        self._address = address
        self._collection = collection
        self._owner = owner

    def set_properties_from_env(self) -> None:
        # keyring's base constructor otherwise permits arbitrary attribute
        # replacement through KEYRING_PROPERTY_*. Keep the storage schema and
        # checked collection invariant across independently launched processes.
        pass

    def get_preferred_collection(self):
        from .credential_session import _connect

        if not self._address or not self._collection or not self._owner.startswith(":"):
            raise CredentialError("Secret Service requires verified session metadata. " + _RECOVERY)
        bus = _PinnedConnection(_connect(self._address), self._owner)
        try:
            collection = secretstorage.Collection(bus, self._collection)
            if collection.is_locked():
                raise CredentialError("OS credential collection is locked. " + _RECOVERY)
            return collection
        except Exception:
            bus.close()
            raise

    def unlock(self, item):
        if item.is_locked():
            raise CredentialError("OS credential item is locked. " + _RECOVERY)
