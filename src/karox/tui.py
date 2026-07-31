"""Full-screen terminal client for KaroX.

``karox`` opens this application for humans.  The argparse command surface is
still available as ``karox <subcommand>`` for scripts and CI, but it is not
used as the chat input language.  Ordinary text entered here is always an
agent task.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import signal
import shlex
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import httpx

from .bridge import BridgeCredentialStore
from .credentials import CredentialStore
from .models import AccessProfile
from .paths import config_dir, session_dir
from .provider_factory import ProviderFactory
from .provider_presets import (
    ProviderPreset,
    provider_preset,
    provider_presets,
    sponsor_messages,
)
from .providers import ModelMessage, ModelRequest, ProviderError, ProviderErrorKind
from .registry import ModelRecord, ProviderRecord, ProviderRegistry
from .sessions import SessionStore
from .tailscale import TailscaleError, find_tailscale, prepare_tailscale_funnel
from .web_bridge_launcher import WEB_BRIDGE_PROFILES, find_cloudflared

try:
    from rich.markup import escape
    from rich.text import Text
    from textual import on
    from textual.app import App, ComposeResult, SkipAction
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.content import Content
    from textual.screen import ModalScreen
    from textual.selection import Selection
    from textual.theme import Theme
    from textual.widgets import (
        Button,
        Checkbox,
        Input,
        Label,
        LoadingIndicator,
        Markdown,
        OptionList,
        RadioButton,
        RadioSet,
        Static,
    )
    from textual.widgets.option_list import Option

    _HAS_TEXTUAL = True
except Exception:  # pragma: no cover - only minimal/broken installations
    _HAS_TEXTUAL = False


SLASH_COMMANDS: Dict[str, str] = {
    "/connect": "connect an API model, website, or both",
    "/models": "show configured API models",
    "/sessions": "show task sessions",
    "/bridge": "connect PromptQL, Notion, or an MCP/OpenAPI client",
    "/bridge stop": "stop the active bridge",
    "/ask TEXT": "ask a configured hosted agent target (PromptQL)",
    "/mcp": "show external MCP servers",
    "/doctor": "run KaroX diagnostics",
    "/verify JSON": "change the verification command",
    "/language": "change interface language",
    "/workspace PATH": "change the working project folder",
    "/sponsors": "show or hide the sponsor line",
    "/clear": "clear the conversation",
    "/help": "show command help",
    "/quit": "exit KaroX",
}

_COMMANDS_RU: Dict[str, str] = {
    "/connect": "подключить API-модель, сайт или оба варианта",
    "/models": "показать настроенные API-модели",
    "/sessions": "показать сессии задач",
    "/bridge": "подключить PromptQL, Notion или MCP/OpenAPI-клиент",
    "/bridge stop": "остановить активный мост",
    "/ask ТЕКСТ": "задать вопрос настроенному агенту-цели (PromptQL)",
    "/mcp": "показать внешние MCP-серверы",
    "/doctor": "запустить диагностику KaroX",
    "/verify JSON": "изменить команду проверки",
    "/language": "изменить язык интерфейса",
    "/workspace ПУТЬ": "изменить рабочую папку проекта",
    "/sponsors": "показать или скрыть строку спонсоров",
    "/clear": "очистить диалог",
    "/help": "показать справку по командам",
    "/quit": "выйти из KaroX",
}

_TEXT: Dict[str, Dict[str, str]] = {
    "ru": {
        "brand": "KaroX\n[dim]API-модели • локальные инструменты • сайты и MCP[/dim]",
        "placeholder": "Опишите задачу для KaroX или введите / для команд…",
        "hint": "Enter — отправить • / — команды • /connect — подключения • Ctrl+C — стоп/копировать",
        "welcome_ready": "[bold #e0dccc]KaroX готов.[/]\nНапишите задачу обычным текстом.\nВведите [#d4b676]/[/], чтобы увидеть все команды.",
        "welcome_unconfigured": "[bold #e0dccc]KaroX запущен, но модель не подключена.[/]\nПодключите API-провайдера командой [#d4b676]/connect[/].\nВведите [#d4b676]/[/], чтобы увидеть все команды.",
        "repo": "репозиторий",
        "model": "модель",
        "not_configured": "не настроена",
        "context": "контекст",
        "context_unknown": "лимит не объявлен",
        "output_limit": "вывод",
        "spent": "израсходовано",
        "session": "сессия",
        "new_task": "новая задача",
        "bridge": "мост",
        "public": "публичный",
        "off": "выключен",
        "commands": "Команды",
        "unknown": "Неизвестная команда {command}. Введите / для списка команд.",
        "connect_first": "Для отправки задачи сначала подключите API-модель через [#d4b676]/connect[/].",
    },
    "en": {
        "brand": "KaroX\n[dim]API models • local tools • websites and MCP[/dim]",
        "placeholder": "Describe a task for KaroX or type / for commands…",
        "hint": "Enter — send • / — commands • /connect — connections • Ctrl+C — stop/copy",
        "welcome_ready": "[bold #e0dccc]KaroX is ready.[/]\nDescribe a task in plain language.\nEnter [#d4b676]/[/] to see every command.",
        "welcome_unconfigured": "[bold #e0dccc]KaroX is running, but no model is connected.[/]\nConnect an API provider with [#d4b676]/connect[/].\nEnter [#d4b676]/[/] to see every command.",
        "repo": "repository",
        "model": "model",
        "not_configured": "not configured",
        "context": "context",
        "context_unknown": "limit not declared",
        "output_limit": "output",
        "spent": "spent",
        "session": "session",
        "new_task": "new task",
        "bridge": "bridge",
        "public": "public",
        "off": "off",
        "commands": "Commands",
        "unknown": "Unknown command {command}. Enter / to see all commands.",
        "connect_first": "Connect an API model with [#d4b676]/connect[/] before sending a task.",
    },
}


def _preferences_path() -> Path:
    return config_dir() / "vnext" / "ui.json"


def _load_preferences() -> dict[str, Any]:
    try:
        value = json.loads(_preferences_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return {}
    return value if isinstance(value, dict) else {}


def _load_language() -> Optional[str]:
    value = _load_preferences().get("language")
    return value if value in {"ru", "en"} else None


def _load_sponsors_visible() -> bool:
    value = _load_preferences().get("sponsors_visible", True)
    return value if isinstance(value, bool) else True


def _save_preferences(**changes: object) -> None:
    path = _preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    value = _load_preferences()
    value.update(changes)
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _save_language(language: str) -> None:
    if language not in {"ru", "en"}:
        raise ValueError("language must be 'ru' or 'en'")
    _save_preferences(language=language)


def _save_sponsors_visible(visible: bool) -> None:
    _save_preferences(sponsors_visible=bool(visible))


def _commands(language: str) -> Dict[str, str]:
    return _COMMANDS_RU if language == "ru" else SLASH_COMMANDS


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def _unsafe_workspace_reason(path: Path, language: str = "ru") -> Optional[str]:
    """Reject operating-system locations that must never be agent workspaces."""
    resolved = path.expanduser().resolve()
    protected: list[Path] = []
    for name in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(name)
        if value:
            protected.append(Path(value))
    drive_root = Path(resolved.anchor) if resolved.anchor else None
    unsafe = (drive_root is not None and resolved == drive_root) or any(
        _is_within(resolved, item) for item in protected
    )
    if not unsafe:
        return None
    return (
        "Системная папка Windows не может быть рабочим проектом. "
        "Перейдите в папку проекта или введите /workspace ПУТЬ."
        if language == "ru"
        else "A Windows system folder cannot be used as a project workspace. "
        "Open the project folder or enter /workspace PATH."
    )


_BACKEND_SLASH: Dict[str, List[str]] = {
    "/models": ["model", "list", "--json"],
    "/sessions": ["session", "list", "--json"],
    "/mcp": ["mcp", "status", "--json"],
    "/doctor": ["doctor", "--json"],
}

_MCP_LIVENESS_TEXT: Dict[str, Tuple[str, str]] = {
    "live": ("живой", "live"),
    "failed": ("недоступен", "unreachable"),
    "not_probed": ("не проверялась", "not probed"),
}

_MCP_CREDENTIAL_TEXT: Dict[str, Tuple[str, str]] = {
    "not_required": ("не требуется", "not required"),
    "present": ("есть", "present"),
    "missing": ("отсутствует", "missing"),
    "unreadable": ("не прочитан", "unreadable"),
}

_MCP_SELECTION_TEXT: Dict[str, Tuple[str, str]] = {
    "not_selected": (
        "не выбран в сессии — инструменты агенту не выданы",
        "not selected in the session — no tools are exposed to the agent",
    ),
    "stale": (
        "выбор устарел после правки сервера — нужен повторный select",
        "selection is stale after a server change — reselect the server",
    ),
}

_MCP_LOCATION_TEXT: Dict[str, Tuple[str, str]] = {
    "local": ("локальный", "local"),
    "remote": ("удалённый", "remote"),
}


def _mcp_status_text(payload: Any, english: bool) -> str:
    """Render the single MCP state screen.

    Reachability and authorization are printed as separate facts on purpose:
    a live server says nothing about which tools may run, and an allowed tool
    says nothing about the server being up.  Liveness that was never probed is
    printed as such instead of being guessed from the configuration.
    """
    servers = payload.get("servers") if isinstance(payload, dict) else None
    if not isinstance(servers, list) or not servers:
        return (
            "Внешние MCP-серверы не настроены.\nИспользуйте /connect для подключения сайта или агента."
            if not english
            else "No external MCP servers are configured.\nUse /connect to connect a website or agent."
        )
    raw_totals = payload.get("totals")
    totals = raw_totals if isinstance(raw_totals, dict) else {}

    def total(name: str) -> int:
        value = totals.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return value

    lines = [
        (
            f"MCP: серверов {total('servers')} · выбрано {total('selected')} · "
            f"живых {total('live')} из проверенных {total('probed')} · "
            f"инструментов разрешено {total('allowed_tools')}, "
            f"заблокировано {total('blocked_tools')}"
        )
        if not english
        else (
            f"MCP: {total('servers')} servers · {total('selected')} selected · "
            f"{total('live')} live of {total('probed')} probed · "
            f"{total('allowed_tools')} tools allowed, "
            f"{total('blocked_tools')} blocked"
        )
    ]
    index = 1 if english else 0
    for item in servers:
        if not isinstance(item, dict):
            continue
        attention = item.get("attention")
        icon = "!" if isinstance(attention, list) and attention else "✓"
        server = str(item.get("server_id") or "?")
        transport = str(item.get("transport") or "unknown")
        facts = [
            part
            for part in (
                str(item.get("namespace") or ""),
                _MCP_LOCATION_TEXT.get(str(item.get("location") or ""), ("", ""))[index],
                str(item.get("endpoint") or ""),
            )
            if part
        ]
        header = f"{icon} {server}  [{transport}]"
        if facts:
            header += "  " + " · ".join(facts)
        lines.append(header)

        selection = item.get("selection")
        selection = selection if isinstance(selection, dict) else {}
        counts = selection.get("counts")
        counts = counts if isinstance(counts, dict) else {}

        def count(name: str) -> int:
            value = counts.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                return 0
            return value

        state = str(selection.get("state") or "not_selected")
        note = _MCP_SELECTION_TEXT.get(state)
        if note is not None:
            lines.append("   " + note[index])
        else:
            lines.append(
                "   "
                + (
                    f"инструменты: {count('allowed')} разрешено, "
                    f"{count('blocked_ask')} ask, {count('blocked_deny')} deny"
                    if not english
                    else (
                        f"tools: {count('allowed')} allowed, "
                        f"{count('blocked_ask')} ask, {count('blocked_deny')} deny"
                    )
                )
            )
        if count("blocked_ask"):
            lines.append(
                "   "
                + (
                    "ask отклоняет вызов без запроса подтверждения — решение меняется вручную в сессии"
                    if not english
                    else "ask refuses the call without prompting — the decision is changed by hand in the session"
                )
            )

        credential = item.get("credential")
        credential = credential if isinstance(credential, dict) else {}
        secret_text = _MCP_CREDENTIAL_TEXT.get(
            str(credential.get("state") or ""), ("неизвестно", "unknown")
        )[index]
        liveness = item.get("liveness")
        liveness = liveness if isinstance(liveness, dict) else {}
        liveness_state = str(liveness.get("state") or "")
        liveness_text = _MCP_LIVENESS_TEXT.get(
            liveness_state, ("неизвестно", "unknown")
        )[index]
        suffix = ""
        advertised = liveness.get("tool_count")
        if (
            liveness_state == "live"
            and isinstance(advertised, int)
            and not isinstance(advertised, bool)
        ):
            suffix = (
                f" (сервер объявил инструментов: {advertised})"
                if not english
                else f" ({advertised} tools advertised)"
            )
        elif liveness_state == "failed":
            kind = str(liveness.get("failure_kind") or "")
            suffix = f" ({kind})" if kind else ""
        lines.append(
            "   "
            + (
                f"ключ: {secret_text} · связь: {liveness_text}{suffix}"
                if not english
                else f"key: {secret_text} · reachability: {liveness_text}{suffix}"
            )
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class ProviderSetup:
    provider_id: str
    adapter: str
    base_url: str
    model_id: str
    api_key: str = ""
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None


@dataclass(frozen=True)
class DiscoveredModel:
    model_id: str
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None


@dataclass(frozen=True)
class ModelDiscovery:
    models: tuple[DiscoveredModel, ...]
    base_url: str
    attempted_urls: tuple[str, ...]


class ModelDiscoveryError(RuntimeError):
    """A safe, structured failure from provider model discovery."""

    def __init__(
        self,
        kind: str,
        *,
        attempted_urls: Sequence[str],
        status_code: Optional[int] = None,
        detail: str = "",
    ) -> None:
        super().__init__(detail or kind)
        self.kind = kind
        self.attempted_urls = tuple(attempted_urls)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class BridgeSetup:
    profile: str
    port: int
    tools: tuple[str, ...]
    # How the local loopback bridge is published for cloud-hosted clients:
    #   "none"      — local only (no public URL);
    #   "cloudflare" — public HTTPS via the cloudflared Quick Tunnel;
    #   "tailscale"  — public HTTPS via Tailscale Funnel (*.ts.net).
    tunnel_provider: str = "cloudflare"
    # OAuth web MCP clients bind codes and tokens to one stable public origin.
    # This is required for chatgpt-web/claude-web and omitted for bearer profiles.
    public_url: Optional[str] = None


@dataclass(frozen=True)
class BridgeLaunch:
    session_id: str
    profile: str
    protocol: str
    endpoint: str
    secret: str
    argv: tuple[str, ...]
    public_url: Optional[str] = None
    managed: bool = False


def _registry() -> ProviderRegistry:
    return ProviderRegistry(config_dir() / "vnext" / "providers.json")


def _pids_listening_on(address: str, port: int) -> List[int]:
    """Return PIDs that own a TCP listener on ``address:port``.

    Best-effort: walks the OS process table via ``netstat`` and returns unique
    owning PIDs.  Any failure (no netstat, parse error) yields an empty list —
    the caller treats an empty list as "port is free, nothing to stop".
    """
    pids: List[int] = []
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # ``netstat`` on Windows emits localized column/state text (cp1251 on a
    # Russian install, etc.); default UTF-8 decoding throws UnicodeDecodeError
    # in the subprocess reader thread.  Use the OS locale encoding (``mbcs`` on
    # Windows) and replace undecodable bytes so the helper never raises.
    encoding = "mbcs" if os.name == "nt" else "utf-8"
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            encoding=encoding,
            errors="replace",
            timeout=10,
            creationflags=flags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return pids
    needle = f"{address}:{port}"
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        # Format on Windows: "Proto LocalAddress ForeignAddress State PID"
        if len(parts) < 5:
            continue
        if needle not in parts[1]:
            continue
        # The state column is localized too; match on the ASCII "LISTEN" prefix
        # of the English state OR any column containing LISTEN (case-insensitive)
        # so it works across locales.
        state_upper = parts[-2].upper()
        if not (state_upper == "LISTENING" or state_upper.startswith("LISTEN")):
            continue
        try:
            pids.append(int(parts[-1]))
        except ValueError:
            continue
    # Deduplicate while preserving order.
    seen: set[int] = set()
    unique: List[int] = []
    for pid in pids:
        if pid and pid not in seen:
            seen.add(pid)
            unique.append(pid)
    return unique


def _free_port_on_address(address: str, port: int, *, skip_pids: Optional[set] = None) -> int:
    """Stop orphan listeners on ``address:port``; return how many were stopped.

    A previous KaroX run (or a crashed bridge from an earlier session in the
    same TUI lifetime) can leave a python process holding the port.  When that
    happens the next ``bridge serve`` fails with ``[Errno 10048]`` on Windows /
    ``EADDRINUSE`` elsewhere, the bridge process exits, and the user is told
    "Мост остановлен (код 3)" with no public URL — even though everything else
    is fine.  We walk ``netstat`` for listeners on the port, skip the current
    process and any PIDs the caller wants to preserve, and stop the rest via
    ``taskkill`` (Windows) / ``kill`` (posix).
    """
    skip = skip_pids or set()
    try:
        own_pid = os.getpid()
    except Exception:
        own_pid = -1
    skip.add(own_pid)
    stopped = 0
    for pid in _pids_listening_on(address, port):
        if pid in skip:
            continue
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    encoding="mbcs",
                    errors="replace",
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
            else:
                os.kill(pid, 15)
        except Exception:
            continue
        stopped += 1
    return stopped


def _port_is_listening(address: str, port: int) -> bool:
    """True if any listener currently owns ``address:port`` (quick netstat probe)."""
    return bool(_pids_listening_on(address, port))


def _selected_model() -> Optional[ModelRecord]:
    return _registry().selected_model()


def _find_cloudflared() -> Optional[str]:
    """Use the same lookup as the managed bridge CLI."""
    return find_cloudflared()


def _find_tailscale() -> Optional[str]:
    """Use the same Tailscale lookup as the managed bridge CLI."""
    return find_tailscale()


def _start_worker(
    target: Callable[..., Any],
    *args: Any,
    name: Optional[str] = None,
) -> threading.Thread:
    """Start a daemon background worker through one patchable seam.

    Every background worker in this screen goes through this function instead of
    calling ``threading.Thread`` inline, so a test that needs to observe or
    suppress a worker patches *this* name.

    The indirection is not cosmetic. ``tui.threading`` is the stdlib module
    object itself, so patching ``tui.threading.Thread`` replaces
    ``threading.Thread`` for the whole interpreter -- including Textual's and
    asyncio's own internals. A test that did that deadlocked forever instead of
    failing, because the framework driving the test could no longer start a
    thread of its own.
    """
    thread = threading.Thread(target=target, args=args, name=name, daemon=True)
    thread.start()
    return thread


def _tailscale_gui_app(executable: Optional[str]) -> Optional[str]:
    """Locate the Tailscale GUI app (``tailscale-ipn.exe`` on Windows).

    On Windows the CLI (``tailscale.exe``) requires administrator privileges to
    drive login when the daemon isn't yet authenticated, so a non-elevated
    ``tailscale up`` silently no-ops (rc=0, no output).  The GUI app runs in the
    user session and can complete login without a separate UAC prompt — it is the
    supported user-facing login flow on Windows.  We look for it next to the CLI
    binary (the standard ``C:\\Program Files\\Tailscale`` layout installs both).
    """
    if os.name != "nt" or not executable:
        return None
    gui = Path(executable).parent / "tailscale-ipn.exe"
    return str(gui) if gui.is_file() else None


def _tailscale_logged_in(executable: str) -> bool:
    """Best-effort check that the node is authenticated to a tailnet.

    A node only gets a ``Self.DNSName`` once it has authenticated with the
    coordination server, so a non-empty DNS name (with ``BackendState == Running``)
    is a reliable "logged in" signal — more reliable than ``BackendState`` alone,
    which can be ``Running`` before login completes.

    This probes an external binary, so it must never raise: any failure (the
    binary is missing, returns non-JSON, the subprocess type is unexpected) is
    treated as "not logged in".
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [executable, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=flags,
            check=False,
        )
        payload = json.loads(result.stdout or "{}")
    except Exception:
        return False
    self_record = payload.get("Self") or {}
    dns = str(self_record.get("DNSName") or "").rstrip(".")
    return payload.get("BackendState") == "Running" and bool(dns)


def _tailscale_backend_state(executable: str) -> str:
    """Read the daemon's ``BackendState`` ("Running", "NoState", "Starting", …).

    Used during login polling to tell a stuck daemon (lingering "NoState"/
    "Starting") apart from one waiting for the user to authorize.  Never raises.
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [executable, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=flags,
            check=False,
        )
        payload = json.loads(result.stdout or "{}")
    except Exception:
        return "Unknown"
    return str(payload.get("BackendState") or "Unknown")


# Login poll loop tunables.  Module-level so tests can shrink the wait window
# without touching the global ``time.monotonic`` (which Textual's run_test also
# calls during startup and which can't be safely side_effect-patched).
_TAILSCALE_LOGIN_TIMEOUT_SECONDS = 300
_TAILSCALE_LOGIN_POLL_INTERVAL = 2
_TAILSCALE_STUCK_STATE_THRESHOLD = 15


def _tailscale_funnel_available(status_payload: Dict[str, Any]) -> bool:
    """Best-effort check that the node can publish a Funnel endpoint.

    The Tailscale status JSON advertises Funnel capability in ``Self.CapMap``
    (``funnel``/``funnel-attributes``).  Newer versions also surface it in
    ``CurrentTailnet.MagicDNSSuffix``.  When in doubt we optimistically allow
    the funnel command to run and rely on its non-zero exit to report failure.
    """
    self_record = status_payload.get("Self") or {}
    cap_map = self_record.get("CapMap") or {}
    cap_blob = json.dumps(cap_map).lower()
    if "funnel" in cap_blob:
        return True
    tailnet = status_payload.get("CurrentTailnet") or {}
    suffix = str(tailnet.get("MagicDNSSuffix") or "").lower()
    if "ts.net" in suffix:
        return True
    # Unknown capability shape — defer to the funnel command's own exit code.
    return True


def _default_verification(repository: Path) -> tuple[tuple[str, ...], ...]:
    """Safe verification commands for a repository, as a set of argv tuples.

    The TUI launches both the managed web bridge and the native agent with
    these commands as the approved verification allowlist; ``checks.run`` may
    only run a command that matches one of them.  For a Node project the
    common CI scripts are returned **only when they exist in ``package.json``**
    so an allowlist never advertises a script the project does not define.
    """
    package_json = repository / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts") if isinstance(data, dict) else None
        except (OSError, ValueError):
            scripts = None
        if isinstance(scripts, dict):
            # Each candidate is only included when the project defines it, so the
            # allowlist never advertises a script that does not exist.
            commands: list[tuple[str, ...]] = []
            for candidate in (("npm", "test"), ("npm", "run", "ci"),
                              ("npm", "run", "test:smoke")):
                script_name = candidate[2] if candidate[1] == "run" else candidate[1]
                if isinstance(script_name, str) and script_name in scripts:
                    commands.append(candidate)
            if commands:
                return tuple(commands)
    if (repository / "pyproject.toml").exists() and (repository / "tests").is_dir():
        return ((sys.executable, "-m", "pytest", "-q"),)
    if package_json.is_file():
        return (("npm", "test"),)
    return (("git", "diff", "--check"),)


def _optional_positive_int(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _discovery_candidates(setup: ProviderSetup) -> list[tuple[str, str]]:
    base_url = setup.base_url.strip().rstrip("/")
    candidates = [(base_url, f"{base_url}/models")]
    if setup.adapter in {"openai_responses", "openai_compatible_chat"} and not (
        base_url.lower().endswith("/v1")
    ):
        versioned = f"{base_url}/v1"
        candidates.append((versioned, f"{versioned}/models"))
    return candidates


def _response_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        raw_error = payload.get("error")
        if isinstance(raw_error, dict):
            value = raw_error.get("message") or raw_error.get("detail")
        else:
            value = raw_error or payload.get("message") or payload.get("detail")
        if isinstance(value, str):
            return value.strip()[:500]
    text = response.text.strip()
    return text[:500] if text and "<html" not in text.lower() else ""


def _discover_models_result(setup: ProviderSetup) -> ModelDiscovery:
    """Discover models, trying the common OpenAI ``/v1`` base automatically."""
    base_url = setup.base_url.strip().rstrip("/")
    ProviderRecord(
        provider_id=setup.provider_id.strip() or "discovery",
        adapter_kind=setup.adapter,
        base_url=base_url,
    )
    api_key = setup.api_key
    if not api_key:
        try:
            api_key = CredentialStore().resolve(
                f"os-keyring:provider/{setup.provider_id.strip()}"
            )
        except Exception:
            api_key = ""
    headers: Dict[str, str] = {"Accept": "application/json"}
    if api_key:
        if setup.adapter == "anthropic_messages":
            headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        elif setup.adapter == "gemini_generate_content":
            headers["x-goog-api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    attempted: list[str] = []
    response: Optional[httpx.Response] = None
    effective_base = base_url
    candidates = _discovery_candidates(setup)
    for index, (candidate_base, endpoint) in enumerate(candidates):
        attempted.append(endpoint)
        try:
            current = httpx.get(endpoint, headers=headers, timeout=20.0)
        except httpx.TimeoutException as exc:
            raise ModelDiscoveryError(
                "timeout", attempted_urls=attempted, detail=str(exc)
            ) from exc
        except httpx.RequestError as exc:
            raise ModelDiscoveryError(
                "network", attempted_urls=attempted, detail=str(exc)
            ) from exc
        if current.status_code == 404 and index + 1 < len(candidates):
            continue
        response = current
        effective_base = candidate_base
        break
    if response is None:  # pragma: no cover - candidates always contains the base URL
        raise ModelDiscoveryError("network", attempted_urls=attempted)
    if response.status_code >= 400:
        raise ModelDiscoveryError(
            "http",
            attempted_urls=attempted,
            status_code=response.status_code,
            detail=_response_error_detail(response),
        )
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        raise ModelDiscoveryError(
            "invalid_response",
            attempted_urls=attempted,
            detail="the server response is not JSON",
        ) from exc
    raw_models = (
        payload.get("models")
        if isinstance(payload, dict) and setup.adapter == "gemini_generate_content"
        else payload.get("data")
        if isinstance(payload, dict)
        else None
    )
    if not isinstance(raw_models, list):
        raise ModelDiscoveryError(
            "invalid_response",
            attempted_urls=attempted,
            detail="the response contains no model list",
        )
    discovered: list[DiscoveredModel] = []
    for raw in raw_models:
        if not isinstance(raw, dict):
            continue
        raw_id = (
            raw.get("name")
            if setup.adapter == "gemini_generate_content"
            else raw.get("id")
        )
        if not isinstance(raw_id, str) or not raw_id.strip():
            continue
        model_id = raw_id.removeprefix("models/")
        context = _optional_positive_int(
            raw.get("inputTokenLimit")
            or raw.get("context_length")
            or raw.get("context_window")
            or raw.get("max_model_len")
        )
        output = _optional_positive_int(
            raw.get("outputTokenLimit")
            or raw.get("max_output_tokens")
            or raw.get("max_completion_tokens")
        )
        discovered.append(DiscoveredModel(model_id, context, output))
    if not discovered:
        raise ModelDiscoveryError(
            "empty",
            attempted_urls=attempted,
            detail="the provider returned an empty model list",
        )
    return ModelDiscovery(
        models=tuple(sorted(discovered, key=lambda item: item.model_id.lower())),
        base_url=effective_base,
        attempted_urls=tuple(attempted),
    )


def _discover_models(setup: ProviderSetup) -> list[DiscoveredModel]:
    """Compatibility wrapper returning only discovered model records."""
    return list(_discover_models_result(setup).models)


def _friendly_discovery_error(error: Exception, language: str, *, base_url: str) -> str:
    english = language == "en"
    if not isinstance(error, ModelDiscoveryError):
        heading = "Не удалось проверить API." if not english else "API check failed."
        return f"{heading}\n{str(error).strip()}"

    urls = "\n".join(f"  • {url}" for url in (error.attempted_urls or (base_url,)))
    checked = "Проверенные адреса:" if not english else "Checked endpoints:"
    if error.kind == "http" and error.status_code == 404:
        heading = (
            "Сервер доступен, но endpoint списка моделей не найден (HTTP 404)."
            if not english
            else "The server is reachable, but no model-list endpoint was found (HTTP 404)."
        )
        advice = (
            "Проверьте Base URL в документации провайдера. Обычно OpenAI-совместимый URL заканчивается на /v1. "
            "Если провайдер не поддерживает список моделей, введите Model ID вручную."
            if not english
            else "Check the provider's documented Base URL. OpenAI-compatible URLs usually end in /v1. "
            "If model listing is unsupported, enter the Model ID manually."
        )
    elif error.kind == "http" and error.status_code in {401, 403}:
        heading = (
            f"Сервер отклонил API-ключ (HTTP {error.status_code})."
            if not english
            else f"The server rejected the API key (HTTP {error.status_code})."
        )
        advice = (
            "Проверьте ключ, права доступа и выбранный тип API."
            if not english
            else "Check the key, its permissions, and the selected API type."
        )
    elif error.kind == "http":
        heading = (
            f"Провайдер вернул ошибку HTTP {error.status_code}."
            if not english
            else f"The provider returned HTTP {error.status_code}."
        )
        advice = (
            "Проверьте Base URL, ключ и статус сервиса."
            if not english
            else "Check the Base URL, key, and provider status."
        )
    elif error.kind == "timeout":
        heading = (
            "Провайдер не ответил за 20 секунд."
            if not english
            else "The provider did not respond within 20 seconds."
        )
        advice = (
            "Проверьте интернет, VPN/proxy и доступность сервиса."
            if not english
            else "Check your network, VPN/proxy, and provider availability."
        )
    elif error.kind == "network":
        heading = (
            "Не удалось установить соединение с провайдером."
            if not english
            else "Could not connect to the provider."
        )
        advice = (
            "Проверьте домен в Base URL, интернет, VPN/proxy и DNS."
            if not english
            else "Check the Base URL domain, network, VPN/proxy, and DNS."
        )
    elif error.kind == "empty":
        heading = (
            "Провайдер вернул пустой список моделей."
            if not english
            else "The provider returned an empty model list."
        )
        advice = (
            "Введите Model ID вручную или проверьте права API-ключа."
            if not english
            else "Enter the Model ID manually or check the API key permissions."
        )
    else:
        heading = (
            "Ответ провайдера имеет неподдерживаемый формат."
            if not english
            else "The provider returned an unsupported response format."
        )
        advice = (
            "Проверьте тип API и Base URL или введите Model ID вручную."
            if not english
            else "Check the API type and Base URL, or enter the Model ID manually."
        )
    detail = ""
    if error.detail and error.kind == "http":
        prefix = "Ответ сервера:" if not english else "Server response:"
        detail = f"\n{prefix} {error.detail}"
    return f"{heading}\n{checked}\n{urls}{detail}\n{advice}"


def _friendly_probe_error(
    error: Exception, language: str, *, setup: ProviderSetup
) -> str:
    english = language == "en"
    base = setup.base_url.strip().rstrip("/")
    suffixes = {
        "openai_responses": "/responses",
        "openai_compatible_chat": "/chat/completions",
        "anthropic_messages": "/messages",
    }
    endpoint = base + suffixes.get(setup.adapter, "")
    if isinstance(error, ProviderError):
        status = f" (HTTP {error.status_code})" if error.status_code else ""
        if error.status_code is not None and 500 <= error.status_code <= 599:
            heading = (
                f"Сервис провайдера временно недоступен{status}."
                if not english
                else f"The provider service is temporarily unavailable{status}."
            )
            advice = (
                "Настройки остались в форме. Подождите немного и повторите проверку."
                if not english
                else "Your settings remain in the form. Wait a moment and retry verification."
            )
            checked = "Endpoint:" if not english else "Endpoint:"
            return f"{heading}\n{advice}\n{checked} {endpoint}"
        elif error.status_code == 404:
            heading = (
                f"Тестовый endpoint не найден{status}."
                if not english
                else f"The test endpoint was not found{status}."
            )
            advice = (
                "Проверьте тип API. Для большинства сторонних сервисов нужен «OpenAI-compatible», "
                "а «OpenAI Responses» выбирайте только при поддержке /responses."
                if not english
                else "Check the API type. Most third-party services use “OpenAI-compatible”; "
                "choose “OpenAI Responses” only when /responses is supported."
            )
        elif error.kind in {
            ProviderErrorKind.AUTHENTICATION,
            ProviderErrorKind.PERMISSION,
        }:
            heading = (
                f"Провайдер отклонил ключ или права доступа{status}."
                if not english
                else f"The provider rejected the key or its permissions{status}."
            )
            advice = (
                "Проверьте API-ключ и доступ этой учётной записи к выбранной модели."
                if not english
                else "Check the API key and account access to the selected model."
            )
        elif error.kind == ProviderErrorKind.MODEL_UNAVAILABLE:
            heading = (
                f"Выбранная модель недоступна{status}."
                if not english
                else f"The selected model is unavailable{status}."
            )
            advice = (
                "Выберите другую найденную модель или проверьте Model ID."
                if not english
                else "Choose another discovered model or check the Model ID."
            )
        elif error.kind == ProviderErrorKind.RATE_LIMIT:
            heading = (
                f"Провайдер ограничил частоту или квоту{status}."
                if not english
                else f"The provider rate limit or quota was reached{status}."
            )
            advice = (
                "Проверьте квоту и повторите позже."
                if not english
                else "Check the quota and try again later."
            )
        elif error.kind == ProviderErrorKind.TRANSPORT:
            heading = (
                "Соединение прервалось во время тестового запроса."
                if not english
                else "The connection failed during the test request."
            )
            advice = (
                "Проверьте интернет, VPN/proxy и доступность провайдера."
                if not english
                else "Check the network, VPN/proxy, and provider availability."
            )
        else:
            heading = (
                f"Провайдер отклонил тестовый запрос{status}."
                if not english
                else f"The provider rejected the test request{status}."
            )
            advice = (
                "Проверьте тип API, Model ID и параметры подключения."
                if not english
                else "Check the API type, Model ID, and connection settings."
            )
        checked = "Проверенный endpoint:" if not english else "Checked endpoint:"
        detail_label = "Ответ:" if not english else "Response:"
        return (
            f"{heading}\n{checked} {endpoint}\n{detail_label} "
            f"{error.safe_message}\n{advice}"
        )
    heading = (
        "Тестовый запрос завершился ошибкой."
        if not english
        else "The test request failed."
    )
    checked = "Проверенный Base URL:" if not english else "Checked Base URL:"
    return f"{heading}\n{checked} {base}\n{str(error).strip()}"


def _save_provider(setup: ProviderSetup, *, activate: bool = True) -> ModelRecord:
    provider_id = setup.provider_id.strip()
    model_id = setup.model_id.strip()
    base_url = setup.base_url.strip().rstrip("/")
    if not provider_id or not model_id or not base_url:
        raise ValueError("provider, base URL, and model are required")

    registry = _registry()
    credential_ref: Optional[str] = None
    try:
        credential_ref = registry.provider(provider_id).credential_ref
    except Exception:
        pass
    if setup.api_key:
        CredentialStore().set(provider_id, setup.api_key)
        credential_ref = f"os-keyring:provider/{provider_id}"
    is_local = base_url.startswith(("http://127.0.0.1", "http://localhost"))
    if credential_ref is None and not is_local:
        raise ValueError("an API key is required for a remote provider")

    registry.put_provider(
        ProviderRecord(
            provider_id=provider_id,
            adapter_kind=setup.adapter,
            base_url=base_url,
            credential_ref=credential_ref,
            privacy_class="local" if is_local else "public",
        )
    )
    registry.put_model(
        ModelRecord(
            provider_id=provider_id,
            model_id=model_id,
            aliases=(),
            context_window=setup.context_window,
            max_output_tokens=setup.max_output_tokens,
            tools="true",
            streaming="true",
            provenance="interactive-setup",
        )
    )
    model = registry.model(provider_id, model_id)
    return registry.select_model(provider_id, model_id) if activate else model


def _probe_provider(setup: ProviderSetup) -> Dict[str, Any]:
    """Perform one minimal real request before making a configured model active."""
    provider_id = setup.provider_id.strip()
    base_url = setup.base_url.strip().rstrip("/")
    credential_ref: Optional[str] = None
    try:
        credential_ref = _registry().provider(provider_id).credential_ref
    except Exception:
        pass
    if setup.api_key:
        CredentialStore().set(provider_id, setup.api_key)
        credential_ref = f"os-keyring:provider/{provider_id}"
    is_local = base_url.startswith(("http://127.0.0.1", "http://localhost"))
    if credential_ref is None and not is_local:
        raise ValueError("для удалённого API требуется ключ")
    provider_record = ProviderRecord(
        provider_id=provider_id,
        adapter_kind=setup.adapter,
        base_url=base_url,
        credential_ref=credential_ref,
        privacy_class="local" if is_local else "public",
    )
    response = (
        ProviderFactory()
        .create(provider_record)
        .complete(
            ModelRequest(
                model=setup.model_id.strip(),
                messages=(ModelMessage("user", "Reply with exactly OK."),),
                max_output_tokens=8,
                deadline_seconds=min(30.0, provider_record.timeout_seconds),
            )
        )
    )
    return {
        "finish_reason": response.finish_reason,
        "usage": response.usage,
        "transport_attempts": response.transport_attempts,
    }


def _agent_argv(
    task: str,
    repository: Path,
    verification: Sequence[Sequence[str]],
    session_id: str,
) -> List[str]:
    argv = [
        "agent",
        "run",
        "--repository",
        str(repository),
        "--session-id",
        session_id,
        "--task",
        task,
    ]
    # ``agent run`` accepts a repeatable ``--verification-command``; emit one
    # per approved command so a repository with several safe checks (npm test,
    # npm run ci, npm run test:smoke) gets the full allowlist.
    for command in verification:
        argv.extend(
            ("--verification-command", json.dumps(list(command), ensure_ascii=False))
        )
    argv.append("--json")
    return argv


def _run_cli(argv: Sequence[str], out: Callable[[str], None]) -> int:
    """Run the scriptable backend and relay its output without SystemExit."""
    code, output = _capture_cli(argv)
    if output.strip():
        out(output.rstrip("\n"))
    return code


def _capture_cli(argv: Sequence[str]) -> tuple[int, str]:
    from .cli import main

    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(argv))
    except SystemExit as exc:
        code = int(exc.code or 0)
    output = stdout.getvalue()
    error = stderr.getvalue()
    if error:
        output += ("\n" if output and not output.endswith("\n") else "") + error
    return code, output


def _child_environment(extra_path: Optional[Path] = None) -> dict[str, str]:
    """Environment for a KaroX child process, with both sides agreed on UTF-8.

    Every one of these children is read back with ``encoding="utf-8"``, but a
    Python process writing to a pipe on Windows encodes with the locale code page
    -- cp1251 on a Russian install -- and nothing told it otherwise. So the two
    ends disagreed, and ``errors="replace"`` turned each undecodable byte into
    U+FFFD: the model's "привет — hello" reached the chat as six replacement marks
    and one more for the dash, which read as though the model had produced
    garbage. Naming the encoding on one side only is what caused it.

    Deliberately not applied to foreign programs like ``netstat`` or
    ``tailscale``: those really do emit the locale code page, and they are decoded
    with it on purpose.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_path is not None:
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            f"{extra_path}{os.pathsep}{existing}" if existing else str(extra_path)
        )
    return env


def _run_agent_cli(
    argv: Sequence[str], on_process: Callable[[subprocess.Popen[str]], None]
) -> tuple[int, str]:
    """Run the agent backend as an interruptible subprocess.

    The agent loop runs in a child process so the user can stop it by
    terminating the returned ``Popen`` handle (passed to ``on_process``).
    The agent persists session state to disk at every step, so a stopped run
    leaves the session resumable.  Falls back to the in-process
    ``_capture_cli`` when a subprocess cannot be started.
    """
    src = Path(__file__).resolve().parent.parent  # repo/src on disk, or site-packages
    env = _child_environment(src)
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "karox.cli", *argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except OSError:
        on_process(_NullProcess())
        return _capture_cli(argv)

    on_process(process)
    try:
        output, _ = process.communicate()
    except KeyboardInterrupt:
        process.kill()
        output, _ = process.communicate()
    return int(process.returncode or 0), output or ""


class _NullProcess:
    """No-op stand-in so the stop action is a safe no-op for the fallback path."""

    def poll(self) -> Optional[int]:
        return 0

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    def wait(self, timeout: Optional[float] = None) -> int:
        return 0



def _slash_to_argv(
    line: str, session_id: Optional[str], repository: str
) -> Optional[List[str]]:
    """Translate safe UI inspection commands; never parse ordinary chat text."""
    del repository
    parts = shlex.split(line)
    if not parts:
        return None
    argv = list(_BACKEND_SLASH.get(parts[0], ()))
    if not argv:
        return None
    if parts[0] == "/mcp" and session_id:
        # Tool permissions live in the session, so the screen can only report
        # authorization when a session is open; otherwise it shows setup only.
        argv.extend(["--session-id", session_id])
    return argv


def _clean_cli_error(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    cleaned = []
    for line in lines:
        cleaned.append(line.removeprefix("karox:").strip())
    return "\n".join(cleaned)


def _inspection_text(label: str, code: int, output: str, language: str) -> str:
    """Turn script-oriented JSON into concise human-facing TUI text."""
    english = language == "en"
    if code != 0:
        detail = _clean_cli_error(output)
        heading = (
            "Команда завершилась ошибкой." if not english else "The command failed."
        )
        fallback = (
            "Подробности не были возвращены. Запустите /doctor."
            if not english
            else "No details were returned. Run /doctor."
        )
        return f"{heading}\n{detail or fallback}"
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        text = output.strip()
        return text or ("Нет данных." if not english else "No data returned.")

    if label == "/models":
        if not isinstance(payload, list) or not payload:
            return (
                "Модели ещё не подключены.\nИспользуйте /connect → API-модель."
                if not english
                else "No models are connected yet.\nUse /connect → API model."
            )
        heading = (
            f"Подключённые модели: {len(payload)}"
            if not english
            else f"Connected models: {len(payload)}"
        )
        lines = [heading]
        for item in payload:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider_id") or "?")
            model = str(item.get("model_id") or "?")
            selected = bool(item.get("selected"))
            marker = "●" if selected else "○"
            active = (
                " — активна"
                if selected and not english
                else " — active"
                if selected
                else ""
            )
            limits = []
            if item.get("context_window"):
                limits.append(f"context {item['context_window']}")
            if item.get("max_output_tokens"):
                limits.append(f"output {item['max_output_tokens']}")
            suffix = f" ({', '.join(limits)})" if limits else ""
            lines.append(f"{marker} {provider}/{model}{active}{suffix}")
        return "\n".join(lines)

    if label == "/sessions":
        if not isinstance(payload, list) or not payload:
            return (
                "Сессий пока нет. Они появятся после первой задачи."
                if not english
                else "No sessions yet. They will appear after your first task."
            )
        heading = (
            f"Сессии: {len(payload)}" if not english else f"Sessions: {len(payload)}"
        )
        lines = [heading]
        for item in payload:
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("session_id") or "?")
            status = str(item.get("status") or item.get("phase") or "unknown")
            task = str(item.get("task") or "").strip()
            task_suffix = f" — {task}" if task else ""
            marker = "✕" if bool(item.get("revoked")) else "●"
            lines.append(f"{marker} {session_id}  [{status}]{task_suffix}")

            def _count(name: str) -> Optional[int]:
                value = item.get(name)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return None
                return int(value)

            # A second line describing what the task actually did, so choosing
            # which one to resume does not require opening each session in turn.
            facts: List[str] = []
            changed = _count("changed_files")
            if changed:
                facts.append(
                    f"{changed} files changed"
                    if english
                    else f"изменено файлов: {changed}"
                )
            checks = _count("checks")
            if checks:
                failed = _count("checks_failed") or 0
                label = (
                    f"{checks} checks"
                    if english
                    else f"проверок: {checks}"
                )
                if failed:
                    label += (
                        f", {failed} failed"
                        if english
                        else f", упало {failed}"
                    )
                facts.append(label)
            tokens = _count("total_tokens")
            if tokens:
                facts.append(
                    f"{tokens} tokens"
                    if english
                    else f"токенов: {tokens}"
                )
            costs = item.get("costs")
            if isinstance(costs, dict):
                for currency, amount in sorted(costs.items()):
                    if isinstance(amount, bool) or not isinstance(
                        amount, (int, float)
                    ):
                        continue
                    facts.append(f"{amount:.2f} {currency}")
            updated = str(item.get("updated_at") or "").strip()
            if updated:
                facts.append(
                    f"updated {updated}"
                    if english
                    else f"обновлена {updated}"
                )
            if facts:
                lines.append("   " + " · ".join(facts))
        return "\n".join(lines)

    if label == "/mcp":
        return _mcp_status_text(payload, english)

    if label == "/doctor" and isinstance(payload, dict):
        status = str(payload.get("status") or "unknown")
        status_names = {
            "ok": "всё в порядке" if not english else "healthy",
            "degraded": "требуется внимание" if not english else "needs attention",
        }
        heading = (
            "Диагностика: " if not english else "Diagnostics: "
        ) + status_names.get(status, status)
        names = {
            "bridge_credentials": "Секреты мостов" if not english else "Bridge secrets",
            "mcp_credentials": "Секреты MCP" if not english else "MCP secrets",
            "provider_credentials": "API-ключи" if not english else "API keys",
            "packs": "Пакеты" if not english else "Packs",
            "sessions": "Сессии" if not english else "Sessions",
        }
        lines = [heading]
        checks = payload.get("checks")
        if isinstance(checks, dict):
            for key, raw in checks.items():
                item = raw if isinstance(raw, dict) else {}
                item_status = str(item.get("status") or "unknown")
                icon = "✓" if item_status == "ok" else "!"
                detail = str(item.get("error") or "").strip()
                if not detail and "count" in item:
                    detail = str(item["count"])
                suffix = f" — {detail}" if detail else ""
                lines.append(f"{icon} {names.get(key, key)}: {item_status}{suffix}")
        return "\n".join(lines)

    if isinstance(payload, dict):
        return "\n".join(f"{key}: {value}" for key, value in payload.items())
    if isinstance(payload, list):
        return (
            f"Получено записей: {len(payload)}"
            if not english
            else f"Records returned: {len(payload)}"
        )
    return str(payload)


def _bridge_launch(repository: Path, setup: BridgeSetup) -> BridgeLaunch:
    protocol = "openapi" if setup.profile == "promptql" else "mcp"
    sid = f"bridge-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    SessionStore(session_dir()).create(
        repository,
        f"{setup.profile} hosted bridge",
        AccessProfile.WORKSPACE_WRITE,
        session_id=sid,
    )
    credential = BridgeCredentialStore().set(sid)
    secret = credential["secret"]
    argv = [
        sys.executable,
        "-m",
        "karox.cli",
        "bridge",
        "serve",
        "--profile",
        setup.profile,
        "--protocol",
        protocol,
        "--repository",
        str(repository),
        "--session-id",
        sid,
        "--credential",
        sid,
        "--port",
        str(setup.port),
    ]
    for tool in setup.tools:
        argv.extend(("--tool", tool))
    if setup.public_url:
        argv.extend(("--public-url", setup.public_url))
    endpoint_path = "/openapi.json" if protocol == "openapi" else "/mcp"
    return BridgeLaunch(
        session_id=sid,
        profile=setup.profile,
        protocol=protocol,
        endpoint=f"http://127.0.0.1:{setup.port}{endpoint_path}",
        secret=secret,
        argv=tuple(argv),
        public_url=setup.public_url,
    )


def _managed_web_bridge_launch(repository: Path, setup: BridgeSetup) -> BridgeLaunch:
    """Build a one-command ChatGPT/Claude bridge owned by the CLI launcher."""
    if setup.profile not in WEB_BRIDGE_PROFILES:
        raise ValueError("managed web bridge requires a ChatGPT or Claude profile")
    if setup.tunnel_provider not in {"cloudflare", "tailscale"}:
        raise ValueError("ChatGPT/Claude web bridges require a public HTTPS tunnel")
    sid = f"web-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    # The WRITE_WEB_TOOLS bundle (browser input + dev_server start/stop + file
    # writes) is gated behind --write in the connect command, so any tool that
    # mutates state or drives a UI surfaces as WORKSPACE_WRITE access here.
    mutating_tools = {
        "karox.repo.edit_file",
        "karox.repo.write_file",
        "karox.git.commit",
        "karox.checks.run",
        "karox.browser.open",
        "karox.browser.click",
        "karox.browser.fill",
        "karox.browser.select",
        "karox.browser.press",
        "karox.browser.close",
        "karox.dev_server.start",
        "karox.dev_server.stop",
    }
    needs_write = any(tool in mutating_tools for tool in setup.tools)
    access_profile = (
        AccessProfile.WORKSPACE_WRITE if needs_write else AccessProfile.READ_ONLY
    )
    argv = [
        sys.executable,
        "-m",
        "karox.cli",
        "bridge",
        "connect",
        setup.profile,
        "--repository",
        str(repository),
        "--session-id",
        sid,
        "--access-profile",
        access_profile.value,
        "--tunnel",
        setup.tunnel_provider,
        "--port",
        str(setup.port),
    ]
    if needs_write:
        # --write tells the connect command to add the browser-input and
        # dev_server-start tools from WRITE_WEB_TOOLS on top of the explicit
        # --tool selections, so the read-only browser/dev_server checks still
        # come from the checkbox tools while the mutating ones come from --write.
        argv.append("--write")
    if "karox.checks.run" in setup.tools:
        # karox.checks.run requires an approved verification allowlist; supply
        # the workspace's discovered default set (npm test / npm run ci /
        # npm run test:smoke for a Node project, or the pytest/git fallbacks)
        # so the bridge starts without a manual command.  Each approved
        # command becomes a separate --verification-command, matching the
        # repeatable CLI flag; the external agent can only run a command in
        # this allowlist, not arbitrary shell.
        for command in _default_verification(repository):
            argv.extend(
                ("--verification-command", json.dumps(list(command), ensure_ascii=False))
            )
    for tool in setup.tools:
        argv.extend(("--tool", tool))
    return BridgeLaunch(
        session_id=sid,
        profile=setup.profile,
        protocol="mcp",
        endpoint="",
        secret="",
        argv=tuple(argv),
        managed=True,
    )


if _HAS_TEXTUAL:

    class LanguageScreen(ModalScreen[Optional[str]]):
        """Small first-run language choice with complete keyboard support."""

        BINDINGS = [
            Binding("1", "choose_ru", "Русский", priority=True),
            Binding("2", "choose_en", "English", priority=True),
            Binding("up", "previous_choice", show=False, priority=True),
            Binding("down", "next_choice", show=False, priority=True),
            Binding("escape", "cancel", "Cancel", priority=True),
        ]
        DEFAULT_CSS = """
        LanguageScreen { align: center middle; background: #0e0c08 92%; }
        #language-dialog { width: 64; height: auto; background: #1a1712;
          border: round #c6a56b; padding: 1 2; }
        #language-dialog .title { text-style: bold; color: #e5e5e5;
          margin-bottom: 1; }
        #language-dialog .hint { color: #8a7e6a; margin-bottom: 1; }
        #language-dialog Button { width: 100%; margin-bottom: 1; text-align: left; }
        """

        def __init__(self, *, allow_cancel: bool = False) -> None:
            super().__init__()
            self.allow_cancel = allow_cancel

        def compose(self) -> ComposeResult:
            with Vertical(id="language-dialog"):
                yield Static("Выберите язык / Choose language", classes="title")
                yield Static(
                    "1 / 2 — выбрать / choose\n↑↓ / Tab — navigation • Enter — confirm",
                    classes="hint",
                )
                yield Button("[1] Русский", id="language-ru")
                yield Button("[2] English", id="language-en")
                if self.allow_cancel:
                    yield Button("Esc  Cancel / Отмена", id="language-cancel")

        def on_mount(self) -> None:
            self.query_one("#language-ru", Button).focus()

        def action_choose_ru(self) -> None:
            self.dismiss("ru")

        def action_choose_en(self) -> None:
            self.dismiss("en")

        def action_cancel(self) -> None:
            if self.allow_cancel:
                self.dismiss(None)

        def action_previous_choice(self) -> None:
            self.focus_previous()

        def action_next_choice(self) -> None:
            self.focus_next()

        @on(Button.Pressed)
        def button_pressed(self, event: Button.Pressed) -> None:
            choices = {
                "language-ru": "ru",
                "language-en": "en",
                "language-cancel": None,
            }
            if event.button.id in choices:
                self.dismiss(choices[event.button.id])

    class CommandInput(Input):
        """Composer input that gives slash suggestions first-class keyboard control."""

        BINDINGS = [
            Binding("up", "previous_command", show=False, priority=True),
            Binding("down", "next_command", show=False, priority=True),
            Binding("tab", "complete_command", show=False, priority=True),
            Binding("escape", "dismiss_commands", show=False, priority=True),
        ]

        def _on_paste(self, event: Any) -> None:
            """Keep a multi-line paste instead of throwing away all but line one.

            Textual's single-line Input keeps ``splitlines()[0]`` and discards
            the rest without saying so, which for a coding agent silently ate
            the most common input there is: a pasted stack trace or diff. The
            text is held whole and the composer shows a short marker for it,
            which is also more readable than one enormous scrolling line.
            """
            text = getattr(event, "text", "")
            if not isinstance(text, str) or not text:
                return
            normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
            if "\n" not in normalized:
                return
            register = getattr(self.app, "_register_pasted_block", None)
            if register is None:
                return
            self.insert_text_at_cursor(register(normalized))
            # Textual runs the handler from every class in the MRO, so without
            # this the base Input would append line one after the marker.
            event.prevent_default()
            event.stop()

        def _app_action(self, name: str) -> bool:
            app = self.app
            if not getattr(app, "_command_menu_open", False):
                return False
            getattr(app, name)()
            return True

        def action_previous_command(self) -> None:
            self._app_action("_select_previous_command")

        def action_next_command(self) -> None:
            self._app_action("_select_next_command")

        def action_complete_command(self) -> None:
            if not self._app_action("_complete_selected_command"):
                self.screen.focus_next()

        def action_dismiss_commands(self) -> None:
            self._app_action("_dismiss_command_menu")

    class ConnectionChoiceScreen(ModalScreen[Optional[str]]):
        """Keyboard-first connection choice: API, hosted client, or both."""

        BINDINGS = [
            Binding("1", "choose_api", "API", priority=True),
            Binding("2", "choose_web", "Сайт", priority=True),
            Binding("3", "choose_both", "Оба", priority=True),
            Binding("up", "previous_choice", show=False, priority=True),
            Binding("down", "next_choice", show=False, priority=True),
            Binding("escape", "cancel", "Отмена", priority=True),
        ]
        DEFAULT_CSS = """
        ConnectionChoiceScreen { align: center middle; background: #0e0c08 92%; }
        #choice-dialog { width: 76; height: auto; background: #1a1712;
          border: round #c6a56b; padding: 1 2; }
        #choice-dialog .title { text-style: bold; color: #e5e5e5; margin-bottom: 1; }
        #choice-dialog .hint { color: #8a7e6a; margin-bottom: 1; }
        #choice-dialog Button { width: 100%; margin-bottom: 1; text-align: left; }
        """

        def __init__(self, language: str = "ru") -> None:
            super().__init__()
            self.language = language

        def compose(self) -> ComposeResult:
            english = self.language == "en"
            with Vertical(id="choice-dialog"):
                yield Static(
                    "Connections" if english else "Настройка подключений",
                    classes="title",
                )
                yield Static(
                    (
                        "Choose a connection. Keys 1–3 work without a mouse; "
                        "Tab moves focus and Enter confirms."
                        if english
                        else "Выерите сценарий. Клавиши 1–3 работают без мыши; "
                        "Tab меняет фокус, Enter подтверждает."
                    ),
                    classes="hint",
                )
                yield Button(
                    (
                        "[1] API model — OpenAI, Anthropic, Gemini, Ollama, compatible"
                        if english
                        else "[1] API-модель — OpenAI, Anthropic, Gemini, Ollama и совместимые"
                    ),
                    id="choice-api",
                )
                yield Button(
                    (
                        "[2] Web client — ChatGPT Web, Claude Web, PromptQL, Notion, MCP/OpenAPI"
                        if english
                        else "[2] Веб-клиент — ChatGPT Web, Claude Web, PromptQL, Notion, MCP/OpenAPI"
                    ),
                    id="choice-web",
                )
                yield Button(
                    "[3] Both connections" if english else "[3] Оба подключения",
                    id="choice-both",
                )
                yield Button(
                    "Esc  Cancel" if english else "Esc  Отмена", id="choice-cancel"
                )

        def on_mount(self) -> None:
            self.query_one("#choice-api", Button).focus()

        def action_choose_api(self) -> None:
            self.dismiss("api")

        def action_choose_web(self) -> None:
            self.dismiss("web")

        def action_choose_both(self) -> None:
            self.dismiss("both")

        def action_cancel(self) -> None:
            self.dismiss(None)

        def action_previous_choice(self) -> None:
            self.focus_previous()

        def action_next_choice(self) -> None:
            self.focus_next()

        @on(Button.Pressed)
        def button_pressed(self, event: Button.Pressed) -> None:
            choices = {
                "choice-api": "api",
                "choice-web": "web",
                "choice-both": "both",
                "choice-cancel": None,
            }
            if event.button.id in choices:
                self.dismiss(choices[event.button.id])

    class ProviderPresetScreen(ModalScreen[Optional[str]]):
        """Searchable, keyboard-first provider picker."""

        BINDINGS = [
            Binding("enter", "choose", "Choose", priority=True),
            Binding("up", "previous_preset", show=False, priority=True),
            Binding("down", "next_preset", show=False, priority=True),
            Binding("escape", "cancel", "Cancel", priority=True),
        ]
        DEFAULT_CSS = """
        ProviderPresetScreen { align: center middle; background: #0e0c08 92%; }
        #preset-dialog { width: 86; height: 90%; background: #1a1712;
          border: round #c6a56b; padding: 1 2; }
        #preset-dialog .title { text-style: bold; color: #e5e5e5; }
        #preset-dialog .hint { color: #8a7e6a; margin-bottom: 1; }
        #preset-search { margin-bottom: 1; }
        #provider-presets { height: 1fr; border: round #4a4338;
          background: #1a1712; }
        #preset-note { height: auto; min-height: 3; color: #c6a56b; }
        #preset-buttons { height: 3; align-horizontal: right; }
        #preset-buttons Button { margin-left: 1; }
        """

        def __init__(self, language: str = "ru") -> None:
            super().__init__()
            self.language = language
            self._items = provider_presets()
            self._filtered_items = list(self._items)

        def _label(self, russian: str, english: str) -> str:
            return english if self.language == "en" else russian

        def compose(self) -> ComposeResult:
            with Vertical(id="preset-dialog"):
                yield Static(
                    self._label("Выберите API-провайдера", "Choose an API provider"),
                    classes="title",
                )
                yield Static(
                    self._label(
                        "Начните печатать для поиска • ↑/↓ — выбор • Enter — продолжить",
                        "Type to search • ↑/↓ — select • Enter — continue",
                    ),
                    classes="hint",
                )
                yield Input(
                    placeholder=self._label(
                        "Поиск провайдера…", "Search providers…"
                    ),
                    id="preset-search",
                )
                yield OptionList(id="provider-presets", markup=False)
                yield Static("", id="preset-note", markup=False)
                with Horizontal(id="preset-buttons"):
                    yield Button(
                        self._label("Esc  Отмена", "Esc  Cancel"), id="preset-cancel"
                    )
                    yield Button(
                        self._label("Enter  Продолжить", "Enter  Continue"),
                        id="preset-continue",
                    )

        def on_mount(self) -> None:
            self._render_presets()
            self.query_one("#preset-search", Input).focus()
            self._update_note()

        def _selected(self) -> Optional[ProviderPreset]:
            options = self.query_one("#provider-presets", OptionList)
            index = options.highlighted
            if index is None or index >= len(self._filtered_items):
                return None
            return self._filtered_items[index]

        def _render_presets(self) -> None:
            options = self.query_one("#provider-presets", OptionList)
            options.clear_options()
            options.add_options(
                Option(
                    f"{item.display_name}  ·  {item.status.upper().replace('_', ' ')}",
                    id=f"preset-{item.preset_id}",
                )
                for item in self._filtered_items
            )
            options.highlighted = 0 if self._filtered_items else None
            self._update_note()

        @on(Input.Changed, "#preset-search")
        def search_changed(self, event: Input.Changed) -> None:
            query = event.value.strip().casefold()
            self._filtered_items = [
                item
                for item in self._items
                if query in item.display_name.casefold()
                or query in item.preset_id.casefold()
            ]
            self._render_presets()

        def _update_note(self) -> None:
            item = self._selected()
            if item is None:
                return
            notes = []
            if item.base_url is None:
                notes.append(
                    self._label(
                        "Base URL нужно подтвердить по документации провайдера.",
                        "The Base URL must be confirmed from provider documentation.",
                    )
                )
            if item.privacy_note:
                notes.append(item.privacy_note)
            self.query_one("#preset-note", Static).update("\n".join(notes))

        @on(OptionList.OptionHighlighted, "#provider-presets")
        def preset_changed(self) -> None:
            self._update_note()

        def action_choose(self) -> None:
            item = self._selected()
            if item is not None:
                self.dismiss(item.preset_id)

        def action_previous_preset(self) -> None:
            self._move_preset(-1)

        def action_next_preset(self) -> None:
            self._move_preset(1)

        def _move_preset(self, direction: int) -> None:
            options = self.query_one("#provider-presets", OptionList)
            if options.option_count == 0:
                return
            current = options.highlighted if options.highlighted is not None else 0
            options.highlighted = (current + direction) % options.option_count
            options.scroll_to_highlight()
            self._update_note()

        def action_cancel(self) -> None:
            self.dismiss(None)

        @on(Button.Pressed)
        def button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "preset-cancel":
                self.action_cancel()
            elif event.button.id == "preset-continue":
                self.action_choose()

    class PuterInfoScreen(ModalScreen[None]):
        """Explain Puter's verified browser contract instead of skipping it."""

        BINDINGS = [
            Binding("enter", "close", "Close", priority=True),
            Binding("escape", "close", "Close", priority=True),
        ]
        DEFAULT_CSS = """
        PuterInfoScreen { align: center middle; background: #0e0c08 92%; }
        #puter-dialog { width: 78; height: auto; background: #1a1712;
          border: round #c6a56b; padding: 1 2; }
        #puter-dialog .title { text-style: bold; color: #e5e5e5; margin-bottom: 1; }
        #puter-contract { color: #c6bca8; margin-bottom: 1; }
        #puter-docs { color: #8a7e6a; margin-bottom: 1; }
        """

        def __init__(self, language: str = "ru") -> None:
            super().__init__()
            self.language = language

        def _label(self, russian: str, english: str) -> str:
            return english if self.language == "en" else russian

        def compose(self) -> ComposeResult:
            with Vertical(id="puter-dialog"):
                yield Static("Puter", classes="title")
                yield Static(
                    self._label(
                        "Puter не использует API-ключ или OpenAI-compatible Base URL. "
                        "По официальному контракту модели вызываются через puter.ai.chat(), "
                        "список — через puter.ai.listModels(), а вход выполняется пользователем "
                        "в браузере по модели user-pays. Поэтому обычная форма API-провайдера "
                        "здесь неприменима. Нативный браузерный мост Puter ещё не реализован; "
                        "KaroX не будет сохранять выдуманный endpoint.",
                        "Puter does not use an API key or an OpenAI-compatible Base URL. "
                        "Its official contract uses puter.ai.chat(), puter.ai.listModels(), "
                        "and browser user authentication under the user-pays model. The normal "
                        "API-provider form does not apply. A native Puter browser bridge is not "
                        "implemented yet, so KaroX will not save a fabricated endpoint.",
                    ),
                    id="puter-contract",
                )
                yield Static(
                    "docs.puter.com/AI/chat/  ·  docs.puter.com/AI/listModels/  ·  "
                    "docs.puter.com/user-pays-model/",
                    id="puter-docs",
                    markup=False,
                )
                yield Button(
                    self._label("Enter  Понятно", "Enter  Close"), id="puter-close"
                )

        def on_mount(self) -> None:
            self.query_one("#puter-close", Button).focus()

        def action_close(self) -> None:
            self.dismiss(None)

        @on(Button.Pressed, "#puter-close")
        def close_pressed(self) -> None:
            self.action_close()

    class ModelPickerScreen(ModalScreen[Optional[DiscoveredModel]]):
        """Search and choose every discovered model with ordinary keys."""

        BINDINGS = [
            Binding("enter", "choose", "Choose", priority=True),
            Binding("up", "previous_model", show=False, priority=True),
            Binding("down", "next_model", show=False, priority=True),
            Binding("escape", "cancel", "Cancel", priority=True),
        ]
        DEFAULT_CSS = """
        ModelPickerScreen { align: center middle; background: #0e0c08 92%; }
        #model-picker-dialog { width: 88; height: 92%; background: #1a1712;
          border: round #c6a56b; padding: 1 2; }
        #model-picker-dialog .title { text-style: bold; color: #e5e5e5; }
        #model-picker-dialog .hint { color: #8a7e6a; margin-bottom: 1; }
        #model-search { margin-bottom: 1; }
        #model-options { height: 1fr; border: round #4a4338;
          background: #1a1712; }
        #model-details { height: 3; color: #d4b676; margin-top: 1; }
        """

        def __init__(
            self, models: Sequence[DiscoveredModel], language: str = "ru"
        ) -> None:
            super().__init__()
            self.language = language
            self._models = list(models)
            self._visible = list(models)

        def _label(self, russian: str, english: str) -> str:
            return english if self.language == "en" else russian

        def compose(self) -> ComposeResult:
            with Vertical(id="model-picker-dialog"):
                yield Static(
                    self._label("Выберите модель", "Choose a model"), classes="title"
                )
                yield Static(
                    self._label(
                        "Введите часть имени • ↑/↓ — подсветка • Enter — выбрать • Esc — назад",
                        "Type to filter • ↑/↓ — highlight • Enter — choose • Esc — back",
                    ),
                    classes="hint",
                )
                yield Input(
                    placeholder=self._label("Поиск по всем моделям…", "Search all models…"),
                    id="model-search",
                )
                yield OptionList(id="model-options", markup=False)
                yield Static("", id="model-details", markup=False)

        def on_mount(self) -> None:
            self._render_models()
            self.query_one("#model-search", Input).focus()

        def _render_models(self) -> None:
            options = self.query_one("#model-options", OptionList)
            options.clear_options()
            options.add_option(
                Option(
                    self._label("Ввести Model ID вручную…", "Enter Model ID manually…"),
                    id="model-manual",
                )
            )
            options.add_options(
                Option(model.model_id, id=f"model-{index}")
                for index, model in enumerate(self._visible)
            )
            options.highlighted = 1 if self._visible else 0
            self._update_details()

        @on(Input.Changed, "#model-search")
        def filter_changed(self, event: Input.Changed) -> None:
            query = event.value.strip().casefold()
            self._visible = [
                model for model in self._models if query in model.model_id.casefold()
            ]
            self._render_models()

        @on(OptionList.OptionHighlighted, "#model-options")
        def model_highlighted(self) -> None:
            self._update_details()

        def _highlighted_model(self) -> Optional[DiscoveredModel]:
            index = self.query_one("#model-options", OptionList).highlighted
            if index is None or index == 0 or index - 1 >= len(self._visible):
                return None
            return self._visible[index - 1]

        def _update_details(self) -> None:
            details = self.query_one("#model-details", Static)
            model = self._highlighted_model()
            if model is None:
                details.update(
                    self._label(
                        "Ручной режим: точный Model ID можно ввести на следующем экране.",
                        "Manual mode: enter the exact Model ID on the next screen.",
                    )
                )
                return
            context = str(model.context_window) if model.context_window else "—"
            output = str(model.max_output_tokens) if model.max_output_tokens else "—"
            details.update(
                self._label(
                    f"Контекст: {context} токенов  •  Максимальный ответ: {output}",
                    f"Context: {context} tokens  •  Maximum output: {output}",
                )
            )

        def _move(self, direction: int) -> None:
            options = self.query_one("#model-options", OptionList)
            if options.option_count == 0:
                return
            current = options.highlighted if options.highlighted is not None else 0
            options.highlighted = (current + direction) % options.option_count
            options.scroll_to_highlight()
            self._update_details()

        def action_previous_model(self) -> None:
            self._move(-1)

        def action_next_model(self) -> None:
            self._move(1)

        def action_choose(self) -> None:
            model = self._highlighted_model()
            if model is None:
                self.dismiss(DiscoveredModel(""))
            else:
                self.dismiss(model)

        def action_cancel(self) -> None:
            self.dismiss(None)

    class ProviderLimitsScreen(ModalScreen[Optional[tuple[str, str]]]):
        """Optional model limits live on their own small screen."""

        BINDINGS = [
            Binding("escape", "cancel", "Cancel", priority=True),
            Binding("ctrl+enter", "save", "Save", priority=True),
        ]
        DEFAULT_CSS = """
        ProviderLimitsScreen { align: center middle; background: #0e0c08 92%; }
        #limits-dialog { width: 68; height: auto; background: #1a1712;
          border: round #c6a56b; padding: 1 2; }
        #limits-dialog .title { text-style: bold; color: #e5e5e5; }
        #limits-dialog .hint { color: #7a6f5e; margin-bottom: 1; }
        #limits-error { height: 1; color: #e0a3a3; }
        #limits-buttons { height: 3; align-horizontal: right; }
        #limits-buttons Button { margin-left: 1; }
        """

        def __init__(
            self, context: str = "", output: str = "", language: str = "ru"
        ) -> None:
            super().__init__()
            self.language = language
            self.context = context
            self.output = output

        def _label(self, russian: str, english: str) -> str:
            return english if self.language == "en" else russian

        def compose(self) -> ComposeResult:
            with Vertical(id="limits-dialog"):
                yield Static(
                    self._label("Контекст и лимиты", "Context and limits"),
                    classes="title",
                )
                yield Static(
                    self._label(
                        "Необязательно. Оставьте пустым, если провайдер не сообщил значения.",
                        "Optional. Leave blank when the provider did not report a value.",
                    ),
                    classes="hint",
                )
                yield Label(self._label("Контекст, токенов", "Context tokens"))
                yield Input(value=self.context, id="limits-context")
                yield Label(
                    self._label("Максимальный ответ, токенов", "Maximum output tokens")
                )
                yield Input(value=self.output, id="limits-output")
                yield Static("", id="limits-error", markup=False)
                with Horizontal(id="limits-buttons"):
                    yield Button(self._label("Отмена", "Cancel"), id="limits-cancel")
                    yield Button(
                        self._label("Сохранить", "Save"),
                        id="limits-save",
                        variant="primary",
                    )

        def on_mount(self) -> None:
            self.query_one("#limits-context", Input).focus()

        def action_save(self) -> None:
            values = (
                self.query_one("#limits-context", Input).value.strip(),
                self.query_one("#limits-output", Input).value.strip(),
            )
            try:
                if any(value and int(value) <= 0 for value in values):
                    raise ValueError
                if any(value and not value.isdigit() for value in values):
                    raise ValueError
            except ValueError:
                self.query_one("#limits-error", Static).update(
                    self._label(
                        "Введите положительные целые числа.",
                        "Enter positive whole numbers.",
                    )
                )
                return
            self.dismiss(values)

        def action_cancel(self) -> None:
            self.dismiss(None)

        @on(Button.Pressed)
        def button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "limits-save":
                self.action_save()
            elif event.button.id == "limits-cancel":
                self.action_cancel()

    class ManualModelScreen(ModalScreen[Optional[tuple[str, str, str]]]):
        """Manual model entry is a separate step, not an expanding form."""

        BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]
        DEFAULT_CSS = """
        ManualModelScreen { align: center middle; background: #0e0c08 92%; }
        #manual-model-dialog { width: 68; height: auto; background: #1a1712;
          border: round #c6a56b; padding: 1 2; }
        #manual-model-dialog .title { text-style: bold; color: #e5e5e5; }
        #manual-model-dialog .hint { color: #7a6f5e; margin-bottom: 1; }
        #manual-model-error { height: 1; color: #e0a3a3; }
        #manual-model-buttons { height: 3; align-horizontal: right; }
        #manual-model-buttons Button { margin-left: 1; }
        """

        def __init__(
            self,
            model_id: str = "",
            context: str = "",
            output: str = "",
            language: str = "ru",
        ) -> None:
            super().__init__()
            self.language = language
            self.model_id = model_id
            self.context = context
            self.output = output

        def _label(self, russian: str, english: str) -> str:
            return english if self.language == "en" else russian

        def compose(self) -> ComposeResult:
            with Vertical(id="manual-model-dialog"):
                yield Static(
                    self._label("Ввести модель вручную", "Enter a model manually"),
                    classes="title",
                )
                yield Static(
                    self._label(
                        "Требуется только Model ID. Лимиты можно оставить пустыми.",
                        "Only Model ID is required. Limits may be left blank.",
                    ),
                    classes="hint",
                )
                yield Label("Model ID")
                yield Input(value=self.model_id, id="manual-model-id")
                yield Label(self._label("Контекст, токенов", "Context tokens"))
                yield Input(value=self.context, id="manual-model-context")
                yield Label(
                    self._label("Максимальный ответ, токенов", "Maximum output tokens")
                )
                yield Input(value=self.output, id="manual-model-output")
                yield Static("", id="manual-model-error", markup=False)
                with Horizontal(id="manual-model-buttons"):
                    yield Button(
                        self._label("Отмена", "Cancel"), id="manual-model-cancel"
                    )
                    yield Button(
                        self._label("Использовать", "Use model"),
                        id="manual-model-save",
                        variant="primary",
                    )

        def on_mount(self) -> None:
            self.query_one("#manual-model-id", Input).focus()

        def action_save(self) -> None:
            values = (
                self.query_one("#manual-model-id", Input).value.strip(),
                self.query_one("#manual-model-context", Input).value.strip(),
                self.query_one("#manual-model-output", Input).value.strip(),
            )
            model_id, context, output = values
            if not model_id:
                message = self._label("Введите Model ID.", "Enter a Model ID.")
            elif any(value and not value.isdigit() for value in (context, output)):
                message = self._label(
                    "Лимиты должны быть положительными целыми числами.",
                    "Limits must be positive whole numbers.",
                )
            elif any(int(value) <= 0 for value in (context, output) if value):
                message = self._label(
                    "Лимиты должны быть положительными целыми числами.",
                    "Limits must be positive whole numbers.",
                )
            else:
                self.dismiss(values)
                return
            self.query_one("#manual-model-error", Static).update(message)

        def action_cancel(self) -> None:
            self.dismiss(None)

        @on(Button.Pressed)
        def button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "manual-model-save":
                self.action_save()
            elif event.button.id == "manual-model-cancel":
                self.action_cancel()

    class ProviderSetupScreen(ModalScreen[Optional[ProviderSetup]]):
        """Compact provider wizard; technical fields stay out of the main flow."""

        BINDINGS = [
            Binding("f5", "discover", "Найти модели", priority=True),
            Binding("f10", "save", "Проверить и сохранить", priority=True),
            Binding("escape", "cancel", "Отмена", priority=True),
        ]
        DEFAULT_CSS = """
        ProviderSetupScreen { align: center middle; background: #0e0c08 92%; }
        #provider-dialog { width: 82; height: 31; max-height: 94%; background: #1a1712;
          border: round #c6a56b; padding: 1 2; }
        #provider-dialog .title { text-style: bold; color: #e5e5e5; }
        #provider-dialog .hint { color: #7a6f5e; margin-bottom: 1; }
        #provider-fields { height: 1fr; }
        #provider-fields .field-label { color: #b3a990; margin-top: 0; }
        #provider-fields Input { margin-bottom: 1; }
        #provider-adapter { height: 8; border: round #4a4338; }
        #provider-adapter.preset-adapter { display: none; }
        .preset-technical { display: none; }
        #provider-endpoint { height: 1; color: #8a7e6a; margin-bottom: 1; }
        .manual-field { display: none; }
        #provider-actions { height: 3; }
        #provider-actions Button { width: 1fr; margin-right: 1; }
        #provider-summary { height: auto; min-height: 3; border: round #4a4338;
          padding: 0 1; margin-top: 1; color: #c6bca8; background: #1a1712; }
        #provider-discovered { display: none; }
        #provider-choose-model { display: none; }
        #provider-error { color: #d4b676; height: auto; min-height: 1;
          max-height: 3; margin-top: 1; text-wrap: wrap; overflow-y: auto; }
        #provider-error.status-success { color: #8aab7e; }
        #provider-error.status-warning { color: #c6a56b; }
        #provider-error.status-error { color: #e0a3a3; }
        #provider-buttons { height: 3; align-horizontal: right; }
        #provider-buttons Button { margin-left: 1; }
        """

        def __init__(
            self, language: str = "ru", preset: Optional[ProviderPreset] = None
        ) -> None:
            super().__init__()
            self.language = language
            self.preset = preset

        def _label(self, russian: str, english: str) -> str:
            return english if self.language == "en" else russian

        def compose(self) -> ComposeResult:
            adapter = self.preset.adapter_kind if self.preset else "openai_responses"
            provider_id = self.preset.preset_id if self.preset else "openai"
            base_url = (
                self.preset.base_url if self.preset else "https://api.openai.com/v1"
            )
            with Vertical(id="provider-dialog"):
                yield Static(
                    self._label(
                        f"Подключить {self.preset.display_name if self.preset else 'API-провайдера'}",
                        f"Connect {self.preset.display_name if self.preset else 'an API provider'}",
                    ),
                    classes="title",
                )
                yield Static(
                    self._label(
                        "Ключ → модели → проверка   •   Tab — перейти   •   Enter — выбрать   •   Esc — назад",
                        "Key → models → verify   •   Tab — move   •   Enter — choose   •   Esc — back",
                    ),
                    classes="hint",
                )
                with Vertical(id="provider-fields"):
                    if self.preset is None:
                        yield Label(
                            self._label("Тип API", "API type"), classes="field-label"
                        )
                    with RadioSet(
                        id="provider-adapter",
                        classes="preset-adapter" if self.preset is not None else None,
                    ):
                        yield RadioButton(
                            self._label(
                                "OpenAI Responses (только API с /responses)",
                                "OpenAI Responses (APIs with /responses)",
                            ),
                            value=adapter == "openai_responses",
                            id="adapter-openai",
                        )
                        yield RadioButton(
                            self._label(
                                "OpenAI-compatible Chat (/chat/completions; большинство сервисов)",
                                "OpenAI-compatible Chat (/chat/completions; most providers)",
                            ),
                            value=adapter == "openai_compatible_chat",
                            id="adapter-compatible",
                        )
                        yield RadioButton(
                            "Anthropic Messages",
                            value=adapter == "anthropic_messages",
                            id="adapter-anthropic",
                        )
                        yield RadioButton(
                            "Google Gemini",
                            value=adapter == "gemini_generate_content",
                            id="adapter-gemini",
                        )
                    yield Label(
                        self._label("Имя подключения", "Connection name"),
                        classes=(
                            "field-label preset-technical"
                            if self.preset is not None
                            else "field-label"
                        ),
                    )
                    yield Input(
                        value=provider_id,
                        placeholder="openai",
                        id="provider-id",
                        classes="preset-technical" if self.preset is not None else None,
                    )
                    if self.preset is not None and base_url:
                        yield Static(
                            f"Endpoint: {base_url}",
                            id="provider-endpoint",
                            markup=False,
                        )
                        yield Input(
                            value=base_url,
                            id="provider-url",
                            classes="preset-technical",
                        )
                    else:
                        yield Label("Base URL", classes="field-label")
                        yield Input(
                            value=base_url or "",
                            placeholder=self._label(
                                "Вставьте Base URL из документации провайдера",
                                "Enter the Base URL from provider documentation",
                            ),
                            id="provider-url",
                        )
                    yield Label(
                        self._label(
                            "API-ключ",
                            "API key",
                        ),
                        classes="field-label",
                    )
                    yield Input(
                        password=True,
                        placeholder=self._label(
                            "Вставьте ключ; он сохранится в системном хранилище",
                            "Paste the key; it will be stored in the system keyring",
                        ),
                        id="provider-key",
                    )
                    with Horizontal(id="provider-actions"):
                        yield Button(
                            self._label("Найти модели", "Find models"),
                            id="provider-discover",
                            variant="primary",
                        )
                        yield Button(
                            self._label("Ввести вручную", "Enter manually"),
                            id="provider-manual",
                        )
                        yield Button(
                            self._label("Лимиты", "Limits"),
                            id="provider-advanced",
                        )
                    yield Static(
                        self._label(
                            "Модель пока не выбрана. KaroX получит список моделей и их лимиты автоматически.",
                            "No model selected yet. KaroX will discover models and their limits automatically.",
                        ),
                        id="provider-summary",
                        markup=False,
                    )
                    yield Static("", id="provider-discovered", markup=False)
                    yield Button(
                        self._label("Выбрать модель", "Choose model"),
                        id="provider-choose-model",
                        disabled=True,
                    )
                    yield Label(
                        "Model ID",
                        id="provider-model-label",
                        classes="field-label manual-field",
                    )
                    yield Input(
                        placeholder=self._label("например gpt-5", "for example gpt-5"),
                        id="provider-model",
                        classes="manual-field",
                    )
                    yield Label(
                        self._label("Контекст, токенов", "Context tokens"),
                        id="provider-context-label",
                        classes="field-label manual-field",
                    )
                    yield Input(
                        placeholder=self._label(
                            "Провайдер не сообщил — можно оставить пустым",
                            "Not reported by provider — may be left empty",
                        ),
                        id="provider-context",
                        classes="manual-field",
                    )
                    yield Label(
                        self._label(
                            "Максимальный ответ, токенов", "Maximum output tokens"
                        ),
                        id="provider-output-label",
                        classes="field-label manual-field",
                    )
                    yield Input(
                        placeholder=self._label(
                            "Провайдер не сообщил — можно оставить пустым",
                            "Not reported by provider — may be left empty",
                        ),
                        id="provider-output",
                        classes="manual-field",
                    )
                yield Static("", id="provider-error", markup=False)
                with Horizontal(id="provider-buttons"):
                    yield Button(
                        self._label("Esc  Отмена", "Esc  Cancel"),
                        id="provider-cancel",
                    )
                    yield Button(
                        self._label(
                            "Проверить подключение",
                            "Verify connection",
                        ),
                        id="provider-save",
                        variant="success",
                        disabled=True,
                    )

        def on_mount(self) -> None:
            target = "#provider-key" if self.preset is not None else "#provider-adapter"
            self.query_one(target).focus()

        def _show_manual_fields(self) -> None:
            self.app.push_screen(
                ManualModelScreen(
                    self.query_one("#provider-model", Input).value,
                    self.query_one("#provider-context", Input).value,
                    self.query_one("#provider-output", Input).value,
                    self.language,
                ),
                self._manual_model_done,
            )

        def _manual_model_done(
            self, values: Optional[tuple[str, str, str]]
        ) -> None:
            if values is not None:
                model_id, context, output = values
                self._discovered_index = -1
                self.query_one("#provider-model", Input).value = model_id
                self.query_one("#provider-context", Input).value = context
                self.query_one("#provider-output", Input).value = output
                self._update_summary()
                self._set_status(
                    self._label(
                        "Модель введена вручную. Теперь проверьте подключение.",
                        "Model entered manually. Now verify the connection.",
                    ),
                    "info",
                )
                self.query_one("#provider-save", Button).focus()
            else:
                self.query_one("#provider-manual", Button).focus()

        def _open_limits(self) -> None:
            self.app.push_screen(
                ProviderLimitsScreen(
                    self.query_one("#provider-context", Input).value,
                    self.query_one("#provider-output", Input).value,
                    self.language,
                ),
                self._limits_done,
            )

        def _limits_done(self, values: Optional[tuple[str, str]]) -> None:
            if values is not None:
                context, output = values
                self.query_one("#provider-context", Input).value = context
                self.query_one("#provider-output", Input).value = output
                self._update_summary()
            self.query_one("#provider-advanced", Button).focus()

        def _update_summary(self, model: Optional[DiscoveredModel] = None) -> None:
            if model is None:
                model_id = self.query_one("#provider-model", Input).value.strip()
                if not model_id:
                    return
                context_value = self.query_one("#provider-context", Input).value.strip()
                output_value = self.query_one("#provider-output", Input).value.strip()
            else:
                model_id = model.model_id
                context_value = str(model.context_window) if model.context_window else "—"
                output_value = (
                    str(model.max_output_tokens) if model.max_output_tokens else "—"
                )
            self.query_one("#provider-summary", Static).update(
                self._label(
                    f"Модель: {model_id}\nКонтекст: {context_value or '—'}  •  Максимальный ответ: {output_value or '—'}",
                    f"Model: {model_id}\nContext: {context_value or '—'}  •  Maximum output: {output_value or '—'}",
                )
            )
            self.query_one("#provider-save", Button).disabled = False

        @on(Input.Changed, "#provider-model")
        def manual_model_changed(self, event: Input.Changed) -> None:
            self.query_one("#provider-save", Button).disabled = not bool(
                event.value.strip()
            )
            self._update_summary()

        def _set_status(self, message: str, kind: str = "info") -> None:
            status = self.query_one("#provider-error", Static)
            colors = {
                "info": "#d4b676",
                "success": "#8aab7e",
                "warning": "#c6a56b",
                "error": "#e0a3a3",
            }
            for name in ("success", "warning", "error"):
                status.set_class(kind == name, f"status-{name}")
            status.styles.color = colors.get(kind, colors["info"])
            status.update(message)

        @on(RadioSet.Changed, "#provider-adapter")
        def adapter_changed(self, event: RadioSet.Changed) -> None:
            defaults = {
                "adapter-openai": ("openai", "https://api.openai.com/v1"),
                "adapter-compatible": (
                    "compatible",
                    "http://127.0.0.1:11434/v1",
                ),
                "adapter-anthropic": (
                    "anthropic",
                    "https://api.anthropic.com/v1",
                ),
                "adapter-gemini": (
                    "gemini",
                    "https://generativelanguage.googleapis.com/v1beta",
                ),
            }
            value = event.pressed.id if event.pressed is not None else ""
            if value in defaults:
                provider, url = defaults[value]
                if (
                    self.preset is not None
                    and self.preset.adapter_kind
                    == {
                        "adapter-openai": "openai_responses",
                        "adapter-compatible": "openai_compatible_chat",
                        "adapter-anthropic": "anthropic_messages",
                        "adapter-gemini": "gemini_generate_content",
                    }.get(value)
                    and self.query_one("#provider-id", Input).value
                    == self.preset.preset_id
                ):
                    return
                self.query_one("#provider-id", Input).value = provider
                self.query_one("#provider-url", Input).value = url

        def _apply_discovered(self, model: DiscoveredModel) -> None:
            self.query_one("#provider-model", Input).value = model.model_id
            self.query_one("#provider-context", Input).value = (
                str(model.context_window) if model.context_window else ""
            )
            self.query_one("#provider-output", Input).value = (
                str(model.max_output_tokens) if model.max_output_tokens else ""
            )
            self._update_summary(model)

        def action_next_model(self) -> None:
            models = getattr(self, "_discovered_models", [])
            if not models:
                return
            current = getattr(self, "_discovered_index", -1)
            self._discovered_index = (current + 1) % len(models)
            self._apply_discovered(models[self._discovered_index])
            self._render_discovered()

        def action_previous_model(self) -> None:
            models = getattr(self, "_discovered_models", [])
            if not models:
                return
            current = getattr(self, "_discovered_index", 0)
            self._discovered_index = (current - 1) % len(models)
            self._apply_discovered(models[self._discovered_index])
            self._render_discovered()

        def _render_discovered(self) -> None:
            models = getattr(self, "_discovered_models", [])
            current = getattr(self, "_discovered_index", -1)
            if not models:
                message = self._label(
                    "Провайдер не вернул моделей. Введите Model ID вручную.",
                    "The provider returned no models. Enter a Model ID manually.",
                )
            elif current < 0:
                message = self._label(
                    f"Найдено: {len(models)}. Модель ещё не выбрана.",
                    f"Found: {len(models)}. No model is selected yet.",
                )
            else:
                message = self._label(
                    f"Найдено: {len(models)}. Выбрано: {models[current].model_id}",
                    f"Found: {len(models)}. Selected: {models[current].model_id}",
                )
            if current < 0:
                self.query_one("#provider-summary", Static).update(message)
            self.query_one("#provider-discovered", Static).update(message)

        def _open_model_picker(self) -> None:
            models = getattr(self, "_discovered_models", [])
            if not models:
                self._set_status(
                    self._label(
                        "Сначала получите список моделей клавишей F5.",
                        "Fetch the model list with F5 first.",
                    ),
                    "warning",
                )
                return
            self.app.push_screen(
                ModelPickerScreen(models, self.language), self._model_picker_done
            )

        def _model_picker_done(self, model: Optional[DiscoveredModel]) -> None:
            if model is None:
                self.query_one("#provider-choose-model", Button).focus()
                return
            if not model.model_id:
                self._discovered_index = -1
                self.query_one("#provider-model", Input).value = ""
                self.query_one("#provider-context", Input).value = ""
                self.query_one("#provider-output", Input).value = ""
                self._render_discovered()
                self._set_status(
                    self._label(
                        "Введите точный Model ID вручную.",
                        "Enter the exact Model ID manually.",
                    ),
                    "info",
                )
                self._show_manual_fields()
                return
            models = getattr(self, "_discovered_models", [])
            self._discovered_index = models.index(model)
            self._apply_discovered(model)
            self._render_discovered()
            self._set_status(
                self._label(
                    "Модель выбрана. Теперь проверьте подключение.",
                    "Model selected. Now verify the connection.",
                ),
                "success",
            )
            self.query_one("#provider-save", Button).focus()

        def _setup_from_form(self, *, require_model: bool) -> ProviderSetup:
            def positive(field: str) -> Optional[int]:
                value = self.query_one(field, Input).value.strip()
                if not value:
                    return None
                parsed = int(value)
                if parsed <= 0:
                    raise ValueError(
                        self._label(
                            "лимиты токенов должны быть положительными",
                            "token limits must be positive",
                        )
                    )
                return parsed

            setup = ProviderSetup(
                provider_id=self.query_one("#provider-id", Input).value,
                adapter=self._adapter_value(),
                base_url=self.query_one("#provider-url", Input).value,
                model_id=self.query_one("#provider-model", Input).value,
                api_key=self.query_one("#provider-key", Input).value,
                context_window=positive("#provider-context"),
                max_output_tokens=positive("#provider-output"),
            )
            if not setup.provider_id.strip() or not setup.base_url.strip():
                raise ValueError(
                    self._label(
                        "укажите имя подключения и Base URL",
                        "enter a connection name and Base URL",
                    )
                )
            if require_model and not setup.model_id.strip():
                raise ValueError(
                    self._label(
                        "выберите найденную модель или введите Model ID вручную",
                        "select a discovered model or enter a Model ID",
                    )
                )
            return setup

        def _adapter_value(self) -> str:
            pressed = self.query_one("#provider-adapter", RadioSet).pressed_button
            values = {
                "adapter-openai": "openai_responses",
                "adapter-compatible": "openai_compatible_chat",
                "adapter-anthropic": "anthropic_messages",
                "adapter-gemini": "gemini_generate_content",
            }
            if pressed is None or pressed.id not in values:
                raise ValueError(self._label("выберите тип API", "select an API type"))
            return values[pressed.id]

        def action_discover(self) -> None:
            try:
                setup = self._setup_from_form(require_model=False)
            except (TypeError, ValueError) as exc:
                self._set_status(str(exc), "error")
                return
            self._set_status(
                self._label(
                    "Проверяю API и получаю список моделей…",
                    "Checking the API and fetching models…",
                ),
                "info",
            )

            def execute() -> None:
                try:
                    result = _discover_models_result(setup)
                except Exception as exc:
                    self.app.call_from_thread(self._discovery_failed, exc, setup)
                    return
                self.app.call_from_thread(self._discovery_ready, result)

            self.run_worker(
                execute, thread=True, exclusive=True, group="provider-discovery"
            )

        def _discovery_ready(self, result: ModelDiscovery) -> None:
            models = list(result.models)
            self._discovered_models = models
            self._discovered_index = -1
            self.query_one("#provider-url", Input).value = result.base_url
            self.query_one("#provider-model", Input).value = ""
            self.query_one("#provider-context", Input).value = ""
            self.query_one("#provider-output", Input).value = ""
            self.query_one("#provider-choose-model", Button).disabled = not bool(models)
            self._render_discovered()
            endpoint_note = ""
            if len(result.attempted_urls) > 1:
                endpoint_note = self._label(
                    f" Base URL исправлен автоматически: {result.base_url}.",
                    f" Base URL corrected automatically: {result.base_url}.",
                )
            self._set_status(
                self._label(
                    f"Найдено моделей: {len(models)}.{endpoint_note}",
                    f"Found models: {len(models)}.{endpoint_note}",
                ),
                "success" if models else "warning",
            )
            if models:
                self._open_model_picker()
            else:
                self.query_one("#provider-model", Input).focus()

        def _discovery_failed(self, error: Exception, setup: ProviderSetup) -> None:
            self._set_status(
                _friendly_discovery_error(
                    error, self.language, base_url=setup.base_url
                ),
                "error",
            )

        def action_save(self) -> None:
            try:
                setup = self._setup_from_form(require_model=True)
            except (TypeError, ValueError) as exc:
                self._set_status(str(exc), "error")
                return
            self._set_status(
                self._label(
                    "Сохраняю ключ в OS keyring и выполняю минимальный тестовый запрос…",
                    "Saving the key in the OS keyring and sending a minimal test request…",
                ),
                "info",
            )
            save_button = self.query_one("#provider-save", Button)
            save_button.disabled = True
            save_button.label = self._label("Проверяю…", "Verifying…")

            def execute() -> None:
                try:
                    _probe_provider(setup)
                    _save_provider(setup)
                except Exception as exc:
                    self.app.call_from_thread(self._probe_failed, exc, setup)
                    return
                self.app.call_from_thread(self.dismiss, setup)

            self.run_worker(execute, thread=True, exclusive=True, group="provider-save")

        def _probe_failed(self, error: Exception, setup: ProviderSetup) -> None:
            self._set_status(
                _friendly_probe_error(error, self.language, setup=setup), "error"
            )
            save_button = self.query_one("#provider-save", Button)
            save_button.disabled = False
            save_button.label = self._label(
                "Повторить проверку", "Retry verification"
            )

        def action_cancel(self) -> None:
            self.dismiss(None)

        @on(Button.Pressed)
        def button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "provider-cancel":
                self.action_cancel()
                return
            if event.button.id == "provider-discover":
                self.action_discover()
            elif event.button.id == "provider-manual":
                self._show_manual_fields()
            elif event.button.id == "provider-advanced":
                self._open_limits()
            elif event.button.id == "provider-choose-model":
                self._open_model_picker()
            elif event.button.id == "provider-save":
                self.action_save()

    class BridgeSetupScreen(ModalScreen[Optional[BridgeSetup]]):
        BINDINGS = [
            Binding("f10", "start", "Запустить", priority=True),
            Binding("escape", "cancel", "Отмена", priority=True),
        ]
        DEFAULT_CSS = """
        BridgeSetupScreen { align: center middle; background: #0e0c08 92%; }
        #bridge-dialog { width: 86; max-width: 96%; height: auto; max-height: 96%;
          background: #1a1712; border: round #c6a56b; padding: 1 2; }
        #bridge-dialog .title { text-style: bold; color: #e5e5e5; margin-bottom: 1; }
        #bridge-dialog .hint { color: #8a7e6a; margin-bottom: 1; }
        #bridge-dialog .section { text-style: bold; color: #d4b676; margin: 1 0 0 0; }
        #bridge-profile { height: auto; border: round #4a4338; }
        #bridge-tunnel-kind { height: auto; border: round #4a4338; }
        #bridge-dialog Checkbox { margin: 0; }
        #bridge-port { height: 3; }
        #bridge-error { color: #e0a3a3; min-height: 1; }
        #bridge-buttons { height: 3; align-horizontal: right; }
        #bridge-buttons Button { margin-left: 1; }
        """

        def __init__(self, language: str = "ru", default_tunnel: str = "cloudflare") -> None:
            super().__init__()
            self.language = language
            self._default_tunnel = default_tunnel

        def _label(self, russian: str, english: str) -> str:
            return english if self.language == "en" else russian

        def compose(self) -> ComposeResult:
            with VerticalScroll(id="bridge-dialog"):
                yield Static(
                    self._label(
                        "Подключение сайта или внешнего агента",
                        "Connect a website or external agent",
                    ),
                    classes="title",
                )
                yield Static(
                    self._label(
                        "Tab / Shift+Tab — переход • Enter/Space — выбор • F10 — запуск • Esc — отмена",
                        "Tab / Shift+Tab — move • Enter/Space — select • F10 — start • Esc — cancel",
                    ),
                    classes="hint",
                )
                yield Static(
                    self._label("Тип подключения", "Connection type"),
                    classes="section",
                )
                with RadioSet(id="bridge-profile"):
                    yield RadioButton(
                        "PromptQL (OpenAPI)", value=True, id="profile-promptql"
                    )
                    yield RadioButton("Notion Custom Agent (MCP)", id="profile-notion")
                    yield RadioButton(
                        "ChatGPT Web (OAuth MCP)", id="profile-chatgpt-web"
                    )
                    yield RadioButton(
                        "Claude Web (OAuth MCP)", id="profile-claude-web"
                    )
                    yield RadioButton(
                        "Generic Streamable HTTP MCP", id="profile-generic"
                    )
                    yield RadioButton(
                        "HyperAgent (MCP, experimental)", id="profile-hyperagent"
                    )
                yield Static(
                    self._label("Локальный порт", "Local port"),
                    classes="section",
                )
                yield Input(
                    value="8765",
                    placeholder=self._label("Локальный порт", "Local port"),
                    id="bridge-port",
                )
                yield Static(
                    self._label(
                        "Инструменты KaroX Core (что разрешить внешнему агенту)",
                        "KaroX Core tools (what the external agent may do)",
                    ),
                    classes="section",
                )
                yield Checkbox(
                    self._label("Чтение файлов", "Read files"),
                    value=True,
                    id="tool-read",
                )
                yield Checkbox(
                    self._label("Список файлов", "List files"),
                    value=True,
                    id="tool-list",
                )
                yield Checkbox(
                    self._label("Запись файлов", "Write files"),
                    value=True,
                    id="tool-write",
                )
                yield Checkbox("Git status", value=True, id="tool-status")
                yield Checkbox("Git diff", value=True, id="tool-diff")
                yield Checkbox(
                    self._label("Запуск проверок", "Run checks"),
                    value=True,
                    id="tool-checks",
                )
                yield Checkbox(
                    self._label(
                        "Браузер: чтение (snapshot, скриншот, консоль, сеть)",
                        "Browser: read (snapshot, screenshot, console, network)",
                    ),
                    value=True,
                    id="tool-browser-read",
                )
                yield Checkbox(
                    self._label(
                        "Браузер: ввод (open, click, fill, select, press, close)",
                        "Browser: input (open, click, fill, select, press, close)",
                    ),
                    value=True,
                    id="tool-browser-input",
                )
                yield Checkbox(
                    self._label(
                        "Dev-сервер: статус и логи (только чтение)",
                        "Dev server: status and logs (read-only)",
                    ),
                    value=True,
                    id="tool-server-read",
                )
                yield Checkbox(
                    self._label(
                        "Dev-сервер: запуск и остановка (start:safe)",
                        "Dev server: start and stop (start:safe)",
                    ),
                    value=True,
                    id="tool-server-input",
                )
                yield Static(
                    self._label(
                        "Публичный доступ (как внешний агент дойдёт до KaroX)",
                        "Public access (how the external agent reaches KaroX)",
                    ),
                    classes="section",
                )
                yield Static(
                    self._label(
                        "Для Notion и других облачных агентов выбирайте Tailscale Funnel или Cloudflare.",
                        "For Notion and other cloud agents pick Tailscale Funnel or Cloudflare.",
                    ),
                    classes="hint",
                )
                with RadioSet(id="bridge-tunnel-kind"):
                    yield RadioButton(
                        self._label(
                            "Локально — только этот компьютер (не для Notion и облака)",
                            "Local — this machine only (not for Notion/cloud)",
                        ),
                        value=self._default_tunnel == "none",
                        id="tunnel-none",
                    )
                    yield RadioButton(
                        self._label(
                            "Cloudflare Tunnel — публичный HTTPS",
                            "Cloudflare Tunnel — public HTTPS",
                        ),
                        value=self._default_tunnel == "cloudflare",
                        id="tunnel-cloudflare",
                    )
                    yield RadioButton(
                        self._label(
                            "Tailscale Funnel — публичный HTTPS (*.ts.net)",
                            "Tailscale Funnel — public HTTPS (*.ts.net)",
                        ),
                        value=self._default_tunnel == "tailscale",
                        id="tunnel-tailscale",
                    )
                yield Label("", id="bridge-error")
                with Horizontal(id="bridge-buttons"):
                    yield Button(
                        self._label("Esc  Отмена", "Esc  Cancel"),
                        id="bridge-cancel",
                    )
                    yield Button(
                        self._label("F10  Запустить мост", "F10  Start bridge"),
                        id="bridge-start",
                    )

        def on_mount(self) -> None:
            self.query_one("#bridge-profile", RadioSet).focus()
            # Default tunnel tracks the profile: Notion (cloud agent) prefers
            # Tailscale Funnel; everything else defaults to Cloudflare.
            self._apply_default_tunnel_for_profile()

        def _apply_default_tunnel_for_profile(self) -> None:
            # Default tunnel tracks the profile: Notion (a cloud-hosted agent)
            # needs a public URL, so it defaults to Tailscale Funnel; the other
            # profiles default to Cloudflare.  Setting a single RadioButton's
            # ``value = True`` does not reliably deselect its siblings inside a
            # RadioSet (the set's internal ``pressed_button`` can desync), so
            # we set all three explicitly to guarantee exactly one is selected.
            target = "tailscale" if self._profile_value() == "notion" else "cloudflare"
            self._default_tunnel = target
            states = {
                "tunnel-none": target == "none",
                "tunnel-cloudflare": target == "cloudflare",
                "tunnel-tailscale": target == "tailscale",
            }
            for widget_id, is_on in states.items():
                self.query_one(f"#{widget_id}", RadioButton).value = is_on

        @on(RadioSet.Changed, "#bridge-profile")
        def profile_changed(self, event: RadioSet.Changed) -> None:  # noqa: ARG002
            self._apply_default_tunnel_for_profile()

        def action_cancel(self) -> None:
            self.dismiss(None)

        def action_start(self) -> None:
            self._submit()

        @on(Button.Pressed)
        def button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "bridge-cancel":
                self.action_cancel()
                return
            if event.button.id == "bridge-start":
                self._submit()

        def _submit(self) -> None:
            try:
                port = int(self.query_one("#bridge-port", Input).value)
                if not 1 <= port <= 65535:
                    raise ValueError
            except ValueError:
                self.query_one("#bridge-error", Label).update(
                    self._label(
                        "Порт должен быть от 1 до 65535.",
                        "Port must be between 1 and 65535.",
                    )
                )
                return
            tool_ids = {
                "tool-read": ("karox.repo.read_file",),
                "tool-list": ("karox.repo.list_files",),
                "tool-write": ("karox.repo.write_file",),
                "tool-status": ("karox.git.status",),
                "tool-diff": ("karox.git.diff",),
                "tool-checks": ("karox.checks.run",),
                "tool-browser-read": (
                    "karox.browser.snapshot",
                    "karox.browser.get_text",
                    "karox.browser.console",
                    "karox.browser.network_failures",
                    "karox.browser.screenshot",
                    "karox.artifact.get",
                    "karox.artifact.read_image",
                ),
                "tool-browser-input": (
                    "karox.browser.open",
                    "karox.browser.click",
                    "karox.browser.fill",
                    "karox.browser.select",
                    "karox.browser.press",
                    "karox.browser.close",
                ),
                "tool-server-read": (
                    "karox.dev_server.status",
                    "karox.dev_server.logs",
                ),
                "tool-server-input": (
                    "karox.dev_server.start",
                    "karox.dev_server.stop",
                ),
            }
            tools = tuple(
                tool
                for widget_id, tool_names in tool_ids.items()
                if self.query_one(f"#{widget_id}", Checkbox).value
                for tool in tool_names
            )
            if not tools:
                self.query_one("#bridge-error", Label).update(
                    self._label(
                        "Выберите хотя бы один инструмент.",
                        "Select at least one tool.",
                    )
                )
                return
            profile = self._profile_value()
            tunnel = self._tunnel_value()
            if profile in WEB_BRIDGE_PROFILES and tunnel == "none":
                self.query_one("#bridge-error", Label).update(
                    self._label(
                        "ChatGPT Web и Claude Web требуют публичный HTTPS: "
                        "выберите Cloudflare или Tailscale Funnel.",
                        "ChatGPT Web and Claude Web require public HTTPS: "
                        "choose Cloudflare or Tailscale Funnel.",
                    )
                )
                return
            self.dismiss(
                BridgeSetup(
                    profile=profile,
                    port=port,
                    tools=tools,
                    tunnel_provider=tunnel,
                )
            )

        def _tunnel_value(self) -> str:
            # RadioSet.pressed_button can desync from the buttons' ``value``
            # after we set the defaults programmatically (see
            # ``_apply_default_tunnel_for_profile``), so we read the actual
            # checkbox state of each RadioButton — that is the source of truth.
            values = {
                "tunnel-none": "none",
                "tunnel-cloudflare": "cloudflare",
                "tunnel-tailscale": "tailscale",
            }
            for widget_id, provider in values.items():
                if self.query_one(f"#{widget_id}", RadioButton).value:
                    return provider
            return self._default_tunnel

        def _profile_value(self) -> str:
            pressed = self.query_one("#bridge-profile", RadioSet).pressed_button
            values = {
                "profile-promptql": "promptql",
                "profile-notion": "notion",
                "profile-chatgpt-web": "chatgpt-web",
                "profile-claude-web": "claude-web",
                "profile-generic": "generic-streamable-http",
                "profile-hyperagent": "hyperagent",
            }
            if pressed is None or pressed.id not in values:
                raise ValueError(
                    self._label("выберите тип подключения", "select a connection type")
                )
            return values[pressed.id]

    class ConfirmScreen(ModalScreen[Optional[bool]]):
        """Small Yes/No modal used to ask the user's permission before
        side-effecting actions like installing or launching Tailscale."""

        BINDINGS = [
            Binding("enter", "yes", "Да", priority=True),
            Binding("escape", "no", "Нет", priority=True),
        ]
        DEFAULT_CSS = """
        ConfirmScreen { align: center middle; background: #0e0c08 92%; }
        #confirm-dialog { width: 70; height: auto; max-height: 80%;
          background: #1a1712; border: round #c6a56b; padding: 1 2; }
        #confirm-dialog .title { text-style: bold; color: #e5e5e5; margin-bottom: 1; }
        #confirm-dialog .body { color: #dcdcdc; margin-bottom: 1; }
        #confirm-buttons { height: 3; align-horizontal: right; }
        #confirm-buttons Button { margin-left: 1; }
        """

        def __init__(
            self,
            title: str,
            body: str,
            *,
            yes: str = "Enter  Да",
            no: str = "Esc  Нет",
            language: str = "ru",
        ) -> None:
            super().__init__()
            self._title = title
            self._body = body
            self._yes = yes
            self._no = no
            self.language = language

        def compose(self) -> ComposeResult:
            with Vertical(id="confirm-dialog"):
                yield Static(self._title, classes="title")
                yield Static(self._body, classes="body")
                with Horizontal(id="confirm-buttons"):
                    yield Button(self._no, id="confirm-no")
                    yield Button(self._yes, id="confirm-yes")

        def on_mount(self) -> None:
            self.query_one("#confirm-yes", Button).focus()

        def action_yes(self) -> None:
            self.dismiss(True)

        def action_no(self) -> None:
            self.dismiss(None)

        @on(Button.Pressed)
        def button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "confirm-yes":
                self.dismiss(True)
            elif event.button.id == "confirm-no":
                self.dismiss(None)


    def _selectable_text(widget: Any) -> str:
        """The text a user would get by selecting the whole of ``widget``.

        Asked through ``Widget.get_selection`` with an unbounded selection, which
        is the same public route Textual's own copy action takes. Reading a
        widget's renderable directly gives the wrong answer twice over: a
        ``Static`` holds a rendered visual rather than the ``Content`` it was
        given, and a ``Markdown`` holds its source, so ``**bold**`` would be
        copied with the asterisks a reader never saw.

        Descendants are included because a ``Markdown`` keeps its text in child
        blocks -- paragraphs, list items, fenced code -- and none of it is on the
        widget itself.
        """
        parts: List[str] = []
        for node in widget.walk_children(with_self=True):
            try:
                extracted = node.get_selection(Selection(None, None))
            except Exception:
                continue
            if extracted and extracted[0]:
                parts.append(extracted[0])
        return "\n".join(parts)

    class MessageBlock(Static):
        """One user message, notice or status line in the transcript.

        ``Static`` holds a :class:`~textual.content.Content`, and
        ``Widget.get_selection`` extracts from exactly that, so Textual's own
        selection returns the characters the user dragged over -- to the
        character, across a resize, with the highlight drawn for free.

        The frame is CSS rather than a Rich ``Panel`` and that is the load-bearing
        detail. ``Widget.get_selection`` returns ``None`` for anything whose
        ``_render()`` is not a ``Content`` or a ``Text``, so a message wrapped in a
        ``Panel`` cannot be selected at all -- which is why the transcript used to
        reimplement selection by hand and got it wrong. A CSS border also fills the
        available width and re-wraps when the window changes, neither of which a
        renderable laid out once at write time can do.
        """

        def __init__(self, text: str, *, title: str = "", classes: str = "") -> None:
            super().__init__(Content(text), classes=classes)
            self._border_title_text = title

        def on_mount(self) -> None:
            if self._border_title_text:
                self.border_title = self._border_title_text

        @property
        def plain_text(self) -> str:
            """The message as the user could copy it."""
            return _selectable_text(self)

    class AssistantMessage(Markdown):
        """An answer, rendered by Textual's own markdown widget.

        Chosen over the Rich renderables KaroX used to build because a
        ``Markdown`` is a tree of widgets rather than a block of pre-rendered
        lines: every paragraph, list item and fenced code block is a ``Static``
        holding a ``Content``, so all of them select natively -- including the code
        block, which is the thing users copy most and the one the previous
        implementation could not give them.

        Being a widget tree also means it re-wraps when the window changes and
        uses the width it is given, which pre-rendered lines never did.
        """

        def __init__(self, markdown: str, *, title: str = "", classes: str = "") -> None:
            super().__init__(markdown, classes=classes)
            self._border_title_text = title

        def on_mount(self) -> None:
            if self._border_title_text:
                self.border_title = self._border_title_text

        @property
        def plain_text(self) -> str:
            """The rendered answer, not its markdown source.

            The source is what the model sent; the rendered text is what the user
            read and what they expect on the clipboard. Returning the source would
            hand back ``**bold**`` and fenced-code backticks that were never on
            screen.
            """
            return _selectable_text(self)

    class TranscriptView(VerticalScroll):
        """The conversation: one selectable widget per message.

        Replaces a ``RichLog`` subclass that carried hand-written mouse handlers, a
        ``render_line`` override reaching into a private attribute, and a table
        mapping screen rows onto whole messages. All of it existed because
        ``RichLog`` does not take part in Textual's selection machinery; none of it
        is needed once each message is a widget that does.
        """

        # A resize re-lays out every block, and that cost is linear in how many
        # there are: measured at 0.86s for 242 blocks on this machine. Three
        # hundred keeps a resize under a second in the worst case while holding far
        # more conversation than a window can show. The session record keeps the
        # full history regardless -- this bounds the *view*, not the transcript.
        MAX_BLOCKS = 300

        def _append(self, block: Any) -> Any:
            self.mount(block)
            self._trim()
            # A message arriving while the user is reading further up must not yank
            # the viewport, but one arriving at the bottom must stay visible.
            if self.is_vertical_scroll_end:
                self.scroll_end(animate=False)
            return block

        def _trim(self) -> None:
            blocks = list(self.children)
            excess = len(blocks) - self.MAX_BLOCKS
            for block in blocks[:excess]:
                block.remove()

        def add_line(self, text: str) -> Any:
            """An unframed line: the welcome, a status line, a short confirmation.

            Kept distinct from :meth:`add_notice` because a frame is a claim that
            something needs attention. Boxing every status line spends that signal
            on nothing and makes the screen busier than the conversation in it.
            """
            return self._append(MessageBlock(text, classes="message message-line"))

        def add_user(self, text: str, *, title: str) -> Any:
            return self._append(
                MessageBlock(text, title=title, classes="message message-user")
            )

        def add_assistant(self, markdown: str, *, title: str = "KaroX") -> Any:
            return self._append(
                AssistantMessage(
                    markdown, title=title, classes="message message-assistant"
                )
            )

        def add_notice(self, text: str, kind: str = "info") -> Any:
            return self._append(
                MessageBlock(text, classes=f"message message-notice notice-{kind}")
            )

        def clear(self) -> None:
            for block in list(self.children):
                block.remove()

        @property
        def plain_text(self) -> str:
            """Every message as plain text, in order.

            Used by tests and by the copy fallback. Reading it from the widgets
            themselves means there is no second transcript to keep in step with the
            first -- the parallel plain-text list this class replaced is exactly
            what drifted out of step with what was on screen.
            """
            return "\n".join(
                str(getattr(block, "plain_text", "")) for block in self.children
            )

        def clear_selection(self) -> None:
            """Drop any selection over the transcript.

            Textual owns the selection now, so this defers to the screen instead of
            editing ``screen.selections`` directly.
            """
            with contextlib.suppress(Exception):
                self.screen.clear_selection()

    class KaroXApp(App[int]):
        """Human-facing KaroX terminal application."""

        TITLE = "KaroX"
        SUB_TITLE = "локальный агент"
        ENABLE_COMMAND_PALETTE = True
        BINDINGS = [
            Binding("ctrl+p", "command_palette", "Команды", show=False),
            Binding("ctrl+s", "onboarding", "Подключения", show=False),
            Binding("ctrl+b", "bridge", "Сайт / MCP", show=False),
            Binding("ctrl+l", "clear_log", "Очистить", show=False),
            Binding("ctrl+q", "quit", "Выход", show=False),
            Binding("escape", "stop_agent", "Стоп", show=False, priority=True),
            # Ctrl+C copies. It used to mean "stop the task, or copy if idle",
            # which meant the copy key destroyed work in progress during exactly
            # the period a user most wants to copy an error scrolling past. Esc
            # already stops, and now says so instead of sharing a key.
            Binding("ctrl+c", "copy_selection", "Копировать", show=False, priority=True),
            Binding(
                "ctrl+shift+c",
                "copy_selection",
                "Копировать",
                show=False,
                priority=True,
            ),
        ]
        # The activity panel is four lines tall, so more than a few steps would
        # scroll the oldest out of sight anyway.
        MAX_VISIBLE_STEPS = 6
        CSS = """
        Screen { background: #121212; color: #dcdcdc; }
        /* The chrome above and below the chat came to seventeen rows before a
           single word of conversation, which in a small window left the answer a
           few lines to live in. The blank row over the title and the one under
           the composer were the two that bought nothing. */
        #brand { height: auto; padding: 0 2; background: #181511;
          border-bottom: solid #4a4338; color: #e5e5e5; }
        #brand-title { height: 1; color: #d4b676; text-style: bold; }
        #status { height: 3; padding: 0 2; background: #1c1916;
          border-bottom: solid #2e2820; }
        /* `padding-right` is the gutter. Five columns at `1fr` with none of it
           put "контекст: лимит" -- exactly 15 cells at an 80-column window -- flush
           against the field after it, so the row read "контекст: лимитмост:
           выключен" and looked like a rendering fault rather than two fields.
           `text-overflow: ellipsis` marks a value that did not fit: at 46 columns
           each field gets 8 cells, and "openai/model-a" drawn as "openai/m" reads
           as a different model to the person checking which one is selected. */
        #status Static { width: 1fr; content-align: left middle; color: #968a7a;
          padding-right: 1; text-overflow: ellipsis; }
        #model-status, #session-status { color: #c6bca8; }
        #sponsor-ticker { height: 1; padding: 0; background: #181511;
          color: #8f8170; text-style: dim; overflow: hidden; }
        #conversation { height: 1fr; padding: 1 2; scrollbar-color: #6b5c3e; }
        /* The frame around a message is CSS rather than a Rich Panel, and that is
           what makes the message selectable: Widget.get_selection returns None for
           anything whose render is not Content or Text, and a Panel is neither.
           `width: 1fr` also gives a message the whole conversation width, where a
           measured renderable took 37 columns of an available 116. */
        .message { width: 1fr; border: round #4a4338; border-title-align: left;
          padding: 0 1; margin-bottom: 1; }
        /* An unframed line. A frame is a claim that something needs attention, so
           the welcome and ordinary status lines do not get one. */
        .message-line { border: none; padding: 0; margin-bottom: 0; }
        .message-user { border: round #8a7a55; }
        .message-assistant { border: round #8aab7e; }
        .message-notice { border: round #c6a56b; }
        .notice-success { border: round #8aab7e; }
        .notice-warning { border: round #d4b676; }
        .notice-error { border: round #cf7c7c; }
        /* Textual's markdown blocks carry a bottom margin so paragraphs separate.
           On the last block that margin lands inside our border and reads as a
           stray blank line, so only that one is removed. */
        .message-assistant > *:last-of-type { margin-bottom: 0; }
        #busy { height: 1; display: none; color: #c6a56b; }
        #activity { display: none; height: auto; min-height: 2; max-height: 4;
          margin: 0 2; padding: 0 1; background: #1a1712;
          border-left: thick #c6a56b; color: #d4b676; }
        #activity.activity-success { border-left: thick #8aab7e; color: #b7c2b0; }
        #activity.activity-error { border-left: thick #cf7c7c; color: #e0a3a3; }
        #activity.activity-warning { border-left: thick #c3a86b; color: #d6c49a; }
        #command-menu { display: none; height: auto; max-height: 14; margin: 0 2;
          padding: 0 1; background: #191612; border: round #4a4338;
          color: #c6bca8; }
        #composer-wrap { height: 4; padding: 0 2; background: #181511;
          border-top: solid #2e2820; }
        #composer { border: round #4a4338; background: #20201c; color: #e5e5e5; }
        #composer:focus { border: round #c6a56b; }
        #composer-hint { height: 1; color: #8a7e6a; }
        """

        def __init__(
            self,
            repository: Path,
            session_id: Optional[str] = None,
            language: Optional[str] = None,
        ) -> None:
            super().__init__()
            self.register_theme(
                Theme(
                    name="karox-neutral",
                    primary="#c6a56b",
                    secondary="#8a7a55",
                    accent="#d4b676",
                    warning="#c6a56b",
                    error="#cf7c7c",
                    success="#8aab7e",
                    foreground="#dcdcdc",
                    background="#121212",
                    surface="#1a1712",
                    panel="#20201c",
                    dark=True,
                )
            )
            self.theme = "karox-neutral"
            self.repository = repository.resolve()
            requested_language = language if language in {"ru", "en"} else None
            stored_language = _load_language() if requested_language is None else None
            self.language = requested_language or stored_language or "en"
            self._needs_language = (
                requested_language is None and stored_language is None
            )
            self.active_session = session_id
            self.verification = _default_verification(self.repository)
            self.pending_task: Optional[str] = None
            self._setup_both = False
            self.agent_busy = False
            self.agent_process: Optional[subprocess.Popen[str]] = None
            self._stop_requested = False
            self._history_seen = 0
            self._history_fingerprint: Optional[Tuple[int, int]] = None
            # One entry per tool call of the current turn, in the order they
            # started, so the whole turn stays visible instead of each call
            # overwriting the one before it.
            self._steps: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
            # Multi-line pastes held aside while the composer shows a short
            # marker for each. Pasting a stack trace or a diff is the most
            # common way a coding agent is handed context, and a one-line widget
            # cannot show one -- so the text is kept whole here and put back
            # when the message is sent.
            self._pasted_blocks: "OrderedDict[str, str]" = OrderedDict()
            self._paste_counter = 0
            self.bridge_process: Optional[subprocess.Popen[str]] = None
            self.tunnel_process: Optional[subprocess.Popen[str]] = None
            self._tailscale_active = False
            self.bridge_launch: Optional[BridgeLaunch] = None
            self.public_endpoint: Optional[str] = None
            self._command_menu_open = False
            self._filtered_commands: List[str] = []
            self._command_index = 0
            self._sponsor_offset = 0
            self._sponsor_text = ""
            self._sponsor_widget: Optional[Static] = None
            self.sponsors_visible = _load_sponsors_visible()
            self._last_assistant_content = ""
            self._task_started_at: Optional[float] = None

        def compose(self) -> ComposeResult:
            with Vertical(id="brand"):
                yield Static("KaroX", id="brand-title")
                yield Static("", id="sponsor-ticker", markup=False)
            with Horizontal(id="status"):
                yield Static("", id="repo-status")
                yield Static("", id="model-status")
                yield Static("", id="session-status")
                yield Static("", id="context-status")
                yield Static("bridge: off", id="bridge-status")
            # A scroll container of per-message widgets rather than a RichLog. The
            # RichLog needed a min_width to stop it laying the chat out at 78
            # columns in a narrower window and clipping words mid-letter; a widget
            # tree is laid out by Textual at whatever width it has, so the problem
            # and the workaround both go away.
            yield TranscriptView(id="conversation")
            yield LoadingIndicator(id="busy")
            yield Static("", id="activity", markup=True)
            yield Static("", id="command-menu", markup=True)
            with Vertical(id="composer-wrap"):
                yield CommandInput(
                    placeholder=_TEXT[self.language]["placeholder"],
                    id="composer",
                )
                yield Static(
                    _TEXT[self.language]["hint"],
                    id="composer-hint",
                )

        def on_mount(self) -> None:
            self._refresh_status()
            self.query_one("#composer", Input).focus()
            self._sponsor_widget = self.query_one("#sponsor-ticker", Static)
            self.set_interval(0.35, self._poll_agent_history)
            self.set_interval(0.18, self._tick_sponsor_ticker)
            self._reset_sponsor_ticker()
            if self._needs_language:
                self.call_after_refresh(
                    lambda: self.push_screen(
                        LanguageScreen(), self._first_language_selected
                    )
                )
            else:
                self._show_welcome()
            reason = _unsafe_workspace_reason(self.repository, self.language)
            if reason:
                self._write_notice(reason, "error")

        def on_unmount(self) -> None:
            self._stop_bridge(quiet=True)

        def _transcript(self) -> Any:
            return self.query_one("#conversation", TranscriptView)

        def _write(self, message: Any) -> None:
            """Write an unframed line to the transcript.

            Callers pass Rich console markup, which the transcript no longer
            renders: a message has to reach the screen as ``Content`` for Textual's
            selection to find it, and markup tags would otherwise appear literally.
            ``Text.from_markup`` applies the tags and yields the words, which is the
            text the user will copy.
            """
            if not isinstance(message, str):
                message = str(message)
            try:
                plain = Text.from_markup(message).plain
            except Exception:
                plain = message
            self._transcript().add_line(plain)

        def _write_user(self, message: str) -> None:
            transcript = self._transcript()
            # A new turn drops a selection left over from the previous one, so it
            # does not linger as a highlight over text the user has moved past.
            transcript.clear_selection()
            transcript.add_user(message, title=self._label("Вы", "You"))

        def _write_assistant(self, message: str) -> None:
            self._last_assistant_content = message
            self._transcript().add_assistant(message)

        def _write_notice(self, message: str, kind: str = "info") -> None:
            self._transcript().add_notice(message, kind)

        def _set_activity(self, message: str, kind: str = "working") -> None:
            activity = self.query_one("#activity", Static)
            # "idle" hides the activity line entirely (no text, no frame) so
            # only the animated dots indicator speaks while the agent works.
            if kind == "idle":
                activity.styles.display = "none"
                activity.update("")
                return
            activity.styles.display = "block"
            activity.set_class(kind == "success", "activity-success")
            activity.set_class(kind == "error", "activity-error")
            # An outcome that is neither a success nor a failure -- a run that
            # changed nothing and answered from nothing -- gets its own colour
            # rather than borrowing one that would misreport it.
            activity.set_class(kind == "warning", "activity-warning")
            activity.update(message)

        def _label(self, russian: str, english: str) -> str:
            return english if self.language == "en" else russian

        def _show_welcome(self) -> None:
            key = "welcome_ready" if _selected_model() is not None else "welcome_unconfigured"
            self._write(_TEXT[self.language][key])

        def _first_language_selected(self, language: Optional[str]) -> None:
            if language is None:
                self.push_screen(LanguageScreen(), self._first_language_selected)
                return
            self._set_language(language)
            self._needs_language = False
            self._show_welcome()
            self.query_one("#composer", Input).focus()

        def _set_language(self, language: str) -> None:
            _save_language(language)
            self.language = language
            text = _TEXT[language]
            composer = self.query_one("#composer", Input)
            composer.placeholder = text["placeholder"]
            self.query_one("#composer-hint", Static).update(text["hint"])
            self._reset_sponsor_ticker()
            self._refresh_status()
            if self._command_menu_open:
                self._update_command_menu(composer.value)

        def _language_selected(self, language: Optional[str]) -> None:
            if language is not None:
                self._set_language(language)
                message = "Язык изменён." if language == "ru" else "Language changed."
                self._write(f"[#b7c2b0]{message}[/]")
            self.query_one("#composer", Input).focus()

        def _refresh_status(self) -> None:
            text = _TEXT[self.language]
            self.query_one("#repo-status", Static).update(
                text["repo"]
                + ": "
                + escape(self.repository.name or str(self.repository))
            )
            selected = _selected_model()
            model = (
                f"{text['model']}: {selected.provider_id}/{selected.model_id}"
                if selected is not None
                else f"{text['model']}: {text['not_configured']}"
            )
            self.query_one("#model-status", Static).update(escape(model))
            self.query_one("#session-status", Static).update(
                escape(
                    text["session"] + ": " + (self.active_session or text["new_task"])
                )
            )
            self.query_one("#context-status", Static).update(
                self._context_summary(selected)
            )
            bridge = (
                f"{text['bridge']}: {text['public']}"
                if self.public_endpoint
                else f"{text['bridge']}: {self.bridge_launch.profile}/{self.bridge_launch.protocol}"
                if self.bridge_launch is not None
                else f"{text['bridge']}: {text['off']}"
            )
            self.query_one("#bridge-status", Static).update(escape(bridge))

        @staticmethod
        def _format_tokens(value: int) -> str:
            if value >= 1_000_000:
                return f"{value / 1_000_000:.1f}M"
            if value >= 1_000:
                return f"{value / 1_000:.1f}K"
            return str(value)

        def _session_usage(self) -> dict:
            if not self.active_session:
                return {}
            try:
                record = SessionStore(session_dir()).load(self.active_session)
            except Exception:
                return {}
            return record.usage if isinstance(record.usage, dict) else {}

        def _last_prompt_tokens(self) -> Optional[int]:
            # Prompt size of the most recent request, recorded by the agent
            # kernel. This is the only figure that describes context occupancy;
            # the session aggregate below is spend and can exceed the window.
            latest = self._session_usage().get("last_request")
            if not isinstance(latest, dict):
                return None
            value = latest.get("prompt_tokens")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return max(0, int(value))

        def _session_spend(self) -> int:
            # Cumulative tokens across every request in the session, which the
            # agent kernel aggregates. This is spend, not occupancy: after
            # several requests the total can exceed one context window.
            usage = self._session_usage()

            def _number(value: object) -> int:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return 0
                return int(value)

            total = _number(usage.get("total_tokens"))
            if total:
                return total
            prompt = _number(usage.get("prompt_tokens", usage.get("input_tokens")))
            completion = _number(
                usage.get("completion_tokens", usage.get("output_tokens"))
            )
            return prompt + completion

        def _context_summary(self, selected: Optional[ModelRecord]) -> str:
            text = _TEXT[self.language]
            label = text["context"]
            if selected is None:
                return escape(f"{label}: {text['not_configured']}")
            window = getattr(selected, "context_window", None)
            output = getattr(selected, "max_output_tokens", None)
            occupied = self._last_prompt_tokens()
            parts: list[str] = []
            colour: Optional[str] = None
            if isinstance(window, int) and window > 0:
                if occupied is None:
                    # No request has been sent in this session yet, so occupancy
                    # is genuinely unknown. Show the declared limit rather than
                    # an empty gauge, which would read as "context is empty".
                    parts.append(self._format_tokens(window))
                else:
                    share = min(1.0, occupied / window)
                    glyphs = ("\u25cb", "\u25d4", "\u25d1", "\u25d5", "\u25cf")
                    index = min(len(glyphs) - 1, int(share * len(glyphs)))
                    colour = (
                        "#8fa98a"
                        if share < 0.6
                        else "#d4b676"
                        if share < 0.85
                        else "#e0a3a3"
                    )
                    parts.append(
                        f"{glyphs[index]} {int(share * 100)}% "
                        f"{self._format_tokens(occupied)}"
                        f"/{self._format_tokens(window)}"
                    )
            else:
                # The registry holds no declared limit for this model. Saying so
                # is more useful than an empty field, which reads as "no limit".
                parts.append(text["context_unknown"])
            if isinstance(output, int) and output > 0:
                parts.append(f"{text['output_limit']} {self._format_tokens(output)}")
            spent = self._session_spend()
            if spent:
                parts.append(f"{text['spent']} {self._format_tokens(spent)}")
            body = escape(f"{label}: " + " \u00b7 ".join(parts))
            return f"[{colour}]{body}[/]" if colour else body

        def _reset_sponsor_ticker(self) -> None:
            separator = "     •     "
            prefix = (
                "Спасибо нашим партнёрам: "
                if self.language == "ru"
                else "Thanks to our partners: "
            )
            self._sponsor_text = prefix + separator.join(
                sponsor_messages(self.language)
            )
            self._sponsor_offset = 0
            self._apply_sponsor_visibility()
            self._tick_sponsor_ticker()

        # Below this many rows the fixed chrome costs more than the conversation is
        # worth. Measured at 46x14: brand, ticker, status bar, two separators and
        # the composer take twelve of fourteen rows, leaving the chat two -- not
        # enough for the three-line welcome, whose first line ("KaroX готов.", the
        # one saying the product works) had already scrolled out of reach with no
        # scrollbar to suggest anything was above it.
        MINIMUM_ROWS_FOR_SPONSORS = 20

        def _sponsors_fit(self) -> bool:
            """Whether the window has a row to spare for the sponsor line."""
            return self.size.height >= self.MINIMUM_ROWS_FOR_SPONSORS

        def _apply_sponsor_visibility(self) -> None:
            ticker = self._sponsor_widget
            if ticker is None or not ticker.is_mounted:
                return
            visible = self.sponsors_visible and self._sponsors_fit()
            ticker.styles.display = "block" if visible else "none"

        def on_resize(self, event: Any) -> None:  # noqa: ARG002
            # The sponsor line is the first thing to go when the window is short,
            # and it comes back when there is room again. The user's own /sponsors
            # preference still wins: this can only hide it, never show it.
            self._apply_sponsor_visibility()

        def _set_sponsors_visible(self, visible: bool) -> None:
            self.sponsors_visible = bool(visible)
            _save_sponsors_visible(self.sponsors_visible)
            self._apply_sponsor_visibility()
            state = self._label(
                "Лента спонсоров показана." if visible else "Лента спонсоров скрыта.",
                "Sponsor line shown." if visible else "Sponsor line hidden.",
            )
            self._write(f"[#d4b676]{state}[/]")

        def _tick_sponsor_ticker(self) -> None:
            ticker = self._sponsor_widget
            if (
                not self.sponsors_visible
                or
                not self._sponsor_text
                or not self.is_mounted
                or ticker is None
                or not ticker.is_mounted
            ):
                return
            width = max(20, ticker.size.width - 4)
            stream = self._sponsor_text + " " * width
            doubled = stream + stream
            start = self._sponsor_offset % len(stream)
            ticker.update(doubled[start : start + width])
            self._sponsor_offset = (self._sponsor_offset + 1) % len(stream)

        @on(Input.Changed, "#composer")
        def composer_changed(self, event: Input.Changed) -> None:
            self._update_command_menu(event.value)

        def _update_command_menu(self, value: str) -> None:
            commands = _commands(self.language)
            query = value.strip().lower()
            matches = (
                [name for name in commands if name.lower().startswith(query)]
                if query.startswith("/")
                else []
            )
            self._filtered_commands = matches
            self._command_index = min(self._command_index, max(0, len(matches) - 1))
            self._command_menu_open = bool(matches)
            menu = self.query_one("#command-menu", Static)
            menu.styles.display = "block" if matches else "none"
            if not matches:
                menu.update("")
                return
            rows = []
            for index, name in enumerate(matches):
                marker = "[bold #e0dccc]> [/]" if index == self._command_index else "  "
                rows.append(
                    f"{marker}[bold #d4b676]{escape(name)}[/]  "
                    f"[dim]{escape(commands[name])}[/]"
                )
            menu.update("\n".join(rows))

        def _select_previous_command(self) -> None:
            if self._filtered_commands:
                self._command_index = (self._command_index - 1) % len(
                    self._filtered_commands
                )
                self._update_command_menu(self.query_one("#composer", Input).value)

        def _select_next_command(self) -> None:
            if self._filtered_commands:
                self._command_index = (self._command_index + 1) % len(
                    self._filtered_commands
                )
                self._update_command_menu(self.query_one("#composer", Input).value)

        @staticmethod
        def _command_insertion(command: str) -> str:
            if command == "/verify JSON":
                return "/verify "
            if command in {"/workspace PATH", "/workspace ПУТЬ"}:
                return "/workspace "
            return command

        def _complete_selected_command(self) -> None:
            if not self._filtered_commands:
                return
            command = self._filtered_commands[self._command_index]
            composer = self.query_one("#composer", Input)
            composer.value = self._command_insertion(command)
            composer.cursor_position = len(composer.value)

        def _dismiss_command_menu(self) -> None:
            composer = self.query_one("#composer", Input)
            composer.clear()
            self._update_command_menu("")

        def _register_pasted_block(self, text: str) -> str:
            self._paste_counter += 1
            marker = f"[paste #{self._paste_counter}: {len(text.splitlines())} lines]"
            self._pasted_blocks[marker] = text
            # A composer that is cleared without sending would otherwise hold
            # every paste of the session in memory.
            while len(self._pasted_blocks) > 20:
                self._pasted_blocks.popitem(last=False)
            return marker

        def _expand_pasted_blocks(self, value: str) -> str:
            for marker, text in self._pasted_blocks.items():
                value = value.replace(marker, text)
            return value

        @on(Input.Submitted, "#composer")
        def input_submitted(self, event: Input.Submitted) -> None:
            value = self._expand_pasted_blocks(event.value).strip()
            if self._command_menu_open and self._filtered_commands:
                selected = self._filtered_commands[self._command_index]
                insertion = self._command_insertion(selected)
                if selected == "/verify JSON" and not value.startswith("/verify "):
                    event.input.value = insertion
                    event.input.cursor_position = len(insertion)
                    return
                value = insertion.strip()
            event.input.clear()
            if not value:
                return
            if value.startswith("/"):
                self._handle_command(value)
            else:
                self._submit_task(value)

        def _handle_command(self, value: str) -> None:
            command, _, argument = value.partition(" ")
            if command in {"/quit", "/exit"}:
                self.exit(0)
            elif command == "/help":
                lines = [f"[bold #e0dccc]{_TEXT[self.language]['commands']}[/]"]
                lines.extend(
                    f"  [#d4b676]{escape(name)}[/]  [dim]{escape(description)}[/]"
                    for name, description in _commands(self.language).items()
                )
                self._write("\n".join(lines))
            elif command in {"/connect", "/setup"}:
                self.action_onboarding()
            elif command == "/ask":
                self._run_ask(argument.strip())
            elif command == "/language":
                self.push_screen(
                    LanguageScreen(allow_cancel=True), self._language_selected
                )
            elif command == "/workspace":
                raw_path = argument.strip().strip('"')
                if not raw_path:
                    self._write_notice(
                        self._label(
                            "Укажите папку проекта: /workspace D:\\путь\\к\\проекту",
                            "Enter a project folder: /workspace D:\\path\\to\\project",
                        ),
                        "warning",
                    )
                    return
                candidate = Path(raw_path).expanduser()
                if not candidate.is_dir():
                    self._write_notice(
                        self._label(
                            "Такой папки не существует.",
                            "That folder does not exist.",
                        ),
                        "error",
                    )
                    return
                reason = _unsafe_workspace_reason(candidate, self.language)
                if reason:
                    self._write_notice(reason, "error")
                    return
                self.repository = candidate.resolve()
                self.verification = _default_verification(self.repository)
                self.active_session = None
                self._refresh_status()
                self._write_notice(
                    self._label(
                        f"Рабочая папка: {self.repository}",
                        f"Workspace: {self.repository}",
                    ),
                    "success",
                )
            elif command == "/sponsors":
                requested = argument.strip().casefold()
                if requested in {"on", "show", "1", "true"}:
                    self._set_sponsors_visible(True)
                elif requested in {"off", "hide", "0", "false"}:
                    self._set_sponsors_visible(False)
                elif requested:
                    self._write(
                        self._label(
                            "[#e0a3a3]Формат:[/] /sponsors [on|off]",
                            "[#e0a3a3]Usage:[/] /sponsors [on|off]",
                        )
                    )
                else:
                    self._set_sponsors_visible(not self.sponsors_visible)
            elif command == "/bridge":
                if argument.strip() == "stop":
                    self._stop_bridge()
                else:
                    self.action_bridge()
            elif command == "/clear":
                self.action_clear_log()
            elif command == "/verify":
                try:
                    decoded = json.loads(argument)
                    if (
                        not isinstance(decoded, list)
                        or not decoded
                        or not all(isinstance(item, str) and item for item in decoded)
                    ):
                        raise ValueError
                    # ``self.verification`` is a set of approved commands (one
                    # per tuple); the user's single /verify entry replaces the
                    # whole set with that one command.
                    self.verification = (tuple(decoded),)
                    self._write(
                        "[#b7c2b0]Команда проверки подтверждена:[/] "
                        + escape(" ".join(self.verification[0]))
                    )
                except (json.JSONDecodeError, ValueError):
                    self._write(
                        '[#e0a3a3]Формат:[/] /verify ["python","-m","pytest","-q"]'
                    )
            elif command in _BACKEND_SLASH:
                self._run_inspection(_BACKEND_SLASH[command], command)
            else:
                message = _TEXT[self.language]["unknown"].format(
                    command=escape(command)
                )
                self._write(f"[#e0a3a3]{message}[/]")

        def action_onboarding(self) -> None:
            self.push_screen(
                ConnectionChoiceScreen(self.language), self._connection_choice_done
            )

        def _connection_choice_done(self, choice: Optional[str]) -> None:
            if choice is None:
                self.query_one("#composer", Input).focus()
            elif choice == "api":
                self._setup_both = False
                self.call_after_refresh(self.action_provider_preset)
            elif choice == "web":
                self._setup_both = False
                self.call_after_refresh(self.action_bridge)
            else:
                self._setup_both = True
                self.call_after_refresh(self.action_provider_preset)

        def action_provider_preset(self) -> None:
            self.push_screen(
                ProviderPresetScreen(self.language), self._provider_preset_done
            )

        def _provider_preset_done(self, preset_id: Optional[str]) -> None:
            if preset_id is None:
                self._setup_both = False
                self.query_one("#composer", Input).focus()
                return
            preset = provider_preset(preset_id)
            if not preset.installable:
                self.call_after_refresh(
                    lambda: self.push_screen(
                        PuterInfoScreen(self.language), self._puter_info_closed
                    )
                )
                return
            self.call_after_refresh(lambda: self.action_setup(preset))

        def _puter_info_closed(self, _result: None) -> None:
            self._setup_both = False
            self.query_one("#composer", Input).focus()

        def action_setup(self, preset: Optional[ProviderPreset] = None) -> None:
            self.push_screen(
                ProviderSetupScreen(self.language, preset), self._provider_setup_done
            )

        def _provider_setup_done(self, setup: Optional[ProviderSetup]) -> None:
            if setup is None:
                self._setup_both = False
                self.query_one("#composer", Input).focus()
                return
            selected = _selected_model()
            if selected is None:
                self._write(
                    "[#e0a3a3]"
                    + self._label(
                        "Модель не была активирована после проверки.",
                        "The model was not activated after verification.",
                    )
                    + "[/]"
                )
                return
            self._write(
                "[#b7c2b0]"
                + self._label(
                    "API подключён и проверен:", "API connected and verified:"
                )
                + f"[/] {escape(selected.provider_id)}/"
                f"{escape(selected.model_id)}"
            )
            self._refresh_status()
            self.query_one("#composer", Input).focus()
            if self._setup_both:
                self._setup_both = False
                self.action_bridge()
            elif self.pending_task:
                task, self.pending_task = self.pending_task, None
                self._submit_task(task)

        def action_bridge(self) -> None:
            if self.bridge_process is not None and self.bridge_process.poll() is None:
                self._write(
                    "[#c6a56b]"
                    + self._label(
                        "Мост уже работает.", "The bridge is already running."
                    )
                    + "[/] "
                    + self._label(
                        "Сначала выполните /bridge stop.",
                        "Run /bridge stop first.",
                    )
                )
                return
            self.push_screen(BridgeSetupScreen(self.language), self._bridge_setup_done)

        def _bridge_setup_done(self, setup: Optional[BridgeSetup]) -> None:
            if setup is None:
                self.query_one("#composer", Input).focus()
                return
            # A previous KaroX run (or a bridge left running from an earlier
            # session in this TUI lifetime) can leave an orphan process holding
            # the loopback port.  When that happens the new ``bridge serve``
            # fails to bind with ``[Errno 10048]`` on Windows / ``EADDRINUSE``
            # elsewhere and exits — so free the port first.  We preserve our own
            # process and any current bridge process (the action_bridge guard
            # already rejects a second launch while one is live).
            skip: set = set()
            current = self.bridge_process
            if current is not None and current.poll() is None:
                try:
                    skip.add(current.pid)
                except Exception:
                    pass
            freed = _free_port_on_address("127.0.0.1", setup.port, skip_pids=skip)
            if freed:
                self._write(
                    "[dim]"
                    + self._label(
                        f"Освобождён порт {setup.port} "
                        f"({freed} старый процесс моста остановлен).",
                        f"Freed port {setup.port} "
                        f"({freed} orphan bridge process stopped).",
                    )
                    + "[/]"
                )
            try:
                launch = (
                    _managed_web_bridge_launch(self.repository, setup)
                    if setup.profile in WEB_BRIDGE_PROFILES
                    else _bridge_launch(self.repository, setup)
                )
                flags = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    if launch.managed and os.name == "nt"
                    else getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
                child_env = _child_environment()
                child_env["KAROX_UI_LANGUAGE"] = self.language
                self.bridge_process = subprocess.Popen(
                    launch.argv,
                    cwd=self.repository,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=flags,
                    env=child_env,
                )
                self.bridge_launch = launch
            except Exception as exc:
                self._write(
                    f"[#e0a3a3]Не удалось запустить мост:[/] {escape(str(exc))}"
                )
                return
            self.active_session = launch.session_id
            self._refresh_status()
            _start_worker(self._read_bridge_output, name="karox-bridge-output")
            if launch.managed:
                self._write(
                    "[dim]"
                    + self._label(
                        "KaroX создаёт HTTPS-туннель и OAuth MCP-мост. "
                        "Готовый URL и пароль появятся ниже.",
                        "KaroX is creating the HTTPS tunnel and OAuth MCP bridge. "
                        "The connector URL and approval password will appear below.",
                    )
                    + "[/]"
                )
                self.query_one("#composer", Input).focus()
                if self.pending_task and _selected_model() is not None:
                    task, self.pending_task = self.pending_task, None
                    self._submit_task(task)
                return
            # Confirm the bridge is actually listening before claiming success.
            # If the bind fails (port in use, permission error, …) the process
            # exits within a second; writing "Мост запущен" first would mislead
            # the user into pasting a dead URL into Notion.
            _start_worker(
                self._confirm_bridge_started,
                setup,
                launch,
                name="karox-bridge-confirm",
            )
            self.query_one("#composer", Input).focus()
            if self.pending_task and _selected_model() is not None:
                task, self.pending_task = self.pending_task, None
                self._submit_task(task)

        def _confirm_bridge_started(
            self, setup: BridgeSetup, launch: BridgeLaunch
        ) -> None:
            """Wait until the bridge is listening, then announce it + start tunnel.

            Runs on a worker thread.  Polls the loopback port for up to a few
            seconds; the moment it's listening we write the success message
            (URL + key) and kick off the public tunnel.  If the bridge process
            exits first we surface a clear bind-failure error instead of the
            misleading "Мост запущен" that the user would otherwise see before
            the crash.
            """
            process = self.bridge_process
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if process is None or process.poll() is not None:
                    # Process exited before the port came up — bind failed.
                    self.call_from_thread(
                        self._write,
                        self._label(
                            "[#e0a3a3]Не удалось запустить мост.[/] "
                            "Порт занят или процесс упал. "
                            "Проверьте, что порт свободен, и повторите.",
                            "[#e0a3a3]Could not start the bridge.[/] "
                            "The port is busy or the process crashed. "
                            "Make sure the port is free and retry.",
                        ),
                    )
                    return
                if _port_is_listening("127.0.0.1", setup.port):
                    self.call_from_thread(
                        self._write,
                        f"[#b7c2b0]Мост запущен (локально).[/] {escape(launch.endpoint)}\n"
                        f"[bold #c6a56b]Ключ доступа (многоразовый):[/] {escape(launch.secret)}\n"
                        "[dim]Ключ хранится в OS keyring. Публичный URL появится ниже — "
                        "вставляйте в Notion именно его (с суффиксом /mcp) "
                        "вместе с этим ключом.[/]",
                    )
                    provider = setup.tunnel_provider
                    if provider == "cloudflare":
                        self.call_from_thread(self._start_tunnel, setup.port, launch)
                    elif provider == "tailscale":
                        self.call_from_thread(
                            self._start_tailscale_funnel, setup.port, launch
                        )
                    return
                time.sleep(0.1)
            # Timed out waiting for the port — don't claim success, but don't
            # kill the process either (it may be slow to bind).  Just surface
            # the uncertainty so the user can investigate.
            self.call_from_thread(
                self._write,
                self._label(
                    "[#c6a56b]Мост запускается, но порт не открылся за 5 секунд.[/] "
                    "Проверьте вывод bridge ниже — возможно, порт занят "
                    "другим процессом.",
                    "[#c6a56b]The bridge is starting but the port did not open "
                    "within 5 seconds.[/] Check the bridge output below — the "
                    "port may be held by another process.",
                ),
            )

        def _start_tunnel(self, port: int, launch: BridgeLaunch) -> None:
            executable = _find_cloudflared()
            if executable is None:
                self._write(
                    "[#c6a56b]Cloudflare Tunnel не установлен.[/] Мост доступен только локально. "
                    "Повторите установку с cloudflared или добавьте его в PATH."
                )
                return
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                self.tunnel_process = subprocess.Popen(
                    [
                        executable,
                        "tunnel",
                        "--url",
                        f"http://127.0.0.1:{port}",
                        "--no-autoupdate",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=flags,
                )
            except Exception as exc:
                self._write(
                    f"[#e0a3a3]Не удалось запустить HTTPS-туннель:[/] {escape(str(exc))}"
                )
                return
            self._write("[dim]Создаю публичный HTTPS endpoint…[/]")
            _start_worker(
                self._read_tunnel_output, launch, name="karox-tunnel-output"
            )

        def _read_tunnel_output(self, launch: BridgeLaunch) -> None:
            process = self.tunnel_process
            if process is None or process.stdout is None:
                return
            for line in process.stdout:
                if self.tunnel_process is not process:
                    break
                match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                if match and self.public_endpoint is None:
                    base = match.group(0)
                    suffix = "/openapi.json" if launch.protocol == "openapi" else "/mcp"
                    self.call_from_thread(self._tunnel_ready, base + suffix)
            code = process.wait()
            if self.tunnel_process is process:
                self.call_from_thread(self._tunnel_exited, code)

        def _tunnel_ready(self, endpoint: str) -> None:
            self.public_endpoint = endpoint
            self._refresh_status()
            # Show the public URL and the access key together so the user has
            # both in one place to paste into the external client.  The key is
            # reusable (it lives in the OS keyring), so we don't say "one-time".
            secret = self.bridge_launch.secret if self.bridge_launch else ""
            self._write(
                "[bold #d4b676]Публичный URL коннектора:[/] "
                + escape(endpoint)
                + ("\n[bold #c6a56b]Ключ доступа:[/] " + escape(secret) if secret else "")
                + "\n[dim]Вставьте URL (с суффиксом /mcp) и этот ключ в Notion. "
                "Ключ многоразовый — Notion хранит его и переиспользует.[/]"
            )

        def _start_tailscale_funnel(self, port: int, launch: BridgeLaunch) -> None:
            """Publish through the same ownership-safe foreground flow as CLI."""
            executable = _find_tailscale()
            if executable is None:
                # Ask permission before installing — never install silently.
                self._offer_tailscale_install(port, launch)
                return
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                status = subprocess.run(
                    [executable, "status", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    creationflags=flags,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self._write(
                    f"[#e0a3a3]Не удалось опросить Tailscale:[/] {escape(str(exc))}"
                )
                return
            try:
                payload = json.loads(status.stdout or "{}")
            except json.JSONDecodeError:
                payload = {}
            backend_state = payload.get("BackendState")
            self_record = payload.get("Self") or {}
            dns_name = str(self_record.get("DNSName") or "").rstrip(".")
            if backend_state != "Running" or not dns_name:
                # Ask permission before running `tailscale up` (opens a browser
                # for login) — never log in silently.
                self._offer_tailscale_login(port, launch, executable)
                return
            try:
                plan = prepare_tailscale_funnel(
                    port, executable=executable, run=subprocess.run
                )
                process = subprocess.Popen(
                    list(plan.argv),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=flags,
                )
            except (OSError, subprocess.SubprocessError, TailscaleError) as exc:
                self._write(
                    f"[#e0a3a3]Tailscale Funnel не запущен:[/] {escape(str(exc))}"
                )
                return
            self.tunnel_process = process
            self._tailscale_active = True
            suffix = "/openapi.json" if launch.protocol == "openapi" else "/mcp"
            self._tunnel_ready(plan.public_url + suffix)
            _start_worker(
                self._read_tailscale_output, process, name="karox-tailscale-output"
            )

        def _read_tailscale_output(self, process: subprocess.Popen[str]) -> None:
            stream = process.stdout
            if stream is not None:
                for line in stream:
                    if self.tunnel_process is not process:
                        break
                    message = line.strip()
                    if message:
                        self.call_from_thread(
                            self._write, "[dim cyan]tailscale[/] " + escape(message)
                        )
            code = process.wait()
            if self.tunnel_process is process:
                self.call_from_thread(self._tunnel_exited, code)

        def _offer_tailscale_install(self, port: int, launch: BridgeLaunch) -> None:
            """Ask the user's permission before installing Tailscale."""
            screen = ConfirmScreen(
                self._label("Установить Tailscale?", "Install Tailscale?"),
                self._label(
                    "Tailscale не найден. Для публичного URL через Tailscale "
                    "Funnel нужно установить Tailscale (~30 МБ, winget: "
                    "tailscale.tailscale) и войти в tailnet. Установить сейчас?",
                    "Tailscale was not found. A public URL via Tailscale Funnel "
                    "needs Tailscale installed (~30 MB, winget: tailscale.tailscale) "
                    "and signed in to a tailnet. Install now?",
                ),
                language=self.language,
            )
            self.push_screen(
                screen,
                lambda approved: self._on_tailscale_install_answer(
                    approved, port, launch
                ),
            )

        def _on_tailscale_install_answer(
            self, approved: Optional[bool], port: int, launch: BridgeLaunch
        ) -> None:
            if not approved:
                self._write(
                    "[#c6a56b]Tailscale не установлен.[/] "
                    + self._label(
                        "Мост доступен только локально. Установите Tailscale "
                        "вручную и повторите подключение.",
                        "The bridge is local only. Install Tailscale manually "
                        "and reconnect.",
                    )
                )
                return
            self._set_activity(self._label("Устанавливаю Tailscale…", "Installing Tailscale…"))
            _start_worker(
                self._install_tailscale_worker,
                port,
                launch,
                name="karox-tailscale-install",
            )

        def _install_tailscale_worker(self, port: int, launch: BridgeLaunch) -> None:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                result = subprocess.run(
                    ["winget", "install", "-e", "--id", "tailscale.tailscale",
                     "--accept-source-agreements", "--accept-package-agreements"],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    creationflags=flags,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self.call_from_thread(
                    self._write,
                    f"[#e0a3a3]Не удалось запустить winget:[/] {escape(str(exc))}",
                )
                return
            if result.returncode != 0:
                self.call_from_thread(
                    self._write,
                    self._label(
                        "[#e0a3a3]Установка Tailscale не удалась.[/] "
                        + escape((result.stderr or result.stdout or "").strip()),
                        "[#e0a3a3]Tailscale install failed.[/] "
                        + escape((result.stderr or result.stdout or "").strip()),
                    ),
                )
                return
            self.call_from_thread(self._set_activity, "", "idle")
            self.call_from_thread(
                self._write,
                self._label(
                    "[#8aab7e]Tailscale установлен.[/] Войдите в tailnet.",
                    "[#8aab7e]Tailscale installed.[/] Sign in to a tailnet.",
                ),
            )
            # Retry the funnel flow — it will now detect Tailscale and ask to log in.
            self.call_from_thread(self._start_tailscale_funnel, port, launch)

        def _offer_tailscale_login(
            self, port: int, launch: BridgeLaunch, executable: str
        ) -> None:
            """Ask the user's permission before running `tailscale up`."""
            screen = ConfirmScreen(
                self._label("Войти в tailnet?", "Sign in to a tailnet?"),
                self._label(
                    "Tailscale установлен, но не подключён к tailnet. "
                    "`tailscale up` откроет браузер для входа. Запустить?",
                    "Tailscale is installed but not signed in. `tailscale up` "
                    "will open a browser to sign in. Run it?",
                ),
                language=self.language,
            )
            self.push_screen(
                screen,
                lambda approved: self._on_tailscale_login_answer(
                    approved, port, launch, executable
                ),
            )

        def _on_tailscale_login_answer(
            self,
            approved: Optional[bool],
            port: int,
            launch: BridgeLaunch,
            executable: str,
        ) -> None:
            if not approved:
                self._write(
                    "[#c6a56b]Tailscale не подключён к tailnet.[/] "
                    + self._label(
                        "Мост доступен только локально. Войдите в tailnet и "
                        "повторите подключение.",
                        "The bridge is local only. Sign in to a tailnet and "
                        "reconnect.",
                    )
                )
                return
            self._set_activity(self._label("Запускаю tailscale up…", "Running tailscale up…"))
            _start_worker(
                self._tailscale_login_worker,
                port,
                launch,
                executable,
                name="karox-tailscale-login",
            )

        def _tailscale_login_worker(
            self, port: int, launch: BridgeLaunch, executable: str
        ) -> None:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            # `tailscale up` prints the auth URL and then BLOCKS until the user
            # finishes the browser login.  Using subprocess.run(capture_output=)
            # would hide that URL until login completes — a chicken-and-egg trap:
            # the user can't log in because they never see the URL, and the URL
            # is only shown after login.  So we stream the output line by line,
            # surface the auth URL the instant it appears, and open the browser
            # explicitly (Tailscale's own browser-open is suppressed by
            # CREATE_NO_WINDOW).
            try:
                process = subprocess.Popen(
                    [executable, "up"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=flags,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self.call_from_thread(
                    self._write,
                    f"[#e0a3a3]Не удалось запустить tailscale up:[/] {escape(str(exc))}",
                )
                return
            auth_url: Optional[str] = None
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    if auth_url is None:
                        match = re.search(r"https://login\.tailscale\.com/\S+", line)
                        if match:
                            auth_url = match.group(0).rstrip()
                            # Open the browser ourselves so the login page shows up
                            # even though the tailscale subprocess has no window.
                            try:
                                import webbrowser
                                webbrowser.open(auth_url)
                            except Exception:
                                pass
                            self.call_from_thread(
                                self._write,
                                self._label(
                                    "[#c6a56b]Открываю вход в tailnet в браузере.[/]\n"
                                    "Если браузер не открылся, перейдите по ссылке:\n"
                                    + escape(auth_url),
                                    "[#c6a56b]Opening the tailnet login in your browser.[/]\n"
                                    "If the browser did not open, visit:\n"
                                    + escape(auth_url),
                                ),
                            )
            except Exception:
                pass
            try:
                process.wait(timeout=180)
            except subprocess.TimeoutExpired:
                process.kill()

            # ``tailscale up`` can silently no-op on Windows when run without
            # administrator privileges: it returns rc=0 with NO output and does
            # NOT print an auth URL (the non-admin CLI can't drive login while
            # the daemon is unauthenticated).  Treating that as "signed in" would
            # loop forever offering login — the exact "tailscale не запускается"
            # symptom.  So once the CLI returns, verify login against the daemon
            # state itself; if the daemon still isn't logged in, fall back to the
            # Tailscale GUI app (``tailscale-ipn.exe``) which runs in the user
            # session and can complete login without a separate UAC prompt.
            if _tailscale_logged_in(executable):
                self.call_from_thread(self._set_activity, "", "idle")
                self.call_from_thread(
                    self._write,
                    self._label(
                        "[#8aab7e]Вход выполнен.[/] Включаю Funnel…",
                        "[#8aab7e]Signed in.[/] Enabling Funnel…",
                    ),
                )
                self.call_from_thread(self._start_tailscale_funnel, port, launch)
                return

            if auth_url is not None:
                # We showed an auth URL but the daemon still isn't logged in —
                # the user likely closed the login page.  Surface the failure
                # rather than silently looping.
                self.call_from_thread(self._set_activity, "", "idle")
                self.call_from_thread(
                    self._write,
                    self._label(
                        "[#c6a56b]Вход в tailnet не завершён.[/] "
                        "Откройте показанную ссылку и авторизуйтесь, затем "
                        "повторите подключение.",
                        "[#c6a56b]Tailnet login not completed.[/] "
                        "Open the shown link and authorize, then reconnect.",
                    ),
                )
                return

            # No auth URL at all: the non-admin CLI silently no-ops on Windows.
            # Hand login to the GUI app, which runs in the user session.
            self.call_from_thread(
                self._write,
                self._label(
                    "[#c6a56b]Запускаю приложение Tailscale для входа.[/]\n"
                    "CLI `tailscale up` без прав администратора на Windows "
                    "молча ничего не делает. Открываю приложение Tailscale — "
                    "войдите там в свой tailnet. После входа подключение "
                    "продолжится автоматически.",
                    "[#c6a56b]Launching the Tailscale app to sign in.[/]\n"
                    "The `tailscale up` CLI silently no-ops on Windows without "
                    "administrator rights. Opening the Tailscale app — sign in "
                    "to your tailnet there. The connection resumes automatically "
                    "once you're signed in.",
                ),
            )
            self.call_from_thread(self._set_activity, "", "idle")
            self.call_from_thread(
                self._set_activity,
                self._label("Ожидаю вход в Tailscale…", "Waiting for Tailscale login…"),
                "working",
            )
            self._await_tailscale_login(port, launch, executable)

        def _await_tailscale_login(
            self, port: int, launch: BridgeLaunch, executable: str
        ) -> None:
            """Launch the Tailscale GUI app and poll the daemon for login.

            Runs on the worker thread.  We open ``tailscale-ipn.exe`` (which runs
            in the user session and can complete login without UAC) and then poll
            ``tailscale status --json`` for up to a few minutes.  The moment the
            node authenticates (``Self.DNSName`` appears with ``BackendState ==
            Running``) we resume the Funnel flow on the UI thread.
            """
            gui_app = _tailscale_gui_app(executable)
            if gui_app is None:
                self.call_from_thread(
                    self._write,
                    self._label(
                        "[#e0a3a3]Приложение Tailscale не найдено.[/] "
                        "Запустите Tailscale вручную из меню Пуск, войдите в "
                        "tailnet и повторите подключение.",
                        "[#e0a3a3]The Tailscale app was not found.[/] "
                        "Launch Tailscale manually from the Start menu, sign in "
                        "to your tailnet, and reconnect.",
                    ),
                )
                self.call_from_thread(self._set_activity, "", "idle")
                return
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "DETACHED_PROCESS", 0
            )
            try:
                subprocess.Popen([gui_app], creationflags=flags, close_fds=True)
            except (OSError, subprocess.SubprocessError) as exc:
                self.call_from_thread(
                    self._write,
                    self._label(
                        "[#e0a3a3]Не удалось запустить приложение Tailscale:[/] "
                        + escape(str(exc)),
                        "[#e0a3a3]Could not launch the Tailscale app:[/] "
                        + escape(str(exc)),
                    ),
                )
                self.call_from_thread(self._set_activity, "", "idle")
                return
            # Poll the daemon until login completes (or we give up).  Login can
            # take a while if the user is slow to authorize in the browser, so
            # we wait up to five minutes.  We also track the daemon's
            # BackendState: if it lingers in "NoState"/"Starting" (rather than a
            # login-prompting state), the tailscaled service is likely stuck and
            # a service restart / reopening the Tailscale app is the real fix —
            # so we say that honestly instead of "login did not complete".
            deadline = time.monotonic() + _TAILSCALE_LOGIN_TIMEOUT_SECONDS
            stuck_state_seen = 0
            last_state = None
            while time.monotonic() < deadline:
                state = _tailscale_backend_state(executable)
                if state != last_state:
                    last_state = state
                if state in ("NoState", "Starting"):
                    stuck_state_seen += 1
                if _tailscale_logged_in(executable):
                    self.call_from_thread(self._set_activity, "", "idle")
                    self.call_from_thread(
                        self._write,
                        self._label(
                            "[#8aab7e]Вход выполнен.[/] Включаю Funnel…",
                            "[#8aab7e]Signed in.[/] Enabling Funnel…",
                        ),
                    )
                    self.call_from_thread(self._start_tailscale_funnel, port, launch)
                    return
                time.sleep(_TAILSCALE_LOGIN_POLL_INTERVAL)
            self.call_from_thread(self._set_activity, "", "idle")
            if stuck_state_seen > _TAILSCALE_STUCK_STATE_THRESHOLD:
                # The daemon sat in NoState/Starting for ~30s+ of polling — it
                # is stuck, not waiting for login.  The fix is at the Tailscale
                # level (service/app restart), not another login attempt.
                self.call_from_thread(
                    self._write,
                    self._label(
                        "[#e0a3a3]Служба Tailscale не отвечает.[/]\n"
                        "Daemon застрял в состоянии «starting» — публичный URL "
                        "не поднимется, пока он не заработает. Откройте "
                        "приложение Tailscale из меню Пуск (или перезапустите "
                        "службу «Tailscale» от имени администратора), дождитесь "
                        "статуса «Connected», затем повторите подключение.",
                        "[#e0a3a3]The Tailscale service is not responding.[/]\n"
                        "The daemon is stuck in the \"starting\" state — the "
                        "public URL won't come up until it recovers. Open the "
                        "Tailscale app from the Start menu (or restart the "
                        "\"Tailscale\" service as administrator), wait for the "
                        "\"Connected\" status, then reconnect.",
                    ),
                )
            else:
                self.call_from_thread(
                    self._write,
                    self._label(
                        "[#c6a56b]Вход в Tailscale не завершён за 5 минут.[/] "
                        "Откройте приложение Tailscale, войдите в tailnet и "
                        "повторите подключение.",
                        "[#c6a56b]Tailscale login did not complete within 5 minutes.[/] "
                        "Open the Tailscale app, sign in to your tailnet, and "
                        "reconnect.",
                    ),
                )

        def _tunnel_exited(self, code: int) -> None:
            self.tunnel_process = None
            self.public_endpoint = None
            self._refresh_status()
            if self.bridge_process is not None:
                self._write(
                    f"[dim]HTTPS-туннель остановлен (код {code}); локальный мост продолжает работу.[/]"
                )

        def _read_bridge_output(self) -> None:
            process = self.bridge_process
            if process is None or process.stdout is None:
                return
            for line in process.stdout:
                if self.bridge_process is not process:
                    break
                message = line.strip()
                if message:
                    if message.startswith("MCP URL:"):
                        endpoint = message.partition(":")[2].strip()
                        if endpoint.startswith("https://"):
                            self.call_from_thread(
                                self._managed_web_endpoint_ready,
                                endpoint,
                            )
                    self.call_from_thread(
                        self._write, "[dim cyan]bridge[/] " + escape(message)
                    )
            code = process.wait()
            if self.bridge_process is process:
                self.call_from_thread(self._bridge_exited, code)

        def _managed_web_endpoint_ready(self, endpoint: str) -> None:
            self.public_endpoint = endpoint
            self._refresh_status()

        def _bridge_exited(self, code: int) -> None:
            if self.bridge_process is not None:
                self._write(f"[dim]Мост остановлен (код {code}).[/]")
            self.bridge_process = None
            self.bridge_launch = None
            self.public_endpoint = None
            self._refresh_status()

        def _stop_bridge(self, quiet: bool = False) -> None:
            tunnel = self.tunnel_process
            self.tunnel_process = None
            if tunnel is not None and tunnel.poll() is None:
                tunnel.terminate()
                try:
                    tunnel.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    tunnel.kill()
            # Tailscale uses a foreground child owned by this bridge. Stopping
            # that child withdraws only this route; a global reset could remove
            # unrelated Serve/Funnel routes and is deliberately forbidden.
            self._tailscale_active = False
            process = self.bridge_process
            launch = self.bridge_launch
            self.bridge_process = None
            if process is not None and process.poll() is None:
                if launch is not None and launch.managed:
                    try:
                        if os.name == "nt":
                            process.send_signal(signal.CTRL_BREAK_EVENT)
                        else:
                            process.send_signal(signal.SIGINT)
                    except (OSError, ValueError):
                        process.terminate()
                else:
                    process.terminate()
                try:
                    process.wait(timeout=12 if launch and launch.managed else 3)
                except subprocess.TimeoutExpired:
                    process.kill()
            self.bridge_launch = None
            self.public_endpoint = None
            if not quiet:
                self._write("[dim]Мост остановлен.[/]")
                self._refresh_status()

        def _submit_task(self, task: str) -> None:
            if self.agent_busy:
                self._write_notice(
                    self._label(
                        "KaroX ещё работает. Текущее действие показано над строкой ввода.",
                        "KaroX is still working. The current action is shown above the input.",
                    ),
                    "warning",
                )
                return
            unsafe_reason = _unsafe_workspace_reason(self.repository, self.language)
            if unsafe_reason:
                self._write_notice(unsafe_reason, "error")
                return
            if _selected_model() is None:
                self._write(f"[#c6a56b]{_TEXT[self.language]['connect_first']}[/]")
                return
            session_id = f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}"
            self.active_session = session_id
            self._history_seen = 0
            self._history_fingerprint = None
            self._steps.clear()
            self.agent_busy = True
            self._stop_requested = False
            self.agent_process = None
            self._last_assistant_content = ""
            self._task_started_at = time.monotonic()
            self.query_one("#composer", Input).disabled = True
            self.query_one("#busy", LoadingIndicator).styles.display = "block"
            self._refresh_status()
            self._write_user(task)
            # While the agent works we show only the animated dots indicator
            # (#busy); the text activity line ("KaroX работает…", "Останавливаю…")
            # used to fight with the dots and was removed by user request.
            self._set_activity("", "idle")
            argv = _agent_argv(task, self.repository, self.verification, session_id)

            def execute() -> None:
                code, output = _run_agent_cli(
                    argv, on_process=lambda proc: setattr(self, "agent_process", proc)
                )
                self.call_from_thread(self._agent_finished, code, output)

            self.run_worker(execute, thread=True, exclusive=True, group="agent")

        def action_stop_agent(self) -> None:
            if not self.agent_busy:
                return
            self._stop_requested = True
            process = self.agent_process
            if process is not None:
                try:
                    process.terminate()
                except OSError:
                    pass
            # Hide the animated dots immediately: once stopped the work indicator
            # should not keep blinking.  The "you stopped the task" notice is
            # shown by _agent_finished, not as a text activity line.
            self.query_one("#busy", LoadingIndicator).styles.display = "none"
            self._set_activity("", "idle")

        def action_copy_selection(self) -> None:
            """Copy, whether or not a task is running, and say what was copied.

            Three things this deliberately does not do, each of which it used to.

            It does not stop the agent. Ctrl+C used to return early to
            ``action_stop_agent`` whenever the agent was busy, so during a long
            task the copy key aborted the task and copied nothing. Esc stops.

            It does not silently substitute something else. When a selection
            exists, that selection is what reaches the clipboard. When none does,
            the last answer is copied as a convenience -- but the notice says which
            of the two happened, because being told "Copied" after selecting a line
            and receiving a different message is worse than being told nothing.

            It does not swallow a failure. A selection that cannot be read reports
            that, rather than presenting as an empty selection and falling through.
            """
            composer = self.query_one("#composer", Input)
            if composer.has_focus and composer.selected_text:
                # Let Input.action_copy handle copying the in-composer selection.
                raise SkipAction()

            try:
                selected = self.screen.get_selected_text()
            except Exception as error:
                self.notify(
                    self._label(
                        f"Не удалось прочитать выделение: {error}",
                        f"Could not read the selection: {error}",
                    ),
                    severity="error",
                )
                return

            if selected and selected.strip():
                self._copy_text(selected)
                self.notify(self._label("Скопировано выделение", "Selection copied"))
                return

            fallback = (self._last_assistant_content or "").strip()
            if not fallback:
                self.notify(
                    self._label(
                        "Нечего копировать: ничего не выделено.",
                        "Nothing to copy: nothing is selected.",
                    )
                )
                return
            self._copy_text(fallback)
            self.notify(
                self._label(
                    "Выделения нет — скопирован последний ответ",
                    "Nothing selected — copied the last answer",
                )
            )

        def _copy_text(self, text: str) -> None:
            """Put text on the clipboard, including over SSH and inside tmux.

            ``App.copy_to_clipboard`` emits OSC 52, which is what makes a copy from
            a remote terminal reach the local clipboard at all; a terminal that
            refuses the sequence is why the transcript remains selectable with the
            terminal's own mouse as well.
            """
            self.copy_to_clipboard(text)

        def _begin_step(self, call_id: str, label: str) -> None:
            if call_id in self._steps:
                return
            self._steps[call_id] = {
                "label": label,
                "started": time.monotonic(),
                "line": None,
            }
            self._render_steps()

        def _finish_step(
            self, call_id: str, name: str, detail: str, *, failed: bool
        ) -> None:
            step = self._steps.get(call_id)
            if step is None:
                # A resumed session replays results whose call the interface
                # never watched start, so there is nothing to time.
                self._steps[call_id] = step = {"label": name, "started": None}
            mark = "✕" if failed else "✓"
            elapsed = ""
            started = step.get("started")
            if isinstance(started, float):
                seconds = time.monotonic() - started
                # Below a second the number is mostly the polling interval, and
                # printing it would present the interface's own latency as the
                # tool's.
                if seconds >= 1.0:
                    elapsed = f" {seconds:.0f}s"
            step["line"] = (
                f"{mark} {escape(name)}{elapsed}"
                + (f"  [dim]{escape(detail)}[/]" if detail else "")
            )
            self._render_steps()

        def _render_steps(self) -> None:
            """Show the whole turn, not only whatever ran last.

            A three-tool turn used to overwrite one line twice, so the user saw
            the third tool and no evidence that the first two had happened.
            """
            while len(self._steps) > self.MAX_VISIBLE_STEPS:
                self._steps.pop(next(iter(self._steps)))
            lines = [
                step["line"]
                if step.get("line")
                else f"⟳ {escape(str(step['label']))}…"
                for step in self._steps.values()
            ]
            if lines:
                self._set_activity("\n".join(lines))

        def _poll_agent_history(self) -> None:
            if not self.agent_busy or not self.active_session:
                return
            store = SessionStore(session_dir())
            state_path = store.state_path(self.active_session)
            try:
                stat = state_path.stat()
            except OSError:
                return
            # Loading a session reads the whole document, re-serialises it
            # canonically and checksums it. That record holds every tool result
            # the run has produced, so at three times a second on a long task it
            # was several milliseconds of work per tick on the thread that draws
            # the interface -- for a file that had usually not changed at all.
            fingerprint = (stat.st_mtime_ns, stat.st_size)
            if fingerprint == self._history_fingerprint:
                return
            self._history_fingerprint = fingerprint
            try:
                history = store.load(self.active_session).provider_history
            except Exception:
                return
            new_entries = history[self._history_seen :]
            self._history_seen = len(history)
            for entry in new_entries:
                role = entry.get("role")
                if role == "assistant":
                    content = entry.get("content")
                    text = str(content) if content else ""
                    if text.strip():
                        # Show the model's answer as a distinct chat bubble,
                        # visually separated from the tool/command activity line.
                        self._last_assistant_content = text
                        self._write_assistant(text)
                    for call in entry.get("tool_calls") or ():
                        if isinstance(call, dict):
                            name = str(call.get("name") or "tool")
                            ru, en = _TOOL_LABELS.get(
                                _canonical_tool_name(name), (name, name)
                            )
                            self._begin_step(
                                str(call.get("call_id") or name),
                                self._label(ru, en),
                            )
                elif role == "tool":
                    name = str(
                        entry.get("core_name") or entry.get("tool_name") or "tool"
                    )
                    result = entry.get("result")
                    details: List[str] = []
                    if isinstance(result, dict):
                        data = result.get("data")
                        if isinstance(data, dict):
                            for key in ("path", "changed", "exit_code"):
                                if key in data:
                                    details.append(f"{key}={data[key]}")
                        details.insert(0, "ok" if result.get("ok", True) else "failed")
                    self._finish_step(
                        str(entry.get("tool_call_id") or name),
                        _canonical_tool_name(name),
                        " • ".join(details),
                        failed=isinstance(result, dict) and not result.get("ok", True),
                    )
                # provider_audit intentionally has no visible activity line:
                # showing "Модель: …" on every turn mixed the model identity
                # into the work indicator and cluttered the chat.

        def _agent_finished(self, code: int, output: str) -> None:
            self._poll_agent_history()
            self.agent_busy = False
            self.agent_process = None
            self.query_one("#composer", Input).disabled = False
            self.query_one("#busy", LoadingIndicator).styles.display = "none"
            if self._stop_requested:
                self._stop_requested = False
                self._write_notice(
                    self._label(
                        "Вы остановили задачу. Сессия сохранена — повторите задачу "
                        "для продолжения.",
                        "You stopped the task. The session was saved — re-run the "
                        "task to resume.",
                    ),
                    "warning",
                )
                self._refresh_status()
                self.query_one("#composer", Input).focus()
                return
            try:
                report = json.loads(output)
            except json.JSONDecodeError:
                report = None
            if isinstance(report, dict):
                message = (
                    report.get("provider_message")
                    or report.get("reason")
                    or "Finished."
                )
                verified = bool(report.get("verified"))
                state = "verified" if verified else str(report.get("status", "stopped"))
                text_message = str(message)
                # The polling reader above already showed this turn's answer, and
                # the report carries the same text again, so every reply was drawn
                # twice. `_last_assistant_content` was recorded for exactly this
                # comparison and never consulted.
                # A run that ends as `no_changes` got here because KaroX asked the
                # model to "give the answer and name the tool results it rests on"
                # after a turn that called no tools. There is nothing to name, so
                # the reply is necessarily a report about itself -- "the task was
                # only a greeting, no repository change was required" -- and the
                # user has already read the actual reply above it. The one-line
                # notice below says the same thing without a paragraph of it.
                ceremony = (
                    report.get("reason") == "no_changes"
                    and bool(self._last_assistant_content.strip())
                )
                if (
                    text_message
                    and not ceremony
                    and text_message.strip() != self._last_assistant_content.strip()
                ):
                    self._write_assistant(text_message)
                changed = ", ".join(report.get("changed_files") or [])
                if verified and report.get("reason") == "answer":
                    # A question is answered, not verified. Saying "completed
                    # and verified" over an empty file list described a change
                    # that never happened, so the answer names what it read.
                    basis = report.get("answer_basis") or []
                    sources = ", ".join(
                        sorted({str(item.get("tool")) for item in basis})
                    )
                    self._set_activity(
                        self._label(
                            f"[bold]Ответ получен[/]\nОснование: {escape(sources)}",
                            f"[bold]Answered[/]\nBased on: {escape(sources)}",
                        ),
                        "success",
                    )
                elif verified:
                    completion = self._label(
                        "Задача завершена и проверена.",
                        "Task completed and verified.",
                    )
                    if changed:
                        completion += self._label(
                            f" Изменены файлы: {changed}",
                            f" Changed files: {changed}",
                        )
                    self._set_activity(
                        f"[bold]{escape(completion)}[/]", "success"
                    )
                elif report.get("reason") == "no_changes":
                    # Nothing changed and the reply rested on nothing KaroX
                    # watched the model read, so this is neither a change nor an
                    # answer. It is still not a crash, so it is stated plainly
                    # rather than dressed up as an error.
                    # One line, not three. The paragraph that used to sit here
                    # repeated what the suppressed report above already said, in a
                    # window where the answer itself had a few rows to live in.
                    self._set_activity(
                        self._label(
                            "[bold]Без изменений[/] — файлы не менялись, "
                            "репозиторий не читался.",
                            "[bold]No change[/] — no file was changed and nothing "
                            "was read from the repository.",
                        ),
                        "warning",
                    )
                else:
                    reason = str(report.get("reason") or state)
                    self._set_activity(
                        self._label(
                            f"[bold]Задача не завершена[/]\nПричина: {escape(reason)}",
                            f"[bold]Task did not complete[/]\nReason: {escape(reason)}",
                        ),
                        "error",
                    )
            else:
                safe = output.strip() or f"Agent exited with code {code}."
                self._write_notice(safe, "error")
                self._set_activity(
                    self._label(
                        "[bold]Задача завершилась ошибкой[/]",
                        "[bold]Task failed[/]",
                    ),
                    "error",
                )
            self._refresh_status()
            self.query_one("#composer", Input).focus()

        def _run_ask(self, message: str) -> None:
            if not message:
                self._write_notice(
                    self._label(
                        "Формат: /ask ваш вопрос или /ask --target promptql вопрос",
                        "Usage: /ask your question or /ask --target promptql question",
                    ),
                    "warning",
                )
                return
            target_id = "promptql"
            text = message
            if message.startswith("--target "):
                rest = message[len("--target ") :].strip()
                parts = rest.split(None, 1)
                if len(parts) < 2:
                    self._write_notice(
                        self._label(
                            "Формат: /ask --target promptql ваш вопрос",
                            "Usage: /ask --target promptql your question",
                        ),
                        "warning",
                    )
                    return
                target_id, text = parts[0], parts[1].strip()
            self._write_user(text)
            self._set_activity(
                self._label(
                    f"Запрашиваю {target_id}…", f"Asking {target_id}…"
                )
            )

            def execute() -> None:
                code, output = _capture_cli(
                    ["target", "ask", target_id, "--message", text, "--json"]
                )
                self.call_from_thread(self._ask_finished, code, output, target_id)

            self.run_worker(execute, thread=True, exclusive=True, group="ask")

        def _ask_finished(self, code: int, output: str, target_id: str) -> None:
            self._set_activity("", "idle")
            if code != 0:
                cleaned = _clean_cli_error(output).strip()
                self._write_notice(
                    self._label(
                        f"Цель {target_id} не ответила: {cleaned or str(code)}",
                        f"Target {target_id} failed: {cleaned or str(code)}",
                    ),
                    "error",
                )
                return
            try:
                payload = json.loads(output)
            except json.JSONDecodeError:
                self._write_notice(
                    self._label(
                        "Цель вернула не-JSON ответ.",
                        "Target returned a non-JSON response.",
                    ),
                    "error",
                )
                return
            actions = payload.get("assistant_actions") or []
            if not actions:
                self._write_assistant(
                    self._label(
                        f"{target_id} вернул пустой ответ.",
                        f"{target_id} returned an empty answer.",
                    )
                )
                return
            blocks = []
            for item in actions:
                if isinstance(item, dict):
                    message_text = item.get("message")
                    if isinstance(message_text, str) and message_text.strip():
                        blocks.append(message_text)
            body = "\n\n".join(blocks) or self._label(
                "Ответ без текста.", "Answer without text."
            )
            self._write_assistant(body)

        def _run_inspection(self, argv: Sequence[str], label: str) -> None:
            loading = {
                "/models": ("Получаю список моделей…", "Loading models…"),
                "/sessions": ("Получаю список сессий…", "Loading sessions…"),
                "/mcp": ("Получаю список MCP-серверов…", "Loading MCP servers…"),
                "/doctor": ("Проверяю KaroX…", "Checking KaroX…"),
            }
            message = loading.get(label, ("Выполняю команду…", "Running command…"))
            self.query_one("#busy", LoadingIndicator).styles.display = "block"
            self.query_one("#composer-hint", Static).update(
                message[1] if self.language == "en" else message[0]
            )

            def execute() -> None:
                code, output = _capture_cli(argv)
                self.call_from_thread(self._inspection_finished, label, code, output)

            self.run_worker(execute, thread=True, exclusive=True, group="inspection")

        def _inspection_finished(self, label: str, code: int, output: str) -> None:
            self.query_one("#busy", LoadingIndicator).styles.display = "none"
            self.query_one("#composer-hint", Static).update(
                _TEXT[self.language]["hint"]
            )
            color = "#b3a990" if code == 0 else "#e0a3a3"
            message = _inspection_text(label, code, output, self.language)
            self._write(f"[{color}]{escape(message)}[/]")

        def action_clear_log(self) -> None:
            self._transcript().clear()


# Core tool names as the audit log and the session record spell them. The model
# sees the same names with dots replaced by underscores, so a running step and
# its completion line used to disagree about what had just run.
_CORE_TOOL_NAMES = (
    "repo.read_file",
    "repo.read_lines",
    "repo.write_file",
    "repo.edit_file",
    "repo.list_files",
    "repo.search",
    "checks.run",
    "git.status",
    "git.diff",
    "git.log",
    "git.commit",
)
_ALIAS_TO_CORE_TOOL = {name.replace(".", "_"): name for name in _CORE_TOOL_NAMES}

_TOOL_LABELS = {
    "repo.list_files": ("просматривает файлы", "listing files"),
    "repo.search": ("ищет по коду", "searching the code"),
    "repo.read_file": ("читает файл", "reading a file"),
    "repo.read_lines": ("читает фрагмент", "reading a region"),
    "repo.write_file": ("изменяет файл", "editing a file"),
    "repo.edit_file": ("правит файл", "editing a file"),
    "checks.run": ("запускает проверку", "running checks"),
    "git.status": ("проверяет изменения", "checking changes"),
    "git.diff": ("анализирует diff", "reviewing the diff"),
    "git.log": ("читает историю", "reading history"),
    "git.commit": ("фиксирует изменения", "committing"),
}


def _canonical_tool_name(name: str) -> str:
    """Spell a tool the one way, whichever side of the boundary named it."""
    return _ALIAS_TO_CORE_TOOL.get(name, name)


def _line_writer(stream: Any) -> Callable[[str], None]:
    """Write to a possibly-redirected stream without dying on a glyph.

    Line mode is what runs when stdout is a pipe or a file, and on Windows that
    stream defaults to the system code page rather than UTF-8. The status
    glyphs KaroX prints are not in cp1251, so redirecting output crashed the
    whole session with UnicodeEncodeError instead of printing a status line.
    """
    with contextlib.suppress(Exception):
        stream.reconfigure(encoding="utf-8")

    def write(text: str) -> None:
        try:
            stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(stream, "encoding", None) or "ascii"
            stream.write(
                text.encode(encoding, "replace").decode(encoding, "replace")
            )

    return write


def _line_help(out: Callable[[str], Any]) -> None:
    out("KaroX commands:\n")
    for command, description in SLASH_COMMANDS.items():
        out(f"  {command:<18} {description}\n")


# Zero-width marks a shell can put in front of a piped line. Windows PowerShell
# writes a UTF-8 BOM ahead of the first line it pipes into a program, and
# ``str.strip()`` does not remove it.
_LINE_NOISE = "﻿￾​‎‏"


def _clean_line(line: str) -> str:
    """Reduce a line of input to what the user actually typed.

    A leading BOM made ``/quit`` match neither the command table nor even
    ``startswith("/")``, so it fell through to the agent: an exit became a billed
    model request that answered "I cannot quit". Anything invisible is removed
    before a command is recognised, so a shell's framing cannot cost money.
    """
    return line.strip().strip(_LINE_NOISE).strip()


def _run_line_mode(
    repository: Path,
    *,
    input_stream: Any,
    output_stream: Any,
) -> int:
    """Non-full-screen fallback for redirected stdin and minimal terminals."""
    out = _line_writer(output_stream)
    out("KaroX line mode. Type a task, /help, or /quit.\n")
    verification = _default_verification(repository)
    while True:
        line = input_stream.readline()
        if line == "":
            return 0
        value = _clean_line(line)
        if not value:
            continue
        if value in {"/quit", "/exit"}:
            return 0
        if value == "/help":
            _line_help(out)
            continue
        if value in _BACKEND_SLASH:
            code, output = _capture_cli(_BACKEND_SLASH[value])
            out(_inspection_text(value, code, output, "en") + "\n")
            continue
        if value.startswith("/"):
            out(f"Unknown command: {value.split()[0]}. Type /help.\n")
            continue
        selected = _selected_model()
        if selected is None:
            out(
                "No API model is configured. Run karox in an interactive terminal "
                "and press Ctrl+S, or use `karox provider` / `karox model`.\n"
            )
            continue
        sid = f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        _run_cli(_agent_argv(value, repository, verification, sid), out)
        out("\n")


def run_tui(
    *,
    session_id: Optional[str] = None,
    repository: Optional[str] = None,
    input_stream: Any = None,
    output_stream: Any = None,
) -> int:
    """Open the full-screen KaroX app, or line mode for redirected streams.

    The full-screen Textual UI launches when both standard streams are live
    terminals (``isatty()``); redirected streams (pipes, ``StringIO`` in tests,
    EOF) get the line-mode reader, which exits cleanly on EOF.

    ``karox`` with no arguments reaches this through ``cli.main`` without
    passing streams, so they are resolved against the *current* ``sys.stdin``
    /``sys.stdout`` here rather than captured as default arguments. The
    interactive decision is based on ``isatty()`` instead of object identity
    with ``sys.stdin``/``sys.stdout``: identity breaks the moment a host
    rebinds those streams between import and launch (some terminal wrappers,
    the ``python -m`` entry path, and redirected hosts), which silently routed
    a real interactive console to line mode and an immediate EOF exit.
    """
    repo = Path(repository or os.getcwd()).expanduser().resolve()
    if input_stream is None:
        input_stream = sys.stdin
    if output_stream is None:
        output_stream = sys.stdout
    interactive = (
        bool(getattr(input_stream, "isatty", lambda: False)())
        and bool(getattr(output_stream, "isatty", lambda: False)())
    )
    if interactive and _HAS_TEXTUAL:
        result = KaroXApp(repo, session_id=session_id).run()
        return int(result or 0)
    if interactive and not _HAS_TEXTUAL:
        output_stream.write(
            "KaroX full-screen UI is unavailable; reinstall to restore the textual dependency.\n"
        )
    return _run_line_mode(
        repo,
        input_stream=input_stream,
        output_stream=output_stream,
    )


__all__ = [
    "BridgeSetup",
    "ProviderSetup",
    "SLASH_COMMANDS",
    "run_tui",
]
