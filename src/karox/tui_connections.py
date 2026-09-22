"""Connections screens for the KaroX TUI.

These screens live behind ``/connect`` and give the user a single place to
manage both connection families KaroX keeps distinct:

* **MCP Clients** (Connections → MCP Clients) -- external clients that connect
  *into* KaroX's bridge MCP server (ClickUp, Notion, ChatGPT Web, Claude Web,
  PromptQL, a generic Streamable HTTP client, or a custom one). Backed by
  :class:`~karox.connections.ConnectionRegistry`.
* **Model Providers** (Connections → Model Providers) -- APIs that serve an LLM
  (OpenAI, Anthropic, OpenRouter, a local model, or a custom OpenAI /
  Anthropic-compatible endpoint). Backed by the existing
  :class:`~karox.registry.ProviderRegistry` + :class:`~karox.credentials.CredentialStore`,
  surfaced here so they can be listed, edited, deleted, tested, and set active
  without a CLI.

The screens are Textual ``ModalScreen`` subclasses, in the same idiom as the
existing screens in ``tui.py``: ``BINDINGS`` with ``f5``/``f10``/``escape``,
scoped ``DEFAULT_CSS``, ``_label(ru, en)`` strings, ``action_*`` that
``self.dismiss(value)``, and background network work via ``run_worker(...,
thread=True, exclusive=True, group=...)`` with ``call_from_thread`` callbacks.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, replace as _dc_replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .connections import (
    CONNECTION_AUTH_SCHEMES,
    CONNECTION_TUNNELS,
    ConnectionConfigurationError,
    ConnectionCredentialStore,
    ConnectionError,
    MCP_CLIENT_PRESETS,
    McpClientTarget,
    connection_registry,
    connection_test_endpoint,
    mcp_client_preset,
    mcp_client_presets,
    prepare_managed_mcp_binding,
    resolve_clickup_defaults,
    resolve_connection_secret,
    rollback_managed_mcp_binding,

    apply_clickup_overrides,
)
from .connection_controller import (
    ConnectionLaunchError,
    ManagedConnectionLaunch,
    connection_controller,
)
from .clickup_setup import (
    ClickupSetupOutcome,
    setup_clickup_connection,
)
from .connection_runtime import ConnectionRuntimeError
from .connection_status import (
    BridgeStatus,
    ChatGPTClientStatus,
    ConfigurationStatus,
    ConnectionLiveStatus,
    CredentialStatus,
    OAuthStatus,
    OverallStatus,
    ToolVerificationStatus,
    TunnelStatus,
    compute_live_status,
)
from .credentials import CredentialError, CredentialStore
from .provider_controller import ProviderController
from .provider_presets import provider_preset
from .security import redact

try:  # pragma: no cover - imports mirror tui.py's guarded block
    from textual import on
    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import (
        Grid,
        Horizontal,
        HorizontalScroll,
        Vertical,
        VerticalScroll,
    )
    from textual.screen import ModalScreen
    from textual.widgets import Button, Input, Label, OptionList, RadioSet, Static, Switch
    from textual.widgets import RadioButton
    from textual.widgets.option_list import Option
except Exception:  # pragma: no cover
    pass


# ---------------------------------------------------------------------------
# Shared string tables (bilingual, matching tui.py's _TEXT convention).
# ---------------------------------------------------------------------------

_C: Dict[str, Dict[str, str]] = {
    "ru": {
        "hub_title": "Подключения",
        "hub_hint": "1 — MCP-клиенты • 2 — Model Providers • 3 — новый API-провайдер • Esc — назад",
        "mcp_clients": "MCP-клиенты",
        "model_providers": "API-провайдеры",
        "new_provider": "Подключить новый API-провайдер",
        "add": "Добавить",
        "edit": "Изменить",
        "delete": "Удалить",
        "test": "Проверить",
        "save": "Сохранить",
        "cancel": "Отмена",
        "copy_url": "Копировать URL",
        "copy_secret": "Копировать секрет",
        "set_active": "Сделать активным",
        "name": "Имя",
        "client_type": "Тип клиента",
        "transport": "Transport",
        "auth": "Авторизация",
        "tunnel": "Tunnel",
        "endpoint_path": "MCP endpoint path",
        "header_name": "Имя заголовка",
        "header_prefix": "Префикс заголовка",
        "url_stability": "Стабильность URL",
        "public_url": "Публичный URL",
        "description": "Описание",
        "instructions": "Инструкция по подключению",
        "secret": "Секрет",
        "no_secret": "нет секрета",
        "active": "активный",
        "inactive": "не активен",
        "no_connections": "Подключений пока нет.",
        "list_hint": "Enter — открыть • e — изменить • r — запустить/перезапустить • t — проверить • x — остановить • Del — удалить • Esc — назад",
        "preset_hint": "Выберите тип клиента. Клавиши 1–9 или стрелки + Enter.",
        "advanced": "Дополнительные параметры",
        "no_auth_warning": "Без авторизации небезопасно. Только для локальной отладки.",
        "testing": "Проверяю подключение…",
        "saved": "Подключение сохранено.",
        "copied": "Скопировано.",
        "need_bridge": "URL станет известен после запуска моста для этого подключения.",
        "active_model": "Активная модель",
        "model_providers_hint": "Enter — открыть • t — проверить • a — сделать активным • Del — удалить",
        "models_endpoint": "Models endpoint (если отличается)",
        "manual_model": "Model ID вручную",
        "context": "Контекст, токенов",
        "output": "Макс. вывод, токенов",
        "base_url": "Base URL",
        "api_key": "API-ключ",
        "adapter": "Тип API",
        "fetch_models": "Получить модели",
        "provider_hint": "Tab — переход • F5 — получить модели • F10 — проверить и сохранить • Esc — отмена",
        "tools": "Tool calling",
        "streaming": "Streaming",
        "vision": "Vision",
        "reasoning": "Reasoning",
        "request_timeout": "Таймаут запроса, сек",
        "custom_headers": "Свои заголовки (header: value, по одному в строке)",
        "org_headers": "Org/Project заголовки",
        "cu_title": "Добавить ClickUp",
        "cu_name": "Имя подключения:",
        "cu_mode": "Режим:",
        "cu_auto": "Автоматическая настройка",
        "cu_status": "Статус:",
        "cu_connect": "Подключить автоматически",
        "cu_advanced_toggle": "Дополнительные настройки ▸",
        "cu_config": "Конфигурация определена",
        "cu_secret_done": "Секрет создан",
        "cu_server_done": "MCP server запущен",
        "cu_tunnel_done": "Tunnel запущен",
        "cu_url_done": "Public URL получен",
        "cu_handshake_done": "MCP handshake пройден",
        "cu_setup_progress": "Настройка…",
        "cu_ready_title": "ClickUp готов к подключению",
        "cu_auth_header": "Авторизация:",
        "cu_header_lbl": "Заголовок:",
        "cu_status_online": "MCP online",
        "cu_tunnel_online": "Tunnel online",
        "cu_handshake_pass": "Handshake passed",
        "cu_actions": "Действия:",
        "cu_copy_instruction": "Копировать инструкцию",
        "cu_retest": "Проверить снова",
        "cu_stop": "Остановить",
        "cu_change_settings": "Изменить настройки",
        "cu_reset_auto": "Сбросить на автоматические",
        "cu_invalid_combo": "Несовместимая комбинация настроек",
        "cu_tunnel_missing": "Tunnel не установлен",
        "cu_tailscale_login": "Tailscale не авторизован",
        "cu_retry": "Повторить",
        "cu_open_log": "Открыть лог",
        "cu_recreate_secret": "Пересоздать секрет",
        "cu_port_busy": "Порт занят, выбран другой автоматически",
    },
    "en": {
        "hub_title": "Connections",
        "hub_hint": "1 — MCP clients • 2 — Model providers • 3 — new API provider • Esc — back",
        "mcp_clients": "MCP clients",
        "model_providers": "API providers",
        "new_provider": "Connect a new API provider",
        "add": "Add",
        "edit": "Edit",
        "delete": "Delete",
        # B6.1. Was "Test". The same action is called "Verify" on the detail
        # screen and in the service flow, and one action with two names reads
        # as two actions -- especially here, where "test" also suggests a dry
        # run that changes nothing while this one records a real result.
        "test": "Verify",
        "save": "Save",
        "cancel": "Cancel",
        "copy_url": "Copy URL",
        "copy_secret": "Copy secret",
        "set_active": "Set active",
        "name": "Name",
        "client_type": "Client type",
        "transport": "Transport",
        "auth": "Authentication",
        "tunnel": "Tunnel",
        "endpoint_path": "MCP endpoint path",
        "header_name": "Header name",
        "header_prefix": "Header prefix",
        "url_stability": "URL stability",
        "public_url": "Public URL",
        "description": "Description",
        "instructions": "Connection instructions",
        "secret": "Secret",
        "no_secret": "no secret",
        "active": "active",
        "inactive": "inactive",
        "no_connections": "No connections yet.",
        "list_hint": "Enter — open • e — edit • r — start/restart • t — test • x — stop • Del — delete • Esc — back",
        "preset_hint": "Choose a client type. Keys 1–9 or arrows + Enter.",
        "advanced": "Advanced configuration",
        "no_auth_warning": "No auth is unsafe. Local debugging only.",
        "testing": "Testing connection…",
        "saved": "Connection saved.",
        "copied": "Copied.",
        "need_bridge": "The URL is known after you start a bridge for this connection.",
        "active_model": "Active model",
        "model_providers_hint": "Enter — open • t — test • a — set active • Del — delete",
        "models_endpoint": "Models endpoint (if different)",
        "manual_model": "Model ID manually",
        "context": "Context tokens",
        "output": "Max output tokens",
        "base_url": "Base URL",
        "api_key": "API key",
        "adapter": "API type",
        "fetch_models": "Fetch models",
        "provider_hint": "Tab — move • F5 — fetch models • F10 — verify and save • Esc — cancel",
        "tools": "Tool calling",
        "streaming": "Streaming",
        "vision": "Vision",
        "reasoning": "Reasoning",
        "request_timeout": "Request timeout, sec",
        "custom_headers": "Custom headers (header: value, one per line)",
        "org_headers": "Org/Project headers",
        "cu_title": "Add ClickUp",
        "cu_name": "Connection name:",
        "cu_mode": "Mode:",
        "cu_auto": "Automatic setup",
        "cu_status": "Status:",
        "cu_connect": "Connect automatically",
        "cu_advanced_toggle": "Advanced settings ▸",
        "cu_config": "Configuration resolved",
        "cu_secret_done": "Secret created",
        "cu_server_done": "MCP server started",
        "cu_tunnel_done": "Tunnel started",
        "cu_url_done": "Public URL obtained",
        "cu_handshake_done": "MCP handshake passed",
        "cu_setup_progress": "Setting up…",
        "cu_ready_title": "ClickUp is ready to connect",
        "cu_auth_header": "Authentication:",
        "cu_header_lbl": "Header:",
        "cu_status_online": "MCP online",
        "cu_tunnel_online": "Tunnel online",
        "cu_handshake_pass": "Handshake passed",
        "cu_actions": "Actions:",
        "cu_copy_instruction": "Copy instructions",
        "cu_retest": "Re-test",
        "cu_stop": "Stop",
        "cu_change_settings": "Change settings",
        "cu_reset_auto": "Reset to automatic",
        "cu_invalid_combo": "Incompatible configuration combination",
        "cu_tunnel_missing": "Tunnel not installed",
        "cu_tailscale_login": "Tailscale not authorized",
        "cu_retry": "Retry",
        "cu_open_log": "Open log",
        "cu_recreate_secret": "Recreate secret",
        "cu_port_busy": "Port busy, another chosen automatically",
    },
}


def _t(language: str, key: str) -> str:
    table = _C.get(language, _C["en"])
    return table.get(key, key)


def _label(language: str, ru: str, en: str) -> str:
    return en if language == "en" else ru


# ---------------------------------------------------------------------------
# B1. The Connection Hub view model.
#
# One screen answers three questions and no others: what is connected, does it
# work, and what can I add. Everything below exists to make a fourth question --
# "what is a bearer scheme and why am I being asked" -- impossible, because the
# root screen never raises it.
#
# The hub replaces a three-button menu of internal protocol families (MCP
# clients / Model providers / new API provider). That menu made the user
# classify a connection before they had seen what exists, and it named the
# product's own architecture: "MCP client" is a fact about KaroX, not about what
# the person is trying to do. A row now says "ClickUp works", and the
# architecture stays where it belongs.
#
# ``HubRow`` is the containment boundary, in the same idiom as the A2 activity
# line: every field is a human name, a model name, or a catalog key. There is
# deliberately no field for adapter_kind, transport, auth_scheme,
# access_profile, credential_ref, port, tunnel or an endpoint, so the root
# screen cannot show one even by accident. Those facts are real and still
# reachable -- one deliberate step deeper, in the detail and advanced screens
# that already exist.

HUB_FAMILY_AI = "ai"
HUB_FAMILY_SERVICE = "service"
HUB_FAMILY_OTHER = "other"

HUB_STATUS_WORKING = "working"
HUB_STATUS_STOPPED = "stopped"
HUB_STATUS_ATTENTION = "attention"
HUB_STATUS_ERROR = "error"
# B5. A record the user parked on purpose. It outranks every runtime status,
# because "stopped" and "switched off" are different answers to the question
# "why is this not working", and only one of them is something the user did.
HUB_STATUS_DISABLED = "disabled"
# B5 audit 5.5. Configured, allowed to run, and not yet proven. This is the
# honest resting state of a record nobody has checked in this process: it is
# not "works", because nothing demonstrated that, and it is not "needs
# attention", because nothing is wrong.
HUB_STATUS_READY = "ready"

HUB_ADD_MODEL = "add_model"
HUB_ADD_SERVICE = "add_service"
HUB_ADD_OTHER = "add_other"
HUB_MANAGE_MODELS = "manage_models"
HUB_MANAGE_CONNECTIONS = "manage_connections"

HUB_ADD_ACTIONS = (HUB_ADD_MODEL, HUB_ADD_SERVICE, HUB_ADD_OTHER)
HUB_MANAGE_ACTIONS = (HUB_MANAGE_MODELS, HUB_MANAGE_CONNECTIONS)

_HUB_STATUS_WORDS: Dict[str, Tuple[str, str]] = {
    HUB_STATUS_WORKING: ("работает", "works"),
    HUB_STATUS_STOPPED: ("остановлено", "stopped"),
    HUB_STATUS_ATTENTION: ("нужно действие", "needs attention"),
    HUB_STATUS_ERROR: ("ошибка", "error"),
    HUB_STATUS_DISABLED: ("отключено", "disabled"),
    HUB_STATUS_READY: ("готово к проверке", "ready to check"),
}

_HUB_ADD_WORDS: Dict[str, Tuple[str, str]] = {
    HUB_ADD_MODEL: ("AI-модель", "AI model"),
    HUB_ADD_SERVICE: (
        "Приложение или сервис — ChatGPT, Claude, ClickUp, Notion…",
        "App or service — ChatGPT, Claude, ClickUp, Notion…",
    ),
    # Protocol choice belongs to the next screen. The root should ask what the
    # person wants to connect, not whether they know what MCP/OpenAPI means.
    HUB_ADD_OTHER: ("Своё подключение", "Custom connection"),
}

_HUB_MANAGE_WORDS: Dict[str, Tuple[str, str]] = {
    HUB_MANAGE_MODELS: ("Все модели", "All models"),
    HUB_MANAGE_CONNECTIONS: ("Все подключения", "All connections"),
}

# Which presets are a product a person recognises by name, and which are a
# protocol they chose on purpose. The split decides only which "Add" entry
# offers them; it is never shown as a label, because "web agent bridge" is
# KaroX vocabulary and "ChatGPT" is the user's.
_HUB_SERVICE_PRESETS = frozenset(
    {"clickup", "chatgpt-web", "claude-web", "notion", "promptql", "adapt"}
)

# Runtime state identifiers to the four words a person needs. An unmapped state
# becomes an error rather than a blank: something is wrong that the row cannot
# name, and saying nothing would read as "fine".
_HUB_RUNTIME_STATUS: Dict[str, str] = {
    "running": HUB_STATUS_WORKING,
    "degraded": HUB_STATUS_ATTENTION,
    "configured_not_running": HUB_STATUS_STOPPED,
    "stopped": HUB_STATUS_STOPPED,
}


@dataclass(frozen=True)
class HubRow:
    """One connection, in the only vocabulary the root screen speaks."""

    row_id: str
    family: str
    name: str
    detail: str = ""
    status: str = HUB_STATUS_WORKING


def hub_status_words(status: str, english: bool) -> str:
    words = _HUB_STATUS_WORDS.get(status) or _HUB_STATUS_WORDS[HUB_STATUS_ERROR]
    return words[1] if english else words[0]


def hub_add_words(action: str, english: bool) -> str:
    words = _HUB_ADD_WORDS.get(action)
    if words is None:
        return action
    return words[1] if english else words[0]


def hub_manage_words(action: str, english: bool, *, count: Optional[int] = None) -> str:
    words = _HUB_MANAGE_WORDS.get(action)
    if words is None:
        return action
    label = words[1] if english else words[0]
    if count is not None:
        return f"{label} ({count})"
    return label


def hub_row_text(row: HubRow, english: bool) -> str:
    """Render one row: the name, the model when there is one, and the state.

    An absent fact contributes nothing rather than an em dash. A column of "—"
    is a table admitting it has nothing to say, and this is not a table.
    """

    parts = [row.name]
    if row.detail:
        parts.append(row.detail)
    parts.append(hub_status_words(row.status, english))
    return " · ".join(parts)


def hub_service_family(preset_id: str) -> str:
    """Which "Add" entry a saved connection belongs under."""

    return (
        HUB_FAMILY_SERVICE
        if str(preset_id) in _HUB_SERVICE_PRESETS
        else HUB_FAMILY_OTHER
    )


def hub_runtime_status(state: Any) -> str:
    return _HUB_RUNTIME_STATUS.get(str(state or ""), HUB_STATUS_ERROR)


def provider_display_name(provider_id: str) -> str:
    """The provider's own name, not its internal id.

    ``openrouter`` is a key in a JSON file; "OpenRouter" is what is printed on
    the account the user is looking at. An id nobody published a name for is
    title-cased rather than dropped -- a custom endpoint the user named himself
    is still the best label available for it.
    """

    identifier = str(provider_id or "")
    published = ""
    try:
        published = str(provider_preset(identifier).display_name or "")
    except Exception:
        # An id with no preset is the normal case for a custom endpoint, not an
        # error worth reporting: the fallback below still produces a label.
        published = ""
    if published:
        return published
    return identifier.replace("-", " ").replace("_", " ").strip().title() or identifier


def provider_hub_rows(registry: Any) -> Tuple[HubRow, ...]:
    """AI providers, read from the registry that already owns them.

    No second store. The hub is a projection of
    :class:`~karox.registry.ProviderRegistry` and the connection controller, so
    a provider added from the wizard appears here because it exists there --
    not because two lists were kept in step by hand.

    A provider with no model is "needs attention" rather than "works": the key
    is saved and nothing can be sent with it, which is precisely the state a
    person opens this screen to discover.
    """

    try:
        providers = list(registry.providers())
    except Exception:
        return ()
    try:
        selected = registry.selected_model()
    except Exception:
        selected = None

    rows: List[HubRow] = []
    for provider in providers:
        provider_id = str(getattr(provider, "provider_id", "") or "")
        if not provider_id:
            continue
        try:
            models = list(registry.models(provider_id))
        except Exception:
            models = []
        # The model the user would actually get. The selected one when this is
        # the active provider, otherwise the only sensible stand-in.
        model_id = ""
        if selected is not None and selected.provider_id == provider_id:
            model_id = str(getattr(selected, "model_id", "") or "")
        elif len(models) == 1:
            model_id = str(getattr(models[0], "model_id", "") or "")
        # B5. A parked provider reads as parked, whatever else is true of it.
        # Showing "needs attention" for a record the user deliberately switched
        # off would send them to fix something that is not broken.
        # B5 audit 5.5, second half. The Detail screen stopped claiming
        # "works" from the mere existence of models; the hub was still doing
        # it, so the same provider read green in the list and neutral one key
        # later. Having a model proves a model is registered, nothing about
        # the key being valid or the endpoint reachable.
        #
        # No verification result is persisted anywhere, so a freshly opened
        # hub can only honestly say "ready to check".
        if not getattr(provider, "enabled", True):
            status = HUB_STATUS_DISABLED
        elif models:
            status = HUB_STATUS_READY
        else:
            status = HUB_STATUS_ATTENTION
        rows.append(
            HubRow(
                row_id=f"{HUB_FAMILY_AI}:{provider_id}",
                family=HUB_FAMILY_AI,
                name=provider_display_name(provider_id),
                detail=model_id,
                status=status,
            )
        )
    return tuple(rows)


def current_provider_hub_rows(registry: Any) -> Tuple[HubRow, ...]:
    """Only the AI model that is selected for new work.

    The root Connections screen answers "what am I using now", not "what has
    ever been configured". The complete provider catalog remains available
    through the explicit management entry. If no model is selected there is no
    current AI row to show; a stale provider record must not masquerade as the
    active model merely because it is the only record left in the registry.
    """

    try:
        selected = registry.selected_model()
    except Exception:
        return ()
    if selected is None:
        return ()
    provider_id = str(getattr(selected, "provider_id", "") or "")
    if not provider_id:
        return ()
    wanted = f"{HUB_FAMILY_AI}:{provider_id}"
    return tuple(row for row in provider_hub_rows(registry) if row.row_id == wanted)


def service_hub_rows(states: Sequence[Any]) -> Tuple[HubRow, ...]:
    """Web agents and MCP/OpenAPI clients, from the connection controller.

    The controller already reconciles the saved target with its live runtime,
    so this reads one object and invents nothing. Only the connection's own
    name and its state survive the trip; the transport, the auth scheme, the
    port and the endpoint are read here only to be *not* carried forward.
    """

    rows: List[HubRow] = []
    for state in states:
        target = getattr(state, "target", None)
        if target is None:
            continue
        connection_id = str(getattr(target, "connection_id", "") or "")
        if not connection_id:
            continue
        preset_id = str(getattr(target, "preset_id", "") or "")
        # B5. Same rule as a provider: the user's own decision wins over the
        # runtime observation. A disabled connection whose bridge is still up
        # is still disabled -- the process is winding down, not the setting.
        if not getattr(target, "enabled", True):
            status = HUB_STATUS_DISABLED
        else:
            status = hub_runtime_status(getattr(state, "state", None))
        rows.append(
            HubRow(
                row_id=f"{hub_service_family(preset_id)}:{connection_id}",
                family=hub_service_family(preset_id),
                name=str(getattr(target, "name", "") or connection_id),
                status=status,
            )
        )
    return tuple(rows)


def active_service_hub_rows(states: Sequence[Any]) -> Tuple[HubRow, ...]:
    """Only service rows that matter *right now* on the root screen.

    Stopped and deliberately disabled records are saved configuration, not an
    active connection. They remain fully reachable through the management
    screen; keeping them out of the root prevents years of old ClickUp/custom
    experiments from crowding out the one connection a user is actually using.
    Degraded/error rows stay visible because they require attention now.
    """

    return tuple(
        row
        for row in service_hub_rows(states)
        if row.status not in {HUB_STATUS_STOPPED, HUB_STATUS_DISABLED}
    )


# ---------------------------------------------------------------------------
# B3. The standard service flow: ChatGPT Web, Claude Web, Hyperagent, Notion, ClickUp.
#
# The user picks a service. KaroX picks the parameters. The screen explains one
# next step. Everything below is presentation over the *existing*
# `MCP_CLIENT_PRESETS` catalog -- the numbered steps and the human status words
# live here because the preset carries one English paragraph and no status
# vocabulary, but transport, auth scheme, tunnel, endpoint path and runtime
# profile are read from the preset and never restated. A second defaults table
# in the UI is a table that will disagree with the launcher.
#
# `ServiceView` is the containment boundary, in the same idiom as `HubRow` and
# the A2 activity line: numbered human steps, one public URL, one status key.
# There is deliberately no field for a port, a tunnel, a transport, an access
# profile, a tool list or a secret, so the standard screen cannot show one.

# The seven states a person is allowed to see, plus one refinement.
SERVICE_NOT_CONFIGURED = "not_configured"
SERVICE_READY = "ready"
SERVICE_AWAITING = "awaiting"
SERVICE_CHECKING = "checking"
SERVICE_WORKING = "working"
SERVICE_ACTION_REQUIRED = "action_required"
SERVICE_ERROR = "error"
# The honest half-state, and the reason it exists. Verifying the bridge proves
# the bridge. It does not prove that ChatGPT finished its side, and a green
# "works" over an unproven external connection is the single most misleading
# thing this screen could say -- the user closes it believing they are done.
SERVICE_BRIDGE_ONLY = "bridge_only"

_SERVICE_STATUS_WORDS: Dict[str, Tuple[str, str]] = {
    SERVICE_NOT_CONFIGURED: ("не настроено", "not configured"),
    SERVICE_READY: ("готово к подключению", "ready to connect"),
    SERVICE_AWAITING: ("ожидает подключения", "waiting for the service"),
    SERVICE_CHECKING: ("проверяется", "checking"),
    SERVICE_WORKING: ("работает", "works"),
    SERVICE_ACTION_REQUIRED: ("требуется действие", "action needed"),
    SERVICE_ERROR: ("ошибка подключения", "connection failed"),
    SERVICE_BRIDGE_ONLY: (
        "мост работает · внешний сервис ещё не подтверждён",
        "the bridge works · the external service is not confirmed yet",
    ),
}

# Only a proven check may say "works". Held as data so no branch can decide to
# be optimistic on its own.
_SERVICE_PROVEN_STATUSES = frozenset({SERVICE_WORKING})

# The numbered steps, per service, in both languages. Written from each
# service's own product surface rather than copied between them: ChatGPT wants a
# custom MCP app in developer mode, Claude wants a connector, ClickUp wants an
# App Center entry and an explicit authentication choice.
_SERVICE_STEPS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "chatgpt-web": (
        (
            "Скопируйте URL KaroX кнопкой ниже",
            "Copy the KaroX URL with the button below",
        ),
        (
            "В ChatGPT откройте Настройки → Приложения и создайте свой MCP app",
            "In ChatGPT open Settings → Apps and create your own MCP app",
        ),
        (
            "Вставьте URL, сохраните подключение и вернитесь сюда",
            "Paste the URL, save the connection, then return here",
        ),
        (
            "KaroX увидит ChatGPT автоматически; если статус не обновился — нажмите «Проверить»",
            "KaroX will detect ChatGPT automatically; if the status does not update, press Verify",
        ),
    ),
    "claude-web": (
        (
            "Откройте Claude → Settings → Connectors",
            "Open Claude → Settings → Connectors",
        ),
        ("Добавьте свой connector", "Add your own connector"),
        ("Вставьте этот адрес:", "Paste this address:"),
        (
            "Подтвердите доступ на странице KaroX",
            "Approve access on the KaroX page",
        ),
    ),
    "hyperagent-web": (
        (
            "Откройте Hyperagent → Settings → Integrations",
            "Open Hyperagent → Settings → Integrations",
        ),
        (
            "Добавьте Custom MCP server и вставьте стабильный адрес KaroX из этого экрана",
            "Add a Custom MCP server and paste the stable KaroX address from this screen",
        ),
        (
            "Оставьте OAuth приложения на стороне Hyperagent автоматическим — Client ID и Client Secret KaroX не нужны",
            "Let Hyperagent handle the OAuth app automatically — no KaroX Client ID or Client Secret is needed",
        ),
        (
            "Завершите подтверждение на странице KaroX и вернитесь в Hyperagent",
            "Complete approval on the KaroX page, then return to Hyperagent",
        ),
    ),
    "notion": (
        (
            "Откройте Custom Agent → Settings → Tools & Access → Add connection → Custom MCP server",
            "Open Custom Agent → Settings → Tools & Access → Add connection → Custom MCP server",
        ),
        (
            "Вставьте стабильный адрес KaroX из этого экрана",
            "Paste the stable KaroX address from this screen",
        ),
        (
            "Выберите OAuth, если Notion показывает выбор; Client ID, Client Secret и Bearer token не нужны",
            "Choose OAuth if Notion shows an auth selector; no Client ID, Client Secret, or bearer token is needed",
        ),
        (
            "Завершите подтверждение на странице KaroX и вернитесь в Notion",
            "Complete approval on the KaroX page, then return to Notion",
        ),
    ),
    "adapt": (
        (
            "Откройте Adapt → Settings → Integrations → Custom Integration",
            "Open Adapt → Settings → Integrations → Custom Integration",
        ),
        (
            "Создайте Personal integration с именем KaroX и укажите стабильный адрес KaroX /mcp в описании",
            "Create a Personal integration named KaroX and put the stable KaroX /mcp address in its description",
        ),
        (
            "Добавьте credential KAROX_AUTHORIZATION и вставьте полное значение Authorization из KaroX. Оно уже начинается с Bearer — второй Bearer не добавляйте",
            "Add credential KAROX_AUTHORIZATION and paste the complete KaroX Authorization value. It already starts with Bearer — do not add a second Bearer",
        ),
        (
            "Сохраните integration, попросите Adapt использовать KaroX и вернитесь сюда для проверки",
            "Save the integration, ask Adapt to use KaroX, then return here to verify",
        ),
    ),
    "clickup": (
        (
            "Откройте ClickUp → App Center → MCP Servers",
            "Open ClickUp → App Center → MCP Servers",
        ),
        ("Выберите Connect an MCP Server", "Choose Connect an MCP Server"),
        ("Вставьте этот адрес:", "Paste this address:"),
        (
            "Выберите способ входа «Authorization header», не OAuth",
            "Choose the “Authorization header” sign-in method, not OAuth",
        ),
    ),
}

# What a successful check actually proves, per service. Read from the preset's
# own recorded limitations rather than assumed: the ChatGPT and Claude presets
# both state that OAuth is covered locally and no live run is recorded, so their
# flows must not claim a confirmed external connection.
_SERVICE_PROVES_EXTERNAL: Dict[str, bool] = {
    "chatgpt-web": False,
    "claude-web": False,
    "hyperagent-web": False,
    "notion": False,
    "adapt": False,
    "clickup": True,
}


@dataclass(frozen=True)
class ServiceView:
    """One standard service screen, in the only vocabulary it speaks."""

    preset_id: str
    name: str
    steps: Tuple[str, ...]
    endpoint: str
    status: str
    manual_note: str = ""


def service_flow_presets() -> Tuple[str, ...]:
    """The services the standard flow offers, in a stable order.

    Every id is checked against the production catalog on use, so a preset
    renamed in `connections.py` fails loudly here instead of quietly offering a
    screen that cannot start anything.
    """

    return ("chatgpt-web", "claude-web", "hyperagent-web", "notion", "clickup", "adapt")


def service_status_words(status: str, english: bool) -> str:
    words = _SERVICE_STATUS_WORDS.get(status) or _SERVICE_STATUS_WORDS[SERVICE_ERROR]
    return words[1] if english else words[0]


def service_status_is_proven(status: str) -> bool:
    return status in _SERVICE_PROVEN_STATUSES


def service_steps(preset_id: str, english: bool) -> Tuple[str, ...]:
    """The numbered human steps for one service.

    Numbering is applied here rather than stored, so a step cannot be added in
    one language and misnumbered in the other.
    """

    entries = _SERVICE_STEPS.get(str(preset_id), ())
    return tuple(
        f"{index}. {entry[1] if english else entry[0]}"
        for index, entry in enumerate(entries, start=1)
    )


def service_display_name(preset_id: str) -> str:
    """The service's own name, from the production preset."""

    if str(preset_id) == "notion":
        return "Notion"
    try:
        return str(mcp_client_preset(str(preset_id)).display_name)
    except Exception:
        return str(preset_id)


def service_verification_proves_external(preset_id: str) -> bool:
    return bool(_SERVICE_PROVES_EXTERNAL.get(str(preset_id), False))


def service_manual_note(preset_id: str, english: bool) -> str:
    """What the user has to confirm by hand, when a check cannot prove it.

    Empty when verification is conclusive. A service whose external side cannot
    be proven says so on the screen rather than leaving the user to infer it
    from a status word that sounds finished.
    """

    if service_verification_proves_external(preset_id):
        return ""
    if str(preset_id) == "chatgpt-web":
        return (
            "After you save the app in ChatGPT, come back here. KaroX watches for the "
            "ChatGPT handshake and updates this screen automatically."
            if english
            else "После сохранения приложения в ChatGPT вернитесь сюда. KaroX сам "
            "увидит подключение ChatGPT и обновит этот экран."
        )
    if str(preset_id) == "hyperagent-web":
        return (
            "Hyperagent saves this custom MCP URL. Keep a stable Tailscale/custom HTTPS address. "
            "KaroX can verify its own bridge locally; the Hyperagent side is confirmed only after "
            "Hyperagent actually reaches it."
            if english
            else "Hyperagent сохраняет этот Custom MCP URL. Используйте стабильный Tailscale/custom HTTPS. "
            "KaroX может проверить свой bridge локально; сторона Hyperagent подтверждается только после "
            "реального подключения Hyperagent."
        )
    if str(preset_id) == "notion":
        return (
            "Notion saves this MCP URL. Keep a stable Tailscale/custom HTTPS address; "
            "a Cloudflare Quick Tunnel must be updated after restart."
            if english
            else "Notion сохраняет этот MCP URL. Используйте стабильный Tailscale/custom HTTPS; "
            "Cloudflare Quick Tunnel после перезапуска придётся обновить."
        )
    if str(preset_id) == "adapt":
        return (
            "After you create the Adapt integration, ask Adapt to use KaroX once. "
            "KaroX can verify its bridge here; the Adapt side is confirmed only "
            "after Adapt actually reaches the endpoint."
            if english
            else "После создания integration попросите Adapt один раз использовать KaroX. "
            "Здесь KaroX проверяет свой bridge; сторона Adapt подтверждается только "
            "после реального обращения Adapt к endpoint."
        )
    return (
        "After you save the connector, return here. KaroX will update this screen "
        "when the external service reaches it."
        if english
        else "После сохранения коннектора вернитесь сюда. KaroX обновит экран, "
        "когда внешний сервис подключится."
    )


def service_status_for_runtime(preset_id: str, state: Any, endpoint: Any) -> str:
    """The status a saved service shows before anyone presses Verify.

    A running bridge is not a working service. The best this can say without a
    check is that the address exists and the service has not answered on it yet.
    """

    if not endpoint:
        return SERVICE_NOT_CONFIGURED
    runtime = hub_runtime_status(state)
    if runtime == HUB_STATUS_WORKING:
        return SERVICE_AWAITING
    if runtime == HUB_STATUS_ATTENTION:
        return SERVICE_ACTION_REQUIRED
    if runtime == HUB_STATUS_STOPPED:
        return SERVICE_READY
    return SERVICE_ERROR


def service_status_after_check(preset_id: str, result: Any) -> str:
    """Translate a verification result into a status the screen may show.

    The `public_pending` case is why this is a function and not a boolean. The
    ClickUp handshake reports it when the loopback answered but the public URL
    has not resolved yet, and calling that "works" would send somebody to paste
    an address that is not live.
    """

    state = str((result or {}).get("state") or "") if isinstance(result, dict) else ""
    if state == "ok":
        return (
            SERVICE_WORKING
            if service_verification_proves_external(preset_id)
            else SERVICE_BRIDGE_ONLY
        )
    if state == "public_pending":
        return SERVICE_BRIDGE_ONLY
    return SERVICE_ERROR


# ---------------------------------------------------------------------------
# B4. One Advanced contract, for providers and services alike.
#
# The standard flow stays simple. Advanced settings open only on a deliberate
# action and are one layer with one set of rules, rather than the several old
# wizards that each had their own.
#
# Three rules make that true rather than aspirational. Every field a group
# offers is *applicable*: a screen never shows a control the flow cannot apply,
# because a field that silently does nothing is worse than an absent one.
# Defaults come from the production preset, never from a UI constant, so the
# form and the launcher cannot disagree. And an empty optional field means "use
# the default", never "write a blank", so clearing a box cannot quietly break a
# working connection.

ADVANCED_GROUP_CONNECTION = "connection"
ADVANCED_GROUP_PERMISSIONS = "permissions"
ADVANCED_GROUP_LIMITS = "limits"
ADVANCED_GROUP_DIAGNOSTICS = "diagnostics"

ADVANCED_GROUPS: Tuple[str, ...] = (
    ADVANCED_GROUP_CONNECTION,
    ADVANCED_GROUP_PERMISSIONS,
    ADVANCED_GROUP_LIMITS,
    ADVANCED_GROUP_DIAGNOSTICS,
)

_ADVANCED_GROUP_WORDS: Dict[str, Tuple[str, str]] = {
    ADVANCED_GROUP_CONNECTION: ("Подключение", "Connection"),
    ADVANCED_GROUP_PERMISSIONS: ("Разрешения", "Permissions"),
    ADVANCED_GROUP_LIMITS: ("Лимиты", "Limits"),
    ADVANCED_GROUP_DIAGNOSTICS: ("Диагностика", "Diagnostics"),
}

# The three modes a person chooses between, and the profile each one *is*.
# This is presentation over `AccessProfile`, not a new permission architecture:
# the mapping is one-to-one onto profiles the policy layer already enforces, so
# the UI cannot offer a combination the policy cannot express.
PERMISSION_READ_ONLY = "read_only"
PERMISSION_PROJECT = "project"
PERMISSION_EXTENDED = "extended"
# Display-only value for saved profiles that hold BROWSER_CONTROL. It is not
# one of the three offered choices, but showing such a profile as "Project
# access" promised repo-write capabilities the session can never use, which is
# exactly the "UI says one thing, effective capabilities say another" state.
PERMISSION_BROWSER = "browser"

PERMISSION_MODES: Tuple[str, ...] = (
    PERMISSION_READ_ONLY,
    PERMISSION_PROJECT,
    PERMISSION_EXTENDED,
)

_PERMISSION_MODE_WORDS: Dict[str, Tuple[str, str]] = {
    PERMISSION_READ_ONLY: ("Только чтение", "Read only"),
    PERMISSION_PROJECT: ("Работа с проектом", "Project access"),
    PERMISSION_EXTENDED: ("Расширенный доступ", "Extended access"),
    PERMISSION_BROWSER: ("Управление браузером", "Browser control"),
}

# One short line each. Not a tool list: the exact allowlist is a technical fact
# and lives one action deeper, behind "Show permissions".
_PERMISSION_MODE_SUMMARY: Dict[str, Tuple[str, str]] = {
    PERMISSION_READ_ONLY: (
        "читать файлы проекта",
        "read the project files",
    ),
    PERMISSION_PROJECT: (
        "читать файлы проекта, изменять проект, запускать разрешённые проверки",
        "read the project, change it, and run the approved checks",
    ),
    PERMISSION_EXTENDED: (
        "всё вышеперечисленное, плюс коммиты и управление браузером",
        "all of the above, plus commits and browser control",
    ),
    PERMISSION_BROWSER: (
        "читать проект и управлять браузером, без изменения файлов",
        "read the project and drive the browser, without changing files",
    ),
}


def advanced_group_words(group: str, english: bool) -> str:
    words = _ADVANCED_GROUP_WORDS.get(group)
    if words is None:
        return group
    return words[1] if english else words[0]


def permission_mode_words(mode: str, english: bool) -> str:
    words = _PERMISSION_MODE_WORDS.get(mode)
    if words is None:
        return mode
    return words[1] if english else words[0]


def permission_mode_summary(mode: str, english: bool) -> str:
    words = _PERMISSION_MODE_SUMMARY.get(mode)
    if words is None:
        return ""
    return words[1] if english else words[0]


def permission_mode_profile(mode: str) -> Any:
    """The existing `AccessProfile` a mode maps onto.

    Imported lazily so this module stays importable without the policy layer,
    and resolved by name so a profile renamed in `models.py` fails here rather
    than silently selecting the wrong one.
    """

    from .models import AccessProfile

    mapping = {
        PERMISSION_READ_ONLY: AccessProfile.READ_ONLY,
        PERMISSION_PROJECT: AccessProfile.WORKSPACE_WRITE,
        PERMISSION_EXTENDED: AccessProfile.ELEVATED,
        PERMISSION_BROWSER: AccessProfile.BROWSER_CONTROL,
    }
    return mapping.get(str(mode), AccessProfile.WORKSPACE_WRITE)


def permission_mode_for_profile(profile: Any) -> str:
    """Which mode an existing saved profile is shown as.

    `BROWSER_CONTROL` is not one of the three offered choices, but it gets its
    own display value: labelling it "Project access" showed a repo-write
    capability list the session can never use. The saved value is not
    rewritten by being displayed.
    """

    from .models import AccessProfile

    identifier = getattr(profile, "value", profile)
    mapping = {
        AccessProfile.READ_ONLY.value: PERMISSION_READ_ONLY,
        AccessProfile.BROWSER_CONTROL.value: PERMISSION_BROWSER,
        AccessProfile.WORKSPACE_WRITE.value: PERMISSION_PROJECT,
        AccessProfile.ELEVATED.value: PERMISSION_EXTENDED,
    }
    return mapping.get(str(identifier), PERMISSION_PROJECT)


def permission_mode_capabilities(mode: str) -> Tuple[str, ...]:
    """The exact allowlist, read from the policy layer that enforces it.

    Only ever shown behind "Show permissions". Computed rather than restated:
    a hand-written list in the UI is a list that will eventually promise a
    capability the policy denies.
    """

    try:
        from .policy import _PROFILE_CAPABILITIES

        capabilities = _PROFILE_CAPABILITIES[permission_mode_profile(mode)]
    except Exception:
        return ()
    return tuple(sorted(str(getattr(item, "value", item)) for item in capabilities))


# Which physical saved-bridge target a service preset is backed by.
#
# This is the logical-binding <-> physical-bridge map, and the reason the two
# cannot be assumed identical: Adapt's Custom Integration reuses the very
# Streamable HTTP bridge ChatGPT Web already owns, so one physical process can
# serve several logical connections. Anything that changes or removes a bridge
# has to consult this map first.
# Presets whose runtime is a saved web-bridge profile, and which profile
# target each one binds to. These are the only presets whose Bypass mode is
# stored on the profile; the rest keep it on their saved connection record.
SAVED_BRIDGE_TARGETS: Dict[str, str] = {
    "chatgpt-web": "chatgpt-web",
    "claude-web": "claude-web",
    "hyperagent-web": "hyperagent-web",
    "notion": "notion",
    # Compatibility alias only: the TUI keeps presenting Adapt, while runtime
    # ownership, URL, and credential stay on the existing saved bridge instead
    # of spawning a duplicate.
    "adapt": "chatgpt-web",
}

# The discovery map adds the presets that are found by other means; ClickUp
# and PromptQL run from a saved connection record rather than a saved bridge
# profile, so their entries here never match one.
SERVICE_TARGET_PROFILES: Dict[str, str] = {
    **SAVED_BRIDGE_TARGETS,
    "clickup": "generic-streamable-http",
    "promptql": "promptql",
}


def service_target_profile(preset_id: str) -> str:
    """The saved-bridge target profile a service preset binds to."""

    return SERVICE_TARGET_PROFILES.get(str(preset_id), str(preset_id))


def saved_bridge_target(preset_id: str) -> Optional[str]:
    """The saved profile target for a preset that has one, else ``None``."""

    return SAVED_BRIDGE_TARGETS.get(str(preset_id))


def shared_bridge_presets(preset_id: str) -> Tuple[str, ...]:
    """Other service presets served by the same physical bridge.

    Non-empty means a change to the saved profile is bridge-wide and visible
    to another connection, so it must be confirmed rather than applied
    silently. Only saved-profile presets can share a bridge: a record-backed
    connection starts its own runtime.
    """

    target = saved_bridge_target(preset_id)
    if target is None:
        return ()
    return tuple(
        sorted(
            other
            for other, mapped in SAVED_BRIDGE_TARGETS.items()
            if mapped == target and other != str(preset_id)
        )
    )


def owns_saved_bridge(preset_id: str) -> bool:
    """Whether this preset owns its saved bridge rather than borrowing one.

    Adapt maps onto the ChatGPT bridge as an alias, so an Adapt-facing action
    that removes a saved profile would delete a physical bridge another
    connection is still using. Deleting the Adapt binding therefore only ever
    removes Adapt's own connection record.
    """

    target = saved_bridge_target(preset_id)
    return target is not None and target == str(preset_id)


def bypass_supported(preset_id: str) -> bool:
    """Whether this preset can carry the shared Bypass mode.

    Every known service preset can: a saved-bridge preset keeps the mode on
    its profile, and every other one keeps it on the saved connection record
    the runtime is started from. The switch is still disabled until such a
    record exists, because there is nowhere to write it before then.
    """

    return str(preset_id) in MCP_CLIENT_PRESETS


def saved_profile_full_access_enabled(profile: Any) -> bool:
    """Whether a saved Hyperagent profile is in the one-click full-dev mode.

    Compatibility alias: the production source of truth is the shared Bypass
    contract in ``access_mode`` -- Hyperagent's Full developer access is the
    same mode, and Hyperagent is simply its first user.
    """

    try:
        from .access_mode import saved_profile_bypass_enabled

        return bool(
            getattr(profile, "target_profile", "") == "hyperagent-web"
            and saved_profile_bypass_enabled(profile)
        )
    except Exception:
        return False


def build_saved_profile_full_access(profile: Any, enabled: bool) -> Any:
    """Return the canonical Hyperagent profile for Full access ON/OFF.

    Compatibility alias over the shared Bypass contract: the canonical
    builder lives in ``access_mode`` and serves every connection family.
    Hyperagent keeps its historical guarantee -- ON is one coherent trusted
    developer contract (elevated capabilities, the whole coding tool surface,
    Git commit, the unrestricted developer command runner, and the headed
    external browser); OFF returns to the protected Project access contract.
    """

    if getattr(profile, "target_profile", "") != "hyperagent-web":
        raise ValueError("full developer access is only available for Hyperagent")
    from .access_mode import build_saved_profile_bypass

    return build_saved_profile_bypass(profile, enabled)


def _csv_items(value: Any) -> Tuple[str, ...]:
    """Normalize one compact TUI comma/newline list without inventing values."""

    if value is None:
        return ()
    if isinstance(value, str):
        raw = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        raise ValueError("list setting must be text or a string list")
    return tuple(dict.fromkeys(item.strip() for item in raw if item.strip()))


def saved_web_profile_settings(profile: Any) -> Dict[str, Any]:
    """Project one saved web profile into the secret-free TUI form model."""

    return {
        "port": str(getattr(profile, "port", 8765)),
        "tunnel": str(getattr(profile, "tunnel", "tailscale")),
        "public_url": str(getattr(profile, "public_url", "") or ""),
        "browser_external_https": bool(getattr(profile, "browser_external_https", False)),
        "browser_headed": bool(getattr(profile, "browser_headed", False)),
        "browser_user_takeover": bool(getattr(profile, "browser_user_takeover", False)),
        "browser_network_inspection": bool(getattr(profile, "browser_network_inspection", False)),
        "browser_payment_confirmation": bool(getattr(profile, "browser_payment_confirmation", False)),
        "browser_allowed_domains": ", ".join(getattr(profile, "browser_allowed_domains", ()) or ()),
        "browser_denied_domains": ", ".join(getattr(profile, "browser_denied_domains", ()) or ()),
        "browser_allowed_emails": ", ".join(getattr(profile, "browser_allowed_emails", ()) or ()),
        "browser_credential_refs": tuple(getattr(profile, "browser_credential_refs", ()) or ()),
    }


def build_saved_web_profile_settings(profile: Any, values: Mapping[str, Any]) -> Any:
    """Validate a saved-web TUI form by rebuilding the production dataclass."""

    try:
        port = int(str(values.get("port", getattr(profile, "port", 8765))).strip())
    except ValueError as exc:
        raise ValueError("port must be a number between 1 and 65535") from exc
    if not 1 <= port <= 65_535:
        raise ValueError("port must be between 1 and 65535")
    tunnel = str(values.get("tunnel", getattr(profile, "tunnel", "tailscale"))).strip().lower()
    if tunnel not in {"tailscale", "cloudflare", "custom"}:
        raise ValueError("tunnel must be tailscale, cloudflare, or custom")
    public_url = str(values.get("public_url", getattr(profile, "public_url", "") or "")).strip()
    if tunnel != "custom":
        public_url = ""

    def flag(name: str) -> bool:
        current = bool(getattr(profile, name, False))
        value = values.get(name, current)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off", ""}:
                return False
        raise ValueError(f"{name} must be enabled or disabled")

    external = flag("browser_external_https")
    headed = flag("browser_headed")
    takeover = flag("browser_user_takeover")
    network = flag("browser_network_inspection")
    payment = flag("browser_payment_confirmation")
    if takeover and not headed:
        raise ValueError("user takeover requires a visible managed browser")

    return _dc_replace(
        profile,
        port=port,
        tunnel=tunnel,
        public_url=public_url or None,
        browser_external_https=external,
        browser_headed=headed,
        browser_user_takeover=takeover,
        browser_network_inspection=network,
        browser_payment_confirmation=payment,
        browser_allowed_domains=_csv_items(values.get("browser_allowed_domains", ())),
        browser_denied_domains=_csv_items(values.get("browser_denied_domains", ())),
        browser_allowed_emails=_csv_items(values.get("browser_allowed_emails", ())),
    )


def _apply_saved_web_profile_object(
    profile_name: str,
    updated: Any,
    *,
    bridge_running: bool,
) -> Dict[str, Any]:
    """TUI adapter over the canonical saved-profile transaction service."""

    from .web_bridge_launcher import apply_saved_bridge_profile

    return dict(
        apply_saved_bridge_profile(
            profile_name,
            updated,
            allow_restart=bridge_running,
        )
    )


def apply_saved_web_profile_settings(
    profile_name: str,
    values: Mapping[str, Any],
    *,
    bridge_running: bool,
) -> Dict[str, Any]:
    from .web_bridge_profiles import WebBridgeProfileStore

    current = WebBridgeProfileStore().get(profile_name)
    updated = build_saved_web_profile_settings(current, values)
    return _apply_saved_web_profile_object(
        profile_name,
        updated,
        bridge_running=bridge_running,
    )


def attach_saved_browser_credential(
    profile_name: str,
    *,
    name: str,
    username: str,
    password: str,
    bridge_running: bool,
) -> Dict[str, Any]:
    """Store a test login locally and attach only its opaque ref to this profile."""

    from .browser_credentials import BrowserCredentialStore
    from .web_bridge_profiles import WebBridgeProfileStore

    credentials = BrowserCredentialStore()
    prepared = credentials.validate_bundle(name, username=username, password=password)
    reference = prepared["reference"]
    store = WebBridgeProfileStore()
    current = store.get(profile_name)
    refs = tuple(dict.fromkeys((*current.browser_credential_refs, reference)))
    updated = _dc_replace(current, browser_credential_refs=refs)
    applied = _apply_saved_web_profile_object(
        profile_name,
        updated,
        bridge_running=bridge_running,
    )
    try:
        stored = credentials.set(name, username=username, password=password)
    except Exception as exc:
        try:
            _apply_saved_web_profile_object(
                profile_name,
                current,
                bridge_running=bridge_running,
            )
        except Exception as rollback_exc:
            raise RuntimeError(
                "browser credential keyring write failed and profile rollback also failed: "
                + str(redact(str(rollback_exc)))[:160]
            ) from exc
        raise
    return {
        "status": applied["status"],
        "reference": reference,
        "fingerprint": stored["fingerprint"],
        "backend": stored["backend"],
    }


def detach_saved_browser_credential(
    profile_name: str,
    reference: str,
    *,
    bridge_running: bool,
) -> Dict[str, Any]:
    """Detach first (security boundary), then best-effort delete the local secret."""

    from .browser_credentials import BrowserCredentialReference, BrowserCredentialStore
    from .web_bridge_profiles import WebBridgeProfileStore

    parsed = BrowserCredentialReference.parse(reference)
    store = WebBridgeProfileStore()
    current = store.get(profile_name)
    if reference not in current.browser_credential_refs:
        raise ValueError("browser credential is not attached to this connection")

    shared = True
    try:
        shared = any(
            sibling.name != profile_name
            and reference in sibling.browser_credential_refs
            for sibling in store.list()
        )
    except Exception:
        # Inventory failure must never delete a possibly shared OS credential.
        shared = True

    updated = _dc_replace(
        current,
        browser_credential_refs=tuple(
            item for item in current.browser_credential_refs if item != reference
        ),
    )
    applied = _apply_saved_web_profile_object(
        profile_name,
        updated,
        bridge_running=bridge_running,
    )
    deleted = False
    delete_error = ""
    if not shared:
        try:
            BrowserCredentialStore().delete(parsed.name)
            deleted = True
        except Exception as exc:
            delete_error = str(redact(str(exc)))[:200]
    return {
        "status": applied["status"],
        "reference": reference,
        "detached": True,
        "keyring_deleted": deleted,
        "keyring_shared": shared,
        "keyring_error": delete_error,
    }


@dataclass(frozen=True)
class AdvancedField:
    """One editable value in one group.

    `secret` marks a field whose stored value is never rendered back;
    `read_only` marks a computed fact that is shown but cannot be saved, so the
    screen never offers an edit it would silently discard.
    """

    field_id: str
    group: str
    label_ru: str
    label_en: str
    default: str = ""
    required: bool = False
    secret: bool = False
    read_only: bool = False

    def label(self, english: bool) -> str:
        return self.label_en if english else self.label_ru


def provider_advanced_fields(preset_id: str) -> Tuple[AdvancedField, ...]:
    """Advanced fields for an AI provider, defaulted from the production preset.

    A known preset fills the base URL and the adapter from the catalog, so the
    person sees the real current value rather than an empty box implying the
    product does not know its own endpoint.
    """

    identifier = str(preset_id or "")
    base_url = ""
    adapter = ""
    try:
        preset = provider_preset(identifier)
        base_url = str(preset.base_url or "")
        adapter = str(preset.adapter_kind or "")
    except Exception:
        base_url = ""
        adapter = ""
    return (
        # B5 section 5. Read-only, and not a rename control.
        #
        # A provider's id *is* its identity in the registry: models reference
        # it, the credential is filed under it, and `put_provider` keys on it.
        # Editing this box and saving would therefore not rename anything --
        # it would create a second provider and orphan the first, which is the
        # exact duplicate B5.1 exists to prevent. Better an honest label than
        # a text box that quietly means something else.
        AdvancedField(
            "connection_name",
            ADVANCED_GROUP_CONNECTION,
            "Имя подключения (неизменяемое)",
            "Connection name (fixed)",
            default=identifier,
            required=True,
            read_only=True,
        ),
        AdvancedField(
            "base_url",
            ADVANCED_GROUP_CONNECTION,
            "Base URL",
            "Base URL",
            default=base_url,
            required=True,
        ),
        AdvancedField(
            "adapter",
            ADVANCED_GROUP_CONNECTION,
            "Тип API",
            "API type",
            default=adapter,
        ),
        # B5 audit 4. This field was editable and ignored: `values()` skips
        # secrets and `_apply` never read one, so anything typed here was
        # silently discarded while the screen reported a successful Save.
        #
        # Option B of the two honest choices. Rotation already works, properly
        # and atomically, in the Provider Edit flow -- that is where the
        # controller resolves the old reference and swaps the value with a
        # rollback. Duplicating it here would mean two rotation paths, one of
        # them newer and less tested, guarding the same keyring. So the field
        # stays visible (the user should see that a key *is* saved) and is
        # read-only, with the label naming where to change it.
        AdvancedField(
            "api_key",
            ADVANCED_GROUP_CONNECTION,
            "API-ключ (меняется в «Изменить»)",
            "API key (change it in Edit)",
            secret=True,
            read_only=True,
        ),
        AdvancedField(
            "timeout",
            ADVANCED_GROUP_LIMITS,
            "Таймаут запроса, сек",
            "Request timeout, sec",
        ),
        AdvancedField(
            "context_window",
            ADVANCED_GROUP_LIMITS,
            "Контекст, токенов",
            "Context tokens",
        ),
        AdvancedField(
            "max_output",
            ADVANCED_GROUP_LIMITS,
            "Максимальный ответ, токенов",
            "Maximum output tokens",
        ),
    )


def service_advanced_fields(preset_id: str) -> Tuple[AdvancedField, ...]:
    """Advanced fields for a service, defaulted from the MCP client preset.

    Deliberately *not* the provider set. A base URL, an adapter or a context
    window would be meaningless here, and showing them would be the screen
    inventing controls the service flow cannot apply.
    """

    identifier = str(preset_id or "")
    transport = ""
    tunnel = ""
    endpoint_path = ""
    try:
        preset = mcp_client_preset(identifier)
        transport = str(preset.transport or "")
        tunnel = str(preset.tunnel_default or "")
        endpoint_path = str(preset.endpoint_path or "")
    except Exception:
        transport = ""
    return (
        AdvancedField(
            "connection_name",
            ADVANCED_GROUP_CONNECTION,
            "Имя подключения",
            "Connection name",
            default=service_display_name(identifier),
            required=True,
        ),
        AdvancedField(
            "transport",
            ADVANCED_GROUP_CONNECTION,
            "Transport",
            "Transport",
            default=transport,
        ),
        AdvancedField(
            "endpoint_path",
            ADVANCED_GROUP_CONNECTION,
            "Путь MCP",
            "MCP path",
            default=endpoint_path,
        ),
        AdvancedField(
            "port",
            ADVANCED_GROUP_CONNECTION,
            "Локальный порт (пусто — авто)",
            "Local port (blank = automatic)",
        ),
        AdvancedField(
            "tunnel",
            ADVANCED_GROUP_CONNECTION,
            "Способ публикации",
            "Tunnel strategy",
            default=tunnel,
        ),
    )


def advanced_fields(kind: str, preset_id: str) -> Tuple[AdvancedField, ...]:
    return (
        provider_advanced_fields(preset_id)
        if kind == "provider"
        else service_advanced_fields(preset_id)
    )


def advanced_defaults(kind: str, preset_id: str) -> Dict[str, str]:
    """The production defaults for every field, keyed by field id."""

    return {
        field.field_id: field.default
        for field in advanced_fields(kind, preset_id)
        if not field.secret
    }


def advanced_visible_groups(kind: str, preset_id: str) -> Tuple[str, ...]:
    """Which groups have something in them. An empty group is not drawn."""

    present = {field.group for field in advanced_fields(kind, preset_id)}
    present.add(ADVANCED_GROUP_PERMISSIONS)
    return tuple(group for group in ADVANCED_GROUPS if group in present)


# Which fields cannot change under a live bridge. Editing any of these moves the
# address the service is already talking to, so applying one silently would
# break a working connection with no explanation on screen.
_RESTART_REQUIRED_FIELDS = frozenset(
    {"transport", "port", "tunnel", "endpoint_path"}
)


def advanced_requires_restart(changed: Sequence[str]) -> bool:
    return any(str(field) in _RESTART_REQUIRED_FIELDS for field in changed)


def advanced_changed_fields(
    before: Mapping[str, str], after: Mapping[str, str]
) -> Tuple[str, ...]:
    """Which fields the user actually changed.

    An empty optional value is "use the default", not "set to blank", so it is
    not reported as a change and cannot overwrite a working value with nothing.
    """

    changed: List[str] = []
    for key, value in after.items():
        new = str(value or "").strip()
        if not new:
            continue
        if new != str(before.get(key, "") or "").strip():
            changed.append(key)
    return tuple(changed)


def validate_advanced(
    kind: str, preset_id: str, values: Mapping[str, str], english: bool
) -> Tuple[str, str]:
    """Check one form. Returns ``(field_id, message)``, empty when valid.

    The field id travels with the message so the screen can focus the offending
    input rather than making the user hunt for it. Checks are deliberately the
    few this layer can make honestly -- required, numeric port, plausible URL --
    and everything deeper stays with the controller that owns it, because a
    second validation library is a second set of rules to drift.
    """

    for field in advanced_fields(kind, preset_id):
        raw = str(values.get(field.field_id, "") or "").strip()
        if field.required and not raw and not field.default:
            return (
                field.field_id,
                f"{field.label(english)}: "
                + ("required" if english else "обязательное поле"),
            )
    port = str(values.get("port", "") or "").strip()
    if port:
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            return (
                "port",
                "Port must be a number between 1 and 65535"
                if english
                else "Порт должен быть числом от 1 до 65535",
            )
    url = str(values.get("base_url", "") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        return (
            "base_url",
            "Base URL must start with http:// or https://"
            if english
            else "Base URL должен начинаться с http:// или https://",
        )
    timeout = str(values.get("timeout", "") or "").strip()
    if timeout and not timeout.isdigit():
        return (
            "timeout",
            "Timeout must be a number of seconds"
            if english
            else "Таймаут должен быть числом секунд",
        )
    return ("", "")


_CAPABILITY_GLYPHS = {"true": "✓", "false": "✗", "unknown": "?"}

_MODEL_CAPABILITY_FIELDS = (
    ("tools", "tools"),
    ("vision", "vision"),
    ("json", "structured_output"),
    ("stream", "streaming"),
)


def model_capability_summary(model: Any) -> str:
    """Honest one-line capability row for a saved model.

    Renders the registry's true/false/unknown contract exactly as recorded:
    ``✓`` for true, ``✗`` for false, and ``?`` for unknown. Unknown is
    shown, never guessed into a promise the provider may not keep.
    """

    parts: list[str] = []
    for label, attribute in _MODEL_CAPABILITY_FIELDS:
        value = str(getattr(model, attribute, "unknown"))
        parts.append(f"{label}{_CAPABILITY_GLYPHS.get(value, '?')}")
    pricing = getattr(model, "pricing", None)
    if pricing is not None:
        input_price = getattr(pricing, "input_per_million", None)
        output_price = getattr(pricing, "output_per_million", None)
        if input_price is not None and output_price is not None:
            if float(input_price) == 0 and float(output_price) == 0:
                parts.append("free")
            else:
                parts.append(
                    f"${float(input_price):g}/M in ${float(output_price):g}/M out"
                )
    return " ".join(parts)


def discovered_model_record(provider_id: str, item: Any) -> Any:
    """A registry record for one discovered model, metadata carried honestly.

    Capabilities keep the discovery's true/false/unknown verdicts exactly;
    pricing is attached only when the catalog published both token prices,
    with source "provider-reported" so provenance stays next to the number.
    Nothing is upgraded from unknown here.
    """

    from .registry import ModelPricing, ModelRecord

    pricing = None
    input_price = getattr(item, "input_per_million", None)
    output_price = getattr(item, "output_per_million", None)
    if input_price is not None and output_price is not None:
        pricing = ModelPricing(
            version="discovered",
            currency="USD",
            input_per_million=float(input_price),
            output_per_million=float(output_price),
            cache_read_per_million=getattr(item, "cache_read_per_million", None),
            source="provider-reported",
        )
    return ModelRecord(
        provider_id=provider_id,
        model_id=item.model_id,
        context_window=item.context_window,
        max_output_tokens=item.max_output_tokens,
        tools=str(getattr(item, "tools", "unknown")),
        vision=str(getattr(item, "vision", "unknown")),
        structured_output=str(getattr(item, "structured_output", "unknown")),
        streaming=str(getattr(item, "streaming", "unknown")),
        pricing=pricing,
        provenance="discovered",
        display_name=getattr(item, "display_name", None),
    )


def discover_models_for_provider(controller: Any, provider_id: str) -> Dict[str, Any]:
    """Discover models for a saved provider and register only the new ones.

    Uses the same discovery wire as the setup wizard and the CLI
    (``_discover_models_result``), with the credential resolved through the
    controller that owns it. New records carry ``provenance="discovered"``
    plus whatever capability and pricing metadata the catalog published;
    existing records are never overwritten, and when discovery lands on a
    different effective base URL (the automatic ``/v1`` fallback) the provider
    record follows it, mirroring ``karox model discover``. The returned
    summary is secret-free.
    """

    from .tui import ProviderSetup, _discover_models_result

    details = controller.details(provider_id)
    provider = details.provider
    api_key = ""
    if provider.credential_ref:
        try:
            api_key = controller.credentials.resolve(provider.credential_ref)
        except Exception:
            api_key = ""
    discovery = _discover_models_result(
        ProviderSetup(
            provider_id=provider.provider_id,
            adapter=provider.adapter_kind,
            base_url=provider.base_url,
            model_id="",
            api_key=api_key,
        )
    )
    if discovery.base_url != provider.base_url:
        controller.edit_provider(provider.provider_id, base_url=discovery.base_url)
    existing = {model.model_id for model in details.models}
    added: list[str] = []
    for item in discovery.models:
        if item.model_id in existing:
            continue
        controller.put_model(
            discovered_model_record(provider.provider_id, item)
        )
        added.append(item.model_id)
    return {
        "discovered": len(discovery.models),
        "added": added,
        "base_url": discovery.base_url,
    }


def secret_display(has_secret: bool, english: bool) -> str:
    """What is shown where a stored secret would be. Never the secret.

    "saved" plus an optional replacement field: enough for the user to know the
    credential exists, and nothing a shoulder or a screen recording can use.
    """

    if has_secret:
        return "saved" if english else "сохранён"
    return "not set" if english else "не задан"


# ---------------------------------------------------------------------------
# B5. Managing a record that already exists.
#
# One saved record, one source of state, one set of actions. There is no second
# list and no management dashboard: the actions below are what a person can do
# after opening a row they can already see in the hub.
#
# Everything here is presentation over the registries proven in
# `tests/test_connection_enablement.py`. This layer decides which words and
# which actions a state offers; it never decides what the state *is*.

DETAIL_VERIFY = "detail_verify"
DETAIL_RECOVER = "detail_recover"
DETAIL_RESTART = "detail_restart"
DETAIL_EDIT = "detail_edit"
DETAIL_DISABLE = "detail_disable"
DETAIL_ENABLE = "detail_enable"
DETAIL_COPY_AUTH = "detail_copy_auth"
DETAIL_DELETE = "detail_delete"
DETAIL_BACK = "detail_back"

_DETAIL_ACTION_WORDS: Dict[str, Tuple[str, str]] = {
    DETAIL_VERIFY: ("Проверить", "Verify"),
    DETAIL_RECOVER: ("Восстановить", "Repair"),
    DETAIL_RESTART: ("Перезапустить", "Restart"),
    DETAIL_EDIT: ("Изменить", "Edit"),
    DETAIL_DISABLE: ("Отключить", "Disable"),
    DETAIL_ENABLE: ("Включить", "Enable"),
    DETAIL_COPY_AUTH: ("Скопировать ключ авторизации", "Copy authorization key"),
    DETAIL_DELETE: ("Удалить", "Delete"),
    DETAIL_BACK: ("Назад", "Back"),
}


def detail_action_words(action: str, english: bool) -> str:
    words = _DETAIL_ACTION_WORDS.get(action)
    if words is None:
        return action
    return words[1] if english else words[0]


def detail_actions(
    enabled: bool,
    *,
    kind: str = "",
    has_secret: bool = False,
    running: Optional[bool] = None,
) -> Tuple[str, ...]:
    """Which actions a record offers.

    A disabled record offers Enable in place of Disable and keeps everything
    else. Hiding Verify or Edit while parked would make disabling a trap: the
    user could not repair the thing they turned off because it was off.

    "Copy authorization key" appears only on a service record that has a
    credential: a provider points at an API key, not a bearer, so copying
    ``Bearer <secret>`` from one would hand a client the wrong header.
    """

    actions: List[str] = [DETAIL_VERIFY]
    if kind == "service" and running is not None and enabled:
        actions.append(DETAIL_RESTART if running else DETAIL_RECOVER)
    actions.extend(
        [
            DETAIL_EDIT,
            DETAIL_DISABLE if enabled else DETAIL_ENABLE,
        ]
    )
    if kind == "service" and has_secret:
        actions.append(DETAIL_COPY_AUTH)
    actions.append(DETAIL_DELETE)
    actions.append(DETAIL_BACK)
    return tuple(actions)


def detail_title(name: str, detail: str) -> str:
    """The human name, and the model or service when there is one.

    Plainly ``str`` again. This was briefly widened to ``Optional[str]`` to
    satisfy the type checker while the screen's own field shadowed Textual's
    ``_name``; the shadowing is gone, so the accommodation for it goes too.
    A workaround kept after its cause is fixed only teaches the next reader
    that this argument can be ``None``, which it cannot.
    """

    return f"{name} · {detail}" if detail else name


def delete_confirmation(name: str, detail: str, english: bool) -> Tuple[str, str]:
    """The question asked before anything is removed, and the warning under it.

    Named here so the confirmation cannot drift from the record it is about: it
    is built from the same title the detail screen shows, not from a second
    lookup that could be one refresh out of date and ask about the wrong row.
    """

    subject = detail_title(name, detail)
    if english:
        return (
            f"Delete {subject}?",
            "The configuration will be deleted. This cannot be undone.",
        )
    return (
        f"Удалить {subject}?",
        "Конфигурация будет удалена. Это действие нельзя отменить.",
    )


def credential_deletion_note(has_secret: bool, english: bool) -> str:
    """Whether the stored secret goes too, stated plainly and honestly.

    The production remover deletes the credential the record points at, so this
    says so rather than leaving the user to guess. It never names the reference
    itself -- what store it lives in is KaroX's business, that it will be gone
    is the user's.
    """

    if not has_secret:
        return ""
    return (
        "The saved key will also be removed from the system store."
        if english
        else "Сохранённый ключ также будет удалён из системного хранилища."
    )


def active_disable_warning(kind: str, running: bool, english: bool) -> str:
    """What switching this off costs, said before it is switched off.

    Two different honest sentences, because the two families lose two different
    things. A provider stops being available to new tasks; a running bridge
    additionally has a live address somebody may already be using, and the
    process behind it is *not* stopped by disabling alone.
    """

    if kind == "provider":
        return (
            "New tasks will not be able to use this model."
            if english
            else "Новые задачи не смогут использовать эту модель."
        )
    if running:
        return (
            "The connection will not be started again. The running process "
            "keeps serving its address until you stop it."
            if english
            else "Подключение больше не будет запускаться. Запущенный процесс "
            "продолжает обслуживать свой адрес, пока вы его не остановите."
        )
    return (
        "The connection will not be started for new work."
        if english
        else "Подключение не будет запускаться для новых задач."
    )


def provider_advanced_values(details: Any) -> Dict[str, str]:
    """Advanced form values read from a *saved* provider, not from its preset.

    B5 audit 5.6/3. The preset is the catalogue entry the record was created
    from; it is not the record. A person who moved their endpoint to a regional
    URL last month and opens Advanced today must see the URL they are actually
    using, not the one openrouter.ai ships with -- and must not have the
    catalogue value silently written back over theirs by pressing Save.

    The preset stays as a fallback for a value the record genuinely lacks.
    """

    provider = getattr(details, "provider", None)
    if provider is None:
        return {}
    model = getattr(details, "selected_model", None)
    models = tuple(getattr(details, "models", ()) or ())
    if model is None and len(models) == 1:
        model = models[0]
    values: Dict[str, str] = {
        "connection_name": str(getattr(provider, "provider_id", "") or ""),
        "base_url": str(getattr(provider, "base_url", "") or ""),
        "adapter": str(getattr(provider, "adapter_kind", "") or ""),
    }
    timeout = getattr(provider, "timeout_seconds", None)
    if timeout:
        values["timeout"] = str(int(timeout))
    if model is not None:
        context = getattr(model, "context_window", None)
        output = getattr(model, "max_output_tokens", None)
        if context:
            values["context_window"] = str(context)
        if output:
            values["max_output"] = str(output)
    return values


def service_advanced_values(target: Any) -> Dict[str, str]:
    """Advanced form values read from a saved connection record.

    Same rule as the provider case, and the same reason. What is deliberately
    absent is as important as what is here: no credential reference and no
    fingerprint. Those are how KaroX finds the secret, and a management screen
    has no use for them that is worth putting them on a display.
    """

    if target is None:
        return {}
    values: Dict[str, str] = {
        "connection_name": str(getattr(target, "name", "") or ""),
        "transport": str(getattr(target, "transport", "") or ""),
        "endpoint_path": str(getattr(target, "endpoint_path", "") or ""),
        "tunnel": str(getattr(target, "tunnel", "") or ""),
    }
    port = getattr(target, "port", None)
    if port:
        values["port"] = str(port)
    return values


def apply_provider_advanced(
    controller: Any,
    provider_id: str,
    values: Mapping[str, str],
) -> None:
    """Write advanced provider settings through the controller that owns them.

    B5 audit 5.6. This is the function whose absence was the defect: Save set
    ``self.applied`` and dismissed, and every caller only re-read the record --
    so the screen reported success while the disk was untouched. A settings
    form that cannot fail and cannot persist is the worst of both.

    Identity and enabled state are carried from the stored record rather than
    from the form, because the form has no field for either.
    """

    from dataclasses import replace as _replace

    current = controller.registry.provider(provider_id)
    timeout = str(values.get("timeout", "") or "").strip()
    updated = _replace(
        current,
        base_url=str(values.get("base_url", "") or current.base_url).rstrip("/"),
        adapter_kind=str(values.get("adapter", "") or current.adapter_kind),
        timeout_seconds=float(timeout) if timeout else current.timeout_seconds,
    )
    controller.registry.put_provider(updated)

    # B5 section 5. The limits belong to a *model*, not to the provider, so
    # they are only applied when the screen knows without guessing which model
    # is meant: the selected one, or the only one there is.
    #
    # With several models and no selection there is no honest answer, and
    # silently rewriting an arbitrary one would be the worst available
    # behaviour -- the user would have changed limits on a model they were not
    # looking at. The fields are simply not applied in that case.
    details = controller.details(provider_id)
    model = getattr(details, "selected_model", None)
    models = tuple(getattr(details, "models", ()) or ())
    if model is None and len(models) == 1:
        model = models[0]
    if model is None:
        return
    context = str(values.get("context_window", "") or "").strip()
    output = str(values.get("max_output", "") or "").strip()
    if not context and not output:
        return
    controller.registry.put_model(
        _replace(
            model,
            context_window=int(context) if context.isdigit() else model.context_window,
            max_output_tokens=(
                int(output) if output.isdigit() else model.max_output_tokens
            ),
        )
    )


def apply_service_advanced(
    controller: Any,
    connection_id: str,
    values: Mapping[str, str],
) -> None:
    """Write advanced service settings through the connection registry.

    ``dataclasses.replace`` on the stored record rather than a fresh
    ``McpClientTarget``: rebuilding one by hand drops every field the form does
    not show, and the two that matter most are ``enabled`` and
    ``credential_ref``. A rebuild would quietly re-enable a parked connection
    through the dataclass default and orphan its secret.
    """

    from dataclasses import replace as _replace

    current = controller.registry.get(connection_id)
    port = str(values.get("port", "") or "").strip()
    updated = _replace(
        current,
        name=str(values.get("connection_name", "") or current.name),
        transport=str(values.get("transport", "") or current.transport),
        endpoint_path=str(values.get("endpoint_path", "") or current.endpoint_path),
        tunnel=str(values.get("tunnel", "") or current.tunnel),
        port=int(port) if port.isdigit() else current.port,
    )
    # B5 section 4. Through the controller transaction, not a bare
    # `registry.put`. A bare write here is what let the screen promise a
    # restart, save a new configuration, and leave the old bridge serving the
    # old one -- saved state, running state and the user's understanding all
    # disagreeing at once. The controller stops, relaunches and restores; a
    # failure raises, and the screen stays open because of it.
    updater = getattr(controller, "update_running_connection", None)
    if updater is None:
        controller.registry.put(updated)
        return
    updater(connection_id, updated)


def active_delete_warning(kind: str, running: bool, english: bool) -> str:
    """What deleting this stops, which is not what disabling it stops.

    B5 audit 5.8. These two must not share a sentence, because the production
    contracts differ and the difference is the whole point:

    * ``ConnectionRegistry.set_enabled`` writes a flag. Nothing is stopped, and
      a live bridge keeps serving the address its user already pasted.
    * ``ConnectionController.remove`` stops a ``running`` or ``degraded``
      runtime *before* removing the record, and then deletes the credential
      through ``remove_connection``.

    Reusing the disable wording here -- which this screen briefly did -- told
    the user their bridge would keep running while the production path was
    about to stop it. Read from the controller, not from the neighbouring
    feature.
    """

    if kind == "provider":
        return (
            "New tasks will not be able to use this model."
            if english
            else "Новые задачи не смогут использовать эту модель."
        )
    if running:
        return (
            "The running bridge will be stopped and its address will stop "
            "working."
            if english
            else "Запущенный мост будет остановлен, и его адрес перестанет работать."
        )
    return (
        "This connection will no longer be available."
        if english
        else "Это подключение больше не будет доступно."
    )


def merge_hub_rows(*groups: Sequence[HubRow]) -> Tuple[HubRow, ...]:
    """One list, and one entry per connection.

    ``row_id`` is namespaced by family and carries the identity its own store
    assigns, so the same connection reached through two code paths collapses to
    one row instead of appearing twice under slightly different words -- the
    failure the old two-list hub made easy.
    """

    seen: Dict[str, HubRow] = {}
    for group in groups:
        for row in group:
            seen.setdefault(row.row_id, row)
    return tuple(seen.values())


# ---------------------------------------------------------------------------
# Hub: Connections → MCP Clients / Model Providers / new provider.
# ---------------------------------------------------------------------------


class _DiscoveredBridgeState:
    """A lightweight ConnectionState-like object for a discovered bridge profile.

    When a ChatGPT Web bridge was launched but never registered in
    ``connections.json``, the ServiceConnectScreen synthesizes this object
    from the saved bridge config so the screen can show the existing endpoint,
    saved profile, and runtime state without a registry record.
    """

    def __init__(self, profile_data: dict, preset_id: str) -> None:
        self._data = profile_data
        self._preset_id = preset_id
        self.target = _DiscoveredBridgeTarget(profile_data, preset_id)
        self.runtime = {
            "connection_id": f"discovered-{profile_data.get('session_id', '')}",
            "session_id": str(profile_data.get("session_id") or ""),
            "state": "discovered",
            "managed": False,
            "bridge_pid": profile_data.get("bridge_pid"),
            "tunnel_pid": profile_data.get("tunnel_pid"),
            "public_endpoint": str(profile_data.get("public_url") or ""),
            "tunnel": str(profile_data.get("tunnel") or ""),
        }
        public_url = str(profile_data.get("public_url") or "")
        self.endpoint = f"{public_url}/mcp" if public_url else ""


class _DiscoveredBridgeTarget:
    """A lightweight McpClientTarget-like object for a discovered bridge."""

    def __init__(self, profile_data: dict, preset_id: str) -> None:
        self.connection_id = f"discovered-{profile_data.get('session_id', '')}"
        self.name = str(profile_data.get("saved_profile") or "")
        self.preset_id = preset_id
        self.repository = str(profile_data.get("repository") or "")
        self.enabled = True


def build_connections_screens(base_app: Any) -> Dict[str, type]:
    """Build the connections screen classes bound to the host app.

    The screens need a couple of host-app callbacks (clipboard copy, status
    refresh, and the existing provider-setup wizard). They are passed in rather
    than imported so this module stays free of a hard dependency on ``tui.py``
    and its ``KaroXApp`` internals.
    """
    # ``base_app`` is the running app instance, so this is a *bound* method and
    # takes only the text. Typing it as unbound made every call site look like
    # it was missing an argument.
    copy_text: Callable[[str], None] = base_app._copy_text
    refresh_status: Callable[[Any], None] = lambda self: base_app._refresh_status()

    class ConnectionHubScreen(ModalScreen[Optional[str]]):
        """The one connection screen: what is connected, and what can be added.

        B1. This replaces a three-button menu naming KaroX's own protocol
        families -- "MCP clients", "Model providers", "new API provider". Two
        things were wrong with that and neither was cosmetic. It asked the user
        to classify a connection before showing them what already exists, which
        is the question they opened the screen to have answered. And the two
        lists behind it were separate screens over separate stores, so nothing
        in the product ever showed "everything that is connected" in one place.

        There is still no new store. The rows are a projection of the provider
        registry and the connection controller -- the two things that were
        already the source of truth -- merged by :func:`merge_hub_rows`.

        Keyboard-first by construction: one ``OptionList`` holds the saved rows
        and the add actions alike, so arrows move through everything, Enter
        opens whatever is highlighted, and no action hides behind a button that
        needs a mouse.
        """

        BINDINGS = [
            Binding("enter", "choose", "Open", priority=True),
            Binding("a", "add_model", _C["en"]["add"], priority=True),
            Binding("up", "previous", show=False, priority=True),
            Binding("down", "next", show=False, priority=True),
            Binding("escape", "cancel", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        ConnectionHubScreen { align: center middle; background: #0e0c08 92%; }
        /* Narrow and short on purpose. A connection list is a handful of lines,
           and a dialog sized for a dashboard would invite one. `max-width`
           rather than a fixed width so 120x30 does not grow a side panel with
           nothing to put in it. */
        #connhub-dialog { width: 100%; max-width: 62; height: auto;
          max-height: 90%; background: #1a1712; border: round #c6a56b;
          padding: 1 2; }
        #connhub-dialog .title { text-style: bold; color: #e5e5e5; }
        #connhub-list { height: auto; max-height: 16; border: none;
          background: #1a1712; }
        #connhub-hint { color: #8a7e6a; margin-top: 1; }
        #connhub-error { color: #e0a3a3; min-height: 0; text-wrap: wrap; }
        """

        def __init__(
            self,
            language: str = "ru",
            *,
            select: Optional[str] = None,
            after_delete: Optional[str] = None,
        ) -> None:
            super().__init__()
            self.language = language
            # B5. Where to land. `select` is a row that should still exist;
            # `after_delete` is one that should not, and asks for its former
            # neighbour instead.
            self._select = select
            self._after_delete = after_delete
            self._rows: Tuple[HubRow, ...] = ()
            # Counts belong only to the management links. The root itself shows
            # current state, while the full historical registries remain one
            # deliberate Enter away.
            self._provider_count = 0
            self._saved_connection_count = 0
            self._controller = connection_controller()
            from .connection_tests import test_model_provider
            from .paths import config_dir
            from .registry import ProviderRegistry

            self._providers = ProviderController(
                registry=ProviderRegistry(
                    config_dir() / "vnext" / "providers.json"
                ),
                credentials=CredentialStore(),
                tester=lambda provider, model: test_model_provider(
                    provider, model, timeout_seconds=30.0
                ),
            )

        def _english(self) -> bool:
            return self.language != "ru"

        def rows(self) -> Tuple[HubRow, ...]:
            """The rows currently on screen, for callers that need the model."""

            return self._rows

        def _collect(self) -> Tuple[HubRow, ...]:
            """Read both existing sources. Neither is allowed to break the screen.

            A keyring that refuses to open or a runtime that cannot be probed
            costs the rows it owns, not the hub: a person whose ClickUp bridge
            is wedged still needs to reach the provider list.
            """

            try:
                all_providers = provider_hub_rows(self._providers.registry)
                providers = current_provider_hub_rows(self._providers.registry)
                self._provider_count = len(all_providers)
            except Exception:
                providers = ()
                self._provider_count = 0
            try:
                all_states = self._controller.list()
                all_services = service_hub_rows(all_states)
                services = active_service_hub_rows(all_states)
                self._saved_connection_count = len(all_services)
            except (ConnectionError, ConnectionRuntimeError, CredentialError):
                services = ()
                self._saved_connection_count = 0
            except Exception:
                services = ()
                self._saved_connection_count = 0
            return merge_hub_rows(providers, services)

        def compose(self) -> ComposeResult:
            # VerticalScroll: at 46x14-class terminals the title + list + hint
            # exceed max-height and a plain Vertical clipped the hint and error
            # rows with no way to reach them.
            with VerticalScroll(id="connhub-dialog"):
                yield Static(
                    _label(
                        self.language, _C["ru"]["hub_title"], _C["en"]["hub_title"]
                    ),
                    classes="title",
                )
                yield OptionList(id="connhub-list")
                yield Static("", id="connhub-error", markup=False)
                yield Static(self._hint(), id="connhub-hint")

        def _hint(self) -> str:
            """The three keys that matter, and nothing about the other twenty.

            Shortened on a narrow terminal rather than wrapped: two rows of key
            hints under a four-row list is the screen explaining itself more
            than it explains the connections.
            """

            try:
                narrow = int(self.app.size.width) < 60
            except Exception:
                narrow = False
            if narrow:
                return _label(
                    self.language,
                    "Enter — открыть · Esc — назад",
                    "Enter — open · Esc — back",
                )
            return _label(
                self.language,
                "Enter — открыть · A — добавить модель · Esc — назад",
                "Enter — open · A — add a model · Esc — back",
            )

        def on_mount(self) -> None:
            # Textual can deliver a screen's Mount before its children are
            # mounted, and this screen's first act is to preset the list with a
            # loading row; querying then crashed the hub with "No nodes match
            # '#connhub-list'" on a loaded runner. Take the normal path when the
            # children are already there and defer only when they are not: an
            # unconditional deferral cost an extra event-loop hop on every open
            # and pushed another test's bounded wait over its limit.
            if next(iter(self.query("#connhub-list")), None) is None:
                self.call_after_refresh(self._begin_hub_refresh)
                return
            self._begin_hub_refresh()

        def _begin_hub_refresh(self) -> None:
            options = self.query_one("#connhub-list", OptionList)
            options.clear_options()
            options.add_option(
                Option(
                    _label(self.language, "  Обновляем подключения…", "  Refreshing connections…"),
                    id="connhub-loading",
                    disabled=True,
                )
            )
            options.focus()
            select = self._select or self._after_delete

            def execute() -> None:
                try:
                    rows = self._collect()
                except Exception:
                    rows = ()
                try:
                    self.app.call_from_thread(self._hub_rows_ready, rows, select)
                except Exception:
                    return

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="connhub-refresh",
            )

        def _hub_rows_ready(
            self,
            rows: Tuple[HubRow, ...],
            select: Optional[str],
        ) -> None:
            if not self.is_mounted:
                return
            self.refresh_rows(select=select, rows=rows)

        def on_resize(self, _event: Any = None) -> None:
            self.query_one("#connhub-hint", Static).update(self._hint())

        def refresh_rows(
            self,
            *,
            select: Optional[str] = None,
            rows: Optional[Tuple[HubRow, ...]] = None,
        ) -> None:
            """Redraw the list, keeping the cursor where the user left it.

            ``select`` names the row to land on after a save, so returning from
            a flow puts the thing just created under the cursor instead of
            resetting to the top of the list.
            """

            options = self.query_one("#connhub-list", OptionList)
            previous = select
            if previous is None:
                highlighted = options.highlighted
                if highlighted is not None and highlighted < len(options.options):
                    previous = getattr(
                        options.get_option_at_index(highlighted), "id", None
                    )

            self._rows = rows if rows is not None else self._collect()
            english = self._english()
            options.clear_options()

            index_of: Dict[str, int] = {}
            cursor = 0
            first_current: Optional[int] = None
            ai_rows = tuple(row for row in self._rows if row.family == HUB_FAMILY_AI)
            service_rows = tuple(row for row in self._rows if row.family != HUB_FAMILY_AI)

            if ai_rows:
                options.add_option(
                    Option(
                        _label(self.language, "Текущая AI-модель", "Current AI model"),
                        id="connhub-heading-ai",
                        disabled=True,
                    )
                )
                cursor += 1
                for row in ai_rows:
                    index_of[row.row_id] = cursor
                    if first_current is None:
                        first_current = cursor
                    options.add_option(
                        Option("  " + hub_row_text(row, english), id=row.row_id)
                    )
                    cursor += 1

            if service_rows:
                if cursor:
                    options.add_option(Option("", id="connhub-gap-current", disabled=True))
                    cursor += 1
                options.add_option(
                    Option(
                        _label(self.language, "Активные подключения", "Active connections"),
                        id="connhub-heading-services",
                        disabled=True,
                    )
                )
                cursor += 1
                for row in service_rows:
                    index_of[row.row_id] = cursor
                    if first_current is None:
                        first_current = cursor
                    options.add_option(
                        Option("  " + hub_row_text(row, english), id=row.row_id)
                    )
                    cursor += 1

            if cursor:
                options.add_option(Option("", id="connhub-gap-add", disabled=True))
                cursor += 1
            options.add_option(
                Option(
                    _label(self.language, "Добавить", "Add"),
                    id="connhub-heading-add",
                    disabled=True,
                )
            )
            cursor += 1
            first_action = cursor
            for action in HUB_ADD_ACTIONS:
                index_of[action] = cursor
                options.add_option(
                    Option("  " + hub_add_words(action, english), id=action)
                )
                cursor += 1

            options.add_option(Option("", id="connhub-gap-manage", disabled=True))
            cursor += 1
            options.add_option(
                Option(
                    _label(self.language, "Управление", "Manage"),
                    id="connhub-heading-manage",
                    disabled=True,
                )
            )
            cursor += 1
            for action in HUB_MANAGE_ACTIONS:
                index_of[action] = cursor
                count = (
                    self._provider_count
                    if action == HUB_MANAGE_MODELS
                    else self._saved_connection_count
                )
                options.add_option(
                    Option(
                        "  " + hub_manage_words(action, english, count=count),
                        id=action,
                    )
                )
                cursor += 1

            # The root is now a "current state" view. Prefer the current model
            # or active service; with nothing active, land on the first add
            # action instead of a historical management entry.
            target = index_of.get(previous or "")
            if target is None:
                target = first_current if first_current is not None else first_action
            options.highlighted = target

        def _set_error(self, message: str) -> None:
            self.query_one("#connhub-error", Static).update(
                str(redact(message)) if message else ""
            )

        def _highlighted_id(self) -> Optional[str]:
            options = self.query_one("#connhub-list", OptionList)
            highlighted = options.highlighted
            if highlighted is None or highlighted >= len(options.options):
                return None
            return getattr(options.get_option_at_index(highlighted), "id", None)

        def _step(self, delta: int) -> None:
            """Move the cursor, stepping over the headings.

            A disabled heading that can hold the cursor is a dead press of the
            arrow key, and two of them in a short list is enough for the
            keyboard to feel broken.
            """

            options = self.query_one("#connhub-list", OptionList)
            count = len(options.options)
            if not count:
                return
            index = options.highlighted if options.highlighted is not None else 0
            for _ in range(count):
                index = max(0, min(count - 1, index + delta))
                option = options.get_option_at_index(index)
                if not getattr(option, "disabled", False):
                    options.highlighted = index
                    options.scroll_to_highlight()
                    return
                if index in (0, count - 1):
                    return

        def action_previous(self) -> None:
            self._step(-1)

        def action_next(self) -> None:
            self._step(1)

        def action_add_model(self) -> None:
            self.dismiss(HUB_ADD_MODEL)

        def neighbour_of(self, row_id: str) -> Optional[str]:
            """The row a cursor should fall to if ``row_id`` disappears.

            B5. Computed here, while the list still contains the row, because
            afterwards nothing can reconstruct where it used to be. The one
            below is preferred so a run of deletions walks down the list the
            way a person expects, and the one above is the fallback at the end.
            """

            identifiers = [row.row_id for row in self._rows]
            if row_id not in identifiers:
                return None
            index = identifiers.index(row_id)
            if index + 1 < len(identifiers):
                return identifiers[index + 1]
            if index > 0:
                return identifiers[index - 1]
            return None

        def _open(self, chosen: Optional[str]) -> None:
            if not chosen or chosen.startswith("connhub-"):
                return
            # Hand the neighbour to the host app before leaving, so a delete on
            # the child screen has somewhere honest to land on the way back.
            with contextlib.suppress(Exception):
                base_app._hub_neighbour = self.neighbour_of(chosen)
            self.dismiss(chosen)

        def action_choose(self) -> None:
            self._open(self._highlighted_id())

        def action_cancel(self) -> None:
            self.dismiss(None)

        @on(OptionList.OptionSelected, "#connhub-list")
        def _selected_event(self, event: OptionList.OptionSelected) -> None:
            self._open(getattr(event.option, "id", None))

    # --- MCP clients list ------------------------------------------------

    class McpClientsScreen(ModalScreen[Optional[str]]):
        """List saved MCP client targets; add/edit/delete/test/copy."""

        BINDINGS = [
            Binding("a", "add", _C["en"]["add"], priority=True),
            Binding("e", "edit", _C["en"]["edit"], priority=True),
            Binding("t", "test", _C["en"]["test"], priority=True),
            Binding("c", "copy_url", _C["en"]["copy_url"], priority=True),
            Binding("s", "copy_secret", _C["en"]["copy_secret"], priority=True),
            Binding("r", "start", "Start / restart", priority=True),
            Binding("x", "stop", _C["en"]["cu_stop"], priority=True),
            # The focused OptionList handles arrows. Global priority arrows made
            # a hidden list keep moving after Tab focused an action button.
            Binding("delete", "delete", _C["en"]["delete"], priority=True),
            Binding("escape", "cancel", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        McpClientsScreen { align: center middle; background: #0e0c08 92%; }
        #mcp-list-dialog { width: 86; height: 86%; background: #1a1712;
          border: round #c6a56b; padding: 1 2; }
        #mcp-list-dialog .title { text-style: bold; color: #e5e5e5; }
        #mcp-list-dialog .hint { color: #8a7e6a; margin-bottom: 1; }
        #mcp-connections { height: 1fr; border: round #4a4338; }
        #mcp-list-error { color: #e0a3a3; min-height: 1; max-height: 4;
          text-wrap: wrap; overflow-y: auto; }
        #mcp-list-actions { height: 3; }
        #mcp-list-actions Button { margin-right: 1; }
        #mcp-list-buttons { height: 3; align-horizontal: right; }
        #mcp-list-buttons Button { margin-left: 1; }
        """

        def __init__(self, language: str = "ru") -> None:
            super().__init__()
            self.language = language
            self._controller = connection_controller()
            # Keep the registry alias for the existing edit form and tests while
            # list/details/runtime operations move behind the controller.
            self._registry = self._controller.registry
            self._refreshing = False
            self._refresh_epoch = 0
            self._poll_timer: Optional[Any] = None

        def _label(self, ru: str, en: str) -> str:
            return _label(self.language, ru, en)

        def _items(self) -> List[Any]:
            try:
                return self._controller.list()
            except (ConnectionError, ConnectionRuntimeError) as exc:
                self._set_error(str(exc))
                return []

        def _row(self, state: Any) -> str:
            item = state.target
            return (
                f"{item.name}  [{state.state}]  "
                f"({item.preset_id} · {item.transport} · {item.auth_scheme})"
            )

        def compose(self) -> ComposeResult:
            with Vertical(id="mcp-list-dialog"):
                yield Static(self._label("MCP-клиенты", "MCP clients"), classes="title")
                yield Static(_t(self.language, "list_hint"), classes="hint")
                yield OptionList(id="mcp-connections")
                yield Static("", id="mcp-list-error", markup=False)
                # HorizontalScroll: seven labelled buttons exceed the dialog
                # width in Russian; cropping them hid Copy secret/Stop entirely,
                # so the row scrolls instead of silently eating actions.
                with HorizontalScroll(id="mcp-list-actions"):
                    yield Button(self._label("Добавить", "Add"), id="mcp-add")
                    yield Button(self._label("Изменить", "Edit"), id="mcp-edit")
                    yield Button(self._label("Проверить", "Verify"), id="mcp-test")
                    yield Button(self._label("Копировать URL", "Copy URL"), id="mcp-copy-url")
                    yield Button(self._label("Копировать секрет", "Copy secret"), id="mcp-copy-secret")
                    yield Button(self._label("Запустить / перезапустить", "Start / restart"), id="mcp-start")
                    yield Button(self._label("Остановить", "Stop"), id="mcp-stop")
                with Horizontal(id="mcp-list-buttons"):
                    yield Button(self._label("Закрыть", "Close"), id="mcp-close")

        def on_mount(self) -> None:
            # Runtime/PID reconciliation is not UI work. Focus immediately and
            # let a worker provide the first snapshot and all later polling.
            self.query_one("#mcp-connections", OptionList).focus()
            self._schedule_refresh(force=True)
            self._poll_timer = self.set_interval(1.0, self._schedule_refresh)

        def on_unmount(self) -> None:
            self._refresh_epoch += 1
            self._refreshing = False
            timer = self._poll_timer
            self._poll_timer = None
            if timer is not None:
                with contextlib.suppress(Exception):
                    timer.stop()

        def _schedule_refresh(self, *, force: bool = False) -> None:
            if self._refreshing and not force:
                return
            self._refresh_epoch += 1
            epoch = self._refresh_epoch
            self._refreshing = True

            def execute() -> None:
                try:
                    items = self._items()
                except Exception:
                    items = []
                with contextlib.suppress(Exception):
                    self.app.call_from_thread(self._refresh_done, epoch, items)

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="mcp-list-refresh",
            )

        def _refresh_done(self, epoch: int, items: List[Any]) -> None:
            if epoch != self._refresh_epoch or not self.is_mounted:
                return
            self._refreshing = False
            self._refresh(items=items)

        def _refresh(self, *, items: Optional[List[Any]] = None) -> None:
            options = self.query_one("#mcp-connections", OptionList)
            selected_id: Optional[str] = None
            highlighted = options.highlighted
            if highlighted is not None and highlighted < len(options.options):
                selected_id = getattr(
                    options.get_option_at_index(highlighted), "id", None
                )

            items = self._items() if items is None else items
            options.clear_options()
            if not items:
                options.add_option(Option(self._label(_C["ru"]["no_connections"], _C["en"]["no_connections"]), id="mcp-empty"))
                options.highlighted = 0
                return

            selected_index = 0
            for index, item in enumerate(items):
                options.add_option(Option(self._row(item), id=item.connection_id))
                if item.connection_id == selected_id:
                    selected_index = index
            options.highlighted = selected_index

        def _selected(self) -> Optional[McpClientTarget]:
            options = self.query_one("#mcp-connections", OptionList)
            highlighted = options.highlighted
            if highlighted is None:
                return None
            option = options.get_option_at_index(highlighted)
            cid = getattr(option, "id", None)
            if not cid or cid == "mcp-empty":
                return None
            try:
                # Selection is configuration-only. Avoid a live runtime/PID
                # probe on the UI thread just to recover the saved target.
                return self._registry.get(cid)
            except (ConnectionError, ConnectionRuntimeError, KeyError):
                return None

        def _set_error(self, message: str, *, ok: bool = False) -> None:
            status = self.query_one("#mcp-list-error", Static)
            status.update(str(redact(message)) if message else "")
            status.set_class(not ok, "status-error")

        def action_add(self) -> None:
            self.app.push_screen(
                _PresetPickerScreen(self.language), self._preset_chosen
            )

        def _preset_chosen(self, preset_id: Optional[str]) -> None:
            if preset_id is None:
                self.query_one("#mcp-connections", OptionList).focus()
                return
            # The ClickUp preset is near-fully automatic -- the user picks it
            # and presses one button, and the bridge/tunnel/handshake start
            # without a long form.  Every other preset (incl. Custom MCP Client)
            # opens the manual form, which keeps auto defaults but offers full
            # manual override.  This split keeps the two scenarios distinct
            # without an ``if clickup`` branch in the form itself.
            if preset_id == "clickup":
                self.app.push_screen(
                    _ClickupAutoScreen(self.language),
                    self._form_closed,
                )
                return
            self.app.push_screen(
                _McpClientFormScreen(self.language, preset_id=preset_id),
                self._form_closed,
            )

        def action_edit(self) -> None:
            target = self._selected()
            if target is None:
                self._set_error(self._label("Выберите подключение.", "Select a connection."))
                return
            self.app.push_screen(
                _McpClientFormScreen(self.language, preset_id=target.preset_id, target=target),
                self._form_closed,
            )

        def _form_closed(self, _result: Optional[McpClientTarget]) -> None:
            self._schedule_refresh(force=True)
            self.query_one("#mcp-connections", OptionList).focus()

        def action_delete(self) -> None:
            target = self._selected()
            if target is None:
                self._set_error(self._label("Выберите подключение.", "Select a connection."))
                return

            def _confirm_delete(confirmed: Optional[bool]) -> None:
                if not confirmed:
                    self.query_one("#mcp-connections", OptionList).focus()
                    return
                self._do_delete(target)

            self.app.push_screen(
                _ConfirmScreen(
                    self._label("Удалить подключение?", "Delete connection?"),
                    self._label(
                        f"Удалить «{target.name}» и его секрет из keyring?",
                        f"Delete “{target.name}” and its secret from the keyring?",
                    ),
                    language=self.language,
                ),
                _confirm_delete,
            )

        def _do_delete(self, target: McpClientTarget) -> None:
            self._set_error(self._label("Удаляю…", "Deleting…"))

            def execute() -> None:
                error: Optional[str] = None
                try:
                    self._controller.remove(target.connection_id)
                except (ConnectionError, CredentialError, ConnectionRuntimeError) as exc:
                    error = str(exc)
                with contextlib.suppress(Exception):
                    self.app.call_from_thread(self._delete_done, error)

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="mcp-delete",
            )

        def _delete_done(self, error: Optional[str]) -> None:
            if not self.is_mounted:
                return
            if error:
                self._set_error(error)
                return
            self._schedule_refresh(force=True)
            self.query_one("#mcp-connections", OptionList).focus()

        def action_test(self) -> None:
            target = self._selected()
            if target is None:
                self._set_error(self._label("Выберите подключение.", "Select a connection."))
                return
            endpoint = connection_test_endpoint(target)
            if endpoint is None:
                self._set_error(_t(self.language, "need_bridge"))
                return
            self._set_error(_t(self.language, "testing"))

            def execute() -> None:
                try:
                    result = self._controller.test(
                        target.connection_id,
                        timeout_seconds=15.0,
                    )
                except (ConnectionError, CredentialError, ConnectionRuntimeError) as exc:
                    result = {
                        "state": "failed",
                        "failure_kind": "controller_error",
                        "detail": str(exc),
                    }
                self.app.call_from_thread(self._test_done, result)

            self.app.run_worker(execute, thread=True, exclusive=True, group="mcp-test")

        def _test_done(self, result: Dict[str, Any]) -> None:
            if not self.is_mounted:
                return
            state = result.get("state")
            if state == "ok":
                self._set_error(
                    self._label(
                        f"OK: {result.get('detail', '')}",
                        f"OK: {result.get('detail', '')}",
                    ),
                    ok=True,
                )
            else:
                kind = result.get("failure_kind", "error")
                detail = result.get("detail", "")
                self._set_error(self._label(f"{kind}: {detail}", f"{kind}: {detail}"))

        def action_copy_url(self) -> None:
            target = self._selected()
            if target is None:
                self._set_error(self._label("Выберите подключение.", "Select a connection."))
                return
            url = connection_test_endpoint(target)
            if not url:
                self._set_error(_t(self.language, "need_bridge"))
                return
            copy_text(url)
            self.app.notify(_t(self.language, "copied"))

        def action_copy_secret(self) -> None:
            target = self._selected()
            if target is None:
                self._set_error(self._label("Выберите подключение.", "Select a connection."))
                return
            if not target.credential_ref:
                self._set_error(_t(self.language, "no_secret"))
                return
            def execute() -> None:
                try:
                    secret = self._controller.secret(target.connection_id)
                    error: Optional[str] = None
                except (ConnectionError, CredentialError) as exc:
                    secret = ""
                    error = str(exc)
                with contextlib.suppress(Exception):
                    self.app.call_from_thread(self._copy_secret_done, secret, error)

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="mcp-copy-secret",
            )

        def _copy_secret_done(self, secret: str, error: Optional[str]) -> None:
            if not self.is_mounted:
                return
            if error:
                self._set_error(error)
                return
            copy_text(secret)
            self.app.notify(_t(self.language, "copied"))

        def action_start(self) -> None:
            target = self._selected()
            if target is None:
                self._set_error(self._label("Выберите подключение.", "Select a connection."))
                return
            self._set_error(
                self._label(
                    "Запускаю или восстанавливаю сохранённое подключение…",
                    "Starting or repairing the saved connection…",
                )
            )

            def execute() -> None:
                try:
                    launch = self._controller.repair(target.connection_id)
                except (
                    ConnectionError,
                    ConnectionLaunchError,
                    ConnectionRuntimeError,
                    CredentialError,
                ) as exc:
                    self.app.call_from_thread(self._start_failed, str(exc))
                    return
                self.app.call_from_thread(self._start_done, launch)

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="mcp-connection-start",
            )

        def _start_failed(self, message: str) -> None:
            if not self.is_mounted:
                return
            self._set_error(
                self._label("Ошибка запуска: ", "Start failed: ") + message
            )
            self._schedule_refresh(force=True)

        def _start_done(self, launch: ManagedConnectionLaunch) -> None:
            if not self.is_mounted:
                return
            if not launch.success or launch.target is None:
                self._start_failed("managed launcher returned no target")
                return
            self._set_error(
                self._label(
                    "Подключение перезапущено."
                    if launch.status == "restarted"
                    else "Подключение запущено.",
                    "Connection restarted."
                    if launch.status == "restarted"
                    else "Connection started.",
                ),
                ok=True,
            )
            self._schedule_refresh(force=True)
            raw = launch.result.raw
            outcome = (
                raw
                if isinstance(raw, ClickupSetupOutcome)
                else ClickupSetupOutcome(
                    success=True,
                    target=launch.target,
                    public_endpoint=launch.result.public_endpoint,
                    local_endpoint=launch.result.local_endpoint,
                    runtime_id=launch.result.runtime_id,
                )
            )
            def _refocus_connection_list(_result: object) -> None:
                # ``focus()`` returns the widget, but push_screen's callback
                # contract is to return nothing, so discard it here.
                self.query_one("#mcp-connections", OptionList).focus()

            self.app.push_screen(
                _ClickupResultScreen(self.language, outcome),
                _refocus_connection_list,
            )

        def action_stop(self) -> None:
            target = self._selected()
            if target is None:
                self._set_error(self._label("Выберите подключение.", "Select a connection."))
                return
            self._set_error(self._label("Останавливаю…", "Stopping…"))

            def execute() -> None:
                try:
                    before = self._controller.status(target.connection_id)
                    if before["state"] in {"configured_not_running", "stopped"}:
                        result: Dict[str, Any] = {"already_stopped": True, "state": before["state"]}
                    else:
                        result = dict(self._controller.stop(target.connection_id))
                except (ConnectionError, ConnectionRuntimeError) as exc:
                    result = {"error": str(exc)}
                with contextlib.suppress(Exception):
                    self.app.call_from_thread(self._stop_connection_done, target, result)

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="mcp-connection-stop",
            )

        def _stop_connection_done(
            self, target: McpClientTarget, result: Dict[str, Any]
        ) -> None:
            if not self.is_mounted:
                return
            if result.get("error"):
                self._set_error(str(result["error"]))
                return
            state = str(result.get("state") or "stopped")
            if result.get("already_stopped"):
                self._set_error(
                    self._label("Подключение уже остановлено.", "The connection is already stopped."),
                    ok=True,
                )
            else:
                self._set_error(
                    self._label(
                        f"Остановлено: {state}.",
                        f"Stopped: {state}.",
                    ),
                    ok=True,
                )

            # Stop has already been confirmed by the controller. Reflect that
            # state immediately instead of waiting for the next background
            # runtime/PID reconciliation worker. Under a busy full-suite or a
            # loaded desktop this refresh can arrive noticeably later, leaving
            # a successful Stop action displayed as [running]. The periodic
            # refresh below remains authoritative and will reconcile again.
            options = self.query_one("#mcp-connections", OptionList)
            prompt = (
                f"{target.name}  [{state}]  "
                f"({target.preset_id} · {target.transport} · {target.auth_scheme})"
            )
            with contextlib.suppress(Exception):
                options.replace_option_prompt(target.connection_id, prompt)
            self._schedule_refresh(force=True)

        def action_previous(self) -> None:
            options = self.query_one("#mcp-connections", OptionList)
            if options.highlighted is None:
                options.highlighted = 0
            else:
                options.highlighted = max(0, options.highlighted - 1)
            options.scroll_to_highlight()

        def action_next(self) -> None:
            options = self.query_one("#mcp-connections", OptionList)
            count = len(options.options)
            if count == 0:
                return
            if options.highlighted is None:
                options.highlighted = 0
            else:
                options.highlighted = min(count - 1, options.highlighted + 1)
            options.scroll_to_highlight()

        def action_cancel(self) -> None:
            self.dismiss(None)

        @on(Button.Pressed)
        def _pressed(self, event: Button.Pressed) -> None:
            actions: Dict[str, Callable[[], None]] = {
                "mcp-add": self.action_add,
                "mcp-edit": self.action_edit,
                "mcp-test": self.action_test,
                "mcp-copy-url": self.action_copy_url,
                "mcp-copy-secret": self.action_copy_secret,
                "mcp-start": self.action_start,
                "mcp-stop": self.action_stop,
                "mcp-close": self.action_cancel,
            }
            action = actions.get(event.button.id or "")
            if action:
                action()

    # --- MCP client preset picker ---------------------------------------

    class _PresetPickerScreen(ModalScreen[Optional[str]]):
        BINDINGS = [
            Binding("enter", "choose", "Choose", priority=True),
            Binding("up", "previous", show=False, priority=True),
            Binding("down", "next", show=False, priority=True),
            Binding("escape", "cancel", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        _PresetPickerScreen { align: center middle; background: #0e0c08 92%; }
        #preset-pick-dialog { width: 80; height: 80%; background: #1a1712;
          border: round #c6a56b; padding: 1 2; }
        #preset-pick-dialog .title { text-style: bold; color: #e5e5e5; margin-bottom: 1; }
        #preset-pick-dialog .hint { color: #8a7e6a; margin-bottom: 1; }
        #preset-pick-list { height: 1fr; border: round #4a4338; }
        #preset-pick-note { color: #c6a56b; min-height: 2; }
        """

        def __init__(self, language: str = "ru") -> None:
            super().__init__()
            self.language = language
            self._items = mcp_client_presets()

        def _label(self, ru: str, en: str) -> str:
            return _label(self.language, ru, en)

        def compose(self) -> ComposeResult:
            with Vertical(id="preset-pick-dialog"):
                yield Static(self._label("Добавить MCP-клиент", "Add MCP client"), classes="title")
                yield Static(_t(self.language, "preset_hint"), classes="hint")
                yield OptionList(id="preset-pick-list")
                yield Static("", id="preset-pick-note", markup=False)

        def on_mount(self) -> None:
            options = self.query_one("#preset-pick-list", OptionList)
            for item in self._items:
                options.add_option(
                    Option(f"{item.display_name}  ({item.transport} · {item.auth_scheme})", id=item.preset_id)
                )
            options.highlighted = 0
            self._update_note(0)
            options.focus()

        def _update_note(self, index: int) -> None:
            if not self._items or index is None or index < 0 or index >= len(self._items):
                return
            item = self._items[index]
            note = self.query_one("#preset-pick-note", Static)
            status_tag = f"[{item.status}] " if item.status != "stable" else ""
            note.update(f"{status_tag}{item.description}")

        def action_previous(self) -> None:
            options = self.query_one("#preset-pick-list", OptionList)
            if options.highlighted is None:
                options.highlighted = 0
            else:
                options.highlighted = max(0, options.highlighted - 1)
            options.scroll_to_highlight()
            self._update_note(options.highlighted)

        def action_next(self) -> None:
            options = self.query_one("#preset-pick-list", OptionList)
            count = len(options.options)
            if count == 0:
                return
            if options.highlighted is None:
                options.highlighted = 0
            else:
                options.highlighted = min(count - 1, options.highlighted + 1)
            options.scroll_to_highlight()
            self._update_note(options.highlighted)

        def action_choose(self) -> None:
            options = self.query_one("#preset-pick-list", OptionList)
            if options.highlighted is None:
                return
            option = options.get_option_at_index(options.highlighted)
            self.dismiss(getattr(option, "id", None))

        def action_cancel(self) -> None:
            self.dismiss(None)

        @on(OptionList.OptionSelected, "#preset-pick-list")
        def _selected_event(self, event: OptionList.OptionSelected) -> None:
            self.dismiss(getattr(event.option, "id", None))

    # --- MCP client form (add/edit) -------------------------------------

    class _McpClientFormScreen(ModalScreen[Optional[McpClientTarget]]):
        BINDINGS = [
            Binding("f5", "save", _C["en"]["save"], priority=True),
            Binding("f10", "save", _C["en"]["save"], priority=True),
            Binding("escape", "cancel", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        _McpClientFormScreen { align: center middle; background: #0e0c08 92%; }
        #mcp-form-dialog { width: 84; height: 88%; background: #1a1712;
          border: round #c6a56b; padding: 1 2; }
        #mcp-form-dialog .title { text-style: bold; color: #e5e5e5; }
        #mcp-form-dialog .hint { color: #8a7e6a; margin-bottom: 1; }
        #mcp-form-scroll { height: 1fr; }
        #mcp-form-scroll .field-label { color: #b3a990; margin-top: 0; }
        #mcp-form-scroll Input { margin-bottom: 1; }
        #mcp-form-auth { height: auto; border: round #4a4338; }
        #mcp-form-tunnel { height: auto; border: round #4a4338; }
        #mcp-form-stability { height: auto; border: round #4a4338; }
        #mcp-form-error { color: #e0a3a3; min-height: 1; max-height: 4;
          text-wrap: wrap; overflow-y: auto; }
        #mcp-form-error.status-success { color: #8aab7e; }
        #mcp-form-error.status-warning { color: #c6a56b; }
        #mcp-form-error.status-error { color: #e0a3a3; }
        .auth-only-bearer, .auth-only-custom, .auth-only-none, .tunnel-only-custom {
          display: none; }
        #mcp-form-buttons { height: 3; align-horizontal: right; }
        #mcp-form-buttons Button { margin-left: 1; }
        """

        def __init__(
            self,
            language: str = "ru",
            *,
            preset_id: str = "custom",
            target: Optional[McpClientTarget] = None,
        ) -> None:
            super().__init__()
            self.language = language
            self.preset_id = preset_id
            self.target = target

        def _label(self, ru: str, en: str) -> str:
            return _label(self.language, ru, en)

        def compose(self) -> ComposeResult:
            preset = mcp_client_preset(self.preset_id)
            editing = self.target is not None
            with Vertical(id="mcp-form-dialog"):
                yield Static(
                    self._label(
                        f"{self._label('Изменить', 'Edit') if editing else self._label('Добавить', 'Add')} {preset.display_name}",
                        f"{('Edit') if editing else 'Add'} {preset.display_name}",
                    ),
                    classes="title",
                )
                yield Static(
                    self._label(
                        "Tab — переход • F10 — сохранить • Esc — отмена",
                        "Tab — move • F10 — save • Esc — cancel",
                    ),
                    classes="hint",
                )
                with VerticalScroll(id="mcp-form-scroll"):
                    yield Label(self._label(_C["ru"]["name"], _C["en"]["name"]), classes="field-label")
                    yield Input(
                        value=self.target.name if self.target else preset.display_name,
                        id="mcf-name",
                    )
                    yield Label(self._label(_C["ru"]["transport"], _C["en"]["transport"]), classes="field-label")
                    transport = self.target.transport if self.target else preset.transport
                    with RadioSet(id="mcp-form-transport"):
                        yield RadioButton(
                            "Streamable HTTP (MCP)",
                            value=transport == "streamable_http",
                            id="mcf-transport-mcp",
                        )
                        yield RadioButton(
                            "OpenAPI (REST)",
                            value=(self.target or preset).transport == "openapi",
                            id="mcf-transport-openapi",
                        )
                    yield Label(self._label(_C["ru"]["auth"], _C["en"]["auth"]), classes="field-label")
                    current_auth = self.target.auth_scheme if self.target else preset.auth_scheme
                    with RadioSet(id="mcp-form-auth"):
                        for index, scheme in enumerate(CONNECTION_AUTH_SCHEMES):
                            current = current_auth
                            label = {
                                "bearer": "Bearer token",
                                "api_key": "API key (X-API-Key)",
                                "custom_header": self._label("Свой заголовок", "Custom header"),
                                "oauth": "OAuth 2.1 (web bridge flow)",
                                "none": self._label("Без авторизации (небезопасно)", "No auth (unsafe)"),
                            }.get(scheme, scheme)
                            yield RadioButton(
                                label, value=current == scheme, id=f"mcf-auth-{scheme}",
                            )
                    yield Label(self._label(_C["ru"]["header_name"], _C["en"]["header_name"]), classes="field-label auth-only-custom")
                    yield Input(value=(self.target.header_name if self.target else ""), placeholder="X-API-Key", id="mcf-header-name", classes="auth-only-custom")
                    yield Label(self._label(_C["ru"]["header_prefix"], _C["en"]["header_prefix"]), classes="field-label auth-only-custom")
                    yield Input(value=(self.target.header_prefix if self.target else ""), placeholder="Bearer ", id="mcf-header-prefix", classes="auth-only-custom")
                    yield Label(self._label(_C["ru"]["tunnel"], _C["en"]["tunnel"]), classes="field-label")
                    current_tunnel = self.target.tunnel if self.target else preset.tunnel_default
                    with RadioSet(id="mcp-form-tunnel"):
                        for tunnel in CONNECTION_TUNNELS:
                            current = current_tunnel
                            yield RadioButton(
                                tunnel, value=current == tunnel, id=f"mcf-tunnel-{tunnel}",
                            )
                    yield Label(self._label(_C["ru"]["public_url"], _C["en"]["public_url"]), classes="field-label tunnel-only-custom")
                    yield Input(value=(self.target.public_url if self.target else ""), placeholder="https://my-tunnel.example.com", id="mcf-public-url", classes="tunnel-only-custom")
                    yield Label(self._label(_C["ru"]["endpoint_path"], _C["en"]["endpoint_path"]), classes="field-label")
                    yield Input(value=self.target.endpoint_path if self.target else preset.endpoint_path, id="mcf-endpoint")
                    yield Label(self._label("Локальный порт", "Local port"), classes="field-label")
                    # 8765 is the primary ChatGPT/Claude bridge port.  Manual
                    # custom clients default to a separate local listener so a
                    # newly-created connection cannot collide with the bridge
                    # the user is already talking to from ChatGPT Web.
                    yield Input(value=str(self.target.port if self.target else 8770), id="mcf-port")
                    yield Label(self._label(_C["ru"]["url_stability"], _C["en"]["url_stability"]), classes="field-label")
                    stability = self.target.url_stability if self.target else "temporary"
                    with RadioSet(id="mcp-form-stability"):
                        yield RadioButton(
                            self._label("Временный", "Temporary"),
                            value=stability == "temporary",
                            id="mcf-stab-temp",
                        )
                        yield RadioButton(
                            self._label("Стабильный", "Stable"),
                            value=stability == "stable",
                            id="mcf-stab-stable",
                        )
                    yield Label(self._label(_C["ru"]["secret"], _C["en"]["secret"]), classes="field-label")
                    if editing:
                        # B5 section 6. The field was an active password box
                        # offering to "leave blank to generate", and Save
                        # rejected every value typed into it. A control that
                        # cannot succeed is worse than an absent one: it reads
                        # as a supported feature until the moment it refuses.
                        #
                        # Rotating a saved secret needs a restart-and-handshake
                        # transaction, because a managed bridge validates its
                        # bearer against the live keyring. That transaction does
                        # not exist in this architecture and B5 does not invent
                        # one, so the form says so plainly and offers no box.
                        yield Static(
                            self._label(
                                "Ключ сохранён. В этой форме его нельзя заменить.",
                                "The key is saved. It cannot be replaced in "
                                "this form.",
                            ),
                            id="mcf-secret-note",
                            markup=False,
                        )
                    else:
                        yield Input(
                            password=True,
                            placeholder=self._label("Вставьте секрет (или оставьте пустым, чтобы сгенерировать)", "Paste the secret (or leave blank to generate)"),
                            id="mcf-secret",
                        )
                    yield Label(self._label(_C["ru"]["description"], _C["en"]["description"]), classes="field-label")
                    yield Input(value=(self.target.description if self.target else preset.description), id="mcf-description")
                    yield Label(self._label(_C["ru"]["instructions"], _C["en"]["instructions"]), classes="field-label")
                    yield Input(value=(self.target.instructions if self.target else preset.instructions), id="mcf-instructions")
                yield Static("", id="mcp-form-error", markup=False)
                with Horizontal(id="mcp-form-buttons"):
                    yield Button(self._label("Отмена", "Cancel"), id="mcf-cancel")
                    yield Button(self._label("Сохранить", "Save"), id="mcf-save", variant="primary")

        def on_mount(self) -> None:
            self._update_visibility()
            self.query_one("#mcf-name", Input).focus()

        @on(RadioSet.Changed, "#mcp-form-auth")
        def _auth_changed(self, _event: RadioSet.Changed) -> None:
            self._update_visibility()

        @on(RadioSet.Changed, "#mcp-form-tunnel")
        def _tunnel_changed(self, _event: RadioSet.Changed) -> None:
            self._update_visibility()

        def _selected_scheme(self) -> str:
            radioset = self.query_one("#mcp-form-auth", RadioSet)
            index = radioset.pressed_index
            if 0 <= index < len(CONNECTION_AUTH_SCHEMES):
                return CONNECTION_AUTH_SCHEMES[index]
            return "bearer"

        def _selected_tunnel(self) -> str:
            radioset = self.query_one("#mcp-form-tunnel", RadioSet)
            index = radioset.pressed_index
            if 0 <= index < len(CONNECTION_TUNNELS):
                return CONNECTION_TUNNELS[index]
            return "cloudflare"

        def _selected_transport(self) -> str:
            radioset = self.query_one("#mcp-form-transport", RadioSet)
            if radioset.pressed_index == 1:
                return "openapi"
            return "streamable_http"

        def _selected_stability(self) -> str:
            radioset = self.query_one("#mcp-form-stability", RadioSet)
            return "stable" if radioset.pressed_index == 1 else "temporary"

        def _update_visibility(self) -> None:
            scheme = self._selected_scheme()
            tunnel = self._selected_tunnel()
            # The CSS classes are named for the condition but hide the widget.
            # Toggling the class with the positive condition inverted the UI:
            # custom fields disappeared when selected and appeared otherwise.
            for widget in self.query(".auth-only-custom"):
                widget.styles.display = "block" if scheme == "custom_header" else "none"
            for widget in self.query(".tunnel-only-custom"):
                widget.styles.display = "block" if tunnel == "custom" else "none"

        def _set_status(self, message: str, kind: str = "error") -> None:
            status = self.query_one("#mcp-form-error", Static)
            status.update(message)
            status.set_class(kind == "success", "status-success")
            status.set_class(kind == "warning", "status-warning")
            status.set_class(kind == "error", "status-error")

        def action_cancel(self) -> None:
            self.dismiss(None)

        @on(Button.Pressed)
        def _pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "mcf-save":
                self.action_save()
            elif event.button.id == "mcf-cancel":
                self.action_cancel()

        def action_save(self) -> None:
            name = self.query_one("#mcf-name", Input).value.strip()
            if not name:
                self._set_status(self._label("Укажите имя подключения.", "Enter a connection name."))
                return
            scheme = self._selected_scheme()
            transport = self._selected_transport()
            tunnel = self._selected_tunnel()
            stability = self._selected_stability()
            endpoint = self.query_one("#mcf-endpoint", Input).value.strip()
            if not endpoint:
                endpoint = "/mcp" if transport == "streamable_http" else "/openapi.json"
            try:
                port = int(self.query_one("#mcf-port", Input).value.strip() or "8765")
            except ValueError:
                self._set_status(self._label("Порт должен быть числом.", "Port must be a number."))
                return
            public_url = self.query_one("#mcf-public-url", Input).value.strip() or None
            header_name = self.query_one("#mcf-header-name", Input).value.strip() if scheme == "custom_header" else ""
            header_prefix = self.query_one("#mcf-header-prefix", Input).value.strip() if scheme == "custom_header" else ""
            description = self.query_one("#mcf-description", Input).value.strip()
            instructions = self.query_one("#mcf-instructions", Input).value.strip()
            # Absent by construction when editing: the field is a note, not an
            # input, so there is no plaintext secret to read here at all.
            try:
                secret_value = self.query_one("#mcf-secret", Input).value
            except Exception:
                secret_value = ""

            from time import time as _now
            from .connections import _new_connection_id

            # Credential identity follows the immutable connection ID, never the
            # editable display name.  Two similarly named connections therefore
            # cannot overwrite each other's keyring entry.
            connection_id = self.target.connection_id if self.target else _new_connection_id()
            created_at = self.target.created_at if self.target else _now()

            # A manual Streamable HTTP + Bearer connection is not just metadata:
            # KaroX must later be able to launch a repository-scoped bridge for
            # it.  Bind those cards to a durable SessionStore record and the
            # bridge keyring namespace now, while this screen still knows which
            # repository is selected.  Other auth/wire combinations remain plain
            # connection metadata and keep using KaroX/connection.
            managed_candidate = bool(
                self.preset_id in {"clickup", "generic-mcp", "custom", "web-agent", "ide"}
                and transport == "streamable_http"
                and scheme == "bearer"
                and tunnel in {"cloudflare", "tailscale", "local"}
            )
            pending_managed_ref: Optional[str] = None
            legacy_connection_ref: Optional[str] = None

            # ``none`` is only ever saved when the user explicitly chose it; the
            # radio set is the opt-in, and the field is empty by design.
            if scheme == "none":
                self._set_status(_t(self.language, "no_auth_warning"), "warning")
                credential_ref = None
                credential_fingerprint = None
            else:
                credential_ref = None
                credential_fingerprint = None
                store = ConnectionCredentialStore()
                if self.target is not None:
                    if secret_value:
                        # A managed bridge may validate a KaroX/bridge credential.
                        # Replacing it from this generic metadata form would make
                        # the saved card and the live process disagree.  Rotation
                        # needs a restart+handshake transaction, so fail safely.
                        self._set_status(
                            self._label(
                                "Секрет существующего подключения нельзя менять в этой форме. Пересоздайте подключение или используйте управляемую ротацию.",
                                "An existing connection secret cannot be changed in this form. Recreate it or use managed rotation.",
                            )
                        )
                        return
                    if not self.target.credential_ref:
                        self._set_status(self._label("У подключения отсутствует ссылка на секрет.", "The connection has no credential reference."))
                        return
                    credential_ref = self.target.credential_ref
                    credential_fingerprint = self.target.credential_fingerprint
                    # Records created by older KaroX builds have only a
                    # KaroX/connection secret.  Migrate them transactionally the
                    # first time they are saved as a managed bearer bridge.
                    if managed_candidate and not credential_ref.startswith("os-keyring:bridge/"):
                        try:
                            old_secret = resolve_connection_secret(self.target)
                            repo = Path(getattr(self.app, "repository", Path.cwd())).expanduser().resolve(strict=True)
                            info = prepare_managed_mcp_binding(
                                connection_id,
                                repo,
                                secret=old_secret,
                                bypass=self.target.bypass,
                                display_name=name,
                            )
                            pending_managed_ref = info["reference"]
                            legacy_connection_ref = credential_ref
                            credential_ref = info["reference"]
                            credential_fingerprint = info["fingerprint"]
                        except Exception as exc:
                            self._set_status(str(exc))
                            return
                elif managed_candidate:
                    try:
                        repo = Path(getattr(self.app, "repository", Path.cwd())).expanduser().resolve(strict=True)
                        info = prepare_managed_mcp_binding(
                            connection_id,
                            repo,
                            secret=secret_value or None,
                            bypass=False,
                            display_name=name,
                        )
                        pending_managed_ref = info["reference"]
                        credential_ref = info["reference"]
                        credential_fingerprint = info["fingerprint"]
                    except Exception as exc:
                        self._set_status(str(exc))
                        return
                else:
                    try:
                        info = store.set(connection_id, secret_value or None)
                        credential_ref = info["reference"]
                        credential_fingerprint = info["fingerprint"]
                    except (CredentialError, ConnectionConfigurationError) as exc:
                        self._set_status(str(exc))
                        return
            # B5.2. The editable half of the record, in one place.
            edited: Dict[str, Any] = {
                "name": name,
                "transport": transport,
                "endpoint_path": endpoint,
                "auth_scheme": scheme,
                "tunnel": tunnel,
                "url_stability": stability,
                "public_url": public_url,
                "header_name": header_name,
                "header_prefix": header_prefix,
                "description": description,
                "instructions": instructions,
                "credential_ref": credential_ref,
                "credential_fingerprint": credential_fingerprint,
                "port": port,
                "updated_at": _now(),
            }
            try:
                if self.target is not None:
                    # B5.2. Edit updates the stored record instead of building a
                    # replacement from the form.
                    #
                    # The defect this closes: the hand-built target passed no
                    # `enabled`, so the dataclass default applied and editing a
                    # *disabled* connection quietly put it back into service --
                    # the one state change disable exists to make durable, undone
                    # by renaming the connection. `replace` cannot lose a field
                    # it was not asked about, which is the property wanted here:
                    # every future field is preserved by default rather than
                    # dropped until somebody remembers to add it.
                    #
                    # `preset_id`, `runtime_profile` and `created_at` ride along
                    # for the same reason. Re-deriving the runtime profile from
                    # the preset would rewrite a record whose profile was changed
                    # deliberately.
                    target = _dc_replace(self.target, **edited)
                else:
                    target = McpClientTarget(
                        connection_id=connection_id,
                        preset_id=self.preset_id,
                        runtime_profile=mcp_client_preset(
                            self.preset_id
                        ).runtime_profile,
                        created_at=created_at,
                        **edited,
                    )
            except (ConnectionConfigurationError, ValueError) as exc:
                if pending_managed_ref:
                    rollback_managed_mcp_binding(pending_managed_ref)
                self._set_status(str(exc))
                return

            try:
                connection_registry().put(target)
            except ConnectionError as exc:
                # A new secret/session is prepared before the atomic registry
                # update. Roll it back if the metadata did not commit, otherwise
                # a failed save would leave a live-looking orphan in keyring and
                # SessionStore.
                if pending_managed_ref:
                    rollback_managed_mcp_binding(pending_managed_ref)
                elif self.target is None and credential_ref and credential_ref.startswith("os-keyring:connection/"):
                    try:
                        ConnectionCredentialStore().delete(connection_id)
                    except CredentialError:
                        pass
                self._set_status(str(exc))
                return

            # Migration is committed: only now remove the old generic secret.
            # Until this point the old card remained fully usable if anything
            # failed while preparing the managed bridge binding.
            if legacy_connection_ref and legacy_connection_ref.startswith("os-keyring:connection/"):
                try:
                    ConnectionCredentialStore().delete(
                        legacy_connection_ref.removeprefix("os-keyring:connection/")
                    )
                except CredentialError:
                    pass
            self.app.notify(_t(self.language, "saved"))
            self.dismiss(target)

    # --- ClickUp automatic setup (compact, single-button) ------------------

    class _ClickupAutoScreen(ModalScreen[Optional[McpClientTarget]]):
        """Compact near-automatic ClickUp setup: name + one button + progress.

        The long manual form is the wrong UX for a ready preset.  This screen
        resolves the effective config from the current environment (transport,
        auth, tunnel, port, endpoint, URL stability, generated secret) without
        asking the user, then launches the bridge + tunnel + handshake on a
        background worker (non-blocking) and shows sequential progress.  An
        Advanced toggle opens a scrollable override panel; everything defaults
        to the auto-resolved values and ``Reset to automatic`` clears the
        overrides.  Incompatible combinations are blocked before the worker
        runs.  On success the result card is shown; on cancel (Esc during
        setup) the worker is told to stop and any started process/secret is
        cleaned up by the orchestrator.
        """

        BINDINGS = [
            # Enter belongs to the focused Input/Button. The previous priority
            # binding launched Connect even when Tab had moved to Advanced,
            # Reset, or Cancel. Keep only the unambiguous modal escape shortcut.
            Binding("escape", "cancel", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        _ClickupAutoScreen { align: center middle; background: #0e0c08 92%; }
        #cu-auto-dialog { width: 76; height: auto; max-height: 88%;
          background: #1a1712; border: round #c6a56b; padding: 1 2; }
        #cu-auto-dialog .title { text-style: bold; color: #e5e5e5; margin-bottom: 1; }
        #cu-auto-dialog .field-label { color: #b3a990; margin-top: 0; }
        #cu-auto-dialog .mode-row { height: 1; margin-bottom: 1; color: #c6a56b; }
        #cu-auto-dialog .status-title { color: #8a7e6a; margin-top: 1; }
        #cu-auto-progress { color: #dcdcdc; margin-top: 1; height: auto; }
        #cu-auto-error { color: #e0a3a3; margin-top: 1; text-wrap: wrap; }
        #cu-auto-error.status-success { color: #8aab7e; }
        #cu-auto-advanced { height: auto; max-height: 60; display: none;
          border: round #4a4338; margin-top: 1; padding: 0 1; }
        #cu-auto-buttons { height: 3; align-horizontal: right; margin-top: 1; }
        #cu-auto-buttons Button { margin-left: 1; }
        #cu-auto-advanced Input { margin-bottom: 1; }
        #cu-auto-advanced .field-label { color: #b3a990; margin-top: 0; }
        #cu-auto-advanced RadioSet { height: auto; border: round #4a4338; margin-bottom: 1; }
        """

        def __init__(self, language: str = "ru") -> None:
            super().__init__()
            self.language = language
            self._setup_running = False
            self._setup_cancel = False
            self._overrides: dict[str, str] = {}
            self._base_defaults: Optional[Any] = None
            self._outcome: Optional[ClickupSetupOutcome] = None

        def _label(self, ru: str, en: str) -> str:
            return _label(self.language, ru, en)

        def compose(self) -> ComposeResult:
            preset = mcp_client_preset("clickup")
            with Vertical(id="cu-auto-dialog"):
                yield Static(self._label("Добавить ClickUp", "Add ClickUp"), classes="title")
                yield Label(self._label("Имя подключения:", "Connection name:"), classes="field-label")
                yield Input(value=preset.display_name, id="cu-auto-name")
                yield Static(self._label("Режим: Автоматическая настройка", "Mode: Automatic setup"), classes="mode-row")
                yield Static(self._label("Статус:", "Status:"), classes="status-title")
                yield Static("", id="cu-auto-progress", markup=False)
                yield Static("", id="cu-auto-error", markup=False)
                with VerticalScroll(id="cu-auto-advanced"):
                    yield Label(self._label("Transport", "Transport"), classes="field-label")
                    with RadioSet(id="cu-auto-transport"):
                        yield RadioButton("Streamable HTTP (MCP)", value=True, id="cu-auto-t-mcp")
                    yield Static(
                        self._label(
                            "ClickUp-профиль KaroX сейчас обслуживает только Streamable HTTP MCP.",
                            "The KaroX ClickUp profile currently serves Streamable HTTP MCP only.",
                        ),
                        classes="mode-row",
                    )
                    yield Label(self._label("Авторизация", "Authentication"), classes="field-label")
                    with RadioSet(id="cu-auto-auth"):
                        yield RadioButton(
                            self._label("Authorization header (Bearer)", "Authorization header (Bearer)"),
                            value=True,
                            id="cu-auto-a-bearer",
                        )
                    yield Static(
                        self._label(
                            "В форме ClickUp нужно выбрать «Authorization header», не OAuth.",
                            "Choose “Authorization header” in ClickUp, not OAuth.",
                        ),
                        classes="mode-row",
                    )
                    yield Label(self._label("Tunnel", "Tunnel"), classes="field-label")
                    with RadioSet(id="cu-auto-tunnel"):
                        yield RadioButton("cloudflare", value=True, id="cu-auto-tun-cf")
                        yield RadioButton("tailscale", value=False, id="cu-auto-tun-ts")
                        yield RadioButton("local", value=False, id="cu-auto-tun-local")
                    yield Label(self._label("Стабильность URL", "URL stability"), classes="field-label")
                    with RadioSet(id="cu-auto-stab"):
                        yield RadioButton(self._label("Временный", "Temporary"), value=True, id="cu-auto-s-temp")
                        yield RadioButton(self._label("Стабильный", "Stable"), value=False, id="cu-auto-s-stable")
                    yield Label(self._label("Endpoint path", "Endpoint path"), classes="field-label")
                    yield Input(value=preset.endpoint_path, id="cu-auto-endpoint")
                    yield Label(self._label("Локальный порт (пусто — авто)", "Local port (blank = auto)"), classes="field-label")
                    yield Input(value="", id="cu-auto-port")
                    yield Label(self._label("Секрет (пусто — сгенерировать)", "Secret (blank = generate)"), classes="field-label")
                    yield Input(value="", password=True, id="cu-auto-secret")
                with Horizontal(id="cu-auto-buttons"):
                    yield Button(self._label("Отмена", "Cancel"), id="cu-auto-cancel")
                    yield Button(self._label("Доп. настройки", "Advanced"), id="cu-auto-advanced-btn")
                    yield Button(self._label("Сбросить", "Reset"), id="cu-auto-reset-btn")
                    yield Button(self._label("Подключить автоматически", "Connect automatically"), id="cu-auto-connect", variant="primary")

        def on_mount(self) -> None:
            # Resolve the environment before the user opens Advanced.  The form
            # must display the actual automatic choice; otherwise its hardcoded
            # Cloudflare radio is mistaken for a user override and silently
            # replaces an available stable Tailscale route.
            try:
                self._base_defaults = resolve_clickup_defaults(self._probe_environment())
                self._apply_defaults_to_form(self._base_defaults)
            except Exception as exc:
                self._base_defaults = None
                self._set_error(self._label("Не удалось определить окружение: ", "Environment probe failed: ") + str(exc))
            self.query_one("#cu-auto-name", Input).focus()

        def _apply_defaults_to_form(self, defaults: Any) -> None:
            tunnel_ids = {
                "cloudflare": "#cu-auto-tun-cf",
                "tailscale": "#cu-auto-tun-ts",
                "local": "#cu-auto-tun-local",
            }
            for tunnel, selector in tunnel_ids.items():
                self.query_one(selector, RadioButton).value = defaults.tunnel == tunnel
            self.query_one("#cu-auto-s-temp", RadioButton).value = defaults.url_stability != "stable"
            self.query_one("#cu-auto-s-stable", RadioButton).value = defaults.url_stability == "stable"
            self.query_one("#cu-auto-endpoint", Input).value = defaults.endpoint_path
            # Blank means the resolved free port.  A number here is therefore an
            # explicit user override rather than a duplicate of automatic state.
            self.query_one("#cu-auto-port", Input).value = ""

        def _set_error(self, message: str, *, kind: str = "error") -> None:
            status = self.query_one("#cu-auto-error", Static)
            status.update(message)
            status.set_class(kind == "success", "status-success")

        def _set_progress(self, message: str) -> None:
            self.query_one("#cu-auto-progress", Static).update(message)

        def action_toggle_advanced(self) -> None:
            panel = self.query_one("#cu-auto-advanced", VerticalScroll)
            visible = panel.styles.display != "none"
            panel.styles.display = "none" if visible else "block"

        def action_reset_overrides(self) -> None:
            self._overrides = {}
            try:
                self._base_defaults = resolve_clickup_defaults(self._probe_environment())
                self._apply_defaults_to_form(self._base_defaults)
            except Exception as exc:
                self._set_error(self._label("Не удалось обновить автонастройки: ", "Could not refresh automatic settings: ") + str(exc))
                return
            self.query_one("#cu-auto-secret", Input).value = ""
            self._set_error(self._label("Сброшено на автоматические.", "Reset to automatic."), kind="success")

        def _gather_overrides(self, base: Any) -> tuple[dict[str, str], Optional[str]]:
            """Return only fields that differ from the resolved automatic base.

            The secret is returned separately and is never placed in the
            serializable override map.
            """
            tunnel = self.query_one("#cu-auto-tunnel", RadioSet)
            stab = self.query_one("#cu-auto-stab", RadioSet)
            overrides: dict[str, str] = {}

            tunnel_values = ("cloudflare", "tailscale", "local")
            tun_idx = tunnel.pressed_index
            selected_tunnel = (
                tunnel_values[tun_idx]
                if 0 <= tun_idx < len(tunnel_values)
                else base.tunnel
            )
            if selected_tunnel != base.tunnel:
                overrides["tunnel"] = selected_tunnel

            selected_stability = "stable" if stab.pressed_index == 1 else "temporary"
            if selected_stability != base.url_stability:
                overrides["url_stability"] = selected_stability

            endpoint = self.query_one("#cu-auto-endpoint", Input).value.strip()
            if endpoint and endpoint != base.endpoint_path:
                overrides["endpoint_path"] = endpoint
            port = self.query_one("#cu-auto-port", Input).value.strip()
            if port:
                overrides["port"] = port
            secret = self.query_one("#cu-auto-secret", Input).value or None
            return overrides, secret

        def action_connect(self) -> None:
            if self._setup_running:
                return
            name = self.query_one("#cu-auto-name", Input).value.strip()
            if not name:
                self._set_error(self._label("Укажите имя подключения.", "Enter a connection name."))
                return
            try:
                base_defaults = self._base_defaults or resolve_clickup_defaults(
                    self._probe_environment()
                )
                self._base_defaults = base_defaults
                overrides, supplied_secret = self._gather_overrides(base_defaults)
                defaults = (
                    apply_clickup_overrides(base_defaults, overrides)
                    if overrides
                    else base_defaults
                )
                if supplied_secret is not None:
                    from dataclasses import replace

                    defaults = replace(defaults, secret_source="paste")
                    # Do not retain plaintext in the mounted password input while
                    # the background worker and result card are active.
                    self.query_one("#cu-auto-secret", Input).value = ""
            except ConnectionConfigurationError as exc:
                self._set_error(self._label("Несовместимая комбинация: ", "Incompatible: ") + str(exc))
                return
            self._setup_running = True
            self._setup_cancel = False
            self._set_error("", kind="success")
            self._set_progress(self._label("Настройка…", "Setting up…"))
            self.query_one("#cu-auto-connect", Button).disabled = True

            def _worker() -> None:
                repo = Path(getattr(self.app, "repository", Path.cwd())).expanduser().resolve()
                progress_lines: list[str] = []

                def on_progress(step_id: str, state: str, detail: Any) -> None:
                    step_label = {
                        "config": self._label("Конфигурация определена", "Configuration resolved"),
                        "secret": self._label("Секрет создан", "Secret created"),
                        "server": self._label("MCP server запущен", "MCP server started"),
                        "tunnel": self._label("Tunnel запущен", "Tunnel started"),
                        "url": self._label("Public URL получен", "Public URL obtained"),
                        "handshake": self._label("MCP handshake пройдён", "MCP handshake passed"),
                    }.get(step_id, step_id)
                    marker = "✓" if state == "ok" else ("✗" if state == "failed" else "…")
                    progress_lines.append(f"{marker} {step_label}")
                    text = "\n".join(progress_lines)
                    self.app.call_from_thread(self._set_progress, text)

                def cancellation() -> bool:
                    return self._setup_cancel

                outcome = setup_clickup_connection(
                    defaults,
                    name=name,
                    repository=repo,
                    supplied_secret=supplied_secret,
                    on_progress=on_progress,
                    cancellation=cancellation,
                )
                self.app.call_from_thread(self._done, outcome)

            self.app.run_worker(_worker, thread=True, exclusive=True, group="clickup-setup")

        def _probe_environment(self) -> dict[str, Any]:
            """Snapshot tunnel availability for the defaults resolver.

            Kept separate from the worker so the resolver runs on the UI thread
            with a frozen snapshot (the orchestrator stays pure).  Missing tools
            (tailscale/cloudflared absent in headless tests) degrade gracefully
            to the local fallback rather than raising.
            """
            try:
                from .tailscale import query_tailscale_status
                ts = query_tailscale_status()
                tailscale_ready = ts.get("ready", False)
                funnel_available = False
                if tailscale_ready and ts.get("dns_name"):
                    try:
                        from .tui import _tailscale_funnel_available
                        funnel_available = _tailscale_funnel_available(ts)
                    except Exception:
                        funnel_available = True
            except Exception:
                tailscale_ready = False
                funnel_available = False
            try:
                from .web_bridge_launcher import find_cloudflared
                cloudflared_installed = find_cloudflared() is not None
            except Exception:
                cloudflared_installed = False
            try:
                from .web_bridge_launcher import _port_is_available
                port_probe = _port_is_available
            except Exception:
                port_probe = None
            return {
                "tailscale_ready": tailscale_ready,
                "tailscale_funnel_available": funnel_available,
                "cloudflared_installed": cloudflared_installed,
                "port_probe": port_probe,
            }

        def _done(self, outcome: ClickupSetupOutcome) -> None:
            self._setup_running = False
            self.query_one("#cu-auto-connect", Button).disabled = False
            if outcome.success and outcome.target is not None:
                # Show the result card, then dismiss with the saved target when
                # the card closes so the MCP clients list refreshes.
                self.app.push_screen(
                    _ClickupResultScreen(self.language, outcome),
                    lambda _r: self.dismiss(outcome.target),
                )
            else:
                kind = outcome.failure_kind or "failed"
                self._set_error(self._label("Ошибка: ", "Failed: ") + kind + " — " + (outcome.failure_detail or ""))
                self._set_progress("")

        def action_cancel(self) -> None:
            if self._setup_running:
                self._setup_cancel = True
                self._set_error(self._label("Отмена…", "Cancelling…"))
                return
            self.dismiss(None)

        @on(Button.Pressed)
        def _pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cu-auto-connect":
                self.action_connect()
            elif event.button.id == "cu-auto-cancel":
                self.action_cancel()
            elif event.button.id == "cu-auto-advanced-btn":
                self.action_toggle_advanced()
            elif event.button.id == "cu-auto-reset-btn":
                self.action_reset_overrides()

    class _ClickupResultScreen(ModalScreen[Optional[bool]]):
        """The ready-to-use ClickUp connection card shown after a successful setup.

        Shows the resolved URL, the masked secret, the actual auth mode, the
        status, and the actions: copy URL/secret/instructions, re-test, stop,
        change settings.  The full secret is never displayed; it is only
        available through the explicit ``Copy secret`` action.
        """

        BINDINGS = [
            Binding("u", "copy_url", _C["en"]["copy_url"], priority=True),
            Binding("s", "copy_secret", _C["en"]["copy_secret"], priority=True),
            Binding("i", "copy_instructions", _C["en"]["cu_copy_instruction"], priority=True),
            Binding("t", "retest", _C["en"]["cu_retest"], priority=True),
            Binding("x", "stop", "Stop", priority=True),
            Binding("escape", "close", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        _ClickupResultScreen { align: center middle; background: #0e0c08 92%; }
        #cu-result-dialog { width: 78; height: auto; max-height: 88%;
          background: #1a1712; border: round #c6a56b; padding: 1 2; }
        #cu-result-dialog .title { text-style: bold; color: #8aab7e; margin-bottom: 1; }
        #cu-result-dialog .field-label { color: #b3a990; margin-top: 0; }
        #cu-result-dialog .value { color: #dcdcdc; margin-bottom: 1; }
        #cu-result-dialog .status-dot { color: #8aab7e; margin-bottom: 0; }
        #cu-result-actions { height: auto; align-horizontal: left; margin-top: 1; }
        #cu-result-actions Button { margin-right: 1; margin-bottom: 1; }
        #cu-result-error { color: #e0a3a3; text-wrap: wrap; }
        """

        def __init__(self, language: str, outcome: ClickupSetupOutcome) -> None:
            super().__init__()
            self.language = language
            self._outcome = outcome
            self._target = outcome.target
            self._public_endpoint = outcome.public_endpoint

        def _label(self, ru: str, en: str) -> str:
            return _label(self.language, ru, en)

        def compose(self) -> ComposeResult:
            from .connections import mask_secret, resolve_connection_secret

            target = self._target
            assert target is not None
            # Resolve the secret only to mask it here; never display the value.
            try:
                secret_value = resolve_connection_secret(target)
            except Exception:
                secret_value = ""
            masked = mask_secret(secret_value)
            # Build the auth display line from the actual resolved mode.
            auth_mode = {
                "bearer": "Bearer token",
                "api_key": "API key (X-API-Key)",
                "custom_header": f"{target.header_name or 'Custom header'}",
            }.get(target.auth_scheme, target.auth_scheme)
            header_line = ""
            if target.auth_scheme == "bearer":
                header_line = "Authorization: Bearer <secret>"
            elif target.auth_scheme == "api_key":
                header_line = "X-API-Key: <secret>"
            elif target.auth_scheme == "custom_header":
                prefix = f"{target.header_prefix} " if target.header_prefix else ""
                header_line = f"{target.header_name or 'X-API-Key'}: {prefix}<secret>"
            with Vertical(id="cu-result-dialog"):
                yield Static(self._label("ClickUp готов к подключению", "ClickUp is ready to connect"), classes="title")
                yield Label(self._label("Name:", "Name:"), classes="field-label")
                yield Static(target.name, classes="value")
                yield Label(self._label("URL:", "URL:"), classes="field-label")
                yield Static(self._public_endpoint or "", classes="value")
                yield Label(self._label("Авторизация:", "Authentication:"), classes="field-label")
                yield Static(auth_mode, classes="value")
                if header_line:
                    yield Label(self._label("Заголовок:", "Header:"), classes="field-label")
                    yield Static(header_line, classes="value")
                yield Label(self._label("Секрет:", "Secret:"), classes="field-label")
                yield Static(masked, classes="value")
                # Name the ClickUp dropdown explicitly.  ClickUp's "Connect an
                # MCP Server" form defaults its ``Authentication Method`` to
                # OAuth, and this bridge serves no OAuth metadata on the bearer
                # profile -- so leaving the default in place ends in ClickUp's
                # own "Authentication method not supported by this MCP Server".
                # The header line above was accurate but described a *header*,
                # while the form asks for a *method*, so nothing here told the
                # user which item to choose.
                yield Label(
                    self._label("В ClickUp выберите:", "Choose in ClickUp:"),
                    classes="field-label",
                )
                yield Static(self._auth_method_hint(target.auth_scheme), classes="value")
                if target.url_stability == "temporary":
                    # A Cloudflare Quick Tunnel name is reissued on every start,
                    # so a connection saved in ClickUp today fails tomorrow with
                    # nothing on either side explaining why.  The CLI path warns
                    # about this; the auto-setup card did not.
                    yield Static(
                        self._label(
                            "Внимание: адрес временный (Cloudflare Quick Tunnel). "
                            "После перезапуска моста он изменится, и подключение "
                            "в ClickUp придётся обновить. Для постоянного адреса "
                            "используйте туннель tailscale или свой домен.",
                            "Note: this URL is temporary (Cloudflare Quick Tunnel). "
                            "It changes when the bridge restarts, and the ClickUp "
                            "connection will then need the new URL. For a lasting "
                            "one, use the tailscale tunnel or your own domain.",
                        ),
                        classes="value",
                    )
                yield Label(self._label("Статус:", "Status:"), classes="field-label")
                yield Static(f"● {self._label('MCP online', 'MCP online')}", classes="status-dot")
                if (self._outcome.handshake or {}).get("state") == "public_pending":
                    yield Static(
                        self._label(
                            "◐ Локальная проверка пройдена; публичный URL ещё не подтверждён",
                            "◐ Local verification passed; public URL is not confirmed yet",
                        ),
                        classes="value",
                    )
                else:
                    yield Static(f"● {self._label('Tunnel online', 'Tunnel online')}", classes="status-dot")
                    yield Static(f"● {self._label('Handshake passed', 'Handshake passed')}", classes="status-dot")
                yield Label(self._label("Действия:", "Actions:"), classes="field-label")
                with HorizontalScroll(id="cu-result-actions"):
                    yield Button(self._label("Копировать URL", "Copy URL"), id="cu-r-copy-url")
                    yield Button(self._label("Копировать секрет", "Copy secret"), id="cu-r-copy-secret")
                    yield Button(self._label("Копировать инструкцию", "Copy instructions"), id="cu-r-copy-instr")
                    yield Button(self._label("Проверить снова", "Re-test"), id="cu-r-retest")
                    yield Button(self._label("Остановить", "Stop"), id="cu-r-stop")
                    yield Button(self._label("Готово", "Done"), id="cu-r-done", variant="primary")
                yield Static("", id="cu-result-error", markup=False)

        def _auth_method_hint(self, auth_scheme: str) -> str:
            """Which item to pick in ClickUp's ``Authentication Method`` dropdown.

            ClickUp preselects OAuth and validates the choice against the server
            before saving, so the default fails on a bearer bridge with
            "Authentication method not supported by this MCP Server".  The card
            has to name the alternative, because the wire-level header shown
            above is not what the form is asking for.

            The dropdown offers exactly three items -- OAuth, "Authorization
            header", and "No Authentication" -- so the hint names the one that
            works instead of listing candidate spellings for the user to guess
            among.  "Authorization header" asks for the token alone; the bridge
            accepts it with or without the ``Bearer `` prefix.
            """
            if auth_scheme == "bearer":
                return self._label(
                    "Authentication Method — «Authorization header», затем "
                    "вставьте секрет (кнопка «Копировать секрет»). Не OAuth: "
                    "OAuth этот мост не отдаёт.",
                    "Authentication Method — \"Authorization header\", then paste "
                    "the secret (the \"Copy secret\" button). Not OAuth: this "
                    "bridge does not serve OAuth.",
                )
            if auth_scheme == "api_key":
                return self._label(
                    "Authentication Method — API key, имя заголовка X-API-Key.",
                    "Authentication Method — API key, header name X-API-Key.",
                )
            if auth_scheme == "custom_header":
                return self._label(
                    "Authentication Method — custom header (см. заголовок выше).",
                    "Authentication Method — custom header (see the header above).",
                )
            return auth_scheme

        def action_copy_url(self) -> None:
            copy_text(self._public_endpoint or "")
            self.app.notify(_t(self.language, "copied"))

        def action_copy_secret(self) -> None:
            from .connections import resolve_connection_secret

            target = self._target
            assert target is not None
            try:
                secret = resolve_connection_secret(target)
            except Exception as exc:
                self._set_error(str(exc))
                return
            copy_text(secret)
            self.app.notify(_t(self.language, "copied"))

        def action_copy_instructions(self) -> None:
            target = self._target
            assert target is not None
            copy_text(target.instructions)
            self.app.notify(_t(self.language, "copied"))

        def action_retest(self) -> None:
            from .connections import resolve_connection_secret
            from .clickup_setup import run_resilient_handshake

            target = self._target
            assert target is not None
            self._set_error(self._label("Проверяю…", "Testing…"))
            endpoint = self._public_endpoint or ""
            # The setup handshake falls back to loopback when the public URL is
            # not resolvable from this host yet (a fresh Cloudflare quick-tunnel
            # name takes a moment to reach public DNS).  Re-test has to use the
            # same fallback, or the button reports a failure for a connection
            # the setup screen just proved sound.
            local_endpoint = self._outcome.local_endpoint

            def _worker() -> None:
                try:
                    secret = resolve_connection_secret(target)
                    result = dict(
                        run_resilient_handshake(
                            target,
                            public_endpoint=endpoint,
                            local_endpoint=local_endpoint,
                            secret=secret,
                        )
                    )
                except Exception as exc:
                    result = {"state": "failed", "failure_kind": "unexpected", "detail": str(exc)}
                self.app.call_from_thread(self._retest_done, result)

            self.app.run_worker(_worker, thread=True, exclusive=True, group="clickup-retest")

        def _retest_done(self, result: dict) -> None:
            if result.get("state") == "ok":
                self._set_error(self._label("Публичная проверка пройдена.", "Public re-test passed."), kind="success")
            elif result.get("state") == "public_pending":
                self._set_error(
                    self._label(
                        "Локальный MCP работает, но публичный URL ещё не подтверждён: ",
                        "Local MCP works, but the public URL is still unconfirmed: ",
                    )
                    + str(result.get("detail", ""))
                )
            else:
                self._set_error(self._label("Ошибка: ", "Failed: ") + str(result.get("detail", "")))

        def action_stop(self) -> None:
            """Shut the live bridge and tunnel down, keeping the saved card.

            Without this the auto-setup had no off switch: the bridge child is
            started in its own process group and outlives the TUI, so every run
            left a listener holding its port.  The next run's readiness probe then
            connected to that survivor and authenticated against its unrelated
            secret, which surfaced as an unexplained 401.  Stopping deliberately
            is what keeps that from accumulating.  The credential is preserved, so
            the saved connection can be restarted later.
            """
            stop = self._outcome.stop
            if stop is None:
                self._set_error(
                    self._label(
                        "Этот мост уже не под управлением этого окна.",
                        "This bridge is no longer managed by this window.",
                    )
                )
                return
            try:
                stop()
            except Exception as exc:
                self._set_error(str(exc))
                return
            self._set_error(
                self._label(
                    "Мост и туннель остановлены. Подключение сохранено.",
                    "Bridge and tunnel stopped. The connection stays saved.",
                ),
                kind="success",
            )

        def action_close(self) -> None:
            self.dismiss(True)

        def _set_error(self, message: str, *, kind: str = "error") -> None:
            status = self.query_one("#cu-result-error", Static)
            status.update(message)
            status.set_class(kind == "success", "status-success")

        @on(Button.Pressed)
        def _pressed(self, event: Button.Pressed) -> None:
            mapping = {
                "cu-r-copy-url": self.action_copy_url,
                "cu-r-copy-secret": self.action_copy_secret,
                "cu-r-copy-instr": self.action_copy_instructions,
                "cu-r-retest": self.action_retest,
                "cu-r-stop": self.action_stop,
                "cu-r-done": self.action_close,
            }
            action = mapping.get(event.button.id or "")
            if action:
                action()

    # --- Model provider details/edit -----------------------------------

    class _ProviderDetailsScreen(ModalScreen[Optional[str]]):
        """Secret-free provider details and direct multi-model selection."""

        BINDINGS = [
            # Arrow keys belong to the focused OptionList. Once Tab moves to a
            # button, they must not keep moving a hidden list selection.
            Binding("d", "discover", "Discover models", priority=True),
            Binding("escape", "close", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        _ProviderDetailsScreen { align: center middle; background: #0e0c08 92%; }
        #provider-details-dialog { width: 84; height: 84%; background: #1a1712;
          border: round #c6a56b; padding: 1 2; }
        #provider-details-dialog .title { text-style: bold; color: #e5e5e5; }
        #provider-details-summary { height: auto; min-height: 6; color: #c6bca8;
          margin-bottom: 1; }
        #provider-details-models { height: 1fr; border: round #4a4338; }
        #provider-bypass-row { height: 3; align-vertical: middle; }
        #provider-bypass-label { width: 1fr; color: #c6bca8; }
        #provider-bypass { width: auto; }
        #provider-bypass-hint { color: #8f8778; margin-bottom: 1; }
        #provider-details-error { min-height: 1; color: #e0a3a3; }
        #provider-details-actions { height: 3; align-horizontal: right; }
        #provider-details-actions Button { margin-left: 1; }
        """

        def __init__(
            self,
            language: str,
            controller: ProviderController,
            provider_id: str,
        ) -> None:
            super().__init__()
            self.language = language
            self._controller = controller
            self.provider_id = provider_id

        def _label(self, ru: str, en: str) -> str:
            return _label(self.language, ru, en)

        def compose(self) -> ComposeResult:
            # Build the modal from registry-only configuration. Credential
            # availability/fingerprint comes from OS keyring and is loaded after
            # mount on a worker so compose never blocks the Textual event loop.
            provider = self._controller.registry.provider(self.provider_id)
            summary = (
                f"{provider.provider_id}\n"
                f"{provider.adapter_kind} · {provider.base_url}\n"
                f"privacy={provider.privacy_class} · timeout={provider.timeout_seconds:g}s · "
                f"retries={provider.max_transport_retries}\n"
                f"credential={self._label('проверяется…', 'checking…')}"
            )
            self._summary_base = summary
            with Vertical(id="provider-details-dialog"):
                yield Static(
                    self._label("Провайдер и модели", "Provider and models"),
                    classes="title",
                )
                yield Static(summary, id="provider-details-summary", markup=False)
                # Access lives with the provider it applies to rather than on
                # the crowded root hub. Same mode, same words, same default as
                # every service connection.
                with Horizontal(id="provider-bypass-row"):
                    yield Label(
                        self._label("Расширенные права", "Elevated access"),
                        id="provider-bypass-label",
                    )
                    yield Switch(value=provider.bypass, id="provider-bypass")
                yield Static(
                    self._label(
                        "Добавляет dev.command, git commit, network и desktop input. Push/publish/auth всё равно требуют отдельного разрешения.",
                        "Adds dev.command, git commit, network, and desktop input. Push/publish/auth still require separate approval.",
                    ),
                    id="provider-bypass-hint",
                    markup=False,
                )
                yield OptionList(id="provider-details-models")
                yield Static("", id="provider-details-error", markup=False)
                with Horizontal(id="provider-details-actions"):
                    yield Button(
                        self._label("Сделать модель активной", "Set model active"),
                        id="provider-details-active",
                    )
                    yield Button(
                        self._label("Найти модели", "Discover models"),
                        id="provider-details-discover",
                    )
                    yield Button(self._label("Закрыть", "Close"), id="provider-details-close")

        def on_mount(self) -> None:
            self.query_one("#provider-details-models", OptionList).focus()
            self._schedule_details_refresh()

        def on_unmount(self) -> None:
            self._details_epoch = getattr(self, "_details_epoch", 0) + 1

        def _schedule_details_refresh(self) -> None:
            self._details_epoch = getattr(self, "_details_epoch", 0) + 1
            epoch = self._details_epoch

            def execute() -> None:
                try:
                    details = self._controller.details(self.provider_id)
                    error: Optional[str] = None
                except Exception as exc:
                    details = None
                    error = str(exc)
                with contextlib.suppress(Exception):
                    self.app.call_from_thread(self._details_ready, epoch, details, error)

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="provider-details-refresh",
            )

        def _details_ready(self, epoch: int, details: Any, error: Optional[str]) -> None:
            if epoch != getattr(self, "_details_epoch", 0) or not self.is_mounted:
                return
            if error or details is None:
                self.query_one("#provider-details-error", Static).update(str(redact(error or "error")))
                return
            provider = details.provider
            credential = details.credential
            credential_text = (
                self._label("доступен", "available")
                if credential.get("available")
                else self._label("не настроен/недоступен", "not configured/unavailable")
            )
            fingerprint = str(credential.get("fingerprint") or "")
            summary = (
                f"{provider.provider_id}\n"
                f"{provider.adapter_kind} · {provider.base_url}\n"
                f"privacy={provider.privacy_class} · timeout={provider.timeout_seconds:g}s · "
                f"retries={provider.max_transport_retries}\n"
                f"credential={credential_text}"
                + (f" · {fingerprint}" if fingerprint else "")
            )
            self._summary_base = summary
            self.query_one("#provider-details-summary", Static).update(summary)
            self._refresh_models(details=details)

        def _refresh_models(self, *, details: Any = None) -> None:
            if details is None:
                details = self._controller.details(self.provider_id)
            self._models = {model.model_id: model for model in details.models}
            options = self.query_one("#provider-details-models", OptionList)
            selected = details.selected_model
            options.clear_options()
            for model in details.models:
                mark = "★ " if selected is not None and selected.model_id == model.model_id else "  "
                limits: list[str] = []
                if model.context_window:
                    limits.append(f"ctx={model.context_window}")
                if model.max_output_tokens:
                    limits.append(f"out={model.max_output_tokens}")
                suffix = f"  ({', '.join(limits)})" if limits else ""
                options.add_option(Option(f"{mark}{model.model_id}{suffix}", id=model.model_id))
            if not details.models:
                options.add_option(
                    Option(self._label("Моделей нет.", "No models."), id="provider-model-empty")
                )
            options.highlighted = 0

        def _selected_model_id(self) -> Optional[str]:
            options = self.query_one("#provider-details-models", OptionList)
            if options.highlighted is None:
                return None
            value = getattr(options.get_option_at_index(options.highlighted), "id", None)
            if value is None or value == "provider-model-empty":
                return None
            return str(value)

        def action_set_active(self) -> None:
            model_id = self._selected_model_id()
            if not model_id:
                return
            try:
                self._controller.select_model(self.provider_id, model_id)
            except Exception as exc:
                self.query_one("#provider-details-error", Static).update(str(redact(exc)))
                return
            refresh_status(self.app)
            self._schedule_details_refresh()
            self.query_one("#provider-details-error", Static).update(
                self._label("Модель активирована.", "Model activated.")
            )

        @on(OptionList.OptionHighlighted, "#provider-details-models")
        def _model_highlighted(self, event: OptionList.OptionHighlighted) -> None:
            """Append the highlighted model's honest capability row to the summary.

            The row renders exactly what the registry records — ``?`` stays
            ``?``. It lives on the summary block, not the status line, so it
            never overwrites an operation result such as "Model activated."
            """

            base = getattr(self, "_summary_base", None)
            if base is None:
                return
            summary = self.query_one("#provider-details-summary", Static)
            model_id = getattr(event.option, "id", None)
            model = getattr(self, "_models", {}).get(model_id)
            if model is None:
                summary.update(base)
                return
            capability = (
                f"{model.model_id}: {model_capability_summary(model)} · "
                + self._label(
                    f"источник={model.provenance}",
                    f"source={model.provenance}",
                )
            )
            summary.update(f"{base}\n{capability}")

        def action_discover(self) -> None:
            """Discover models from the provider endpoint and add the new ones."""

            self.query_one("#provider-details-error", Static).update(
                self._label("Поиск моделей…", "Discovering models…")
            )

            def execute() -> None:
                try:
                    result: Optional[Dict[str, Any]] = discover_models_for_provider(
                        self._controller, self.provider_id
                    )
                    error: Optional[str] = None
                except Exception as exc:
                    result = None
                    error = str(redact(exc))
                with contextlib.suppress(Exception):
                    self.app.call_from_thread(self._discover_done, result, error)

            self.app.run_worker(
                execute, thread=True, exclusive=True, group="provider-discover"
            )

        def _discover_done(
            self, result: Optional[Dict[str, Any]], error: Optional[str]
        ) -> None:
            status = self.query_one("#provider-details-error", Static)
            if error or result is None:
                status.update(str(redact(error or "error")))
                return
            added = result.get("added") or []
            status.update(
                self._label(
                    f"Найдено моделей: {result.get('discovered', 0)}, добавлено: {len(added)}.",
                    f"Discovered {result.get('discovered', 0)} model(s), added {len(added)}.",
                )
            )
            self._schedule_details_refresh()

        @on(Switch.Changed, "#provider-bypass")
        def _bypass_changed(self, event: Switch.Changed) -> None:
            """Persist the mode on the provider record that already exists.

            Nothing about the provider's transport is touched: this writes one
            additive preference through the controller that owns the registry,
            and the next KaroX agent session created for this provider reads
            it. A session already running keeps the profile it started with.
            """

            requested = bool(event.value)
            try:
                current = self._controller.registry.provider(self.provider_id)
                if bool(current.bypass) == requested:
                    return
                self._controller.edit_provider(self.provider_id, bypass=requested)
            except Exception as exc:
                self.query_one("#provider-details-error", Static).update(
                    str(redact(exc))
                )
                return
            self.query_one("#provider-details-error", Static).update(
                self._label(
                    "Расширенные права включены." if requested else "Обычные права включены.",
                    "Elevated access enabled." if requested else "Normal access enabled.",
                )
            )

        @on(OptionList.OptionSelected, "#provider-details-models")
        def _model_selected(self, _event: OptionList.OptionSelected) -> None:
            # Enter on the model list activates the highlighted model. Enter on
            # either button remains native Button.Pressed behavior.
            self.action_set_active()

        def action_previous(self) -> None:
            options = self.query_one("#provider-details-models", OptionList)
            if options.highlighted is not None:
                options.highlighted = max(0, options.highlighted - 1)

        def action_next(self) -> None:
            options = self.query_one("#provider-details-models", OptionList)
            if options.option_count:
                current = options.highlighted or 0
                options.highlighted = min(options.option_count - 1, current + 1)

        def action_close(self) -> None:
            self.dismiss("updated")

        @on(Button.Pressed)
        def _pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "provider-details-active":
                self.action_set_active()
            elif event.button.id == "provider-details-discover":
                self.action_discover()
            elif event.button.id == "provider-details-close":
                self.action_close()

    class _ProviderEditScreen(ModalScreen[Optional[str]]):
        """Edit provider transport settings without exposing its credential."""

        BINDINGS = [
            Binding("f10", "save", _C["en"]["save"], priority=True),
            Binding("escape", "cancel", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        _ProviderEditScreen { align: center middle; background: #0e0c08 92%; }
        #provider-edit-dialog { width: 76; height: auto; max-height: 90%;
          background: #1a1712; border: round #c6a56b; padding: 1 2; }
        #provider-edit-dialog .title { text-style: bold; color: #e5e5e5; }
        #provider-edit-adapter { height: auto; border: round #4a4338; }
        #provider-edit-error { min-height: 1; color: #e0a3a3; }
        #provider-edit-actions { height: 3; align-horizontal: right; }
        #provider-edit-actions Button { margin-left: 1; }
        """

        _ADAPTERS = (
            "openai_responses",
            "openai_compatible_chat",
            "anthropic_messages",
            "gemini_generate_content",
        )

        def __init__(
            self,
            language: str,
            controller: ProviderController,
            provider_id: str,
        ) -> None:
            super().__init__()
            self.language = language
            self._controller = controller
            self.provider_id = provider_id

        def _label(self, ru: str, en: str) -> str:
            return _label(self.language, ru, en)

        def compose(self) -> ComposeResult:
            # Editing provider configuration needs no credential lookup. Using
            # details() here synchronously touched the OS keyring during modal
            # composition and could freeze the first keyboard event.
            provider = self._controller.registry.provider(self.provider_id)
            with Vertical(id="provider-edit-dialog"):
                yield Static(self._label("Изменить провайдера", "Edit provider"), classes="title")
                yield Label("Adapter")
                with RadioSet(id="provider-edit-adapter"):
                    for adapter in self._ADAPTERS:
                        yield RadioButton(
                            adapter,
                            value=provider.adapter_kind == adapter,
                            id=f"provider-edit-{adapter}",
                        )
                yield Label("Base URL")
                yield Input(value=provider.base_url, id="provider-edit-url")
                yield Label(self._label("Таймаут, сек", "Timeout, sec"))
                yield Input(value=str(provider.timeout_seconds), id="provider-edit-timeout")
                yield Label(self._label("Повторы транспорта", "Transport retries"))
                yield Input(value=str(provider.max_transport_retries), id="provider-edit-retries")
                yield Static("", id="provider-edit-error", markup=False)
                with Horizontal(id="provider-edit-actions"):
                    yield Button(self._label("Отмена", "Cancel"), id="provider-edit-cancel")
                    yield Button(self._label("Сохранить", "Save"), id="provider-edit-save", variant="primary")

        def action_save(self) -> None:
            adapter_set = self.query_one("#provider-edit-adapter", RadioSet)
            index = adapter_set.pressed_index
            adapter = (
                self._ADAPTERS[index]
                if isinstance(index, int) and 0 <= index < len(self._ADAPTERS)
                else None
            )
            try:
                timeout = float(self.query_one("#provider-edit-timeout", Input).value)
                retries = int(self.query_one("#provider-edit-retries", Input).value)
                self._controller.edit_provider(
                    self.provider_id,
                    adapter_kind=adapter,
                    base_url=self.query_one("#provider-edit-url", Input).value.strip(),
                    timeout_seconds=timeout,
                    max_transport_retries=retries,
                )
            except Exception as exc:
                self.query_one("#provider-edit-error", Static).update(str(redact(exc)))
                return
            self.dismiss("updated")

        def action_cancel(self) -> None:
            self.dismiss(None)

        @on(Button.Pressed)
        def _pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "provider-edit-save":
                self.action_save()
            elif event.button.id == "provider-edit-cancel":
                self.action_cancel()

    # --- Model providers list -------------------------------------------

    class ModelProvidersScreen(ModalScreen[Optional[str]]):
        """List saved model providers; test/set-active/delete/copy."""

        BINDINGS = [
            Binding("a", "add", _C["en"]["add"], priority=True),
            Binding("e", "edit", _C["en"]["edit"], priority=True),
            Binding("t", "test", _C["en"]["test"], priority=True),
            Binding("c", "copy_secret", _C["en"]["copy_secret"], priority=True),
            # The focused provider list handles arrows natively. Global priority
            # arrows made the selection move even after Tab focused an action.
            Binding("delete", "delete", _C["en"]["delete"], priority=True),
            Binding("escape", "cancel", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        ModelProvidersScreen { align: center middle; background: #0e0c08 92%; }
        /* `width: 100%` with a ceiling: a fixed 86 was wider than 40-80
           column terminals and clipped the whole right side. Height grows
           with content and scrolls past max-height instead of clipping. */
        #mp-list-dialog { width: 100%; max-width: 86; height: auto;
          max-height: 90%; background: #1a1712;
          border: round #c6a56b; padding: 1 2; }
        #mp-list-dialog .title { text-style: bold; color: #e5e5e5; }
        #mp-list-dialog .hint { color: #8a7e6a; margin-bottom: 1; }
        #mp-providers { height: auto; max-height: 16; border: round #4a4338; }
        #mp-list-error { color: #e0a3a3; min-height: 1; max-height: 4;
          text-wrap: wrap; overflow-y: auto; }
        #mp-list-error.status-success { color: #8aab7e; }
        #mp-list-actions { height: 3; }
        #mp-list-actions Button { margin-right: 1; }
        #mp-list-buttons { height: 3; align-horizontal: right; }
        #mp-list-buttons Button { margin-left: 1; }
        """

        def __init__(self, language: str = "ru") -> None:
            super().__init__()
            self.language = language
            from .connection_tests import test_model_provider
            from .paths import config_dir
            from .registry import ProviderRegistry

            registry = ProviderRegistry(config_dir() / "vnext" / "providers.json")
            self._controller = ProviderController(
                registry=registry,
                credentials=CredentialStore(),
                tester=lambda provider, model: test_model_provider(
                    provider, model, timeout_seconds=30.0
                ),
            )
            self._registry = self._controller.registry

        def _label(self, ru: str, en: str) -> str:
            return _label(self.language, ru, en)

        def _provider_rows(self) -> List[str]:
            selected = None
            try:
                selected = self._registry.selected_model()
            except Exception:
                selected = None
            rows: List[str] = []
            for provider in self._registry.providers():
                models = [m.model_id for m in self._registry.models(provider.provider_id)]
                active = selected is not None and selected.provider_id == provider.provider_id
                mark = "★ " if active else "  "
                rows.append(
                    f"{mark}{provider.provider_id}  ({provider.adapter_kind} · {len(models)} model(s))"
                )
            return rows

        def compose(self) -> ComposeResult:
            # VerticalScroll for the same reason as the hub: fixed-height
            # dialogs clipped the action buttons at small terminal sizes.
            with VerticalScroll(id="mp-list-dialog"):
                yield Static(self._label("API-провайдеры", "API providers"), classes="title")
                yield Static(_t(self.language, "model_providers_hint"), classes="hint")
                yield OptionList(id="mp-providers")
                yield Static("", id="mp-list-error", markup=False)
                with HorizontalScroll(id="mp-list-actions"):
                    yield Button(self._label("Подробности", "Details"), id="mp-details")
                    yield Button(self._label("Добавить", "Add"), id="mp-add")
                    yield Button(self._label("Изменить", "Edit"), id="mp-edit")
                    yield Button(self._label("Проверить", "Verify"), id="mp-test")
                    yield Button(self._label("Сделать активным", "Set active"), id="mp-active")
                    yield Button(self._label("Копировать ключ", "Copy key"), id="mp-copy")
                with Horizontal(id="mp-list-buttons"):
                    yield Button(self._label("Закрыть", "Close"), id="mp-close")

        def on_mount(self) -> None:
            self._refresh()
            self.query_one("#mp-providers", OptionList).focus()

        def _refresh(self) -> None:
            options = self.query_one("#mp-providers", OptionList)
            options.clear_options()
            rows = self._provider_rows()
            if not rows:
                options.add_option(Option(self._label("Провайдеров пока нет.", "No providers yet."), id="mp-empty"))
                return
            for provider in self._registry.providers():
                row = f"{'★ ' if self._is_active(provider.provider_id) else '  '}{provider.provider_id}"
                options.add_option(Option(row, id=provider.provider_id))

        def _is_active(self, provider_id: str) -> bool:
            try:
                selected = self._registry.selected_model()
                return selected is not None and selected.provider_id == provider_id
            except Exception:
                return False

        def _selected_provider_id(self) -> Optional[str]:
            options = self.query_one("#mp-providers", OptionList)
            if options.highlighted is None:
                return None
            option = options.get_option_at_index(options.highlighted)
            cid = getattr(option, "id", None)
            if not cid or cid == "mp-empty":
                return None
            return cid

        @on(OptionList.OptionSelected, "#mp-providers")
        def _provider_selected(self, _event: OptionList.OptionSelected) -> None:
            self.action_details()

        def _set_error(self, message: str, *, ok: bool = False) -> None:
            status = self.query_one("#mp-list-error", Static)
            status.update(str(redact(message)) if message else "")
            status.set_class(not ok, "status-error")
            status.set_class(ok, "status-success")

        def action_details(self) -> None:
            provider_id = self._selected_provider_id()
            if not provider_id:
                self._set_error(self._label("Выберите провайдера.", "Select a provider."))
                return
            self.app.push_screen(
                _ProviderDetailsScreen(
                    self.language,
                    self._controller,
                    provider_id,
                ),
                lambda _result: self._details_closed(),
            )

        def _details_closed(self) -> None:
            refresh_status(self.app)
            self._refresh()
            self.query_one("#mp-providers", OptionList).focus()

        def action_edit(self) -> None:
            provider_id = self._selected_provider_id()
            if not provider_id:
                self._set_error(self._label("Выберите провайдера.", "Select a provider."))
                return
            self.app.push_screen(
                _ProviderEditScreen(
                    self.language,
                    self._controller,
                    provider_id,
                ),
                lambda _result: self._details_closed(),
            )

        def action_add(self) -> None:
            # Close this screen and ask the app to launch the existing API
            # provider wizard; the wizard has its own callback semantics, so it
            # runs cleanly at the top of the modal stack. The list refreshes
            # when the user returns to it.
            self.dismiss("add_provider")

        def action_test(self) -> None:
            provider_id = self._selected_provider_id()
            if not provider_id:
                self._set_error(self._label("Выберите провайдера.", "Select a provider."))
                return
            try:
                details = self._controller.details(provider_id)
            except Exception as exc:
                self._set_error(str(exc))
                return
            if not details.models:
                self._set_error(self._label("У провайдера нет моделей.", "The provider has no models."))
                return
            model = details.selected_model or details.models[0]
            self._set_error(_t(self.language, "testing"))

            def execute() -> None:
                try:
                    result = self._controller.test_provider(
                        provider_id,
                        model_or_alias=model.model_id,
                    )
                except Exception as exc:
                    result = {
                        "state": "failed",
                        "failure_kind": "provider_test_failed",
                        "detail": str(redact(exc)),
                    }
                self.app.call_from_thread(self._test_done, result)

            self.run_worker(execute, thread=True, exclusive=True, group="provider-test")

        def _test_done(self, result: Dict[str, Any]) -> None:
            state = result.get("state")
            if state == "ok":
                usage = result.get("usage") or {}
                self._set_error(
                    self._label(
                        f"OK: {result.get('model_id', '')} · {usage}",
                        f"OK: {result.get('model_id', '')} · {usage}",
                    ),
                    ok=True,
                )
            else:
                kind = result.get("failure_kind", "error")
                detail = result.get("detail", "")
                self._set_error(self._label(f"{kind}: {detail}", f"{kind}: {detail}"))

        def action_set_active(self) -> None:
            provider_id = self._selected_provider_id()
            if not provider_id:
                self._set_error(self._label("Выберите провайдера.", "Select a provider."))
                return
            try:
                details = self._controller.details(provider_id)
            except Exception as exc:
                self._set_error(str(exc))
                return
            if not details.models:
                self._set_error(self._label("У провайдера нет моделей.", "The provider has no models."))
                return
            if len(details.models) > 1:
                self.action_details()
                return
            try:
                self._controller.select_model(provider_id, details.models[0].model_id)
            except Exception as exc:
                self._set_error(str(exc))
                return
            refresh_status(self.app)
            self._set_error(self._label("Сделано активным.", "Set as active."), ok=True)
            self._refresh()

        def action_copy_secret(self) -> None:
            provider_id = self._selected_provider_id()
            if not provider_id:
                self._set_error(self._label("Выберите провайдера.", "Select a provider."))
                return
            try:
                details = self._controller.details(provider_id)
                reference = details.provider.credential_ref
                if not reference:
                    self._set_error(_t(self.language, "no_secret"))
                    return
                secret = self._controller.credentials.resolve(reference)
            except (CredentialError, ValueError) as exc:
                self._set_error(str(exc))
                return
            copy_text(secret)
            self.app.notify(_t(self.language, "copied"))

        def action_delete(self) -> None:
            provider_id = self._selected_provider_id()
            if not provider_id:
                self._set_error(self._label("Выберите провайдера.", "Select a provider."))
                return

            def _confirm(confirmed: Optional[bool]) -> None:
                if not confirmed:
                    self.query_one("#mp-providers", OptionList).focus()
                    return
                try:
                    result = self._controller.remove_provider(
                        provider_id,
                        cascade=True,
                        delete_credential=True,
                    )
                    if result.credential_cleanup == "shared_not_deleted":
                        self._set_error(
                            self._label(
                                "Провайдер удалён; общий ключ сохранён для другого провайдера.",
                                "Provider removed; the shared key was kept for another provider.",
                            ),
                            ok=True,
                        )
                except Exception as exc:
                    self._set_error(str(exc))
                refresh_status(self.app)
                self._refresh()
                self.query_one("#mp-providers", OptionList).focus()

            self.app.push_screen(
                _ConfirmScreen(
                    self._label("Удалить провайдера?", "Delete provider?"),
                    self._label(
                        f"Удалить «{provider_id}» и все его модели и ключи?",
                        f"Delete “{provider_id}”, its models, and its keys?",
                    ),
                    language=self.language,
                ),
                _confirm,
            )

        def action_previous(self) -> None:
            options = self.query_one("#mp-providers", OptionList)
            if options.highlighted is None:
                options.highlighted = 0
            else:
                options.highlighted = max(0, options.highlighted - 1)
            options.scroll_to_highlight()

        def action_next(self) -> None:
            options = self.query_one("#mp-providers", OptionList)
            count = len(options.options)
            if count == 0:
                return
            if options.highlighted is None:
                options.highlighted = 0
            else:
                options.highlighted = min(count - 1, options.highlighted + 1)
            options.scroll_to_highlight()

        def action_cancel(self) -> None:
            self.dismiss(None)

        @on(Button.Pressed)
        def _pressed(self, event: Button.Pressed) -> None:
            actions: Dict[str, Callable[[], None]] = {
                "mp-details": self.action_details,
                "mp-add": self.action_add,
                "mp-edit": self.action_edit,
                "mp-test": self.action_test,
                "mp-active": self.action_set_active,
                "mp-copy": self.action_copy_secret,
                "mp-close": self.action_cancel,
            }
            action = actions.get(event.button.id or "")
            if action:
                action()

    # --- Small confirm modal (mirrors tui.ConfirmScreen) -----------------

    class _ConfirmScreen(ModalScreen[Optional[bool]]):
        BINDINGS = [
            # Enter is owned by the focused Yes/No button. A priority screen
            # binding here made Enter choose Yes even after Tab moved to Cancel.
            Binding("escape", "no", _C["ru"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        _ConfirmScreen { align: center middle; background: #0e0c08 92%; }
        /* B5 responsive. Was a fixed `width: 70`, so in a 46-column terminal
           the confirmation was wider than the screen and its buttons ran off
           the edge -- on the one dialog whose entire purpose is to be read and
           answered before something irreversible happens. Exactly the B2
           defect, on a worse screen. `100%` with a ceiling keeps the
           comfortable size on a real window and fits the small one. */
        #cc-confirm-dialog { width: 100%; max-width: 70; height: auto;
          max-height: 80%; background: #1a1712; border: round #c6a56b;
          padding: 1 2; }
        #cc-confirm-dialog .title { text-style: bold; color: #e5e5e5; margin-bottom: 1; }
        #cc-confirm-dialog .body { color: #dcdcdc; margin-bottom: 1; }
        #cc-confirm-buttons { height: 3; align-horizontal: right; }
        #cc-confirm-buttons Button { margin-left: 1; }
        """

        def __init__(self, title: str, body: str, *, language: str = "ru") -> None:
            super().__init__()
            self._title = title
            self._body = body
            self.language = language

        def compose(self) -> ComposeResult:
            with VerticalScroll(id="cc-confirm-dialog"):
                yield Static(self._title, classes="title")
                yield Static(self._body, classes="body")
                with Horizontal(id="cc-confirm-buttons"):
                    yield Button(_t(self.language, "cancel"), id="cc-no")
                    yield Button(_t(self.language, "save"), id="cc-yes")

        def on_mount(self) -> None:
            self.query_one("#cc-yes", Button).focus()

        def action_yes(self) -> None:
            self.dismiss(True)

        def action_no(self) -> None:
            self.dismiss(None)

        @on(Button.Pressed)
        def _pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cc-yes":
                self.dismiss(True)
            elif event.button.id == "cc-no":
                self.dismiss(None)

    class ServicePickerScreen(ModalScreen[Optional[str]]):
        """Which known service to connect. One list, no protocols.

        B3. Entries are addressed by preset id, which is also the production
        catalog key, so a test and a callback name the same thing and reordering
        the list cannot repoint either.
        """

        BINDINGS = [
            Binding("enter", "choose", "Open", priority=True),
            Binding("up", "previous", show=False, priority=True),
            Binding("down", "next", show=False, priority=True),
            Binding("escape", "cancel", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        ServicePickerScreen { align: center middle; background: #0e0c08 92%; }
        #svc-pick-dialog { width: 100%; max-width: 56; height: auto;
          max-height: 90%; background: #1a1712; border: round #c6a56b;
          padding: 1 2; }
        #svc-pick-dialog .title { text-style: bold; color: #e5e5e5; }
        #svc-pick-list { height: auto; max-height: 10; border: none;
          background: #1a1712; }
        #svc-pick-hint { color: #8a7e6a; margin-top: 1; }
        """

        def __init__(self, language: str = "ru") -> None:
            super().__init__()
            self.language = language

        def compose(self) -> ComposeResult:
            with Vertical(id="svc-pick-dialog"):
                yield Static(
                    _label(self.language, "Подключить сервис", "Connect a service"),
                    classes="title",
                )
                yield OptionList(id="svc-pick-list")
                yield Static(
                    _label(
                        self.language,
                        "Enter — открыть · Esc — назад",
                        "Enter — open · Esc — back",
                    ),
                    id="svc-pick-hint",
                )

        def on_mount(self) -> None:
            options = self.query_one("#svc-pick-list", OptionList)
            for preset_id in service_flow_presets():
                options.add_option(
                    Option(service_display_name(preset_id), id=preset_id)
                )
            options.highlighted = 0
            options.focus()

        def _step(self, delta: int) -> None:
            options = self.query_one("#svc-pick-list", OptionList)
            count = len(options.options)
            if not count:
                return
            index = options.highlighted if options.highlighted is not None else 0
            options.highlighted = max(0, min(count - 1, index + delta))

        def action_previous(self) -> None:
            self._step(-1)

        def action_next(self) -> None:
            self._step(1)

        def action_choose(self) -> None:
            options = self.query_one("#svc-pick-list", OptionList)
            if options.highlighted is None:
                return
            option = options.get_option_at_index(options.highlighted)
            self.dismiss(getattr(option, "id", None))

        def action_cancel(self) -> None:
            self.dismiss(None)

        @on(OptionList.OptionSelected, "#svc-pick-list")
        def _selected(self, event: OptionList.OptionSelected) -> None:
            self.dismiss(getattr(event.option, "id", None))

    class ServiceConnectScreen(ModalScreen[Optional[str]]):
        """One service, its steps, its address, and one check.

        B3. The screen a person sees after choosing ChatGPT Web, Claude Web or
        ClickUp. It answers: what do I do, where do I paste it, does it work.

        What it does *not* do is the important half. Opening it starts nothing,
        stops nothing and restarts nothing: it reads the connection controller,
        shows the endpoint that already exists, and leaves a healthy bridge
        exactly as it found it. A screen that restarted a bridge to render itself
        would invalidate the URL the user had already pasted into the service.

        Defaults are not restated here. The transport, the auth scheme, the
        tunnel and the endpoint path all come from the production preset through
        the existing launcher path; this layer reads a display name and a set of
        steps and shows no technical field at all.
        """

        BINDINGS = [
            # Enter belongs to the focused button. A global priority binding here
            # made the primary button look clickable while Enter secretly ran a
            # different action (Verify). Space mirrors ordinary button keyboard
            # behaviour on this button-only surface without stealing text input.
            Binding("space", "activate_focused", show=False, priority=True),
            Binding("b", "focus_bypass", "Bypass", priority=True),
            Binding("f1", "navigation_help", "Help", priority=True),
            Binding("f5", "verify", "Verify", priority=True),
            # B4. The same key as the provider form. One advanced contract means
            # one way in, so a person who learned F2 on a provider does not have
            # to learn a second key for a service.
            Binding("f2", "advanced", "Advanced settings", priority=True),
            Binding("f3", "advanced", "Limits", priority=True),
            Binding("ctrl+w", "workspace", "Project", priority=True),
            Binding("s", "start_repair", "Start / Repair", priority=True),
            Binding("c", "copy_url", _C["en"]["copy_url"], priority=True),
            Binding("p", "copy_approval_password", "Copy credential", priority=True),
            Binding("t", "copy_test_prompt", "Copy test prompt", priority=True),
            Binding("d", "diagnostics", "Diagnostics", priority=True),
            Binding("x", "stop", "Stop", priority=True),
            Binding("delete", "delete_profile", "Delete", priority=True),
            Binding("escape", "cancel", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        ServiceConnectScreen { align: center middle; background: #0e0c08 92%; }
        /* `width: 100%` with a ceiling, and a height that grows only as far as
           the content needs. The B2 lesson applies: a percentage height with a
           `1fr` child eats the terminal, so nothing here is `1fr`. */
        #svc-dialog { width: 100%; max-width: 64; height: auto; max-height: 92%;
          background: #1a1712; border: round #c6a56b; padding: 1 2; }
        #svc-dialog .title { text-style: bold; color: #e5e5e5; }
        #svc-project { height: auto; color: #8a7e6a; margin-top: 1; }
        #svc-full-access-row { height: 3; margin-top: 1; align-vertical: middle; }
        #svc-full-access-label { width: 1fr; color: #c6bca8; }
        #svc-full-access { width: auto; }
        #svc-status { height: auto; color: #c6bca8; margin-top: 1; padding: 0 1; }
        #svc-status.status-ok { color: #8aab7e; border-left: thick #8aab7e; }
        #svc-status.status-warn { color: #d4b676; border-left: thick #d4b676; }
        #svc-status.status-error { color: #e0a3a3; border-left: thick #e0a3a3; }
        #svc-steps { height: auto; color: #c6bca8; margin-top: 1; }
        #svc-endpoint-label { height: 1; color: #8a7e6a; margin-top: 1; }
        #svc-endpoint { height: auto; color: #e6c773; text-style: bold; }
        #svc-note { height: auto; color: #8a7e6a; margin-top: 1; }
        #svc-actions { height: auto; margin-top: 1; grid-size: 3; grid-gutter: 0 1; }
        /* The normal screen is intentionally small: one primary action, at most
           one context action, and More. Technical lifecycle actions stay mounted
           for keyboard/API compatibility but are never rendered here. */
        #svc-actions Button { width: 1fr; min-width: 0; margin: 0; }
        #svc-primary { background: #c6a56b; color: #121212; text-style: bold; }
        #svc-auth, #svc-verify, #svc-diagnostics, #svc-lifecycle, #svc-stop, #svc-delete { display: none; }
        #svc-delete { color: #e0a3a3; }
        #svc-hint { height: auto; color: #6f6658; margin-top: 1; }
        """

        def __init__(
            self,
            language: str = "ru",
            *,
            preset_id: str = "clickup",
            connection_id: Optional[str] = None,
        ) -> None:
            super().__init__()
            self.language = language
            self.preset_id = preset_id
            self.connection_id = connection_id
            self._controller = connection_controller()
            self._status = SERVICE_NOT_CONFIGURED
            self._endpoint = ""
            self._checking = False
            self._typed_status: Optional[ConnectionLiveStatus] = None
            self._last_state: Optional[Any] = None
            self._has_snapshot = False
            self._full_access_busy = False
            self._deleting = False
            # A programmatic Switch.value sync posts the same Changed message as
            # a human toggle. Remember that one value until its queued message is
            # consumed so a refresh can never trigger a second bridge restart.
            self._ignore_full_access_event: Optional[bool] = None
            # Live connection discovery touches the runtime store, process table
            # and (for credentials) the OS keyring. None of that may run on the
            # Textual event loop: a slow Windows/keyring probe would otherwise
            # freeze Tab/Enter/Esc and make dismissing this modal feel hung.
            self._refreshing = False
            self._refresh_epoch = 0
            self._poll_timer: Optional[Any] = None

        def _english(self) -> bool:
            return self.language != "ru"

        # ------------------------------------------------------------ view model

        def _current_repository(self) -> Path:
            try:
                return Path(getattr(self.app, "repository", Path.cwd())).expanduser().resolve()
            except Exception:
                return Path.cwd().resolve()

        def _repository_matches(self, raw: Any) -> bool:
            """Whether a saved bridge is bound to the project selected in KaroX."""
            if not isinstance(raw, str) or not raw.strip():
                return False
            try:
                saved = Path(raw).expanduser().resolve()
            except (OSError, RuntimeError):
                return False
            return os.path.normcase(str(saved)) == os.path.normcase(
                str(self._current_repository())
            )

        def _saved_profile_covers_current(self, saved: Any) -> bool:
            """Whether this saved bridge serves the project selected in KaroX.

            A physical bridge serves every project in its approved registry,
            not only the anchor repository it was first created for. Selecting
            another approved project must keep the same bridge card connected:
            project selection chooses the target for new tasks, it never
            changes bridge identity. Without this, choosing a second approved
            project made the shared bridge card fall back to "not configured"
            and offer to launch a duplicate bridge.
            """
            if self._repository_matches(str(getattr(saved, "repository", "") or "")):
                return True
            for entry in getattr(saved, "projects", ()) or ():
                path = entry.get("path", "") if isinstance(entry, dict) else ""
                if path and self._repository_matches(path):
                    return True
            return False

        def _state(self) -> Optional[Any]:
            """The saved connection for this service, if there is one.

            First checks the connection registry for a record with this
            preset_id. If none is found, discovers saved bridge profiles whose
            target ``profile`` matches the preset — so a ChatGPT Web bridge
            launched with ``profile=chatgpt-web`` is found even if it was never
            registered in ``connections.json``.
            """
            hosted_web = self.preset_id in {
                "chatgpt-web",
                "claude-web",
                "hyperagent-web",
                "notion",
            }
            try:
                if self.connection_id:
                    return self._controller.get(self.connection_id)
                # Registry records predate repository binding. They remain the
                # source for bearer/generic services, but hosted OAuth bridges
                # are selected from their repository-bound saved profiles below.
                if not hosted_web:
                    for state in self._controller.list():
                        target = getattr(state, "target", None)
                        if target is not None and target.preset_id == self.preset_id:
                            return state
            except Exception:
                pass
            # B7: discover a live watchdog only when it belongs to the project
            # currently selected in KaroX. Two Hyperagent profiles for two repos
            # must never collapse into one ambiguous service row.
            try:
                from .web_bridge_launcher import discover_saved_bridge_profiles_for_preset
                from .web_bridge_profiles import WebBridgeProfileStore

                store = WebBridgeProfileStore()
                for p in discover_saved_bridge_profiles_for_preset(self.preset_id):
                    repository = str(p.get("repository") or "")
                    saved_record: Optional[Any] = None
                    name = str(p.get("saved_profile") or "")
                    if name:
                        with contextlib.suppress(Exception):
                            saved_record = store.get(name)
                    if not repository and saved_record is not None:
                        repository = str(saved_record.repository or "")
                    # The selected project matches when it is the anchor
                    # repository OR any entry of the bridge's approved
                    # project registry (multi-project saved profiles).
                    if self._repository_matches(repository) or (
                        saved_record is not None
                        and self._saved_profile_covers_current(saved_record)
                    ):
                        p = {**p, "repository": repository}
                        return _DiscoveredBridgeState(p, self.preset_id)
            except Exception:
                pass

            # A saved launch profile is still a configured connection even while
            # its bridge is stopped (or a first bootstrap failed before writing a
            # watchdog). The old screen looked only at live watchdogs, so Notion
            # fell back to "not configured" and reopened the whole setup wizard
            # despite `notion-auto-*` already existing. Synthesize a stopped state
            # from the secret-free profile store so Start/Repair can call the
            # canonical `start_saved_bridge()` path directly.
            try:
                from .web_bridge_launcher import saved_web_bridge_session_id
                from .web_bridge_profiles import WebBridgeProfileStore

                # One source of truth for the logical/physical binding; see
                # SERVICE_TARGET_PROFILES.
                wanted = service_target_profile(self.preset_id)
                matching_saved = []
                for saved in WebBridgeProfileStore().list():
                    if saved.target_profile != wanted:
                        continue
                    if not self._saved_profile_covers_current(saved):
                        continue
                    matching_saved.append(saved)
                if matching_saved:
                    chosen = self._select_saved_profile(matching_saved)
                    p = {
                        "saved_profile": chosen.name,
                        "session_id": saved_web_bridge_session_id(chosen.name),
                        "profile": chosen.target_profile,
                        "repository": chosen.repository or "",
                        "public_url": chosen.public_url or "",
                        "tunnel": chosen.tunnel,
                        "bridge_pid": None,
                        "tunnel_pid": None,
                        "persistent_session": True,
                    }
                    return _DiscoveredBridgeState(p, self.preset_id)
            except Exception:
                pass
            return None

        def view(self) -> ServiceView:
            """Everything the screen may show, and nothing else."""

            english = self._english()
            return ServiceView(
                preset_id=self.preset_id,
                name=service_display_name(self.preset_id),
                steps=service_steps(self.preset_id, english),
                endpoint=self._endpoint,
                status=self._status,
                manual_note=service_manual_note(self.preset_id, english),
            )

        def _steps_text(self) -> str:
            """Short, state-driven instructions for the normal connection path."""

            english = self._english()
            typed = self._typed_status
            name = service_display_name(self.preset_id)
            if typed is None or typed.overall == OverallStatus.NOT_CONFIGURED:
                return _label(
                    self.language,
                    "KaroX сначала подготовит защищённый адрес для этого подключения.\n"
                    "Нажмите «Запустить KaroX» — URL появится здесь автоматически.",
                    "KaroX will first prepare a protected address for this connection.\n"
                    "Press Start KaroX and the URL will appear here automatically.",
                )
            if typed.overall == OverallStatus.STOPPED_READY_TO_RESTART:
                return _label(
                    self.language,
                    "Настройка уже сохранена. Нажмите «Восстановить» — новый адрес создавать не нужно.",
                    "The setup is already saved. Press Repair — you do not need to create a new connection.",
                )
            if typed.overall == OverallStatus.FULLY_VERIFIED:
                return _label(
                    self.language,
                    f"✓ {name} подключён\n✓ Инструменты KaroX отвечают\nГотово — можно закрыть это окно.",
                    f"✓ {name} connected\n✓ KaroX tools respond\nDone — you can close this window.",
                )
            if typed.overall == OverallStatus.CONNECTED_UNVERIFIED:
                return _label(
                    self.language,
                    f"✓ {name} уже подключился к KaroX.\nОсталось нажать «Проверить», чтобы подтвердить инструменты.",
                    f"✓ {name} has reached KaroX.\nPress Verify once to confirm the tools.",
                )
            if typed.overall == OverallStatus.WAITING_FOR_CHATGPT:
                return _label(
                    self.language,
                    f"✓ {name} уже начал подключение.\nЗавершите сохранение в {name}; KaroX обновит статус автоматически.",
                    f"✓ {name} has started connecting.\nFinish saving it in {name}; KaroX will update automatically.",
                )
            return "\n".join(service_steps(self.preset_id, english))

        def _primary_starts_bridge(self) -> bool:
            typed = self._typed_status
            if not self._endpoint or typed is None:
                return True
            return typed.overall in {
                OverallStatus.NOT_CONFIGURED,
                OverallStatus.STOPPED_READY_TO_RESTART,
                OverallStatus.DEGRADED,
                OverallStatus.ERROR,
            }

        def _primary_label(self) -> str:
            if self._primary_starts_bridge():
                typed = self._typed_status
                if typed is not None and typed.overall in {
                    OverallStatus.DEGRADED,
                    OverallStatus.ERROR,
                }:
                    return _label(self.language, "Исправить", "Repair")
                # A saved registry/profile record already exists even when the
                # first launch failed before watchdog/credential creation. That
                # is recovery, not setup, so do not send the user back through a
                # mental model of "starting from scratch".
                if self._has_snapshot and self._last_state is not None:
                    return _label(self.language, "Восстановить", "Repair")
                return _label(self.language, "Запустить KaroX", "Start KaroX")
            return _label(self.language, "Скопировать URL", "Copy URL")

        def _show_restart_button(self) -> bool:
            typed = self._typed_status
            if not self._has_snapshot or self._checking or not self._endpoint or typed is None:
                return False
            return typed.overall in {
                OverallStatus.READY_FOR_CHATGPT_SETUP,
                OverallStatus.WAITING_FOR_CHATGPT,
                OverallStatus.CONNECTED_UNVERIFIED,
                OverallStatus.FULLY_VERIFIED,
            }

        def _uses_bearer_auth(self) -> bool:
            try:
                return mcp_client_preset(self.preset_id).auth_scheme == "bearer"
            except Exception:
                return False

        def _project_text(self) -> str:
            """Show which local folder a new bridge will bind to."""
            try:
                repository = Path(getattr(self.app, "repository", Path.cwd())).expanduser().resolve()
            except Exception:
                repository = Path.cwd()
            return _label(
                self.language,
                f"Проект: {repository} · Ctrl+W — сменить",
                f"Project: {repository} · Ctrl+W — change",
            )

        def _select_saved_profile(self, candidates: List[Any]) -> Any:
            """Pick which saved profile this service row represents.

            Several saved profiles can target one preset in one repository and
            still share a port (five ``chatgpt-web`` profiles on 8765 is a real
            configuration). The port can only belong to one of them, so a name or
            ``desired_running`` heuristic is free to pick a profile that is *not*
            the live one -- and Start/Repair then reports the live bridge as
            ``port 8765 is held by an unrelated process``, telling the user their
            working connection is a foreign intruder.

            Whoever provably holds the port wins, because that is the bridge the
            user is actually looking at. Only when nothing is live do the previous
            intent/name heuristics decide.
            """
            if len(candidates) == 1:
                return candidates[0]

            from .port_ownership import check_port_ownership

            for cand in candidates:
                try:
                    verdict = check_port_ownership(cand.name, port=cand.port)
                except Exception:
                    continue
                # Live, or ours-but-unmanaged: both mean this profile owns the
                # port, so it is the one this row must act on.
                if verdict.verdict == "reuse_same_profile" or (
                    verdict.owned_orphan_pid is not None
                ):
                    return cand

            chosen = candidates[0]
            for cand in candidates:
                try:
                    from .saved_bridge_supervisor import _read_desired_running

                    if _read_desired_running(cand.name):
                        chosen = cand
                        break
                except Exception:
                    pass
            if chosen is candidates[0] and len(candidates) > 1:
                for cand in candidates:
                    if cand.name.startswith("chatgpt") or cand.name == self.preset_id:
                        chosen = cand
                        break
            return chosen

        def _saved_profile_name(self) -> str:
            state = self._last_state if self._has_snapshot else None
            target = getattr(state, "target", None)
            return str(getattr(target, "name", "") or "")

        def _bypass_state(self) -> Optional[bool]:
            """Current Bypass mode for the saved bridge this row acts on.

            ``None`` means "unknown, so do not offer the switch": either this
            preset keeps no saved profile or none has been created yet.
            """

            if not bypass_supported(self.preset_id):
                return None
            profile_name = self._saved_profile_name()
            if profile_name and saved_bridge_target(self.preset_id) is not None:
                try:
                    from .access_mode import saved_profile_bypass_enabled
                    from .web_bridge_profiles import WebBridgeProfileStore

                    profile = WebBridgeProfileStore().get(profile_name)
                    return saved_profile_bypass_enabled(profile)
                except Exception:
                    return None
            # Record-backed connection (ClickUp, PromptQL, generic/custom MCP):
            # the mode lives on the saved connection record its runtime is
            # started from.
            connection_id = self._bypass_connection_id()
            if not connection_id:
                return None
            try:
                return bool(connection_registry().get(connection_id).bypass)
            except Exception:
                return None

        def _bypass_connection_id(self) -> str:
            state = self._last_state if self._has_snapshot else None
            target = getattr(state, "target", None)
            value = str(getattr(target, "connection_id", "") or "")
            if not value or value.startswith("discovered-"):
                return ""
            return value

        def _bypass_shared_with(self) -> Tuple[str, ...]:
            """Display names of other connections served by the same bridge.

            Bypass is stored on the saved profile, and the bridge reads its
            capability profile from there, so the enforcement boundary is the
            physical bridge -- not the logical connection. When the bridge is
            shared this is stated in a confirmation instead of pretending the
            mode is per-client.
            """

            profile_name = self._saved_profile_name()
            if not profile_name:
                return ()
            names: list[str] = []
            for other in shared_bridge_presets(self.preset_id):
                with contextlib.suppress(Exception):
                    names.append(service_display_name(other))
            return tuple(names)

        def compose(self) -> ComposeResult:
            english = self._english()
            name = service_display_name(self.preset_id)
            # VerticalScroll, not Vertical: at small terminal sizes the
            # connected-state content (status + steps + address + actions)
            # exceeds max-height and a plain Vertical clipped the lower
            # controls with no way to reach them. Scrolling keeps every
            # action reachable; focus auto-scrolls into view.
            with VerticalScroll(id="svc-dialog"):
                # The service name is the title. The old "Connect …" heading made
                # an already-running bridge look as though setup had not started.
                yield Static(name, classes="title")
                yield Static(self._project_text(), id="svc-project", markup=False)
                if bypass_supported(self.preset_id):
                    with Horizontal(id="svc-full-access-row"):
                        yield Label(
                            _label(
                                self.language,
                                "Bypass режим",
                                "Bypass mode",
                            ),
                            id="svc-full-access-label",
                        )
                        yield Switch(value=False, id="svc-full-access")
                # Status comes first: before reading instructions the user should
                # know whether KaroX is already ready, waiting, or connected.
                yield Static("", id="svc-status", markup=False)
                yield Static(
                    self._steps_text(),
                    id="svc-steps",
                    markup=False,
                )
                yield Static(
                    _label(
                        self.language,
                        f"Адрес для {name}",
                        f"Address for {name}",
                    ),
                    id="svc-endpoint-label",
                    markup=False,
                )
                yield Static("", id="svc-endpoint", markup=False)
                yield Static(
                    service_manual_note(self.preset_id, english),
                    id="svc-note",
                    markup=False,
                )
                with Grid(id="svc-actions"):
                    yield Button("", id="svc-primary")
                    yield Button("", id="svc-auth")
                    yield Button(
                        _label(self.language, "Проверить", "Verify"),
                        id="svc-verify",
                    )
                    yield Button(
                        _label(self.language, "Ещё", "More"),
                        id="svc-more",
                    )
                    yield Button(
                        _label(self.language, "Диагностика", "Diagnostics"),
                        id="svc-diagnostics",
                    )
                    yield Button("", id="svc-lifecycle")
                    yield Button(_label(self.language, "Остановить", "Stop"), id="svc-stop")
                    yield Button(_label(self.language, "Удалить", "Delete"), id="svc-delete", variant="error")
                yield Static(self._hint(), id="svc-hint", markup=False)

        def _hint(self) -> str:
            # Diagnostic/OAuth/stop shortcuts still exist for experienced users,
            # but the normal screen no longer teaches a seven-key control panel.
            return _label(
                self.language,
                "Tab · Enter/Space · B — Bypass · F1 — помощь · Esc — назад",
                "Tab · Enter/Space · B — Bypass · F1 — help · Esc — back",
            )

        def on_mount(self) -> None:
            # Paint/focus immediately; discovery happens in a worker so a slow
            # process/keyring probe can never delay the first keyboard event.
            self.refresh_view(force=True)
            self._write()
            with contextlib.suppress(Exception):
                self.query_one("#svc-primary", Button).focus()
            # While the user is in ChatGPT saving the MCP app, keep watching the
            # evidence file. The timer itself is intentionally tiny: it only
            # schedules background work and returns to Textual immediately.
            self._poll_timer = self.set_interval(2.0, self._poll_connection_status)

        def on_unmount(self) -> None:
            # A dismissed modal must be dismissible *now*. Stop future polls and
            # invalidate any worker already in flight; its eventual callback is
            # ignored instead of touching a dead screen or delaying navigation.
            self._refresh_epoch += 1
            self._refreshing = False
            timer = self._poll_timer
            self._poll_timer = None
            if timer is not None:
                with contextlib.suppress(Exception):
                    timer.stop()

        def _poll_connection_status(self) -> None:
            if self._checking or self._refreshing:
                return
            typed = self._typed_status
            if typed is not None and typed.overall == OverallStatus.FULLY_VERIFIED:
                return
            self.refresh_view()

        def on_resize(self, _event: Any = None) -> None:
            with contextlib.suppress(Exception):
                self.query_one("#svc-hint", Static).update(self._hint())

        def refresh_view(self, *, force: bool = False) -> None:
            """Refresh live state without ever blocking the Textual event loop.

            Runtime/process/keyring discovery is performed on a worker thread.
            ``force`` supersedes an older in-flight snapshot (used after an
            explicit start/stop); stale callbacks are fenced by ``_refresh_epoch``.
            """

            if self._refreshing and not force:
                return
            self._refresh_epoch += 1
            epoch = self._refresh_epoch
            self._refreshing = True

            def execute() -> None:
                try:
                    state = self._state()
                    endpoint = str(getattr(state, "endpoint", "") or "")
                    typed = self._compute_typed_status(state)
                except Exception:
                    state = None
                    endpoint = ""
                    typed = None
                try:
                    self.app.call_from_thread(
                        self._refresh_view_done,
                        epoch,
                        state,
                        endpoint,
                        typed,
                    )
                except Exception:
                    # The screen may have been dismissed while Windows/keyring
                    # I/O was still finishing. Dismissal wins; no late UI write.
                    return

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="svc-live-refresh",
            )

        def _refresh_view_done(
            self,
            epoch: int,
            state: Optional[Any],
            endpoint: str,
            typed: Optional[ConnectionLiveStatus],
        ) -> None:
            if epoch != self._refresh_epoch or not self.is_mounted:
                return
            self._refreshing = False
            self._has_snapshot = True
            self._last_state = state
            self._endpoint = endpoint
            self._typed_status = typed
            if not self._checking:
                self._status = self._status_from_typed(typed)
            self._write()

        def _compute_typed_status(self, state: Any) -> Optional[ConnectionLiveStatus]:
            """Compute the typed live status from the current state.

            Works for both registry-backed connections and discovered saved
            profiles. For discovered profiles, the raw profile_data dict is
            extracted from ``_DiscoveredBridgeState._data`` and passed to
            :func:`compute_live_status`.
            """
            if state is None:
                return None
            # Extract the raw profile_data dict for compute_live_status.
            raw_data = getattr(state, "_data", None)
            if isinstance(raw_data, dict):
                profile_data = raw_data
            else:
                # Registry-backed connection: synthesize a minimal dict from
                # the connection state's runtime fields.
                runtime = getattr(state, "runtime", {}) or {}
                target = getattr(state, "target", None)
                profile_data = {
                    "saved_profile": str(getattr(target, "name", "") or ""),
                    "session_id": str(runtime.get("session_id") or ""),
                    "public_url": str(runtime.get("public_endpoint") or ""),
                    "bridge_pid": runtime.get("bridge_pid"),
                    "tunnel_pid": runtime.get("tunnel_pid"),
                }
            try:
                return compute_live_status(profile_data, preset_id=self.preset_id)
            except Exception:
                return None

        def _status_from_typed(self, typed: Optional[ConnectionLiveStatus]) -> str:
            """Map the typed overall status to the legacy SERVICE_* constants.

            This keeps the existing status class logic (status-ok / status-warn /
            status-error) working without rewriting every caller.
            """
            if typed is None:
                return SERVICE_NOT_CONFIGURED
            overall = typed.overall
            if overall == OverallStatus.FULLY_VERIFIED:
                return SERVICE_WORKING
            if overall in (
                OverallStatus.READY_FOR_CHATGPT_SETUP,
                OverallStatus.WAITING_FOR_CHATGPT,
                OverallStatus.CONNECTED_UNVERIFIED,
            ):
                return SERVICE_BRIDGE_ONLY
            if overall == OverallStatus.STOPPED_READY_TO_RESTART:
                return SERVICE_AWAITING
            if overall == OverallStatus.NOT_CONFIGURED:
                return SERVICE_NOT_CONFIGURED
            if overall in (OverallStatus.STARTING, OverallStatus.DEGRADED, OverallStatus.ERROR):
                return SERVICE_ERROR
            return SERVICE_NOT_CONFIGURED

        def _summary_status_text(self, typed: Optional[ConnectionLiveStatus]) -> str:
            """One actionable sentence for the normal connection screen.

            The eight-field typed status remains available through Diagnostics;
            the primary screen answers only the user's next question: is KaroX
            ready, and what do I do now?
            """
            name = service_display_name(self.preset_id)
            if typed is None or typed.overall == OverallStatus.NOT_CONFIGURED:
                return _label(
                    self.language,
                    "KaroX для этого сервиса ещё не запущен. Нажмите «Запустить KaroX» — остальное подготовится автоматически.",
                    "KaroX is not running for this service yet. Press Start KaroX and the rest will be prepared automatically.",
                )
            overall = typed.overall
            if overall == OverallStatus.STOPPED_READY_TO_RESTART:
                return _label(
                    self.language,
                    "Подключение сохранено. Нажмите «Восстановить», чтобы снова сделать его доступным.",
                    "The connection is saved. Press Repair to make it available again.",
                )
            if overall == OverallStatus.STARTING:
                return _label(self.language, "KaroX запускается…", "KaroX is starting…")
            if overall == OverallStatus.READY_FOR_CHATGPT_SETUP:
                return _label(
                    self.language,
                    f"KaroX готов. Скопируйте URL ниже и добавьте его в {name}.",
                    f"KaroX is ready. Copy the URL below and add it in {name}.",
                )
            if overall == OverallStatus.WAITING_FOR_CHATGPT:
                return _label(
                    self.language,
                    f"{name} уже начал подключение. Завершите сохранение там — KaroX обновит статус сам.",
                    f"{name} has started connecting. Finish saving it there and KaroX will update automatically.",
                )
            if overall == OverallStatus.CONNECTED_UNVERIFIED:
                return _label(
                    self.language,
                    f"{name} подключён. Осталось проверить, что инструменты KaroX отвечают.",
                    f"{name} is connected. One check remains: confirm the KaroX tools respond.",
                )
            if overall == OverallStatus.FULLY_VERIFIED:
                return _label(
                    self.language,
                    f"✓ Готово: {name} подключён, инструменты KaroX работают.",
                    f"✓ Ready: {name} is connected and the KaroX tools work.",
                )
            return _label(
                self.language,
                "Подключение требует внимания. Нажмите «Исправить» или откройте «Дополнительно».",
                "The connection needs attention. Press Repair or open Advanced.",
            )

        def _typed_status_text(self, typed: Optional[ConnectionLiveStatus]) -> str:
            """Format the typed status as 8 bilingual lines for diagnostics.

            Never includes secrets: only status strings, PIDs, fingerprints,
            and tool counts.
            """
            en = self._english()
            if typed is None:
                return _label(self.language, "Не настроено", "Not configured")

            def _field(label_ru: str, label_en: str, value: str) -> str:
                return f"{_label(self.language, label_ru, label_en)} {value}"

            lines: List[str] = [
                _field("Конфигурация:", "Configuration:", self._status_word_typed(typed.configuration, en)),
                _field("Credential:", "Credential:", self._status_word_typed(typed.credential, en)),
                _field("Bridge:", "Bridge:", self._status_word_typed(typed.bridge, en)),
                _field("Tunnel:", "Tunnel:", self._status_word_typed(typed.tunnel, en)),
                _field("OAuth:", "OAuth:", self._status_word_typed(typed.oauth, en)),
                _field("ChatGPT клиент:", "ChatGPT client:", self._status_word_typed(typed.chatgpt_client, en)),
                _field("Проверка инструментов:", "Tool verification:", self._status_word_typed(typed.tool_verification, en)),
                _field("Общее:", "Overall:", self._status_word_typed(typed.overall, en)),
            ]
            return "\n".join(lines)

        @staticmethod
        def _status_word_typed(value: str, english: bool) -> str:
            """Translate a typed status constant to a bilingual display word."""
            _TABLE: Dict[str, Tuple[str, str]] = {
                # Configuration
                ConfigurationStatus.MISSING: ("отсутствует", "missing"),
                ConfigurationStatus.SAVED: ("сохранён", "saved"),
                ConfigurationStatus.INVALID: ("неверный", "invalid"),
                # Credential
                CredentialStatus.MISSING: ("отсутствует", "missing"),
                CredentialStatus.AVAILABLE: ("доступен", "available"),
                CredentialStatus.REVOKED: ("отозван", "revoked"),
                CredentialStatus.ERROR: ("ошибка", "error"),
                # Bridge
                BridgeStatus.STOPPED: ("остановлен", "stopped"),
                BridgeStatus.STARTING: ("запуск", "starting"),
                BridgeStatus.RUNNING: ("работает", "running"),
                BridgeStatus.STALE: ("устарел", "stale"),
                BridgeStatus.ERROR: ("ошибка", "error"),
                # Tunnel
                TunnelStatus.STOPPED: ("остановлен", "stopped"),
                TunnelStatus.STARTING: ("запуск", "starting"),
                TunnelStatus.PUBLIC_URL_READY: ("URL готов", "public URL ready"),
                TunnelStatus.ROUTE_CONFLICT: ("конфликт маршрута", "route conflict"),
                TunnelStatus.ERROR: ("ошибка", "error"),
                # OAuth
                OAuthStatus.UNAVAILABLE: ("недоступен (bridge остановлен)", "unavailable (bridge stopped)"),
                OAuthStatus.READY: ("готов", "ready"),
                OAuthStatus.AUTHORIZATION_PENDING: ("ожидает авторизации", "authorization pending"),
                OAuthStatus.AUTHORIZED: ("авторизован", "authorized"),
                OAuthStatus.REVOKED: ("отозван", "revoked"),
                OAuthStatus.ERROR: ("ошибка", "error"),
                # ChatGPT client
                ChatGPTClientStatus.NOT_SEEN: ("не подключался", "not seen"),
                ChatGPTClientStatus.INITIALIZE_RECEIVED: ("initialize получен", "initialize received"),
                ChatGPTClientStatus.TOOLS_LIST_RECEIVED: ("tools/list получен", "tools/list received"),
                ChatGPTClientStatus.DISCONNECTED: ("отключён", "disconnected"),
                ChatGPTClientStatus.CONNECTED: ("подключён", "connected"),
                # Tool verification
                ToolVerificationStatus.NOT_TESTED: ("не проверено", "not tested"),
                ToolVerificationStatus.PASSED: ("пройдено", "passed"),
                ToolVerificationStatus.FAILED: ("не пройдено", "failed"),
                # Overall
                OverallStatus.NOT_CONFIGURED: ("не настроено", "not configured"),
                OverallStatus.STOPPED_READY_TO_RESTART: ("остановлен, готов к перезапуску", "stopped, ready to restart"),
                OverallStatus.STARTING: ("запуск", "starting"),
                OverallStatus.READY_FOR_CHATGPT_SETUP: ("готов к настройке ChatGPT", "ready for ChatGPT setup"),
                OverallStatus.WAITING_FOR_CHATGPT: ("ожидает ChatGPT", "waiting for ChatGPT"),
                OverallStatus.CONNECTED_UNVERIFIED: ("подключён, не верифицирован", "connected, unverified"),
                OverallStatus.FULLY_VERIFIED: ("полностью проверен", "fully verified"),
                OverallStatus.DEGRADED: ("деградирован", "degraded"),
                OverallStatus.ERROR: ("ошибка", "error"),
            }
            pair = _TABLE.get(value, (value, value))
            return pair[1] if english else pair[0]

        def _write(self) -> None:
            english = self._english()
            with contextlib.suppress(Exception):
                steps = self.query_one("#svc-steps", Static)
                steps.update(self._steps_text())

                project = self.query_one("#svc-project", Static)
                project.update(self._project_text())

                if bypass_supported(self.preset_id):
                    full_state = self._bypass_state()
                    full_switch = self.query_one("#svc-full-access", Switch)
                    full_label = self.query_one("#svc-full-access-label", Label)
                    if full_state is not None and full_switch.value != full_state:
                        self._ignore_full_access_event = full_state
                        full_switch.value = full_state
                    full_switch.disabled = (
                        self._full_access_busy
                        or self._checking
                        or not self._has_snapshot
                        or full_state is None
                    )
                    full_label.update(
                        _label(
                            self.language,
                            "Bypass режим",
                            "Bypass mode",
                        )
                        + (
                            _label(self.language, " — ВКЛ", " — ON")
                            if full_state
                            else _label(self.language, " — ВЫКЛ", " — OFF")
                        )
                    )

                endpoint_label = self.query_one("#svc-endpoint-label", Static)
                endpoint = self.query_one("#svc-endpoint", Static)
                visible = "block" if self._endpoint else "none"
                endpoint_label.styles.display = visible
                endpoint.styles.display = visible
                endpoint.update(self._endpoint)

                note = self.query_one("#svc-note", Static)
                note_text = (
                    service_manual_note(self.preset_id, english)
                    if self._endpoint
                    and not (
                        self._typed_status is not None
                        and self._typed_status.overall == OverallStatus.FULLY_VERIFIED
                    )
                    else ""
                )
                note.update(note_text)
                note.styles.display = "block" if note_text else "none"

                primary = self.query_one("#svc-primary", Button)
                primary.label = self._primary_label()
                # The first snapshot is intentionally asynchronous. Until it is
                # known, do not let a fast Enter assume "not configured" and
                # create a duplicate saved bridge.
                primary.disabled = self._checking or not self._has_snapshot

                auth = self.query_one("#svc-auth", Button)
                uses_bearer = self._uses_bearer_auth()
                # The auth action is the primary secret hand-off for BOTH auth
                # schemes. Hiding it for OAuth profiles (the old `display:
                # none`) left the approval password reachable only through a
                # hidden P key — the exact "user must know internal commands"
                # failure this screen exists to prevent.
                auth.label = (
                    _label(self.language, "Скопировать Authorization", "Copy Authorization")
                    if uses_bearer and self.preset_id == "adapt"
                    else _label(self.language, "Скопировать ключ", "Copy key")
                    if uses_bearer
                    else _label(
                        self.language,
                        "Скопировать пароль",
                        "Copy approval password",
                    )
                )
                overall = (
                    self._typed_status.overall
                    if self._typed_status is not None
                    else None
                )
                show_auth = bool(self._endpoint) and self._has_snapshot and (
                    overall is None
                    or overall in {
                        OverallStatus.READY_FOR_CHATGPT_SETUP,
                        OverallStatus.WAITING_FOR_CHATGPT,
                    }
                )
                auth.styles.display = "block" if show_auth else "none"
                auth.disabled = self._checking or not show_auth

                verify = self.query_one("#svc-verify", Button)
                show_verify = (
                    bool(self._endpoint)
                    and self._has_snapshot
                    and overall == OverallStatus.CONNECTED_UNVERIFIED
                )
                verify.styles.display = "block" if show_verify else "none"
                verify.disabled = self._checking or not show_verify

                # Diagnostics/lifecycle/destructive actions are intentionally
                # kept out of the normal screen. They remain mounted so existing
                # shortcuts and tests can address the same actions, while More
                # is the single human-facing entry point for them.
                diagnostics = self.query_one("#svc-diagnostics", Button)
                diagnostics.styles.display = "none"
                diagnostics.disabled = self._checking or not self._has_snapshot

                lifecycle = self.query_one("#svc-lifecycle", Button)
                lifecycle.label = _label(self.language, "Перезапустить", "Restart")
                show_restart = self._show_restart_button()
                lifecycle.styles.display = "none"
                lifecycle.disabled = not show_restart

                stop_button = self.query_one("#svc-stop", Button)
                stop_button.styles.display = "none"
                stop_button.disabled = not show_restart

                delete_button = self.query_one("#svc-delete", Button)
                show_delete = self._has_snapshot and self._last_state is not None
                delete_button.styles.display = "none"
                delete_button.disabled = self._checking or self._deleting or not show_delete

            status = self.query_one("#svc-status", Static)
            if not self._has_snapshot and self._refreshing:
                status.update(
                    _label(
                        self.language,
                        "Проверяем текущее подключение…",
                        "Checking the current connection…",
                    )
                )
            elif self._typed_status is not None:
                status.update(self._summary_status_text(self._typed_status))
            else:
                label = _label(self.language, "Состояние:", "Status:")
                status.update(
                    f"{label} {service_status_words(self._status, english)}"
                )
            status.set_class(service_status_is_proven(self._status), "status-ok")
            status.set_class(self._status == SERVICE_ERROR, "status-error")
            status.set_class(
                self._status
                in {SERVICE_BRIDGE_ONLY, SERVICE_ACTION_REQUIRED, SERVICE_AWAITING},
                "status-warn",
            )

        # --------------------------------------------------------------- actions

        def status_text(self) -> str:
            try:
                return str(self.query_one("#svc-status", Static).render())
            except Exception:
                return ""

        def rendered_text(self) -> str:
            """Everything on screen as one string, for a leak assertion."""

            parts: List[str] = []
            for selector in (
                "#svc-steps",
                "#svc-endpoint",
                "#svc-note",
                "#svc-status",
                "#svc-hint",
            ):
                try:
                    parts.append(str(self.query_one(selector, Static).render()))
                except Exception:
                    continue
            return "\n".join(parts)

        def action_workspace(self) -> None:
            """Open the same project picker as Ctrl+W in the main chat."""
            switcher = getattr(self.app, "action_workspace", None)
            if callable(switcher):
                switcher()

        def action_navigation_help(self) -> None:
            from .tui_onboarding import NavigationHelpScreen
            self.app.push_screen(NavigationHelpScreen(self.language))

        def action_focus_bypass(self) -> None:
            if bypass_supported(self.preset_id):
                with contextlib.suppress(Exception):
                    self.query_one("#svc-full-access", Switch).focus()

        def action_activate_focused(self) -> None:
            """Activate exactly the focused service button with Space."""
            focused = self.app.focused
            # The screen's priority Space binding also intercepts Switch's
            # native key. Dispatch it back rather than swallowing Bypass toggles.
            if isinstance(focused, Switch):
                if not focused.disabled:
                    focused.toggle()
                return
            button_id = getattr(focused, "id", "")
            if button_id == "svc-primary":
                if self._primary_starts_bridge():
                    self.action_start_repair()
                else:
                    self.action_copy_url()
            elif button_id == "svc-auth":
                self.action_copy_static_bearer()
            elif button_id == "svc-verify":
                self.action_verify()
            elif button_id == "svc-more":
                self.action_more()
            elif button_id == "svc-diagnostics":
                self.action_diagnostics()
            elif button_id == "svc-lifecycle":
                self.action_restart_service()
            elif button_id == "svc-stop":
                self.action_stop()
            elif button_id == "svc-delete":
                self.action_delete_profile()

        @on(Button.Pressed)
        def _pressed(self, event: Button.Pressed) -> None:
            button_id = event.button.id or ""
            if button_id == "svc-primary":
                if self._primary_starts_bridge():
                    self.action_start_repair()
                else:
                    self.action_copy_url()
                return
            if button_id == "svc-auth":
                self.action_copy_static_bearer()
                return
            if button_id == "svc-verify":
                self.action_verify()
                return
            if button_id == "svc-more":
                self.action_more()
                return
            if button_id == "svc-diagnostics":
                self.action_diagnostics()
                return
            if button_id == "svc-lifecycle":
                self.action_restart_service()
                return
            if button_id == "svc-stop":
                self.action_stop()
                return
            if button_id == "svc-delete":
                self.action_delete_profile()

        @on(Switch.Changed, "#svc-full-access")
        def _full_access_changed(self, event: Switch.Changed) -> None:
            if not bypass_supported(self.preset_id):
                return
            requested = bool(event.value)
            if self._ignore_full_access_event is not None and requested == self._ignore_full_access_event:
                self._ignore_full_access_event = None
                return
            current = self._bypass_state()
            if current is None:
                self._write()
                return
            if requested == current or self._full_access_busy:
                return
            profile_name = self._saved_profile_name()
            if not profile_name:
                self._write()
                return

            shared = self._bypass_shared_with()
            if shared:
                # The bridge reads its capability profile from the saved
                # profile, so this change is bridge-wide. Say so, and only
                # apply it when the user explicitly accepts that effect.
                names = ", ".join(shared)
                self.app.push_screen(
                    _ConfirmScreen(
                        _label(
                            self.language,
                            "Изменить Bypass для общего моста?",
                            "Change Bypass on a shared bridge?",
                        ),
                        _label(
                            self.language,
                            f"Этот мост также обслуживает: {names}. "
                            "Режим хранится в общем сохранённом профиле, "
                            "поэтому изменение затронет все подключения этого "
                            "моста. URL, credential и идентичность моста не "
                            "меняются.",
                            f"This bridge also serves: {names}. The mode lives "
                            "on the shared saved profile, so the change applies "
                            "to every connection using this bridge. Its URL, "
                            "credential, and identity stay unchanged.",
                        ),
                        language=self.language,
                    ),
                    lambda confirmed: self._bypass_confirmed(
                        confirmed, requested=requested, profile_name=profile_name
                    ),
                )
                return
            self._apply_bypass(requested, profile_name)

        def _bypass_confirmed(
            self,
            confirmed: Optional[bool],
            *,
            requested: bool,
            profile_name: str,
        ) -> None:
            if not confirmed:
                # Put the switch back: nothing was written.
                self._write()
                return
            self._apply_bypass(requested, profile_name)

        def _apply_bypass(self, requested: bool, profile_name: str) -> None:
            if self._full_access_busy:
                return
            if saved_bridge_target(self.preset_id) is None:
                self._apply_record_bypass(requested)
                return
            self._full_access_busy = True
            self._write()

            def execute() -> None:
                previous: Optional[Any] = None
                store: Optional[Any] = None
                try:
                    from .web_bridge_launcher import restart_saved_bridge
                    from .web_bridge_profiles import WebBridgeProfileStore

                    from .access_mode import build_saved_profile_bypass

                    store = WebBridgeProfileStore()
                    previous = store.get(profile_name)
                    updated = build_saved_profile_bypass(previous, requested)
                    store.put(updated)
                    result = restart_saved_bridge(
                        profile_name,
                        allow_legacy_migration=True,
                    )
                    if result.get("action") == "error":
                        raise ConnectionLaunchError(
                            str(result.get("error") or "bridge restart failed")
                        )
                    error: Optional[str] = None
                except Exception as exc:
                    error = str(redact(str(exc)))[:240]
                    if previous is not None and store is not None:
                        with contextlib.suppress(Exception):
                            store.put(previous)
                        with contextlib.suppress(Exception):
                            from .web_bridge_launcher import restart_saved_bridge

                            restart_saved_bridge(
                                profile_name,
                                allow_legacy_migration=True,
                            )
                with contextlib.suppress(Exception):
                    self.app.call_from_thread(
                        self._full_access_done,
                        requested,
                        error,
                    )

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="svc-full-access",
            )

        def _apply_record_bypass(self, requested: bool) -> None:
            """Persist the mode on a record-backed connection.

            Nothing is started or stopped: the record is what the next
            Start/Repair reads, so a live bridge keeps serving the URL its
            user already pasted into the service.
            """

            connection_id = self._bypass_connection_id()
            if not connection_id:
                self._write()
                return
            try:
                self._controller.set_bypass(connection_id, requested)
                error: Optional[str] = None
            except Exception as exc:
                error = str(redact(str(exc)))[:240]
            self._full_access_done(requested, error, restarted=False)

        def _full_access_done(
            self,
            enabled: bool,
            error: Optional[str],
            *,
            restarted: bool = True,
        ) -> None:
            if not self.is_mounted:
                return
            self._full_access_busy = False
            if error:
                self.app.notify(
                    _label(
                        self.language,
                        f"Не удалось переключить Bypass режим: {error}",
                        f"Could not switch Bypass mode: {error}",
                    ),
                    severity="error",
                )
            else:
                # A record-backed connection is not restarted here, so say
                # when the change takes effect instead of implying it already
                # has.
                suffix = (
                    ""
                    if restarted
                    else _label(
                        self.language,
                        " — применится при следующем запуске",
                        " — applies on next start",
                    )
                )
                self.app.notify(
                    _label(
                        self.language,
                        "Bypass режим включён"
                        if enabled
                        else "Bypass режим выключен",
                        "Bypass mode enabled" if enabled else "Bypass mode disabled",
                    )
                    + suffix,
                    severity="information",
                )
            self.refresh_view(force=True)

        def action_copy_url(self) -> None:
            if not self._endpoint:
                return
            copy_text(self._endpoint)
            self.app.notify(_t(self.language, "copied"))

        def action_copy_static_bearer(self) -> None:
            """Copy bearer auth through the same guarded credential service."""
            self.action_copy_approval_password()

        def action_copy_approval_password(self) -> None:
            """Copy the OAuth approval password to clipboard without printing it.

            Calls the canonical :func:`karox.web_bridge_launcher.copy_oauth_approval_password`
            service — the same typed secret path the CLI uses
            (``karox bridge oauth approval-password --saved <profile> --copy --quiet``).
            The secret is never printed, logged, or returned to UI state — only
            the fingerprint and clipboard status are shown.
            """
            state = self._last_state if self._has_snapshot else None
            if state is None:
                self.app.notify(
                    _label(
                        self.language,
                        "Нет сохранённого подключения — сначала запустите bridge",
                        "No saved connection — start the bridge first",
                    ),
                    severity="warning",
                )
                return
            # The approval password and static bearer both resolve from the OS
            # keyring through the saved profile name alone; a live session is
            # not required, so a missing runtime session_id must not block the
            # copy (it blocks re-auth later, not the credential itself).
            # Resolve the saved profile name from the connection target.
            target = getattr(state, "target", None)
            profile_name = str(getattr(target, "name", "") or "")
            if not profile_name:
                self.app.notify(
                    _label(
                        self.language,
                        "Не удалось определить сохранённый профиль",
                        "Could not determine saved profile name",
                    ),
                    severity="warning",
                )
                return
            uses_bearer = self._uses_bearer_auth()

            def execute() -> None:
                try:
                    from .web_bridge_launcher import (
                        copy_oauth_approval_password,
                        copy_static_bearer_credential_for_saved_bridge,
                        copy_static_bearer_token_for_saved_bridge,
                    )

                    if uses_bearer and self.preset_id == "notion":
                        # Notion already supplies the ``Bearer`` prefix in its
                        # dedicated Prefix field, so its Token input needs only
                        # the raw secret. Copying an Authorization value here
                        # would produce ``Bearer Bearer <secret>``.
                        result = copy_static_bearer_token_for_saved_bridge(profile_name)
                    elif uses_bearer:
                        result = copy_static_bearer_credential_for_saved_bridge(profile_name)
                    else:
                        result = copy_oauth_approval_password(profile_name)
                    self.app.call_from_thread(self._copy_approval_done, result, None)
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        self.app.call_from_thread(
                            self._copy_approval_done,
                            None,
                            str(exc)[:200],
                        )

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="svc-copy-oauth",
            )

        def _copy_approval_done(self, result: Optional[Any], error: Optional[str]) -> None:
            if not self.is_mounted:
                return
            if error or result is None or (result.error and not result.copied):
                self.app.notify(
                    _label(
                        self.language,
                        "Credential недоступен — проверьте подключение или создайте новый",
                        "Credential unavailable — check the connection or create a new one",
                    ),
                    severity="warning",
                )
                return
            if result.copied:
                if getattr(result, "purpose", "") == "static_bearer":
                    message = _label(
                        self.language,
                        f"Authorization-ключ скопирован (fingerprint: {result.fingerprint[:16]}…, авто-очистка через {result.auto_clear_seconds}s)",
                        f"Authorization key copied (fingerprint: {result.fingerprint[:16]}…, auto-clear in {result.auto_clear_seconds}s)",
                    )
                else:
                    message = _label(
                        self.language,
                        f"Пароль подтверждения скопирован (fingerprint: {result.fingerprint[:16]}…, авто-очистка через {result.auto_clear_seconds}s)",
                        f"Approval password copied (fingerprint: {result.fingerprint[:16]}…, auto-clear in {result.auto_clear_seconds}s)",
                    )
                self.app.notify(message, severity="information")
            else:
                message = _label(
                    self.language,
                    f"Clipboard недоступен (fingerprint: {result.fingerprint[:16]}…)",
                    f"Clipboard unavailable (fingerprint: {result.fingerprint[:16]}…)",
                )
                self.app.notify(message, severity="warning")

        def action_restart_service(self) -> None:
            """Restart the current service through its canonical managed path."""
            state = self._last_state if self._has_snapshot else None
            target = getattr(state, "target", None)
            if state is None or target is None:
                return
            connection_id = str(getattr(target, "connection_id", "") or "")
            profile_name = str(getattr(target, "name", "") or "")
            self._status = SERVICE_CHECKING
            self._write()

            def execute() -> None:
                try:
                    if not connection_id or connection_id.startswith("discovered-"):
                        if not profile_name:
                            raise ConnectionLaunchError("saved bridge profile is unavailable")
                        from .web_bridge_launcher import restart_saved_bridge

                        result = restart_saved_bridge(
                            profile_name,
                            allow_legacy_migration=True,
                        )
                        if result.get("action") == "error":
                            error = str(result.get("error") or "bridge restart failed")[:200]
                        else:
                            error = None
                    else:
                        launch = self._controller.restart(connection_id)
                        error = None if launch.success else "managed connection restart failed"
                except Exception as exc:
                    error = str(redact(str(exc)))[:200]
                with contextlib.suppress(Exception):
                    self.app.call_from_thread(self._restart_service_done, error)

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="svc-restart",
            )

        def _restart_service_done(self, error: Optional[str]) -> None:
            if not self.is_mounted:
                return
            if error:
                self._status = SERVICE_ERROR
                self.app.notify(
                    _label(
                        self.language,
                        f"Ошибка перезапуска: {error}",
                        f"Restart error: {error}",
                    ),
                    severity="error",
                )
            else:
                self.app.notify(
                    _label(
                        self.language,
                        "Подключение перезапущено",
                        "Connection restarted",
                    ),
                    severity="information",
                )
            self.refresh_view(force=True)

        def action_start_repair(self) -> None:
            """Start or repair the bridge for this connection.

            Launches the production persistent bridge for the discovered saved
            profile. Never rotates credentials, resets OAuth, or changes the
            ClickUp connector. The bridge is launched as a detached owned
            process that survives TUI close.
            """
            state = self._last_state if self._has_snapshot else None
            if state is None:
                opener = getattr(self.app, "open_service_bridge_setup", None)
                if callable(opener):
                    opener(self.preset_id, lambda: self.refresh_view(force=True))
                    return
                # Fail-soft for non-KaroX host apps used by embedders/tests. The
                # normal KaroXApp always provides the production bridge wizard.
                self.app.notify(
                    _label(
                        self.language,
                        "Не удалось открыть мастер подключения",
                        "Could not open the connection wizard",
                    ),
                    severity="warning",
                )
                return
            target = getattr(state, "target", None)
            profile_name = str(getattr(target, "name", "") or "")
            if not profile_name:
                self.app.notify(
                    _label(self.language, "Профиль не найден", "Profile not found"),
                    severity="warning",
                )
                return
            self._status = SERVICE_CHECKING
            self._write()
            # Whether the bridge was already serving before this Start/Repair:
            # a no-op repair on a live connection must not toast "Bridge
            # started" over a status line that already says it is ready.
            typed = self._typed_status
            was_serving = (
                typed is not None
                and typed.overall
                in (
                    OverallStatus.READY_FOR_CHATGPT_SETUP,
                    OverallStatus.WAITING_FOR_CHATGPT,
                    OverallStatus.CONNECTED_UNVERIFIED,
                    OverallStatus.FULLY_VERIFIED,
                )
            )

            def execute() -> None:
                try:
                    from .web_bridge_launcher import start_saved_bridge
                    result = start_saved_bridge(profile_name)
                    self.app.call_from_thread(
                        self._start_repair_done, result, was_serving
                    )
                except Exception as exc:
                    self.app.call_from_thread(
                        self._start_repair_done,
                        {"error": str(exc)[:200]},
                        was_serving,
                    )

            self.app.run_worker(execute, thread=True, exclusive=True, group="svc-start")

        def _start_repair_done(self, result: dict, was_serving: bool = False) -> None:
            if not self.is_mounted:
                return
            if result.get("error"):
                self._status = SERVICE_ERROR
                self.app.notify(
                    _label(
                        self.language,
                        f"Ошибка запуска: {result['error']}",
                        f"Start error: {result['error']}",
                    ),
                    severity="error",
                )
            elif not was_serving and result.get("action") == "started":
                # Only a launch that truly started a process gets a toast. A
                # `reused` result means the bridge was already running: the
                # status line below already says so, and a "Bridge started"
                # toast on top of it was both a duplicate and a lie.
                self.app.notify(
                    _label(
                        self.language,
                        "Bridge запущен",
                        "Bridge started",
                    ),
                    severity="information",
                )
            self.refresh_view(force=True)

        def action_copy_test_prompt(self) -> None:
            """Copy a safe read-only test prompt for ChatGPT to the clipboard."""
            prompt = (
                "Проверь подключение KaroX. Вызови только безопасный read-only "
                "инструмент karox.runtime.status. Сообщи repository, access profile "
                "и количество доступных инструментов. Ничего не изменяй."
            )
            copy_text(prompt)
            self.app.notify(_t(self.language, "copied"))

        def action_diagnostics(self) -> None:
            """Open readable recovery health with technical detail behind it."""
            state = self._last_state if self._has_snapshot else None
            runtime = getattr(state, "runtime", {}) or {}
            endpoint = getattr(state, "endpoint", "") or self._endpoint
            summary: List[str] = []
            technical: List[str] = []
            if self._typed_status is not None:
                summary.append(
                    _label(self.language, "Подключение: ", "Connection: ")
                    + self._status_word_typed(
                        self._typed_status.overall,
                        self._english(),
                    )
                )

            profile_name = self._saved_profile_name()
            if profile_name:
                try:
                    from .saved_bridge_supervisor import saved_bridge_supervisor_status

                    supervisor = saved_bridge_supervisor_status(profile_name)
                    desired = bool(supervisor.get("desired_running"))
                    alive = bool(supervisor.get("supervisor_alive"))
                    heartbeat = bool(supervisor.get("supervisor_heartbeat_fresh"))
                    if desired and alive and heartbeat:
                        recovery = _label(self.language, "активно", "active")
                    elif not desired:
                        recovery = _label(self.language, "остановлено", "stopped")
                    else:
                        recovery = _label(self.language, "требует внимания", "needs attention")
                    summary.append(
                        _label(self.language, "Автовосстановление: ", "Auto-recovery: ")
                        + recovery
                    )
                    summary.append(
                        _label(self.language, "Автоперезапусков: ", "Automatic restarts: ")
                        + str(int(supervisor.get("restart_count") or 0))
                    )
                    technical.append(
                        f"Supervisor heartbeat age: {supervisor.get('supervisor_heartbeat_age_seconds', 'n/a')}"
                    )
                except Exception:
                    summary.append(
                        _label(
                            self.language,
                            "Автовосстановление: статус недоступен",
                            "Auto-recovery: status unavailable",
                        )
                    )
                try:
                    from .web_bridge_profiles import WebBridgeProfileStore

                    profile = WebBridgeProfileStore().get(profile_name)
                    if profile.target_profile in {"chatgpt-web", "claude-web", "hyperagent-web"}:
                        browser_ready = (
                            profile.browser_external_https
                            and profile.browser_headed
                            and profile.browser_user_takeover
                        )
                        summary.append(
                            _label(self.language, "Managed browser: ", "Managed browser: ")
                            + _label(
                                self.language,
                                "готов к HTTPS + takeover" if browser_ready else "ограниченный режим",
                                "HTTPS + takeover ready" if browser_ready else "limited mode",
                            )
                        )
                        summary.append(
                            _label(self.language, "Тестовых аккаунтов: ", "Test accounts: ")
                            + str(len(profile.browser_credential_refs))
                        )
                        technical.append(
                            "Browser policy: "
                            f"external_https={profile.browser_external_https}, "
                            f"headed={profile.browser_headed}, "
                            f"takeover={profile.browser_user_takeover}, "
                            f"network_inspection={profile.browser_network_inspection}"
                        )
                except Exception:
                    pass

            technical.extend(
                [
                    f"Endpoint: {endpoint or 'none'}",
                    f"State: {runtime.get('state', 'unknown')}",
                    f"Bridge PID: {runtime.get('bridge_pid', 'none')}",
                    f"Tunnel PID: {runtime.get('tunnel_pid', 'none')}",
                    f"Session ID: {runtime.get('session_id', 'none')}",
                    f"Saved profile: {profile_name or 'none'}",
                ]
            )
            self.app.push_screen(
                ServiceDiagnosticsScreen(
                    self.language,
                    title=service_display_name(self.preset_id),
                    summary_lines=summary or (
                        _label(self.language, "Статус пока недоступен.", "Status is not available yet."),
                    ),
                    technical_lines=technical,
                )
            )

        def action_delete_profile(self) -> None:
            """Delete this saved connection through its ownership-safe service."""
            if self._deleting or not self._has_snapshot or self._last_state is None:
                return
            state = self._last_state
            target = getattr(state, "target", None)
            connection_id = str(getattr(target, "connection_id", "") or "")
            profile_name = self._saved_profile_name()
            # Only a preset that owns its saved bridge may delete the profile;
            # an alias user (Adapt on the ChatGPT bridge) can delete nothing
            # but its own connection record. See owns_saved_bridge().
            hosted_saved = owns_saved_bridge(self.preset_id) and bool(profile_name)
            if not hosted_saved and (not connection_id or connection_id.startswith("discovered-")):
                self.app.notify(
                    _label(self.language, "Подключение нельзя удалить безопасно.", "The connection cannot be deleted safely."),
                    severity="warning",
                )
                return
            display = service_display_name(self.preset_id)
            body = (
                _label(
                    self.language,
                    f"Удалить «{display}»? KaroX остановит только принадлежащий ему bridge, удалит сохранённый профиль и его локальные credentials. Managed Chrome этого профиля тоже будет закрыт. Личный Chrome и другие подключения не затрагиваются. Отменить это действие нельзя.",
                    f"Delete “{display}”? KaroX will stop only its owned bridge, remove the saved profile and its local credentials, and close that profile's managed Chrome. Personal Chrome and other connections are untouched. This cannot be undone.",
                )
                if hosted_saved
                else _label(
                    self.language,
                    f"Удалить «{display}» и его сохранённый credential? Активный owned runtime будет остановлен. Отменить это действие нельзя.",
                    f"Delete “{display}” and its stored credential? An active owned runtime will be stopped. This cannot be undone.",
                )
            )
            self.app.push_screen(
                _ConfirmScreen(
                    _label(self.language, "Удалить подключение?", "Delete connection?"),
                    body,
                    language=self.language,
                ),
                lambda confirmed: self._delete_profile_confirmed(
                    confirmed,
                    profile_name=profile_name,
                    connection_id=connection_id,
                    hosted_saved=hosted_saved,
                ),
            )

        def _delete_profile_confirmed(
            self,
            confirmed: Optional[bool],
            *,
            profile_name: str,
            connection_id: str,
            hosted_saved: bool,
        ) -> None:
            if not confirmed or self._deleting:
                return
            self._deleting = True
            self._write()

            def execute() -> None:
                try:
                    if hosted_saved:
                        from .web_bridge_launcher import delete_saved_bridge_profile

                        result = delete_saved_bridge_profile(
                            profile_name,
                            allow_legacy_migration=True,
                        )
                        error = (
                            str(result.get("error") or "saved profile delete failed")[:200]
                            if result.get("action") == "error"
                            else None
                        )
                        cleanup_pending = bool(result.get("cleanup_pending"))
                    else:
                        self._controller.remove(connection_id)
                        error = None
                        cleanup_pending = False
                except Exception as exc:
                    error = str(redact(str(exc)))[:200]
                    cleanup_pending = False
                with contextlib.suppress(Exception):
                    self.app.call_from_thread(
                        self._delete_profile_done,
                        error,
                        cleanup_pending,
                    )

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="svc-delete-profile",
            )

        def _delete_profile_done(
            self,
            error: Optional[str],
            cleanup_pending: bool,
        ) -> None:
            if not self.is_mounted:
                return
            self._deleting = False
            if error:
                self.app.notify(
                    _label(
                        self.language,
                        f"Не удалось удалить подключение: {error}",
                        f"Could not delete connection: {error}",
                    ),
                    severity="error",
                )
                self.refresh_view(force=True)
                return
            self.connection_id = None
            self._last_state = None
            self._typed_status = None
            self._has_snapshot = False
            self._endpoint = ""
            self.app.notify(
                _label(
                    self.language,
                    "Подключение удалено; часть локального cleanup требует Doctor."
                    if cleanup_pending
                    else "Подключение удалено",
                    "Connection deleted; some local cleanup requires Doctor."
                    if cleanup_pending
                    else "Connection deleted",
                ),
                severity="warning" if cleanup_pending else "information",
            )
            self.refresh_view(force=True)

        def action_stop(self) -> None:
            """Stop the owned bridge/tunnel for this connection.

            Stop is not Revoke: this shuts down the bridge and tunnel processes
            but does not delete credentials or OAuth authorization.

            Works for both registry-backed connections and discovered saved
            profiles: a ChatGPT Web bridge found via saved-profile discovery
            (synthetic ``discovered-*`` connection_id) is stopped through the
            canonical ``stop_saved_bridge`` service, which uses the same
            ownership check as ``karox bridge stop --saved NAME``.
            """
            state = self._last_state if self._has_snapshot else None
            if state is None:
                return
            runtime = getattr(state, "runtime", {}) or {}
            connection_id = str(runtime.get("connection_id") or "")

            # Discovered saved profiles have a synthetic connection_id and no
            # registry record. Stop them through the canonical saved-bridge
            # service, which checks ownership and never touches credentials,
            # OAuth, ClickUp, or personal Chrome.
            if not connection_id or connection_id.startswith("discovered-"):
                target = getattr(state, "target", None)
                profile_name = str(getattr(target, "name", "") or "")
                if not profile_name:
                    self.app.notify(
                        _label(
                            self.language,
                            "Нет активного bridge для остановки",
                            "No active bridge to stop",
                        ),
                        severity="warning",
                    )
                    return
                self._status = SERVICE_CHECKING
                self._write()

                def execute_discovered() -> None:
                    try:
                        from .web_bridge_launcher import stop_saved_bridge

                        result = stop_saved_bridge(
                            profile_name,
                            allow_legacy_migration=True,
                        )
                    except Exception as exc:
                        result = {"action": "error", "error": str(exc)[:200]}
                    self.app.call_from_thread(self._stop_done, result)

                self.app.run_worker(
                    execute_discovered, thread=True, exclusive=True, group="svc-stop"
                )
                return

            self._status = SERVICE_CHECKING
            self._write()

            def execute_registry() -> None:
                try:
                    self._controller.stop(connection_id)
                    result = {"action": "stopped"}
                except Exception as exc:
                    result = {"action": "error", "error": str(exc)[:200]}
                try:
                    self.app.call_from_thread(self._stop_done, result)
                except Exception:
                    return

            self.app.run_worker(
                execute_registry,
                thread=True,
                exclusive=True,
                group="svc-stop",
            )

        def _stop_done(self, result: dict) -> None:
            """Callback for the discovered-profile stop worker."""
            if not self.is_mounted:
                return
            action = str(result.get("action") or "")
            if action == "stopped":
                self.app.notify(
                    _label(self.language, "Bridge остановлен", "Bridge stopped"),
                    severity="information",
                )
            elif action == "no_action":
                verdict = str(result.get("verdict") or "")
                self.app.notify(
                    _label(
                        self.language,
                        f"Нет активного bridge ({verdict})",
                        f"No active bridge ({verdict})",
                    ),
                    severity="warning",
                )
            else:
                self.app.notify(
                    _label(
                        self.language,
                        f"Ошибка остановки: {result.get('error', 'неизвестно')}",
                        f"Stop error: {result.get('error', 'unknown')}",
                    ),
                    severity="error",
                )
            self.refresh_view(force=True)

        def action_verify(self) -> None:
            """Check the connection. Never start or restart anything to do it.

            For discovered saved profiles (synthetic ``discovered-*``
            connection_id), re-checks live processes and credential via
            :func:`compute_live_status` — the controller has no registry record
            to test. For registry-backed connections, runs the real MCP
            ``initialize`` + ``tools/list`` via the controller.
            """

            if self._checking:
                return
            state = self._last_state if self._has_snapshot else None
            target = getattr(state, "target", None)
            if target is None or not self._endpoint:
                self._status = SERVICE_NOT_CONFIGURED
                self._typed_status = None
                self._write()
                return

            connection_id = str(getattr(target, "connection_id", "") or "")
            # Discovered profiles: re-check PID liveness + credential via the
            # typed status model. This re-reads the watchdog record, re-verifies
            # PIDs, and re-resolves the credential — a live re-check, not a redraw.
            if not connection_id or connection_id.startswith("discovered-"):
                self._checking = True
                self._status = SERVICE_CHECKING
                self._write()

                def execute_discovered() -> None:
                    # compute_live_status is fast (PID checks + keyring resolve);
                    # run it in the worker to avoid blocking the UI thread.
                    try:
                        typed = self._compute_typed_status(state)
                    except Exception:
                        typed = None
                    self.app.call_from_thread(self._checked_typed, typed)

                self.app.run_worker(
                    execute_discovered, thread=True, exclusive=True, group="svc-verify"
                )
                return

            self._checking = True
            self._status = SERVICE_CHECKING
            self._write()

            def execute() -> None:
                try:
                    result = self._controller.test(
                        connection_id, timeout_seconds=15.0
                    )
                except Exception as exc:
                    # Through the shared redaction boundary, not a second copy of
                    # it: a provider or transport error may carry the bearer
                    # token or the whole request in its message.
                    result = {
                        "state": "failed",
                        "detail": str(redact(str(exc))),
                    }
                self.app.call_from_thread(self._checked, result)

            self.app.run_worker(execute, thread=True, exclusive=True, group="svc-verify")

        def _checked_typed(self, typed: Optional[ConnectionLiveStatus]) -> None:
            """Callback for the discovered-profile typed-status re-check."""
            if not self.is_mounted:
                return
            self._checking = False
            self._typed_status = typed
            self._status = self._status_from_typed(typed)
            self._write()

        def _checked(self, result: Dict[str, Any]) -> None:
            if not self.is_mounted:
                return
            self._checking = False
            self._status = service_status_after_check(self.preset_id, result)
            self._write()

        def _more_actions(self) -> tuple[tuple[str, str], ...]:
            """Actions intentionally hidden from the normal connection surface."""
            actions: List[tuple[str, str]] = [
                ("settings", _label(self.language, "Настройки", "Settings")),
            ]
            if self._endpoint:
                actions.append(
                    (
                        "copy-auth",
                        _label(
                            self.language,
                            "Скопировать ключ" if self._uses_bearer_auth() else "Скопировать пароль",
                            "Copy key" if self._uses_bearer_auth() else "Copy approval password",
                        ),
                    )
                )
                actions.append(("verify", _label(self.language, "Проверить", "Verify")))
            if self._has_snapshot:
                actions.append(
                    ("diagnostics", _label(self.language, "Диагностика", "Diagnostics"))
                )
            if self._show_restart_button():
                actions.append(("restart", _label(self.language, "Перезапустить", "Restart")))
                actions.append(("stop", _label(self.language, "Остановить", "Stop")))
            if self._has_snapshot and self._last_state is not None:
                actions.append(("delete", _label(self.language, "Удалить подключение", "Delete connection")))
            return tuple(actions)

        def action_more(self) -> None:
            """Open the small overflow menu instead of rendering every action."""
            self.app.push_screen(
                ServiceMoreScreen(
                    self.language,
                    title=service_display_name(self.preset_id),
                    actions=self._more_actions(),
                ),
                self._more_selected,
            )

        def _more_selected(self, action: Optional[str]) -> None:
            if action == "settings":
                self.action_advanced()
            elif action == "copy-auth":
                self.action_copy_static_bearer()
            elif action == "verify":
                self.action_verify()
            elif action == "diagnostics":
                self.action_diagnostics()
            elif action == "restart":
                self.action_restart_service()
            elif action == "stop":
                self.action_stop()
            elif action == "delete":
                self.action_delete_profile()

        def action_advanced(self) -> None:
            """Open a real saved-profile editor when this service owns one."""

            state = self._last_state if self._has_snapshot else None
            target = getattr(state, "target", None)
            bridge_running = (
                hub_runtime_status(getattr(state, "state", None))
                == HUB_STATUS_WORKING
            )
            profile_name = self._saved_profile_name()
            if profile_name:
                try:
                    from .web_bridge_profiles import WebBridgeProfileStore

                    saved = WebBridgeProfileStore().get(profile_name)
                    if saved.target_profile in {
                        "chatgpt-web",
                        "claude-web",
                        "hyperagent-web",
                        "notion",
                    }:
                        self.app.push_screen(
                            SavedWebProfileSettingsScreen(
                                self.language,
                                profile_name=profile_name,
                                bridge_running=bridge_running,
                            ),
                            self._advanced_closed,
                        )
                        return
                except Exception:
                    # Fall through to the generic service form. Opening Advanced
                    # must remain possible even when an old/broken saved profile
                    # needs repair through the normal service flow.
                    pass
            connection_id = str(getattr(target, "connection_id", "") or "")
            registry_backed = bool(connection_id) and not connection_id.startswith("discovered-")
            if not registry_backed:
                # Before the first saved object exists, Advanced must configure
                # the object that Start will actually create.  Opening a generic
                # form with no apply callback was a convincing-looking no-op.
                open_setup = getattr(self.app, "open_service_bridge_setup", None)
                if not callable(open_setup):
                    self.app.notify(
                        _label(
                            self.language,
                            "Экран первичной настройки недоступен.",
                            "The initial setup screen is unavailable.",
                        ),
                        severity="error",
                    )
                    return
                open_setup(
                    self.preset_id,
                    lambda: self.refresh_view(force=True),
                )
                return
            current_values = service_advanced_values(target)
            apply_callback: Optional[Callable[[Mapping[str, str]], None]] = (
                lambda values: apply_service_advanced(
                    self._controller,
                    connection_id,
                    values,
                )
            )
            self.app.push_screen(
                AdvancedSettingsScreen(
                    self.language,
                    kind="service",
                    preset_id=self.preset_id,
                    values=current_values,
                    has_secret=bool(getattr(target, "credential_ref", "")),
                    bridge_running=bridge_running,
                    apply=apply_callback,
                ),
                self._advanced_closed,
            )

        def _advanced_closed(self, _values: Optional[Mapping[str, Any]]) -> None:
            """Back to the standard flow, re-reading whatever actually changed."""

            self.refresh_view(force=True)

        def action_cancel(self) -> None:
            self.dismiss(None)

    class ServiceMoreScreen(ModalScreen[Optional[str]]):
        """Compact overflow menu for connection actions."""

        BINDINGS = [Binding("escape", "cancel", _C["en"]["cancel"], priority=True)]
        DEFAULT_CSS = """
        ServiceMoreScreen { align: center middle; background: #0e0c08 92%; }
        #svc-more-dialog { width: 100%; max-width: 44; height: auto; max-height: 80%;
          background: #1a1712; border: round #c6a56b; padding: 1 2; }
        #svc-more-title { height: auto; text-style: bold; color: #e5e5e5; }
        #svc-more-list { height: auto; max-height: 12; border: none; background: #1a1712; margin-top: 1; }
        #svc-more-hint { height: auto; color: #6f6658; margin-top: 1; }
        """

        def __init__(
            self,
            language: str,
            *,
            title: str,
            actions: Sequence[tuple[str, str]],
        ) -> None:
            super().__init__()
            self.language = language
            self.heading = title
            self.actions = tuple(actions)

        def compose(self) -> ComposeResult:
            with Vertical(id="svc-more-dialog"):
                yield Static(
                    _label(self.language, f"{self.heading} · Ещё", f"{self.heading} · More"),
                    id="svc-more-title",
                    markup=False,
                )
                yield OptionList(
                    *(Option(label, id=action) for action, label in self.actions),
                    id="svc-more-list",
                )
                yield Static(
                    _label(
                        self.language,
                        "↑↓ — выбрать · Enter — открыть · Esc — назад",
                        "↑↓ — choose · Enter — open · Esc — back",
                    ),
                    id="svc-more-hint",
                    markup=False,
                )

        def on_mount(self) -> None:
            options = self.query_one("#svc-more-list", OptionList)
            if self.actions:
                options.highlighted = 0
            options.focus()

        @on(OptionList.OptionSelected, "#svc-more-list")
        def _selected(self, event: OptionList.OptionSelected) -> None:
            action = str(getattr(event.option, "id", "") or "")
            self.dismiss(action or None)

        def action_cancel(self) -> None:
            self.dismiss(None)

    class ServiceDiagnosticsScreen(ModalScreen[None]):
        """Readable connection health first; technical detail second."""

        BINDINGS = [
            Binding("c", "copy", "Copy", priority=True),
            Binding("escape", "close", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        ServiceDiagnosticsScreen { align: center middle; background: #0e0c08 92%; }
        #svc-diag-dialog { width: 100%; max-width: 72; height: auto; max-height: 88%;
          background: #1a1712; border: round #c6a56b; padding: 1 2; }
        #svc-diag-dialog .title { text-style: bold; color: #e5e5e5; }
        #svc-diag-summary { color: #c9c1b2; height: auto; text-wrap: wrap; margin: 1 0; }
        #svc-diag-tech-title { color: #d4b676; text-style: bold; margin-top: 1; }
        #svc-diag-tech { height: auto; max-height: 14; color: #8f8778; text-wrap: wrap; }
        #svc-diag-actions { height: auto; margin-top: 1; }
        #svc-diag-actions Button { margin-right: 1; }
        """

        def __init__(
            self,
            language: str,
            *,
            title: str,
            summary_lines: Sequence[str],
            technical_lines: Sequence[str],
        ) -> None:
            super().__init__()
            self.language = language
            self.heading = title
            self.summary_lines = tuple(str(item) for item in summary_lines if str(item).strip())
            self.technical_lines = tuple(str(item) for item in technical_lines if str(item).strip())

        def compose(self) -> ComposeResult:
            with Vertical(id="svc-diag-dialog"):
                yield Static(self.heading, classes="title", markup=False)
                yield Static("\n".join(self.summary_lines), id="svc-diag-summary", markup=False)
                yield Static(
                    _label(self.language, "Технические детали", "Technical details"),
                    id="svc-diag-tech-title",
                    markup=False,
                )
                with VerticalScroll(id="svc-diag-tech"):
                    yield Static("\n".join(self.technical_lines), id="svc-diag-tech-body", markup=False)
                with Horizontal(id="svc-diag-actions"):
                    yield Button(_label(self.language, "Скопировать", "Copy"), id="svc-diag-copy")
                    yield Button(_label(self.language, "Назад", "Back"), id="svc-diag-close")

        def text(self) -> str:
            return "\n".join((self.heading, *self.summary_lines, "", *self.technical_lines))

        @on(Button.Pressed)
        def _pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "svc-diag-copy":
                self.action_copy()
            elif event.button.id == "svc-diag-close":
                self.action_close()

        def action_copy(self) -> None:
            copy_text(self.text())
            self.app.notify(_t(self.language, "copied"))

        def action_close(self) -> None:
            self.dismiss(None)

    class PermissionDetailScreen(ModalScreen[None]):
        """The exact allowlist, and the only place tool names may appear.

        B4. Read-only, opened by a deliberate action, and computed from the
        policy layer rather than restated -- a hand-written list here would
        eventually promise a capability the policy denies.
        """

        BINDINGS = [Binding("escape", "close", _C["en"]["cancel"], priority=True)]
        DEFAULT_CSS = """
        PermissionDetailScreen { align: center middle; background: #0e0c08 92%; }
        #perm-dialog { width: 100%; max-width: 56; height: auto; max-height: 80%;
          background: #1a1712; border: round #c6a56b; padding: 1 2; }
        #perm-dialog .title { text-style: bold; color: #e5e5e5; }
        #perm-body { height: auto; color: #c6bca8; margin-top: 1; }
        """

        def __init__(self, language: str = "ru", *, mode: str = PERMISSION_PROJECT) -> None:
            super().__init__()
            self.language = language
            self.mode = mode

        def compose(self) -> ComposeResult:
            english = self.language != "ru"
            with Vertical(id="perm-dialog"):
                yield Static(
                    permission_mode_words(self.mode, english), classes="title"
                )
                yield Static(
                    "\n".join(permission_mode_capabilities(self.mode))
                    or _label(self.language, "нет данных", "no data"),
                    id="perm-body",
                    markup=False,
                )

        def body_text(self) -> str:
            try:
                return str(self.query_one("#perm-body", Static).render())
            except Exception:
                return ""

        def action_close(self) -> None:
            self.dismiss(None)

    class BrowserCredentialManagerScreen(ModalScreen[Optional[Dict[str, Any]]]):
        """Attach/detach test-only browser logins without rendering secret values."""

        BINDINGS = [Binding("escape", "close", _C["en"]["cancel"], priority=True)]
        DEFAULT_CSS = """
        BrowserCredentialManagerScreen { align: center middle; background: #0e0c08 92%; }
        #bc-dialog { width: 100%; max-width: 66; height: auto; max-height: 88%;
          background: #1a1712; border: round #c6a56b; padding: 1 2; }
        #bc-dialog .title { text-style: bold; color: #e5e5e5; }
        #bc-warning { color: #e0bd78; height: auto; text-wrap: wrap; margin-bottom: 1; }
        #bc-attached-list { height: 5; border: round #3a3328; margin-bottom: 1; }
        #bc-attached-empty { color: #8a7e6a; height: auto; margin-bottom: 1; }
        #bc-error { color: #e0a3a3; height: auto; text-wrap: wrap; }
        #bc-actions { height: auto; margin-top: 1; }
        #bc-actions Button { margin-right: 1; }
        """

        def __init__(
            self,
            language: str,
            *,
            profile_name: str,
            bridge_running: bool,
        ) -> None:
            super().__init__()
            self.language = language
            self.profile_name = profile_name
            self.bridge_running = bridge_running
            self._ref_by_option_id: Dict[str, str] = {}

        def _refs(self) -> Tuple[str, ...]:
            try:
                from .web_bridge_profiles import WebBridgeProfileStore

                return tuple(WebBridgeProfileStore().get(self.profile_name).browser_credential_refs)
            except Exception:
                return ()

        def _refs_text(self) -> str:
            refs = self._refs()
            if not refs:
                return _label(self.language, "Прикреплённых тестовых аккаунтов нет.", "No test accounts are attached.")
            return _label(self.language, "Прикреплено тестовых аккаунтов: ", "Attached test accounts: ") + str(len(refs))

        def compose(self) -> ComposeResult:
            with Vertical(id="bc-dialog"):
                yield Static(
                    _label(self.language, "Тестовые аккаунты браузера", "Browser test accounts"),
                    classes="title",
                )
                yield Static(
                    _label(
                        self.language,
                        "Используйте только фейковые/тестовые/неважные аккаунты. Логин и пароль сохраняются локально в системном keyring и никогда не передаются в ChatGPT. CAPTCHA, 2FA и consent всегда остаются за вами.",
                        "Use only fake/test/non-important accounts. Username and password stay in the local OS keyring and are never sent to ChatGPT. CAPTCHA, 2FA and consent always remain user takeover steps.",
                    ),
                    id="bc-warning",
                    markup=False,
                )
                yield Static(self._refs_text(), id="bc-attached-empty", markup=False)
                yield OptionList(id="bc-attached-list")
                yield Label(_label(self.language, "Имя нового тестового аккаунта", "New test account name"))
                yield Input(placeholder="gmail-test-1", id="bc-name")
                yield Label(_label(self.language, "Логин / email", "Username / email"))
                yield Input(id="bc-username")
                yield Label(_label(self.language, "Пароль", "Password"))
                yield Input(password=True, id="bc-password")
                yield Static("", id="bc-error", markup=False)
                with Horizontal(id="bc-actions"):
                    yield Button(_label(self.language, "Сохранить / прикрепить", "Save / attach"), id="bc-save", variant="primary")
                    yield Button(_label(self.language, "Удалить", "Delete"), id="bc-delete", variant="error")
                    yield Button(_label(self.language, "Назад", "Back"), id="bc-close")

        def _error(self, message: str) -> None:
            with contextlib.suppress(Exception):
                self.query_one("#bc-error", Static).update(str(redact(message)))

        def on_mount(self) -> None:
            self._refresh_refs()

        def _refresh_refs(self) -> None:
            refs = self._refs()
            self._ref_by_option_id.clear()
            with contextlib.suppress(Exception):
                empty = self.query_one("#bc-attached-empty", Static)
                empty.update(self._refs_text())
                empty.styles.display = "none" if refs else "block"
            with contextlib.suppress(Exception):
                options = self.query_one("#bc-attached-list", OptionList)
                options.clear_options()
                for index, reference in enumerate(refs):
                    option_id = f"bc-ref-{index}"
                    self._ref_by_option_id[option_id] = reference
                    display = reference.split("/", 1)[-1]
                    options.add_option(Option(display, id=option_id))
                options.styles.display = "block" if refs else "none"
                if refs:
                    options.highlighted = 0

        def _selected_ref(self) -> Optional[str]:
            try:
                options = self.query_one("#bc-attached-list", OptionList)
                highlighted = options.highlighted
                if highlighted is None or highlighted >= len(options.options):
                    return None
                option_id = getattr(options.get_option_at_index(highlighted), "id", None)
                return self._ref_by_option_id.get(str(option_id))
            except Exception:
                return None

        def _clear_secret_fields(self) -> None:
            for selector in ("#bc-username", "#bc-password"):
                with contextlib.suppress(Exception):
                    self.query_one(selector, Input).value = ""

        @on(Button.Pressed)
        def _pressed(self, event: Button.Pressed) -> None:
            button_id = event.button.id or ""
            if button_id == "bc-close":
                self.action_close()
                return
            if button_id == "bc-save":
                self._save()
                return
            if button_id == "bc-delete":
                self._delete()

        def _save(self) -> None:
            name = self.query_one("#bc-name", Input).value.strip()
            username = self.query_one("#bc-username", Input).value
            password = self.query_one("#bc-password", Input).value
            if not name or not username or not password:
                self._error(_label(self.language, "Заполните имя, логин и пароль.", "Enter name, username and password."))
                return
            self._error("")
            if self.bridge_running:
                self.app.push_screen(
                    _ConfirmScreen(
                        _label(self.language, "Прикрепить аккаунт и перезапустить?", "Attach account and restart?"),
                        _label(
                            self.language,
                            "Секрет останется только в системном keyring. Чтобы живой bridge увидел новый разрешённый аккаунт, KaroX безопасно перезапустит только свой bridge с rollback при ошибке.",
                            "The secret stays only in the OS keyring. To expose the newly approved account to the live bridge, KaroX will safely restart only its owned bridge with rollback on failure.",
                        ),
                        language=self.language,
                    ),
                    self._save_confirmed,
                )
                return
            self._save_apply()

        def _save_confirmed(self, confirmed: Optional[bool]) -> None:
            if confirmed:
                self._save_apply()

        def _save_apply(self) -> None:
            name = self.query_one("#bc-name", Input).value.strip()
            username = self.query_one("#bc-username", Input).value
            password = self.query_one("#bc-password", Input).value
            if not name or not username or not password:
                self._error(_label(self.language, "Заполните имя, логин и пароль.", "Enter name, username and password."))
                return

            def work() -> None:
                from .web_bridge_launcher import SavedBridgeRestartRequired

                try:
                    result = attach_saved_browser_credential(
                        self.profile_name,
                        name=name,
                        username=username,
                        password=password,
                        bridge_running=self.bridge_running,
                    )
                except SavedBridgeRestartRequired:
                    self.app.call_from_thread(self._save_detected_live_bridge)
                    return
                except Exception as exc:
                    self.app.call_from_thread(self._error, str(exc))
                    return
                self.app.call_from_thread(self._credential_saved, result)

            self.run_worker(work, thread=True, exclusive=True, group="browser-credential")

        def _save_detected_live_bridge(self) -> None:
            self.bridge_running = True
            self._save()

        def _credential_saved(self, result: Mapping[str, Any]) -> None:
            self._clear_secret_fields()
            self._refresh_refs()
            self._error(
                _label(self.language, "Сохранено локально: ", "Stored locally: ")
                + str(result.get("reference") or "")
            )

        def _delete(self) -> None:
            reference = self._selected_ref()
            if not reference:
                self._error(
                    _label(
                        self.language,
                        "Выберите прикреплённый тестовый аккаунт в списке.",
                        "Select an attached test account from the list.",
                    )
                )
                return
            self._error("")
            if self.bridge_running:
                self.app.push_screen(
                    _ConfirmScreen(
                        _label(self.language, "Открепить аккаунт и перезапустить?", "Detach account and restart?"),
                        _label(
                            self.language,
                            "KaroX сначала уберёт разрешение этого аккаунта из профиля, затем безопасно перезапустит свой bridge. После успешного применения запись будет удалена из keyring, если она больше нигде не нужна.",
                            "KaroX will first remove this account from the profile, then safely restart its owned bridge. After the change applies, the keyring entry will be deleted when it is no longer needed.",
                        ),
                        language=self.language,
                    ),
                    self._delete_confirmed,
                )
                return
            self._delete_apply()

        def _delete_confirmed(self, confirmed: Optional[bool]) -> None:
            if confirmed:
                self._delete_apply()

        def _delete_apply(self) -> None:
            reference = self._selected_ref()
            if not reference:
                self._error(
                    _label(
                        self.language,
                        "Выберите прикреплённый тестовый аккаунт в списке.",
                        "Select an attached test account from the list.",
                    )
                )
                return

            def work() -> None:
                from .web_bridge_launcher import SavedBridgeRestartRequired

                try:
                    result = detach_saved_browser_credential(
                        self.profile_name,
                        reference,
                        bridge_running=self.bridge_running,
                    )
                except SavedBridgeRestartRequired:
                    self.app.call_from_thread(self._delete_detected_live_bridge)
                    return
                except Exception as exc:
                    self.app.call_from_thread(self._error, str(exc))
                    return
                self.app.call_from_thread(self._credential_deleted, result)

            self.run_worker(work, thread=True, exclusive=True, group="browser-credential")

        def _delete_detected_live_bridge(self) -> None:
            self.bridge_running = True
            self._delete()

        def _credential_deleted(self, result: Mapping[str, Any]) -> None:
            self._clear_secret_fields()
            self._refresh_refs()
            if result.get("keyring_shared"):
                message = _label(
                    self.language,
                    "Откреплено от этого подключения; запись сохранена в keyring, потому что её использует другое подключение.",
                    "Detached from this connection; the keyring entry was kept because another connection still uses it.",
                )
            elif result.get("keyring_deleted"):
                message = _label(self.language, "Откреплено и удалено из keyring.", "Detached and deleted from keyring.")
            else:
                message = _label(
                    self.language,
                    "Откреплено от KaroX; удалить запись из keyring автоматически не удалось.",
                    "Detached from KaroX; the keyring entry could not be deleted automatically.",
                )
            self._error(message)

        def action_close(self) -> None:
            self.dismiss({"changed": True})

    class SavedWebProfileSettingsScreen(ModalScreen[Optional[Dict[str, Any]]]):
        """Real editor for the saved profile backing ChatGPT/Claude/Hyperagent."""

        BINDINGS = [
            Binding("ctrl+s", "save", _C["en"]["save"], priority=True),
            Binding("f10", "save", _C["en"]["save"], priority=True),
            Binding("escape", "cancel", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        SavedWebProfileSettingsScreen { align: center middle; background: #0e0c08 92%; }
        #swp-dialog { width: 100%; max-width: 90; height: auto; max-height: 94%;
          background: #1a1712; border: round #c6a56b; padding: 1 2; }
        #swp-dialog .title { text-style: bold; color: #e5e5e5; }
        #swp-body { height: auto; max-height: 32; scrollbar-size-vertical: 1; }
        #swp-body .section { color: #d4b676; text-style: bold; margin-top: 1; }
        #swp-body .row { height: 3; min-height: 3; align-vertical: middle; }
        #swp-body .row Label { width: 1fr; color: #c6bca8; content-align: left middle; }
        #swp-body .row Switch { width: 8; min-width: 8; background: transparent; }
        #swp-error { color: #e0a3a3; height: auto; text-wrap: wrap; margin-top: 1; }
        #swp-hint { color: #8a7e6a; height: auto; }
        #swp-actions { height: auto; margin-top: 1; }
        #swp-actions Button { margin-right: 1; }
        """

        def __init__(
            self,
            language: str,
            *,
            profile_name: str,
            bridge_running: bool,
        ) -> None:
            super().__init__()
            self.language = language
            self.profile_name = profile_name
            self.bridge_running = bridge_running
            from .web_bridge_profiles import WebBridgeProfileStore

            self.profile = WebBridgeProfileStore().get(profile_name)
            self.browser_capable = self.profile.target_profile in {
                "chatgpt-web",
                "claude-web",
                "hyperagent-web",
            }
            self.initial = saved_web_profile_settings(self.profile)
            self._saving = False

        def compose(self) -> ComposeResult:
            value = self.initial
            with Vertical(id="swp-dialog"):
                yield Static(
                    _label(self.language, "Настройки подключения", "Connection settings")
                    + f" · {self.profile_name}",
                    classes="title",
                    markup=False,
                )
                with VerticalScroll(id="swp-body"):
                    yield Static(_label(self.language, "Подключение", "Connection"), classes="section")
                    yield Label(_label(self.language, "Локальный порт", "Local port"))
                    yield Input(value=str(value["port"]), id="swp-port")
                    yield Label(_label(self.language, "Tunnel", "Tunnel"))
                    with RadioSet(id="swp-tunnel"):
                        yield RadioButton(
                            "Tailscale",
                            value=value["tunnel"] == "tailscale",
                            id="swp-tunnel-tailscale",
                        )
                        yield RadioButton(
                            "Cloudflare",
                            value=value["tunnel"] == "cloudflare",
                            id="swp-tunnel-cloudflare",
                        )
                        yield RadioButton(
                            "Custom",
                            value=value["tunnel"] == "custom",
                            id="swp-tunnel-custom",
                        )
                    yield Label(
                        _label(self.language, "Custom HTTPS URL", "Custom HTTPS URL"),
                        id="swp-public-url-label",
                    )
                    yield Input(value=str(value["public_url"]), id="swp-public-url")

                    if self.browser_capable:
                        yield Static(_label(self.language, "Браузер", "Browser"), classes="section")
                        for selector, ru, en, current in (
                            ("swp-external", "Публичный HTTPS + localhost", "Public HTTPS + localhost", value["browser_external_https"]),
                            ("swp-headed", "Видимый managed Chrome", "Visible managed Chrome", value["browser_headed"]),
                            ("swp-takeover", "Передача управления для CAPTCHA / 2FA", "User takeover for CAPTCHA / 2FA", value["browser_user_takeover"]),
                            ("swp-network", "Метаданные сетевых запросов", "Network request metadata", value["browser_network_inspection"]),
                            ("swp-payment", "Разрешить подтверждение платежей", "Allow payment confirmation", value["browser_payment_confirmation"]),
                        ):
                            with Horizontal(classes="row"):
                                yield Label(_label(self.language, ru, en))
                                yield Switch(value=bool(current), id=selector)
                        yield Label(_label(self.language, "Разрешённые домены (пусто = любой публичный HTTPS)", "Allowed domains (empty = any public HTTPS)"))
                        yield Input(value=str(value["browser_allowed_domains"]), id="swp-allow-domains")
                        yield Label(_label(self.language, "Запрещённые домены", "Denied domains"))
                        yield Input(value=str(value["browser_denied_domains"]), id="swp-deny-domains")
                        yield Label(_label(self.language, "Разрешённые email", "Allowed emails"))
                        yield Input(value=str(value["browser_allowed_emails"]), id="swp-emails")
                        yield Static(
                            _label(self.language, "Тестовые аккаунты: ", "Test accounts: ")
                            + str(len(value["browser_credential_refs"])),
                            id="swp-credential-count",
                        )
                yield Static("", id="swp-error", markup=False)
                yield Static(
                    _label(
                        self.language,
                        "Ctrl+S — применить · изменения живого профиля перезапускаются безопасно с rollback · Esc — назад",
                        "Ctrl+S — apply · live profile changes restart safely with rollback · Esc — back",
                    ),
                    id="swp-hint",
                    markup=False,
                )
                with Horizontal(id="swp-actions"):
                    if self.browser_capable:
                        yield Button(_label(self.language, "Тестовые аккаунты", "Test accounts"), id="swp-credentials")
                    yield Button(_label(self.language, "Сохранить", "Save"), id="swp-save", variant="primary")
                    yield Button(_label(self.language, "Назад", "Back"), id="swp-cancel")

        def _error(self, message: str) -> None:
            with contextlib.suppress(Exception):
                self.query_one("#swp-error", Static).update(str(redact(message)))

        def on_mount(self) -> None:
            self._sync_tunnel_visibility()

        @on(RadioSet.Changed, "#swp-tunnel")
        def _tunnel_changed(self, _event: RadioSet.Changed) -> None:
            self._sync_tunnel_visibility()

        def _selected_tunnel(self) -> str:
            index = self.query_one("#swp-tunnel", RadioSet).pressed_index
            values = ("tailscale", "cloudflare", "custom")
            if 0 <= index < len(values):
                return values[index]
            return str(self.initial.get("tunnel") or "tailscale")

        def _sync_tunnel_visibility(self) -> None:
            custom = self._selected_tunnel() == "custom"
            with contextlib.suppress(Exception):
                self.query_one("#swp-public-url-label", Label).styles.display = (
                    "block" if custom else "none"
                )
            with contextlib.suppress(Exception):
                self.query_one("#swp-public-url", Input).styles.display = (
                    "block" if custom else "none"
                )

        def values(self) -> Dict[str, Any]:
            values: Dict[str, Any] = {
                "port": self.query_one("#swp-port", Input).value,
                "tunnel": self._selected_tunnel(),
                "public_url": self.query_one("#swp-public-url", Input).value,
            }
            if self.browser_capable:
                values.update(
                    {
                        "browser_external_https": self.query_one("#swp-external", Switch).value,
                        "browser_headed": self.query_one("#swp-headed", Switch).value,
                        "browser_user_takeover": self.query_one("#swp-takeover", Switch).value,
                        "browser_network_inspection": self.query_one("#swp-network", Switch).value,
                        "browser_payment_confirmation": self.query_one("#swp-payment", Switch).value,
                        "browser_allowed_domains": self.query_one("#swp-allow-domains", Input).value,
                        "browser_denied_domains": self.query_one("#swp-deny-domains", Input).value,
                        "browser_allowed_emails": self.query_one("#swp-emails", Input).value,
                    }
                )
            return values

        @on(Button.Pressed)
        def _pressed(self, event: Button.Pressed) -> None:
            button_id = event.button.id or ""
            if button_id == "swp-save":
                self.action_save()
            elif button_id == "swp-cancel":
                self.action_cancel()
            elif button_id == "swp-credentials":
                self.app.push_screen(
                    BrowserCredentialManagerScreen(
                        self.language,
                        profile_name=self.profile_name,
                        bridge_running=self.bridge_running,
                    ),
                    self._credentials_closed,
                )

        def _credentials_closed(self, _result: Optional[Dict[str, Any]]) -> None:
            try:
                from .web_bridge_profiles import WebBridgeProfileStore

                self.profile = WebBridgeProfileStore().get(self.profile_name)
                self.initial = saved_web_profile_settings(self.profile)
                count = len(self.initial["browser_credential_refs"])
                self.query_one("#swp-credential-count", Static).update(
                    _label(self.language, "Тестовые аккаунты: ", "Test accounts: ") + str(count)
                )
            except Exception as exc:
                self._error(str(exc))

        def action_save(self) -> None:
            if self._saving:
                return
            values = self.values()
            try:
                # Validate synchronously so simple field errors do not start a worker.
                build_saved_web_profile_settings(self.profile, values)
            except Exception as exc:
                self._error(str(exc))
                return
            enabling_payment = bool(values.get("browser_payment_confirmation")) and not bool(
                self.profile.browser_payment_confirmation
            )
            if enabling_payment:
                self.app.push_screen(
                    _ConfirmScreen(
                        _label(self.language, "Разрешить подтверждение платежей?", "Allow payment confirmation?"),
                        _label(
                            self.language,
                            "Это расширяет browser-права: KaroX сможет взаимодействовать с платёжными/подписочными подтверждениями в разрешённой сессии. Самостоятельные платежи не выполняются, а CAPTCHA/2FA остаются за пользователем. Включайте только если это действительно нужно.",
                            "This expands browser permission: KaroX may interact with payment/subscription confirmation controls in the approved session. It does not make autonomous payments, and CAPTCHA/2FA remain user-only. Enable this only when genuinely needed.",
                        ),
                        language=self.language,
                    ),
                    self._payment_permission_confirmed,
                )
                return
            self._continue_save()

        def _payment_permission_confirmed(self, confirmed: Optional[bool]) -> None:
            if confirmed:
                self._continue_save()

        def _continue_save(self) -> None:
            if self.bridge_running:
                self.app.push_screen(
                    _ConfirmScreen(
                        _label(self.language, "Применить и безопасно перезапустить?", "Apply and safely restart?"),
                        _label(
                            self.language,
                            "KaroX остановит только доказанно свой bridge, применит профиль и запустит его снова. Если новый профиль не поднимется, будет восстановлен предыдущий.",
                            "KaroX will stop only its proven owned bridge, apply the profile and start it again. If the new profile fails, the previous one will be restored.",
                        ),
                        language=self.language,
                    ),
                    self._save_confirmed,
                )
                return
            self._apply()

        def _save_confirmed(self, confirmed: Optional[bool]) -> None:
            if confirmed:
                self._apply()

        def _apply(self) -> None:
            if self._saving:
                return
            self._saving = True
            self._error("")
            values = self.values()

            def work() -> None:
                from .web_bridge_launcher import SavedBridgeRestartRequired

                try:
                    result = apply_saved_web_profile_settings(
                        self.profile_name,
                        values,
                        bridge_running=self.bridge_running,
                    )
                except SavedBridgeRestartRequired:
                    self.app.call_from_thread(self._save_requires_restart)
                    return
                except Exception as exc:
                    self.app.call_from_thread(self._save_failed, str(exc))
                    return
                self.app.call_from_thread(self._save_done, result)

            self.run_worker(work, thread=True, exclusive=True, group="saved-web-profile")

        def _save_requires_restart(self) -> None:
            self._saving = False
            self.bridge_running = True
            self._continue_save()

        def _save_failed(self, message: str) -> None:
            self._saving = False
            self._error(message)

        def _save_done(self, result: Mapping[str, Any]) -> None:
            self._saving = False
            self.dismiss(dict(result))

        def action_cancel(self) -> None:
            if not self._saving:
                self.dismiss(None)

    class AdvancedSettingsScreen(ModalScreen[Optional[Dict[str, str]]]):
        """One advanced layer for providers and services alike.

        B4. Opening this applies nothing. Editing applies nothing. Only Save
        applies, and only once; Cancel and Esc leave the connection exactly as
        they found it. A change that would move the address a live service is
        already talking to asks first and says what will happen.

        Fields are chosen per kind rather than shown universally, because a base
        URL on a ClickUp screen or a tunnel strategy on a provider screen is a
        control the flow cannot apply -- worse than an absent one, because the
        user believes it did something.
        """

        BINDINGS = [
            Binding("ctrl+s", "save", _C["en"]["save"], priority=True),
            Binding("f10", "save", _C["en"]["save"], priority=True),
            Binding("f4", "reset", "Reset", priority=True),
            Binding("f6", "permissions", "Permissions", priority=True),
            Binding("escape", "cancel", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        AdvancedSettingsScreen { align: center middle; background: #0e0c08 92%; }
        /* The B2 rule: a bounded height with a scrolling body, and nothing
           `1fr` outside that body, so a long group scrolls instead of the
           dialog swallowing the terminal. */
        #adv-dialog { width: 100%; max-width: 72; height: auto; max-height: 92%;
          background: #1a1712; border: round #c6a56b; padding: 1 2; }
        #adv-dialog .title { text-style: bold; color: #e5e5e5; }
        #adv-body { height: auto; max-height: 18; }
        #adv-body .group { color: #d4b676; text-style: bold; margin-top: 1; }
        #adv-body .field-label { color: #b3a990; }
        #adv-body Input { margin-bottom: 1; }
        #adv-perms { height: auto; color: #c6bca8; }
        #adv-error { height: auto; color: #e0a3a3; text-wrap: wrap; }
        #adv-hint { height: auto; color: #8a7e6a; margin-top: 1; }
        """

        def __init__(
            self,
            language: str = "ru",
            *,
            kind: str = "service",
            preset_id: str = "clickup",
            values: Optional[Mapping[str, str]] = None,
            has_secret: bool = False,
            bridge_running: bool = False,
            permission_mode: str = PERMISSION_PROJECT,
            apply: Optional[Callable[[Mapping[str, str]], None]] = None,
        ) -> None:
            super().__init__()
            self.language = language
            self.kind = kind
            self.preset_id = preset_id
            self.has_secret = has_secret
            self.bridge_running = bridge_running
            self.permission_mode = permission_mode
            # B5 audit 5.6. The production mutation, injected. The screen owns
            # the form, the validation, the confirmation and the one-shot
            # guard; the callback owns the write. Splitting it this way is what
            # lets "Save persisted" be asserted against a real file instead of
            # against a dict the screen kept to itself.
            #
            # `None` is still allowed, for the add-a-connection flows where the
            # values are collected and applied by the flow that opened this.
            self._apply_callback = apply
            self._initial: Dict[str, str] = dict(
                advanced_defaults(kind, preset_id)
            )
            if values:
                self._initial.update(
                    {k: str(v) for k, v in values.items() if k in self._initial}
                )
            # Set once, on a successful Save. The guard is what makes "applies
            # once" a property rather than a hope: a double Enter cannot write
            # twice.
            self.applied: Optional[Dict[str, str]] = None

        def _english(self) -> bool:
            return self.language != "ru"

        def fields(self) -> Tuple[AdvancedField, ...]:
            return advanced_fields(self.kind, self.preset_id)

        def groups(self) -> Tuple[str, ...]:
            return advanced_visible_groups(self.kind, self.preset_id)

        def compose(self) -> ComposeResult:
            english = self._english()
            with Vertical(id="adv-dialog"):
                yield Static(
                    _label(
                        self.language,
                        "Дополнительные настройки",
                        "Advanced settings",
                    ),
                    classes="title",
                )
                with VerticalScroll(id="adv-body"):
                    for group in self.groups():
                        yield Static(
                            advanced_group_words(group, english),
                            classes="group",
                            markup=False,
                        )
                        if group == ADVANCED_GROUP_PERMISSIONS:
                            yield Static(
                                permission_mode_words(self.permission_mode, english)
                                + " — "
                                + permission_mode_summary(
                                    self.permission_mode, english
                                ),
                                id="adv-perms",
                                markup=False,
                            )
                            continue
                        for field in self.fields():
                            if field.group != group:
                                continue
                            yield Label(
                                field.label(english), classes="field-label"
                            )
                            if field.secret:
                                yield Static(
                                    secret_display(self.has_secret, english),
                                    id=f"adv-secret-{field.field_id}",
                                    markup=False,
                                )
                            yield Input(
                                value=(
                                    ""
                                    if field.secret
                                    else self._initial.get(field.field_id, "")
                                ),
                                password=field.secret,
                                disabled=field.read_only,
                                id=f"adv-{field.field_id}",
                            )
                yield Static("", id="adv-error", markup=False)
                yield Static(self._hint(), id="adv-hint", markup=False)

        def _hint(self) -> str:
            try:
                narrow = int(self.app.size.width) < 60
            except Exception:
                narrow = False
            if narrow:
                return _label(
                    self.language,
                    "Ctrl+S — сохранить · Esc — назад",
                    "Ctrl+S — save · Esc — back",
                )
            return _label(
                self.language,
                "Ctrl+S — сохранить · F4 — сбросить · F6 — разрешения · Esc — назад",
                "Ctrl+S — save · F4 — reset · F6 — permissions · Esc — back",
            )

        def on_resize(self, _event: Any = None) -> None:
            with contextlib.suppress(Exception):
                self.query_one("#adv-hint", Static).update(self._hint())

        # ----------------------------------------------------------- form state

        def values(self) -> Dict[str, str]:
            """What is in the form now, secrets excluded."""

            current: Dict[str, str] = {}
            for field in self.fields():
                if field.secret:
                    continue
                try:
                    current[field.field_id] = self.query_one(
                        f"#adv-{field.field_id}", Input
                    ).value
                except Exception:
                    current[field.field_id] = self._initial.get(field.field_id, "")
            return current

        def secret_value(self) -> str:
            try:
                return self.query_one("#adv-api_key", Input).value
            except Exception:
                return ""

        def error_text(self) -> str:
            try:
                return str(self.query_one("#adv-error", Static).render())
            except Exception:
                return ""

        def rendered_text(self) -> str:
            parts: List[str] = []
            for selector in ("#adv-perms", "#adv-error", "#adv-hint"):
                with contextlib.suppress(Exception):
                    parts.append(str(self.query_one(selector, Static).render()))
            for field in self.fields():
                if not field.secret:
                    continue
                with contextlib.suppress(Exception):
                    parts.append(
                        str(
                            self.query_one(
                                f"#adv-secret-{field.field_id}", Static
                            ).render()
                        )
                    )
            return "\n".join(parts)

        def _set_error(self, message: str) -> None:
            with contextlib.suppress(Exception):
                self.query_one("#adv-error", Static).update(
                    str(redact(message)) if message else ""
                )

        # --------------------------------------------------------------- actions

        def action_reset(self) -> None:
            """Back to the production defaults, not to blank."""

            defaults = advanced_defaults(self.kind, self.preset_id)
            for field_id, value in defaults.items():
                with contextlib.suppress(Exception):
                    self.query_one(f"#adv-{field_id}", Input).value = value
            self._set_error("")

        def action_permissions(self) -> None:
            self.app.push_screen(
                PermissionDetailScreen(self.language, mode=self.permission_mode)
            )

        def action_save(self) -> None:
            """Validate, then apply once -- or explain and stay put.

            A validation failure keeps every value the user typed, focuses the
            field at fault and changes nothing in the registry. Retyping a form
            to correct one character is where people abandon a settings screen.
            """

            english = self._english()
            current = self.values()
            field_id, message = validate_advanced(
                self.kind, self.preset_id, current, english
            )
            if message:
                self._set_error(message)
                with contextlib.suppress(Exception):
                    self.query_one(f"#adv-{field_id}", Input).focus()
                return
            changed = advanced_changed_fields(self._initial, current)
            if (
                self.bridge_running
                and advanced_requires_restart(changed)
            ):
                self.app.push_screen(
                    _ConfirmScreen(
                        _label(
                            self.language,
                            "Перезапустить подключение?",
                            "Restart the connection?",
                        ),
                        _label(
                            self.language,
                            "Текущий адрес станет недоступен, пока подключение "
                            "перезапускается. Сервис, который им пользуется, "
                            "нужно будет обновить.",
                            "The current address will be unavailable while the "
                            "connection restarts, and the service using it will "
                            "need the new one.",
                        ),
                        language=self.language,
                    ),
                    self._restart_confirmed,
                )
                return
            self._apply(current)

        def _restart_confirmed(self, confirmed: Optional[bool]) -> None:
            """Cancelling leaves the live bridge exactly as it was."""

            if not confirmed:
                self._set_error(
                    _label(
                        self.language,
                        "Изменения не применены.",
                        "No changes were applied.",
                    )
                )
                return
            self._apply(self.values())

        def _apply(self, values: Mapping[str, str]) -> None:
            """Persist once, then close. A failure keeps the form and the values.

            The guard is checked *before* the callback and set only after it
            succeeds, which gives both halves of the contract: a double Enter
            cannot write twice, and a failed write can be retried without
            reopening the screen and retyping everything.
            """

            if self.applied is not None:
                return
            if self._apply_callback is not None:
                try:
                    self._apply_callback(dict(values))
                except Exception as exc:
                    # Stay open, keep every typed value, and say what happened
                    # through the shared redaction boundary -- a controller or
                    # keyring error can carry a secret in its message.
                    self._set_error(str(exc))
                    return
            self.applied = dict(values)
            self.dismiss(self.applied)

        def action_cancel(self) -> None:
            self.dismiss(None)

    class ConnectionDetailScreen(ModalScreen[Optional[str]]):
        """One saved record: what it is, what state it is in, what can be done.

        B5. Opening a hub row used to mean opening a *list*. An AI row pushed
        the whole provider list and left the user to find the thing they had
        just pointed at; there was no screen for "this connection" at all, and
        so no place for edit, disable or delete to live.

        This is that place. It reads the same registries the hub projects, and
        it holds no state of its own beyond the in-flight guards below -- so
        anything done here is visible in the hub the moment it returns, because
        both are looking at the same file.

        The ordinary view is deliberately poor in facts: a name, a model or
        service, a status, and an address only when one genuinely exists. No
        credential reference, no adapter id, no access profile, no port, no
        transport, no JSON. Those live behind F2, where B4 already put them.
        """

        BINDINGS = [
            Binding("enter", "choose", "Open", priority=True),
            Binding("f5", "verify", "Verify", priority=True),
            Binding("f2", "advanced", "Advanced settings", priority=True),
            Binding("up", "previous", show=False, priority=True),
            Binding("down", "next", show=False, priority=True),
            Binding("escape", "cancel", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        ConnectionDetailScreen { align: center middle; background: #0e0c08 92%; }
        /* The B2 lesson, once more: `100%` with a ceiling so a 46-column
           terminal gets a dialog that fits, and `height: auto` with no `1fr`
           child so a short window is not eaten by a five-line action list. */
        #cd-dialog { width: 100%; max-width: 62; height: auto; max-height: 92%;
          background: #1a1712; border: round #c6a56b; padding: 1 2; }
        #cd-dialog .title { text-style: bold; color: #e5e5e5; }
        #cd-status { height: auto; color: #c6bca8; margin-top: 1; }
        #cd-status.status-ok { color: #8aab7e; }
        #cd-status.status-warn { color: #d4b676; }
        #cd-status.status-error { color: #e0a3a3; }
        #cd-status.status-off { color: #8a7e6a; }
        #cd-endpoint { height: auto; color: #d4b676; }
        #cd-note { height: auto; color: #8a7e6a; }
        #cd-actions { height: auto; max-height: 7; border: none;
          background: #1a1712; margin-top: 1; }
        #cd-error { color: #e0a3a3; height: auto; min-height: 0;
          text-wrap: wrap; }
        #cd-hint { height: auto; color: #8a7e6a; margin-top: 1; }
        """

        def __init__(
            self,
            language: str = "ru",
            *,
            kind: str = "provider",
            identity: str = "",
        ) -> None:
            super().__init__()
            self.language = language
            # "provider" or "service": which store owns this record. Not a
            # display value and never rendered.
            self.kind = kind
            self.identity = identity
            self._controller = connection_controller()
            from .connection_tests import test_model_provider
            from .paths import config_dir
            from .registry import ProviderRegistry

            self._providers = ProviderController(
                registry=ProviderRegistry(config_dir() / "vnext" / "providers.json"),
                credentials=CredentialStore(),
                tester=lambda provider, model: test_model_provider(
                    provider, model, timeout_seconds=30.0
                ),
            )
            # Not `_name`: Textual declares its own `_name` on `Widget` as
            # `str | None`, and shadowing it here silently changed the type of
            # a framework attribute. A future Textual that reads its own field
            # would find a connection label in it.
            self._display_name = ""
            # The saved record's own preset. Empty until read, and never
            # defaulted to a real service: see `advanced_preset_id`.
            self._preset_id = ""
            self._detail = ""
            self._enabled = True
            # Connection state, not screen state. Textual's MessagePump owns
            # ``_running`` to mean "this node's event loop is alive"; assigning
            # a connection value there made ``screen.is_running`` flip False on
            # a stopped/disabled record, and mount then raised
            # ``SignalError: Node must be running``. The bridge flag lives here.
            self._bridge_running = False
            self._has_secret = False
            self._endpoint = ""
            self._advanced_values_cache: Dict[str, str] = {}
            self._status = HUB_STATUS_ATTENTION
            self._checking = False
            self._loaded = False
            self._refresh_epoch = 0
            # B5. Delete is one-shot by construction. A second Enter on a
            # confirmation that already fired must not remove anything twice,
            # and a failed delete must leave the record reachable.
            self._deleting = False
            self._gone = False

        def _english(self) -> bool:
            return self.language != "ru"

        # ----------------------------------------------------------- view model

        def _read(self) -> None:
            """Re-read this one record from the store that owns it.

            Read-only: nothing here starts, stops or verifies anything, so
            opening the screen cannot disturb a bridge already serving the URL
            the user pasted into a service.
            """

            if self.kind == "provider":
                self._read_provider()
            else:
                self._read_service()

        def _read_provider(self) -> None:
            try:
                details = self._providers.details(self.identity)
            except Exception:
                self._status = HUB_STATUS_ERROR
                return
            provider = details.provider
            self._advanced_values_cache = provider_advanced_values(details)
            self._display_name = provider_display_name(self.identity)
            selected = details.selected_model
            if selected is not None:
                self._detail = str(selected.model_id)
            elif len(details.models) == 1:
                self._detail = str(details.models[0].model_id)
            else:
                self._detail = ""
            self._enabled = bool(provider.enabled)
            self._has_secret = bool(details.credential.get("configured"))
            # A provider has no process and no address. Saying "stopped" about
            # one would invent a runtime it does not have.
            self._bridge_running = False
            self._endpoint = ""
            # B5 audit 5.5. Having models is not evidence that anything works.
            # The key may have been revoked, the endpoint moved, the account
            # suspended -- none of which the registry knows. "Works" is earned
            # by a check inside this screen and nowhere else, so a freshly read
            # record rests at "ready to check".
            #
            # It is also not persisted. No proof survives the process, so a
            # re-opened screen starts neutral rather than restoring a green
            # word from a check that happened before the last restart.
            if not self._enabled:
                self._status = HUB_STATUS_DISABLED
            elif details.models:
                self._status = HUB_STATUS_READY
            else:
                self._status = HUB_STATUS_ATTENTION

        def _read_service(self) -> None:
            try:
                state = self._controller.get(self.identity)
            except Exception:
                self._status = HUB_STATUS_ERROR
                return
            target = state.target
            self._advanced_values_cache = service_advanced_values(target)
            self._display_name = str(target.name or self.identity)
            # B5 audit 5.3. The record's own preset, kept so the advanced layer
            # is opened for the service the user is actually looking at.
            self._preset_id = str(target.preset_id or "")
            self._detail = service_display_name(target.preset_id)
            if self._detail == self._display_name:
                self._detail = ""
            self._enabled = bool(target.enabled)
            self._has_secret = bool(target.credential_ref)
            self._bridge_running = hub_runtime_status(state.state) == HUB_STATUS_WORKING
            self._endpoint = str(state.endpoint or "")
            self._status = (
                HUB_STATUS_DISABLED
                if not self._enabled
                else hub_runtime_status(state.state)
            )

        def view(self) -> Dict[str, Any]:
            """Everything the screen may show, for a test to assert against."""

            return {
                "kind": self.kind,
                "identity": self.identity,
                "name": self._display_name,
                "detail": self._detail,
                "enabled": self._enabled,
                "running": self._bridge_running,
                "has_secret": self._has_secret,
                "endpoint": self._endpoint,
                "status": self._status,
                "actions": detail_actions(
                    self._enabled,
                    kind=self.kind,
                    has_secret=self._has_secret,
                    running=self._bridge_running if self.kind == "service" else None,
                ),
            }

        def compose(self) -> ComposeResult:
            with Vertical(id="cd-dialog"):
                yield Static("", id="cd-title", classes="title", markup=False)
                yield Static("", id="cd-status", markup=False)
                yield Static("", id="cd-endpoint", markup=False)
                yield Static("", id="cd-note", markup=False)
                yield OptionList(id="cd-actions")
                yield Static("", id="cd-error", markup=False)
                yield Static(self._hint(), id="cd-hint", markup=False)

        def _hint(self) -> str:
            try:
                narrow = int(self.app.size.width) < 60
            except Exception:
                narrow = False
            if narrow:
                return _label(
                    self.language,
                    "Enter — выбрать · Esc — назад",
                    "Enter — choose · Esc — back",
                )
            return _label(
                self.language,
                "Enter — выбрать · F5 — проверить · F2 — дополнительно · Esc — назад",
                "Enter — choose · F5 — verify · F2 — advanced · Esc — back",
            )

        def on_mount(self) -> None:
            # The record read may touch keyring/process/runtime state. Focus the
            # action list immediately and load the detail snapshot off-thread.
            with contextlib.suppress(Exception):
                self.query_one("#cd-status", Static).update(
                    _label(self.language, "Загружаем состояние…", "Loading state…")
                )
                self.query_one("#cd-actions", OptionList).focus()
            self._schedule_detail_refresh()

        def on_unmount(self) -> None:
            self._refresh_epoch += 1

        def _schedule_detail_refresh(self, *, select: Optional[str] = None) -> None:
            self._refresh_epoch += 1
            epoch = self._refresh_epoch

            def execute() -> None:
                self._read()
                with contextlib.suppress(Exception):
                    self.app.call_from_thread(self._detail_refresh_done, epoch, select)

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="connection-detail-refresh",
            )

        def _detail_refresh_done(self, epoch: int, select: Optional[str]) -> None:
            if epoch != self._refresh_epoch or not self.is_mounted:
                return
            self._loaded = True
            self.refresh_view(select=select, read=False)

        def on_resize(self, _event: Any = None) -> None:
            with contextlib.suppress(Exception):
                self.query_one("#cd-hint", Static).update(self._hint())

        def refresh_view(
            self,
            *,
            select: Optional[str] = None,
            read: bool = True,
        ) -> None:
            """Re-read and redraw, keeping the cursor on the same action.

            B5. Selection survives verify, save, enable and disable. Landing
            back on the top of a five-item list after every action is how a
            person deletes the wrong thing.
            """

            if read:
                self._read()
                self._loaded = True
            english = self._english()
            options = self.query_one("#cd-actions", OptionList)
            previous = select
            if previous is None:
                highlighted = options.highlighted
                if highlighted is not None and highlighted < len(options.options):
                    previous = getattr(
                        options.get_option_at_index(highlighted), "id", None
                    )

            with contextlib.suppress(Exception):
                self.query_one("#cd-title", Static).update(
                    detail_title(self._display_name, self._detail)
                )
            status = self.query_one("#cd-status", Static)
            status.update(
                _label(self.language, "Состояние:", "Status:")
                + " "
                + hub_status_words(self._status, english)
            )
            status.set_class(self._status == HUB_STATUS_WORKING, "status-ok")
            status.set_class(self._status == HUB_STATUS_ATTENTION, "status-warn")
            status.set_class(self._status == HUB_STATUS_ERROR, "status-error")
            status.set_class(self._status == HUB_STATUS_DISABLED, "status-off")
            # Only an address that genuinely exists. A placeholder here reads as
            # a URL the user could paste somewhere, and it would not work.
            with contextlib.suppress(Exception):
                self.query_one("#cd-endpoint", Static).update(self._endpoint)
            with contextlib.suppress(Exception):
                self.query_one("#cd-note", Static).update(
                    _label(self.language, "Ключ:", "Key:")
                    + " "
                    + secret_display(self._has_secret, english)
                    if self._has_secret
                    else ""
                )

            actions = detail_actions(
                self._enabled,
                kind=self.kind,
                has_secret=self._has_secret,
                running=self._bridge_running if self.kind == "service" else None,
            )
            options.clear_options()
            for action in actions:
                options.add_option(
                    Option(detail_action_words(action, english), id=action)
                )
            index = actions.index(previous) if previous in actions else 0
            options.highlighted = index

        def _set_error(self, message: str) -> None:
            # Through the shared redaction boundary. A controller or transport
            # error may carry a bearer token or a whole request in its message,
            # and this is the last hop before a widget.
            with contextlib.suppress(Exception):
                self.query_one("#cd-error", Static).update(
                    str(redact(message)) if message else ""
                )

        def status_key(self) -> str:
            return self._status

        def rendered_text(self) -> str:
            """Everything on screen as one string, for a leak assertion."""

            parts: List[str] = []
            for selector in (
                "#cd-title",
                "#cd-status",
                "#cd-endpoint",
                "#cd-note",
                "#cd-error",
                "#cd-hint",
            ):
                with contextlib.suppress(Exception):
                    parts.append(str(self.query_one(selector, Static).render()))
            with contextlib.suppress(Exception):
                options = self.query_one("#cd-actions", OptionList)
                for index in range(len(options.options)):
                    parts.append(str(options.get_option_at_index(index).prompt))
            return "\n".join(parts)

        # --------------------------------------------------------------- actions

        def _step(self, delta: int) -> None:
            options = self.query_one("#cd-actions", OptionList)
            count = len(options.options)
            if not count:
                return
            index = options.highlighted if options.highlighted is not None else 0
            options.highlighted = max(0, min(count - 1, index + delta))

        def action_previous(self) -> None:
            self._step(-1)

        def action_next(self) -> None:
            self._step(1)

        def action_cancel(self) -> None:
            """Esc goes back exactly one level, to the hub that opened this."""

            self.dismiss(None)

        def _highlighted(self) -> Optional[str]:
            options = self.query_one("#cd-actions", OptionList)
            highlighted = options.highlighted
            if highlighted is None or highlighted >= len(options.options):
                return None
            return getattr(options.get_option_at_index(highlighted), "id", None)

        def action_choose(self) -> None:
            self.run_action_id(self._highlighted())

        @on(OptionList.OptionSelected, "#cd-actions")
        def _selected(self, event: OptionList.OptionSelected) -> None:
            self.run_action_id(getattr(event.option, "id", None))

        def run_action_id(self, action: Optional[str]) -> None:
            """The one production entry point for every action on this screen.

            Tests drive this rather than a key or a row index, so reordering the
            action list cannot silently repoint a test at a different action --
            and, more to the point, cannot repoint Delete at Disable.
            """

            if not action:
                return
            if action != DETAIL_BACK and not self._loaded:
                # The detail snapshot is still being read in the background.
                # Back/Esc always works; mutating actions wait for real state.
                return
            if action == DETAIL_BACK:
                self.dismiss(None)
            elif action == DETAIL_VERIFY:
                self.action_verify()
            elif action == DETAIL_RECOVER:
                self.action_recover()
            elif action == DETAIL_RESTART:
                self.action_restart()
            elif action == DETAIL_EDIT:
                self.action_edit()
            elif action == DETAIL_DISABLE:
                self.action_disable()
            elif action == DETAIL_ENABLE:
                self.action_enable()
            elif action == DETAIL_DELETE:
                self.action_delete()
            elif action == DETAIL_COPY_AUTH:
                self.action_copy_auth()

        def action_recover(self) -> None:
            self._run_service_lifecycle(restart=False)

        def action_restart(self) -> None:
            self._run_service_lifecycle(restart=True)

        def _run_service_lifecycle(self, *, restart: bool) -> None:
            if self._gone or self.kind != "service" or not self._enabled:
                return
            self._set_error(
                _label(
                    self.language,
                    "Перезапускаем подключение…" if restart else "Восстанавливаем подключение…",
                    "Restarting connection…" if restart else "Repairing connection…",
                )
            )
            identity = self.identity

            def execute() -> None:
                try:
                    launch = self._controller.restart(identity) if restart else self._controller.repair(identity)
                    error = None if launch.success else "managed launcher reported failure"
                except (ConnectionError, ConnectionLaunchError, ConnectionRuntimeError, CredentialError) as exc:
                    error = str(exc)
                with contextlib.suppress(Exception):
                    self.app.call_from_thread(self._service_lifecycle_done, restart, error)

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="connection-detail-lifecycle",
            )

        def _service_lifecycle_done(self, restart: bool, error: Optional[str]) -> None:
            if not self.is_mounted:
                return
            if error:
                self._set_error(error)
                self._schedule_detail_refresh(
                    select=DETAIL_RESTART if restart else DETAIL_RECOVER
                )
                return
            self._set_error(
                _label(
                    self.language,
                    "Подключение перезапущено." if restart else "Подключение восстановлено.",
                    "Connection restarted." if restart else "Connection repaired.",
                )
            )
            self._schedule_detail_refresh(select=DETAIL_RESTART)

        def action_verify(self) -> None:
            """Re-check this record through the existing verify path.

            Never starts or restarts anything to do it: a healthy bridge keeps
            the address the user already pasted into a service. A second press
            while a check is in flight is ignored rather than launching a
            parallel probe -- two answers racing to write one status is how a
            failure gets overwritten by a stale success.
            """

            if self._checking or self._gone:
                return
            self._checking = True
            self._set_error("")
            with contextlib.suppress(Exception):
                self.query_one("#cd-status", Static).update(
                    _label(self.language, "Состояние: Проверяется", "Status: Checking")
                )
            kind, identity = self.kind, self.identity

            preset_id = self._preset_id

            def execute() -> None:
                try:
                    if kind == "provider":
                        result = dict(self._providers.test_provider(identity))
                    else:
                        result = dict(
                            self._controller.test(identity, timeout_seconds=15.0)
                        )
                except Exception as exc:
                    self.app.call_from_thread(
                        self._verified,
                        {"state": "failed", "detail": str(redact(str(exc)))},
                        preset_id,
                    )
                    return
                self.app.call_from_thread(self._verified, result, preset_id)

            self.app.run_worker(execute, thread=True, exclusive=True, group="cd-verify")

        def verified_status(self, result: Mapping[str, Any], preset_id: str) -> str:
            """Turn a controller result into one honest word.

            B5 audit 5.4. The defect this closes: the worker treated "no
            exception was raised" as success. Every interesting failure the
            controller has arrives as a *returned value* -- a failed handshake,
            a bridge that is up while the external service has never called it,
            a public URL that does not exist yet. All of those returned
            normally, and all of them were painted green.

            B3 already owns this judgement for services, including the
            distinction between a local bridge answering and the external
            service being confirmed, so this delegates rather than inventing a
            second opinion that would disagree with the connect screen.
            """

            if self.kind == "service" and preset_id:
                service_status = service_status_after_check(preset_id, result)
                if service_status_is_proven(service_status):
                    return HUB_STATUS_WORKING
                if service_status in {SERVICE_ERROR, SERVICE_NOT_CONFIGURED}:
                    return HUB_STATUS_ERROR
                # bridge-only, awaiting, action-required: real, partial, and
                # explicitly not "works".
                return HUB_STATUS_ATTENTION
            state = str(result.get("state") or result.get("status") or "").lower()
            if state in {"ok", "success", "passed"}:
                return HUB_STATUS_WORKING
            if state in {"pending", "public_pending", "unconfirmed", "partial"}:
                return HUB_STATUS_ATTENTION
            return HUB_STATUS_ERROR

        def _verified(self, result: Mapping[str, Any], preset_id: str) -> None:
            """A failed check must clear the old green, not sit beside it.

            The last successful status is not evidence once a newer check has
            proved otherwise, so the outcome overwrites whatever was there.
            """

            if not self.is_mounted:
                return
            self._checking = False
            # The verification result is already newer evidence than another
            # synchronous runtime/keyring read. Apply it immediately; a later
            # explicit/background refresh may reconcile the resting fields.
            self._status = self.verified_status(result, preset_id)
            english = self._english()
            with contextlib.suppress(Exception):
                status = self.query_one("#cd-status", Static)
                status.update(
                    _label(self.language, "Состояние:", "Status:")
                    + " "
                    + hub_status_words(self._status, english)
                )
                status.set_class(self._status == HUB_STATUS_WORKING, "status-ok")
                status.set_class(self._status == HUB_STATUS_ATTENTION, "status-warn")
                status.set_class(self._status == HUB_STATUS_ERROR, "status-error")
                status.set_class(False, "status-off")
            if self._status == HUB_STATUS_WORKING:
                self._set_error("")
                return
            self._set_error(str(result.get("detail") or ""))

        def action_edit(self) -> None:
            """Hand off to the existing form for this record, by its own id.

            No new editor. A provider goes to the provider wizard it was
            created with and a service to its standard screen, both carrying
            the saved identity -- which is what stops a save from writing a
            second record beside the one being edited.
            """

            if self._gone:
                return
            self.dismiss(f"edit:{self.kind}:{self.identity}")

        def action_advanced(self) -> None:
            """B4's advanced layer, unchanged, reached from the same key."""

            if self._gone:
                return
            preset_id = self.advanced_preset_id()
            if not preset_id:
                # Fail-soft rather than guess. Opening ClickUp's advanced panel
                # for an unknown record would show the wrong permissions and
                # the wrong limits, and a person acting on them would be acting
                # on a service they are not looking at.
                self._set_error(
                    _label(
                        self.language,
                        "Дополнительные настройки недоступны для этой записи.",
                        "Advanced settings are not available for this record.",
                    )
                )
                return
            # B5 audit 5.6/3. Two things this passes that it previously did
            # not: the record's *own* values, so the form shows what is saved
            # rather than what the catalogue suggests, and a callback that
            # actually writes.
            self.app.push_screen(
                AdvancedSettingsScreen(
                    self.language,
                    kind=self.kind,
                    preset_id=preset_id,
                    values=self.advanced_values(),
                    has_secret=self._has_secret,
                    bridge_running=self._bridge_running,
                    apply=self._apply_advanced,
                ),
                lambda _values: self._schedule_detail_refresh(),
            )

        def advanced_values(self) -> Dict[str, str]:
            """The values from the last already-loaded detail snapshot."""

            # Do not re-enter provider/keyring/runtime I/O from a button press.
            # The detail screen loads these together with the rest of the record.
            return dict(self._advanced_values_cache)

        def _apply_advanced(self, values: Mapping[str, str]) -> None:
            """The production mutation behind Advanced Save, for this record."""

            if self.kind == "provider":
                apply_provider_advanced(self._providers, self.identity, values)
                return
            apply_service_advanced(self._controller, self.identity, values)

        def advanced_preset_id(self) -> str:
            """Which preset the advanced layer should be opened for.

            B5 audit 5.3. This used to be ``self.identity if provider else
            "clickup"`` -- so opening Advanced on a saved ChatGPT or Claude
            connection showed *ClickUp's* fields. A literal fallback that
            happens to name a real service is worse than no fallback: it is
            indistinguishable from a correct answer on screen.

            The value now comes from the saved record. An empty string means
            "do not know", and the caller declines rather than substituting.
            """

            if self.kind == "provider":
                return self.identity
            return self._preset_id

        def action_disable(self) -> None:
            """Ask first, and say what it costs, before switching anything off.

            Disable is reversible and delete is not, so this confirmation is
            about *clarity* rather than danger: a person disabling the model
            their next task would have used deserves to be told that before it
            happens, not after the task fails to start.
            """

            if self._gone or not self._enabled:
                return
            self.app.push_screen(
                _ConfirmScreen(
                    _label(
                        self.language,
                        f"Отключить {detail_title(self._display_name, self._detail)}?",
                        f"Disable {detail_title(self._display_name, self._detail)}?",
                    ),
                    active_disable_warning(
                        self.kind, self._bridge_running, self._english()
                    ),
                    language=self.language,
                ),
                self._disable_confirmed,
            )

        def _disable_confirmed(self, confirmed: Optional[bool]) -> None:
            """Declining leaves the record, the config and any process alone."""

            if not confirmed:
                return
            self._set_enabled(False)

        def action_enable(self) -> None:
            """Put the record back in service. Asks for no secret and starts nothing.

            The saved configuration and the saved credential are both still
            there -- that is what disable preserved -- so enabling is a single
            flag write. It does not become "working": the status afterwards is
            whatever the runtime honestly reports, and only a check the user
            runs can turn that green.
            """

            if self._gone or self._enabled:
                return
            self._set_enabled(True)

        def _set_enabled(self, enabled: bool) -> None:
            self._set_error(_label(self.language, "Применяем…", "Applying…"))

            def execute() -> None:
                error: Optional[str] = None
                try:
                    if self.kind == "provider":
                        self._providers.set_provider_enabled(self.identity, enabled)
                    else:
                        self._controller.set_enabled(self.identity, enabled)
                except Exception as exc:
                    error = str(exc)
                with contextlib.suppress(Exception):
                    self.app.call_from_thread(self._set_enabled_done, enabled, error)

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="connection-detail-enable",
            )

        def _set_enabled_done(self, enabled: bool, error: Optional[str]) -> None:
            if not self.is_mounted:
                return
            if error:
                self._set_error(error)
                self._schedule_detail_refresh()
                return
            self._set_error("")
            self._schedule_detail_refresh(
                select=DETAIL_DISABLE if enabled else DETAIL_ENABLE
            )

        def action_copy_auth(self) -> None:
            """Copy the bridge ``Authorization`` value, never the raw secret.

            The external client (ClickUp, ChatGPT) sends this as
            ``Authorization: Bearer <secret>``.  The clipboard receives the
            full ``Bearer <secret>`` string so the user pastes exactly the
            right header value without retyping a prefix; the secret itself is
            never rendered, never printed, and auto-clears from the clipboard
            in 120 seconds.  Confirmation shows the fingerprint (safe) so the
            user can match it against what the client expects.
            """
            from .connections import resolve_connection_secret

            if self._gone or self.kind != "service" or not self._has_secret:
                return
            def execute() -> None:
                try:
                    target = self._controller.get(self.identity).target
                    secret = resolve_connection_secret(target)
                    safe_fingerprint = CredentialStore.fingerprint(secret)
                    error: Optional[str] = None
                except Exception as exc:
                    secret = ""
                    safe_fingerprint = ""
                    error = str(exc)
                with contextlib.suppress(Exception):
                    self.app.call_from_thread(
                        self._copy_auth_ready,
                        secret,
                        safe_fingerprint,
                        error,
                    )

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="connection-detail-copy-auth",
            )

        def _copy_auth_ready(
            self,
            secret: str,
            safe_fingerprint: str,
            error: Optional[str],
        ) -> None:
            if not self.is_mounted:
                return
            if error:
                self._set_error(error)
                return
            body = _label(
                self.language,
                f"Скопировать Bearer-ключ в буфер обмена?\n"
                f"Фингерпринт: {safe_fingerprint}\n"
                f"Буфер очистится через 120 секунд.",
                f"Copy the Bearer key to the clipboard?\n"
                f"Fingerprint: {safe_fingerprint}\n"
                f"Clipboard clears in 120 seconds.",
            )
            self.app.push_screen(
                _ConfirmScreen(
                    _label(
                        self.language,
                        "Скопировать ключ авторизации",
                        "Copy authorization key",
                    ),
                    body,
                    language=self.language,
                ),
                lambda confirmed: self._copy_auth_confirmed(confirmed, secret),
            )

        def _copy_auth_confirmed(
            self, confirmed: Optional[bool], secret: str
        ) -> None:
            if not confirmed:
                return

            def execute() -> None:
                from . import clipboard as _clipboard

                bearer = _clipboard.bearer_value(secret)
                copied = _clipboard.write_text(bearer)
                _clipboard.schedule_clear()
                with contextlib.suppress(Exception):
                    self.app.call_from_thread(self._copy_auth_written, copied)

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="connection-detail-clipboard",
            )

        def _copy_auth_written(self, copied: bool) -> None:
            if not self.is_mounted:
                return
            if copied:
                self._set_error(
                    _label(
                        self.language,
                        "Bearer-ключ скопирован. Буфер очистится через 120 секунд.",
                        "Bearer key copied. Clipboard clears in 120 seconds.",
                    )
                )
            else:
                # Clipboard unavailable: the secret stays in the keyring and
                # is never printed.
                self._set_error(
                    _label(
                        self.language,
                        "Буфер обмена недоступен. Ключ сохранён в OS keyring и не выведен.",
                        "Clipboard unavailable. The key stays in the OS keyring and is not printed.",
                    )
                )

        def action_delete(self) -> None:
            """The first Delete only ever opens a confirmation.

            Nothing is removed here -- not the config, not the secret. The
            guard below is what makes a second Enter on an already-answered
            confirmation harmless: a delete that is in flight or has already
            succeeded cannot be started again.
            """

            if self._gone or self._deleting:
                return
            title, body = delete_confirmation(
                self._display_name, self._detail, self._english()
            )
            note = credential_deletion_note(self._has_secret, self._english())
            # Delete, not disable. The production remover stops a live runtime
            # before removing the record, so this must say so.
            warning = active_delete_warning(
                self.kind, self._bridge_running, self._english()
            )
            self.app.push_screen(
                _ConfirmScreen(
                    title,
                    "\n\n".join(part for part in (body, note, warning) if part),
                    language=self.language,
                ),
                self._delete_confirmed,
            )

        def _delete_confirmed(self, confirmed: Optional[bool]) -> None:
            """Cancel changes nothing at all. Confirm applies exactly once."""

            if not confirmed or self._deleting or self._gone:
                return
            self._deleting = True
            self._set_error(_label(self.language, "Удаляю…", "Deleting…"))

            def execute() -> None:
                error: Optional[str] = None
                try:
                    if self.kind == "provider":
                        self._providers.remove_provider(
                            self.identity,
                            cascade=True,
                            delete_credential=True,
                        )
                    else:
                        self._controller.remove(self.identity)
                except Exception as exc:
                    error = str(exc)
                with contextlib.suppress(Exception):
                    self.app.call_from_thread(self._delete_record_done, error)

            self.app.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="connection-detail-delete",
            )

        def _delete_record_done(self, error: Optional[str]) -> None:
            if not self.is_mounted:
                return
            if error:
                self._deleting = False
                self._set_error(error)
                self._schedule_detail_refresh(select=DETAIL_DELETE)
                return
            self._gone = True
            self.dismiss(f"deleted:{self.kind}:{self.identity}")

    return {
        "ConnectionHubScreen": ConnectionHubScreen,
        "ConnectionDetailScreen": ConnectionDetailScreen,
        "McpClientsScreen": McpClientsScreen,
        "ModelProvidersScreen": ModelProvidersScreen,
        "ServicePickerScreen": ServicePickerScreen,
        "_ClickupAutoScreen": _ClickupAutoScreen,
        "_ClickupResultScreen": _ClickupResultScreen,
        "ServiceConnectScreen": ServiceConnectScreen,
        "SavedWebProfileSettingsScreen": SavedWebProfileSettingsScreen,
        "BrowserCredentialManagerScreen": BrowserCredentialManagerScreen,
        "ServiceMoreScreen": ServiceMoreScreen,
        "ServiceDiagnosticsScreen": ServiceDiagnosticsScreen,
        "AdvancedSettingsScreen": AdvancedSettingsScreen,
        "PermissionDetailScreen": PermissionDetailScreen,
    }


__all__ = ["build_connections_screens"]
