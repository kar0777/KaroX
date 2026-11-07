"""Constructors for the durable registries read by the command surface."""

from __future__ import annotations

from .credentials import CredentialStore
from .mcp_client import McpRegistry
from .paths import config_dir
from .provider_controller import ProviderController
from .registry import ProviderRegistry


def _registry() -> ProviderRegistry:
    return ProviderRegistry(config_dir() / "vnext" / "providers.json")


def _controller() -> ProviderController:
    return ProviderController(registry=_registry(), credentials=CredentialStore())


def _mcp_registry() -> McpRegistry:
    return McpRegistry(config_dir() / "vnext" / "mcp-servers.json")
