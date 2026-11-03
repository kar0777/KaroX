"""The two forms that need more than one line: a custom endpoint and an MCP server.

Everything else -- choosing a model, a session, a catalog provider, an MCP
action -- is an inline list above the composer. A form returns one value and
the app decides what it means; no form mutates app state directly.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from .backend import BackendError, CLIBackend
from .i18n import tr
from .widgets import MUTED, RED


class ProviderSetupScreen(ModalScreen[dict[str, Any] | None]):
    """Pick a preset, enter the endpoint/key/model, verify live, then save."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, backend: CLIBackend, language: str) -> None:
        super().__init__()
        self.backend = backend
        self.language = language
        self._presets: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="modal form-modal"):
            yield Static(tr(self.language, "provider.title"), classes="modal-title")
            yield Static(tr(self.language, "provider.preset"), classes="field-label")
            yield Select([], prompt="…", id="preset", allow_blank=True)
            yield Static(tr(self.language, "provider.id"), classes="field-label")
            yield Input(id="provider-id")
            yield Static(tr(self.language, "provider.base_url"), classes="field-label")
            yield Input(id="base-url")
            yield Static(tr(self.language, "provider.model"), classes="field-label")
            yield Input(id="model")
            yield Static(tr(self.language, "provider.key"), classes="field-label")
            yield Input(id="api-key", password=True)
            yield Static("", id="provider-error", classes="form-error")
            with Horizontal(classes="modal-buttons"):
                yield Button(
                    tr(self.language, "provider.save"), variant="primary", id="save"
                )
                yield Button(tr(self.language, "provider.cancel"), id="cancel")
            yield Static("", id="provider-status", classes="modal-hint")

    def on_mount(self) -> None:
        try:
            presets = [
                item
                for item in self.backend.provider_presets()
                if item.get("installable")
            ]
        except BackendError as exc:
            self._error(str(exc))
            presets = []
        self._presets = {
            str(item.get("preset_id")): item for item in presets if item.get("preset_id")
        }
        if not self._presets:
            self._error(tr(self.language, "provider.no_presets"))
            return
        select = self.query_one("#preset", Select)
        select.set_options(
            [
                (str(item.get("display_name") or key), key)
                for key, item in self._presets.items()
            ]
        )
        if self._presets:
            first = next(iter(self._presets))
            select.value = first
            self._apply_preset(first)
        self.query_one("#model", Input).focus()

    @on(Select.Changed, "#preset")
    def preset_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self._apply_preset(event.value)

    def _apply_preset(self, preset_id: str) -> None:
        preset = self._presets.get(preset_id) or {}
        provider = self.query_one("#provider-id", Input)
        base_url = self.query_one("#base-url", Input)
        if not provider.value or provider.value == getattr(self, "_last_preset", ""):
            provider.value = preset_id
        self._last_preset = preset_id
        preset_url = preset.get("base_url")
        if isinstance(preset_url, str):
            base_url.value = preset_url

    @on(Button.Pressed, "#cancel")
    def cancel_pressed(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def save_pressed(self) -> None:
        self._save()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save(self) -> None:
        preset = self.query_one("#preset", Select).value
        provider_id = self.query_one("#provider-id", Input).value.strip()
        base_url = self.query_one("#base-url", Input).value.strip()
        model = self.query_one("#model", Input).value.strip()
        api_key = self.query_one("#api-key", Input).value.strip()
        if not isinstance(preset, str) or not model:
            self._error(tr(self.language, "provider.required"))
            return
        selected = self._presets.get(preset, {})
        if not base_url and not selected.get("base_url"):
            self._error(tr(self.language, "provider.required"))
            return
        local = (base_url or str(selected.get("base_url") or "")).startswith(
            ("http://127.0.0.1", "http://localhost", "http://[::1]")
        )
        if not api_key and not local:
            self._error(tr(self.language, "provider.key_required"))
            return
        self._error("")
        self.query_one("#save", Button).disabled = True
        self.query_one("#provider-status", Static).update(
            Text(tr(self.language, "provider.testing"), style=MUTED)
        )
        self.run_worker(
            lambda: self._persist(preset, provider_id, base_url, model, api_key),
            thread=True,
            exclusive=True,
        )

    def _persist(
        self, preset: str, provider_id: str, base_url: str, model: str, api_key: str
    ) -> None:
        try:
            result = self.backend.provider_setup(
                preset,
                model_id=model,
                api_key=api_key,
                provider_id=provider_id,
                base_url=base_url,
            )
        except BackendError as exc:
            self.app.call_from_thread(self._failed, str(exc))
        else:
            self.app.call_from_thread(self.dismiss, result)

    def _failed(self, error: str) -> None:
        self.query_one("#save", Button).disabled = False
        self.query_one("#provider-status", Static).update("")
        self._error(tr(self.language, "provider.failed", error=error))

    def _error(self, message: str) -> None:
        widget = self.query_one("#provider-error", Static)
        widget.update(Text(message, style=RED))
        widget.display = bool(message)


class McpAddScreen(ModalScreen[str | None]):
    """Add one MCP server (stdio command or Streamable HTTP URL)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, backend: CLIBackend, language: str) -> None:
        super().__init__()
        self.backend = backend
        self.language = language

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="modal form-modal"):
            yield Static(tr(self.language, "mcp.add_title"), classes="modal-title")
            yield Static(tr(self.language, "mcp.server_id"), classes="field-label")
            yield Input(id="server-id")
            yield Static(tr(self.language, "mcp.namespace"), classes="field-label")
            yield Input(id="namespace")
            yield Static(tr(self.language, "mcp.transport"), classes="field-label")
            yield Select(
                [("stdio", "stdio"), ("streamable_http", "streamable_http")],
                value="stdio",
                allow_blank=False,
                id="transport",
            )
            yield Static(tr(self.language, "mcp.command"), classes="field-label")
            yield Input(id="command", placeholder="npx")
            yield Static(tr(self.language, "mcp.args"), classes="field-label")
            yield Input(id="arguments", placeholder="-y @modelcontextprotocol/server-filesystem .")
            yield Static(tr(self.language, "mcp.url"), classes="field-label")
            yield Input(id="url", placeholder="https://example.com/mcp")
            yield Static(tr(self.language, "mcp.env"), classes="field-label")
            yield Input(id="env", placeholder="TOKEN=...; REGION=eu")
            yield Static(tr(self.language, "mcp.headers"), classes="field-label")
            yield Input(id="headers", placeholder="X-API-Key=...")
            yield Static(tr(self.language, "mcp.credential_ref"), classes="field-label")
            yield Input(id="credential-ref", placeholder="os-keyring:mcp/github")
            yield Static(tr(self.language, "mcp.credential_target"), classes="field-label")
            yield Input(id="credential-target", placeholder="Authorization")
            yield Static(tr(self.language, "mcp.oauth"), classes="field-label")
            yield Select(
                [(tr(self.language, "form.no"), "no"), (tr(self.language, "form.yes"), "yes")],
                value="no",
                allow_blank=False,
                id="oauth",
            )
            yield Static(tr(self.language, "mcp.read_only"), classes="field-label")
            yield Input(id="read-only", placeholder="read_file,list_files")
            yield Static(tr(self.language, "mcp.timeout"), classes="field-label")
            yield Input(id="timeout", value="30")
            yield Static("", id="mcp-error", classes="form-error")
            with Horizontal(classes="modal-buttons"):
                yield Button(tr(self.language, "form.save"), variant="primary", id="save")
                yield Button(tr(self.language, "form.cancel"), id="cancel")
            yield Static("", id="mcp-status", classes="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#server-id", Input).focus()

    @on(Button.Pressed, "#cancel")
    def cancel_pressed(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def save_pressed(self) -> None:
        server_id = self.query_one("#server-id", Input).value.strip()
        namespace = self.query_one("#namespace", Input).value.strip() or server_id
        transport = str(self.query_one("#transport", Select).value or "stdio")
        command = self.query_one("#command", Input).value.strip()
        url = self.query_one("#url", Input).value.strip()
        raw_args = self.query_one("#arguments", Input).value.strip()
        if not server_id or not namespace:
            self._error(tr(self.language, "mcp.error_id"))
            return
        if transport == "stdio" and not command:
            self._error(tr(self.language, "mcp.error_command"))
            return
        if transport != "stdio" and not url:
            self._error(tr(self.language, "mcp.error_url"))
            return
        arguments = raw_args.split() if raw_args else []
        env = self.query_one("#env", Input).value.strip()
        headers = self.query_one("#headers", Input).value.strip()
        credential_ref = self.query_one("#credential-ref", Input).value.strip()
        credential_target = self.query_one("#credential-target", Input).value.strip()
        oauth = str(self.query_one("#oauth", Select).value or "no") == "yes"
        read_only = [
            item.strip()
            for item in self.query_one("#read-only", Input).value.split(",")
            if item.strip()
        ]
        try:
            timeout = float(self.query_one("#timeout", Input).value.strip() or "30")
        except ValueError:
            self._error(tr(self.language, "mcp.error_timeout"))
            return
        self.query_one("#save", Button).disabled = True
        self.query_one("#mcp-status", Static).update(Text(tr(self.language, "form.saving"), style=MUTED))
        self.run_worker(
            lambda: self._persist(
                server_id,
                namespace,
                transport,
                command,
                url,
                arguments,
                env,
                headers,
                credential_ref,
                credential_target,
                oauth,
                read_only,
                timeout,
            ),
            thread=True,
            exclusive=True,
        )

    def _persist(
        self,
        server_id: str,
        namespace: str,
        transport: str,
        command: str,
        url: str,
        arguments: list[str],
        env: str,
        headers: str,
        credential_ref: str,
        credential_target: str,
        oauth: bool,
        read_only: list[str],
        timeout: float,
    ) -> None:
        def pairs(value: str) -> list[tuple[str, str]]:
            result: list[tuple[str, str]] = []
            for item in value.split(";"):
                item = item.strip()
                if not item:
                    continue
                if "=" not in item:
                    raise BackendError(f"expected NAME=VALUE, got {item!r}")
                name, raw = item.split("=", 1)
                result.append((name.strip(), raw))
            return result

        try:
            self.backend.mcp_add(
                server_id=server_id,
                namespace=namespace,
                transport=transport,
                command=command,
                url=url,
                arguments=arguments,
                environment=pairs(env),
                headers=pairs(headers),
                credential_ref=credential_ref,
                credential_target=credential_target,
                oauth=oauth,
                read_only_tools=read_only,
                timeout_seconds=timeout,
            )
        except BackendError as exc:
            self.app.call_from_thread(self._failed, str(exc))
        else:
            self.app.call_from_thread(self.dismiss, server_id)

    def _failed(self, error: str) -> None:
        self.query_one("#save", Button).disabled = False
        self.query_one("#mcp-status", Static).update("")
        self._error(error)

    def _error(self, message: str) -> None:
        widget = self.query_one("#mcp-error", Static)
        widget.update(Text(message, style=RED))
        widget.display = bool(message)

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = ["McpAddScreen", "ProviderSetupScreen"]
