"""Separate target, tool-provider, and optional integration registries."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional


SCHEMA_VERSION = 1
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SECRET_NAME = re.compile(r"(?i)(secret|token|password|api.?key|authorization|cookie)")


@dataclass(frozen=True)
class EcosystemPreset:
    preset_id: str
    display_name: str
    status: str
    description: str
    transports: tuple[str, ...] = ()
    data_boundary: str = "external"


TARGET_PRESETS = {
    item.preset_id: item
    for item in (
        EcosystemPreset(
            "promptql", "PromptQL", "stable", "OpenAPI agent target", ("openapi",)
        ),
        EcosystemPreset(
            "notion",
            "Notion Custom Agent",
            "stable",
            "Streamable HTTP MCP target",
            ("streamable_http",),
        ),
        EcosystemPreset(
            "letaido", "letaido", "stable", "Compatibility OpenAPI target", ("openapi",)
        ),
        EcosystemPreset(
            "generic-openapi",
            "Generic OpenAPI client",
            "stable",
            "Generic OpenAPI target",
            ("openapi",),
        ),
        EcosystemPreset(
            "generic-mcp",
            "Generic Streamable HTTP MCP",
            "stable",
            "Generic MCP target",
            ("streamable_http",),
        ),
        EcosystemPreset(
            "relevance-ai",
            "Relevance AI",
            "experimental",
            "Contract must be verified per workspace",
            ("openapi",),
        ),
        EcosystemPreset(
            "dust",
            "Dust",
            "experimental",
            "Contract must be verified per workspace",
            ("mcp", "openapi"),
        ),
        EcosystemPreset(
            "hyperagent",
            "Hyperagent",
            "experimental",
            "MCP target profile",
            ("streamable_http",),
        ),
        EcosystemPreset(
            "mindstudio",
            "MindStudio",
            "documentation_required",
            "No transport is assumed without official contract",
        ),
        EcosystemPreset(
            "browser-use",
            "Browser Use",
            "documentation_required",
            "Enabled only for a documented MCP/API direction",
        ),
    )
}

TOOL_PRESETS = {
    item.preset_id: item
    for item in (
        EcosystemPreset("tavily", "Tavily", "experimental", "Web search and extract"),
        EcosystemPreset("cohere", "Cohere", "experimental", "Embeddings and reranking"),
        EcosystemPreset(
            "fal", "fal", "experimental", "Media generation and asynchronous jobs"
        ),
        EcosystemPreset(
            "browser-use",
            "Browser Use",
            "documentation_required",
            "Optional documented browser-agent jobs",
        ),
    )
}

INTEGRATION_PRESETS = {
    item.preset_id: item
    for item in (
        EcosystemPreset(
            name, display, "experimental", description, data_boundary="telemetry"
        )
        for name, display, description in (
            ("sentry", "Sentry", "Sanitized runtime error events"),
            ("sonarqube", "SonarQube Cloud", "Quality-gate summaries"),
            ("buildkite", "Buildkite", "Allowed CI workflow status"),
            ("circleci", "CircleCI", "Allowed CI workflow status"),
            ("browserstack", "BrowserStack", "Cross-browser verification jobs"),
            ("braintrust", "Braintrust", "Opt-in traces and evaluations"),
            (
                "wandb",
                "Weights & Biases (W&B)",
                "Opt-in traces, evaluations, and experiment metadata",
            ),
            ("langfuse", "Langfuse", "Opt-in traces and evaluations"),
            ("honeycomb", "Honeycomb", "Opt-in operational telemetry"),
            ("convex", "Convex", "Optional hosted session metadata"),
            ("verda", "Verda", "Optional GPU/runtime deployment profile"),
        )
    )
}


@dataclass(frozen=True)
class EcosystemRecord:
    item_id: str
    preset_id: str
    enabled: bool = False
    credential_ref: Optional[str] = None
    settings: dict[str, str] = field(default_factory=dict)
    telemetry_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value, label in ((self.item_id, "item ID"), (self.preset_id, "preset ID")):
            if not isinstance(value, str) or _ID.fullmatch(value) is None:
                raise ValueError(f"{label} is invalid")
        for name, value in self.settings.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("settings must contain text")
            if _SECRET_NAME.search(name) or _SECRET_NAME.search(value):
                raise ValueError("secrets must use an opaque credential reference")
        if len(set(self.telemetry_fields)) != len(self.telemetry_fields):
            raise ValueError("telemetry fields must be unique")


class EcosystemRegistry:
    def __init__(self, path: Path, presets: Mapping[str, EcosystemPreset]) -> None:
        self.path = path.expanduser().resolve()
        self.presets = dict(presets)

    def _load(self) -> list[EcosystemRecord]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != SCHEMA_VERSION or not isinstance(
                payload.get("items"), list
            ):
                raise ValueError("unsupported schema")
            return [EcosystemRecord(**item) for item in payload["items"]]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"cannot read {self.path.name}: {exc}") from exc

    def _save(self, records: list[EcosystemRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        value = {
            "schema_version": SCHEMA_VERSION,
            "items": [
                asdict(item) for item in sorted(records, key=lambda item: item.item_id)
            ],
        }
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def list(self) -> list[EcosystemRecord]:
        return sorted(self._load(), key=lambda item: item.item_id)

    def get(self, item_id: str) -> EcosystemRecord:
        for item in self._load():
            if item.item_id == item_id:
                return item
        raise ValueError(f"configuration does not exist: {item_id}")

    def put(self, record: EcosystemRecord) -> EcosystemRecord:
        if record.preset_id not in self.presets:
            raise ValueError(f"unknown preset: {record.preset_id}")
        records = [item for item in self._load() if item.item_id != record.item_id]
        records.append(record)
        self._save(records)
        return record

    def add(self, preset_id: str, item_id: Optional[str] = None) -> EcosystemRecord:
        if preset_id not in self.presets:
            raise ValueError(f"unknown preset: {preset_id}")
        return self.put(EcosystemRecord(item_id or preset_id, preset_id))

    def configure(
        self,
        item_id: str,
        settings: Mapping[str, str],
        credential_ref: Optional[str] = None,
        telemetry_fields: Optional[tuple[str, ...]] = None,
    ) -> EcosystemRecord:
        current = self.get(item_id)
        return self.put(
            replace(
                current,
                settings=dict(settings),
                credential_ref=credential_ref
                if credential_ref is not None
                else current.credential_ref,
                telemetry_fields=telemetry_fields
                if telemetry_fields is not None
                else current.telemetry_fields,
            )
        )

    def enable(self, item_id: str, enabled: bool) -> EcosystemRecord:
        return self.put(replace(self.get(item_id), enabled=enabled))

    def remove(self, item_id: str) -> EcosystemRecord:
        current = self.get(item_id)
        self._save([item for item in self._load() if item.item_id != item_id])
        return current

    def doctor(self, item_id: str) -> dict[str, Any]:
        item = self.get(item_id)
        preset = self.presets[item.preset_id]
        status = (
            "unconfigured"
            if preset.status == "documentation_required"
            else "ready"
            if item.enabled
            else "disabled"
        )
        return {
            "item_id": item.item_id,
            "preset": preset.preset_id,
            "status": status,
            "preset_status": preset.status,
            "network_test_performed": False,
        }
