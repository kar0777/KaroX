"""The `provider` and `model` command groups: presets, connection, catalog, credentials and registry edits."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import re
import shlex
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional
from .cli_common import (
    emit as _emit,
    json_output_value as _json,
    write as _write,
)
from .credentials import CredentialError, CredentialStore
from .provider_adapters import (
    AnthropicMessagesProvider,
    GeminiGenerateContentProvider,
    OpenAIResponsesProvider,
)
from .provider_presets import provider_preset, provider_presets
from .providers import (
    ModelMessage,
    ModelRequest,
    OpenAIChatCompletionsProvider,
)
from .registry import (
    ModelRecord,
    ProviderRecord,
    RegistryError,
)

from .cli_context import _controller, _registry
from .cli_present import _pairs, _pricing

def _probe_candidate(
    adapter_kind: str,
    base_url: str,
    model_id: str,
    *,
    credential: Any,
    timeout_seconds: float,
) -> dict[str, Any]:
    constructors = {
        "openai_compatible_chat": OpenAIChatCompletionsProvider,
        "openai_responses": OpenAIResponsesProvider,
        "anthropic_messages": AnthropicMessagesProvider,
        "gemini_generate_content": GeminiGenerateContentProvider,
    }
    try:
        constructor = constructors[adapter_kind]
    except KeyError as exc:
        raise ValueError(f"unsupported provider adapter: {adapter_kind}") from exc
    runtime: Any = constructor(
        base_url,
        credential=credential,
        timeout_seconds=min(60.0, timeout_seconds),
    )
    try:
        response = runtime.complete(
            ModelRequest(
                model=model_id,
                messages=(ModelMessage("user", "Reply with exactly OK."),),
                max_output_tokens=8,
                deadline_seconds=min(60.0, timeout_seconds),
            )
        )
    finally:
        close = getattr(runtime, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()
    return {
        "status": "ok",
        "provider_id": getattr(runtime, "endpoint", ""),
        "model_id": model_id,
        "finish_reason": response.finish_reason,
        "usage": response.usage,
        "transport_attempts": response.transport_attempts,
    }


def _privacy_class(base_url: str, fallback: str) -> str:
    from urllib.parse import urlsplit

    host = (urlsplit(base_url).hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return "local"
    return fallback


def _handle_provider_setup(args: argparse.Namespace) -> int:
    preset = provider_preset(args.preset_id)
    if not preset.installable:
        raise ValueError(
            f"provider preset requires a specialized adapter: {preset.display_name}"
        )
    base_url = (args.base_url or preset.base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError(f"{preset.display_name} requires --base-url")
    provider_id = (args.provider_id or preset.preset_id).strip()
    model_id = args.model.strip()
    if not model_id:
        raise ValueError("--model is required")
    controller = _controller()
    existing_reference: Optional[str] = None
    with contextlib.suppress(RegistryError, ValueError):
        existing_reference = controller.details(provider_id).provider.credential_ref
    secret: Optional[str] = None
    probe_secret: Optional[str] = None
    credential_ref = args.credential_ref or existing_reference
    if args.stdin_key:
        secret = sys.stdin.readline().rstrip("\r\n")
        if not secret.strip():
            raise ValueError("stdin did not contain an API key")
        probe_secret = secret
    elif args.credential_ref:
        probe_secret = CredentialStore().resolve(args.credential_ref)
    elif existing_reference:
        with contextlib.suppress(CredentialError):
            probe_secret = CredentialStore().resolve(existing_reference)
    privacy = _privacy_class(base_url, preset.privacy_class)
    is_local = privacy == "local"
    if not is_local and not (probe_secret or credential_ref):
        raise ValueError(
            "remote provider setup requires --stdin-key, --credential-ref, "
            "or an existing saved credential"
        )
    verification: dict[str, Any] = {"status": "skipped"}
    if not args.no_test:
        if probe_secret is None and not is_local:
            raise ValueError(
                "a live test requires --stdin-key, --credential-ref, or a saved credential"
            )
        if probe_secret is None:
            verification = _probe_candidate(
                str(preset.adapter_kind),
                base_url,
                model_id,
                credential=None,
                timeout_seconds=60.0,
            )
        else:
            key = probe_secret
            verification = _probe_candidate(
                str(preset.adapter_kind),
                base_url,
                model_id,
                credential=lambda: key,
                timeout_seconds=60.0,
            )
    provider = ProviderRecord(
        provider_id=provider_id,
        adapter_kind=str(preset.adapter_kind),
        base_url=base_url,
        credential_ref=credential_ref,
        privacy_class=privacy,
    )
    model = ModelRecord(
        provider_id=provider_id,
        model_id=model_id,
        context_window=args.context_window,
        max_output_tokens=args.max_output_tokens,
        tools="true",
        streaming="true",
        pricing=_pricing(args),
        provenance=f"preset-setup:{preset.preset_id}",
    )
    mutation = controller.configure_provider_model(
        provider,
        model,
        secret=secret,
        activate=not args.no_activate,
    )
    payload = {
        **mutation.to_dict(),
        "preset": preset.preset_id,
        "verification": verification,
    }
    _emit(payload, json_output=args.json)
    return 0


def _handle_provider_catalog(args: argparse.Namespace) -> int:
    from .provider_catalog import provider_catalog
    from .provider_connect import connected_catalog_ids

    catalog = provider_catalog()
    connected = connected_catalog_ids(_registry(), catalog)
    if args.provider:
        entry = catalog.provider(args.provider)
        payload = entry.to_dict(include_models=True)
        payload["connected_as"] = connected.get(entry.provider_id)
        payload["snapshot"] = catalog.fetched_at
        if args.json:
            _emit(payload, json_output=True)
            return 0
        print(f"{entry.display_name} ({entry.provider_id}) - models.dev snapshot {catalog.fetched_at}")
        print(f"  endpoint: {entry.base_url or '-'}")
        print(f"  key: {', '.join(entry.env) or '-'}")
        if entry.unsupported_reason:
            print(f"  cannot connect: {entry.unsupported_reason}")
        for item in entry.models:
            price = (
                f"${item.input_per_million:g}/${item.output_per_million:g} per 1M"
                if item.priced else "no published price"
            )
            context = f"{item.context_window // 1000}k" if item.context_window else "?"
            flag = " (deprecated)" if item.deprecated else ""
            print(f"  {item.model_id:<48} {context:>6}  {price}{flag}")
        return 0
    rows = []
    for entry in catalog.search(args.query, installable_only=not args.all):
        row = entry.to_dict()
        row["connected_as"] = connected.get(entry.provider_id)
        rows.append(row)
    if args.json:
        _emit(rows, json_output=True)
        return 0
    print(f"models.dev snapshot {catalog.fetched_at}: {len(rows)} providers")
    for row in rows:
        state = "connected" if row["connected_as"] else (
            "ready" if row["installable"] else "unsupported"
        )
        note = row["unsupported_reason"] or ", ".join(row.get("key_env", row["env"])[:1])
        print(f"  {row['provider_id']:<28} {state:<11} {row['model_count']:>4} models  {note}")
    print("Connect one with: karox provider connect <id>")
    return 0


def _endpoint_variables(entry: Any, supplied: dict[str, str]) -> dict[str, str]:
    """``--var`` values, completed from same-named environment variables.

    The catalog names endpoint placeholders after the provider's own
    conventional variables (``AZURE_RESOURCE_NAME``), so an environment that is
    already configured for that provider needs no ``--var`` at all.
    """

    values = dict(supplied)
    for name in entry.url_variables:
        if not values.get(name) and os.environ.get(name, "").strip():
            values[name] = os.environ[name].strip()
    return values


def _handle_provider_connect(args: argparse.Namespace) -> int:
    from .provider_catalog import provider_catalog
    from .provider_connect import ConnectError, connect_from_catalog

    catalog = provider_catalog()
    entry = catalog.provider(args.catalog_id)
    controller = _controller()
    registry_id = (args.provider_id or entry.provider_id).strip()
    existing: Optional[ProviderRecord] = None
    with contextlib.suppress(RegistryError, ValueError):
        existing = controller.registry.provider(registry_id)
    secret: Optional[str] = None
    credential_ref: Optional[str] = args.credential_ref
    if args.stdin_key:
        secret = sys.stdin.readline().strip()
        if not secret:
            raise ValueError("stdin did not contain an API key")
    elif args.env_key:
        # Runtime references stay in KaroX's own namespace (see credentials.py);
        # the provider's conventional variable is imported once into the keyring.
        own = "KAROX_PROVIDER_" + re.sub(r"[^A-Z0-9]+", "_", registry_id.upper()).strip("_") + "_API_KEY"
        variable = next((name for name in entry.key_env if os.environ.get(name)), None)
        if os.environ.get(own):
            credential_ref = f"env:{own}"
        elif variable is not None:
            secret = os.environ[variable].strip()
        else:
            raise ValueError(
                "none of these environment variables is set: "
                + ", ".join([own, *entry.key_env])
            )
    elif not credential_ref and not (existing and existing.credential_ref) and entry.env:
        if not sys.stdin.isatty():
            raise ValueError("pass --stdin-key, --credential-ref or --env-key")
        secret = getpass.getpass(f"{entry.display_name} API key: ").strip()
        if not secret:
            raise ValueError("no API key entered")
    try:
        result = connect_from_catalog(
            controller,
            entry.provider_id,
            secret=secret,
            credential_ref=credential_ref,
            provider_id=registry_id,
            base_url=args.base_url,
            variables=_endpoint_variables(entry, _pairs(args.var, "var")),
            model=args.model,
            include_deprecated=bool(args.include_deprecated),
            test=not args.no_test,
            activate=False if args.no_activate else None,
            catalog=catalog,
            probe=_probe_candidate,
        )
    except ConnectError as exc:
        raise ValueError(str(exc)) from None
    payload = result.to_dict()
    if args.json:
        _emit(payload, json_output=True)
        return 0
    kept = (
        f"; {result.models_kept} already configured kept as entered"
        if result.models_kept
        else ""
    )
    print(
        f"Connected {result.display_name} as '{result.provider_id}': "
        f"{result.models_installed} models ({result.priced_models} with published prices{kept})."
    )
    if result.activated:
        print(f"Selected model: {result.provider_id}/{result.model_id}")
    else:
        print(f"Select a model with: karox model select {result.provider_id} <model>")
    return 0


def _handle_provider_catalog_sync(args: argparse.Namespace) -> int:
    from .provider_connect import sync_from_catalog

    payload = sync_from_catalog(_registry(), args.provider_ids or ())
    _emit(payload, json_output=args.json)
    return 0


def _program_name() -> str:
    """``karox`` or the suffixed command this copy was started as (``karox6``)."""

    stem = Path(sys.argv[0] or "").stem.lower() if sys.argv else ""
    return stem if re.fullmatch(r"karox[0-9a-z_-]*", stem) else "karox"


def _handle_provider_local(args: argparse.Namespace) -> int:
    import subprocess

    from .local_runtimes import KNOWN_RUNTIMES, scan_local_runtimes

    found = scan_local_runtimes()
    if args.json:
        _json({"found": [item.to_dict() for item in found]})
        return 0
    if not found:
        ports = ", ".join(f"{kind.display_name} {kind.port}" for kind in KNOWN_RUNTIMES)
        _write(f"No local model server answered on 127.0.0.1 ({ports}).\n")
        _write(f"Start one, then run: {_program_name()} provider local\n")
        return 0
    for item in found:
        guessed = " (by port)" if item.identified_by == "port" else ""
        listed = ", ".join(item.models[:5]) + (" ..." if len(item.models) > 5 else "")
        _write(f"{item.display_name}{guessed}: {item.base_url}\n")
        _write(f"  models ({len(item.models)}): {listed or 'none loaded'}\n")
        argv = item.setup_argv(program=_program_name())
        line = subprocess.list2cmdline(argv) if os.name == "nt" else " ".join(shlex.quote(part) for part in argv)
        _write("  connect: " + line + "\n")
    return 0


def _handle_provider(args: argparse.Namespace) -> int:
    controller = _controller()
    command = args.provider_command
    if command == "presets":
        _emit([item.to_dict() for item in provider_presets()], json_output=args.json)
        return 0
    if command == "setup":
        return _handle_provider_setup(args)
    if command == "local":
        return _handle_provider_local(args)
    if command == "catalog":
        return _handle_provider_catalog(args)
    if command == "connect":
        return _handle_provider_connect(args)
    if command == "catalog-sync":
        return _handle_provider_catalog_sync(args)
    if command == "add":
        provider = ProviderRecord(
            provider_id=args.provider_id,
            adapter_kind=args.adapter,
            base_url=args.base_url,
            credential_ref=args.credential_ref,
            privacy_class=args.privacy_class,
            timeout_seconds=args.timeout_seconds,
            max_transport_retries=args.max_transport_retries,
            headers=_pairs(args.header, "header"),
            query=_pairs(args.query, "query"),
        )
        _emit(controller.put_provider(provider).to_dict(), json_output=args.json)
        return 0
    if command == "edit":
        mutation = controller.edit_provider(
            args.provider_id,
            adapter_kind=args.adapter,
            base_url=args.base_url,
            credential_ref=None if args.clear_credential else args.credential_ref,
            credential_ref_supplied=bool(args.credential_ref or args.clear_credential),
            headers=_pairs(args.header, "header") if args.header else None,
            query=_pairs(args.query, "query") if args.query else None,
            privacy_class=args.privacy_class,
            timeout_seconds=args.timeout_seconds,
            max_transport_retries=args.max_transport_retries,
        )
        _emit(mutation.to_dict(), json_output=args.json)
        return 0
    if command == "remove":
        mutation = controller.remove_provider(
            args.provider_id,
            cascade=bool(args.cascade),
            delete_credential=bool(args.delete_credential),
        )
        _emit(mutation.to_dict(), json_output=args.json)
        return 0
    if command == "list":
        _emit([item.to_dict() for item in controller.list()], json_output=args.json)
        return 0
    if command in {"show", "details"}:
        _emit(controller.details(args.provider_id).to_dict(), json_output=args.json)
        return 0
    if command == "test":
        _emit(
            controller.test_provider(args.provider_id, model_or_alias=args.model),
            json_output=args.json,
        )
        return 0
    if command == "credential-set":
        secret = (
            sys.stdin.readline().rstrip("\r\n")
            if args.stdin
            else getpass.getpass("Provider credential: ")
        )
        mutation = controller.set_credential(
            args.provider_id, secret, credential_name=args.name
        )
        _emit(mutation.to_dict(), json_output=args.json)
        return 0
    if command != "credential-clear":
        raise ValueError(f"unsupported provider command: {command}")
    mutation = controller.clear_credential(
        args.provider_id, delete_stored=bool(args.delete_stored)
    )
    _emit(mutation.to_dict(), json_output=args.json)
    return 0


def _handle_model(args: argparse.Namespace) -> int:
    controller = _controller()
    registry = controller.registry
    command = args.model_command
    if command == "list":
        selected = registry.selected_model()
        payload = []
        for item in registry.models(args.provider):
            value = asdict(item)
            value["selected"] = selected is not None and (
                item.provider_id,
                item.model_id,
            ) == (selected.provider_id, selected.model_id)
            payload.append(value)
        _emit(payload, json_output=args.json)
        return 0
    if command == "inspect":
        _emit(asdict(registry.model(args.provider_id, args.model)), json_output=args.json)
        return 0
    if command == "select":
        _emit(
            controller.select_model(args.provider_id, args.model).to_dict(),
            json_output=args.json,
        )
        return 0
    if command == "add":
        record = ModelRecord(
            provider_id=args.provider_id,
            model_id=args.model_id,
            aliases=tuple(args.alias),
            context_window=args.context_window,
            max_output_tokens=args.max_output_tokens,
            tools=args.tools,
            vision=args.vision,
            structured_output=args.structured_output,
            streaming=args.streaming,
            pricing=_pricing(args),
            provenance=args.provenance,
        )
        _emit(controller.put_model(record).to_dict(), json_output=args.json)
        return 0
    if command == "discover":
        from .model_discovery import discover_models_result, model_record

        provider = registry.provider(args.provider_id)
        result = discover_models_result(
            provider,
            controller.credentials,
            timeout_seconds=args.timeout_seconds,
        )
        if result.base_url != provider.base_url:
            controller.edit_provider(provider.provider_id, base_url=result.base_url)
        saved: list[str] = []
        if args.save:
            existing = {
                model.model_id for model in registry.models(provider.provider_id)
            }
            for discovered in result.models:
                if discovered.model_id in existing:
                    continue
                controller.put_model(model_record(provider.provider_id, discovered))
                saved.append(discovered.model_id)
        _emit(
            {
                "provider_id": provider.provider_id,
                "base_url": result.base_url,
                "models": [item.as_dict() for item in result.models],
                "saved": saved,
                "saved_count": len(saved),
            },
            json_output=args.json,
        )
        return 0
    if command == "map":
        _emit(
            controller.map_alias(args.provider_id, args.alias, args.model).to_dict(),
            json_output=args.json,
        )
        return 0
    if command == "repair-selection":
        repaired = controller.repair_selection(
            preferred_provider=args.preferred_provider
        )
        _emit(
            {
                "status": "selected" if repaired is not None else "no_model",
                "model": asdict(repaired) if repaired is not None else None,
            },
            json_output=args.json,
        )
        return 0
    if command == "remove":
        _emit(
            controller.remove_model(args.provider_id, args.model).to_dict(),
            json_output=args.json,
        )
        return 0
    if command != "test":
        raise ValueError(f"unsupported model command: {command}")
    _emit(
        controller.test_provider(args.provider_id, model_or_alias=args.model),
        json_output=args.json,
    )
    return 0
