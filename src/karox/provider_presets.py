"""Declarative model-provider presets.

Presets never contain credentials and do not guess undocumented contracts.  A
preset without a base URL remains selectable, but the user must provide the
documented endpoint before it can be installed.

Three rules keep this file honest:

1. A base URL is recorded only when the provider documents it publicly.  A
   gateway whose endpoint is not documented keeps ``base_url = None`` and the
   ``endpoint_required`` status, so the picker states plainly that the user has
   to supply it instead of failing later with a confusing connection error.
2. Context and output limits are model-level facts, not provider-level ones.
   They are therefore reported as ``per_model`` here and resolved through model
   discovery into the registry, which is the only place that stores real
   numbers.
3. Ordering is part of the product: sponsor gateways stay at the top of the
   picker, and the generic, local, and first-party entries follow.  Screens and
   their tests select by position, so entries are appended rather than inserted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


_BASE_CAPABILITIES = {
    "chat_messages": "true",
    "responses_endpoint": "false",
    "streaming": "true",
    "tool_calling": "true",
    "parallel_tool_calls": "unknown",
    "structured_output": "unknown",
    "vision_images": "unknown",
    "embeddings": "false",
    "reranking": "false",
    "model_listing": "true",
    "token_usage": "true",
    "cost_data": "unknown",
    "cancellation": "true",
    "idempotency": "unknown",
    "max_context": "per_model",
}

# Per-adapter deltas over the shared contract.  Anything not listed here stays
# at the shared value, and anything genuinely unknown stays "unknown" rather
# than being optimistically promoted to "true".
_ADAPTER_CAPABILITIES = {
    "openai_compatible_chat": {
        "responses_endpoint": "unknown",
    },
    "openai_responses": {
        "responses_endpoint": "true",
        "parallel_tool_calls": "true",
        "structured_output": "true",
        "vision_images": "true",
    },
    "anthropic_messages": {
        "responses_endpoint": "false",
        "parallel_tool_calls": "true",
        "structured_output": "unknown",
        "vision_images": "true",
    },
    "gemini_generate_content": {
        "responses_endpoint": "false",
        "parallel_tool_calls": "unknown",
        "structured_output": "true",
        "vision_images": "true",
    },
}

_CAPABILITY_PROVENANCE = (
    "shared adapter contract plus documented per-adapter deltas; "
    "provider-specific unknowns remain unknown and context limits are "
    "resolved per model, not per provider"
)


@dataclass(frozen=True)
class ProviderPreset:
    preset_id: str
    display_name: str
    adapter_kind: Optional[str]
    base_url: Optional[str]
    status: str
    sponsor: bool = False
    gratitude_ru: str = "Спасибо за поддержку KaroX"
    gratitude_en: str = "Thank you for supporting KaroX"
    privacy_class: str = "public"
    privacy_note: str = ""
    contract_source: str = "user-supplied"
    setup_note: str = ""

    @property
    def installable(self) -> bool:
        return self.adapter_kind is not None

    @property
    def endpoint_known(self) -> bool:
        """True when the preset already carries a documented endpoint."""
        return self.base_url is not None

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["installable"] = self.installable
        value["endpoint_known"] = self.endpoint_known
        if self.adapter_kind is None:
            capabilities = {name: "unknown" for name in _BASE_CAPABILITIES}
        else:
            capabilities = dict(_BASE_CAPABILITIES)
            capabilities.update(_ADAPTER_CAPABILITIES.get(self.adapter_kind, {}))
        value["capabilities"] = capabilities
        value["capability_provenance"] = _CAPABILITY_PROVENANCE
        return value


@dataclass(frozen=True)
class Sponsor:
    display_name: str
    gratitude_ru: str = "Спасибо за поддержку KaroX"
    gratitude_en: str = "Thank you for supporting KaroX"


_ENDPOINT_HINT = (
    "Documented endpoint not published by the provider. Copy the base URL from "
    "your account dashboard and pass it with --base-url."
)


_PRESETS = (
    # --- Sponsor gateways -------------------------------------------------
    # Kept first on purpose: the picker leads with the gateways that support
    # the project. Do not insert entries above or between these.
    ProviderPreset(
        "routing-run",
        "routing.run",
        "openai_compatible_chat",
        None,
        "endpoint_required",
        True,
        setup_note=_ENDPOINT_HINT,
    ),
    ProviderPreset(
        "omniakey",
        "OmniaKey",
        "openai_compatible_chat",
        None,
        "endpoint_required",
        True,
        setup_note=_ENDPOINT_HINT,
    ),
    ProviderPreset(
        "apimaster",
        "APIMaster",
        "openai_compatible_chat",
        None,
        "endpoint_required",
        True,
        setup_note=_ENDPOINT_HINT,
    ),
    ProviderPreset(
        "chutes",
        "Chutes",
        "openai_compatible_chat",
        "https://llm.chutes.ai/v1",
        "experimental",
        True,
        contract_source="https://chutes.ai/docs",
    ),
    ProviderPreset(
        "empiriolabs",
        "EmpirioLabs",
        "openai_compatible_chat",
        "https://api.empiriolabs.ai/v1",
        "experimental",
        True,
        contract_source="user-verified endpoint",
    ),
    ProviderPreset(
        "puter",
        "Puter",
        None,
        None,
        "documentation_required",
        True,
        privacy_note=(
            "Specialized adapter required; no OpenAI-compatible contract is assumed."
        ),
        setup_note=(
            "No adapter implements this provider yet, so it cannot be installed. "
            "It stays listed to record that the contract is still missing."
        ),
    ),
    ProviderPreset(
        "tinfoil",
        "Tinfoil",
        "openai_compatible_chat",
        None,
        "endpoint_required",
        True,
        privacy_class="private",
        privacy_note=(
            "Confidential-inference metadata is provider-claimed and has not "
            "been independently verified by KaroX."
        ),
        setup_note=_ENDPOINT_HINT,
    ),
    ProviderPreset(
        "vivgrid",
        "Vivgrid",
        "openai_compatible_chat",
        None,
        "endpoint_required",
        True,
        setup_note=_ENDPOINT_HINT,
    ),
    ProviderPreset(
        "merge-gateway",
        "Merge Gateway",
        "openai_compatible_chat",
        None,
        "endpoint_required",
        True,
        setup_note=_ENDPOINT_HINT,
    ),
    ProviderPreset(
        "openrouter",
        "OpenRouter",
        "openai_compatible_chat",
        "https://openrouter.ai/api/v1",
        "stable",
        True,
        contract_source="https://openrouter.ai/docs/api/reference/overview",
    ),
    # --- Generic and local ------------------------------------------------
    ProviderPreset(
        "openai-compatible",
        "Generic OpenAI-compatible",
        "openai_compatible_chat",
        None,
        "stable",
        setup_note="Supply the endpoint of any OpenAI-compatible service.",
    ),
    ProviderPreset(
        "anthropic-compatible",
        "Generic Anthropic-compatible",
        "anthropic_messages",
        None,
        "stable",
        setup_note="Supply the endpoint of any Anthropic-compatible service.",
    ),
    ProviderPreset(
        "local-openai",
        "Local OpenAI-compatible",
        "openai_compatible_chat",
        "http://127.0.0.1:11434/v1",
        "stable",
        privacy_class="local",
        setup_note="Defaults to a local runtime listening on the loopback port.",
    ),
    # --- First-party model providers --------------------------------------
    # Appended so the picker works out of the box without a manual endpoint.
    # Every adapter named here is already implemented in provider_factory,
    # including the Responses and Gemini paths that previously had no preset
    # and were therefore unreachable from the interface.
    ProviderPreset(
        "openai",
        "OpenAI",
        "openai_responses",
        "https://api.openai.com/v1",
        "stable",
        contract_source="https://platform.openai.com/docs/api-reference",
    ),
    ProviderPreset(
        "anthropic",
        "Anthropic",
        "anthropic_messages",
        "https://api.anthropic.com/v1",
        "stable",
        contract_source="https://docs.anthropic.com/en/api",
    ),
    ProviderPreset(
        "gemini",
        "Google Gemini",
        "gemini_generate_content",
        "https://generativelanguage.googleapis.com/v1beta",
        "stable",
        contract_source="https://ai.google.dev/api",
    ),
    # --- Aggregating gateways ---------------------------------------------
    # One preset per gateway, not one per provider behind it. A gateway already
    # normalises hundreds of upstreams to a single OpenAI-compatible contract
    # and tracks their quotas, so copying its catalogue into this file would
    # duplicate data that goes stale the moment an upstream changes an endpoint
    # or a free tier.
    ProviderPreset(
        "omniroute",
        "OmniRoute (local gateway)",
        "openai_compatible_chat",
        "http://localhost:20128/v1",
        "stable",
        contract_source="https://github.com/diegosouzapw/OmniRoute",
        privacy_note=(
            "The gateway runs locally but forwards to whichever upstream is "
            "enabled in it, so requests still leave the machine unless only "
            "local upstreams are enabled."
        ),
        setup_note=(
            "Install and start OmniRoute, enable the upstreams you want in its "
            "dashboard, then point KaroX at this endpoint. Model discovery "
            "lists whatever the gateway currently exposes, so free tiers stay "
            "correct without being duplicated here."
        ),
    ),
)

PROVIDER_PRESETS = {item.preset_id: item for item in _PRESETS}


# Sponsors are an independent list, not a projection of the provider table.
# Deriving them from presets previously forced observability sponsors to be
# appended by hand, which is why W&B used to sit outside the generated tuple.
# A sponsor is credited here whether or not it serves models.
SPONSORS = (
    Sponsor("routing.run"),
    Sponsor("Vivgrid"),
    Sponsor("Puter"),
    Sponsor("OmniaKey"),
    Sponsor("Browser Use"),
    Sponsor("Verda"),
    Sponsor("Tinfoil"),
    Sponsor("fal"),
    Sponsor("Tavily"),
    Sponsor("Cohere"),
    Sponsor("Chutes"),
    Sponsor("EmpirioLabs"),
    Sponsor("Langfuse"),
    Sponsor("AIReiter"),
    Sponsor("Scout APM"),
    Sponsor("APIMaster"),
    Sponsor("Merge Gateway"),
    Sponsor("OpenRouter"),
    Sponsor("Weights & Biases (W&B)"),
)


def provider_presets() -> tuple[ProviderPreset, ...]:
    return _PRESETS


def provider_preset(preset_id: str) -> ProviderPreset:
    try:
        return PROVIDER_PRESETS[preset_id]
    except KeyError as exc:
        raise ValueError(f"unknown provider preset: {preset_id}") from exc


def sponsor_messages(language: str) -> tuple[str, ...]:
    return tuple(
        f"{item.display_name} — "
        + (item.gratitude_ru if language == "ru" else item.gratitude_en)
        for item in SPONSORS
    )
