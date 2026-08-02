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

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .connections import (
    CONNECTION_AUTH_SCHEMES,
    CONNECTION_TUNNELS,
    ConnectionConfigurationError,
    ConnectionCredentialStore,
    ConnectionError,
    McpClientTarget,
    connection_registry,
    mcp_client_preset,
    mcp_client_presets,
    resolve_clickup_defaults,

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
from .credentials import CredentialError, CredentialStore
from .provider_controller import ProviderController
from .security import redact

try:  # pragma: no cover - imports mirror tui.py's guarded block
    from textual import on
    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import Button, Input, Label, OptionList, RadioSet, Static
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
        "test": "Test",
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
# Hub: Connections → MCP Clients / Model Providers / new provider.
# ---------------------------------------------------------------------------


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
        """Top-level Connections hub: MCP clients, model providers, new API provider."""

        BINDINGS = [
            Binding("1", "mcp_clients", _C["en"]["mcp_clients"], priority=True),
            Binding("2", "model_providers", _C["en"]["model_providers"], priority=True),
            Binding("3", "new_provider", _C["en"]["new_provider"], priority=True),
            Binding("escape", "cancel", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        ConnectionHubScreen { align: center middle; background: #0e0c08 92%; }
        #connhub-dialog { width: 78; height: auto; background: #1a1712;
          border: round #c6a56b; padding: 1 2; }
        #connhub-dialog .title { text-style: bold; color: #e5e5e5; margin-bottom: 1; }
        #connhub-dialog .hint { color: #8a7e6a; margin-bottom: 1; }
        #connhub-dialog Button { width: 100%; margin-bottom: 1; text-align: left; }
        """

        def __init__(self, language: str = "ru") -> None:
            super().__init__()
            self.language = language

        def compose(self) -> ComposeResult:
            with Vertical(id="connhub-dialog"):
                yield Static(_label(self.language, _C["ru"]["hub_title"], _C["en"]["hub_title"]), classes="title")
                yield Static(_t(self.language, "hub_hint"), classes="hint")
                yield Button(f"[1] {_t(self.language, 'mcp_clients')}", id="connhub-mcp")
                yield Button(f"[2] {_t(self.language, 'model_providers')}", id="connhub-providers")
                yield Button(f"[3] {_t(self.language, 'new_provider')}", id="connhub-new-provider")
                yield Button(_t(self.language, "cancel"), id="connhub-cancel")

        def on_mount(self) -> None:
            self.query_one("#connhub-mcp", Button).focus()

        def action_mcp_clients(self) -> None:
            self.dismiss("mcp_clients")

        def action_model_providers(self) -> None:
            self.dismiss("model_providers")

        def action_new_provider(self) -> None:
            self.dismiss("new_provider")

        def action_cancel(self) -> None:
            self.dismiss(None)

        @on(Button.Pressed)
        def _pressed(self, event: Button.Pressed) -> None:
            mapping = {
                "connhub-mcp": "mcp_clients",
                "connhub-providers": "model_providers",
                "connhub-new-provider": "new_provider",
                "connhub-cancel": None,
            }
            if event.button.id in mapping:
                self.dismiss(mapping[event.button.id])

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
            Binding("up", "previous", show=False, priority=True),
            Binding("down", "next", show=False, priority=True),
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
                with Horizontal(id="mcp-list-actions"):
                    yield Button(self._label("Добавить", "Add"), id="mcp-add")
                    yield Button(self._label("Изменить", "Edit"), id="mcp-edit")
                    yield Button(self._label("Проверить", "Test"), id="mcp-test")
                    yield Button(self._label("Копировать URL", "Copy URL"), id="mcp-copy-url")
                    yield Button(self._label("Копировать секрет", "Copy secret"), id="mcp-copy-secret")
                    yield Button(self._label("Запустить / перезапустить", "Start / restart"), id="mcp-start")
                    yield Button(self._label("Остановить", "Stop"), id="mcp-stop")
                with Horizontal(id="mcp-list-buttons"):
                    yield Button(self._label("Закрыть", "Close"), id="mcp-close")

        def on_mount(self) -> None:
            self._refresh()
            # Runtime state can change outside this screen: a background start
            # worker may finish, the CLI may stop a bridge, or a managed process
            # may exit. Reconcile rows with the runtime registry while open.
            self.set_interval(0.5, self._refresh)
            self.query_one("#mcp-connections", OptionList).focus()

        def _refresh(self) -> None:
            options = self.query_one("#mcp-connections", OptionList)
            selected_id: Optional[str] = None
            highlighted = options.highlighted
            if highlighted is not None and highlighted < len(options.options):
                selected_id = getattr(
                    options.get_option_at_index(highlighted), "id", None
                )

            items = self._items()
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
                return self._controller.get(cid).target
            except (ConnectionError, ConnectionRuntimeError):
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
            self._refresh()
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
            try:
                self._controller.remove(target.connection_id)
            except (ConnectionError, CredentialError, ConnectionRuntimeError) as exc:
                self._set_error(str(exc))
                return
            self._refresh()
            self.query_one("#mcp-connections", OptionList).focus()

        def action_test(self) -> None:
            target = self._selected()
            if target is None:
                self._set_error(self._label("Выберите подключение.", "Select a connection."))
                return
            try:
                state = self._controller.get(target.connection_id)
            except (ConnectionError, ConnectionRuntimeError) as exc:
                self._set_error(str(exc))
                return
            if state.endpoint is None:
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

            self.run_worker(execute, thread=True, exclusive=True, group="mcp-test")

        def _test_done(self, result: Dict[str, Any]) -> None:
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
            try:
                url = self._controller.get(target.connection_id).endpoint
            except (ConnectionError, ConnectionRuntimeError) as exc:
                self._set_error(str(exc))
                return
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
            try:
                secret = self._controller.secret(target.connection_id)
            except (ConnectionError, CredentialError) as exc:
                self._set_error(str(exc))
                return
            copy_text(secret)
            self.app.notify(_t(self.language, "copied"))

        def action_start(self) -> None:
            target = self._selected()
            if target is None:
                self._set_error(self._label("Выберите подключение.", "Select a connection."))
                return
            try:
                state = self._controller.status(target.connection_id)["state"]
            except (ConnectionError, ConnectionRuntimeError) as exc:
                self._set_error(str(exc))
                return

            self._set_error(
                self._label(
                    "Перезапускаю сохранённое подключение…"
                    if state in {"running", "degraded"}
                    else "Запускаю сохранённое подключение…",
                    "Restarting saved connection…"
                    if state in {"running", "degraded"}
                    else "Starting saved connection…",
                )
            )

            def execute() -> None:
                try:
                    launch = (
                        self._controller.restart(target.connection_id)
                        if state in {"running", "degraded"}
                        else self._controller.start(target.connection_id)
                    )
                except (
                    ConnectionError,
                    ConnectionLaunchError,
                    ConnectionRuntimeError,
                    CredentialError,
                ) as exc:
                    self.app.call_from_thread(self._start_failed, str(exc))
                    return
                self.app.call_from_thread(self._start_done, launch)

            self.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group="mcp-connection-start",
            )

        def _start_failed(self, message: str) -> None:
            self._set_error(
                self._label("Ошибка запуска: ", "Start failed: ") + message
            )
            self._refresh()

        def _start_done(self, launch: ManagedConnectionLaunch) -> None:
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
            self._refresh()
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
            try:
                before = self._controller.status(target.connection_id)
                if before["state"] in {"configured_not_running", "stopped"}:
                    self._set_error(
                        self._label("Подключение уже остановлено.", "The connection is already stopped."),
                        ok=True,
                    )
                    return
                result = self._controller.stop(target.connection_id)
            except (ConnectionError, ConnectionRuntimeError) as exc:
                self._set_error(str(exc))
                return
            self._set_error(
                self._label(
                    f"Остановлено: {result['state']}.",
                    f"Stopped: {result['state']}.",
                ),
                ok=True,
            )
            self._refresh()

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
                    yield Input(value=str(self.target.port if self.target else 8765), id="mcf-port")
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
            secret_value = self.query_one("#mcf-secret", Input).value

            from time import time as _now
            from .connections import _new_connection_id

            # Credential identity follows the immutable connection ID, never the
            # editable display name.  Two similarly named connections therefore
            # cannot overwrite each other's keyring entry.
            connection_id = self.target.connection_id if self.target else _new_connection_id()
            created_at = self.target.created_at if self.target else _now()

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
                else:
                    try:
                        info = store.set(connection_id, secret_value or None)
                        credential_ref = info["reference"]
                        credential_fingerprint = info["fingerprint"]
                    except (CredentialError, ConnectionConfigurationError) as exc:
                        self._set_status(str(exc))
                        return
            try:
                target = McpClientTarget(
                    connection_id=connection_id,
                    name=name,
                    preset_id=self.preset_id,
                    transport=transport,
                    endpoint_path=endpoint,
                    auth_scheme=scheme,
                    tunnel=tunnel,
                    runtime_profile=mcp_client_preset(self.preset_id).runtime_profile,
                    url_stability=stability,
                    public_url=public_url,
                    header_name=header_name,
                    header_prefix=header_prefix,
                    description=description,
                    instructions=instructions,
                    credential_ref=credential_ref,
                    credential_fingerprint=credential_fingerprint,
                    port=port,
                    created_at=created_at,
                    updated_at=_now(),
                )
            except (ConnectionConfigurationError, ValueError) as exc:
                self._set_status(str(exc))
                return

            try:
                connection_registry().put(target)
            except ConnectionError as exc:
                # A new secret is written before the atomic registry update. If
                # that update fails, remove the just-created keyring entry so a
                # failed save cannot leave an orphaned credential.
                if self.target is None and credential_ref and credential_ref.startswith("os-keyring:connection/"):
                    try:
                        ConnectionCredentialStore().delete(connection_id)
                    except CredentialError:
                        pass
                self._set_status(str(exc))
                return
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
            # ``enter`` starts the automatic setup; ``escape`` cancels.  Letter
            # keys (a/r) would collide with the name ``Input`` when it has
            # focus, so Advanced/Reset are exposed as buttons instead of
            # bindings, and only enter/escape remain as priority bindings.
            Binding("enter", "connect", _C["en"]["cu_connect"], priority=True),
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
                import os

                repo = Path(os.getcwd())
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
                with Horizontal(id="cu-result-actions"):
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
            Binding("enter", "set_active", _C["en"]["set_active"], priority=True),
            Binding("up", "previous", show=False, priority=True),
            Binding("down", "next", show=False, priority=True),
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
            details = self._controller.details(self.provider_id)
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
            with Vertical(id="provider-details-dialog"):
                yield Static(
                    self._label("Провайдер и модели", "Provider and models"),
                    classes="title",
                )
                yield Static(summary, id="provider-details-summary", markup=False)
                yield OptionList(id="provider-details-models")
                yield Static("", id="provider-details-error", markup=False)
                with Horizontal(id="provider-details-actions"):
                    yield Button(
                        self._label("Сделать модель активной", "Set model active"),
                        id="provider-details-active",
                    )
                    yield Button(self._label("Закрыть", "Close"), id="provider-details-close")

        def on_mount(self) -> None:
            self._refresh_models()
            self.query_one("#provider-details-models", OptionList).focus()

        def _refresh_models(self) -> None:
            details = self._controller.details(self.provider_id)
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
            self._refresh_models()
            self.query_one("#provider-details-error", Static).update(
                self._label("Модель активирована.", "Model activated.")
            )

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
            provider = self._controller.details(self.provider_id).provider
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
            Binding("enter", "details", "Details", priority=True),
            Binding("a", "add", _C["en"]["add"], priority=True),
            Binding("e", "edit", _C["en"]["edit"], priority=True),
            Binding("t", "test", _C["en"]["test"], priority=True),
            Binding("c", "copy_secret", _C["en"]["copy_secret"], priority=True),
            Binding("up", "previous", show=False, priority=True),
            Binding("down", "next", show=False, priority=True),
            Binding("delete", "delete", _C["en"]["delete"], priority=True),
            Binding("escape", "cancel", _C["en"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        ModelProvidersScreen { align: center middle; background: #0e0c08 92%; }
        #mp-list-dialog { width: 86; height: 86%; background: #1a1712;
          border: round #c6a56b; padding: 1 2; }
        #mp-list-dialog .title { text-style: bold; color: #e5e5e5; }
        #mp-list-dialog .hint { color: #8a7e6a; margin-bottom: 1; }
        #mp-providers { height: 1fr; border: round #4a4338; }
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
            with Vertical(id="mp-list-dialog"):
                yield Static(self._label("API-провайдеры", "API providers"), classes="title")
                yield Static(_t(self.language, "model_providers_hint"), classes="hint")
                yield OptionList(id="mp-providers")
                yield Static("", id="mp-list-error", markup=False)
                with Horizontal(id="mp-list-actions"):
                    yield Button(self._label("Подробности", "Details"), id="mp-details")
                    yield Button(self._label("Добавить", "Add"), id="mp-add")
                    yield Button(self._label("Изменить", "Edit"), id="mp-edit")
                    yield Button(self._label("Проверить", "Test"), id="mp-test")
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
            Binding("enter", "yes", _C["ru"]["save"], priority=True),
            Binding("escape", "no", _C["ru"]["cancel"], priority=True),
        ]
        DEFAULT_CSS = """
        _ConfirmScreen { align: center middle; background: #0e0c08 92%; }
        #cc-confirm-dialog { width: 70; height: auto; max-height: 80%;
          background: #1a1712; border: round #c6a56b; padding: 1 2; }
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
            with Vertical(id="cc-confirm-dialog"):
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

    return {
        "ConnectionHubScreen": ConnectionHubScreen,
        "McpClientsScreen": McpClientsScreen,
        "ModelProvidersScreen": ModelProvidersScreen,
    }


__all__ = ["build_connections_screens"]
