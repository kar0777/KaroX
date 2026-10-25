"""Modal surfaces: pick a model or session, connect a provider, manage MCP.

A modal returns one value and the app decides what it means. No modal mutates
app state directly, which is what keeps the screens small and testable.
"""

from __future__ import annotations

from typing import Any, Iterable

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Select, Static
from textual.widgets.option_list import Option

from .backend import BackendError, CLIBackend
from .i18n import tr
from .widgets import MUTED, RED, TEXT


class ListScreen(ModalScreen[str | None]):
    """A titled list with stable ids; Enter returns the id, Esc returns None."""

    BINDINGS = [Binding("escape", "close", "Close", priority=True)]

    def __init__(self, title: str, options: Iterable[tuple[str, str]], hint: str = "") -> None:
        super().__init__()
        self._title = title
        self._options = list(options)
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Static(self._title, classes="modal-title")
            yield OptionList(
                *[Option(label, id=key) for key, label in self._options],
                id="choices",
            )
            if self._hint:
                yield Static(self._hint, classes="modal-hint")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected, "#choices")
    def choose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_close(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close", priority=True)]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal help-modal"):
            yield Static(self._title, classes="modal-title")
            with VerticalScroll(classes="help-scroll"):
                yield Static(Text(self._body, style=TEXT), markup=False)
            yield Static("Esc", classes="modal-hint")

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()

    def action_close(self) -> None:
        self.dismiss(None)


class TextViewerScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close", priority=True)]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal help-modal"):
            yield Static(self._title, classes="modal-title")
            with VerticalScroll(classes="help-scroll"):
                yield Static(Text(self._body, style=MUTED), markup=False)
            yield Static("Esc", classes="modal-hint")

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()

    def action_close(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "yes", "Yes", priority=True),
        Binding("n", "no", "No", priority=True),
        Binding("escape", "no", "No", priority=True),
    ]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal confirm-modal"):
            yield Static(self._title, classes="modal-title")
            yield Static(Text(self._body, style=TEXT), markup=False)
            with Horizontal(classes="modal-buttons"):
                yield Button("Yes", variant="primary", id="yes")
                yield Button("No", id="no")
            yield Static("y / n", classes="modal-hint")

    @on(Button.Pressed, "#yes")
    def yes_pressed(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def no_pressed(self) -> None:
        self.dismiss(False)

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


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
            self._error(tr(self.language, "provider.failed", error="no installable presets"))
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
            Text("testing the endpoint…", style=MUTED)
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
                [("no", "no"), ("yes", "yes")],
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
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")
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
            self._error("server id and namespace are required")
            return
        if transport == "stdio" and not command:
            self._error("a stdio server needs a command")
            return
        if transport != "stdio" and not url:
            self._error("a remote server needs a URL")
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
            self._error("timeout must be a number")
            return
        self.query_one("#save", Button).disabled = True
        self.query_one("#mcp-status", Static).update(Text("saving…", style=MUTED))
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


class McpScreen(ModalScreen[str | None]):
    """The server list plus the four actions that matter."""

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("a", "add_server", "Add", priority=True),
        Binding("d", "remove_server", "Remove", priority=True),
        Binding("p", "probe_server", "Probe", priority=True),
        Binding("s", "select_server", "Select", priority=True),
    ]

    def __init__(self, backend: CLIBackend, language: str, session_id: str) -> None:
        super().__init__()
        self.backend = backend
        self.language = language
        self.session_id = session_id
        self._entries: list[dict[str, Any]] = []
        self._notice = ""

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal mcp-modal"):
            yield Static(tr(self.language, "mcp.title"), classes="modal-title")
            yield OptionList(id="servers")
            yield Static(tr(self.language, "mcp.hint"), classes="modal-hint")
            yield Static("", id="mcp-footer", classes="modal-hint")

    def on_mount(self) -> None:
        self._refresh()
        self.query_one("#servers", OptionList).focus()

    def _refresh(self) -> None:
        try:
            payload = self.backend.mcp_status(self.session_id or None)
        except BackendError as exc:
            self._footer(str(exc), error=True)
            self._entries = []
            return
        self._entries = [
            item for item in payload.get("servers", []) if isinstance(item, dict)
        ]
        options = []
        for item in self._entries:
            limits = item.get("liveness") or {}
            selection = item.get("selection") or {}
            counts = selection.get("counts") or {}
            live = str(limits.get("state") or "not_probed")
            selected = str(selection.get("state") or "not_selected")
            allowed = counts.get("allowed", 0) if isinstance(counts, dict) else 0
            label = (
                f"{item.get('server_id', '?')}  ·  {item.get('transport', '?')}"
                f"  ·  {selected}  ·  {live}  ·  tools {allowed}"
            )
            options.append(Option(label, id=str(item.get("server_id") or "")))
        listing = self.query_one("#servers", OptionList)
        listing.clear_options()
        if options:
            listing.add_options(options)
        else:
            listing.add_option(
                Option(tr(self.language, "notice.mcp_empty"), disabled=True)
            )

    def _highlighted(self) -> dict[str, Any] | None:
        listing = self.query_one("#servers", OptionList)
        index = listing.highlighted
        if index is None or not self._entries:
            return None
        if index < 0 or index >= len(self._entries):
            return None
        return self._entries[index]

    def _footer(self, text: str, *, error: bool = False) -> None:
        colour = RED if error else MUTED
        self.query_one("#mcp-footer", Static).update(Text(text, style=colour))

    def action_close(self) -> None:
        self.dismiss(self._notice)

    def action_add_server(self) -> None:
        self.app.push_screen(
            McpAddScreen(self.backend, self.language), self._added
        )

    def _added(self, result: str | None) -> None:
        if result:
            self._notice = tr(self.language, "mcp.added", server=result)
        self._refresh()

    def action_remove_server(self) -> None:
        entry = self._highlighted()
        if entry is None:
            return
        server = str(entry.get("server_id") or "")
        self.app.push_screen(
            ConfirmScreen(
                tr(self.language, "mcp.title"),
                f"Remove MCP server {server}?",
            ),
            lambda confirmed: self._remove(server) if confirmed else None,
        )

    def _remove(self, server: str) -> None:
        try:
            self.backend.mcp_remove(server)
        except BackendError as exc:
            self._footer(str(exc), error=True)
            return
        self._notice = tr(self.language, "mcp.removed", server=server)
        self._refresh()

    def action_probe_server(self) -> None:
        entry = self._highlighted()
        if entry is None:
            return
        server = str(entry.get("server_id") or "")
        self._footer(tr(self.language, "notice.mcp_probe", server=server))
        self.run_worker(lambda: self._probe(server), thread=True, exclusive=False)

    def _probe(self, server: str) -> None:
        try:
            payload = self.backend.mcp_probe(server)
        except BackendError as exc:
            self.app.call_from_thread(
                self._footer, tr(self.language, "notice.mcp_probe_failed", server=server, error=str(exc)), error=True
            )
            return
        count = payload.get("tool_count", 0)
        self.app.call_from_thread(
            self._footer,
            tr(self.language, "notice.mcp_probe_ok", server=server, count=count),
        )
        self.app.call_from_thread(self._refresh)

    def action_select_server(self) -> None:
        entry = self._highlighted()
        if entry is None:
            return
        server = str(entry.get("server_id") or "")
        if not self.session_id or self.backend.session(self.session_id) is None:
            self._footer("Run one task in this window first; MCP selection is per session.")
            return
        self.app.push_screen(
            ConfirmScreen(
                tr(self.language, "mcp.select_confirm", server=server),
                tr(self.language, "mcp.select_confirm_body"),
            ),
            lambda confirmed: self._select(server, confirmed),
        )

    def _select(self, server: str, confirmed: bool | None) -> None:
        if not confirmed:
            return
        self._footer(f"selecting {server}…")
        self.run_worker(lambda: self._select_worker(server), thread=True)

    def _select_worker(self, server: str) -> None:
        try:
            payload = self.backend.mcp_select_all(server, self.session_id)
        except BackendError as exc:
            self.app.call_from_thread(self._footer, str(exc), error=True)
            return
        selection = payload.get("selection") or {}
        tools = selection.get("tools") if isinstance(selection, dict) else {}
        count = len(tools) if isinstance(tools, dict) else 0
        self.app.call_from_thread(
            self._footer,
            tr(self.language, "notice.mcp_selected", server=server, count=count),
        )
        self.app.call_from_thread(self._refresh)