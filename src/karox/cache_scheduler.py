"""Cache-aware scheduling over the stable prefix, with honest economics.

The agent loop resends its transcript every step, so the provider-side
prompt cache is the single biggest cost lever we do not control directly.
This scheduler makes the lever visible and defensible:

* **Capability is evidence, not assumption.** Whether a provider actually
  serves and reports prompt-cache traffic starts UNKNOWN and is upgraded
  only by a provider-reported usage payload with a non-zero cached count.
  A zero never proves absence -- it stays exactly as unknown as it was.
* **Decisions protect reuse.** The stable prefix (system prompt + tool
  schemas) is the cacheable part; the scheduler classifies every step as
  first request, reuse, or an expected re-write after the prefix changed,
  and counts invalidations so a churning prefix is a visible defect
  instead of a silent bill.
* **Money is only computed when every component is real.** Estimates use
  the provenance-carrying :mod:`karox.provider_pricing` records; a missing
  rate makes the whole figure ``None`` (UNAVAILABLE), never a partial sum.
  Token counts in estimates are provider-reported (MEASURED); the rates
  are registry values (ESTIMATED); the result is therefore ESTIMATED.

The scheduler never disables caching to save money: advertising a cache
key costs nothing and the providers that ignore it simply ignore it.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Optional

from .cost_intelligence import CacheKey
from .provider_pricing import ModelPricing

_MTOK = 1_000_000.0


@dataclasses.dataclass(frozen=True)
class CacheCapability:
    """What is actually known about one provider/model prompt cache.

    ``None`` means UNKNOWN. Only observed provider usage reports upgrade a
    field to ``True``; nothing here ever concludes ``False``, because one
    request without cached tokens says nothing about the next.
    """

    supports_prompt_caching: Optional[bool] = None
    reports_cache_reads: Optional[bool] = None
    reports_cache_writes: Optional[bool] = None
    ttl_seconds: Optional[int] = None

    def label(self) -> str:
        """Render for surfaces that must not overstate: CONFIRMED or UNKNOWN."""

        return "CONFIRMED" if self.supports_prompt_caching else "UNKNOWN"


@dataclasses.dataclass(frozen=True)
class CacheDecision:
    """One step's scheduling classification with only defensible numbers.

    ``estimated_*`` fields are ``None`` unless every rate they need exists
    in the pricing record; they never carry a partial sum. Character sizes
    are measured in this process; the token estimate inside the prefix key
    is the documented chars/4 heuristic and is therefore an estimate.
    """

    advertise_cache_key: bool
    verdict: str
    reason: str
    prefix_sha: str
    prefix_changed: bool
    stable_prefix_token_estimate: int
    dynamic_chars: Optional[int]
    expected_reuse_steps: Optional[int]
    estimated_write_usd: Optional[float]
    estimated_saving_per_reuse_usd: Optional[float]

    VERDICT_FIRST = "FIRST_REQUEST"
    VERDICT_REUSE = "REUSE"
    VERDICT_WRITE = "WRITE_EXPECTED"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class CacheAwareScheduler:
    """Classifies prefix reuse per step and accounts cache traffic honestly.

    The scheduler is advisory and measurement-only: it never mutates the
    request beyond the cache key the kernel already sends, never blocks a
    step, and never trades quality for price. Its value is (a) counting
    prefix invalidations so churn is visible, (b) confirming capability
    from evidence, and (c) exposing the real economics when -- and only
    when -- the pricing registry actually knows the rates.
    """

    def __init__(self, *, pricing: Optional[ModelPricing] = None) -> None:
        self._pricing = pricing
        self._capability = CacheCapability()
        self._decisions = 0
        self._reuse_decisions = 0
        self._invalidations = 0
        self._cache_read_tokens = 0
        self._cache_write_tokens = 0

    # -- pricing ---------------------------------------------------------
    def set_pricing(self, pricing: Optional[ModelPricing]) -> None:
        """Follow a routed model switch; ``None`` returns to UNAVAILABLE."""

        self._pricing = pricing

    @property
    def pricing(self) -> Optional[ModelPricing]:
        return self._pricing

    # -- capability from evidence ----------------------------------------
    def observe_usage(
        self, *, cache_read_tokens: int = 0, cache_write_tokens: int = 0
    ) -> None:
        """Upgrade capability from a provider-reported usage payload.

        Positive counts are the only accepted evidence. Zeros change
        nothing: UNKNOWN stays UNKNOWN and a confirmed capability is not
        demoted by one uncached request.
        """

        read = max(0, int(cache_read_tokens))
        write = max(0, int(cache_write_tokens))
        if read > 0:
            self._cache_read_tokens += read
            self._capability = dataclasses.replace(
                self._capability,
                supports_prompt_caching=True,
                reports_cache_reads=True,
            )
        if write > 0:
            self._cache_write_tokens += write
            self._capability = dataclasses.replace(
                self._capability,
                supports_prompt_caching=True,
                reports_cache_writes=True,
            )

    @property
    def capability(self) -> CacheCapability:
        return self._capability

    # -- per-step decision -------------------------------------------------
    def decide(
        self,
        *,
        prefix_key: CacheKey,
        previous_key: Optional[CacheKey],
        dynamic_chars: Optional[int] = None,
        expected_reuse_steps: Optional[int] = None,
    ) -> CacheDecision:
        """Classify this step's prefix against the previous one.

        ``expected_reuse_steps`` is accepted only when the caller really
        knows it (it usually does not mid-run); it defaults to ``None`` =
        UNKNOWN rather than a flattering guess.
        """

        self._decisions += 1
        if previous_key is None:
            verdict = CacheDecision.VERDICT_FIRST
            reason = "no previous prefix in this run"
            changed = False
        elif previous_key.key == prefix_key.key:
            verdict = CacheDecision.VERDICT_REUSE
            reason = "stable prefix unchanged; provider may serve it from cache"
            changed = False
            self._reuse_decisions += 1
        else:
            verdict = CacheDecision.VERDICT_WRITE
            reason = (
                "stable prefix changed; the provider cannot serve the old entry"
            )
            changed = True
            self._invalidations += 1
        return CacheDecision(
            advertise_cache_key=True,
            verdict=verdict,
            reason=reason,
            prefix_sha=prefix_key.prefix_hash,
            prefix_changed=changed,
            stable_prefix_token_estimate=prefix_key.token_estimate,
            dynamic_chars=dynamic_chars,
            expected_reuse_steps=expected_reuse_steps,
            estimated_write_usd=self._estimate_write_usd(prefix_key),
            estimated_saving_per_reuse_usd=self._estimate_saving_per_reuse(
                prefix_key
            ),
        )

    # -- measured counters -------------------------------------------------
    @property
    def decisions(self) -> int:
        return self._decisions

    @property
    def reuse_decisions(self) -> int:
        return self._reuse_decisions

    @property
    def invalidations(self) -> int:
        return self._invalidations

    @property
    def cache_read_tokens_total(self) -> int:
        return self._cache_read_tokens

    @property
    def cache_write_tokens_total(self) -> int:
        return self._cache_write_tokens

    # -- economics: real or None -------------------------------------------
    def estimated_reuse_saving_usd(self) -> Optional[float]:
        """Estimated saving from provider-reported cached reads so far.

        Cached-read token counts are MEASURED (provider usage payloads).
        The rates are registry values, so the product is ESTIMATED. When
        either rate is missing, or nothing was ever served from cache,
        there is no number to show and the answer is ``None``.
        """

        if self._cache_read_tokens <= 0:
            return None
        pricing = self._pricing
        if (
            pricing is None
            or pricing.input_per_mtok is None
            or pricing.cached_input_per_mtok is None
        ):
            return None
        delta = pricing.input_per_mtok - pricing.cached_input_per_mtok
        return (self._cache_read_tokens / _MTOK) * delta

    def _estimate_write_usd(self, prefix_key: CacheKey) -> Optional[float]:
        pricing = self._pricing
        if pricing is None or pricing.cache_write_per_mtok is None:
            return None
        return (prefix_key.token_estimate / _MTOK) * pricing.cache_write_per_mtok

    def _estimate_saving_per_reuse(
        self, prefix_key: CacheKey
    ) -> Optional[float]:
        pricing = self._pricing
        if (
            pricing is None
            or pricing.input_per_mtok is None
            or pricing.cached_input_per_mtok is None
        ):
            return None
        delta = pricing.input_per_mtok - pricing.cached_input_per_mtok
        return (prefix_key.token_estimate / _MTOK) * delta


__all__ = [
    "CacheAwareScheduler",
    "CacheCapability",
    "CacheDecision",
]
