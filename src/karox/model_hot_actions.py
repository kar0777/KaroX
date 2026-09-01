"""Hot-reload model delegation actions for the stable hosted command surface.

These actions intentionally live behind ``karox.browser.command`` so new guarded
model capabilities can be tested without rotating a durable bridge or widening
its public schema. Money authority still comes from :mod:`karox.model_grants`.
Hosted actions may consume, inspect, or revoke an existing scoped grant, but
must never mint or widen financial authority.
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from .credentials import CredentialStore
from .model_delegation_broker import (
    ModelDelegationBroker,
    OpenRouterDelegationAdapter,
)
from .model_grants import GrantScope, ModelGrantStore
from .openrouter_delegation import OpenRouterPricingResolver
from .paths import config_dir, runtime_dir
from .providers import ModelMessage, ModelRequest
from .registry import ProviderRegistry


class ModelHotActionError(RuntimeError):
    pass


def _runtime_scope(runtime: Any) -> GrantScope:
    try:
        info = dict(runtime.session_info())
    except Exception as exc:  # pragma: no cover - defensive host boundary
        raise ModelHotActionError("cannot identify hosted session") from exc
    session_id = str(info.get("session_id") or "").strip()
    if not session_id:
        raise ModelHotActionError("hosted session has no session id")
    connection_id = str(info.get("saved_profile") or "hosted-mcp").strip()
    project_id = str(getattr(runtime, "_anchor_project_id", "") or "").strip()
    if not project_id:
        repository = str(info.get("repository") or "").strip()
        project_id = Path(repository).name if repository else "hosted-project"
    return GrantScope(
        connection_id=connection_id[:256],
        session_id=session_id[:256],
        project_id=project_id[:256],
    )


def _registry() -> ProviderRegistry:
    return ProviderRegistry(config_dir() / "vnext" / "providers.json")


def _grant_store() -> ModelGrantStore:
    return ModelGrantStore(runtime_dir() / "vnext" / "model-delegation-grants.sqlite3")


def _selected_openrouter() -> tuple[ProviderRegistry, Any, Any]:
    registry = _registry()
    model = registry.selected_model()
    if model is None:
        raise ModelHotActionError("no selected model")
    provider = registry.provider(model.provider_id)
    if provider.provider_id != "openrouter":
        raise ModelHotActionError("selected model is not OpenRouter")
    if not provider.enabled:
        raise ModelHotActionError("OpenRouter provider is disabled")
    if provider.credential_ref is None:
        raise ModelHotActionError("OpenRouter has no local credential reference")
    return registry, provider, model


def _request(payload: Mapping[str, Any], *, model_id: str, deadline_seconds: float) -> ModelRequest:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ModelHotActionError("model prompt must be non-empty text")
    if len(prompt) > 500_000:
        raise ModelHotActionError("model prompt exceeds 500000 characters")
    system = payload.get("system")
    if system is not None and (not isinstance(system, str) or len(system) > 100_000):
        raise ModelHotActionError("system prompt must be text up to 100000 characters")
    raw_cap = payload.get("max_output_tokens", 8192)
    if isinstance(raw_cap, bool) or not isinstance(raw_cap, int) or not 1 <= raw_cap <= 131_072:
        raise ModelHotActionError("max_output_tokens must be between 1 and 131072")
    messages: list[ModelMessage] = []
    if isinstance(system, str) and system.strip():
        messages.append(ModelMessage("system", system))
    messages.append(ModelMessage("user", prompt))
    return ModelRequest(
        model=model_id,
        messages=tuple(messages),
        max_output_tokens=raw_cap,
        # The hosted MCP caller times out before the bridge-wide 600s default.
        # Keep delegated inference below that outer budget so a timed-out tool
        # cannot leave an invisible provider request running for minutes.
        deadline_seconds=max(5.0, min(float(deadline_seconds), 115.0)),
        reasoning_effort=(
            str(payload["reasoning_effort"])
            if payload.get("reasoning_effort") in {"low", "medium", "high", "xhigh", "max"}
            else None
        ),
    )


def _usage_dict(usage: Any) -> dict[str, Any]:
    grant = usage.grant
    return {
        "grant_id": grant.grant_id,
        "free_only": grant.free_only,
        "remaining_calls": usage.remaining_calls,
        "used_calls": usage.used_calls,
        "used_input_tokens": usage.used_input_tokens,
        "used_output_tokens": usage.used_output_tokens,
        "spent_microusd": usage.spent_microusd,
        "active_reservations": usage.active_reservations,
        "expires_at": grant.expires_at,
        "revoked": usage.revoked,
    }


def _openrouter_key_status(deadline_seconds: float) -> dict[str, Any]:
    _registry_obj, provider, _model = _selected_openrouter()
    credentials = CredentialStore()
    resolver = OpenRouterPricingResolver(
        base_url=provider.base_url,
        credential=credentials.accessor(provider.credential_ref),
        cache_seconds=60.0,
        timeout_seconds=min(10.0, provider.timeout_seconds, max(1.0, deadline_seconds)),
    )
    value = resolver._get_json("/key")
    data = value.get("data")
    if not isinstance(data, Mapping):
        raise ModelHotActionError("OpenRouter key status response is invalid")
    allowed = (
        "usage", "usage_daily", "usage_weekly", "usage_monthly",
        "limit", "limit_remaining", "limit_reset",
        "is_free_tier", "rate_limit",
    )
    safe: dict[str, Any] = {}
    for name in allowed:
        raw = data.get(name)
        if raw is None or isinstance(raw, (str, int, float, bool)):
            safe[name] = raw
        elif isinstance(raw, Mapping):
            safe[name] = {
                str(k): v for k, v in raw.items()
                if isinstance(k, str) and isinstance(v, (str, int, float, bool, type(None)))
            }
    return {"provider_id": "openrouter", "key_status": safe}


def _free_probe(deadline_seconds: float) -> dict[str, Any]:
    _registry_obj, provider, model = _selected_openrouter()
    credentials = CredentialStore()
    credential = credentials.accessor(provider.credential_ref)
    resolver = OpenRouterPricingResolver(
        base_url=provider.base_url,
        credential=credential,
        cache_seconds=60.0,
        timeout_seconds=min(10.0, provider.timeout_seconds, max(1.0, deadline_seconds)),
    )
    request = ModelRequest(
        model=model.model_id,
        messages=(ModelMessage("user", "Pricing preflight only. Do not execute this model request."),),
        max_output_tokens=8,
        deadline_seconds=max(5.0, min(deadline_seconds, 60.0)),
    )
    prepared = resolver.prepare(request)
    now = time.time()
    proof = prepared.pricing
    return {
        "provider_id": provider.provider_id,
        "model_id": model.model_id,
        "verified_free": proof.verified_free(now=now, max_age_seconds=120.0),
        "cost_upper_bound_microusd": prepared.cost_upper_bound_microusd,
        "pricing_source": proof.source,
        "pricing_observed_at": proof.observed_at,
        "billing_dimensions": [
            {"name": item.name, "unit": item.unit, "usd": item.usd}
            for item in proof.rates
        ],
        "credential_protection": credentials.doctor(provider.credential_ref),
    }


def _delegate(runtime: Any, payload: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
    grant_id = payload.get("grant_id")
    if not isinstance(grant_id, str) or not grant_id:
        raise ModelHotActionError("model.delegate requires grant_id")
    registry, provider, model = _selected_openrouter()
    requested_model = payload.get("model_id", model.model_id)
    if requested_model != model.model_id:
        model = registry.model(provider.provider_id, str(requested_model))
    request = _request(payload, model_id=model.model_id, deadline_seconds=deadline_seconds)
    store = _grant_store()
    broker = ModelDelegationBroker(
        registry=registry,
        credentials=CredentialStore(),
        grant_store=store,
        adapters=(
            OpenRouterDelegationAdapter(
                pricing_cache_seconds=60.0,
                pricing_timeout_seconds=min(10.0, provider.timeout_seconds),
                clock=time.time,
            ),
        ),
    )
    operation_id = str(payload.get("operation_id") or f"hosted-{uuid.uuid4().hex}")
    outcome = broker.complete(
        provider_id=provider.provider_id,
        request=request,
        grant_id=grant_id,
        scope=_runtime_scope(runtime),
        operation_id=operation_id,
    )
    response = outcome.response
    receipt = outcome.receipt
    return {
        "provider_id": outcome.provider_id,
        "model_id": outcome.model_id,
        "grant_id": grant_id,
        "content": response.content,
        "finish_reason": response.finish_reason,
        "response_id": response.response_id,
        "usage": dict(response.usage),
        "transport_attempts": response.transport_attempts,
        "receipt": dataclasses.asdict(receipt),
        "grant_usage": _usage_dict(outcome.grant_usage),
    }


def _benchmark_run_case(runtime: Any, payload: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
    from .model_quality_benchmark import evaluate_source, failed_check_ids, karo_system_prompt, task_prompt

    kind = payload.get("kind")
    variant = payload.get("variant")
    if kind not in {"frontend", "3d"}:
        raise ModelHotActionError("benchmark kind must be frontend or 3d")
    if variant not in {"raw", "karo"}:
        raise ModelHotActionError("benchmark variant must be raw or karo")
    cap = 800 if kind == "frontend" else 1200
    operation_id = str(payload.get("operation_id") or f"bench-{variant}-{kind}-{uuid.uuid4().hex[:8]}")
    request_payload: dict[str, Any] = {
        "grant_id": payload.get("grant_id"),
        "operation_id": operation_id,
        "model_id": "stealth/ox-alpha",
        "max_output_tokens": cap,
        "prompt": task_prompt(str(kind)),
    }
    if variant == "karo":
        request_payload["system"] = karo_system_prompt(str(kind))
        request_payload["reasoning_effort"] = "low" if kind == "frontend" else "medium"
    delegated = _delegate(runtime, request_payload, deadline_seconds)
    content = delegated.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ModelHotActionError("benchmark model returned no HTML")
    try:
        info = dict(runtime.session_info())
        repository = Path(str(info["repository"])).expanduser().resolve(strict=True)
    except Exception as exc:
        raise ModelHotActionError("cannot identify benchmark repository") from exc
    run_dir = (repository / "benchmarks" / "model_quality" / "runs").resolve()
    if repository not in run_dir.parents:
        raise ModelHotActionError("benchmark run directory escaped repository")
    run_dir.mkdir(parents=True, exist_ok=True)
    safe_op = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in operation_id)[:100]
    output = run_dir / f"{variant}-{kind}-{safe_op}.html"
    output.write_text(content, encoding="utf-8", newline="\n")
    evaluation = evaluate_source(str(kind), content)
    return {
        "variant": variant,
        "kind": kind,
        "operation_id": operation_id,
        "output_path": output.relative_to(repository).as_posix(),
        "source_bytes": len(content.encode("utf-8")),
        "static_evaluation": evaluation.to_dict(),
        "failed_check_ids": list(failed_check_ids(evaluation)),
        "usage": delegated.get("usage", {}),
        "receipt": delegated.get("receipt", {}),
        "grant_usage": delegated.get("grant_usage", {}),
        "response_id": delegated.get("response_id"),
    }


def _benchmark_repair_case(runtime: Any, payload: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
    from .model_quality_benchmark import evaluate_source, failed_check_ids, karo_system_prompt, repair_prompt

    kind = payload.get("kind")
    if kind not in {"frontend", "3d"}:
        raise ModelHotActionError("benchmark kind must be frontend or 3d")
    source_path = payload.get("source_path")
    if not isinstance(source_path, str) or not source_path:
        raise ModelHotActionError("benchmark repair requires source_path")
    try:
        info = dict(runtime.session_info())
        repository = Path(str(info["repository"])).expanduser().resolve(strict=True)
        source = (repository / source_path).resolve(strict=True)
    except Exception as exc:
        raise ModelHotActionError("cannot resolve benchmark source") from exc
    allowed_root = (repository / "benchmarks" / "model_quality" / "runs").resolve()
    if allowed_root not in source.parents or not source.is_file():
        raise ModelHotActionError("benchmark repair source must be inside benchmark runs")
    current = source.read_text(encoding="utf-8")
    evaluation = evaluate_source(str(kind), current)
    failures = failed_check_ids(evaluation)
    if not failures:
        return {
            "variant": "karo-repair",
            "kind": kind,
            "source_path": source.relative_to(repository).as_posix(),
            "skipped": True,
            "reason": "already_passes_static_acceptance",
            "static_evaluation": evaluation.to_dict(),
            "failed_check_ids": [],
        }
    cap = 800 if kind == "frontend" else 1200
    operation_id = str(payload.get("operation_id") or f"bench-karo-repair-{kind}-{uuid.uuid4().hex[:8]}")
    delegated = _delegate(
        runtime,
        {
            "grant_id": payload.get("grant_id"),
            "operation_id": operation_id,
            "model_id": "stealth/ox-alpha",
            "max_output_tokens": cap,
            "system": karo_system_prompt(str(kind)),
            "reasoning_effort": "low" if kind == "frontend" else "medium",
            "prompt": repair_prompt(str(kind), current, failures),
        },
        deadline_seconds,
    )
    content = delegated.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ModelHotActionError("benchmark repair returned no HTML")
    output = source.with_name(source.stem + "-repair.html")
    output.write_text(content, encoding="utf-8", newline="\n")
    repaired = evaluate_source(str(kind), content)
    return {
        "variant": "karo-repair",
        "kind": kind,
        "operation_id": operation_id,
        "source_path": source.relative_to(repository).as_posix(),
        "output_path": output.relative_to(repository).as_posix(),
        "source_bytes": len(content.encode("utf-8")),
        "static_evaluation": repaired.to_dict(),
        "failed_check_ids": list(failed_check_ids(repaired)),
        "usage": delegated.get("usage", {}),
        "receipt": delegated.get("receipt", {}),
        "grant_usage": delegated.get("grant_usage", {}),
        "response_id": delegated.get("response_id"),
    }


def _micro_benchmark_run_case(runtime: Any, payload: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
    from .model_micro_benchmark import evaluate, karo_system_prompt, task_prompt

    kind = payload.get("kind")
    variant = payload.get("variant")
    if kind not in {"frontend", "3d"}:
        raise ModelHotActionError("micro benchmark kind must be frontend or 3d")
    if variant not in {"raw", "karo"}:
        raise ModelHotActionError("micro benchmark variant must be raw or karo")
    cap = 420 if kind == "frontend" else 700
    operation_id = str(payload.get("operation_id") or f"micro-{variant}-{kind}-{uuid.uuid4().hex[:8]}")
    request_payload: dict[str, Any] = {
        "grant_id": payload.get("grant_id"),
        "operation_id": operation_id,
        "model_id": "stealth/ox-alpha",
        "max_output_tokens": cap,
        "prompt": task_prompt(str(kind)),
    }
    if variant == "karo":
        request_payload["system"] = karo_system_prompt(str(kind))
        request_payload["reasoning_effort"] = "low"
    delegated = _delegate(runtime, request_payload, deadline_seconds)
    content = delegated.get("content")
    if not isinstance(content, str) or not content.strip():
        return {
            "variant": variant,
            "kind": kind,
            "operation_id": operation_id,
            "usable_artifact": False,
            "static_evaluation": None,
            "usage": delegated.get("usage", {}),
            "receipt": delegated.get("receipt", {}),
            "grant_usage": delegated.get("grant_usage", {}),
            "finish_reason": delegated.get("finish_reason"),
        }
    try:
        repository = Path(str(dict(runtime.session_info())["repository"])).expanduser().resolve(strict=True)
    except Exception as exc:
        raise ModelHotActionError("cannot identify micro benchmark repository") from exc
    run_dir = (repository / "benchmarks" / "model_quality" / "runs").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    safe_op = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in operation_id)[:100]
    output = run_dir / f"micro-{variant}-{kind}-{safe_op}.html"
    output.write_text(content, encoding="utf-8", newline="\n")
    evaluation = evaluate(str(kind), content)
    return {
        "variant": variant,
        "kind": kind,
        "operation_id": operation_id,
        "usable_artifact": True,
        "output_path": output.relative_to(repository).as_posix(),
        "source_bytes": len(content.encode("utf-8")),
        "static_evaluation": evaluation.to_dict(),
        "usage": delegated.get("usage", {}),
        "receipt": delegated.get("receipt", {}),
        "grant_usage": delegated.get("grant_usage", {}),
        "finish_reason": delegated.get("finish_reason"),
        "response_id": delegated.get("response_id"),
    }


def execute_model_action(
    runtime: Any,
    action: str,
    payload: dict[str, Any],
    deadline_seconds: float,
) -> dict[str, Any]:
    if action == "model.credential.start_capture":
        from .provider_credential_capture import start_capture

        provider_id = str(payload.get("provider_id") or "openrouter")
        return start_capture(provider_id).public()
    if action == "model.credential.capture_status":
        from .provider_credential_capture import capture_status

        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise ModelHotActionError("credential capture status requires token")
        return capture_status(token).public()
    if action == "model.openrouter.key_status":
        return _openrouter_key_status(deadline_seconds)
    if action == "model.free_probe":
        return _free_probe(deadline_seconds)
    if action == "model.grant.status":
        grant_id = payload.get("grant_id")
        if not isinstance(grant_id, str) or not grant_id:
            raise ModelHotActionError("model.grant.status requires grant_id")
        usage = _grant_store().status(grant_id)
        if not usage.grant.scope.matches(_runtime_scope(runtime)):
            raise ModelHotActionError("grant is outside this hosted session scope")
        return _usage_dict(usage)
    if action == "model.grant.revoke_free_benchmark":
        grant_id = payload.get("grant_id")
        if not isinstance(grant_id, str) or not grant_id:
            raise ModelHotActionError("grant revoke requires grant_id")
        usage = _grant_store().status(grant_id)
        if not usage.grant.free_only or not usage.grant.scope.matches(_runtime_scope(runtime)):
            raise ModelHotActionError("only this session's free benchmark grant may be revoked here")
        return _usage_dict(_grant_store().revoke(grant_id, reason="benchmark_complete"))
    if action == "model.benchmark.micro_run_case":
        return _micro_benchmark_run_case(runtime, payload, deadline_seconds)
    if action == "model.benchmark.run_case":
        return _benchmark_run_case(runtime, payload, deadline_seconds)
    if action == "model.benchmark.repair_case":
        return _benchmark_repair_case(runtime, payload, deadline_seconds)
    if action == "model.delegate":
        return _delegate(runtime, payload, deadline_seconds)
    raise ModelHotActionError("unsupported model action")


__all__ = ["ModelHotActionError", "execute_model_action"]
