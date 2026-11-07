"""Locator guards: what may be clicked, typed into, or read back.

The guard reads the target's metadata, refuses payment/credential surfaces,
masks secret fields, and retries once through a stale element. Split from
``browser/session`` so the refusal vocabulary is readable apart from the
Playwright lifecycle.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from .errors import BrowserError, BrowserSecurityError
from .probes import (
    ACTIVE_FIELD_METADATA_JS,
    LOCATOR_METADATA_JS,
    SECRET_FIELD_HINT as _SECRET_INPUT_HINT,
)

if TYPE_CHECKING:  # pragma: no cover
    from .contract import CancelToken


_REF_ID = re.compile(r"^k[0-9]{1,4}$")

_PAYMENT_TEXT = re.compile(
    r"(?i)\b(buy|purchase|pay|checkout|subscribe|upgrade|add credits|confirm order|"
    r"place order|complete purchase|renew|auto.?renew)\b"
)

_FREE_TRIAL_TEXT = re.compile(
    r"(?i)\b(start|begin|activate|try)\b.{0,30}\bfree trial\b|\bfree trial\b"
)

_FREE_EVIDENCE = re.compile(
    r"(?i)(\$\s*0(?:\.00)?|€\s*0(?:[,.]00)?|0\s*(?:zł|pln|usd|eur)|"
    r"no card required|without a card|card not required|bez karty)"
)

_STALE_MESSAGE = re.compile(
    r"(?i)(not attached|stale|execution context was destroyed|"
    r"element is outside of the viewport|frame was detached)"
)

_PAYMENT_CREDENTIAL_HINT = re.compile(
    r"(?i)(card|cvv|cvc|iban|\bpan\b|billing|payment)"
)


class SessionGuardsMixin:
    """Guard methods mixed into :class:`browser.session.BrowserSession`."""

    def _locator(self, payload: Mapping[str, Any], page: Any) -> Any:
        selector = payload.get("selector")
        ref = payload.get("ref")
        if ref is not None:
            if not isinstance(ref, str) or not _REF_ID.fullmatch(ref):
                raise BrowserError("ref must look like a snapshot ref (k1..k9999)")
            selector = f'[data-karox-ref="{ref}"]'
        if selector is None:
            raise BrowserError("action requires selector or ref")
        selector = self._selector(selector)
        frame = payload.get("frame")
        if frame is not None:
            return page.frame_locator(self._selector(frame)).locator(selector).first
        return page.locator(selector).first

    def _with_stale_recovery(
        self,
        work: Callable[[], Any],
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> Any:
        """Run one element action; re-anchor and retry once after DOM churn.

        Playwright locators re-resolve on their own, so true staleness shows up
        as an action that fails while the page replaces its DOM (framework
        re-render, lazy hydration). One stability wait plus retry recovers the
        common case; a second failure is reported as ``stale_element`` with the
        instruction that actually helps: take a fresh snapshot.
        """

        try:
            return work()
        except Exception as exc:  # noqa: BLE001 - classified below
            if cancel is not None:
                cancel.raise_if_cancelled()
            message = str(exc)
            if not _STALE_MESSAGE.search(message):
                raise
            page = self._active_page()
            try:
                page.wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:
                pass
            if cancel is not None:
                cancel.wait(0.15)
                cancel.raise_if_cancelled()
            else:
                time.sleep(0.15)
            try:
                return work()
            except Exception as retry_exc:  # noqa: BLE001
                if _STALE_MESSAGE.search(str(retry_exc)):
                    raise BrowserError(
                        "stale_element: the target was replaced while the action "
                        "ran and re-anchoring failed; take a fresh snapshot and retry"
                    ) from None
                raise

    def _locator_metadata(self, locator: Any) -> dict[str, str]:
        try:
            data = locator.evaluate(
                LOCATOR_METADATA_JS
            )
            return {str(k): str(v) for k, v in (data or {}).items()}
        except Exception:
            return {}

    def _assert_action_safe(self, locator: Any) -> None:
        metadata = self._locator_metadata(locator)
        if not metadata or not any(metadata.values()):
            raise BrowserSecurityError(
                "browser target could not be inspected safely; use user takeover"
            )
        text = " ".join(metadata.values())
        payment_like = bool(_PAYMENT_TEXT.search(text))
        free_trial = bool(_FREE_TRIAL_TEXT.search(text))
        if payment_like and not free_trial and not self.policy.payment_confirmation:
            raise BrowserSecurityError(
                "payment or subscription action requires explicit browser "
                "payment_confirmation permission or user takeover"
            )
        if free_trial and not self.policy.payment_confirmation:
            # Evidence must be near the control, not somewhere unrelated in the
            # page footer or another pricing card.
            evidence_text = f"{metadata.get('context', '')}\n{text}"
            if not _FREE_EVIDENCE.search(evidence_text):
                raise BrowserSecurityError(
                    "free-trial action is ambiguous: its surrounding form or "
                    "section does not clearly show zero price or no-card-required "
                    "evidence; use user takeover"
                )

    def _mask_secret_element(self, locator: Any) -> None:
        try:
            locator.evaluate(
                """el => {
                    el.dataset.karoxSecret = 'true';
                    el.setAttribute('data-karox-secret', 'true');
                    el.style.setProperty('-webkit-text-security', 'disc', 'important');
                    return true;
                }"""
            )
        except Exception:
            pass

    def _assert_agent_input_allowed(self) -> None:
        if self._ensure_open().takeover_active:
            raise BrowserSecurityError(
                "browser input is paused while the user has control; call "
                "resume_after_user_takeover after the user finishes"
            )

    def _guard_fill_target(self, locator: Any, value: str) -> dict[str, str]:
        metadata = self._locator_metadata(locator)
        if not metadata or not metadata.get("type"):
            raise BrowserSecurityError(
                "input field could not be inspected safely; use user takeover"
            )
        descriptor = " ".join(
            metadata.get(key, "") for key in ("type", "name", "id", "aria", "placeholder")
        )
        secret_field = (
            metadata.get("secret") == "true"
            or metadata.get("type") == "password"
            or bool(_SECRET_INPUT_HINT.search(descriptor))
        )
        if secret_field:
            if not self.policy.plain_credentials:
                raise BrowserSecurityError(
                    "password, token, credential, or payment fields must be completed "
                    "through user takeover or fill_credential; explicitly supplied test "
                    "credentials require the plain_credentials session permission"
                )
            if _PAYMENT_CREDENTIAL_HINT.search(descriptor):
                raise BrowserSecurityError(
                    "payment, card, CVV/CVC, IBAN, or billing fields are never filled "
                    "by the agent; use user takeover"
                )
        if metadata.get("type") == "email" and self.policy.allowed_emails:
            if value.strip().lower() not in self.policy.allowed_emails:
                raise BrowserSecurityError("email is not allowed for this browser session")
        return metadata

    def _assert_press_text_allowed(
        self, payload: Mapping[str, Any], page: Any
    ) -> None:
        """Refuse a character press aimed at a secret field.

        ``fill`` and ``type`` already refuse password/token fields unless the
        session was granted plain test credentials or the value comes from the
        OS keyring. Pressing one character reaches the same field, so the same
        decision applies here. An uninspectable focused field fails closed.
        """

        try:
            if payload.get("selector") is not None or payload.get("ref") is not None:
                metadata = self._locator_metadata(self._locator(payload, page))
            else:
                raw = page.evaluate(ACTIVE_FIELD_METADATA_JS)
                metadata = {
                    str(key): str(value) for key, value in (raw or {}).items()
                }
        except Exception as exc:
            raise BrowserSecurityError(
                "input field could not be inspected safely; use user takeover"
            ) from exc
        if not metadata:
            return
        descriptor = " ".join(
            metadata.get(key, "")
            for key in ("type", "name", "id", "aria", "placeholder")
        )
        secret_field = (
            metadata.get("secret") == "true"
            or metadata.get("type") == "password"
            or bool(_SECRET_INPUT_HINT.search(descriptor))
        )
        if not secret_field:
            return
        if not self.policy.plain_credentials:
            raise BrowserSecurityError(
                "password, token, credential, or payment fields must be completed "
                "through user takeover or fill_credential; explicitly supplied test "
                "credentials require the plain_credentials session permission"
            )
        if _PAYMENT_CREDENTIAL_HINT.search(descriptor):
            raise BrowserSecurityError(
                "payment, card, CVV/CVC, IBAN, or billing fields are never filled "
                "by the agent; use user takeover"
            )

    def _assert_action_safe_page(self, page: Any) -> None:
        """Payment guard for a page-level key press (no single element to inspect)."""

        try:
            data = page.evaluate(
                """() => {
                    const el = document.activeElement;
                    if (!el) return '';
                    const scope = el.closest('form,section,article,[role=dialog]') || document.body;
                    return ((el.innerText || el.value || '') + ' ' + (scope.innerText || '')).slice(0, 4000);
                }"""
            )
        except Exception:
            return
        text = str(data or "")
        if _PAYMENT_TEXT.search(text) and not _FREE_TRIAL_TEXT.search(text):
            if not self.policy.payment_confirmation:
                raise BrowserSecurityError(
                    "payment or subscription action requires explicit browser "
                    "payment_confirmation permission or user takeover"
                )
