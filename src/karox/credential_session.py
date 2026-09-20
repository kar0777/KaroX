"""Explicit Linux Secret Service recovery; never a plaintext fallback.

The ordinary probe does not activate a service, create a collection, unlock it,
read credentials, or persist session addresses. Recovery actions require separate
consent. A shared OS user bus, not Python memory, owns the unlocked lifecycle.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import warnings
from contextlib import closing
from typing import Any

from .credentials import CredentialError, KeyringBackend

_SERVICE = "org.freedesktop.secrets"
_BUS = "org.freedesktop.DBus"
_ROOT = "/org/freedesktop/secrets"
_RECOVERY = "Run 'karox credential setup' for consent-guided recovery."
_CHECK_SERVICE = "KaroX/credential-check"


def _linux_only() -> None:
    if not sys.platform.startswith("linux"):
        raise CredentialError("Secret Service recovery is Linux-only; use the OS keychain UI.")


def session_bus_address() -> str:
    """Use the inherited local bus or a private, same-user systemd user bus.

    Never autolaunch D-Bus or silently switch away from a stale explicit address.
    No address is saved in configuration, so reboot/socket replacement is safe.
    """
    _linux_only()
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if address is None:
        address = f"unix:path=/run/user/{int(getattr(os, 'getuid')())}/bus"
    match = re.fullmatch(r"unix:path=(/[^,;\x00\r\n%]+)(?:,guid=[0-9a-fA-F]{32})?", address)
    if match is None:
        raise CredentialError("A local unix:path D-Bus user bus is required. " + _RECOVERY)
    path = Path(match[1])
    try:
        parent = path.parent.lstat()
        socket = path.lstat()
        if (
            path.parent.resolve() != path.parent
            or parent.st_uid != int(getattr(os, "getuid")())
            or parent.st_mode & 0o077
            or not stat.S_ISDIR(parent.st_mode)
            or socket.st_uid != int(getattr(os, "getuid")())
            or not stat.S_ISSOCK(socket.st_mode)
        ):
            raise CredentialError("D-Bus socket must belong to this user in a private directory.")
    except OSError:
        raise CredentialError("No accessible OS user session bus. " + _RECOVERY) from None
    return address


def _connect(address: str) -> Any:
    # Linux-only dependencies are intentionally absent on native Windows.
    # Scoped import diagnostics cover platform availability, not runtime failures.
    from jeepney.io.blocking import (  # type: ignore[import-not-found,import-untyped]
        DBusConnection,
        get_bus,
        prep_socket,
    )

    class BoundedConnection(DBusConnection):
        def __init__(self, sock: Any) -> None:
            try:
                # The constructor's Hello uses our bounded reply method too.
                super().__init__(sock)
            except BaseException:
                selector = getattr(self, "selector", None)
                if selector is not None:
                    selector.close()
                raise

        def send_and_get_reply(self, message: Any, *, timeout: Any = None) -> Any:
            return super().send_and_get_reply(
                message, timeout=5.0 if timeout is None else min(timeout, 5.0)
            )

    sock = prep_socket(get_bus(address), timeout=2.0)
    try:
        # Bound sends as well as selector-based reply waits, including Hello.
        sock.settimeout(5.0)
        return BoundedConnection(sock)
    except BaseException:
        sock.close()
        raise


def _call(
    connection: Any,
    destination: str,
    path: str,
    interface: str,
    method: str,
    signature: str = "",
    body: tuple[Any, ...] = (),
) -> tuple[Any, ...]:
    from jeepney import (  # type: ignore[import-not-found,import-untyped]
        DBusAddress,
        DBusErrorResponse,
        MessageType,
        new_method_call,
    )

    response = connection.send_and_get_reply(
        new_method_call(DBusAddress(path, destination, interface), method, signature, body),
        timeout=5.0,
    )
    if response.header.message_type == MessageType.error:
        raise DBusErrorResponse(response)
    return tuple(response.body)


def _bus_call(
    connection: Any, method: str, signature: str = "", body: tuple[Any, ...] = ()
) -> tuple[Any, ...]:
    return _call(connection, _BUS, "/org/freedesktop/DBus", _BUS, method, signature, body)


def inspect_secret_service() -> dict[str, str]:
    """Read existing service/collection metadata, never trigger activation/UI."""
    report = {"status": "unavailable", "backend": "secret-service", "recovery": _RECOVERY}
    try:
        address = session_bus_address()
        with closing(_connect(address)) as connection:
            if not _bus_call(connection, "NameHasOwner", "s", (_SERVICE,))[0]:
                return {**report, "reason": "service-not-running"}
            # Pin read-only requests to the current owner: loss of that owner
            # must fail, not auto-activate another daemon during a doctor probe.
            owner = _bus_call(connection, "GetNameOwner", "s", (_SERVICE,))[0]
            collection = _call(
                connection, owner, _ROOT, "org.freedesktop.Secret.Service", "ReadAlias", "s", ("default",)
            )[0]
            if collection == "/":
                return {**report, "reason": "no-default-collection"}
            if collection == _ROOT + "/collection/session":
                return {**report, "reason": "non-durable-session-collection"}
            locked = _call(
                connection,
                owner,
                collection,
                "org.freedesktop.DBus.Properties",
                "Get",
                "ss",
                ("org.freedesktop.Secret.Collection", "Locked"),
            )[0]
            if locked != ("b", False):
                return {**report, "reason": "collection-locked"}
        return {
            "status": "ok",
            "backend": "secret-service",
            "protection": "os-protected",
            "verification": "unlocked-collection-metadata-only",
            "collection": collection,
            "service_owner": owner,
        }
    except CredentialError as exc:
        return {**report, "reason": "no-session-bus", "error": str(exc)}
    except Exception as exc:
        # D-Bus error payloads and backend exception messages are not safe output.
        return {**report, "reason": "service-unreachable", "error_type": type(exc).__name__}


def secure_linux_backend() -> Any:
    """Return only the native Secret Service backend, with no implicit unlock."""
    from .credential_secret_service import ExistingCollectionKeyring

    override = os.environ.get("PYTHON_KEYRING_BACKEND", "")
    if override and override != "keyring.backends.SecretService.Keyring":
        raise CredentialError(
            "Linux credentials require Secret Service; backend override refused. " + _RECOVERY
        )
    report = inspect_secret_service()
    if report["status"] != "ok":
        raise CredentialError(
            "Secure OS credential storage unavailable (" + report["reason"] + "). " + _RECOVERY
        )
    # Inherited by bridge supervisors/children. Independently launched processes
    # discover the same /run/user/<uid>/bus if no explicit address was inherited.
    os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", session_bus_address())

    return ExistingCollectionKeyring(
        session_bus_address(), report["collection"], report["service_owner"]
    )


def _consent(consent: bool) -> None:
    if not consent:
        raise CredentialError("Explicit --consent is required; no OS changes were made.")
    _linux_only()
    KeyringBackend._check_test_isolation()


def activate_secret_service(*, consent: bool = False) -> dict[str, str]:
    """Ask an existing user bus to activate its registered Secret Service."""
    _consent(consent)
    try:
        with closing(_connect(session_bus_address())) as connection:
            _bus_call(connection, "StartServiceByName", "su", (_SERVICE, 0))
    except CredentialError:
        raise
    except Exception as exc:
        raise CredentialError(
            "Secret Service activation failed: " + type(exc).__name__ + ". " + _RECOVERY
        ) from None
    return inspect_secret_service()


def unlock_secret_service(*, consent: bool = False) -> dict[str, str]:
    """Unlock/create GNOME's encrypted login keyring using a private stdin pipe.

    No shell, argv password, environment password, plaintext file, or getpass
    echo fallback. This intentionally cannot be automated through a remote agent.
    The OS daemon retains the unlock for other same-user bridge processes.
    """
    _consent(consent)
    address = session_bus_address()
    executable = Path("/usr/bin/gnome-keyring-daemon")
    try:
        info = executable.stat()
    except OSError:
        raise CredentialError(
            "GNOME Keyring is not installed; review 'karox credential setup'."
        ) from None
    if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
        raise CredentialError("Refusing a non-system or writable GNOME Keyring executable.")
    if not sys.stdin.isatty():
        raise CredentialError("Unlock requires a local interactive terminal; no password was read.")
    password = confirmation = ""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Login keyring password (never your API key): ")
            if not password or "\x00" in password or "\n" in password or "\r" in password:
                raise CredentialError("A nonempty, single-line keyring password is required.")
            confirmation = getpass.getpass(
                "Confirm keyring password (also required for recovery): "
            )
        if not hmac.compare_digest(password.encode(), confirmation.encode()):
            raise CredentialError("Keyring passwords did not match; nothing was started.")
        result = subprocess.run(
            [str(executable), "--unlock", "--components=secrets"],
            input=password.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # The daemon may outlive this CLI. Do not leak provider/bridge
            # tokens or dynamic-loader injection variables into its environment.
            env={
                **{key: os.environ[key] for key in (
                    "HOME", "USER", "LOGNAME", "LANG", "LC_ALL",
                    "XDG_RUNTIME_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
                    "GNOME_KEYRING_CONTROL", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY",
                ) if key in os.environ},
                "PATH": "/usr/bin:/bin",
                "DBUS_SESSION_BUS_ADDRESS": address,
            },
            timeout=20,
            check=False,
        )
        if result.returncode:
            raise CredentialError("GNOME Keyring unlock failed; no daemon output was exposed.")
    except (getpass.GetPassWarning, EOFError):
        raise CredentialError("A non-echoing local terminal is required for unlock.") from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise CredentialError("GNOME Keyring unlock failed: " + type(exc).__name__) from None
    finally:
        # Python strings cannot be reliably zeroed; never retain them beyond this
        # helper. No claim of locked/zeroized process memory is made.
        password = confirmation = ""
    return inspect_secret_service()


def verify_child_storage(*, consent: bool = False) -> dict[str, str]:
    """Exercise a disposable secret write/read in a new interpreter/delete."""
    _consent(consent)
    backend = KeyringBackend()
    name = "probe-" + secrets.token_hex(16)
    value = secrets.token_urlsafe(32)
    try:
        # A failed/lost reply can still mean the write committed. Always try
        # to remove this random disposable name, even when set() raises.
        backend.set(_CHECK_SERVICE, name, value)
        child = subprocess.run(
            [sys.executable, "-m", "karox.credential_session", "--verify-child", name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
            env=dict(os.environ),
        )
        expected = hashlib.sha256(value.encode()).hexdigest().encode()
        if child.returncode or not hmac.compare_digest(child.stdout.strip(), expected):
            raise CredentialError(
                "A fresh background process could not read secure storage. " + _RECOVERY
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CredentialError(
            "Background credential verification failed: " + type(exc).__name__
        ) from None
    finally:
        try:
            backend.delete(_CHECK_SERVICE, name)
        except Exception:
            raise CredentialError(
                "Credential check cleanup failed; remove disposable item "
                + _CHECK_SERVICE
                + "/"
                + name
                + " in your OS keyring UI."
            ) from None
    return {
        "status": "ok",
        "backend": "secret-service",
        "protection": "os-protected",
        "verification": "write-child-read-delete",
        "cleanup": "complete",
    }


def setup_plan() -> dict[str, Any]:
    """Return facts and user-operated recovery, with no OS mutations."""
    if not sys.platform.startswith("linux"):
        return {
            "status": "manual-setup",
            "instructions": [
                "Use Windows Credential Manager or macOS Keychain; unlock through the OS UI."
            ],
        }
    return {
        **inspect_secret_service(),
        "consent_required": True,
        "instructions": [
            "Debian/Ubuntu: ask your administrator to review installing dbus-user-session "
            "and gnome-keyring; KaroX will not install packages or alter PAM/login settings.",
            "Log in as the bridge's OS user with a persistent user session bus. "
            "Do not use sudo karox. See docs/TROUBLESHOOTING.md for session lifetimes.",
            "To activate a registered service on that bus: karox credential setup --activate --consent",
            "For a local hidden-password GNOME login-keyring unlock/create: "
            "karox credential setup --unlock --consent (never use an empty password).",
            "Verify disposable write, independent child read, and cleanup: "
            "karox credential setup --verify --consent",
            "Run karox credential doctor, then restart failed bridges as the same OS user. "
            "No environment-token or plaintext fallback is enabled by setup.",
        ],
    }


def _main() -> int:
    # Internal subprocess protocol: only a random probe account is accepted.
    if (
        len(sys.argv) != 3
        or sys.argv[1] != "--verify-child"
        or not re.fullmatch(r"probe-[0-9a-f]{32}", sys.argv[2])
    ):
        return 2
    try:
        value = KeyringBackend().get(_CHECK_SERVICE, sys.argv[2])
        if not value:
            return 1
        print(hashlib.sha256(value.encode()).hexdigest())
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
