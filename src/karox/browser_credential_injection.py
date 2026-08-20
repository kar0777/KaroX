"""Secret-safe local browser credential injection.

The external AI client is allowed to name an opaque browser credential reference
and one field (``username`` or ``password``).  The raw value is resolved inside
KaroX immediately before the owned browser element is filled and is never
returned to the caller, written to logs, or placed in a public tool argument.

This module intentionally does not expose a hosted tool by itself.  It is a
small trusted primitive that can be wired into Playwright and the managed Chrome
extension after its contracts are proven in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .browser_credentials import BrowserCredentialStore
from .credentials import CredentialError


CredentialField = Literal["username", "password"]


class BrowserCredentialInjectionError(RuntimeError):
    """Credential injection failed without exposing the credential value."""


class CredentialLocator(Protocol):
    def evaluate(self, expression: str) -> Any: ...

    def fill(self, value: str, *, timeout: int) -> Any: ...


@dataclass(frozen=True)
class BrowserCredentialInjectionResult:
    """Public result metadata; never contains the resolved credential value."""

    filled: bool
    field: CredentialField
    reference: str
    masked: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "filled": self.filled,
            "field": self.field,
            "reference": self.reference,
            "masked": self.masked,
        }


_MASK_ELEMENT_SCRIPT = r"""
el => {
  if (!(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)) {
    throw new Error('target is not a text input');
  }
  el.dataset.karoxSecret = 'true';
  el.setAttribute('data-karox-secret', 'true');
  el.setAttribute('autocomplete', 'off');
  el.style.setProperty('-webkit-text-security', 'disc', 'important');
  el.style.setProperty('text-security', 'disc', 'important');
  return true;
}
""".strip()


def inject_browser_credential(
    *,
    locator: CredentialLocator,
    credential_store: BrowserCredentialStore,
    reference: str,
    field: CredentialField,
    timeout_ms: int,
) -> BrowserCredentialInjectionResult:
    """Resolve one credential field locally, mask the element, then fill it.

    ``reference`` is safe to expose (``os-keyring:browser/<name>``); ``value`` is
    never part of the return object or an exception.  Masking is applied before
    filling so a headed browser or screenshot never shows the raw value even for
    a single frame.  The existing browser snapshot code independently treats
    password/credential-shaped inputs as secret, so this marker is defense in
    depth rather than the sole confidentiality control.
    """

    if field not in {"username", "password"}:
        raise ValueError("browser credential field must be username or password")
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
        raise ValueError("browser credential timeout_ms must be a positive integer")

    try:
        secret = credential_store.resolve_field(reference, field)
    except CredentialError as exc:
        raise BrowserCredentialInjectionError(
            "browser credential could not be resolved from secure local storage"
        ) from exc

    try:
        masked = bool(locator.evaluate(_MASK_ELEMENT_SCRIPT))
        if not masked:
            raise BrowserCredentialInjectionError("browser credential target could not be masked")
        locator.fill(secret, timeout=timeout_ms)
    except BrowserCredentialInjectionError:
        raise
    except Exception:
        # Do not chain arbitrary browser-driver exceptions: a driver, page, or
        # fake locator is allowed to include submitted values in its exception
        # text.  Suppressing the chain prevents a credential from escaping via a
        # traceback/log collector.
        raise BrowserCredentialInjectionError("browser credential injection failed") from None
    finally:
        # Drop our strong reference as soon as the synchronous fill completed.
        secret = ""

    return BrowserCredentialInjectionResult(
        filled=True,
        field=field,
        reference=reference,
        masked=True,
    )


__all__ = [
    "BrowserCredentialInjectionError",
    "BrowserCredentialInjectionResult",
    "CredentialField",
    "CredentialLocator",
    "inject_browser_credential",
]
