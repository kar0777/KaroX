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
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

import httpx

from .bridge import BridgeCredentialStore
from .credentials import CredentialStore
from .event_bus import EventBus, EventKind, EventLevel, event_bus
from .models import AccessProfile
from .paths import config_dir, session_dir
from .provider_controller import ProviderController
from .provider_factory import ProviderFactory
from .provider_presets import (
    ProviderPreset,
    provider_preset,
    provider_presets,
    sponsor_messages,
)
from .agent_modes import (
    DEFAULT_MODE,
    ModeError,
    mode_display_name,
    mode_summary,
    normalize_mode,
)
from .effort import (
    AUTO_EFFORT,
    effort_summary,
    normalize_effort,
)
from .providers import (
    REASONING_EFFORTS,
    ModelMessage,
    ModelRequest,
    ProviderError,
    ProviderErrorKind,
)
from .registry import ModelRecord, ProviderRecord, ProviderRegistry
from .session_view import (
    ACTION_REVIEW_RISK,
    ACTION_STOP,
    WAIT_CONFIRMATION,
    RiskStateView,
    SessionDetail,
    SessionSummary,
    SessionViewStore,
    TimelineEntry,
    ToolCallView,
)
from .security import redact
from .sessions import SessionStore
from .tailscale import TailscaleError, find_tailscale, prepare_tailscale_funnel
from .verification import discover_verification_commands
from .web_bridge_launcher import (
    MUTATING_WEB_TOOLS,
    WEB_BRIDGE_PROFILES,
    find_cloudflared,
)

try:
    from rich.markup import escape
    from rich.text import Text
    from textual import events, on
    from textual.app import App, ComposeResult, SkipAction, SystemCommand
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
    "/model": "choose model",
    "/mode": "show or set agent mode (build, plan, ideate)",
    "/map": "project map: status, preview, refresh, or a level (low..ultra)",
    "/memory": "inspect, remember, edit, or forget KaroX memory",
    "/effort": "show or set agent effort (auto, low, medium, high, extra-high, ultra)",
    "/status": "show project, model, effort, and run state",
    "/home": "return to chat",
    "/usage": "show model usage, cache, and cost",
    "/cost": "show or switch the run economy profile",
    "/connect": "connect an API model, website, or both",
    "/models": "show configured API models",
    "/sessions": "show task sessions (compact browser)",
    "/sessions --verbose": "show task sessions as text",
    "/session-log": "alias for /sessions --verbose",
    "/bridge": "connect PromptQL, Notion, or an MCP/OpenAPI client",
    "/bridge stop": "stop the active bridge",
    "/ask TEXT": "ask a configured hosted agent target (PromptQL)",
    "/mcp": "show external MCP servers",
    "/doctor": "run KaroX diagnostics",
    "/verify JSON": "change the verification command",
    "/language": "change interface language",
    "/project": "switch project: manager, or /project ID or PATH",
    "/workspace PATH": "change the working project folder",
    "/sponsors": "show or hide the sponsor line",
    "/clear": "clear the conversation",
    "/new": "start a new task (durable sessions are kept)",
    "/resume": "return to a session: browser, or /resume SESSION_ID",
    "/compact": "compact the conversation into a handoff + continuation context",
    "/help": "show command help",
    "/quit": "exit KaroX",
    "/connections": "manage connections (MCP clients and API providers)",
    "/providers": "manage API providers",
    "/mcp-clients": "manage MCP client targets",
}

_COMMANDS_RU: Dict[str, str] = {
    "/model": "выбрать модель",
    "/mode": "режим агента (build, plan, ideate)",
    "/map": "карта проекта: status, preview, refresh или уровень (low..ultra)",
    "/memory": "память KaroX: просмотр, запись, правка, удаление",
    "/effort": "уровень усилий агента (auto, low, medium, high, extra-high, ultra)",
    "/status": "показать проект, модель, Effort и состояние запуска",
    "/home": "вернуться в чат",
    "/usage": "показать токены, кэш и расходы",
    "/cost": "режим расходов",
    "/connect": "подключить API-модель, сайт или оба варианта",
    "/models": "показать настроенные API-модели",
    "/sessions": "показать сессии задач (компактный браузер)",
    "/sessions --verbose": "показать сессии задач как текст",
    "/session-log": "псевдоним для /sessions --verbose",
    "/bridge": "подключить PromptQL, Notion или MCP/OpenAPI-клиент",
    "/bridge stop": "остановить активный мост",
    "/ask ТЕКСТ": "задать вопрос настроенному агенту-цели (PromptQL)",
    "/mcp": "показать внешние MCP-серверы",
    "/doctor": "запустить диагностику KaroX",
    "/verify JSON": "изменить команду проверки",
    "/language": "изменить язык интерфейса",
    "/project": "проекты: менеджер или /project ID или ПУТЬ",
    "/workspace ПУТЬ": "изменить рабочую папку проекта",
    "/sponsors": "показать или скрыть строку спонсоров",
    "/clear": "очистить диалог",
    "/new": "начать новую задачу (сессии сохраняются)",
    "/resume": "вернуться к сессии: браузер или /resume ID",
    "/compact": "сжать диалог в handoff и контекст продолжения",
    "/help": "показать справку по командам",
    "/quit": "выйти из KaroX",
    "/connections": "управление подключениями (MCP-клиенты и API-провайдеры)",
    "/providers": "управление API-провайдерами",
    "/mcp-clients": "управление MCP-клиентами",
}

_TEXT: Dict[str, Dict[str, str]] = {
    "ru": {
        "brand": "KaroX\n[dim]API-модели • локальные инструменты • сайты и MCP[/dim]",
        "placeholder": "Опишите задачу для KaroX…",
        "hint": "Enter — отправить • Ctrl+G — модель и Effort • / — команды • Ctrl+C — остановить/выйти",
        "welcome_ready": "[bold #e0dccc]KaroX готов.[/]\nНапишите задачу обычным текстом.\n[#d4b676]Ctrl+G[/] — модель и Effort; [#d4b676]/[/] — остальные действия.",
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
        "suggestion": "Возможно, вы имели в виду [#d4b676]{cmd}[/].",
        "connect_first": "Для отправки задачи сначала подключите API-модель через [#d4b676]/connect[/].",
    },
    "en": {
        "brand": "KaroX\n[dim]API models • local tools • websites and MCP[/dim]",
        "placeholder": "Describe a task for KaroX…",
        "hint": "Enter — send • Ctrl+G — model and Effort • / — commands • Ctrl+C — stop/exit",
        "welcome_ready": "[bold #e0dccc]KaroX is ready.[/]\nDescribe a task in plain language.\n[#d4b676]Ctrl+G[/] — model and Effort; [#d4b676]/[/] — every other action.",
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
        "suggestion": "Did you mean [#d4b676]{cmd}[/]?",
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


def _load_effort_level() -> str:
    """The persisted /effort choice; unknown spellings fall back to auto.

    Fallback rather than raise: a hand-edited ui.json must never prevent the
    TUI from starting, and auto is the only always-safe level.
    """

    raw = _load_preferences().get("effort_level", AUTO_EFFORT)
    try:
        return normalize_effort(raw)
    except ValueError:
        return AUTO_EFFORT


def _save_effort_level(level: str) -> None:
    _save_preferences(effort_level=normalize_effort(level))


def _load_agent_mode() -> str:
    """The persisted /mode choice; unknown spellings fall back to Build.

    Same contract as the effort ladder: a hand-edited ui.json must never
    prevent the TUI from starting, and Build is the only stance whose
    behavior matches the product before modes existed.
    """

    raw = _load_preferences().get("agent_mode", DEFAULT_MODE)
    try:
        return normalize_mode(raw)
    except ModeError:
        return DEFAULT_MODE


def _save_agent_mode(mode: str) -> None:
    _save_preferences(agent_mode=normalize_mode(mode))


_RECENT_WORKSPACE_LIMIT = 8


def _load_recent_workspaces() -> Tuple[str, ...]:
    """Return recent workspace paths that still exist, newest first."""
    raw = _load_preferences().get("recent_workspaces", [])
    if not isinstance(raw, list):
        return ()
    result: List[str] = []
    seen = set()
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            resolved = str(Path(value).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
        key = os.path.normcase(resolved)
        if key in seen or not Path(resolved).is_dir():
            continue
        seen.add(key)
        result.append(resolved)
        if len(result) >= _RECENT_WORKSPACE_LIMIT:
            break
    return tuple(result)


def _remember_workspace(path: Path) -> None:
    """Persist one workspace without allowing the recent list to grow forever."""
    resolved = str(path.expanduser().resolve())
    key = os.path.normcase(resolved)
    recent = [
        item
        for item in _load_recent_workspaces()
        if os.path.normcase(item) != key
    ]
    _save_preferences(
        recent_workspaces=[resolved, *recent][:_RECENT_WORKSPACE_LIMIT]
    )


# The commands a person is offered. KaroX is a coding agent, not a control
# panel: the composer is for describing work, and the slash menu exists so the
# few things that are *not* work are reachable -- not to advertise the surface.
#
# Everything else in SLASH_COMMANDS still runs when typed. Hidden is not removed:
# `/verify`, `/ask`, `/sponsors`, `/mcp` and `/bridge stop` are real commands with
# real users, and deleting them to shorten a menu would be a regression dressed
# up as simplification.
VISIBLE_COMMANDS: Tuple[str, ...] = (
    "/model",
    "/mode",
    "/effort",
    "/map",
    "/memory",
    "/status",
    "/usage",
    "/connect",
    "/sessions",
    "/new",
    "/resume",
    "/compact",
    "/project",
    "/workspace",
    "/help",
    "/quit",
)

# Where a retired connection command now goes. One product scenario had five
# entry points -- `/connect`, `/connections`, `/providers`, `/mcp-clients` and
# `/bridge` -- which is four ways for two screens to disagree about what is
# connected. They all resolve to the single Connections screen; the value is the
# section it should open on, so a person who typed `/providers` still lands where
# they meant to go.
#
# Deliberately no per-use warning in the chat: a message on every invocation is
# noise, and the alias is not a mistake the user made -- it is a path the product
# used to offer.
CONNECT_FOCUS_MODELS = "ai_models"
CONNECT_FOCUS_CLIENTS = "external_clients"

DEPRECATED_COMMAND_ALIASES: Dict[str, Optional[str]] = {
    "/connection": None,
    "/connections": None,
    "/providers": CONNECT_FOCUS_MODELS,
    "/mcp-clients": CONNECT_FOCUS_CLIENTS,
    "/bridge": CONNECT_FOCUS_CLIENTS,
}


def _commands(language: str) -> Dict[str, str]:
    """The slash menu and ``/help``, in the user's language.

    Filtered to :data:`VISIBLE_COMMANDS` rather than assembled separately, so the
    descriptions cannot drift from the routing table and a command cannot be
    listed in one language and missing in the other.

    A prefix match is used for ``/workspace`` because the catalogs spell it with
    its argument (``/workspace PATH``, ``/workspace ПУТЬ``), which is what makes
    the menu entry self-explanatory.
    """

    catalog = _COMMANDS_RU if language == "ru" else SLASH_COMMANDS
    visible: Dict[str, str] = {}
    for name, description in catalog.items():
        head = name.split(" ", 1)[0]
        if head in VISIBLE_COMMANDS:
            visible[name] = description
    return visible


def _suggest_command(typed: str, language: str) -> str:
    """Return a 'Did you mean ...' suggestion for an unknown slash command.

    Uses difflib.get_close_matches against every routable command so a typo
    like ``/connections`` suggests ``/connection`` and ``/cnnection`` suggests
    ``/connection``. Returns an empty string when no close match exists.
    """
    import difflib

    candidates: set[str] = {
        name.split(" ", 1)[0] for name in SLASH_COMMANDS
    } | set(DEPRECATED_COMMAND_ALIASES) | {"/setup", "/browser", "/exit"}
    matches = difflib.get_close_matches(typed, sorted(candidates), n=1, cutoff=0.6)
    if not matches:
        return ""
    suggested = matches[0]
    if language == "ru":
        return _TEXT["ru"].get("suggestion", "Возможно, вы имели в виду {cmd}.").format(cmd=suggested)
    return _TEXT["en"].get("suggestion", "Did you mean {cmd}?").format(cmd=suggested)


_CONTINUATION_CONTEXT_LIMIT = 4000


def _continuation_from_handoff(document: Mapping[str, Any]) -> str:
    """Render a bounded continuation preamble from a structured handoff.

    ``build_handoff`` already redacted and length-capped every field; this
    rendering only selects and truncates, it never adds payloads. Goal and
    constraints come first because they are the contract, then what happened,
    then what is still open.
    """

    parts: list[str] = []

    def _add(label: str, value: Any) -> None:
        text = str(value).strip() if value is not None else ""
        if text:
            parts.append(f"{label}: {text}")

    _add("Goal", document.get("goal"))
    constraints = document.get("constraints")
    if isinstance(constraints, Mapping):
        _add("Repository", constraints.get("repository"))
        _add("Branch", constraints.get("branch"))
        _add("Access profile", constraints.get("access_profile"))
    _add("Summary", document.get("summary"))
    changed = document.get("changed_files")
    if isinstance(changed, list) and changed:
        shown = [str(item) for item in changed[-20:]]
        suffix = "" if len(changed) <= 20 else f" (+{len(changed) - 20} more)"
        parts.append("Changed files: " + ", ".join(shown) + suffix)
    checks = document.get("check_results")
    if isinstance(checks, list) and checks:
        rendered: list[str] = []
        for item in checks[-5:]:
            if not isinstance(item, Mapping):
                continue
            argv = item.get("argv")
            command = (
                " ".join(str(part) for part in argv)
                if isinstance(argv, list)
                else str(argv or "?")
            )
            rendered.append(f"{command} -> exit {item.get('exit_code')}")
        if rendered:
            parts.append("Recent checks: " + "; ".join(rendered))
    remaining = document.get("remaining_steps")
    if isinstance(remaining, list) and remaining:
        parts.append(
            "Remaining steps: " + "; ".join(str(step) for step in remaining[:10])
        )
    errors = document.get("errors")
    if isinstance(errors, list) and errors:
        parts.append("Open errors: " + "; ".join(str(item) for item in errors[-5:]))
    unfinished = document.get("unfinished_actions")
    if isinstance(unfinished, list) and unfinished:
        parts.append(
            "Unfinished actions: "
            + "; ".join(str(item) for item in unfinished[-5:])
        )
    return "\n".join(parts)[:_CONTINUATION_CONTEXT_LIMIT]


def _resolve_project_target(registry: Any, target: str) -> Optional[str]:
    """Resolve a /project argument to an approved path: id first, then path.

    Returns ``None`` when the registry does not know the target, so the caller
    can fall through to the same path rules /workspace already enforces -- the
    two spellings must not disagree about safety.
    """

    from .project_registry import ProjectRegistryError

    cleaned = target.strip().strip('"')
    try:
        return str(registry.get(cleaned).path)
    except (ProjectRegistryError, OSError):
        pass
    try:
        entry = registry.entry_for_path(cleaned)
    except (ProjectRegistryError, OSError):
        return None
    return str(entry.path) if entry is not None else None


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

# Commands that are valid but need the interactive Textual TUI — they open
# modal screens the line-mode fallback cannot render. Derived from the same
# routing contract as the full-screen app, not a divergent hand-maintained list.
_LINE_INTERACTIVE_ONLY: frozenset[str] = (
    frozenset(VISIBLE_COMMANDS)
    | frozenset(DEPRECATED_COMMAND_ALIASES.keys())
    | {"/setup", "/browser", "/clear", "/verify", "/ask"}
) - frozenset(_BACKEND_SLASH) - {"/quit", "/help"}

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

# SessionSummary carries stable machine identifiers, never localized prose, so
# the words a user reads live here in the UI catalog. An unknown identifier is
# shown verbatim rather than hidden: a status nobody translated yet is still a
# fact about the run.
def _identifier(value: Any) -> str:
    """Normalise a persisted field to the stable identifier a view model wants.

    Session records hold Enum members for fields such as ``access_profile``.
    Passing one straight through renders ``AccessProfile.WORKSPACE_WRITE`` in
    the interface, which is an internal enum leaking into user-facing text and
    also fails every identifier comparison in the store. The boundary adapter
    unwraps ``.value`` so events and durable records agree on one vocabulary.
    """

    if value is None:
        return ""
    inner = getattr(value, "value", value)
    return inner if isinstance(inner, str) else str(inner)


# Which UI projections typed events actually own today. The migration is per
# projection: a SESSION_STATE event proves the status row and proves nothing
# about the transcript, so anything not listed here still reads the legacy
# provider_history path. Add a name here only when a real typed publisher for
# it exists, otherwise the compatibility fallback goes silent and the user
# watches a frozen screen while the agent is still working.
PROJECTION_STATUS = "status"
PROJECTION_TRANSCRIPT = "transcript"
PROJECTION_TOOL_ACTIVITY = "tool_activity"
PROJECTION_USAGE = "usage"
PROJECTION_EVIDENCE = "evidence"
PROJECTION_ERRORS = "errors"

_EVENT_BACKED_PROJECTIONS: frozenset[str] = frozenset({PROJECTION_STATUS})

# The parent TUI process owns the agent lifecycle: it starts the child, reads its
# structured result and decides that a run succeeded, failed or was cancelled.
# EventBus is a process-local singleton, so a typed event published inside the
# child never reaches this process. Until a framed child transport exists these
# lifecycle transitions are published here, by the same methods the product runs,
# which is what makes the status projection a real production path rather than a
# test fixture.
EVENT_SOURCE_TUI_AGENT = "tui.agent"

# A stable, machine-readable error code. A UI switches on this; the human words
# stay in the localisation catalogs and never become the contract.
ERROR_AGENT_FAILED = "agent_failed"


class RunIdentity(NamedTuple):
    """Which run a completion callback belongs to.

    A session id alone is not enough. One session can be run again -- a resume,
    or the same task submitted twice -- and the worker callback of the previous
    attempt can arrive after the next one has already started. Keyed only by
    session, that late callback would clear ``agent_busy``, drop the new child's
    process handle, re-enable the composer and publish a terminal status over a
    run that is still working.

    The generation is a monotonic counter on the application, not on the
    session: two different sessions therefore also get two different
    identities, and a counter that never reuses a value cannot be confused by a
    session id that repeats.
    """

    session_id: str
    generation: int

# Lifecycle statuses this process may publish. Constants rather than inline
# literals so the publisher, the localisation contract test and the session
# browser share one vocabulary instead of three copies of it.
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
# A run that reached its own end without a verified change and without an
# evidence-backed answer. Neither success nor crash: the session is saved and
# resumable, so it gets a neutral status and never a typed ERROR. ``stopped``
# is already in the store's waiting vocabulary, which is what makes the row
# offer ``resume`` rather than ``open``.
STATUS_STOPPED = "stopped"

# The CLI exit code and the structured report disagreeing is itself a defect:
# exit 0 beside a failed report, or exit 1 beside ``verified``. Guessing which
# half is honest is how a broken run gets a green row, so the disagreement is
# reported under its own code instead.
ERROR_AGENT_CONTRACT = "agent_contract_mismatch"

# A child that printed no parseable structured report at all. Distinct from a
# contract mismatch: there the two halves disagreed, here there is nothing to
# disagree with, and the two need different words and different diagnostics.
ERROR_AGENT_MALFORMED = "agent_malformed_report"

# ``Event.summary`` is a stable machine-readable message identifier, never a
# sentence. One field, not two: a second ``message_id`` beside a prose summary
# would inevitably drift, and the drifting copy would be the one a screen reads.
# The words a person sees come from _EVENT_SUMMARY_TEXT below, in their own
# language; the free-form diagnostic detail stays in the bounded ``data.reason``
# and, for an ERROR, ``data.code`` remains the machine contract.
SUMMARY_RUN_STARTED = "agent_run_started"
SUMMARY_RUN_COMPLETED = "agent_run_completed"
SUMMARY_RUN_STOPPED = "agent_run_stopped"
SUMMARY_RUN_FAILED = "agent_run_failed"
SUMMARY_RUN_CANCELLED = "agent_run_cancelled"
SUMMARY_RUN_USAGE = "agent_run_usage"
SUMMARY_CONTRACT_MISMATCH = "agent_contract_mismatch"
SUMMARY_MALFORMED_REPORT = "agent_malformed_report"

# Every lifecycle identifier this process can publish, with both languages. A
# separate catalog from _SESSION_STATUS_TEXT on purpose: a status describes what
# a session *is*, an event summary describes what just *happened*, and
# overloading one dictionary with both would make "stopped" mean two things.
_EVENT_SUMMARY_TEXT: Dict[str, Tuple[str, str]] = {
    SUMMARY_RUN_STARTED: ("запуск задачи", "run started"),
    SUMMARY_RUN_COMPLETED: ("задача завершена", "run completed"),
    SUMMARY_RUN_STOPPED: ("задача остановлена", "run stopped"),
    SUMMARY_RUN_FAILED: ("задача завершилась ошибкой", "run failed"),
    SUMMARY_RUN_CANCELLED: ("задача отменена пользователем", "run cancelled"),
    SUMMARY_RUN_USAGE: ("расход токенов", "token usage"),
    SUMMARY_CONTRACT_MISMATCH: (
        "противоречивый результат запуска",
        "inconsistent run result",
    ),
    SUMMARY_MALFORMED_REPORT: (
        "нет структурированного отчёта",
        "no structured report",
    ),
}


def _event_summary_text(identifier: str, english: bool) -> str:
    """Localize one event summary identifier, safely for unknown values.

    An identifier nobody has translated yet is returned verbatim rather than
    blanked or raised on: a message the catalog has not caught up with is still
    a fact about the run, and a diagnostic fallback beats an empty line.
    """

    words = _EVENT_SUMMARY_TEXT.get(identifier)
    if words is None:
        return identifier
    return words[1] if english else words[0]


# Every status identifier SessionViewStore can produce must be translatable:
# the release scope forbids showing a raw internal enum as the primary UI text.
# Kept in sync with _RUNNING_STATUSES / _WAITING_STATUSES / _TERMINAL_STATUSES
# in session_view.py, and asserted by a contract test.
_SESSION_STATUS_TEXT: Dict[str, Tuple[str, str]] = {
    "running": ("выполняется", "running"),
    "active": ("выполняется", "running"),
    "working": ("выполняется", "running"),
    "executing": ("выполняется", "running"),
    "planning": ("планирование", "planning"),
    "waiting": ("ожидание", "waiting"),
    "paused": ("пауза", "paused"),
    "blocked": ("заблокирована", "blocked"),
    "finished": ("завершена", "finished"),
    "completed": ("завершена", "completed"),
    "failed": ("ошибка", "failed"),
    "revoked": ("отозвана", "revoked"),
    "cancelled": ("отменена", "cancelled"),
    "stopped": ("остановлена", "stopped"),
    "unknown": ("неизвестно", "unknown"),
}

# The one thing the row asks the user to do. `review_risk` outranks the rest
# because it is the only state where the agent is stopped and waiting.
_SESSION_ACTION_TEXT: Dict[str, Tuple[str, str]] = {
    "review_risk": ("нужно подтверждение", "confirmation needed"),
    "resume": ("продолжить", "resume"),
    "stop": ("остановить", "stop"),
    "open": ("открыть", "open"),
}

# Why a run is not moving, in words. The identifiers are the reasons
# :mod:`karox.agent` puts in ``AgentReport.reason`` (``step_limit``,
# ``wall_time_limit``, ``budget_exceeded``, ``repeated_action``,
# ``unverified_changes``, ``no_changes``, ``provider_error``), plus the two this
# process publishes itself (``stopped_by_user`` and the store's
# ``confirmation_required``). A status word alone said "stopped" and left the
# user to guess whether the agent hit a limit, changed nothing, or was stopped
# by hand -- three different next actions.
_WAITING_REASON_TEXT: Dict[str, Tuple[str, str]] = {
    "no_changes": ("изменений нет", "no changes"),
    "unverified_changes": ("изменения не проверены", "unverified changes"),
    "step_limit": ("достигнут лимит шагов", "step limit reached"),
    "wall_time_limit": ("достигнут лимит времени", "time limit reached"),
    "budget_exceeded": ("превышен бюджет", "budget exceeded"),
    "repeated_action": ("повторное действие", "repeated action"),
    "provider_error": ("ошибка провайдера", "provider error"),
    "already_verified": ("уже проверено", "already verified"),
    "stopped": ("остановлена", "stopped"),
    "stopped_by_user": ("остановлена пользователем", "stopped by user"),
    WAIT_CONFIRMATION: ("нужно подтверждение", "confirmation needed"),
}


def _catalog_text(
    catalog: Dict[str, Tuple[str, str]], identifier: str, english: bool
) -> str:
    """One catalog lookup, with the identifier itself as the fallback.

    Every catalog in this module shares the rule: a value nobody has translated
    yet is rendered verbatim rather than blanked. A screen that silently drops an
    unknown identifier hides a real fact about the run, and one that raises takes
    the interface down over a missing dictionary entry.
    """

    words = catalog.get(identifier)
    if words is None:
        return identifier
    return words[1] if english else words[0]


def _session_status_words(status: str, english: bool) -> str:
    return _catalog_text(_SESSION_STATUS_TEXT, status, english)


def _session_action_words(action: str, english: bool) -> str:
    return _catalog_text(_SESSION_ACTION_TEXT, action, english)


# C. Whole shorter spellings of the same actions, for the narrow browser footer.
#
# The alternative was character truncation, and it does not survive contact with
# a reader: `Enter: нужно подтверж…` and `Enter: confirmatio…` both make
# the person guess what the key is about to do, which is precisely the guess a
# confirmation exists to prevent. Every entry here is a complete word for the
# same action, so the footer degrades in vocabulary and never in meaning.
#
# ``review_risk`` says "review" rather than "confirm": Enter opens the decision,
# it does not approve it, and a short form that promises approval would be a
# more dangerous lie than the long one it replaced.
_SESSION_ACTION_SHORT: Dict[str, Tuple[str, str]] = {
    "review_risk": ("проверить", "review"),
}


def _session_action_spellings(action: str, english: bool) -> Tuple[str, ...]:
    """Every whole way to name one action, longest first.

    Ordered so a caller with a width budget can walk it and stop at the first
    thing that fits. Duplicates are dropped, so an action whose long form is
    already short contributes one spelling rather than the same word twice.
    """

    spellings = [_session_action_words(action, english)]
    short = _SESSION_ACTION_SHORT.get(action)
    if short is not None:
        candidate = short[1] if english else short[0]
        if candidate and candidate not in spellings:
            spellings.append(candidate)
    return tuple(spellings)


# C. What an empty browser says, long and short. Same rule as the actions: the
# narrow form is a complete sentence, not the wide one with its instruction cut
# in half. "No sessions yet" is still true and still actionable; "No sessions
# yet. Start a ta…" is neither.
_BROWSER_EMPTY_TEXT: Tuple[Tuple[str, str], ...] = (
    (
        "Сессий пока нет. Начните задачу в чате.",
        "No sessions yet. Start a task in chat.",
    ),
    ("Сессий пока нет.", "No sessions yet."),
)


def _browser_empty_text(english: bool, width: int = 0) -> str:
    """The widest whole empty-state sentence that fits.

    Goes through the same policy as everything else in the footer instead of
    being written straight into the screen class, which is how this branch
    escaped the width budget in the first place and could wrap the one line the
    footer is allowed.
    """

    forms = [pair[1] if english else pair[0] for pair in _BROWSER_EMPTY_TEXT]
    if width <= 0:
        return forms[0]
    for form in forms:
        if len(form) <= width:
            return form
    # Narrower than the shortest sentence. Still whole: a cut instruction is
    # worse than a short one, and the binding works either way.
    return forms[-1]


def _waiting_reason_text(identifier: str, english: bool) -> str:
    """Localize a waiting reason, including the compound ones.

    ``budget_exceeded:output_tokens`` is a real value: :mod:`karox.agent`
    appends the specific budget to the reason. Only the part before the colon is
    a catalog key, so the qualifier is stripped for the lookup rather than
    turning the whole reason into an untranslated identifier on screen.
    """

    if not identifier:
        return ""
    if identifier in _WAITING_REASON_TEXT:
        return _catalog_text(_WAITING_REASON_TEXT, identifier, english)
    base = identifier.split(":", 1)[0]
    if base in _WAITING_REASON_TEXT:
        return _catalog_text(_WAITING_REASON_TEXT, base, english)
    return identifier


def _session_detail_words(row: SessionSummary, english: bool) -> str:
    """The single extra fact shown beside the status word.

    One detail, not three. A pending confirmation outranks everything because it
    is the only state waiting on the person reading the screen; a live step is
    next because it is the most recent thing that happened; a waiting reason is
    last and is what makes a finished-but-unverified run explain itself.
    """

    if row.primary_action == ACTION_REVIEW_RISK:
        return _session_action_words(ACTION_REVIEW_RISK, english)
    if row.current_step:
        return row.current_step
    return _waiting_reason_text(row.waiting_reason, english)


# What a row shows for a fact that has no canonical source. ``workspace_mode`` is
# the one that matters: no persisted or configuration field of that name exists in
# the product, so a row must neither invent a value nor refuse to draw itself.
UNKNOWN_FIELD = "\u2014"


def _elapsed_text(seconds: float) -> str:
    """How long a run has been going, short enough for a row.

    Digits and unit letters only: a duration is the same fact in either language,
    and putting it through the catalogs would buy nothing but two more entries to
    keep in step.
    """

    total = max(int(seconds), 0)
    if total < 60:
        return f"{total}s"
    minutes, remaining = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{remaining:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _identity_fields(row: SessionSummary) -> List[str]:
    """Which run this is: task, model, access profile, workspace mode.

    Only what a publisher actually reported. ``workspace_mode`` is rendered as a
    neutral dash when nothing reported it, because a plausible default renders
    identically to a fact and nothing on the screen would distinguish the two.
    """

    fields: List[str] = []
    if row.title:
        fields.append(row.title if len(row.title) <= 48 else row.title[:47] + "\u2026")
    model = "/".join(part for part in (row.provider, row.model) if part)
    if model:
        fields.append(model)
    if row.access_profile:
        fields.append(row.access_profile)
    fields.append(row.workspace_mode or UNKNOWN_FIELD)
    return fields


def _measurement_fields(row: SessionSummary, english: bool) -> List[str]:
    """What the run has spent, and only where somebody measured it.

    A zero is not published as a measurement: "this run cost nothing" and "nobody
    counted" are different claims, and the second one must not look like the
    first.
    """

    fields: List[str] = []
    if row.changed_files:
        label = "files" if english else "файлов"
        fields.append(f"{label}={row.changed_files}")
    if row.elapsed_seconds > 0:
        fields.append(_elapsed_text(row.elapsed_seconds))
    if row.token_budget.used > 0:
        label = "tokens" if english else "токенов"
        fields.append(f"{label}={int(row.token_budget.used)}")
    if row.cost_budget.used > 0:
        label = "cost" if english else "стоимость"
        amount = f"{row.cost_budget.used:.4f}".rstrip("0").rstrip(".")
        unit = f" {row.cost_budget.unit}" if row.cost_budget.unit else ""
        fields.append(f"{label}={amount}{unit}")
    return fields


def _session_row_text(
    row: SessionSummary, english: bool, *, verbose: bool = False
) -> str:
    """One technical session line, rendered from a typed view model only.

    This is the ``/sessions`` live block and the technical detail surfaces. It
    is *not* the Session Browser: C gave the browser its own contract in
    :func:`_session_browser_row_text`, because a chooser and a record of what
    happened want opposite things from the same facts, and one renderer asked
    to be both serves neither.

    What the two still share is the source. Both read one ``SessionSummary``
    and both spell a status, an action and a waiting reason out of the same
    catalogs, so a run cannot be "Stopped" in one surface and "Paused" in the
    other. Only the selection and the ordering of fields differ.

    ``verbose`` selects the wide form: identity, measurements and both of step
    and waiting reason. It is kept for the detail surfaces that pin it.

    Built entirely from the catalogs above, so the same row renders in either
    language and a test can assert on it without matching prose. Nothing here
    parses ``session.json`` or ``provider_history``: if a fact is not in the
    view model, it does not reach the row.

    ``last_event_summary`` is the reason the event summary catalog exists. The
    publishers send stable identifiers such as ``agent_run_stopped``, which is
    exactly what a row must not show verbatim, and the words for it live in
    :data:`_EVENT_SUMMARY_TEXT` in both languages.
    """

    fields = [row.session_id]
    if verbose:
        fields.extend(_identity_fields(row))
    fields.append(_session_status_words(row.status, english))
    if verbose:
        # Both facts, not the better of the two. `_session_detail_words` picks
        # one on purpose because a status *bar* has room for one, while a browser
        # row is where a person decides what to do next and needs to see that a
        # run is on `apply_patch` *and* waiting on a confirmation.
        if row.current_step:
            fields.append(row.current_step)
        reason = _waiting_reason_text(row.waiting_reason, english)
        if reason:
            fields.append(reason)
    else:
        detail = _session_detail_words(row, english)
        if detail:
            fields.append(detail)
    if row.last_event_summary:
        fields.append(_event_summary_text(row.last_event_summary, english))
    if verbose:
        fields.extend(_measurement_fields(row, english))
    if row.error_count:
        label = "errors" if english else "ошибок"
        fields.append(f"{label}={row.error_count}")
    fields.append(_session_action_words(row.primary_action, english))
    return " • ".join(fields)


# ------------------------------------------------- C. compact browser rows
#
# A separate presentation contract, deliberately beside `_session_row_text`
# rather than a third mode inside it. The verbose form is what `/sessions`
# publishes and what several tests pin; widening that function's job to also
# mean "but smaller" is how one renderer ends up serving two products badly.
#
# This layer is pure: it reads a `SessionSummary`, holds no state, reduces
# nothing, and reuses the existing catalogs so a status cannot mean one thing
# here and another in the status bar.

# How wide a terminal has to be before a field earns its place. Fields are
# dropped whole and in a fixed order, because half a model name is worse than
# no model name: `openai/claude-op...` cannot be acted on.
# Measured against the *content* width a row actually gets, which after the
# dialog max-width, the border, the padding and the scrollbar is well short of
# the terminal. 88 is what a 120-column terminal leaves once the browser stops
# stretching, so the model appears there and nowhere narrower.
BROWSER_WIDE = 88
BROWSER_STANDARD = 64

# The fewest columns worth giving a task before an optional field is dropped
# instead. Below this the headline stops identifying anything and the row is
# just decoration with a status on the end.
TASK_MIN_COLUMNS = 16

# What a row may not become. A task is a person's sentence, and a person's
# sentence can contain newlines, escape sequences and Rich markup.
_ROW_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _browser_task_text(row: SessionSummary, english: bool, budget: int) -> str:
    """The row's headline: the task, or a short stand-in for it.

    Control characters are stripped rather than escaped. A task carrying a
    newline would otherwise draw a second visual line inside a one-line row and
    push every following row down; an ANSI sequence would repaint the terminal.
    ``markup=False`` on the widget covers Rich markup, and this covers the rest.

    With no task the row falls back to a short suffix of the session id. The
    full id is an internal identifier that means nothing to a person choosing a
    session, and Session Detail is where it belongs.
    """

    title = _ROW_CONTROL.sub(" ", row.title or "").strip()
    title = " ".join(title.split())
    if not title:
        # A short id is shown whole. Slicing the tail off `s-summary` gave
        # "ummary" -- a truncated word that reads as a name and is not one.
        # A long minted id (`task-<epoch>-<suffix>`) keeps its final segment,
        # which is the part that distinguishes it.
        identifier = row.session_id or ""
        if len(identifier) <= 14:
            suffix = identifier or "?"
        else:
            suffix = identifier.rsplit("-", 1)[-1] or identifier[-8:]
        title = f"Session {suffix}" if english else f"\u0421\u0435\u0441\u0441\u0438\u044f {suffix}"
    if budget > 1 and len(title) > budget:
        title = title[: budget - 1] + "\u2026"
    return title


def _browser_activity_kind(step: Any) -> str:
    """What a live step means, or nothing at all.

    Deliberately not :func:`_activity_kind_for_tool`. That function is fail-soft
    towards ``ACTIVITY_WORKING`` because the activity line always has to say
    something -- it is the only indicator ordinary mode has. A browser row is
    under no such obligation: resolving every uncatalogued tool to "Working"
    would put the same word beside every running session, which distinguishes
    nothing and costs a column that the task could have used.

    So the lookup is the same catalog and the same spelling rules as A2, and
    only the fallback differs. Aliases matter here: the model is shown
    ``repo_edit_file`` while the audit log records ``repo.edit_file``, and
    ``current_step`` can carry either.
    """

    return _TOOL_ACTIVITY_KINDS.get(_canonical_tool_name(_identifier(step)), "")


def _browser_activity_text(row: SessionSummary, english: bool) -> str:
    """What the run is doing or waiting on, in words, or nothing.

    Priority is the product's, not this function's: a pending confirmation is
    the only state waiting on the reader, so it wins; then a waiting reason,
    which explains a stopped run; and a live step only if it can be said
    humanly.

    A raw ``current_step`` such as ``repo_edit_file`` is *omitted* rather than
    shown. The browser exists to choose a session, and a tool identifier does
    not help choose -- it only leaks vocabulary the user never agreed to learn.
    An unclassified step therefore contributes nothing: silence is a smaller
    lie than a word the reader cannot act on.
    """

    if row.primary_action == ACTION_REVIEW_RISK:
        return _session_action_words(ACTION_REVIEW_RISK, english)
    reason = _waiting_reason_text(row.waiting_reason, english)
    if reason and reason != row.waiting_reason:
        # Translated: safe to show. An untranslated identifier is not.
        return reason
    kind = _browser_activity_kind(row.current_step)
    if kind:
        russian, plain_english = _ACTIVITY_WORDS[kind]
        return plain_english if english else russian
    return ""


def _session_browser_fields(
    row: SessionSummary, english: bool, width: int
) -> List[str]:
    """The fields one browser row shows at this width, already in order.

    Everything a person needs to choose a session, and nothing they would need
    only after choosing it. No tokens, no cost, no budgets, no access profile,
    no workspace mode, no error count, no per-row action: those live in Session
    Detail, and repeating them here is what turned the browser into a report.
    """

    # The task gets whatever the terminal can spare, so a narrow window loses
    # the tail of a long sentence rather than the status that follows it.
    # The tail is built first and the task is given what is left, because the
    # task is the only elastic field: it can be shortened and still identify
    # the session, while half a status word identifies nothing. A fixed task
    # budget was the earlier bug -- 48 columns fitted in English and pushed a
    # long Russian row past 80 into a wrap.
    tail = [_session_status_words(row.status, english)]
    activity = _browser_activity_text(row, english)
    if activity:
        tail.append(activity)
    if width >= BROWSER_STANDARD and row.elapsed_seconds > 0:
        tail.append(_elapsed_text(row.elapsed_seconds))
    if width >= BROWSER_WIDE:
        model = "/".join(part for part in (row.provider, row.model) if part)
        if model:
            tail.append(model)

    def spent(fields: List[str]) -> int:
        return sum(len(field) for field in fields) + 3 * len(fields)

    # Optional fields are dropped whole, from the least important end, until
    # the task has a readable share. The status is never dropped.
    while len(tail) > 1 and width - spent(tail) < TASK_MIN_COLUMNS:
        tail.pop()
    return [_browser_task_text(row, english, max(width - spent(tail), 1)), *tail]


def _session_browser_row_text(
    row: SessionSummary, english: bool, width: int
) -> str:
    return " \u00b7 ".join(_session_browser_fields(row, english, width))


def _session_browser_footer(
    row: Optional[SessionSummary],
    english: bool,
    width: int = 0,
    *,
    shown: int = 0,
    hidden: int = 0,
) -> str:
    """One footer line, assembled to fit the width it is given.

    A browser that repeats Stop/Resume/Open on every line is a list of buttons
    rather than a list of sessions. The action still comes from the store, so
    this changes where it is shown and never what it is.

    Priority, and fields are dropped whole from the bottom of it: the selected
    row's action, then what the list is not showing, then the Esc hint. Two
    rules follow from the fact that this has to stay on one line. An action is
    never half-printed, because ``Enter: \u043f\u0440\u043e\u0434\u043e\u043b\u0436\u2026`` asks the reader to guess what
    key does what. And the Esc hint is the first thing to go, because Escape
    keeps working whether or not the footer advertises it, while the count of
    hidden sessions exists nowhere else on the screen.

    That last point is why the overflow field has a short form. Dropping it on
    a narrow terminal would leave the browser silently claiming to show every
    session it has, which is the one thing this footer must not do.
    """

    variants: List[Tuple[str, ...]] = []
    if row is not None:
        spellings = _session_action_spellings(row.primary_action, english)
        variants.append(
            tuple(
                [f"Enter: {spellings[0]}"]
                + [spelling for spelling in spellings]
            )
        )
    if hidden > 0:
        variants.append(
            (
                f"showing latest {shown} \u00b7 {hidden} older hidden"
                if english
                else f"\u043f\u043e\u043a\u0430\u0437\u0430\u043d\u044b {shown} \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0445 \u00b7 {hidden} \u0441\u0442\u0430\u0440\u044b\u0445 \u0441\u043a\u0440\u044b\u0442\u043e",
                f"+{hidden} older" if english else f"+{hidden} \u0441\u0442\u0430\u0440\u044b\u0445",
            )
        )
    variants.append(("Esc: close" if english else "Esc: \u0437\u0430\u043a\u0440\u044b\u0442\u044c",))

    if width <= 0:
        return " \u00b7 ".join(field[0] for field in variants)

    text = ""
    for field in variants:
        chosen = ""
        for spelling in field:
            candidate = f"{text} \u00b7 {spelling}" if text else spelling
            if len(candidate) <= width:
                chosen = candidate
                break
        if not chosen:
            # Lower-priority fields are not tried once one has been dropped:
            # a footer reading "Enter: stop \u00b7 Esc: close" while twelve sessions
            # are hidden spends its last columns on the least useful fact.
            break
        text = chosen
    if text:
        return text
    # Narrower than even the shortest whole spelling of the highest-priority
    # field. Return it anyway, whole. Truncating it would print `подтверж…`
    # and ask the reader to guess what Enter is about to do, and the layout
    # minimum is what actually keeps this branch unreachable in production:
    # the dialog gives the footer forty columns at the narrowest supported
    # terminal, against a longest short action of ten.
    return variants[0][-1]


# ---------------------------------------------------------------- detail view
#
# Everything below renders SessionDetail. The same rule as above applies: the
# view models carry identifiers and numbers, the words live here, and an
# identifier nobody has translated yet is shown verbatim rather than dropped.

# Which event family a timeline row belongs to. Kept in sync with EventKind by a
# contract test rather than by hope.
_EVENT_KIND_TEXT: Dict[str, Tuple[str, str]] = {
    "agent_action": ("шаг агента", "agent step"),
    "tool_call": ("инструмент", "tool call"),
    "browser_action": ("браузер", "browser"),
    "session_state": ("состояние", "state"),
    "connection_state": ("подключение", "connection"),
    "risk_decision": ("проверка риска", "risk decision"),
    "confirmation": ("подтверждение", "confirmation"),
    "performance_span": ("замер", "span"),
    "health_change": ("здоровье", "health"),
    "evidence": ("доказательство", "evidence"),
    "error": ("ошибка", "error"),
}

_EVENT_LEVEL_TEXT: Dict[str, Tuple[str, str]] = {
    "info": ("инфо", "info"),
    "warning": ("предупреждение", "warning"),
    "error": ("ошибка", "error"),
}

# Section headings, and the words for the facts inside them.
_DETAIL_TEXT: Dict[str, Tuple[str, str]] = {
    "timeline": ("Хронология", "Timeline"),
    "tools": ("Инструменты", "Tool calls"),
    "risk": ("Smart Stop", "Smart Stop"),
    "errors": ("Ошибки", "Errors"),
    "usage": ("Расход", "Usage"),
    "workspace": ("Рабочая копия", "Workspace"),
    "browser": ("Браузер", "Browser"),
    "evidence": ("Доказательства", "Evidence"),
    "performance": ("Производительность", "Performance"),
    "empty": ("нет данных", "no data"),
    "running": ("выполняется", "running"),
    "ok": ("успех", "ok"),
    "failed": ("ошибка", "failed"),
    "risk_level": ("уровень риска", "risk level"),
    "action_digest": ("действие", "action"),
    "reasons": ("причины", "reasons"),
    "awaiting": ("ожидает подтверждения", "awaiting confirmation"),
    "allowed": ("разрешено", "allowed"),
    "blocked": ("заблокировано", "blocked"),
    "dropped": ("события потеряны", "events dropped"),
    "truncated": ("строк не показано", "entries not shown"),
    "cost": ("стоимость", "cost"),
    "limit": ("лимит", "limit"),
    "spent": ("израсходовано", "spent"),
    "no_risk": (
        "Подтверждение не требуется.",
        "No confirmation is pending.",
    ),
    # D. The overview-first vocabulary.
    "overview": ("Обзор", "Overview"),
    "attention": ("Требует внимания", "Needs attention"),
    "progress": ("Изменения и проверки", "Changes and checks"),
    "diagnostics": ("Диагностика", "Diagnostics"),
    "needs_confirmation": (
        "Требуется ваше подтверждение.",
        "Your confirmation is required.",
    ),
    # Three outcomes, and the third is the default. "Not confirmed" is not a
    # softer way of saying failed: it says nobody published a result, which is
    # the honest answer far more often than either of the other two.
    "checks_passed": ("Проверки пройдены.", "Checks passed."),
    "checks_failed": ("Проверки не пройдены.", "Checks failed."),
    "checks_unknown": (
        "Проверка не подтверждена.",
        "Verification is not confirmed.",
    ),
    "changed_files": ("изменено файлов", "files changed"),
    "no_changes": ("Изменений пока нет.", "No changes yet."),
    "has_diff": ("есть diff", "a diff was recorded"),
    "details_below": (
        "Технические подробности ниже.",
        "Technical details are below.",
    ),
    "no_live_activity": (
        "Живой активности пока нет: сессия восстановлена из записи.",
        "No live activity yet: this session was restored from a record.",
    ),
    "next_step": ("дальше", "next"),
    "errors_count": ("ошибок", "errors"),
    "evidence_count": ("доказательств", "evidence records"),
}


def _detail_words(name: str, english: bool) -> str:
    return _catalog_text(_DETAIL_TEXT, name, english)


def _event_kind_words(kind: str, english: bool) -> str:
    return _catalog_text(_EVENT_KIND_TEXT, kind, english)


def _event_level_words(level: str, english: bool) -> str:
    return _catalog_text(_EVENT_LEVEL_TEXT, level, english)


def _clock_text(timestamp: float) -> str:
    """A wall-clock time for one row, or nothing when none was recorded.

    Local time and seconds only: a timeline is read against "what happened just
    now", and a date on every row would cost width without answering that. A
    zero timestamp means no publisher recorded one, which is not 1970.
    """

    if timestamp <= 0:
        return ""
    try:
        return time.strftime("%H:%M:%S", time.localtime(timestamp))
    except (OSError, OverflowError, ValueError):
        return ""


def _duration_text(milliseconds: Optional[float]) -> str:
    """A measured duration, in the coarsest unit that stays readable."""

    if milliseconds is None or milliseconds < 0:
        return ""
    if milliseconds < 1000:
        return f"{int(milliseconds)}ms"
    return _elapsed_text(milliseconds / 1000.0)


def _timeline_entry_text(entry: TimelineEntry, english: bool) -> str:
    """One timeline row, from a typed entry and the catalogs only.

    ``TimelineEntry.data`` is already an explicit per-kind allowlist built by the
    reducer, so the whole mapping can be shown: there is no payload here that
    nobody asked for. It is still rendered key by key rather than as a ``dict``
    repr, because a repr is how a field added later reaches a screen unreviewed.
    """

    fields: List[str] = [f"#{entry.seq}"]
    clock = _clock_text(entry.timestamp)
    if clock:
        fields.append(clock)
    fields.append(_event_kind_words(entry.kind, english))
    # Only worth saying when it is not the ordinary case.
    if entry.level and entry.level != "info":
        fields.append(_event_level_words(entry.level, english))
    if entry.summary:
        fields.append(_event_summary_text(entry.summary, english))
    for key, value in entry.data.items():
        if key == "duration_ms":
            # ``bool`` is an ``int`` in Python, and a duration of ``True`` is not
            # a duration, so the flag case is excluded explicitly.
            measured = (
                float(value)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else None
            )
            duration = _duration_text(measured)
            if duration:
                fields.append(duration)
            continue
        if isinstance(value, bool):
            fields.append(f"{key}={'yes' if value else 'no'}")
        else:
            fields.append(f"{key}={value}")
    return " · ".join(fields)


def _tool_call_text(call: ToolCallView, english: bool) -> str:
    """One tool call: what ran, whether it finished, and how long it took."""

    fields: List[str] = [call.name or call.call_id]
    if call.running:
        fields.append(_detail_words("running", english))
    elif call.ok is None:
        # Finished, but no publisher said whether it succeeded. Reporting "ok"
        # here would be an invention.
        fields.append(UNKNOWN_FIELD)
    else:
        fields.append(_detail_words("ok" if call.ok else "failed", english))
    clock = _clock_text(call.started_at)
    if clock:
        fields.append(clock)
    duration = _duration_text(call.duration_ms)
    if duration:
        fields.append(duration)
    if call.detail:
        fields.append(call.detail)
    return " · ".join(fields)


def _usage_lines(row: SessionSummary, detail: SessionDetail, english: bool) -> List[str]:
    """Measured tokens, money and budgets -- and nothing when unmeasured.

    A zero is never printed for an absent measurement: "this run was free" and
    "nobody counted" are different claims, and the second must not look like the
    first.
    """

    lines: List[str] = []
    for name, value in sorted(detail.usage.items()):
        lines.append(f"{name}: {int(value) if float(value).is_integer() else value}")
    for currency, amount in sorted(detail.costs.items()):
        lines.append(f"{_detail_words('cost', english)} {currency}: {amount:g}")
    for label, budget in (
        ("tokens" if english else "токены", row.token_budget),
        (_detail_words("cost", english), row.cost_budget),
    ):
        if budget.used <= 0 and budget.limit is None:
            continue
        text = f"{label}: {_detail_words('spent', english)} {budget.used:g}"
        if budget.limit is not None:
            text += f" / {_detail_words('limit', english)} {budget.limit:g}"
        lines.append(text)
    return lines


def _mapping_lines(values: Mapping[str, Any], limit: int = 12) -> List[str]:
    """Named fields of an already-bounded view mapping, one per line.

    Used for the git, diff and browser blocks. The reducer copied only allowed
    keys into these, so the mapping is safe to show -- key by key, never as a
    repr, and never more lines than a block can hold.
    """

    lines: List[str] = []
    for key, value in list(values.items())[:limit]:
        if isinstance(value, bool):
            lines.append(f"{key}: {'yes' if value else 'no'}")
        else:
            lines.append(f"{key}: {value}")
    return lines


def _evidence_lines(items: Sequence[Mapping[str, Any]], english: bool) -> List[str]:
    lines: List[str] = []
    for item in items:
        parts = [
            str(item[key])
            for key in ("kind", "evidence_id", "summary")
            if item.get(key)
        ]
        if parts:
            lines.append(" · ".join(parts))
    return lines


def _performance_lines(
    spans: Mapping[str, Mapping[str, float]], english: bool
) -> List[str]:
    lines: List[str] = []
    for component, values in spans.items():
        count = int(values.get("count", 0))
        total = _duration_text(values.get("total_ms"))
        worst = _duration_text(values.get("max_ms"))
        lines.append(f"{component}: {count} · {total} · max {worst}")
    return lines


def _risk_lines(risk: Optional[RiskStateView], english: bool) -> List[str]:
    """Describe a risk verdict without carrying anything secret.

    Named fields only. The ledger keeps confirmation tokens out of events by
    construction and this layer has no key for one, so there is nothing here to
    leak; assembling the text field by field is what keeps that true if a
    publisher ever changes. There is deliberately no Approve control: approving
    must go through the ledger, and a button that only looked like it did would
    be worse than no button.
    """

    if risk is None:
        return [_detail_words("no_risk", english)]
    lines = [
        f"{_detail_words('risk_level', english)}: {risk.level or UNKNOWN_FIELD}",
        f"{_detail_words('allowed' if risk.allowed else 'blocked', english)}",
    ]
    if risk.reason:
        lines.append(_waiting_reason_text(risk.reason, english))
    if risk.action_digest:
        lines.append(
            f"{_detail_words('action_digest', english)}: {risk.action_digest[:16]}"
        )
    if risk.reasons:
        lines.append(f"{_detail_words('reasons', english)}: " + "; ".join(risk.reasons))
    if risk.awaiting_confirmation:
        lines.append(_detail_words("awaiting", english))
    return lines


def _detail_header_lines(detail: SessionDetail, english: bool) -> List[str]:
    """Who this session is, above the sections.

    The first line is `_session_row_text` verbatim, in its verbose form. Detail
    is a technical surface, so it wants the technical renderer: identity,
    measurements, step and waiting reason all at once.

    It is deliberately *wider* than the browser row the user pressed Enter on,
    and that is the point of opening it. The two cannot contradict each other
    because both are rendered from the same ``SessionSummary`` and the same
    catalogs; Detail simply keeps the fields the browser dropped.
    """

    row = detail.summary
    lines = [_session_row_text(row, english, verbose=True)]
    fields: List[str] = []
    if row.agent:
        fields.append(f"agent: {row.agent}")
    if row.source:
        fields.append(f"source: {row.source}")
    if row.last_event_seq:
        fields.append(f"seq: {row.last_event_seq}")
    if fields:
        lines.append(" · ".join(fields))
    return lines


# ------------------------------------------------- D. overview-first detail
#
# A separate presentation contract over the same `SessionDetail`, for the same
# reason C gave the browser one: the technical renderer above answers "what
# exactly happened", and these answer "what is this, and does it need me".
#
# The screen used to open on `_session_row_text(verbose=True)` -- an internal
# session id, an access profile and a raw tool name, before any sentence a
# person could act on. Nothing is deleted here; the identity line simply moved
# to the bottom, into diagnostics, where somebody debugging will look for it.
#
# Pure: reads one `SessionDetail`, holds no state, reduces nothing, and reuses
# the catalogs so a status cannot mean one thing here and another in the
# browser row the user pressed Enter on.

# How much of a task headline the detail screen shows. Wider than a browser row
# because there is no status column competing for the same line.
_DETAIL_TASK_COLUMNS = 72


def _detail_check_outcome(detail: SessionDetail) -> Optional[bool]:
    """Whether a verification actually reported a result, and which.

    ``True`` passed, ``False`` failed, ``None`` nobody said. The third case is
    the common one and must not collapse into either of the others.

    Only a *finished* tool call belonging to the testing family, carrying an
    explicit ``ok``, counts. The name selects which call is a verification; the
    published ``ok`` supplies the verdict. Neither is inferred from the other,
    and none of the tempting proxies are used: a run without an exception, a
    diff that exists, a command whose name contains "test", or a call that
    started. Starting a test is not passing it, and a screen that says
    otherwise is worse than one that says nothing.
    """

    outcome: Optional[bool] = None
    for call in detail.tool_calls:
        kind = _TOOL_ACTIVITY_KINDS.get(_canonical_tool_name(_identifier(call.name)))
        if kind != ACTIVITY_TESTING or call.running or call.ok is None:
            continue
        outcome = bool(call.ok)
    return outcome


def _detail_overview_lines(detail: SessionDetail, english: bool) -> List[str]:
    """What this session is and what is happening to it, in that order.

    Only facts the view model actually carries. No access profile, no workspace
    mode, no budgets, no raw step, no identifiers -- every one of those is a
    thing a person needs *after* choosing to look, and every one of them is
    still on this screen further down.
    """

    row = detail.summary
    lines = [_browser_task_text(row, english, _DETAIL_TASK_COLUMNS)]

    facts = [_session_status_words(row.status, english)]
    activity = _browser_activity_text(row, english)
    if activity:
        facts.append(activity)
    if row.elapsed_seconds > 0:
        facts.append(_elapsed_text(row.elapsed_seconds))
    model = "/".join(part for part in (row.provider, row.model) if part)
    if model:
        # Whole or not at all: half a model name cannot be acted on.
        facts.append(model)
    lines.append(" \u00b7 ".join(facts))

    if row.changed_files:
        lines.append(
            f"{_detail_words('changed_files', english)}: {row.changed_files}"
        )
    action = _session_action_words(row.primary_action, english)
    if action:
        lines.append(f"{_detail_words('next_step', english)}: {action}")
    return lines


def _detail_attention_lines(detail: SessionDetail, english: bool) -> List[str]:
    """Whatever is waiting on the reader, or nothing at all.

    Nothing at all is the point. The screen used to carry a permanent "No
    confirmation is pending" card, which trains a reader to skip the exact
    region that will one day matter. An absent problem is better said by an
    absent block.

    One state is reported, not a digest of all of them, in the order a person
    would act: a confirmation is blocking the agent right now; a failure has
    already happened; a verification came back negative; a run is waiting on
    something explainable.
    """

    row = detail.summary
    risk = detail.pending_confirmation or row.risk
    if risk is not None and risk.awaiting_confirmation:
        lines = [_detail_words("needs_confirmation", english)]
        if risk.level:
            lines.append(f"{_detail_words('risk_level', english)}: {risk.level}")
        if risk.reasons:
            lines.append(
                f"{_detail_words('reasons', english)}: " + "; ".join(risk.reasons)
            )
        # Deliberately no action digest and no token. The digest is a hash a
        # person cannot check and the token is a credential; both belong to the
        # ledger, and the reasons above are what the decision is actually made
        # on.
        return lines

    if detail.errors:
        latest = detail.errors[-1]
        lines = [_event_summary_text(latest.summary, english)] if latest.summary else []
        code = _identifier(latest.data.get("code"))
        if code:
            lines.append(code)
        if lines:
            lines.append(_detail_words("details_below", english))
            return lines

    if _detail_check_outcome(detail) is False:
        return [
            _detail_words("checks_failed", english),
            _detail_words("details_below", english),
        ]

    reason = _waiting_reason_text(row.waiting_reason, english)
    if reason and reason != row.waiting_reason:
        # Translated only. An untranslated identifier is a leak, not a warning.
        return [reason]
    return []


def _detail_progress_lines(detail: SessionDetail, english: bool) -> List[str]:
    """What the run changed and whether anything proved it.

    Above the timeline because "did it work" is a question, and a timeline is
    an answer only to somebody willing to read sixty rows to find out.
    """

    row = detail.summary
    lines: List[str] = []
    if row.changed_files:
        lines.append(
            f"{_detail_words('changed_files', english)}: {row.changed_files}"
        )
    elif not detail.diff:
        lines.append(_detail_words("no_changes", english))
    if detail.diff:
        lines.append(_detail_words("has_diff", english))

    outcome = _detail_check_outcome(detail)
    if outcome is True:
        lines.append(_detail_words("checks_passed", english))
    elif outcome is False:
        lines.append(_detail_words("checks_failed", english))
    else:
        lines.append(_detail_words("checks_unknown", english))

    errors = row.error_count or len(detail.errors)
    if errors:
        lines.append(f"{_detail_words('errors_count', english)}: {errors}")
    if detail.evidence:
        lines.append(
            f"{_detail_words('evidence_count', english)}: {len(detail.evidence)}"
        )
    if not detail.timeline and row.last_event_seq <= 0:
        # Restored from a durable record and never seen live. Saying so is what
        # stops the empty technical sections below reading as measurements.
        lines.append(_detail_words("no_live_activity", english))
    return lines


def _detail_footer_text(english: bool, width: int = 0) -> str:
    """One footer line for Session Detail, assembled to fit.

    Same policy as the browser's: whole hints, dropped in priority order, never
    a second line. Esc is last to be *kept* rather than first to go, because it
    is the only one of the three a reader cannot guess from the scrollbar.
    """

    fields = [
        "Esc: back" if english else "Esc: \u043d\u0430\u0437\u0430\u0434",
        "\u2191\u2193 scroll" if english else "\u2191\u2193 \u043f\u0440\u043e\u043a\u0440\u0443\u0442\u043a\u0430",
        "PgUp/PgDn pages" if english else "PgUp/PgDn \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u044b",
    ]
    # Read left to right in the order a person expects, but dropped from the
    # least important end.
    order = [fields[1], fields[2], fields[0]]
    keep = list(order)
    while len(keep) > 1:
        text = " \u00b7 ".join(keep)
        if width <= 0 or len(text) <= width:
            return text
        # Drop the lowest-priority field that is still present.
        for candidate in (fields[2], fields[1]):
            if candidate in keep:
                keep.remove(candidate)
                break
        else:  # pragma: no cover - defensive
            break
    return keep[0] if keep else ""


class SessionAction(NamedTuple):
    """What a person chose to do about one session in the browser.

    A stable action identifier rather than a callback, so the row, the test and
    the application all name the same four outcomes and the screen decides
    nothing about how they are carried out.
    """

    session_id: str
    action: str


# The one header line, and the widths at which fields stop fitting.
#
# Five equal columns were the old shell. At an 80-column window each got 16
# cells, so "контекст: лимит" filled its column exactly and ran into the next
# field, and a model name was cut to "openai/m" -- which reads as a *different*
# model to the person checking which one is selected. One line with explicit
# breakpoints replaces them: when something does not fit it is dropped whole,
# never abbreviated into a plausible lie.
HEADER_WIDE_COLUMNS = 76
HEADER_MEDIUM_COLUMNS = 52

# Context occupancy is not shown until it matters. Below this it is noise on
# every single redraw; above it, it is the one thing that explains a truncated
# answer, so it earns its place.
CONTEXT_WARNING_FRACTION = 0.75


def _header_line(
    *,
    repository: str,
    model: str,
    activity: str,
    width: int,
    effort: str = "auto",
    mode: str = DEFAULT_MODE,
    economy: bool = False,
    context_note: str = "",
) -> str:
    """Render the run state with the model as the primary fact.

    A coding-agent header is not branding. The first thing a person needs to know
    is which model will answer the next prompt, then its reasoning level, then
    the project, then what the agent is doing right now. Fields disappear whole
    -- never cut into a different-looking value -- from the right as the width
    shrinks; the model itself is never dropped in favour of product chrome.
    """

    model_id = model.split("/", 1)[-1] if "/" in model else model
    effort_text = f"effort {effort or 'auto'}"
    repo_text = f"@{repository}" if repository else ""
    economy_text = "Economy" if economy else ""
    # Build renders nothing: like Economy, the header spends width on
    # divergence from the default, not on restating it.
    try:
        normalized_mode = normalize_mode(mode) if mode else DEFAULT_MODE
    except ModeError:
        normalized_mode = DEFAULT_MODE
    mode_text = (
        mode_display_name(normalized_mode) if normalized_mode != DEFAULT_MODE else ""
    )
    if width < HEADER_MEDIUM_COLUMNS:
        # Smallest tier: the agent state outranks the project name, and the
        # provider prefix gives way to the model id. Dropping a whole field is
        # what keeps "openai…" from reading as a different model.
        candidates = [model_id, activity, mode_text, effort_text, repo_text]
    elif width < HEADER_WIDE_COLUMNS:
        candidates = [model, mode_text, effort_text, repo_text, activity]
    else:
        candidates = [model, mode_text, effort_text, repo_text, activity, economy_text, context_note]
    kept = [field for field in candidates if field]
    while len(kept) > 1 and len(" · ".join(kept)) > width:
        kept.pop()
    line = " · ".join(kept)
    if len(line) <= width:
        return line
    return line[: max(1, width - 1)] + "…"


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
class ProviderEdit:
    """A saved provider, loaded for editing. Never carries the secret.

    B5.1. The form needs the record's non-secret values and its identity. It
    deliberately does not receive the API key: the key is resolved by the
    controller at save time from the reference already on the record, so it has
    no reason to exist in a widget, a snapshot, or a traceback.

    ``has_secret`` is the only thing the form learns about the credential --
    enough to say "saved" and to treat an empty replacement field as "keep it".
    """

    provider_id: str
    adapter: str
    base_url: str
    model_id: str = ""
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    has_secret: bool = False


def load_provider_edit(provider_id: str) -> Optional[ProviderEdit]:
    """Read one saved provider into an edit view model, or ``None``.

    Reads through the same controller the rest of the product writes with, so
    an edit form cannot be populated from a source that disagrees with what a
    save would update.
    """

    try:
        details = _provider_controller().details(provider_id)
    except Exception:
        return None
    provider = details.provider
    model = details.selected_model
    if model is None and len(details.models) == 1:
        model = details.models[0]
    return ProviderEdit(
        provider_id=provider.provider_id,
        adapter=provider.adapter_kind,
        base_url=provider.base_url,
        model_id=str(model.model_id) if model is not None else "",
        context_window=model.context_window if model is not None else None,
        max_output_tokens=model.max_output_tokens if model is not None else None,
        has_secret=bool(details.credential.get("configured")),
    )


@dataclass(frozen=True)
class DiscoveredModel:
    model_id: str
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    # Capability metadata keeps the registry's true/false/unknown contract.
    # "unknown" means the catalog did not say; discovery never invents a
    # capability from the adapter kind or the model name.
    display_name: Optional[str] = None
    tools: str = "unknown"
    vision: str = "unknown"
    structured_output: str = "unknown"
    streaming: str = "unknown"
    # USD per million tokens, straight from the provider catalog when it
    # publishes prices. None means the provider did not publish one.
    input_per_million: Optional[float] = None
    output_per_million: Optional[float] = None
    cache_read_per_million: Optional[float] = None
    # True/False only when both token prices were published; None is honest.
    free: Optional[bool] = None


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


def _provider_controller() -> ProviderController:
    return ProviderController(registry=_registry(), credentials=CredentialStore())


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
    """Compatibility wrapper around the shared project-aware discovery."""
    return discover_verification_commands(repository)


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


def _price_per_million(value: Any) -> Optional[float]:
    """A catalog's per-token price (OpenRouter style) as USD per million.

    Catalogs publish token prices as decimal strings ("0.000001") or numbers.
    Anything unparseable, negative, or non-finite is treated as unpublished
    rather than guessed.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return None
    return number * 1_000_000


def _capability_from_list(values: Any, *names: str) -> str:
    """true/false when the catalog published a capability list, else unknown.

    A published list is evidence in both directions: naming the capability is
    "true", omitting it from an existing list is "false". No list at all --
    an id-only catalog -- stays "unknown".
    """

    if not isinstance(values, (list, tuple)):
        return "unknown"
    published = {str(item).strip().lower() for item in values}
    return "true" if any(name in published for name in names) else "false"


def _discovered_metadata(raw: dict[str, Any], raw_id: str) -> dict[str, Any]:
    """Capability and pricing metadata one catalog entry actually published.

    ``raw_id`` is the identifier field the entry was keyed by: a catalog whose
    "name" *is* the id (Gemini) does not thereby publish a display name.
    """

    extra: dict[str, Any] = {}
    name = raw.get("displayName") or raw.get("display_name") or raw.get("name")
    if (
        isinstance(name, str)
        and name.strip()
        and name.strip() not in {raw_id, raw_id.removeprefix("models/")}
    ):
        extra["display_name"] = name.strip()[:200]
    parameters = raw.get("supported_parameters")
    extra["tools"] = _capability_from_list(parameters, "tools", "tool_choice")
    extra["structured_output"] = _capability_from_list(
        parameters, "structured_outputs", "response_format"
    )
    architecture = raw.get("architecture")
    modalities = (
        architecture.get("input_modalities")
        if isinstance(architecture, dict)
        else None
    )
    extra["vision"] = _capability_from_list(modalities, "image")
    pricing = raw.get("pricing")
    if isinstance(pricing, dict):
        prompt = _price_per_million(pricing.get("prompt"))
        completion = _price_per_million(pricing.get("completion"))
        extra["input_per_million"] = prompt
        extra["output_per_million"] = completion
        extra["cache_read_per_million"] = _price_per_million(
            pricing.get("input_cache_read")
        )
        if prompt is not None and completion is not None:
            extra["free"] = prompt == 0 and completion == 0
    return extra


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
        discovered.append(
            DiscoveredModel(
                model_id, context, output, **_discovered_metadata(raw, raw_id)
            )
        )
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
    # B2. The unclassified branch is the one that ends up on screen when a
    # provider raises something KaroX has no case for -- an SDK error, a proxy
    # error, an httpx error. Interpolating `str(error)` raw is how a key reaches
    # the terminal: several SDKs echo the Authorization header or the query
    # string in their message, and this is the last hop before a widget. The
    # classified branch above is safe because it uses `error.safe_message`; this
    # one had no such guarantee, so it goes through the same redaction the rest
    # of the product uses at its boundaries.
    return f"{heading}\n{checked} {base}\n{str(redact(str(error))).strip()}"


def _save_provider(setup: ProviderSetup, *, activate: bool = True) -> ModelRecord:
    provider_id = setup.provider_id.strip()
    model_id = setup.model_id.strip()
    base_url = setup.base_url.strip().rstrip("/")
    if not provider_id or not model_id or not base_url:
        raise ValueError("provider, base URL, and model are required")

    controller = _provider_controller()
    credential_ref: Optional[str] = None
    try:
        credential_ref = controller.details(provider_id).provider.credential_ref
    except Exception:
        pass
    is_local = base_url.startswith(("http://127.0.0.1", "http://localhost"))
    if not setup.api_key and credential_ref is None and not is_local:
        raise ValueError("an API key is required for a remote provider")

    mutation = controller.configure_provider_model(
        ProviderRecord(
            provider_id=provider_id,
            adapter_kind=setup.adapter,
            base_url=base_url,
            credential_ref=credential_ref,
            privacy_class="local" if is_local else "public",
        ),
        ModelRecord(
            provider_id=provider_id,
            model_id=model_id,
            aliases=(),
            context_window=setup.context_window,
            max_output_tokens=setup.max_output_tokens,
            tools="true",
            streaming="true",
            provenance="interactive-setup",
        ),
        secret=setup.api_key or None,
        activate=activate,
    )
    model = mutation.selected_model if activate else mutation.model
    if model is None:
        raise RuntimeError("provider setup did not return a saved model")
    return model


def _probe_provider(setup: ProviderSetup) -> Dict[str, Any]:
    """Perform one real request without persisting a newly entered API key."""

    provider_id = setup.provider_id.strip()
    base_url = setup.base_url.strip().rstrip("/")
    credential_ref: Optional[str] = None
    factory = ProviderFactory()

    if setup.api_key:
        # ProviderFactory accepts an injected CredentialStore, so the setup key
        # can be exercised without touching the OS keyring.  Persistence happens
        # only after the probe succeeds in ``_save_provider``.
        class _ProbeCredentialBackend:
            def __init__(self) -> None:
                self.value: Optional[str] = None

            def set(self, service: str, account: str, secret: str) -> None:
                del service, account
                self.value = secret

            def get(self, service: str, account: str) -> Optional[str]:
                del service, account
                return self.value

            def delete(self, service: str, account: str) -> None:
                del service, account
                self.value = None

        probe_credentials = CredentialStore(_ProbeCredentialBackend())
        credential_ref = probe_credentials.set(provider_id, setup.api_key)["reference"]
        factory = ProviderFactory(probe_credentials)
    else:
        try:
            credential_ref = _provider_controller().details(provider_id).provider.credential_ref
        except Exception:
            credential_ref = None

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
    response = factory.create(provider_record).complete(
        ModelRequest(
            model=setup.model_id.strip(),
            messages=(ModelMessage("user", "Reply with exactly OK."),),
            max_output_tokens=8,
            deadline_seconds=min(30.0, provider_record.timeout_seconds),
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
    *,
    run_cost_profile: str = "balanced",
    reasoning_effort: Optional[str] = None,
    effort_level: Optional[str] = None,
    agent_mode: Optional[str] = None,
    token_ceiling: Optional[int] = None,
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
    if reasoning_effort is None:
        stored_effort = _load_preferences().get("reasoning_effort")
        if stored_effort in REASONING_EFFORTS:
            reasoning_effort = str(stored_effort)
    if reasoning_effort is not None:
        if reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("unknown reasoning effort")
        argv.extend(("--effort", reasoning_effort))
    if effort_level is None:
        stored_level = _load_preferences().get("effort_level")
        if isinstance(stored_level, str) and stored_level.strip():
            try:
                effort_level = normalize_effort(stored_level)
            except ValueError:
                effort_level = None
    # AUTO is forwarded since the task-signal wiring landed: the CLI
    # resolves it at submission from the task text, the stored project map
    # (dependency breadth, churn, freshness), and the mode, then records the
    # chosen level plus reasons in the report. A run with no stored
    # preference at all still emits nothing and stays legacy bit for bit.
    if effort_level is not None:
        argv.extend(("--effort-level", normalize_effort(effort_level)))
    if agent_mode is None:
        stored_mode = _load_preferences().get("agent_mode")
        if isinstance(stored_mode, str) and stored_mode.strip():
            try:
                agent_mode = normalize_mode(stored_mode)
            except ModeError:
                agent_mode = None
    # Build deliberately emits nothing: it is the stance every run had before
    # modes existed, so a legacy argv stays bit for bit identical. The flag is
    # sent only when the run diverges from that default.
    if agent_mode is not None and normalize_mode(agent_mode) != DEFAULT_MODE:
        argv.extend(("--mode", normalize_mode(agent_mode)))
    if run_cost_profile not in {"balanced", "economy"}:
        raise ValueError("run cost profile must be balanced or economy")
    if run_cost_profile == "economy":
        argv.append("--economy")
        # Quality-neutral by contract: Economy keeps the selected model, Effort,
        # route order, context threshold, and tool-result ceiling unchanged.
        # Savings come from exact duplicate reuse and provider prompt caching.
        argv.extend(
            (
                "--route-strategy", "ordered",
                "--context-utilization", "0.6",
                "--max-tool-result-chars", "24000",
            )
        )
    token_limit = token_ceiling if token_ceiling is not None else None
    if token_limit is not None:
        if isinstance(token_limit, bool) or token_limit <= 0:
            raise ValueError("run token limit must be a positive integer")
        argv.extend(("--max-total-tokens", str(token_limit)))
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


def _persist_tui_saved_bridge_profile(
    repository: Path,
    setup: BridgeSetup,
    *,
    language: str,
) -> str:
    """Persist the TUI wizard result as the one canonical saved-bridge model.

    The TUI must not maintain a second process/CLI lifecycle.  Once this returns,
    Start/Repair, Restart, Stop, Delete and Advanced all operate on exactly the
    same ``SavedWebBridgeProfile`` through ``start_saved_bridge`` and friends.
    """
    if setup.profile not in WEB_BRIDGE_PROFILES:
        raise ValueError("saved hosted bridge requires a supported web profile")
    if setup.tunnel_provider not in {"cloudflare", "tailscale", "custom"}:
        raise ValueError("hosted bridges require cloudflare, tailscale, or custom tunnel")

    import hashlib

    from .hosted_tools_runtime import server_profiles_for_repository
    from .web_bridge_profiles import SavedWebBridgeProfile, WebBridgeProfileStore

    browser_capable = setup.profile in {"chatgpt-web", "claude-web", "hyperagent-web"}
    effective_tools = tuple(
        dict.fromkeys(
            (
                "karox.runtime.status",
                *setup.tools,
                *(("karox.browser.wait_for",) if browser_capable else ()),
            )
        )
    )
    needs_write = any(tool in MUTATING_WEB_TOOLS for tool in effective_tools)
    access_profile = (
        AccessProfile.WORKSPACE_WRITE if needs_write else AccessProfile.READ_ONLY
    )
    prefix = {
        "chatgpt-web": "chatgpt",
        "claude-web": "claude",
        "hyperagent-web": "hyperagent",
        "notion": "notion",
        "adapt": "adapt",
    }[setup.profile]
    repository_key = str(repository.resolve()).casefold().encode("utf-8")
    digest = hashlib.sha256(repository_key).hexdigest()[:10]
    canonical_name = f"{prefix}-auto-{digest}"
    verification_commands = (
        tuple(_default_verification(repository))
        if "karox.checks.run" in effective_tools
        else ()
    )
    server_profiles = (
        tuple(item.to_public_dict() for item in server_profiles_for_repository(repository))
        if any(
            tool in {"karox.dev_server.start", "karox.dev_server.stop"}
            for tool in effective_tools
        )
        else ()
    )
    from .web_bridge_profiles import WebBridgeProfileError

    store = WebBridgeProfileStore()
    candidates = (
        canonical_name,
        *(f"{canonical_name}-{item.value}" for item in AccessProfile),
    )
    existing_profiles: list[SavedWebBridgeProfile] = []
    for candidate in candidates:
        try:
            candidate_profile = store.get(candidate)
        except WebBridgeProfileError as exc:
            if "does not exist" not in str(exc):
                raise
            continue
        existing_profiles.append(candidate_profile)
    if len(existing_profiles) > 1:
        raise ValueError(
            "multiple legacy auto profiles exist for this service/repository; "
            "open /connect and delete the obsolete duplicate before re-running setup"
        )
    existing = existing_profiles[0] if existing_profiles else None
    profile_name = existing.name if existing is not None else canonical_name
    if existing is not None and existing.access_profile != access_profile:
        raise ValueError(
            "changing the permission level of an existing durable connection would rotate "
            "its connector identity; delete/recreate that connection from /connect instead"
        )

    if existing is not None:
        # The setup wizard intentionally exposes only connection-level choices.
        # Preserve browser/domain/keyring policy edited later in Advanced instead
        # of resetting it merely because the user re-ran setup.
        from dataclasses import replace as _replace

        profile = _replace(
            existing,
            repository=str(repository.resolve()),
            tools=effective_tools,
            verification_commands=verification_commands,
            server_profiles=server_profiles,
            tunnel=setup.tunnel_provider,
            public_url=setup.public_url,
            language=language,
            access_profile=access_profile,
            port=setup.port,
        )
    else:
        profile = SavedWebBridgeProfile(
            name=profile_name,
            target_profile=setup.profile,
            repository=str(repository.resolve()),
            tools=effective_tools,
            verification_commands=verification_commands,
            server_profiles=server_profiles,
            browser_external_https=browser_capable,
            browser_headed=browser_capable,
            browser_user_takeover=browser_capable,
            browser_network_inspection=browser_capable,
            browser_payment_confirmation=False,
            deadline_seconds=3600.0,
            tunnel=setup.tunnel_provider,
            public_url=setup.public_url,
            language=language,
            access_profile=access_profile,
            port=setup.port,
        )

    from .web_bridge_launcher import apply_saved_bridge_profile

    if existing is None:
        # Bootstrap has no prior identity/runtime to preserve.
        store.put(profile)
    else:
        apply_saved_bridge_profile(
            profile_name,
            profile,
            allow_restart=True,
        )
    return profile_name


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

        @on(Input.Submitted, "#preset-search")
        def preset_search_submitted(self, _event: Input.Submitted) -> None:
            self.action_choose()

        @on(OptionList.OptionSelected, "#provider-presets")
        def preset_selected(self, _event: OptionList.OptionSelected) -> None:
            self.action_choose()

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
            # B2. Advanced settings and the limits editor are both reachable
            # without a mouse. `f2` keeps `_open_limits` on a production path
            # after the button it used to own became the advanced toggle: a
            # working editor must not become unreachable because its entry point
            # was renamed.
            Binding("f2", "advanced", "Дополнительные настройки", priority=True),
            Binding("f3", "limits", "Лимиты", priority=True),
            Binding("f10", "save", "Проверить и сохранить", priority=True),
            Binding("escape", "cancel", "Отмена", priority=True),
        ]
        DEFAULT_CSS = """
        ProviderSetupScreen { align: center middle; background: #0e0c08 92%; }
        /* B2. The width was a fixed 82, so in a 46-column terminal the dialog
           was wider than the screen and the API key field ran off the edge --
           the one field the standard path exists to collect. `100%` with a
           `max-width` keeps the comfortable size on a real window and fits the
           small one.

           The height deliberately stays fixed with a percentage ceiling. That
           pair already handled both cases: 31 rows keeps the form compact on a
           tall terminal, and `94%` is what shrinks it on a short one. `height:
           auto` looks tidier and is wrong -- `#provider-fields` is `1fr`, so the
           dialog grows to fill whatever it is given and a connection form eats a
           42-row window. */
        #provider-dialog { width: 100%; max-width: 82; height: 31;
          max-height: 94%; background: #1a1712;
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

        # B2. Which fields the standard path keeps out of sight, and the ids the
        # advanced toggle reveals. Named here rather than inline so the CSS, the
        # toggle and the tests cannot drift about what "advanced" means.
        ADVANCED_FIELD_IDS: Tuple[str, ...] = (
            "#provider-context-label",
            "#provider-context",
            "#provider-output-label",
            "#provider-output",
        )

        def __init__(
            self,
            language: str = "ru",
            preset: Optional[ProviderPreset] = None,
            *,
            existing: Optional[ProviderEdit] = None,
        ) -> None:
            super().__init__()
            self.language = language
            # B5.1. Editing a saved provider is the same form, opened on a
            # record instead of on a preset. `preset` is forced to None in that
            # case on purpose: the preset styling hides the connection name and
            # the Base URL behind `preset-technical`, and those are exactly the
            # two fields somebody opening "Edit" came to change.
            self.existing = existing
            self.preset = None if existing is not None else preset
            # B2. Advanced settings start closed for a known preset and are a
            # deliberate second step, never a prerequisite.
            self._advanced_open = False

        def _label(self, russian: str, english: str) -> str:
            return english if self.language == "en" else russian

        def advanced_open(self) -> bool:
            """Whether the technical fields are currently revealed."""

            return self._advanced_open

        def _toggle_advanced(self) -> None:
            """Show or hide base URL, adapter, connection name and the limits.

            B2. The defect this closes: for a known preset those fields were
            hidden by `preset-technical` and there was **no** way to reveal them.
            Hiding a field is progressive disclosure; hiding it with no path back
            is a missing feature wearing the same clothes. A person whose
            provider moved to a regional endpoint had to abandon the preset and
            re-enter everything as a custom provider.

            The reverse mistake is the one this avoids: showing the base URL by
            default. A preset already knows its endpoint, and an input holding
            the right answer still reads as a question the user must answer.
            """

            self._advanced_open = not self._advanced_open
            visibility = "block" if self._advanced_open else "none"
            for widget in self.query(".preset-technical"):
                widget.styles.display = visibility
            for selector in self.ADVANCED_FIELD_IDS:
                with contextlib.suppress(Exception):
                    self.query_one(selector).styles.display = visibility
            # The adapter radio set is technical for a preset and essential for a
            # custom provider, so it is only ever toggled in the preset case.
            if self.preset is not None:
                with contextlib.suppress(Exception):
                    self.query_one("#provider-adapter").styles.display = visibility
            with contextlib.suppress(Exception):
                self.query_one("#provider-advanced", Button).label = (
                    self._label("Скрыть настройки", "Hide settings")
                    if self._advanced_open
                    else self._label("Дополнительные настройки", "Advanced settings")
                )

        def compose(self) -> ComposeResult:
            # A custom provider should start from the broad compatibility path,
            # not from OpenAI's first-party contract. Most third-party gateways
            # expose /chat/completions, while /responses is opt-in and has its own
            # explicit OpenAI preset. Keep custom identity/endpoint blank so the
            # form never looks configured before the user supplies their service.
            adapter = (
                self.preset.adapter_kind if self.preset else "openai_compatible_chat"
            )
            provider_id = self.preset.preset_id if self.preset else ""
            base_url = self.preset.base_url if self.preset else ""
            # B5.1. A saved record overrides the new-provider defaults. Its id
            # goes into the same field a new provider names itself in, which is
            # what makes the save an update: `configure_provider_model` writes
            # by provider_id, so an unchanged id cannot produce a second row.
            if self.existing is not None:
                adapter = self.existing.adapter
                provider_id = self.existing.provider_id
                base_url = self.existing.base_url
            with Vertical(id="provider-dialog"):
                yield Static(
                    self._label(
                        f"Изменить {provider_id}"
                        if self.existing is not None
                        else f"Подключить {self.preset.display_name if self.preset else 'API-провайдера'}",
                        f"Edit {provider_id}"
                        if self.existing is not None
                        else f"Connect {self.preset.display_name if self.preset else 'an API provider'}",
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
                        # B5.1/5.7. On an edit the field starts *empty* and the
                        # placeholder says the key is saved. Two things this is
                        # not: it is not the secret, and it is not a row of
                        # dots pretending to be one. A masked placeholder that
                        # looks like a value invites the user to clear it,
                        # which would read as "remove the key".
                        #
                        # Empty means keep. The controller resolves the stored
                        # reference at save time, so nothing has to travel
                        # through this widget to survive.
                        placeholder=(
                            self._label(
                                "Ключ сохранён. Оставьте пустым, чтобы не менять его",
                                "Key saved. Leave empty to keep it",
                            )
                            if self.existing is not None and self.existing.has_secret
                            else self._label(
                                "Вставьте ключ; он сохранится в системном хранилище",
                                "Paste the key; it will be stored in the system keyring",
                            )
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
                        # B2. "Limits" named one of the things behind this
                        # button and hid the rest. Base URL, adapter and the
                        # connection name were unreachable for a known preset,
                        # so the label now names the step and the button opens
                        # all of it. The limits screen is still reachable from
                        # the fields this reveals.
                        yield Button(
                            self._label(
                                "Дополнительные настройки", "Advanced settings"
                            ),
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
            if self.existing is not None:
                # The model and its limits live on a different record than the
                # provider, so they are filled here rather than in `compose`.
                # `_update_summary` also enables Save, which is right for an
                # edit: the record already has a model, so there is nothing to
                # discover before the user may save a changed Base URL.
                self.query_one("#provider-model", Input).value = (
                    self.existing.model_id
                )
                self.query_one("#provider-context", Input).value = (
                    str(self.existing.context_window)
                    if self.existing.context_window
                    else ""
                )
                self.query_one("#provider-output", Input).value = (
                    str(self.existing.max_output_tokens)
                    if self.existing.max_output_tokens
                    else ""
                )
                if self.existing.model_id:
                    self._update_summary()
                self.query_one("#provider-url", Input).focus()
                return
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
                # A verified provider gets its catalog registered right away:
                # the owner should not need to know a model id by heart. A
                # discovery failure never fails the save -- the provider is
                # verified and usable, and the catalog can be refreshed later
                # with /model refresh or the details screen.
                try:
                    from .tui_connections import discover_models_for_provider

                    discover_models_for_provider(
                        _provider_controller(), setup.provider_id.strip()
                    )
                except Exception:
                    pass
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

        def action_advanced(self) -> None:
            self._toggle_advanced()

        def action_limits(self) -> None:
            self._open_limits()

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
                self._toggle_advanced()
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

        def __init__(
            self,
            language: str = "ru",
            default_tunnel: str = "cloudflare",
            default_profile: str = "promptql",
            locked_profile: Optional[str] = None,
        ) -> None:
            super().__init__()
            self.language = language
            self._default_tunnel = default_tunnel
            self._locked_profile = locked_profile
            self._default_profile = locked_profile or default_profile

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
                if self._locked_profile is not None:
                    profile_labels = {
                        "notion": "Notion Custom Agent (MCP)",
                        "chatgpt-web": "ChatGPT Web (OAuth MCP)",
                        "claude-web": "Claude Web (OAuth MCP)",
                        "hyperagent-web": "Hyperagent (OAuth MCP)",
                        "adapt": "Adapt (MCP bearer)",
                        "clickup": self._label(
                            "ClickUp (MCP, отдельный Cloudflare-процесс)",
                            "ClickUp (MCP, separate Cloudflare process)",
                        ),
                    }
                    yield Static(
                        profile_labels.get(self._locked_profile, self._locked_profile),
                        id="bridge-profile-locked",
                        classes="hint",
                    )
                else:
                    with RadioSet(id="bridge-profile"):
                        yield RadioButton(
                            "PromptQL (OpenAPI)",
                            value=self._default_profile == "promptql",
                            id="profile-promptql",
                        )
                        yield RadioButton(
                            "Notion Custom Agent (MCP)",
                            value=self._default_profile == "notion",
                            id="profile-notion",
                        )
                        yield RadioButton(
                            "ChatGPT Web (OAuth MCP)",
                            value=self._default_profile == "chatgpt-web",
                            id="profile-chatgpt-web",
                        )
                        yield RadioButton(
                            self._label(
                                "ClickUp (MCP, отдельный Cloudflare-процесс)",
                                "ClickUp (MCP, separate Cloudflare process)",
                            ),
                            value=self._default_profile == "clickup",
                            id="profile-clickup",
                        )
                        yield RadioButton(
                            "Claude Web (OAuth MCP)",
                            value=self._default_profile == "claude-web",
                            id="profile-claude-web",
                        )
                        yield RadioButton(
                            "Generic Streamable HTTP MCP",
                            value=self._default_profile == "generic-streamable-http",
                            id="profile-generic",
                        )
                        yield RadioButton(
                            "Hyperagent (OAuth MCP)",
                            value=self._default_profile == "hyperagent-web",
                            id="profile-hyperagent",
                        )
                yield Static("", id="bridge-profile-note", classes="hint", markup=False)
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
            if self._locked_profile is None:
                self.query_one("#bridge-profile", RadioSet).focus()
            else:
                self.query_one("#bridge-tunnel-kind", RadioSet).focus()
            # Defaults are service-specific: Notion uses the durable parallel
            # Tailscale listener, while ClickUp keeps its Cloudflare quick tunnel.
            self._apply_default_tunnel_for_profile()

        def _apply_default_tunnel_for_profile(self) -> None:
            # Notion defaults to its durable Tailscale listener; the launcher
            # publishes it on HTTPS 8443 so it can run beside the main bridge on
            # HTTPS 443. ClickUp keeps its independent Cloudflare quick tunnel.
            # Setting a single RadioButton's
            # ``value = True`` does not reliably deselect its siblings inside a
            # RadioSet (the set's internal ``pressed_button`` can desync), so
            # we set all three explicitly to guarantee exactly one is selected.
            profile = self._profile_value()
            # Hosted OAuth services need a durable callback URL. Keep each
            # concurrently useful service on its own supported Funnel HTTPS port:
            # ChatGPT/Claude use 443, Notion uses 8443, Hyperagent uses 10000.
            target = "tailscale" if profile in {"notion", "hyperagent-web", "adapt"} else "cloudflare"
            self._default_tunnel = target
            self.query_one("#bridge-port", Input).value = (
                "8766"
                if profile == "clickup"
                else "8767"
                if profile == "notion"
                else "8768"
                if profile == "hyperagent-web"
                else "8769"
                if profile == "adapt"
                else "8765"
            )
            note = self.query_one("#bridge-profile-note", Static)
            if profile == "clickup":
                note.update(
                    self._label(
                        "ClickUp настроится внутри KaroX на порту 8766 через Cloudflare. Текущий ChatGPT-мост продолжит работать.",
                        "ClickUp will be configured inside KaroX on port 8766 through Cloudflare. The current ChatGPT bridge keeps running.",
                    )
                )
            elif profile == "notion":
                note.update(
                    self._label(
                        "Notion запустится отдельно на порту 8767 через Tailscale Funnel (HTTPS 8443). Текущий ChatGPT-мост на HTTPS 443 продолжит работать; адрес Notion останется стабильным после перезапуска.",
                        "Notion starts separately on port 8767 through Tailscale Funnel (HTTPS 8443). The current ChatGPT bridge on HTTPS 443 keeps running, and the Notion address stays stable across restarts.",
                    )
                )
            elif profile == "hyperagent-web":
                note.update(
                    self._label(
                        "Hyperagent запустится отдельным мостом на порту 8768 через Tailscale Funnel (HTTPS 10000). ChatGPT на HTTPS 443 и Notion на HTTPS 8443 можно оставить работающими; Hyperagent будет привязан к выбранной сейчас папке проекта.",
                        "Hyperagent starts as a separate bridge on port 8768 through Tailscale Funnel (HTTPS 10000). ChatGPT on HTTPS 443 and Notion on HTTPS 8443 may keep running; Hyperagent binds to the project folder currently selected in KaroX.",
                    )
                )
            elif profile == "adapt":
                note.update(
                    self._label(
                        "Adapt запустится минимальным bearer MCP-мостом на порту 8769 через Tailscale Funnel (HTTPS 10001). Если в этом проекте уже работает совместимый ChatGPT bridge, экран Adapt переиспользует его вместо запуска второго процесса.",
                        "Adapt starts as a minimal bearer MCP bridge on port 8769 through Tailscale Funnel (HTTPS 10001). If this project already has a compatible ChatGPT bridge, the Adapt screen reuses it instead of launching a second process.",
                    )
                )
            else:
                note.update("")
            states = {
                "tunnel-none": target == "none",
                "tunnel-cloudflare": target == "cloudflare",
                "tunnel-tailscale": target == "tailscale",
            }
            for widget_id, is_on in states.items():
                self.query_one(f"#{widget_id}", RadioButton).value = is_on
            if profile == "adapt":
                # Adapt's first-run profile is deliberately smaller than the
                # general bridge wizard. Repository/Git/check capabilities stay
                # enabled; browser and managed-server control require an explicit
                # user opt-in instead of arriving by accident with a Custom
                # Integration credential.
                for widget_id in (
                    "tool-browser-read",
                    "tool-browser-input",
                    "tool-server-read",
                    "tool-server-input",
                ):
                    self.query_one(f"#{widget_id}", Checkbox).value = False

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
            # Each checkbox is a capability family, not one arbitrarily chosen
            # primitive.  The hosted CLI receives an explicit --tool list, so
            # omitting siblings here *replaces* DEFAULT_WEB_TOOLS and leaves a
            # coding agent unable to navigate an unfamiliar repository.  Keep
            # the UI compact while granting the complete low-level family the
            # user actually selected.
            tool_ids = {
                "tool-read": (
                    "karox.repo.read_file",
                    "karox.repo.read_lines",
                    "karox.repo.search",
                    "karox.repo.inspect",
                    "karox.task.bootstrap",
                    "karox.task.resume",
                    "karox.task.status",
                    "karox.task.workstreams",
                    "karox.memory.remember",
                    "karox.memory.recall",
                    "karox.memory.context",
                    "karox.memory.list",
                    "karox.memory.forget",
                ),
                "tool-list": ("karox.repo.list_files",),
                "tool-write": (
                    "karox.repo.write_file",
                    "karox.repo.edit_file",
                ),
                "tool-status": (
                    "karox.git.status",
                    "karox.git.log",
                ),
                "tool-diff": ("karox.git.diff",),
                "tool-checks": ("karox.checks.run",),
                "tool-browser-read": (
                    "karox.browser.tabs",
                    "karox.browser.snapshot",
                    "karox.browser.wait_for",
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
            if profile == "clickup" and tunnel != "cloudflare":
                self.query_one("#bridge-error", Label).update(
                    self._label(
                        "ClickUp в автоматическом TUI-режиме использует Cloudflare Tunnel.",
                        "ClickUp uses Cloudflare Tunnel in the automatic TUI flow.",
                    )
                )
                return
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
            if self._locked_profile is not None:
                return self._locked_profile
            pressed = self.query_one("#bridge-profile", RadioSet).pressed_button
            values = {
                "profile-promptql": "promptql",
                "profile-notion": "notion",
                "profile-chatgpt-web": "chatgpt-web",
                "profile-clickup": "clickup",
                "profile-claude-web": "claude-web",
                "profile-generic": "generic-streamable-http",
                "profile-hyperagent": "hyperagent-web",
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
            # The focused button owns Enter. This matters after Tab moves from
            # Yes to No: Enter must activate No rather than a priority Yes action.
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

    class SessionBrowserScreen(ModalScreen[Optional[SessionAction]]):
        """Every run this process knows about, in one live list.

        The screen renders :class:`SessionViewStore` and folds nothing itself. The
        store is the only reducer, the bus is the realtime source and the durable
        records were merged once at startup, so a browser that recomputed a status
        from files would be a second source of truth that is free to disagree with
        the status bar three rows above it.

        Rows come from :func:`_session_browser_row_text`, the compact contract C
        gave this screen. It is not the ``/sessions`` renderer and is not meant
        to be: this list answers "which session do I open", so it carries the
        task, a human status, a human activity and little else, while the
        technical record stays in ``/sessions`` and Session Detail.

        The two contracts still share their source. One ``SessionSummary``, one
        set of status and action catalogs, so the words cannot disagree between
        surfaces even though the field sets do.

        **Ordering is by session id, not by activity.** The store sorts by most
        recent event, which is right for a status line and wrong for a list a
        person is moving a cursor through: an event about any session would
        reorder the list and move a row out from under the selection. Session ids
        are minted as ``task-<epoch>-<suffix>``, so sorting by id is stable *and*
        reads chronologically.

        Redraws are incremental. :meth:`apply_changes` is handed the dirty set the
        application already consumed and re-renders exactly those rows, so an
        event about one session touches no other row and cannot disturb the
        selection.
        """

        # A hard bound, not a scrolling hint. The store tracks up to
        # DEFAULT_SESSION_LIMIT sessions and one widget per session is what makes
        # a long-lived process slow to draw. The newest ids are kept and the
        # remainder is reported as a count rather than silently dropped.
        MAX_ROWS = 100

        BINDINGS = [
            Binding("up", "previous_session", "Up", priority=True),
            Binding("down", "next_session", "Down", priority=True),
            Binding("enter", "primary", "Action", priority=True),
            Binding("escape", "cancel", "Close", priority=True),
        ]
        DEFAULT_CSS = """
        SessionBrowserScreen { align: center middle; background: #0e0c08 92%; }
        /* C. `width: 100%` with a ceiling, the same shape the connection hub
           uses. A percentage alone gave a 120-column terminal a 113-column
           list whose rows were mostly whitespace between the status and the
           model -- wide enough to read as a table, which is the impression
           this screen must not make. The ceiling is what stops it. */
        #session-browser { width: 100%; max-width: 100; height: 80%;
          background: #1a1712; border: round #c6a56b; padding: 1 2; }
        #session-browser-title { height: 1; text-style: bold; color: #e5e5e5; }
        #session-rows { height: 1fr; scrollbar-color: #6b5c3e; }
        #session-rows Static { width: 1fr; padding: 0 1; color: #c6bca8; }
        #session-rows Static.selected { background: #2a251c; color: #e5e5e5;
          text-style: bold; }
        /* One line, enforced. `height: auto` let a long Russian action plus
           an overflow count wrap and eat a session row. */
        #session-browser-hint { height: 1; color: #8a7e6a; }
        """

        def __init__(
            self,
            store: SessionViewStore,
            language: str = "ru",
            *,
            initial_selection: Optional[str] = None,
        ) -> None:
            super().__init__()
            self._store = store
            self.language = language
            # Which row the cursor starts on. The application passes the session
            # the user was last looking at, so returning from the detail screen
            # puts the cursor back where they left it rather than at the top.
            self._selected = initial_selection
            # The rows on screen, in display order, and the widget drawing each.
            # Two structures rather than one because the selection is held by
            # session id and the DOM order has to match the sorted order.
            self._rows: "OrderedDict[str, SessionSummary]" = OrderedDict()
            self._widgets: Dict[str, Static] = {}
            # The display order as of the last refresh. Needed to answer "which
            # row was next to the one that vanished" -- afterwards nothing can
            # reconstruct where it used to be.
            self._previous_order: Tuple[str, ...] = ()
            self._overflow = 0
            # Whether the footer has ever been drawn. Without it the very first
            # refresh of an empty store would skip the update and leave the
            # empty-state sentence unwritten.
            self._hint_drawn = False

        # ------------------------------------------------------------- rendering

        def _label(self, russian: str, english: str) -> str:
            return english if self.language != "ru" else russian

        def compose(self) -> ComposeResult:
            with Vertical(id="session-browser"):
                yield Static(
                    # The shell already shows the brand; repeating it in every
                    # modal title spends width on something the user knows.
                    self._label("Сессии", "Sessions"),
                    id="session-browser-title",
                    markup=False,
                )
                yield VerticalScroll(id="session-rows")
                yield Static("", id="session-browser-hint", markup=False)

        def on_mount(self) -> None:
            # No dirty set on the first draw: every row is new.
            self.apply_changes(())

        def content_width(self) -> int:
            """How many columns a row's *text* actually gets.

            C. Not `app.size.width`, which was the earlier mistake. Between the
            terminal and the text sit the modal width, a border, dialog
            padding, the scroll container, row padding and possibly a
            scrollbar. Handing the renderer the terminal width gave it several
            columns that do not exist, so it decided the activity fitted and
            Textual then wrapped the line -- turning a one-line row into two
            and pushing every row below it down.

            Measured from the row container after mount, which accounts for all
            of the above at once and needs no hand-counted `-2`s scattered
            across the class. Before mount there is nothing to measure, so a
            conservative fallback is used: guessing high is what caused the
            wrap, and guessing low only drops an optional field.
            """

            # `#session-rows Static` carries `padding: 0 1`.
            return self._measured_width("#session-rows", padding=2)

        def footer_width(self) -> int:
            """How many columns the footer's text gets.

            Not the same number as :meth:`content_width`: the hint is a direct
            child of the dialog, so it pays the border and the dialog padding
            but neither the scroll container's scrollbar nor the per-row
            padding. Measuring it separately is the whole reason this class has
            no hand-counted offsets left in it.
            """

            return self._measured_width("#session-browser-hint", padding=0)

        def _measured_width(self, selector: str, *, padding: int) -> int:
            """The one place a usable text width is decided."""

            try:
                width = int(self.query_one(selector, Static).content_size.width)
            except Exception:
                try:
                    width = int(
                        self.query_one(selector, VerticalScroll).content_size.width
                    )
                except Exception:
                    width = 0
            if width > 0:
                return max(width - padding, 16)
            try:
                # Pre-mount: the dialog is not laid out yet, so this subtracts
                # the border and padding it is about to have. Guessing high is
                # what caused the wrap; guessing low only drops a field.
                return max(int(self.app.size.width) - 8 - padding, 16)
            except Exception:
                return BROWSER_STANDARD

        def _row_text(self, row: SessionSummary) -> str:
            # C. The compact contract, not the verbose one `/sessions` uses.
            # The browser answers "which session"; every field it drops is a
            # field Session Detail still shows to whoever chose.
            return _session_browser_row_text(
                row, self.language != "ru", self.content_width()
            )

        def _visible(self) -> Tuple[SessionSummary, ...]:
            """The rows this screen may draw, ordered and bounded.

            A store fault costs the refresh and never the screen: the browser is
            an observer, and an observer must not take the interface down with it.
            """

            try:
                rows = sorted(self._store.summaries(), key=lambda row: row.session_id)
            except Exception:
                return tuple(self._rows.values())
            self._overflow = max(len(rows) - self.MAX_ROWS, 0)
            if self._overflow:
                rows = rows[-self.MAX_ROWS :]
            return tuple(rows)

        def apply_changes(self, changed: Sequence[str] = ()) -> None:
            """Redraw the rows named in ``changed``, plus any structural change.

            ``changed`` is the dirty set the application consumed from the store.
            This screen deliberately never calls ``consume_dirty`` itself:
            consuming clears, so a second consumer would steal the set from the
            first and whichever ran second would quietly stop updating.
            """

            previous_ids = tuple(self._rows)
            previous_selected = self._selected
            previous_overflow = self._overflow
            incoming = OrderedDict((row.session_id, row) for row in self._visible())
            container = self.query_one("#session-rows", VerticalScroll)
            for session_id in [key for key in self._widgets if key not in incoming]:
                self._widgets.pop(session_id).remove()
            dirty = set(changed)
            order = list(incoming)
            for index, (session_id, row) in enumerate(incoming.items()):
                if session_id not in self._widgets:
                    widget = Static(self._row_text(row), markup=False)
                    self._widgets[session_id] = widget
                    self._mount_in_order(container, widget, order, index)
                elif session_id in dirty:
                    self._update_row(row)
            self._rows = incoming
            self._restore_selection()
            # C. The footer describes the *selected* row and the shape of the
            # list. An event about some other session changes neither, and
            # repainting it anyway would undo half the point of the incremental
            # row redraw directly above.
            if (
                not self._hint_drawn
                or tuple(incoming) != previous_ids
                or self._selected != previous_selected
                or self._overflow != previous_overflow
                or (self._selected is not None and self._selected in dirty)
            ):
                self._update_hint()

        def _mount_in_order(
            self,
            container: Any,
            widget: Static,
            order: List[str],
            index: int,
        ) -> None:
            """Mount a new row where the ordering says it belongs.

            Appending would be correct only while ids arrive in ascending order.
            That holds for a freshly minted session and fails for a durable record
            read after a later one, which is exactly the startup case.
            """

            for following in order[index + 1 :]:
                anchor = self._widgets.get(following)
                if anchor is not None:
                    container.mount(widget, before=anchor)
                    return
            container.mount(widget)

        def _update_row(self, row: SessionSummary) -> None:
            widget = self._widgets.get(row.session_id)
            if widget is not None:
                widget.update(self._row_text(row))

        def _redraw_rows(self) -> None:
            """Re-render every mounted row from the view models already held."""

            for session_id, row in self._rows.items():
                widget = self._widgets.get(session_id)
                if widget is not None:
                    widget.update(self._row_text(row))

        def refresh_language(self, language: str) -> None:
            """Re-say every word on this screen in the other language.

            One method rather than a caller poking at fields, because a
            language change makes *all* of it stale at once and the title was
            the piece that kept being forgotten: it is written in ``compose``,
            which never runs again, so a browser left open through a switch
            read "Sessions" above Russian rows.

            Deliberately not ``apply_changes``: nothing about the data changed.
            No widget is created or removed, the store is not read, the dirty
            set is not touched, and the selection and scroll survive because
            each existing widget is simply told to say something else.
            """

            self.language = language
            with contextlib.suppress(Exception):
                self.query_one("#session-browser-title", Static).update(
                    self._label("\u0421\u0435\u0441\u0441\u0438\u0438", "Sessions")
                )
            with contextlib.suppress(Exception):
                self._redraw_rows()
            with contextlib.suppress(Exception):
                # Covers the footer, the empty state and the overflow line,
                # all three of which are decided in one place.
                self._update_hint()

        def _update_hint(self) -> None:
            """One footer: the selected row's action, and what is hidden.

            C. The action moved here from every row. It is still whatever the
            store decided -- this changes where it is said, not what it says --
            but saying it once beside the cursor is the difference between a
            list of sessions and a list of buttons.
            """

            if not self._rows:
                # C. Through the policy, not around it. Written inline this
                # branch ignored the width budget entirely, and the Russian
                # sentence is long enough to wrap the one line the footer has.
                text = _browser_empty_text(
                    self.language != "ru", self.footer_width()
                )
            else:
                text = _session_browser_footer(
                    self._selected_row(),
                    self.language != "ru",
                    self.footer_width(),
                    shown=len(self._rows),
                    hidden=self._overflow,
                )
            self.query_one("#session-browser-hint", Static).update(text)
            self._hint_drawn = True

        # ------------------------------------------------------------- selection

        @property
        def selected_session(self) -> Optional[str]:
            return self._selected

        def rows(self) -> Tuple[SessionSummary, ...]:
            """The view models on screen, in display order."""

            return tuple(self._rows.values())

        def row_text(self, session_id: str) -> str:
            """What one row actually says, read back from its widget."""

            widget = self._widgets.get(session_id)
            return "" if widget is None else str(widget.render())

        def _selected_row(self) -> Optional[SessionSummary]:
            if self._selected is None:
                return None
            return self._rows.get(self._selected)

        def _restore_selection(self) -> None:
            """Keep the selected session across a refresh.

            Selection is a session id rather than an index, which is what makes an
            update to a different row -- or a new session appearing above this one
            -- leave the cursor where the user put it.
            """

            if self._selected not in self._rows:
                # C. The nearest surviving neighbour, not always the first row.
                # A session ending under the cursor used to throw the user to
                # the top of the list, which on a long list means losing their
                # place entirely.
                previous = list(self._previous_order)
                candidate: Optional[str] = None
                if self._selected in previous:
                    index = previous.index(self._selected)
                    for following in previous[index + 1 :]:
                        if following in self._rows:
                            candidate = following
                            break
                    if candidate is None:
                        for preceding in reversed(previous[:index]):
                            if preceding in self._rows:
                                candidate = preceding
                                break
                self._selected = candidate or next(iter(self._rows), None)
            self._previous_order = tuple(self._rows)
            self._highlight()

        def _highlight(self) -> None:
            for session_id, widget in self._widgets.items():
                widget.set_class(session_id == self._selected, "selected")

        def on_resize(self, _event: Any = None) -> None:
            """Re-render every visible row under the new width policy.

            A resize is the one event that legitimately touches all rows: the
            fields a row may show changed. The store and its dirty set are not
            involved, so this costs a redraw and nothing else.
            """

            self._redraw_rows()
            with contextlib.suppress(Exception):
                # The footer has its own width budget and its own fields to
                # drop, so a resize is one of the few events that legitimately
                # repaints it.
                self._update_hint()

        def _move(self, direction: int) -> None:
            order = list(self._rows)
            if not order:
                return
            index = order.index(self._selected) if self._selected in order else 0
            self._selected = order[(index + direction) % len(order)]
            self._highlight()
            # The footer names the *selected* row's action, so moving the
            # cursor has to refresh it or it would describe the previous row.
            with contextlib.suppress(Exception):
                self._update_hint()
            widget = self._widgets.get(self._selected)
            if widget is not None:
                with contextlib.suppress(Exception):
                    widget.scroll_visible()

        def action_previous_session(self) -> None:
            self._move(-1)

        def action_next_session(self) -> None:
            self._move(1)

        def action_primary(self) -> None:
            """Take the one action this row offers.

            Exactly one, chosen by the store: a row with four buttons is a row
            nobody reads, and the browser must not invent a fifth outcome.
            """

            row = self._selected_row()
            if row is None:
                return
            self.dismiss(SessionAction(row.session_id, row.primary_action))

        def action_cancel(self) -> None:
            self.dismiss(None)

    class SessionDetailScreen(ModalScreen[None]):
        """Everything one session is doing, from typed view models only.

        The browser answers "which session"; this answers "what is it actually
        doing, what did it cost, and what is it waiting on". It reads
        :meth:`SessionViewStore.detail` and nothing else: no ``provider_history``,
        no ``session.json``, no subprocess output, no raw payload dumps. The store
        stays the only reducer, and this screen keeps no store of its own.

        Sections are one widget each rather than one widget per row. A timeline of
        two hundred entries as two hundred widgets is what makes a long-lived
        process slow to draw, and rebuilding a widget list is also what loses the
        scroll position on every event. Each section is bounded and says how many
        rows it did not show.

        Updates arrive from the application's single dirty-set drain. An event
        about another session is ignored here, so it cannot cost a redraw or move
        the scroll.
        """

        # Bounds, not hints. The store's own history is already capped; these cap
        # what one screen draws out of it, and the difference is reported.
        MAX_TIMELINE_ROWS = 60
        MAX_TOOL_ROWS = 20
        MAX_LIST_ROWS = 10

        BINDINGS = [
            Binding("escape", "back", "Back", priority=True),
            Binding("up", "scroll_up", "Up", show=False),
            Binding("down", "scroll_down", "Down", show=False),
            Binding("pageup", "page_up", "PgUp", show=False),
            Binding("pagedown", "page_down", "PgDn", show=False),
        ]
        DEFAULT_CSS = """
        SessionDetailScreen { align: center middle; background: #0e0c08 92%; }
        /* D. A ceiling, like the browser and the hub. Technical text at 113
           columns is a telemetry grid; at 100 it is still a document. */
        #session-detail { width: 100%; max-width: 100; height: 88%;
          background: #1a1712; border: round #c6a56b; padding: 1 2; }
        /* The overview is pinned above the scroll, so "what is this and what is
           happening" cannot end up below the fold on a short terminal. Bounded,
           because a header that grows is a header that eats the body. */
        #session-detail-header { height: auto; max-height: 5; color: #e5e5e5;
          text-style: bold; }
        #session-detail-body { height: 1fr; scrollbar-color: #6b5c3e; }
        #session-detail-body .section-title { color: #d4b676; text-style: bold; }
        #session-detail-body .section-body { color: #c6bca8; margin-bottom: 1; }
        #session-detail-body .section-risk { color: #d6c49a; }
        #session-detail-hint { height: 1; color: #8a7e6a; }
        """

        # D. The order sections are drawn in, and the only names the screen
        # knows. Attention first because it is the only thing that can be
        # waiting on the reader; then what the run did; then, in descending
        # order of how rarely anybody needs it, the technical record. Nothing
        # was removed -- `risk` became `attention` and the old technical header
        # became `diagnostics` at the very bottom.
        SECTIONS: Tuple[str, ...] = (
            "attention",
            "progress",
            "errors",
            "timeline",
            "tools",
            "usage",
            "workspace",
            "browser",
            "evidence",
            "performance",
            "diagnostics",
        )

        # Sections that stay on screen even with nothing in them, because their
        # emptiness is itself a fact the reader asked for. Everything else is
        # hidden when empty: nine `no data` blocks taught people to scroll past
        # the region where the real answer appears.
        ALWAYS_SHOWN: frozenset = frozenset({"progress", "diagnostics"})

        def __init__(
            self,
            store: SessionViewStore,
            session_id: str,
            language: str = "ru",
            *,
            focus_risk: bool = False,
        ) -> None:
            super().__init__()
            self._store = store
            self.session_id = session_id
            self.language = language
            self._focus_risk = focus_risk
            # What each section currently says, so a test can assert on rendered
            # text and a redraw can compare against it.
            self._text: Dict[str, str] = {}
            self._bodies: Dict[str, Static] = {}
            # Instrumentation and contract: a redraw happens for this session and
            # for no other, and a test can prove the second half.
            self.redraws = 0
            # Which pending confirmation the reader has already been scrolled
            # to. `focus_risk` must fire on arrival and when a *new* decision
            # appears, and never on the ordinary tick in between: a screen that
            # jumps every second is one nobody can read.
            self._focused_confirmation: Optional[str] = None

        # ------------------------------------------------------------- rendering

        @property
        def english(self) -> bool:
            return self.language != "ru"

        def _label(self, russian: str, english: str) -> str:
            return english if self.english else russian

        def compose(self) -> ComposeResult:
            with Vertical(id="session-detail"):
                yield Static("", id="session-detail-header", markup=False)
                with VerticalScroll(id="session-detail-body"):
                    for name in self.SECTIONS:
                        yield Static(
                            "",
                            id=f"section-title-{name}",
                            classes="section-title",
                            markup=False,
                        )
                        yield Static(
                            "",
                            id=f"section-{name}",
                            classes=(
                                "section-body section-risk"
                                if name == "risk"
                                else "section-body"
                            ),
                            markup=False,
                        )

            yield Static("", id="session-detail-hint", markup=False)

        def on_mount(self) -> None:
            for name in self.SECTIONS:
                with contextlib.suppress(Exception):
                    self._bodies[name] = self.query_one(f"#section-{name}", Static)
            self.refresh_detail()
            if self._focus_risk:
                # ``review_risk`` brought the user here to look at one thing.
                # D. `attention`, not the retired `risk` name. Scrolling to a
                # section this screen no longer composes put the one reader who
                # was *sent here to decide something* in front of nothing.
                self.scroll_to_section("attention")
                self._focused_confirmation = self._confirmation_key()

        def _confirmation_key(self) -> Optional[str]:
            """An identity for the decision currently waiting, if any.

            The digest is used because it is what changes when the agent asks
            about a *different* action, and it never reaches the screen: this
            compares it, it does not render it.
            """

            detail = self._detail()
            if detail is None:
                return None
            risk = detail.pending_confirmation or detail.summary.risk
            if risk is None or not risk.awaiting_confirmation:
                return None
            return risk.action_digest or "pending"

        def _detail(self) -> Optional[SessionDetail]:
            """The snapshot to draw, or nothing when the store has no session.

            A store fault costs the refresh, never the screen: this is an
            observer, and an observer must not take the interface down with it.
            """

            try:
                return self._store.detail(self.session_id)
            except Exception:
                return None

        def _bounded(
            self, items: Sequence[Any], limit: int
        ) -> Tuple[Sequence[Any], int]:
            """The newest ``limit`` items, and how many were left out."""

            hidden = max(len(items) - limit, 0)
            return (items[-limit:] if hidden else items), hidden

        def _block(self, lines: Sequence[str], hidden: int = 0) -> str:
            """One section body: bounded lines, or an explicit "no data".

            An empty block says so. It must not show a zero or a blank, because
            both read as a measured fact rather than as an absent one.
            """

            if not lines:
                return _detail_words("empty", self.english)
            text = "\n".join(lines)
            if hidden:
                text += (
                    f"\n… {hidden} {_detail_words('truncated', self.english)}"
                )
            return text

        def _section_lines(self, name: str, detail: SessionDetail) -> Tuple[List[str], int]:
            english = self.english
            if name == "attention":
                return _detail_attention_lines(detail, english), 0
            if name == "progress":
                return _detail_progress_lines(detail, english), 0
            if name == "diagnostics":
                # Where the old technical header went. The full session id, the
                # access profile, the workspace mode and the raw step are all
                # still here, verbatim, for whoever is debugging rather than
                # deciding.
                lines = _detail_header_lines(detail, english)
                risk = detail.pending_confirmation or detail.summary.risk
                if risk is not None:
                    lines.extend(_risk_lines(risk, english))
                return lines, 0
            if name == "timeline":
                entries, hidden = self._bounded(detail.timeline, self.MAX_TIMELINE_ROWS)
                lines = [_timeline_entry_text(entry, english) for entry in entries]
                # The reducer's own drop counts belong here: a timeline that
                # silently lost events is worse than one that says it did.
                if detail.truncated_timeline:
                    lines.append(
                        f"… {detail.truncated_timeline} "
                        f"{_detail_words('truncated', english)}"
                    )
                if detail.dropped_events:
                    lines.append(
                        f"… {detail.dropped_events} {_detail_words('dropped', english)}"
                    )
                return lines, hidden
            if name == "tools":
                calls, hidden = self._bounded(detail.tool_calls, self.MAX_TOOL_ROWS)
                return [_tool_call_text(call, english) for call in calls], hidden
            if name == "errors":
                errors, hidden = self._bounded(detail.errors, self.MAX_LIST_ROWS)
                return [_timeline_entry_text(entry, english) for entry in errors], hidden
            if name == "usage":
                return _usage_lines(detail.summary, detail, english), 0
            if name == "workspace":
                return _mapping_lines(detail.git) + _mapping_lines(detail.diff), 0
            if name == "browser":
                return _mapping_lines(detail.browser), 0
            if name == "evidence":
                items, hidden = self._bounded(detail.evidence, self.MAX_LIST_ROWS)
                return _evidence_lines(items, english), hidden
            if name == "performance":
                return _performance_lines(detail.performance, english), 0
            return [], 0

        def refresh_detail(self) -> None:
            """Redraw the header and every section from the current snapshot."""

            self.redraws += 1
            detail = self._detail()
            english = self.english
            self._empty = set()
            if detail is None:
                # A session the store does not know. One sentence, and every
                # optional section hidden: eleven `no data` blocks would look
                # like measured emptiness rather than an absent session.
                header = self._label(
                    f"{self.session_id}: \u043d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445 \u043e \u0441\u0435\u0441\u0441\u0438\u0438.",
                    f"{self.session_id}: no data for this session.",
                )
                self._text = {
                    name: _detail_words("empty", english) for name in self.SECTIONS
                }
                self._empty = set(self.SECTIONS)
            else:
                # D. The overview, not the identity line. What this is and what
                # is happening to it, above the fold and above everything
                # technical.
                header = "\n".join(_detail_overview_lines(detail, english))
                for name in self.SECTIONS:
                    try:
                        lines, hidden = self._section_lines(name, detail)
                    except Exception:
                        # One malformed section must not cost the other ten.
                        lines, hidden = [], 0
                    if not lines:
                        self._empty.add(name)
                    self._text[name] = self._block(lines, hidden)
            self._write_widgets(header)

            # D. A confirmation that appears while the reader is already here
            # earns one scroll, and only one. Comparing the decision's identity
            # rather than its presence is what stops a live run dragging the
            # viewport back to the top on every tick.
            pending = self._confirmation_key()
            if pending is not None and pending != self._focused_confirmation:
                self._focused_confirmation = pending
                with contextlib.suppress(Exception):
                    self.scroll_to_section("attention")
            elif pending is None:
                self._focused_confirmation = None
            return

        def _write_widgets(self, header: str) -> None:
            with contextlib.suppress(Exception):
                self.query_one("#session-detail-header", Static).update(header)
            for name in self.SECTIONS:
                widget = self._bodies.get(name)
                if widget is None:
                    continue
                # D. Hidden rather than unmounted. The DOM stays fixed, so a
                # section appearing or disappearing costs a visibility flag and
                # not a rebuild -- which is what keeps the scroll position and
                # keeps a live update cheap.
                visible = name in self.ALWAYS_SHOWN or name not in self._empty
                with contextlib.suppress(Exception):
                    title = self.query_one(f"#section-title-{name}", Static)
                    title.update(_detail_words(name, self.english))
                    title.display = visible
                with contextlib.suppress(Exception):
                    widget.update(self._text.get(name, ""))
                    widget.display = visible
            with contextlib.suppress(Exception):
                self.query_one("#session-detail-hint", Static).update(
                    _detail_footer_text(self.english, self.footer_width())
                )

        def visible_sections(self) -> Tuple[str, ...]:
            """Which sections a reader can actually see, in screen order."""

            return tuple(
                name
                for name in self.SECTIONS
                if name in self.ALWAYS_SHOWN or name not in self._empty
            )

        # ------------------------------------------------------------- width

        def content_width(self) -> int:
            """How many columns a section body's text actually gets."""

            return self._measured_width("#session-detail-body")

        def footer_width(self) -> int:
            """How many columns the footer gets.

            A different number from :meth:`content_width`: the hint is a direct
            child of the dialog and pays neither the scroll container nor its
            scrollbar. Measuring both from the real content region is what
            keeps hand-counted offsets out of this class.
            """

            return self._measured_width("#session-detail-hint")

        def _measured_width(self, selector: str) -> int:
            """The one place a usable text width is decided."""

            for widget_type in (Static, VerticalScroll):
                try:
                    width = int(
                        self.query_one(selector, widget_type).content_size.width
                    )
                except Exception:
                    continue
                if width > 0:
                    return width
            try:
                # Pre-mount: subtract the border and padding the dialog is
                # about to have. Guessing low only drops an optional hint.
                return max(int(self.app.size.width) - 8, 16)
            except Exception:
                return BROWSER_STANDARD

        def refresh_language(self, language: str) -> None:
            """Re-say every word on this screen in the other language.

            One method, because a language change makes the overview, the
            section titles, the bodies and the footer stale at once. It is a
            redraw from the snapshot already in the store: no second screen, no
            new widget, no dirty set touched, and the browser underneath keeps
            its selection because it is never consulted.
            """

            self.language = language
            self.refresh_detail()

        # --------------------------------------------------------------- updates

        def apply_changes(self, changed: Sequence[str] = ()) -> bool:
            """Redraw only when this session is the one that changed.

            Returns whether a redraw happened, which is the property a test
            needs: an event about another session must cost nothing here.
            """

            if changed and self.session_id not in set(changed):
                return False
            self.refresh_detail()
            return True

        # ------------------------------------------------------------ inspection

        def sections(self) -> Dict[str, str]:
            """What every section currently says. Rendered text, not view models."""

            return dict(self._text)

        def section_text(self, name: str) -> str:
            return self._text.get(name, "")

        def header_text(self) -> str:
            try:
                return str(self.query_one("#session-detail-header", Static).render())
            except Exception:
                return ""

        def rendered_text(self) -> str:
            """Everything on screen as one string, for a leak assertion."""

            return "\n".join([self.header_text(), *self._text.values()])

        # ----------------------------------------------------------- navigation

        def scroll_to_section(self, name: str) -> None:
            widget = self._bodies.get(name)
            if widget is None:
                return
            with contextlib.suppress(Exception):
                widget.scroll_visible()

        def _body(self) -> Any:
            return self.query_one("#session-detail-body", VerticalScroll)

        def action_scroll_up(self) -> None:
            with contextlib.suppress(Exception):
                self._body().scroll_up()

        def action_scroll_down(self) -> None:
            with contextlib.suppress(Exception):
                self._body().scroll_down()

        def action_page_up(self) -> None:
            with contextlib.suppress(Exception):
                self._body().scroll_page_up()

        def action_page_down(self) -> None:
            with contextlib.suppress(Exception):
                self._body().scroll_page_down()

        def action_back(self) -> None:
            self.dismiss(None)

    class KaroXApp(App[int]):
        """Human-facing KaroX terminal application."""

        TITLE = "KaroX"
        SUB_TITLE = "локальный агент"
        ENABLE_COMMAND_PALETTE = True
        BINDINGS = [
            Binding("ctrl+p", "command_palette", "Команды", show=False),
            # Ctrl+M is not bindable in a real terminal: every terminal sends
            # carriage return for it, so Textual can only see Enter. The model
            # picker therefore lives on Ctrl+G, a key that arrives intact
            # everywhere. Advertised in the welcome and the command palette.
            Binding("ctrl+g", "model", "Модель", show=False),
            # No Ctrl+U binding: the composer Input owns ctrl+u for "delete to
            # the start of the line", and an app-level shortcut that only works
            # while the user is *not* typing is a trap. Usage & Cost stays on
            # /usage and in the command palette.
            Binding("ctrl+s", "onboarding", "Подключения", show=False),
            Binding("ctrl+o", "session_browser", "Сессии", show=False),
            Binding("ctrl+w", "workspace", "Папка", show=False, priority=True),
            Binding("ctrl+l", "clear_log", "Очистить", show=False),
            Binding("ctrl+q", "quit", "Выход", show=False),
            Binding("escape", "stop_agent", "Стоп", show=False, priority=True),
            # Ctrl+C follows terminal convention: stop active work first,
            # otherwise stop the bridge, and exit only when nothing is running.
            # Copying remains available on Ctrl+Shift+C.
            Binding("ctrl+c", "interrupt", "Остановить / выйти", show=False, priority=True),
            Binding(
                "ctrl+shift+c",
                "copy_selection",
                "Копировать",
                show=False,
                priority=True,
            ),
        ]
        # A2. There is no step list any more, so there is no cap on one. The
        # `#activity` pane holds a single line, and the second row exists only
        # for a critical failure.
        CSS = """
        Screen { background: #121212; color: #dcdcdc; }
        /* Keyboard focus must be unmistakable. Tab used to move focus while the
           screen gave almost no visual feedback, making Enter feel random even
           when dispatch was correct. Keep geometry unchanged: only paint. */
        Button:focus { background: #d4b676; color: #121212; text-style: bold; }
        Input:focus { border: round #d4b676; }
        OptionList:focus { border: round #d4b676; }
        RadioSet:focus { border: round #d4b676; }
        /* The chrome above and below the chat came to seventeen rows before a
           single word of conversation, which in a small window left the answer a
           few lines to live in. The blank row over the title and the one under
           the composer were the two that bought nothing. */
        /* One line of chrome above the conversation, and nothing else.
           This replaced a brand block plus a three-row grid of five equal
           status columns. Two defects came from that grid and both are gone
           with it: at 80 columns each column got 16 cells, so "контекст: лимит"
           filled its own column exactly and ran flush into the next field --
           the row read "контекст: лимитмост: выключен" and looked like a
           rendering fault; and `text-overflow: ellipsis` drew "openai/model-a"
           as "openai/m", which reads as a *different* model to the person
           checking which one is selected. `_header_line` drops a field whole
           instead, so nothing on screen is ever a half-truth. */
        #header-status { height: 1; padding: 0 2; background: #181511;
          color: #e0dccc; }
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
        /* A2. One row, and a second only for a critical failure. The four-row
           panel this replaces held a list of raw tool calls; it now holds one
           sentence, and the three rows it stops reserving go to the
           conversation, which is the thing the user came for. `max-height: 2`
           is the contract stated where the layout engine can enforce it: even
           if a line escaped `_fit_activity_line`, it cannot eat the chat. */
        #activity { display: none; height: auto; min-height: 1; max-height: 2;
          margin: 0 2; padding: 0 1; background: #1a1712;
          border-left: thick #c6a56b; color: #d4b676; }
        #activity.activity-success { border-left: thick #8aab7e; color: #b7c2b0; }
        #activity.activity-error { border-left: thick #cf7c7c; color: #e0a3a3; }
        #activity.activity-warning { border-left: thick #c3a86b; color: #d6c49a; }
        #command-menu { display: none; height: auto; max-height: 14; margin: 0 2;
          padding: 0 1; background: #191612; border: round #4a4338;
          color: #c6bca8; }
        #composer-wrap { height: 3; padding: 0 2; background: #181511; }
        #composer { border: round #4a4338; background: #20201c; color: #e5e5e5; }
        #composer:focus { border: round #c6a56b; }
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
            # B1. Whether the provider wizard was entered from the Connection
            # Hub, and should therefore hand the user back to it. The wizard is
            # also reachable on its own (Ctrl+S used to, `action_setup` still
            # does), and those callers must keep landing in the chat -- so this
            # is a property of the journey, not of the wizard.
            self._connect_return_to_hub = False
            # B5. Where the hub should put the cursor when it next opens, and
            # which record the open detail screen is about. Both are journey
            # state rather than connection state -- the registries own the
            # latter -- so they live here and are cleared as soon as they are
            # spent, rather than lingering to steer an unrelated flow.
            self._connection_detail_id: Optional[str] = None
            self._hub_select: Optional[str] = None
            self._hub_select_after_delete: Optional[str] = None
            self._hub_neighbour: Optional[str] = None
            self.agent_busy = False
            self.agent_process: Optional[subprocess.Popen[str]] = None
            self._stop_requested = False
            self._history_seen = 0
            self._history_fingerprint: Optional[Tuple[int, int]] = None
            # The single typed view layer for this application instance. The TUI
            # folds no events itself: EventBus is the realtime truth, this store
            # is the only reducer, and widgets render its view models. One store
            # per app, attached on mount and detached on unmount.
            self._view_store = SessionViewStore()
            # Which session the Session Browser should start on. Remembered
            # across the detail screen so returning does not send the cursor
            # back to the top of the list.
            self._browser_selection: Optional[str] = None
            # What /compact produced and the next submission carries: a bounded,
            # redacted continuation preamble. Consumed exactly once on dispatch.
            self._continuation_context: Optional[str] = None
            self._view_detach: Optional[Callable[[], None]] = None
            self._view_bus: Optional[EventBus] = None
            # Run identity, not session identity. A monotonic counter bumped by
            # every real submission, the run the application currently owns, and
            # the single terminal claim belonging to that run.
            #
            # This replaced an unbounded ``set`` of session ids, which grew for
            # the life of the process and still could not tell two runs of one
            # session apart. Two values bounded by definition say more: a late
            # callback from a previous generation is recognised and dropped, and
            # a duplicate callback of the current run claims the terminal
            # transition only once.
            self._run_generation = 0
            self._active_run: Optional[RunIdentity] = None
            self._terminal_claimed: Optional[RunIdentity] = None
            # A2. The single activity line, and everything it is derived from.
            #
            # `_activity_calls` maps a call id to the *kind* of action it is, so
            # a result arriving after its call can be classified without
            # consulting the tool name again. `_activity_changed` is a set of
            # paths kept only to be counted -- the count reaches the screen, the
            # names never do. Nothing here is free-form text destined for a
            # widget; see the activity-line catalogs for why that is the whole
            # security argument.
            self._activity_calls: "OrderedDict[str, str]" = OrderedDict()
            self._activity_changed: "OrderedDict[str, None]" = OrderedDict()
            self._activity_tests: Optional[int] = None
            self._activity_kind = ""
            self._activity_reason = ""
            self._activity_started: Optional[float] = None
            # The text currently on screen, so a tick that changes nothing costs
            # a string compare instead of a repaint.
            self._activity_rendered = ""
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
            self.run_cost_profile = str(
                _load_preferences().get("run_cost_profile", "balanced")
            )
            if self.run_cost_profile not in {"balanced", "economy"}:
                self.run_cost_profile = "balanced"
            raw_effort = _load_preferences().get("reasoning_effort")
            self.reasoning_effort = (
                str(raw_effort) if raw_effort in REASONING_EFFORTS else None
            )
            # The ladder is separate from the raw provider hint above:
            # reasoning_effort is the legacy per-provider knob, effort_level
            # is the product budget ladder from /effort.
            self.effort_level = _load_effort_level()
            # The agent stance from /mode, persisted exactly like the ladder.
            self.agent_mode = _load_agent_mode()
            self._last_assistant_content = ""
            self._task_started_at: Optional[float] = None

        def compose(self) -> ComposeResult:
            # One permanent line of chrome. The brand block, the five status
            # columns and the sponsor ticker are gone: seventeen rows of chrome
            # stood between the top of the window and the first word of the
            # conversation, and in a 14-row terminal that left the answer almost
            # nowhere to live. The sponsor ticker in particular is not mounted at
            # all now, so it costs no height and cannot animate behind the work.
            yield Static("", id="header-status", markup=False)
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

        def on_mount(self) -> None:
            self._start_session_view()
            self._refresh_status()
            self.query_one("#composer", Input).focus()
            # The sponsor ticker is no longer part of the ordinary shell, so
            # there is no widget to bind and no scrolling timer to start. The
            # helpers below stay because ``/sponsors`` is still a real command;
            # they simply have nothing to draw into and return immediately.
            self._sponsor_widget = None
            # Phase 3: typed transcript replaces _poll_agent_history. The
            # transcript store (SQLite WAL) is the process-boundary source;
            # the agent subprocess writes events and this timer reads them.
            self.set_interval(0.35, self._poll_typed_transcript)
            # A separate, slower timer than the history poll: events arrive on
            # the publisher's thread and mark sessions dirty, and this drains
            # that set. A burst of two hundred events therefore costs one
            # redraw here rather than two hundred.
            self.set_interval(0.25, self._drain_session_view)
            # The elapsed number, and nothing else. Slower than either poll
            # above because a second is the resolution the line shows, and
            # `_show_activity` returns without touching the widget when the
            # rendered text has not changed.
            self.set_interval(1.0, self._tick_activity)
            if self._needs_language:
                self.call_after_refresh(
                    lambda: self.push_screen(
                        LanguageScreen(), self._first_language_selected
                    )
                )
            else:
                # Start in the chat, not behind another modal. Home remains one
                # key away (Ctrl+H / /home), but an automatic dashboard stole
                # focus from the composer and made the first command feel broken.
                self._show_welcome()
            reason = _unsafe_workspace_reason(self.repository, self.language)
            if reason:
                self._write_notice(reason, "error")

        def on_unmount(self) -> None:
            # Detach before the bridge stops: a subscriber that outlives the UI
            # would hold this application object alive and fold events into
            # widgets that no longer exist.
            self._stop_session_view()
            self._stop_bridge(quiet=True)
            # The typed-transcript poller reads through the process-shared
            # store; the app leaving is the moment that SQLite handle loses
            # its last reader here, so close it explicitly instead of leaving
            # it to garbage collection.
            try:
                from .transcript_shadow import close_transcript_store

                close_transcript_store()
            except Exception:
                pass

        # ------------------------------------------------- typed event view layer

        def _start_session_view(self) -> None:
            """Attach the view store to the process-wide bus and backfill it.

            Order matters and is deliberate. Subscribing first and syncing after
            means an event published between the two is folded twice, which the
            store discards by sequence number. The reverse order would drop it
            entirely.

            Nothing here is allowed to prevent the interface from starting: a
            missing bus or an unreadable session directory costs typed status,
            not the application.
            """

            # Contract: start is transactional and stop is idempotent. A repeated
            # start first stops the previous subscription, so a successful start
            # always leaves exactly one subscriber and a failed one leaves none.
            self._stop_session_view()
            detach: Optional[Callable[[], None]] = None
            try:
                bus = event_bus()
                # Held locally, not published to self, until initialisation has
                # fully succeeded. Assigning the handle first meant that a merge
                # or sync failure reset the attribute while the subscriber stayed
                # registered on the bus -- an unreachable, un-detachable leak.
                detach = self._view_store.attach(bus)
                # Fold the in-process event ring immediately; disk history is
                # intentionally backfilled after first paint on a worker. Reading
                # every saved session here made the composer feel frozen on a
                # long-lived install even though none of that history is needed
                # to type the next task.
                self._view_store.sync(bus)
            except Exception:
                if detach is not None:
                    try:
                        detach()
                    except Exception:
                        pass
                self._view_bus = None
                self._view_detach = None
                return
            self._view_bus = bus
            self._view_detach = detach
            self._view_store.consume_dirty()
            self.run_worker(
                self._merge_persisted_sessions_safely,
                thread=True,
                exclusive=True,
                group="session-history-backfill",
            )

        def _merge_persisted_sessions_safely(self) -> None:
            """Backfill on a worker without letting a disk fault break the app.

            ``_merge_persisted_sessions`` is itself fail-soft, but it runs in a
            Textual worker thread: an unexpected raise there would surface as a
            worker crash and, under ``run_test``, as an app exception. A history
            backfill failure costs persisted rows, never the live subscription
            or the interface, so the wrapper guarantees exactly that.
            """

            try:
                self._merge_persisted_sessions()
            except Exception:
                pass

        def _stop_session_view(self) -> None:
            detach = self._view_detach
            self._view_detach = None
            self._view_bus = None
            if detach is None:
                return
            try:
                detach()
            except Exception:
                # An unsubscribe that raises is not a reason to fail shutdown.
                pass

        def _publish_event(
            self,
            kind: EventKind,
            *,
            session_id: str,
            summary: str,
            data: Dict[str, Any],
            level: EventLevel = EventLevel.INFO,
        ) -> None:
            """Publish one typed lifecycle event, never at the cost of the run.

            Publishing is observability. An agent run that completed must not be
            reported as failed, and must not fail at all, because a bus was
            missing or a subscriber raised -- so every fault here is swallowed.
            The durable ``SessionStore`` remains the recovery source; this is the
            realtime projection on top of it.

            ``session_id`` is required by the view store: an event without one
            cannot appear in any per-session view, so an empty id publishes
            nothing rather than a row nobody can find.
            """

            if not session_id:
                return
            try:
                bus = self._view_bus or event_bus()
            except Exception:
                return
            try:
                bus.publish(
                    kind,
                    session_id=session_id,
                    summary=summary,
                    source=EVENT_SOURCE_TUI_AGENT,
                    level=level,
                    data=data,
                )
            except Exception:
                # A broken view layer is not allowed to stop an agent.
                return

        def _claim_terminal(self, run: Optional[RunIdentity]) -> bool:
            """Claim the single terminal transition one run may publish.

            Keyed on the run, not the session. ``_agent_finished`` runs once per
            run, but a stop racing the child's own exit could reach two terminal
            publishers for the same run, and a *later* run of the same session
            must not inherit the earlier run's spent claim. The first claim of a
            given identity wins; the next generation starts unclaimed.
            """

            if run is None or not run.session_id:
                return False
            if self._terminal_claimed == run:
                return False
            self._terminal_claimed = run
            return True

        def _lifecycle_identity(self, session_id: str) -> Dict[str, Any]:
            """Facts about this run, from their real sources, or nothing.

            Every field here used to be a convenient guess: ``workspace_write``
            was hardcoded whatever the session was actually created with, and
            ``workspace_mode`` was published as ``repository`` although no
            canonical field or enum of that name exists anywhere in the product.
            A guess that renders identically to a fact is worse than a blank,
            because nothing on the screen distinguishes the two.

            So the rule is: publish what the parent knows, read what the durable
            record knows, and omit the rest. Reading the record is fail-soft --
            a ``SessionStore`` fault costs identity fields, never the run.
            """

            data: Dict[str, Any] = {}
            try:
                record = SessionStore(session_dir()).load(session_id)
            except Exception:
                record = None
            if record is not None:
                profile = _identifier(getattr(record, "access_profile", ""))
                if profile:
                    data["access_profile"] = profile
                task = _identifier(getattr(record, "task", ""))
                if task:
                    data["task"] = task
            try:
                selected = _selected_model()
            except Exception:
                selected = None
            if selected is not None:
                provider = _identifier(getattr(selected, "provider_id", ""))
                model = _identifier(getattr(selected, "model_id", ""))
                if provider:
                    data["provider"] = provider
                if model:
                    data["model"] = model
            return data

        def _publish_agent_started(self, run: RunIdentity, task: str) -> None:
            """Announce a run that this process just started.

            Every field is something the parent knows for certain at this point:
            the task it is about to send, the model selection the run will use,
            and the access profile the durable session was created with. Nothing
            is parsed out of human-readable log text and nothing is invented.
            """

            session_id = run.session_id
            data: Dict[str, Any] = {
                "status": STATUS_RUNNING,
                "phase": STATUS_RUNNING,
                "task": task,
                # Only this publisher may say ``karox``: it is the native KaroX
                # agent that ``_submit_task`` dispatches. A future publisher for
                # a different agent must name that agent instead of inheriting
                # this one.
                "agent": "karox",
                # A fresh run has no step yet. Published explicitly so a resumed
                # session cannot keep showing the step of its previous attempt.
                "current_step": "",
            }
            # Durable and selection-sourced facts. The submitted task stays
            # authoritative over the recorded one: it is what this run was given.
            identity = self._lifecycle_identity(session_id)
            identity.pop("task", None)
            data.update(identity)
            self._publish_event(
                EventKind.SESSION_STATE,
                session_id=session_id,
                summary=SUMMARY_RUN_STARTED,
                data=data,
            )

        def _publish_agent_completed(
            self, run: RunIdentity, report: Optional[Dict[str, Any]]
        ) -> None:
            """Announce a run this process observed finishing successfully."""

            if not self._claim_terminal(run):
                return
            session_id = run.session_id
            data: Dict[str, Any] = {
                "status": STATUS_COMPLETED,
                "phase": STATUS_COMPLETED,
                "current_step": "",
            }
            if isinstance(report, dict):
                changed = report.get("changed_files")
                if isinstance(changed, (list, tuple)):
                    data["changed_files"] = len(changed)
            # Identity is read here rather than only at start. ``_submit_task``
            # mints a fresh session id and the *child* is what creates the
            # durable record, so at start time there is nothing on disk to read
            # and ``access_profile`` is genuinely unprovable. By the terminal
            # event the record exists, so the browser gets the real profile
            # instead of a hardcoded guess. Still fail-soft: no record, no field.
            data.update(self._lifecycle_identity(session_id))
            self._publish_event(
                EventKind.SESSION_STATE,
                session_id=session_id,
                summary=SUMMARY_RUN_COMPLETED,
                data=data,
            )
            self._publish_agent_usage(session_id, report)

        def _publish_agent_usage(
            self, session_id: str, report: Optional[Dict[str, Any]]
        ) -> None:
            """Publish measured token usage as the event kind that folds it.

            SessionViewStore reads usage, cost and budgets in its AGENT_ACTION
            fold and nowhere else, so usage attached to a SESSION_STATE payload
            was silently dropped: the publisher looked correct and the number
            never reached a row. Usage is a separate typed event for that reason.

            Only real measurements are published. A report with no usage block
            produces no event at all, because a zero here would be read as "this
            run cost nothing" rather than "nobody measured it".
            """

            if not isinstance(report, dict):
                return
            raw = report.get("usage")
            if not isinstance(raw, dict):
                return
            usage: Dict[str, Any] = {}
            for key, value in raw.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                usage[str(key)] = value
            if not usage:
                return
            self._publish_event(
                EventKind.AGENT_ACTION,
                session_id=session_id,
                summary=SUMMARY_RUN_USAGE,
                data={"usage": usage},
            )

        def _publish_agent_stopped(self, run: RunIdentity, reason: str) -> None:
            """Announce a run that ended without a verified outcome and without a crash.

            ``no_changes`` and the bounded limits land here. The CLI exits
            non-zero for these because nothing was verified, but nothing failed
            either, so publishing an ERROR would put a red row on a session the
            user can simply resume. The reason travels as a stable identifier in
            ``waiting_reason``; the words come from the UI catalog.
            """

            if not self._claim_terminal(run):
                return
            self._publish_event(
                EventKind.SESSION_STATE,
                session_id=run.session_id,
                summary=SUMMARY_RUN_STOPPED,
                level=EventLevel.WARNING,
                data={
                    "status": STATUS_STOPPED,
                    "phase": STATUS_STOPPED,
                    "current_step": "",
                    "waiting_reason": reason or "stopped",
                },
            )

        def _publish_agent_failed(
            self, run: RunIdentity, reason: str, *, code: str = ERROR_AGENT_FAILED
        ) -> None:
            """Announce a failed run as a typed error plus a terminal status.

            Two events, not one: the error carries a stable code for the error
            list, and the state change moves the session out of ``running``. A
            failure that only logged text would leave the row running forever.

            ``code`` distinguishes an honestly failed agent from a report that
            could not be parsed and from a report contradicting its own exit
            code. The free-form diagnostic stays in ``data.reason``; the summary
            is an identifier, never the reason text, because a reason can carry
            arbitrary child output and a summary is rendered as a message.
            """

            if not self._claim_terminal(run):
                return
            session_id = run.session_id
            summary = {
                ERROR_AGENT_CONTRACT: SUMMARY_CONTRACT_MISMATCH,
                ERROR_AGENT_MALFORMED: SUMMARY_MALFORMED_REPORT,
            }.get(code, SUMMARY_RUN_FAILED)
            self._publish_event(
                EventKind.ERROR,
                session_id=session_id,
                summary=summary,
                level=EventLevel.ERROR,
                data={"code": code, "reason": reason},
            )
            self._publish_event(
                EventKind.SESSION_STATE,
                session_id=session_id,
                summary=SUMMARY_RUN_FAILED,
                level=EventLevel.ERROR,
                data={
                    "status": STATUS_FAILED,
                    "phase": STATUS_FAILED,
                    "current_step": "",
                },
            )

        def _publish_agent_cancelled(self, run: RunIdentity) -> None:
            """Announce a run the user stopped.

            Cancelled is not failed: the session was saved and can be resumed, so
            it gets its own terminal status and a reason the browser can show.
            """

            if not self._claim_terminal(run):
                return
            self._publish_event(
                EventKind.SESSION_STATE,
                session_id=run.session_id,
                summary=SUMMARY_RUN_CANCELLED,
                level=EventLevel.WARNING,
                data={
                    "status": STATUS_CANCELLED,
                    "phase": STATUS_CANCELLED,
                    "current_step": "",
                    "waiting_reason": "stopped_by_user",
                },
            )

        def _merge_persisted_sessions(self) -> None:
            """Seed the store with what is on disk, as durable startup facts only.

            This is a one-shot backfill, not a realtime source. After mount the
            store is fed by events; nothing re-reads these files to learn a
            current status.
            """

            try:
                records = SessionStore(session_dir()).list()
                iterator = iter(records)
            except (Exception, TypeError):
                return
            payloads: List[Dict[str, Any]] = []
            for record in iterator:
                try:
                    payloads.append(
                        {
                            "session_id": record.session_id,
                            "task": record.task,
                            "status": _identifier(record.status),
                            "phase": _identifier(record.phase),
                            "access_profile": _identifier(record.access_profile),
                            "created_at": record.created_at,
                            "updated_at": record.updated_at,
                            "changed_files": record.changed_files,
                        }
                    )
                except Exception:
                    continue
            if not payloads:
                return
            try:
                self.call_from_thread(self._apply_persisted_sessions, payloads)
            except Exception:
                # The app may have closed while the disk snapshot was loading.
                return

        def _apply_persisted_sessions(self, payloads: Sequence[Mapping[str, Any]]) -> None:
            """Merge a disk snapshot on Textual's UI thread."""

            self._view_store.merge_records(payloads)

        def _drain_session_view(self) -> None:
            """Redraw once per refresh tick for the sessions that actually changed.

            ``consume_dirty`` is the debounce primitive, so this costs one status
            update per burst rather than one per event. A render fault must not
            propagate: the publisher is the agent, and a broken widget is not
            allowed to stop it.
            """

            try:
                changed = self._view_store.consume_dirty()
            except Exception:
                return
            if not changed:
                return
            if self.active_session and self.active_session in changed:
                try:
                    self._refresh_status()
                except Exception:
                    pass
            self._update_session_browser(changed)

        def _update_session_browser(self, changed: Sequence[str]) -> None:
            """Hand the dirty set to an open Session Browser.

            This tick is the single consumer of ``consume_dirty``, because
            consuming clears: a browser that consumed the set itself would steal
            it from the status bar, and whichever ran second would quietly stop
            updating. So the set travels from here instead of being read twice.
            """

            for screen in list(self.screen_stack):
                if not isinstance(screen, (SessionBrowserScreen, SessionDetailScreen)):
                    continue
                try:
                    screen.apply_changes(changed)
                except Exception:
                    # An observer is never allowed to take the interface down.
                    pass

        def action_session_browser(self) -> None:
            """Open the Session Browser over the conversation.

            The screen is handed the application's own store rather than building
            one: a second store would have its own cursor, re-fold the whole ring
            and disagree with the status bar. The detail screen is handed the same
            one for the same reason.
            """

            self.push_screen(
                SessionBrowserScreen(
                    self._view_store,
                    self.language,
                    initial_selection=self._browser_selection,
                ),
                self._session_browser_done,
            )

        @on(events.Click, "#activity")
        def _activity_clicked(self, event: events.Click) -> None:
            """Open technical session detail when the visible failure row is clicked."""
            if self._activity_kind != ACTIVITY_FAILED or not self.active_session:
                return
            event.stop()
            self._open_session_detail(self.active_session)

        def _open_session_detail(
            self, session_id: str, *, focus_risk: bool = False
        ) -> None:
            """Open the Session Detail screen for one session.

            ``focus_risk`` is what ``review_risk`` means for now: bring the person
            to the risk block of the session that is waiting on them. Approving
            still has to go through the ledger, so no control here claims to do it.
            """

            self._browser_selection = session_id
            self.push_screen(
                SessionDetailScreen(
                    self._view_store,
                    session_id,
                    self.language,
                    focus_risk=focus_risk,
                ),
                self._session_detail_done,
            )

        def _session_detail_done(self, _result: Optional[None] = None) -> None:
            """Escape from the detail screen returns to the browser.

            The browser is re-opened with the session the user was looking at, so
            the cursor is where they left it. Re-opening rather than keeping the
            old screen alive means one screen owns the list at a time and there is
            no hidden observer still being handed dirty sets.
            """

            self.action_session_browser()

        def _session_browser_done(self, choice: Optional[SessionAction]) -> None:
            """Carry out the one action a row offered.

            ``stop`` is the only outcome that touches a process, and it is refused
            unless this application actually owns the run: a row can describe a
            session started by a different process, and pretending to stop that
            one would be a false statement on screen.
            """

            if choice is None:
                return
            if choice.action == ACTION_STOP:
                if self.agent_busy and self.active_session == choice.session_id:
                    self.action_stop_agent()
                else:
                    self._write_notice(
                        self._label(
                            "Этот запуск ведёт другой процесс — отсюда его не остановить.",
                            "Another process owns this run, so it cannot be stopped here.",
                        ),
                        "warning",
                    )
                return
            if choice.action == ACTION_REVIEW_RISK:
                # The session is stopped and waiting on this person, so take them
                # to it rather than printing one line about it. The active session
                # is deliberately not switched: reviewing a risk is not the same
                # act as choosing which session the composer talks to.
                self._write_notice(self._risk_review_text(choice.session_id), "warning")
                self._open_session_detail(choice.session_id, focus_risk=True)
                return
            # ``resume`` and ``open`` are the same act from the user's side: make
            # this the session on screen and show what it is doing. Resuming the
            # work itself stays an explicit task the person types, so nothing here
            # starts an agent.
            self.active_session = choice.session_id
            self._history_seen = 0
            self._history_fingerprint = None
            self._refresh_status()
            self._open_session_detail(choice.session_id)

        def _risk_review_text(self, session_id: str) -> str:
            """Describe a pending confirmation without carrying its token.

            The ledger keeps tokens out of events by construction and the view
            layer has no key for one, so there is nothing here to leak. The text
            is still assembled from named fields rather than a payload dump, which
            is what keeps that true if a publisher ever changes.
            """

            row = self._view_store.summary(session_id)
            risk = row.risk if row is not None else None
            if risk is None:
                return self._label(
                    "Подтверждение больше не требуется.",
                    "This session is no longer waiting for a confirmation.",
                )
            level = risk.level or self._label("неизвестный", "unknown")
            digest = risk.action_digest[:12]
            return self._label(
                f"{session_id}: нужно подтверждение, риск {level}, действие {digest}.",
                f"{session_id}: confirmation needed, risk {level}, action {digest}.",
            )

        def _session_is_event_backed(self, session_id: str) -> bool:
            """Whether any typed event has been folded for this session.

            This answers one narrow question and must not be read as "the whole
            session is migrated". Use :meth:`_projection_is_event_backed` to
            decide whether a specific projection may skip the legacy path.
            """

            try:
                row = self._view_store.summary(session_id)
            except Exception:
                return False
            # Asked of the store rather than a cached set: a set updated on the
            # refresh timer would still report "legacy" for a session whose event
            # arrived between two ticks, and the fallback would re-parse the
            # whole document on exactly the ticks that matter.
            return row is not None and row.last_event_seq > 0

        def _projection_is_event_backed(self, session_id: str, projection: str) -> bool:
            """Whether typed events own one named UI projection of a session.

            The migration is per projection, not per session. A SESSION_STATE
            event proves the status row and proves nothing about the transcript,
            so one global boolean was wrong: it silenced the compatibility path
            for data that path is still the only source of.

            A projection is event-backed only when it is listed as migrated and
            the session has actually produced a typed event.
            """

            if projection not in _EVENT_BACKED_PROJECTIONS:
                return False
            return self._session_is_event_backed(session_id)

        def _active_session_view(self) -> Optional[SessionSummary]:
            """The typed view model for the session on screen, if events cover it."""

            if not self.active_session:
                return None
            try:
                return self._view_store.summary(self.active_session)
            except Exception:
                return None

        def _session_status_text(self) -> str:
            """The session field of the status bar, from typed events when present.

            The view model supplies stable identifiers; the words come from the
            UI catalogs above. A session with no events yet keeps the plain id,
            which is the pre-existing behaviour and the compatibility fallback.
            """

            text = _TEXT[self.language]
            if not self.active_session:
                return text["session"] + ": " + text["new_task"]
            label = text["session"] + ": " + self.active_session
            row = self._active_session_view()
            if row is None or row.last_event_seq <= 0:
                return label
            english = self.language != "ru"
            parts = [_session_status_words(row.status, english)]
            # One detail, chosen by :func:`_session_detail_words`. The waiting
            # reason it now falls back to is what a stopped run was missing: the
            # publisher already sends ``no_changes`` or ``step_limit``, and the
            # bar showed a bare "stopped" that gave the user nothing to act on.
            detail = _session_detail_words(row, english)
            if detail:
                parts.append(detail)
            return f"{label} ({' • '.join(parts)})"

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
                # The cache exists to skip a repaint that would draw the same
                # text. Hiding the widget is a repaint it did not see, so a
                # stale cache here would suppress the next real line.
                self._activity_rendered = ""
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
            # First-run language selection must land in the same chat-first shell
            # as every later launch. Home is explicit (Ctrl+H / /home); pushing it
            # here steals focus from the composer just after the user chose a
            # language and recreates the old two-root onboarding confusion.
            self.query_one("#composer", Input).focus()

        def _set_language(self, language: str) -> None:
            _save_language(language)
            self.language = language
            text = _TEXT[language]
            composer = self.query_one("#composer", Input)
            composer.placeholder = text["placeholder"]
            self._refresh_status()
            if self._command_menu_open:
                self._update_command_menu(composer.value)
            # An open Session Browser renders identifiers through the UI
            # catalogs, so a language change makes every row stale at once --
            # the one case where redrawing the whole list is the right answer.
            #
            # D. Session Detail is in the same position and was being missed:
            # it has had `refresh_language` since the overview landed, and
            # nothing called it, so switching language with the screen open
            # left an entire English document above a Russian shell.
            for screen in list(self.screen_stack):
                if not isinstance(
                    screen, (SessionBrowserScreen, SessionDetailScreen)
                ):
                    continue
                # One call, so the title changes with the rows. Setting
                # `.language` and replaying the dirty set left the heading in
                # the old locale, because `compose` had already run.
                try:
                    screen.refresh_language(language)
                except Exception:
                    pass

        def _language_selected(self, language: Optional[str]) -> None:
            if language is not None:
                self._set_language(language)
                message = "Язык изменён." if language == "ru" else "Language changed."
                self._write(f"[#b7c2b0]{message}[/]")
            self.query_one("#composer", Input).focus()

        def _refresh_status(self) -> None:
            """Gather the facts and hand them to the one header renderer.

            This method collects; :func:`_header_line` decides. Formatting the
            header here as well would put the width rules in two places, and the
            copy that drifts is always the one a screen actually reads.

            Bridge state, the session id, the access profile and total spend are
            deliberately absent: they are answers to questions a person asks
            occasionally, and they were costing a permanent column each.
            """

            selected = _selected_model()
            model = (
                f"{selected.provider_id}/{selected.model_id}"
                if selected is not None
                else _TEXT[self.language]["not_configured"]
            )
            self.query_one("#header-status", Static).update(
                _header_line(
                    repository=self.repository.name or str(self.repository),
                    model=model,
                    activity=self._header_activity_text(),
                    width=self._header_width(),
                    effort=(
                        self.effort_level
                        if self.effort_level != AUTO_EFFORT
                        else self.reasoning_effort or "auto"
                    ),
                    mode=self.agent_mode,
                    economy=self.run_cost_profile == "economy",
                    context_note=self._context_warning(selected),
                )
            )

        def _header_width(self) -> int:
            """How many columns the header actually has, right now.

            Asked of the widget rather than assumed, because a terminal is
            resized while the application runs and a breakpoint chosen at mount
            would be wrong for the rest of the session.
            """

            try:
                width = int(self.size.width)
            except Exception:
                width = 0
            # A sane floor: during the first paint the size can still be zero,
            # and rendering nothing at all reads as a broken interface.
            return width if width > 0 else HEADER_WIDE_COLUMNS

        def _header_activity_text(self) -> str:
            """The one human state word for the header.

            Reuses the session status projection the store already produces, so
            the header cannot disagree with the Session Browser about what a run
            is doing. When no session is active this is empty rather than a
            placeholder: "no activity" is noise on a fresh screen.
            """

            row = self._active_session_view()
            if row is None:
                return ""
            english = self.language != "ru"
            words = _session_status_words(row.status, english)
            # The one extra fact, chosen by the shared rule: a pending
            # confirmation outranks a live step, which outranks a waiting
            # reason. "выполняется" alone would hide the single state where the
            # agent has stopped and is waiting on the person reading the screen.
            detail = _session_detail_words(row, english)
            return f"{words} · {detail}" if detail else words

        def _context_warning(self, selected: Optional[ModelRecord]) -> str:
            """Context occupancy, and only when it is close to mattering.

            Below :data:`CONTEXT_WARNING_FRACTION` this returns nothing at all.
            A permanent percentage is noise on every redraw; the same number at
            eighty percent is the one fact that explains a truncated answer.
            Nothing is invented: an unmeasured window produces no note.
            """

            if selected is None:
                return ""
            limit = _optional_positive_int(getattr(selected, "context_window", None))
            used = self._last_prompt_tokens()
            if not limit or used is None:
                return ""
            fraction = used / float(limit)
            if fraction < CONTEXT_WARNING_FRACTION:
                return ""
            label = "context" if self.language != "ru" else "контекст"
            return f"{label} {int(fraction * 100)}%"

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
            # The header chooses its fields by the width it has, so a resize is
            # exactly when it has to be recomputed. Without this, a window
            # narrowed after launch keeps drawing fields that no longer fit.
            try:
                self._refresh_status()
            except Exception:
                # A redraw fault must not propagate out of an event handler and
                # take the interface down mid-resize.
                pass
            self._apply_sponsor_visibility()

        def _set_sponsors_visible(self, visible: bool) -> None:
            self.sponsors_visible = bool(visible)
            _save_sponsors_visible(self.sponsors_visible)
            self._apply_sponsor_visibility()
            # Honest wording: the preference is still stored, but the ticker is
            # no longer part of the ordinary shell, so claiming it is "shown"
            # would be a statement the screen does not back up.
            state = self._label(
                "Лента спонсоров больше не занимает строку интерфейса."
                if visible
                else "Лента спонсоров скрыта.",
                "The sponsor line no longer occupies a row of the interface."
                if visible
                else "Sponsor line hidden.",
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
            elif command == "/home":
                self.action_home()
            elif command in {"/model", "/models"}:
                requested = argument.strip().casefold()
                if requested in {"refresh", "\u043e\u0431\u043d\u043e\u0432\u0438\u0442\u044c"}:
                    self._refresh_models_command()
                elif requested:
                    self._write_notice(
                        self._label(
                            "\u0424\u043e\u0440\u043c\u0430\u0442: /model \u0438\u043b\u0438 /model refresh",
                            "Usage: /model or /model refresh",
                        ),
                        "error",
                    )
                else:
                    self.action_model()
            elif command == "/usage":
                self.action_usage()
            elif command == "/cost":
                requested = argument.strip().casefold()
                if not requested:
                    self._write(
                        self._label(
                            f"Режим расходов: [#d4b676]{self.run_cost_profile}[/]. "
                            "Используйте /cost economy или /cost balanced.",
                            f"Run cost profile: [#d4b676]{self.run_cost_profile}[/]. "
                            "Use /cost economy or /cost balanced.",
                        )
                    )
                elif requested in {"balanced", "economy"}:
                    self.run_cost_profile = requested
                    _save_preferences(run_cost_profile=requested)
                    self._refresh_status()
                    self._write_notice(
                        self._label(
                            "Economy включён: модель, Effort и качественные лимиты не меняются; "
                            "KaroX убирает точные повторы и использует prompt cache."
                            if requested == "economy"
                            else "Обычный режим включён. Модель и Effort не изменены.",
                            "Economy enabled: model, Effort, and quality limits stay unchanged; "
                            "KaroX removes exact duplication and uses prompt caching."
                            if requested == "economy"
                            else "Balanced mode enabled. Model and Effort are unchanged.",
                        ),
                        "success",
                    )
                else:
                    self._write_notice(
                        self._label(
                            "Формат: /cost economy или /cost balanced",
                            "Usage: /cost economy or /cost balanced",
                        ),
                        "error",
                    )
            elif command == "/memory":
                requested = argument.strip()
                usage = self._label(
                    "Формат: /memory [user|project|workstream|session] | "
                    "show ID | remember [scope] ТЕКСТ | edit ID ТЕКСТ | "
                    "forget ID",
                    "Usage: /memory [user|project|workstream|session] | "
                    "show ID | remember [scope] TEXT | edit ID TEXT | "
                    "forget ID",
                )
                # Local import for the same reason as /map: pay for the
                # subsystem when it is used, not at TUI startup.
                from .memory import (
                    KaroXMemory,
                    MemoryError,
                    MemoryKind,
                    MemoryScope,
                )
                from .paths import session_dir

                memory = KaroXMemory(session_dir() / "memory")
                scope_names = ("user", "project", "workstream", "session")

                def memory_scope_pair(name: str) -> tuple[Any, str]:
                    # Mirrors the hosted runtime's convention exactly, so the
                    # TUI reads and writes the same files the bridge does.
                    if name == "user":
                        return MemoryScope.USER, "default"
                    if name == "project":
                        from .project_registry import _generated_project_id

                        return (
                            MemoryScope.PROJECT,
                            _generated_project_id(self.repository),
                        )
                    if name == "workstream":
                        return MemoryScope.WORKSTREAM, "default"
                    return (
                        MemoryScope.SESSION,
                        self.active_session or "default",
                    )

                def render_entry_line(entry: Any) -> str:
                    text = entry.content.replace("\n", " ")
                    if len(text) > 70:
                        text = text[:69] + "…"
                    marker = (
                        ""
                        if entry.validation == "valid"
                        else f" [#d47a6a]{entry.validation.upper()}[/]"
                    )
                    return (
                        f"[#d4b676]{entry.entry_id}[/] "
                        f"{entry.scope.value}/{entry.kind.value}"
                        f"{marker} — {text}"
                    )

                parts = requested.split(maxsplit=1)
                action = parts[0].lower() if parts else ""
                remainder = parts[1].strip() if len(parts) > 1 else ""
                try:
                    if not requested or action in scope_names:
                        wanted = (
                            None
                            if not requested
                            else MemoryScope(action)
                        )
                        entries = memory.entries(scope=wanted)
                        if not entries:
                            self._write(
                                self._label(
                                    "Память пуста для этого раздела.",
                                    "Memory is empty for this view.",
                                )
                                + f" {usage}"
                            )
                        else:
                            shown = entries[:20]
                            lines = [
                                render_entry_line(entry) for entry in shown
                            ]
                            if len(entries) > len(shown):
                                lines.append(
                                    self._label(
                                        f"…и ещё {len(entries) - len(shown)}.",
                                        f"…and {len(entries) - len(shown)} more.",
                                    )
                                )
                            lines.append(usage)
                            self._write("\n".join(lines))
                    elif action == "show" and remainder:
                        entry = memory.find(remainder.split()[0])
                        if entry is None:
                            self._write_notice(
                                self._label(
                                    "Нет записи с таким id.",
                                    "No memory entry with that id.",
                                ),
                                "error",
                            )
                        else:
                            details = [
                                render_entry_line(entry),
                                f"content: {entry.content}",
                                (
                                    f"provenance: {entry.provenance} · "
                                    f"confidence: {entry.confidence} · "
                                    f"validation: {entry.validation}"
                                ),
                            ]
                            if entry.source_path:
                                details.append(
                                    f"source: {entry.source_path} "
                                    f"({(entry.source_sha256 or '')[:16]})"
                                )
                            self._write("\n".join(details))
                    elif action == "remember" and remainder:
                        scope_word = remainder.split(maxsplit=1)
                        if (
                            scope_word
                            and scope_word[0].lower() in scope_names
                            and len(scope_word) > 1
                        ):
                            scope, scope_id = memory_scope_pair(
                                scope_word[0].lower()
                            )
                            text = scope_word[1]
                        else:
                            scope, scope_id = memory_scope_pair("user")
                            text = remainder
                        entry = memory.remember(
                            scope=scope,
                            scope_id=scope_id,
                            kind=MemoryKind.NOTE,
                            content=text,
                            provenance="user-tui",
                            confidence=1.0,
                        )
                        self._write_notice(
                            self._label(
                                f"Записано: {entry.entry_id}",
                                f"Remembered: {entry.entry_id}",
                            ),
                            "success",
                        )
                    elif action == "edit" and len(remainder.split()) > 1:
                        entry_id, new_text = remainder.split(maxsplit=1)
                        entry = memory.edit(
                            entry_id=entry_id, content=new_text
                        )
                        self._write_notice(
                            self._label(
                                f"Обновлено: {entry.entry_id}",
                                f"Updated: {entry.entry_id}",
                            ),
                            "success",
                        )
                    elif action == "forget" and remainder:
                        removed = memory.forget_entry(remainder.split()[0])
                        if removed:
                            self._write_notice(
                                self._label("Забыто.", "Forgotten."),
                                "success",
                            )
                        else:
                            self._write_notice(
                                self._label(
                                    "Нет записи с таким id.",
                                    "No memory entry with that id.",
                                ),
                                "error",
                            )
                    else:
                        self._write_notice(usage, "error")
                except MemoryError as exc:
                    self._write_notice(str(exc), "error")
            elif command == "/map":
                requested = argument.strip().lower()
                usage = self._label(
                    "Формат: /map [status|preview [уровень]|refresh|"
                    "low|medium|high|extra-high|ultra]",
                    "Usage: /map [status|preview [level]|refresh|"
                    "low|medium|high|extra-high|ultra]",
                )
                # Imported here, not at module top: the map service pulls the
                # repository-context engine, and TUI startup must not pay for
                # it before the command is actually used.
                from .map_service import (
                    MapService,
                    default_map_level,
                    normalize_map_level,
                    render_preview,
                    render_status,
                )

                parts = requested.split()
                action = parts[0] if parts else "status"
                if action == "status":
                    try:
                        status = MapService(self.repository).status()
                    except Exception as exc:
                        self._write_notice(
                            f"Map status failed: {type(exc).__name__}", "error"
                        )
                    else:
                        self._write(
                            render_status(status, self.language) + f"\n{usage}"
                        )
                elif action == "preview":
                    raw_level = (
                        parts[1]
                        if len(parts) > 1
                        else default_map_level(self.effort_level)
                    )
                    try:
                        level = normalize_map_level(raw_level)
                    except ValueError:
                        self._write_notice(usage, "error")
                    else:
                        try:
                            preview = MapService(self.repository).preview(level)
                        except Exception as exc:
                            self._write_notice(
                                f"Map preview failed: {type(exc).__name__}",
                                "error",
                            )
                        else:
                            self._write(render_preview(preview, self.language))
                else:
                    if action == "refresh":
                        try:
                            existing = MapService(self.repository).load()
                        except Exception:
                            existing = None
                        raw_level = (
                            str(existing.get("level"))
                            if existing
                            else default_map_level(self.effort_level)
                        )
                    else:
                        raw_level = action
                    try:
                        level = normalize_map_level(raw_level)
                    except ValueError:
                        self._write_notice(usage, "error")
                    else:
                        try:
                            map_service = MapService(self.repository)
                        except OSError:
                            self._write_notice(
                                self._label(
                                    "Проект недоступен для карты.",
                                    "The project is not reachable for mapping.",
                                ),
                                "error",
                            )
                        else:
                            self._write(
                                self._label(
                                    f"[#b7c2b0]Строю карту проекта "
                                    f"(уровень {level})…[/]",
                                    f"[#b7c2b0]Building the project map "
                                    f"(level {level})…[/]",
                                )
                            )

                            def build_map() -> None:
                                try:
                                    state = map_service.build(level)
                                except Exception as exc:
                                    self.call_from_thread(
                                        self._write_notice,
                                        "Map build failed: "
                                        f"{type(exc).__name__}",
                                        "error",
                                    )
                                else:
                                    seconds = round(
                                        float(state.get("duration_ms") or 0)
                                        / 1000,
                                        1,
                                    )
                                    kind = (
                                        "warm" if state.get("warm") else "cold"
                                    )
                                    self.call_from_thread(
                                        self._write_notice,
                                        self._label(
                                            f"Карта готова: уровень {level}, "
                                            f"файлов "
                                            f"{state.get('files_scanned')}, "
                                            f"{seconds} с ({kind}).",
                                            f"Map ready: level {level}, "
                                            f"{state.get('files_scanned')} "
                                            f"files, {seconds}s ({kind}).",
                                        ),
                                        "success",
                                    )

                            self.run_worker(
                                build_map,
                                thread=True,
                                exclusive=True,
                                group="map",
                            )
            elif command == "/mode":
                requested = argument.strip()
                usage = self._label(
                    "Формат: /mode build|plan|ideate",
                    "Usage: /mode build|plan|ideate",
                )
                if not requested:
                    self._write(
                        f"Mode: [#d4b676]{self.agent_mode}[/]. "
                        f"{mode_summary(self.agent_mode, self.language)} {usage}"
                    )
                else:
                    try:
                        mode = normalize_mode(requested)
                    except ModeError:
                        self._write_notice(usage, "error")
                    else:
                        # The explicit command is the user gate: a mode never
                        # changes silently, and /mode build is the transition
                        # that re-enables production mutation.
                        previous = self.agent_mode
                        self.agent_mode = mode
                        _save_agent_mode(mode)
                        self._refresh_status()
                        notice = mode_summary(mode, self.language)
                        if previous != mode and mode == DEFAULT_MODE:
                            notice += self._label(
                                " Изменения кода снова идут по обычной политике.",
                                " Production-code changes follow the normal"
                                " policy again.",
                            )
                        self._write_notice(notice, "success")
            elif command == "/effort":
                requested = argument.strip()
                usage = self._label(
                    "Формат: /effort auto|low|medium|high|extra-high|ultra",
                    "Usage: /effort auto|low|medium|high|extra-high|ultra",
                )
                if not requested:
                    if self.effort_level == AUTO_EFFORT:
                        detail = self._label(
                            "AUTO выбирает уровень по сигналам задачи: "
                            "названные файлы, зависимости карты, churn, "
                            "риск, режим. Уровень и причины - в отчёте.",
                            "AUTO picks the level from task signals: named "
                            "files, map dependency breadth, churn, risk, "
                            "and mode. The run reports the level and why.",
                        )
                    else:
                        detail = effort_summary(self.effort_level, self.language)
                    self._write(
                        f"Effort: [#d4b676]{self.effort_level}[/]. "
                        f"{detail} {usage}"
                    )
                else:
                    try:
                        level = normalize_effort(requested)
                    except ValueError:
                        self._write_notice(usage, "error")
                    else:
                        self.effort_level = level
                        _save_effort_level(level)
                        self._refresh_status()
                        if level == AUTO_EFFORT:
                            notice = self._label(
                                "Effort AUTO включён: уровень выбирается "
                                "по сигналам задачи при отправке.",
                                "Effort AUTO enabled: the level is resolved "
                                "from task signals at submission.",
                            )
                        else:
                            notice = effort_summary(level, self.language)
                        self._write_notice(notice, "success")
            elif command == "/status":
                # Honest subset only: rows appear here as their subsystems
                # become real. No placeholder "Map: n/a" noise.
                selected = _selected_model()
                model = (
                    f"{selected.provider_id}/{selected.model_id}"
                    if selected is not None
                    else _TEXT[self.language]["not_configured"]
                )
                if self.effort_level == AUTO_EFFORT:
                    effort_note = self._label(
                        "AUTO: уровень выбирается по сигналам задачи.",
                        "AUTO: the level is resolved from task signals.",
                    )
                else:
                    effort_note = effort_summary(self.effort_level, self.language)
                if self.agent_busy and self.active_session:
                    task_state = self._label(
                        f"выполняется ({self.active_session})",
                        f"running ({self.active_session})",
                    )
                else:
                    task_state = self._label("простаивает", "idle")
                bridge_state = self.public_endpoint or _TEXT[self.language]["off"]
                rows = [
                    (self._label("Проект", "Project"), str(self.repository)),
                    (self._label("Модель", "Model"), model),
                    (
                        self._label("Режим агента", "Mode"),
                        f"{self.agent_mode} - "
                        f"{mode_summary(self.agent_mode, self.language)}",
                    ),
                    ("Effort", f"{self.effort_level} - {effort_note}"),
                    (
                        self._label("Режим расходов", "Cost profile"),
                        self.run_cost_profile,
                    ),
                    (self._label("Задача", "Task"), task_state),
                    (self._label("Мост", "Bridge"), bridge_state),
                ]
                if self.reasoning_effort:
                    # After Effort: the Mode row above shifted the list by one.
                    rows.insert(
                        4,
                        (
                            self._label("Провайдер-хинт", "Provider hint"),
                            self.reasoning_effort,
                        ),
                    )
                lines = [f"[bold #e0dccc]{self._label('Статус', 'Status')}[/]"]
                lines.extend(
                    f"  [#d4b676]{escape(str(name))}:[/] {escape(str(value))}"
                    for name, value in rows
                )
                self._write("\n".join(lines))
            elif command == "/help":
                lines = [f"[bold #e0dccc]{_TEXT[self.language]['commands']}[/]"]
                lines.extend(
                    f"  [#d4b676]{escape(name)}[/]  [dim]{escape(description)}[/]"
                    for name, description in _commands(self.language).items()
                )
                self._write("\n".join(lines))
            elif command in {"/connect", "/setup"}:
                # The single connection entry point. Not the legacy api/web/both
                # wizard: that asked the user to classify a connection before
                # showing them what already exists.
                self._open_connections(None)
            elif command in DEPRECATED_COMMAND_ALIASES and command != "/bridge":
                # Retired entry points, kept working and kept out of the menu.
                # ``/bridge`` is excluded because ``/bridge stop`` is a real
                # action and is routed by its own branch below.
                self._open_connections(DEPRECATED_COMMAND_ALIASES.get(command))
            elif command == "/browser":
                self.action_session_browser()
            elif command == "/sessions":
                # Phase 2.3: /sessions opens the compact Session Browser by
                # default. The verbose text renderer stays available via
                # --verbose or /session-log for diagnostics.
                if argument.strip() == "--verbose":
                    self._run_inspection(_BACKEND_SLASH[command], command)
                else:
                    self.action_session_browser()
            elif command == "/session-log":
                self._run_inspection(["session", "list", "--json"], "/sessions")
            elif command == "/ask":
                self._run_ask(argument.strip())
            elif command == "/language":
                self.push_screen(
                    LanguageScreen(allow_cancel=True), self._language_selected
                )
            elif command == "/project":
                self._project_command(argument.strip())
            elif command == "/workspace":
                raw_path = argument.strip().strip('"')
                if not raw_path:
                    self.action_workspace()
                    return
                self._switch_workspace(raw_path)
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
                    # A real action, not a screen, so it keeps working verbatim.
                    self._stop_bridge()
                else:
                    self._open_connections(CONNECT_FOCUS_CLIENTS)
            elif command == "/clear":
                self.action_clear_log()
            elif command == "/new":
                self._start_new_task()
            elif command == "/resume":
                self._resume_session(argument.strip())
            elif command == "/compact":
                self._compact_conversation()
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
                suggestion = _suggest_command(command, self.language)
                if suggestion:
                    message += " " + suggestion
                self._write(f"[#e0a3a3]{message}[/]")

        def _workspace_saved_profiles(self) -> tuple[Any, ...]:
            """Saved connections whose approved project set contains the current folder."""
            from .project_registry import ProjectRegistry, ProjectRegistryError
            from .web_bridge_profiles import WebBridgeProfileError, WebBridgeProfileStore

            try:
                current = self.repository.expanduser().resolve(strict=True)
                profiles = WebBridgeProfileStore().list()
            except (OSError, WebBridgeProfileError):
                return ()
            matched: list[Any] = []
            for profile in profiles:
                try:
                    registry = ProjectRegistry.from_profile(
                        repository=profile.repository,
                        projects=profile.projects,
                        default_project_id=profile.default_project_id,
                    )
                    if registry.entry_for_path(current) is not None:
                        matched.append(profile)
                except (OSError, ProjectRegistryError):
                    continue
            return tuple(matched)

        def _workspace_registry(self) -> Any:
            from .project_registry import ProjectRegistry, ProjectRegistryError

            profiles = self._workspace_saved_profiles()
            registry: Optional[ProjectRegistry] = None
            for profile in profiles:
                try:
                    candidate = ProjectRegistry.from_profile(
                        repository=profile.repository,
                        projects=profile.projects,
                        default_project_id=profile.default_project_id,
                    )
                except ProjectRegistryError:
                    continue
                if registry is None:
                    registry = candidate
                    continue
                for entry in candidate.projects:
                    if registry.entry_for_path(entry.path) is None:
                        registry = registry.add(
                            entry.path,
                            project_id=entry.project_id,
                            label=entry.label,
                        )
            if registry is not None:
                return registry

            preferences = _load_preferences()
            raw_projects = preferences.get("workspace_projects", [])
            raw_default = preferences.get("workspace_default_project_id")
            if not isinstance(raw_projects, list):
                raw_projects = []
            default_project_id = raw_default if isinstance(raw_default, str) else None
            try:
                return ProjectRegistry.from_profile(
                    repository=str(self.repository),
                    projects=raw_projects,
                    default_project_id=default_project_id,
                )
            except ProjectRegistryError:
                return ProjectRegistry.single(self.repository)

        def _workspace_project_in_use(self, project_id: str) -> bool:
            """Conservatively refuse removal while a durable workstream is bound."""
            from .task_state import TaskStateStore
            from .web_bridge_launcher import saved_web_bridge_session_candidates

            sessions = SessionStore(session_dir())
            states = TaskStateStore(sessions)
            for profile in self._workspace_saved_profiles():
                for session_id in saved_web_bridge_session_candidates(profile.name):
                    if not sessions.state_path(session_id).exists():
                        continue
                    workstreams: list[Optional[str]] = [None]
                    with contextlib.suppress(Exception):
                        workstreams.extend(states.list_workstreams(session_id))
                    for workstream_id in workstreams:
                        with contextlib.suppress(Exception):
                            state = states.load_optional(
                                session_id,
                                workstream_id=workstream_id,
                            )
                            if state is None:
                                continue
                            project = state.facts.get("project_id")
                            if project is not None and str(project.value) == project_id:
                                return True
            return False

        def _persist_workspace_registry(self, registry: Any) -> None:
            """Persist project routing without restarting or rebinding saved bridges."""
            from dataclasses import replace as _replace
            from .web_bridge_launcher import apply_saved_bridge_profile

            profiles = self._workspace_saved_profiles()
            for profile in profiles:
                updated = _replace(
                    profile,
                    projects=tuple(registry.to_payload()),
                    default_project_id=registry.default_project_id,
                )
                apply_saved_bridge_profile(
                    profile.name,
                    updated,
                    allow_restart=False,
                )
            _save_preferences(
                workspace_projects=registry.to_payload(),
                workspace_default_project_id=registry.default_project_id,
            )

        def action_workspace(self) -> None:
            """Ctrl+W: manage approved project folders for future tasks."""
            from .tui_workspace import WorkspaceManagerScreen

            self.push_screen(
                WorkspaceManagerScreen(
                    self._workspace_registry(),
                    self.repository,
                    self.language,
                ),
                self._workspace_manager_result,
            )

        def _workspace_manager_result(self, action: Any) -> None:
            if action is None:
                self.query_one("#composer", Input).focus()
                return
            from .project_registry import ProjectRegistryError

            try:
                registry = self._workspace_registry()
                if action.action == "add":
                    if action.path is None:
                        raise ProjectRegistryError("project path is missing")
                    registry = registry.add(action.path)
                    self._persist_workspace_registry(registry)
                    self._write_notice(
                        self._label("Папка добавлена без перезапуска bridge.", "Workspace added without restarting the bridge."),
                        "success",
                    )
                    self.action_workspace()
                    return
                if action.project_id is None:
                    raise ProjectRegistryError("project selection is missing")
                entry = registry.get(action.project_id)
                if action.action == "use":
                    self._switch_workspace(entry.path)
                    return
                if action.action == "default":
                    registry = registry.with_default(entry.project_id)
                    self._persist_workspace_registry(registry)
                    self._write_notice(
                        self._label(
                            f"По умолчанию для новых задач: {entry.label}",
                            f"Default for new tasks: {entry.label}",
                        ),
                        "success",
                    )
                    self.action_workspace()
                    return
                if action.action == "remove":
                    for profile in self._workspace_saved_profiles():
                        if profile.repository and os.path.normcase(str(Path(profile.repository).resolve())) == os.path.normcase(entry.path):
                            raise ProjectRegistryError(
                                "the durable session anchor cannot be removed; keep it approved or recreate the connection"
                            )
                    if self.agent_busy and os.path.normcase(str(self.repository)) == os.path.normcase(entry.path):
                        raise ProjectRegistryError("the current running task still uses this project")
                    if self._workspace_project_in_use(entry.project_id):
                        raise ProjectRegistryError("a durable workstream is still bound to this project")
                    registry = registry.remove(entry.project_id)
                    self._persist_workspace_registry(registry)
                    if os.path.normcase(str(self.repository)) == os.path.normcase(entry.path):
                        target = registry.default
                        if target is not None:
                            self._switch_workspace(target.path)
                    self._write_notice(
                        self._label("Папка удалена из разрешённых.", "Workspace removed from the approved list."),
                        "success",
                    )
                    self.action_workspace()
                    return
                raise ProjectRegistryError(f"unknown workspace action: {action.action}")
            except (OSError, RuntimeError, ProjectRegistryError) as exc:
                self._write_notice(str(exc), "error")
                self.action_workspace()

        def _project_command(self, target: str) -> None:
            """/project: the workspace act, typed.

            Without an argument this opens the same manager Ctrl+W opens -- one
            screen owns the approved list. With an argument it switches to an
            approved project by id or approved path, and an unknown target falls
            through to ``_switch_workspace`` so /project and /workspace cannot
            disagree about which folders are safe.
            """

            if not target:
                self.action_workspace()
                return
            resolved: Optional[str] = None
            try:
                resolved = _resolve_project_target(
                    self._workspace_registry(), target
                )
            except Exception:
                resolved = None
            self._switch_workspace(resolved if resolved is not None else target)

        def _workspace_selected(self, path: Optional[str]) -> None:
            """Legacy picker callback retained for direct tests and old extensions."""
            if path is None:
                self.query_one("#composer", Input).focus()
                return
            self._switch_workspace(path)

        def _switch_workspace(self, raw_path: str) -> bool:
            """Switch future local tasks to one validated folder.

            A running agent keeps ownership of the repository it started in, so
            switching is refused while work is active. Durable saved bridges keep
            their original session anchor and may route new workstreams to another
            user-approved project without rebinding or restarting.
            """
            if self.agent_busy:
                self._write_notice(
                    self._label(
                        "Сначала дождитесь завершения или остановите текущую задачу.",
                        "Wait for the current task to finish or stop it first.",
                    ),
                    "warning",
                )
                return False

            candidate = Path(str(raw_path).strip().strip('"')).expanduser()
            if not candidate.is_dir():
                self._write_notice(
                    self._label(
                        "Такой папки не существует.",
                        "That folder does not exist.",
                    ),
                    "error",
                )
                return False
            reason = _unsafe_workspace_reason(candidate, self.language)
            if reason:
                self._write_notice(reason, "error")
                return False

            resolved = candidate.resolve()
            previous = self.repository
            with contextlib.suppress(Exception):
                _remember_workspace(previous)
            self.repository = resolved
            self.verification = _default_verification(resolved)
            self.active_session = None
            self._history_seen = 0
            self._history_fingerprint = None
            self._content_seen = 0
            with contextlib.suppress(Exception):
                _remember_workspace(resolved)
            self._refresh_status()
            self._write_notice(
                self._label(
                    f"Рабочая папка: {resolved}",
                    f"Workspace: {resolved}",
                ),
                "success",
            )
            if self._workspace_saved_profiles():
                self._write_notice(
                    self._label(
                        "Сохранённые подключения сохраняют URL/сессию и маршрутизируют новые workstream в эту папку без перезапуска.",
                        "Saved connections keep their URL/session and route new workstreams to this folder without a restart.",
                    ),
                    "success",
                )
            elif self.bridge_launch is not None or self.public_endpoint:
                self._write_notice(
                    self._label(
                        "Это ad-hoc подключение остаётся привязано к исходной папке до явного перезапуска.",
                        "This ad-hoc bridge stays bound to its original workspace until explicitly restarted.",
                    ),
                    "warning",
                )
            return True

        def action_onboarding(self) -> None:
            """Ctrl+S. The same hub the command opens, not a second root.

            This used to push ``ConnectionChoiceScreen`` -- the api/web/both
            wizard -- which left the product with two competing roots for one
            scenario: `/connect` showed what exists, Ctrl+S demanded a
            classification first. Whichever one a person happened to use
            decided what they believed was connected. The key binding now goes
            where the command goes, and the legacy screen is off every
            production path.
            """

            self._open_connections(None)

        def action_connect(self) -> None:
            """Open the universal Connections hub. The one connection entry point."""

            self._open_connections(None)

        def action_home(self) -> None:
            """The chat is the home screen; do not open a second dashboard root."""

            self.query_one("#composer", Input).focus()

        def get_system_commands(self, screen: Any) -> Iterable[Any]:
            """The one curated command surface behind Ctrl+P.

            Textual's palette normally collects bindings, but this app hides
            its bindings from the footer, so the palette would otherwise offer
            nothing but system entries. Every entry here is one existing action,
            carries the key that also runs it, and appears in exactly one place.
            The default system entries stay, minus the duplicate Quit.
            """

            english = self.language != "ru"
            yield SystemCommand(
                "Model and Effort" if english else "Модель и Effort",
                "Ctrl+G — switch model and reasoning effort",
                self.action_model,
            )
            yield SystemCommand(
                "Usage & Cost",
                "/usage — tokens, cache and spend",
                self.action_usage,
            )
            yield SystemCommand(
                "Connections" if english else "Подключения",
                "Ctrl+S — one hub for models and services",
                self.action_onboarding,
            )
            yield SystemCommand(
                "Sessions" if english else "Сессии",
                "Ctrl+O — task sessions",
                self.action_session_browser,
            )
            yield SystemCommand(
                "Project folder" if english else "Папка проекта",
                "Ctrl+W — switch the working project",
                self.action_workspace,
            )
            yield SystemCommand(
                "Clear chat" if english else "Очистить чат",
                "Ctrl+L — clear the conversation",
                self.action_clear_log,
            )
            yield SystemCommand(
                "Copy" if english else "Копировать",
                "Ctrl+Shift+C — copy the selection",
                self.action_copy_selection,
            )
            yield SystemCommand(
                "Quit" if english else "Выйти",
                "Ctrl+Q — exit KaroX",
                self.action_quit,
            )
            for command in super().get_system_commands(screen):
                if command.title == "Quit":
                    continue
                yield command

        def action_model(self) -> None:
            """Open the fast model picker used by Ctrl+G and /model."""

            if self.agent_busy:
                self._write_notice(
                    self._label(
                        "Модель и Effort можно менять после завершения текущей задачи.",
                        "Model and Effort can be changed after the current task finishes.",
                    ),
                    "warning",
                )
                return

            from .tui_dashboard import ModelPickerScreen

            self.push_screen(
                ModelPickerScreen(self.language, effort=self.reasoning_effort),
                self._model_picker_done,
            )

        def _refresh_models_command(self) -> None:
            """/model refresh: rediscover the active provider's catalog.

            Runs the same in-place discovery as the provider details screen,
            so the command UX and the button cannot drift apart. Honest
            counts, no invented capabilities, errors redacted.
            """

            selected = _selected_model()
            if selected is None:
                self._write_notice(
                    self._label(
                        "\u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u0432\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043c\u043e\u0434\u0435\u043b\u044c: /model \u0438\u043b\u0438 /connect.",
                        "Select a model first: /model or /connect.",
                    ),
                    "warning",
                )
                return
            provider_id = selected.provider_id
            self._write_notice(
                self._label(
                    f"\u041e\u0431\u043d\u043e\u0432\u043b\u044f\u044e \u043a\u0430\u0442\u0430\u043b\u043e\u0433 \u043c\u043e\u0434\u0435\u043b\u0435\u0439 {provider_id}\u2026",
                    f"Refreshing the {provider_id} model catalog\u2026",
                ),
                "info",
            )

            def execute() -> None:
                from .tui_connections import discover_models_for_provider

                try:
                    summary = discover_models_for_provider(
                        _provider_controller(), provider_id
                    )
                except Exception as exc:
                    self.call_from_thread(
                        self._write_notice, str(redact(exc)), "error"
                    )
                    return
                added = len(summary.get("added") or [])
                discovered = summary.get("discovered", 0)
                self.call_from_thread(
                    self._write_notice,
                    self._label(
                        f"\u041d\u0430\u0439\u0434\u0435\u043d\u043e \u043c\u043e\u0434\u0435\u043b\u0435\u0439: {discovered}, \u043d\u043e\u0432\u044b\u0445 \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d\u043e: {added}.",
                        f"Discovered {discovered} models, {added} new added.",
                    ),
                    "success",
                )

            self.run_worker(
                execute, thread=True, exclusive=True, group="model-refresh"
            )

        def _model_picker_done(self, choice: Optional[str]) -> None:
            from .tui_dashboard import MODEL_PICKER_CONNECT

            if choice is None:
                self.query_one("#composer", Input).focus()
                return
            if choice == MODEL_PICKER_CONNECT:
                self._open_connections(CONNECT_FOCUS_MODELS)
                return
            if choice.startswith("effort:"):
                value = choice.split(":", 1)[1] or "auto"
                self.reasoning_effort = None if value == "auto" else value
                _save_preferences(reasoning_effort=self.reasoning_effort)
                self._refresh_status()
                self._write_notice(f"Effort: {value}", "success")
                self.query_one("#composer", Input).focus()
                return
            prefix, provider_id, model_id = (choice.split(":", 2) + ["", "", ""])[:3]
            if prefix != "model" or not provider_id or not model_id:
                self.query_one("#composer", Input).focus()
                return
            try:
                _registry().select_model(provider_id, model_id)
            except Exception as exc:
                self._write_notice(str(redact(exc)), "error")
            else:
                self._refresh_status()
                self._write_notice(f"Model: {provider_id}/{model_id}", "success")
            self.query_one("#composer", Input).focus()

        def action_usage(self) -> None:
            """Open exact persisted Usage & Cost analytics."""

            from .tui_dashboard import UsageCostScreen

            selected = _selected_model()
            model_text = (
                f"{selected.provider_id}/{selected.model_id}"
                if selected is not None
                else self._label("модель не выбрана", "model not selected")
            )
            self.push_screen(
                UsageCostScreen(
                    self.language,
                    session_id=self.active_session,
                    model_text=model_text,
                    effort=self.reasoning_effort,
                )
            )

        def _open_connections(self, focus: Optional[str] = None) -> None:
            """Open the single Connections screen, optionally on one section.

            One entry point, one root flow. ``/connect`` and every retired alias
            arrive here; ``focus`` only chooses which section is already open, so
            a person who typed ``/providers`` lands on AI models without there
            being a second hub that could disagree with this one about what is
            connected.

            The legacy ``ConnectionChoiceScreen`` api/web/both wizard is no longer
            on any production path: it asked the user to classify the connection
            before they had seen what exists, which is the question the hub
            answers for them.
            """

            screens = self._connections_screens_cached()
            if focus == CONNECT_FOCUS_MODELS:
                self.push_screen(
                    screens["ModelProvidersScreen"](self.language),
                    self._connections_screen_closed,
                )
                return
            if focus == CONNECT_FOCUS_CLIENTS:
                self.push_screen(
                    screens["McpClientsScreen"](self.language),
                    self._connections_screen_closed,
                )
                return
            # B5. Where the cursor lands. `select` is the record the user was
            # just on; `after_delete` is the record that no longer exists, and
            # the hub uses it to land on the neighbour rather than resetting to
            # the top of the list.
            select, self._hub_select = self._hub_select, None
            after_delete, self._hub_select_after_delete = (
                self._hub_select_after_delete,
                None,
            )
            self.push_screen(
                screens["ConnectionHubScreen"](
                    self.language,
                    select=select,
                    after_delete=after_delete,
                ),
                self._connection_hub_done,
            )

        def _connections_screens_cached(self):
            cached = getattr(self, "_connections_screens", None)
            if cached is None:
                from .tui_connections import build_connections_screens

                cached = build_connections_screens(self)
                self._connections_screens = cached
            return cached

        def _connection_hub_done(self, choice: Optional[str]) -> None:
            """Where a hub row or an add action goes next.

            The hub speaks semantic ids, never list positions: an add action is
            named for what it adds, and a saved row carries its own family and
            the identity its store assigned. Reordering the screen therefore
            cannot silently repoint an entry at a different flow.

            Every destination comes back through ``_connection_hub_reopen``, so
            the user lands in the hub they started from rather than being left
            in the chat holding a half-finished thought.
            """

            from .tui_connections import (
                HUB_ADD_MODEL,
                HUB_ADD_OTHER,
                HUB_ADD_SERVICE,
                HUB_FAMILY_AI,
                HUB_FAMILY_SERVICE,
                HUB_MANAGE_CONNECTIONS,
                HUB_MANAGE_MODELS,
            )

            if choice is None:
                self.query_one("#composer", Input).focus()
                return
            screens = self._connections_screens_cached()
            if choice == HUB_ADD_MODEL:
                self._connect_return_to_hub = True
                self.call_after_refresh(self.action_provider_preset)
                return
            if choice == HUB_ADD_SERVICE:
                # B3. The three known services get the standard flow, not the
                # generic MCP client form: a person choosing "ChatGPT" is not
                # asking to configure a transport.
                self.call_after_refresh(
                    lambda: self.push_screen(
                        screens["ServicePickerScreen"](self.language),
                        self._service_chosen,
                    )
                )
                return
            if choice == HUB_ADD_OTHER:
                self.call_after_refresh(
                    lambda: self.push_screen(
                        screens["McpClientsScreen"](self.language),
                        self._connection_hub_reopen,
                    )
                )
                return
            if choice == HUB_MANAGE_MODELS:
                self.call_after_refresh(
                    lambda: self.push_screen(
                        screens["ModelProvidersScreen"](self.language),
                        self._connection_hub_reopen,
                    )
                )
                return
            if choice == HUB_MANAGE_CONNECTIONS:
                self.call_after_refresh(
                    lambda: self.push_screen(
                        screens["McpClientsScreen"](self.language),
                        self._connection_hub_reopen,
                    )
                )
                return
            # A saved row. The family prefix decides which surface owns it; the
            # identity after the colon is that store's own and is not
            # reinterpreted here.
            family, _, identity = choice.partition(":")
            if family == HUB_FAMILY_AI and identity:
                # B5. Enter on a saved row opens *that record*, not the list it
                # belongs to. Pushing `ModelProvidersScreen` here was the defect:
                # the user pointed at one provider and got every provider, then
                # had to find it again -- and there was nowhere for edit, disable
                # or delete to live, because no screen was about one record.
                self._open_connection_detail("provider", identity)
                return
            if family == HUB_FAMILY_SERVICE and identity:
                # B5. Same rule for a saved service. B3's connect screen is the
                # right thing when *adding* one; an existing record opens the
                # management view, which can reach the connect flow through Edit.
                self._open_connection_detail("service", identity)
                return
            self.call_after_refresh(
                lambda: self.push_screen(
                    screens["McpClientsScreen"](self.language),
                    self._connection_hub_reopen,
                )
            )

        def _saved_record_exists(self, kind: str, identity: str) -> bool:
            """Whether the store that owns this identity still has it.

            B5. The hub is a snapshot. A row can be rendered and then deleted --
            by a CLI, by another window, by the previous keystroke -- before
            Enter reaches it. Resolving through the owning store *before*
            opening anything is what keeps a stale selection from producing a
            detail screen full of blanks that offers to verify and delete a
            record which is not there.

            No guessing either way: an identity the store cannot resolve is
            treated as absent rather than assumed into a preset.
            """

            if not identity:
                return False
            try:
                if kind == "provider":
                    from .registry import ProviderRegistry
                    from .paths import config_dir

                    ProviderRegistry(
                        config_dir() / "vnext" / "providers.json"
                    ).provider(identity)
                    return True
                from .connection_controller import connection_controller

                connection_controller().get(identity)
                return True
            except Exception:
                return False

        def _open_connection_detail(self, kind: str, identity: str) -> None:
            """Open the management view for one saved record.

            The screen reads the registries directly, so this passes an identity
            and nothing else: no snapshot of the row is handed over that could
            already be stale by the time it renders.

            A record that no longer exists falls back to the generic list rather
            than opening an empty detail screen. The hub also re-reads its
            sources when it next opens, so the vanished row disappears from it.
            """

            screens = self._connections_screens_cached()
            if not self._saved_record_exists(kind, identity):
                self.call_after_refresh(
                    lambda: self.push_screen(
                        screens["McpClientsScreen"](self.language),
                        self._connection_hub_reopen,
                    )
                )
                return
            self._connection_detail_id = f"{kind}:{identity}"
            self.call_after_refresh(
                lambda: self.push_screen(
                    screens["ConnectionDetailScreen"](
                        self.language, kind=kind, identity=identity
                    ),
                    self._connection_detail_closed,
                )
            )

        def _connection_detail_closed(self, result: Optional[str]) -> None:
            """Back to the hub, on the right row.

            Three outcomes, and the selection rule differs for each. A plain
            close or a change lands back on the same record, because that is
            where the user was. A delete cannot: the row is gone, so the hub
            picks its neighbour rather than snapping to the top of the list.
            An edit hands off to the form that owns the record.
            """

            self._refresh_status()
            previous = getattr(self, "_connection_detail_id", None)
            self._connection_detail_id = None
            if result and result.startswith("edit:"):
                _, _, rest = result.partition(":")
                kind, _, identity = rest.partition(":")
                self._edit_saved_connection(kind, identity)
                return
            if result and result.startswith("deleted:"):
                # The hub worked out the neighbour while the row still existed;
                # nothing can reconstruct it now that the record is gone.
                self._hub_select_after_delete = getattr(self, "_hub_neighbour", None)
                self._hub_neighbour = None
                self.call_after_refresh(lambda: self._open_connections(None))
                return
            self._hub_select_after_delete = None
            self._hub_select = previous
            self.call_after_refresh(lambda: self._open_connections(None))

        def _edit_saved_connection(self, kind: str, identity: str) -> None:
            """Send an existing record to the form it was created with.

            No second editor and no new record: the provider wizard is opened
            on the saved provider's own id, so saving updates that provider
            rather than creating a near-duplicate beside it.
            """

            if kind == "provider":
                self.open_provider_editor(identity)
                return
            self._open_service_flow(connection_id=identity)

        def open_provider_editor(self, provider_id: str) -> None:
            """Open the setup form on a saved provider. One explicit path.

            B5.1. What this replaces: setting a `_edit_provider_id` attribute
            that nothing ever read, and then opening the *preset picker*. The
            attribute was inert, so "Edit OpenRouter" asked the user to choose a
            provider type from scratch and would have written a second record.
            A flag nobody consumes is not a feature with a missing wire; it is
            the absence of the feature.

            The record is loaded here and handed to the screen, so there is no
            hidden state between the two and no picker in front of them.
            """

            from .tui_connections import HUB_FAMILY_AI

            existing = load_provider_edit(provider_id)
            if existing is None:
                # Vanished between the hub render and this keystroke. Back to
                # the hub rather than an edit form for nothing.
                self._hub_select = None
                self.call_after_refresh(lambda: self._open_connections(None))
                return
            self._connect_return_to_hub = True
            self._hub_select = f"{HUB_FAMILY_AI}:{provider_id}"
            self.call_after_refresh(
                lambda: self.push_screen(
                    ProviderSetupScreen(self.language, existing=existing),
                    self._provider_setup_done,
                )
            )

        def _service_chosen(self, preset_id: Optional[str]) -> None:
            """A service was picked, or the picker was dismissed.

            Esc goes back one level -- to the hub -- rather than to the chat, so
            changing your mind about which service costs one key rather than a
            re-typed command.
            """

            if not preset_id:
                self.call_after_refresh(lambda: self._open_connections(None))
                return
            if preset_id == "notion":
                self._start_notion_workspace_connect()
                return
            self._open_service_flow(preset_id=preset_id)

        def _start_notion_workspace_connect(self) -> None:
            """Connect KaroX to Notion's official hosted MCP through OAuth."""

            self._set_activity(
                self._label("Подключаю Notion через OAuth…", "Connecting Notion with OAuth…")
            )
            self._write_notice(
                self._label(
                    "Notion подключается напрямую к KaroX. Tailscale и Bearer-ключ не нужны. "
                    "Если OAuth ещё не подтверждён, откроется браузер Notion.",
                    "Notion connects directly to KaroX. Tailscale and a Bearer key are not needed. "
                    "If OAuth is not authorized yet, the Notion browser page will open.",
                ),
                "info",
            )
            _start_worker(
                self._notion_workspace_connect_worker,
                name="karox-notion-oauth-connect",
            )

        def _notion_workspace_connect_worker(self) -> None:
            try:
                from .mcp_client import McpClient, McpRegistry, mcp_selection
                from .mcp_oauth import McpOAuthStorage, authorize_mcp_oauth
                from .notion_mcp import ensure_notion_mcp_record
                from .paths import config_dir, session_dir

                registry = McpRegistry(config_dir() / "vnext" / "mcp-servers.json")
                server = ensure_notion_mcp_record(registry)
                storage = McpOAuthStorage(server.server_id)
                if not storage.available():
                    authorize_mcp_oauth(
                        server.server_id,
                        server.url or "",
                        timeout_seconds=max(120.0, server.timeout_seconds),
                    )
                tools = McpClient(registry).discover_record(server, Path(self.repository))

                allowed = 0
                session_id = self.active_session
                if session_id:
                    store = SessionStore(session_dir())
                    if store.state_path(session_id).exists():
                        decisions = {
                            tool.remote_name: ("allow" if tool.read_only else "ask")
                            for tool in tools
                        }
                        with store.mutate(
                            session_id,
                            f"notion-oauth-select-{os.getpid()}",
                            ttl_seconds=10.0,
                        ) as session:
                            store.validate_repository(session, Path(self.repository))
                            previous = next(
                                (
                                    item
                                    for item in session.mcp_servers
                                    if isinstance(item, dict)
                                    and item.get("server_id") == server.server_id
                                ),
                                None,
                            )
                            selection = mcp_selection(
                                server,
                                tools,
                                decisions,
                                previous=previous,
                            )
                            session.mcp_servers = [
                                item
                                for item in session.mcp_servers
                                if not (
                                    isinstance(item, dict)
                                    and item.get("server_id") == server.server_id
                                )
                            ]
                            session.mcp_servers.append(selection)
                            session.mcp_servers.sort(
                                key=lambda item: str(item.get("server_id", ""))
                                if isinstance(item, dict)
                                else ""
                            )
                        allowed = sum(1 for tool in tools if tool.read_only)
                self.call_from_thread(
                    self._notion_workspace_connected,
                    len(tools),
                    allowed,
                )
            except Exception as exc:
                self.call_from_thread(
                    self._notion_workspace_connect_failed,
                    f"{type(exc).__name__}: {exc}",
                )

        def _notion_workspace_connected(self, tool_count: int, allowed: int) -> None:
            self._set_activity("", "idle")
            suffix = (
                self._label(
                    f" Для текущей сессии автоматически разрешены {allowed} безопасных read-only tools.",
                    f" {allowed} safe read-only tools were enabled for the current session.",
                )
                if allowed
                else ""
            )
            self._write_notice(
                self._label(
                    f"Notion подключён: найдено {tool_count} MCP-инструментов.",
                    f"Notion connected: {tool_count} MCP tools discovered.",
                )
                + suffix,
                "success",
            )
            self.call_after_refresh(lambda: self._open_connections(None))

        def _notion_workspace_connect_failed(self, detail: str) -> None:
            self._set_activity("", "idle")
            self._write_notice(
                self._label(
                    "Не удалось подключить Notion: ",
                    "Could not connect Notion: ",
                )
                + escape(detail),
                "error",
            )
            self.call_after_refresh(lambda: self._open_connections(None))

        def _open_service_flow(
            self,
            *,
            preset_id: Optional[str] = None,
            connection_id: Optional[str] = None,
        ) -> None:
            """Open the standard service screen for a preset or a saved row.

            Nothing is started here. The screen reads the existing connection
            state, so opening it cannot disturb a bridge that is already serving
            the URL the user pasted into the service.
            """

            screens = self._connections_screens_cached()
            resolved = preset_id
            if resolved is None and connection_id:
                resolved = self._service_preset_for(connection_id)
            if resolved is None:
                self.call_after_refresh(
                    lambda: self.push_screen(
                        screens["McpClientsScreen"](self.language),
                        self._connection_hub_reopen,
                    )
                )
                return
            self.call_after_refresh(
                lambda: self.push_screen(
                    screens["ServiceConnectScreen"](
                        self.language,
                        preset_id=resolved,
                        connection_id=connection_id,
                    ),
                    self._connection_hub_reopen,
                )
            )

        def _service_preset_for(self, connection_id: str) -> Optional[str]:
            """Which preset a saved connection belongs to, read from the registry.

            Fail-soft: a connection the controller cannot describe falls back to
            the generic list rather than opening a service screen for the wrong
            service.
            """

            from .connection_controller import connection_controller

            try:
                target = connection_controller().get(connection_id).target
            except Exception:
                return None
            preset_id = getattr(target, "preset_id", "") or ""
            from .tui_connections import service_flow_presets

            return preset_id if preset_id in service_flow_presets() else None

        def _connection_hub_reopen(self, result: Optional[Any]) -> None:
            """Return to the hub, not to the chat.

            A child screen closing used to drop the user back at the composer,
            so adding two connections meant typing ``/connect`` twice. Coming
            back here also means the row just saved is visible immediately,
            because the hub re-reads its two sources on mount.

            The one exception is the provider wizard hand-off, which owns the
            top of the modal stack while it runs and returns through its own
            callback.
            """

            self._refresh_status()
            if result == "add_provider":
                self._connect_return_to_hub = True
                self.call_after_refresh(self.action_provider_preset)
                return
            self.call_after_refresh(lambda: self._open_connections(None))

        def _connections_screen_closed(self, result: Optional[Any]) -> None:
            self._refresh_status()
            if result == "add_provider":
                self.call_after_refresh(self.action_provider_preset)
                return
            self.query_one("#composer", Input).focus()

        def _open_connections_screen(self, name: str) -> None:
            screens = self._connections_screens_cached()
            screen_cls = screens[name]
            self.push_screen(screen_cls(self.language), self._connections_screen_closed)

        def _open_provider_preset_screen(self, _ignored: Optional[str]) -> None:
            """Hook used by the Model Providers screen to launch the wizard."""
            self.push_screen(
                ProviderPresetScreen(self.language), self._provider_preset_done
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

        def _leave_provider_flow(self) -> None:
            """Where the provider wizard puts the user down. One place, one rule.

            B1. Entered from the Connection Hub, it returns to the hub -- and the
            hub re-reads its sources on mount, so the provider just saved is
            visible without anything having to tell it. Entered any other way it
            focuses the composer, which is the pre-existing behaviour and the
            right one: nobody who pressed a wizard key asked for a list.

            The flag is cleared here rather than at each call site, so a flow
            that ends early cannot leave it armed for the next unrelated wizard.
            """

            if self._connect_return_to_hub:
                self._connect_return_to_hub = False
                self.call_after_refresh(lambda: self._open_connections(None))
                return
            self.query_one("#composer", Input).focus()

        def action_provider_preset(self) -> None:
            self.push_screen(
                ProviderPresetScreen(self.language), self._provider_preset_done
            )

        def _provider_preset_done(self, preset_id: Optional[str]) -> None:
            if preset_id is None:
                self._setup_both = False
                self._leave_provider_flow()
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
            self._leave_provider_flow()

        def action_setup(self, preset: Optional[ProviderPreset] = None) -> None:
            self.push_screen(
                ProviderSetupScreen(self.language, preset), self._provider_setup_done
            )

        def _provider_setup_done(self, setup: Optional[ProviderSetup]) -> None:
            if setup is None:
                self._setup_both = False
                self._leave_provider_flow()
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
            # B1. A saved provider goes back to the hub it was started from, and
            # nowhere else. Two things used to follow a successful save and both
            # were wrong for this journey: the composer took focus, so adding two
            # connections meant typing `/connect` twice; and `_setup_both` could
            # push `BridgeSetupScreen` on top, which asked a person who wanted a
            # model to configure a tunnel. `_setup_both` is only ever armed by
            # the retired api/web/both wizard, so no production path reaches that
            # branch any more -- it is kept for the pending-task hand-off below
            # and cleared here so it cannot leak into the next flow.
            if self._setup_both:
                self._setup_both = False
                self._connect_return_to_hub = False
                self.query_one("#composer", Input).focus()
                self.action_bridge()
                return
            if self.pending_task:
                # A task was waiting on a model. Finishing the work the user
                # actually asked for outranks showing them a list.
                self._connect_return_to_hub = False
                self.query_one("#composer", Input).focus()
                task, self.pending_task = self.pending_task, None
                self._submit_task(task)
                return
            self._leave_provider_flow()

        def _open_bridge_setup(
            self,
            *,
            default_profile: str = "promptql",
            locked_profile: Optional[str] = None,
            after: Optional[Callable[[], None]] = None,
        ) -> None:
            """Open the one production bridge wizard, optionally for a service.

            The service flow uses this instead of inventing a second launcher.
            Cancelling a nested service setup simply returns to that service;
            the standalone /bridge action keeps its historical composer-focus
            behavior through ``_bridge_setup_done(None)``.
            """

            def closed(setup: Optional[BridgeSetup]) -> None:
                if setup is not None or after is None:
                    self._bridge_setup_done(setup)
                if after is not None:
                    self.call_after_refresh(after)

            self.push_screen(
                BridgeSetupScreen(
                    self.language,
                    default_profile=default_profile,
                    locked_profile=locked_profile,
                ),
                closed,
            )

        def open_service_bridge_setup(
            self, preset_id: str, after: Optional[Callable[[], None]] = None
        ) -> None:
            """Create the selected service entirely through the TUI service layer."""

            if preset_id == "clickup":
                screens = self._connections_screens_cached()
                screen = screens["_ClickupAutoScreen"](self.language)

                def clickup_closed(_target: Optional[Any]) -> None:
                    if after is not None:
                        self.call_after_refresh(after)

                self.push_screen(screen, clickup_closed)
                return

            profile = {
                "chatgpt-web": "chatgpt-web",
                "claude-web": "claude-web",
                "hyperagent-web": "hyperagent-web",
                "notion": "notion",
                "adapt": "adapt",
            }.get(preset_id)
            if profile is None:
                raise ValueError(f"unsupported service preset: {preset_id}")
            self._open_bridge_setup(
                default_profile=profile,
                locked_profile=profile,
                after=after,
            )

        def action_bridge(self) -> None:
            # Always allow the picker to open. ClickUp is launched in its own
            # terminal and may run beside the bridge already owned by this TUI.
            # The single-process guard is applied later only to profiles that
            # would reuse ``self.bridge_process``.
            self._open_bridge_setup()

        def _launch_saved_bridge_from_tui(self, setup: BridgeSetup) -> None:
            """Persist and start one hosted bridge without shelling out to the CLI."""
            try:
                profile_name = _persist_tui_saved_bridge_profile(
                    self.repository,
                    setup,
                    language=self.language,
                )
            except Exception as exc:
                self._write_notice(
                    self._label(
                        "Не удалось сохранить профиль подключения: ",
                        "Could not save the connection profile: ",
                    )
                    + str(redact(str(exc)))[:200],
                    "error",
                )
                return

            self._write_notice(
                self._label(
                    f"Запускаю {setup.profile} через сохранённый профиль KaroX…",
                    f"Starting {setup.profile} through a saved KaroX profile…",
                ),
                "info",
            )

            def execute() -> None:
                try:
                    from .web_bridge_launcher import start_saved_bridge

                    result = dict(start_saved_bridge(profile_name))
                    error = (
                        str(result.get("error") or "bridge start failed")[:200]
                        if result.get("action") == "error"
                        else None
                    )
                except Exception as exc:
                    result = {}
                    error = str(redact(str(exc)))[:200]
                with contextlib.suppress(Exception):
                    self.call_from_thread(
                        self._saved_bridge_from_tui_done,
                        profile_name,
                        result,
                        error,
                    )

            self.run_worker(
                execute,
                thread=True,
                exclusive=True,
                group=f"saved-bridge-setup-{setup.profile}",
            )

        def _saved_bridge_from_tui_done(
            self,
            profile_name: str,
            result: Mapping[str, Any],
            error: Optional[str],
        ) -> None:
            if error:
                self._write_notice(
                    self._label(
                        f"Профиль {profile_name} сохранён, но запуск не удался: {error}",
                        f"Profile {profile_name} was saved, but start failed: {error}",
                    ),
                    "error",
                )
                return
            action = str(result.get("action") or "started")
            self._write_notice(
                self._label(
                    f"Подключение готово: {profile_name} ({action}). Управление доступно в /connect.",
                    f"Connection ready: {profile_name} ({action}). Manage it in /connect.",
                ),
                "success",
            )

        def _bridge_setup_done(self, setup: Optional[BridgeSetup]) -> None:
            if setup is None:
                self.query_one("#composer", Input).focus()
                return
            if setup.profile == "clickup":
                screens = self._connections_screens_cached()
                self.push_screen(screens["_ClickupAutoScreen"](self.language))
                self.query_one("#composer", Input).focus()
                return
            if setup.profile in WEB_BRIDGE_PROFILES:
                self._launch_saved_bridge_from_tui(setup)
                self.query_one("#composer", Input).focus()
                return

            current = self.bridge_process
            if current is not None and current.poll() is None:
                self._write_notice(
                    self._label(
                        "Текущий мост уже работает. Notion, Hyperagent и ClickUp запускаются отдельно; "
                        "для замены этого моста сначала выполните /bridge stop.",
                        "The current bridge is already running. Notion, Hyperagent and ClickUp launch "
                        "separately; run /bridge stop first to replace this bridge.",
                    ),
                    "warning",
                )
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
                # WEB_BRIDGE_PROFILES return above through the saved-profile
                # service layer. Reaching this point means a generic/PromptQL
                # bridge whose subprocess is the transport process itself.
                launch = _bridge_launch(self.repository, setup)
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
                        f"[bold #c6a56b]Ключ доступа:[/] хранится в OS keyring "
                        f"(fingerprint: {BridgeCredentialStore.fingerprint(launch.secret)[:16]}…)\n"
                        f"[dim]Скопировать безопасно: karox bridge credential copy "
                        f"{escape(launch.session_id)} --json. Публичный URL появится ниже — "
                        "вставляйте в Notion именно его (с суффиксом /mcp) "
                        "вместе со скопированным Bearer-значением.[/]",
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
            # Keep the public URL visible, but never turn terminal scrollback
            # into a credential store. The reusable secret remains in the OS
            # keyring and is copied explicitly through the typed CLI action.
            launch = self.bridge_launch
            credential_note = ""
            if launch is not None:
                credential_note = (
                    "\n[bold #c6a56b]Ключ доступа:[/] хранится в OS keyring "
                    f"(fingerprint: {BridgeCredentialStore.fingerprint(launch.secret)[:16]}…)"
                    "\n[dim]Скопировать безопасно: karox bridge credential copy "
                    f"{escape(launch.session_id)} --json.[/]"
                )
            self._write(
                "[bold #d4b676]Публичный URL коннектора:[/] "
                + escape(endpoint)
                + credential_note
                + "\n[dim]Вставьте URL (с суффиксом /mcp) и скопированное Bearer-значение в Notion. "
                "Credential многоразовый — Notion хранит его и переиспользует.[/]"
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
            # A new generation for a genuinely new run. Bumped only after every
            # guard above has passed, so a rejected submission (busy, unsafe
            # workspace, no model) does not invalidate the run in flight.
            self._run_generation += 1
            run = RunIdentity(session_id, self._run_generation)
            self._active_run = run
            self.active_session = session_id
            self._history_seen = 0
            self._history_fingerprint = None
            self._reset_activity()
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
            # The run is now really starting: the session id exists, the model is
            # selected and the worker is about to be dispatched. Published from
            # the production path rather than from a helper a test could call on
            # its own, so the typed status row proves the real lifecycle.
            self._publish_agent_started(run, task)
            task_for_agent = task
            if self._continuation_context:
                # /compact stored a bounded, redacted continuation preamble.
                # The screen and the published run keep the task as typed; only
                # the dispatched agent carries the extra context, exactly once.
                task_for_agent = (
                    "Continuation context from the previous session "
                    "(structured handoff, bounded):\n"
                    f"{self._continuation_context}\n\n"
                    f"Task:\n{task}"
                )
                self._continuation_context = None
            argv = _agent_argv(
                task_for_agent,
                self.repository,
                self.verification,
                session_id,
                run_cost_profile=self.run_cost_profile,
                effort_level=self.effort_level,
                agent_mode=self.agent_mode,
            )

            def execute() -> None:
                code, output = _run_agent_cli(
                    argv, on_process=lambda proc: setattr(self, "agent_process", proc)
                )
                # ``run`` is captured by the closure, so the callback names the
                # run it belongs to rather than whatever session happens to be
                # active by the time it is marshalled back.
                self.call_from_thread(self._agent_finished, code, output, run)

            self.run_worker(execute, thread=True, exclusive=True, group="agent")

        def check_action(self, action: str, parameters: Any) -> bool:
            # ``KaroXApp`` binds ``escape`` to ``stop_agent`` with ``priority=True``.
            # A priority binding is checked from the App down, so it swallows the
            # key before any modal screen's own ``escape`` binding (cancel/close)
            # can run -- and since the agent is almost always idle while a modal is
            # open, ``stop_agent`` returns immediately and the key simply vanishes,
            # making every modal impossible to close with Esc. Disable that binding
            # only when a modal screen is active and there is nothing to stop; the
            # key then falls through to the modal's binding chain as expected.
            if action == "stop_agent" and not self.agent_busy:
                if isinstance(self.screen, ModalScreen):
                    return False
            return True

        def action_interrupt(self) -> None:
            """Copy a selection; otherwise stop active work or exit when idle."""
            selected = self.screen.get_selected_text() or ""
            if selected:
                self.action_copy_selection()
                return
            if self.agent_busy:
                self.action_stop_agent()
                return
            process = self.bridge_process
            if process is not None and process.poll() is None:
                self._stop_bridge()
                return
            self.exit(0)

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

        def _begin_step(self, call_id: str, tool_name: str) -> None:
            """A tool call started; the line becomes what that call *means*.

            The tool name is consumed here and goes no further. It selects a
            catalog entry and is then discarded, which is why no caller can
            arrange for `repo.edit_file` to appear on screen.
            """

            kind = _activity_kind_for_tool(tool_name)
            self._activity_calls[_identifier(call_id)] = kind
            self._set_activity_kind(kind)

        def _set_activity_kind(self, kind: str, *, restart: bool = True) -> None:
            """Adopt a new action, replacing whatever the line said before.

            The clock restarts with the action, not with the turn: an elapsed
            number spanning two different actions describes neither of them.
            """

            if restart and kind != self._activity_kind:
                self._activity_started = time.monotonic()
            self._activity_kind = kind
            self._show_activity()

        def _activity_action(self) -> Optional[ActivityAction]:
            """The current action, or nothing at all when there is none.

            Idle draws no line. A framed "Working" over a finished conversation
            is a claim that something is happening, and the person who believes
            it waits for an agent that stopped minutes ago.
            """

            kind = self._activity_kind
            if not kind:
                return None
            # A pending confirmation outranks whatever tool ran last: it is the
            # only state waiting on the person reading the screen, and the one
            # the animated dots cannot express. Read from the typed view store,
            # which already owns this fact for the header and the browser.
            row = self._active_session_view()
            if row is not None and (
                row.primary_action == ACTION_REVIEW_RISK
                or _identifier(row.waiting_reason) == WAIT_CONFIRMATION
            ):
                kind = ACTIVITY_WAITING
            elapsed: Optional[float] = None
            started = self._activity_started
            if isinstance(started, float) and kind in _ACTIVITY_IN_PROGRESS:
                elapsed = time.monotonic() - started
            return ActivityAction(
                kind=kind,
                files=len(self._activity_changed) or None,
                tests_passed=self._activity_tests,
                elapsed_seconds=elapsed,
                reason=self._activity_reason,
            )

        def _show_activity(self) -> None:
            action = self._activity_action()
            if action is None:
                self._activity_rendered = ""
                self._set_activity("", "idle")
                return
            text = _activity_text(
                action,
                english=self.language != "ru",
                width=self._activity_width(),
            )
            if text == self._activity_rendered:
                return
            self._activity_rendered = text
            self._set_activity(text, _ACTIVITY_STYLES.get(action.kind, "working"))

        def _activity_width(self) -> int:
            """Columns the line actually has, which is not the window's width.

            The pane is inset by its margin, padding and left rule. Measured
            against the window, a line fits the terminal and still wraps inside
            its own frame -- which is the one thing the contract forbids.
            """

            try:
                width = int(self.size.width) - _ACTIVITY_CHROME_COLUMNS
            except Exception:
                width = 0
            return width if width > 0 else 0

        def _tick_activity(self) -> None:
            """Advance the elapsed number without redrawing anything else.

            One `Static.update` on one widget, and only when the rendered text
            actually differs -- `_show_activity` compares before it writes. A
            second in which nothing changed therefore costs a string comparison
            rather than a frame, which is what makes a per-second timer
            affordable beside a conversation that must stay scrollable.
            """

            if not self.agent_busy or not self._activity_kind:
                return
            self._show_activity()

        def _reset_activity(self) -> None:
            """Forget the previous turn completely before starting a new one."""

            self._activity_calls.clear()
            self._activity_changed.clear()
            self._activity_tests = None
            self._activity_kind = ""
            self._activity_reason = ""
            self._activity_started = None
            self._activity_rendered = ""
            self._set_activity("", "idle")

        def _finish_activity(
            self, kind: str, reason: str = "", *, files: Sequence[str] = ()
        ) -> None:
            """Turn the end of a run into the short summary the line becomes."""

            for path in files:
                self._activity_changed[_identifier(path)] = None
            self._activity_reason = _identifier(reason)
            self._activity_started = None
            self._set_activity_kind(kind, restart=False)

        def _finish_step(
            self,
            call_id: str,
            tool_name: str,
            *,
            failed: bool,
            changed: Sequence[str] = (),
            tests_passed: Optional[int] = None,
        ) -> None:
            """A tool call ended. An outcome moves the line; a call alone does not.

            A finished call is not worth an announcement of its own. Making one
            had the line flicker "Reading code", "Done", "Reading code", "Done"
            through a fast turn -- motion that reports nothing, because the next
            call is already running by the time the eye arrives. The line keeps
            describing the action until a *different* action starts, which is
            what makes it legible at agent speed.

            The parameters are the second half of the containment argument. This
            method is handed a classification, a boolean, a list of paths to
            count and an integer. There is no `detail` string any more, so the
            branch that used to interpolate a tool result into rendered text no
            longer exists to be exploited.
            """

            kind = self._activity_calls.pop(
                _identifier(call_id), ""
            ) or _activity_kind_for_tool(tool_name)
            # Paths are counted, never rendered. The count is the fact a person
            # wants from a glance; the names are in Session Detail, which has
            # the room and the context for them.
            for path in changed:
                self._activity_changed[_identifier(path)] = None
            if tests_passed is not None:
                self._activity_tests = _optional_positive_int(tests_passed)
            if failed:
                # A failing tool is not yet a failing run -- the agent may well
                # recover on the next step -- but it is the one in-flight event
                # worth interrupting the line for.
                self._set_activity_kind(ACTIVITY_FAILED, restart=False)
                return
            self._set_activity_kind(kind, restart=False)

        def _poll_typed_transcript(self) -> None:
            """Phase 3: typed transcript reader, replacing _poll_agent_history.

            Reads new events from the SQLite WAL transcript store and projects
            them into the chat bubbles and activity lines the UI shows. The
            store is written by the agent subprocess via
            :func:`~karox.transcript_shadow.make_transcript_observer`, so this
            timer is a lightweight read of what has changed since the last tick
            -- no session JSON load, no canonical re-serialise.
            """
            if not self.agent_busy or not self.active_session:
                return
            try:
                from .transcript_shadow import get_transcript_store
                store = get_transcript_store()
            except Exception:
                return
            # Check for new events since last tick by sequence number.
            try:
                latest = store.latest_sequence(self.active_session)
            except Exception:
                self._poll_assistant_content()
                return
            if latest < self._history_seen:
                # Session changed (new run); reset cursor.
                self._history_seen = 0
            if latest >= self._history_seen:
                try:
                    new_events = list(store.replay(
                        self.active_session, from_sequence=self._history_seen
                    ))
                except Exception:
                    new_events = []
                self._history_seen = latest + 1
                for event in new_events:
                    kind = event.kind
                    payload = event.payload
                    if kind == "ToolCallStarted":
                        tool = str(payload.get("tool") or "tool")
                        call_id = str(payload.get("call_id") or event.parent_id or tool)
                        self._begin_step(call_id, tool)
                    elif kind == "ToolCallCompleted":
                        tool = str(payload.get("tool") or "tool")
                        call_id = str(payload.get("call_id") or event.parent_id or tool)
                        ok = payload.get("ok")
                        self._finish_step(
                            call_id, tool,
                            failed=(ok is False),
                        )
                    elif kind == "SessionStateChanged":
                        pass
                    elif kind == "AgentStepCompleted":
                        pass
            # Always read assistant content: the typed stream tracks tool
            # calls and steps but NOT the model's text output.
            self._poll_assistant_content()

        def _poll_assistant_content(self) -> None:
            """Read new entries from the session store for chat bubbles + activity.

            The typed transcript tracks tool calls and steps but not the
            model's text output (that arrives as streaming TEXT_DELTA fragments
            which are intentionally not persisted as events). For the chat
            bubble display AND the activity widget fallback (when the typed
            stream has no events for this session), this reads the session
            store's provider_history when it changes.
            """
            if not self.agent_busy or not self.active_session:
                return
            store = SessionStore(session_dir())
            state_path = store.state_path(self.active_session)
            try:
                stat = state_path.stat()
            except OSError:
                return
            fingerprint = (stat.st_mtime_ns, stat.st_size)
            if fingerprint == self._history_fingerprint:
                return
            self._history_fingerprint = fingerprint
            try:
                history = store.load(self.active_session).provider_history
            except Exception:
                return
            content_seen = getattr(self, "_content_seen", 0)
            new_entries = history[content_seen:]
            self._content_seen = len(history)
            # The agent's internal "answer_prompt" nudge produces a ceremonial
            # self-report reply ("the task was only a greeting...") that the
            # user has already read as the real answer above it. Provider history
            # is persisted in separate atomic updates, so the nudge and its reply
            # can arrive in *different polling ticks*. Keep the suppression state
            # on the app and bind it to this session; a function-local flag loses
            # the race and renders the self-report as a second assistant card.
            suppression_session = getattr(
                self, "_answer_prompt_suppression_session", None
            )
            suppress_next_assistant_text = bool(
                getattr(self, "_suppress_next_answer_prompt_assistant", False)
                and suppression_session == self.active_session
            )
            for entry in new_entries:
                if entry.get("role") == "user" and entry.get("kind") == "answer_prompt":
                    suppress_next_assistant_text = True
                    self._suppress_next_answer_prompt_assistant = True
                    self._answer_prompt_suppression_session = self.active_session
                    continue
                if entry.get("role") == "assistant":
                    suppress_now = suppress_next_assistant_text
                    suppress_next_assistant_text = False
                    if suppress_now:
                        self._suppress_next_answer_prompt_assistant = False
                        self._answer_prompt_suppression_session = None
                    content = entry.get("content")
                    text = str(content) if content else ""
                    if text.strip() and not suppress_now:
                        self._last_assistant_content = text
                        self._write_assistant(text)
                    for call in entry.get("tool_calls") or ():
                        if isinstance(call, dict):
                            name = str(call.get("name") or "tool")
                            self._begin_step(
                                str(call.get("call_id") or name), name
                            )
                elif entry.get("role") == "tool":
                    name = str(
                        entry.get("core_name") or entry.get("tool_name") or "tool"
                    )
                    result = entry.get("result")
                    self._finish_step(
                        str(entry.get("tool_call_id") or name),
                        name,
                        failed=isinstance(result, dict)
                        and not result.get("ok", True),
                    )

        def _agent_finished(
            self, code: int, output: str, run: Optional[RunIdentity] = None
        ) -> None:
            """Finish one run, and only if that run is still the current one.

            The identity check happens before every mutation below, which is the
            entire point of it. A stale callback -- run A finishing after run B
            of the same session has already started -- reached this method and
            cleared ``agent_busy``, dropped B's process handle, re-enabled the
            composer and published a terminal status for a run still working.
            Recognising it here, before anything is touched, is what makes it
            harmless.

            ``run`` is optional so a caller that drives this method directly
            still finishes the active run; the worker always names its own.
            """

            if run is None:
                run = self._active_run or RunIdentity(
                    self.active_session or "", self._run_generation
                )
            elif run != self._active_run:
                # Late callback from a superseded run. Nothing is touched: not
                # the busy flag, not the process handle, not the active session,
                # not the composer, and no terminal event is published.
                #
                # Compared against the run the application currently owns, with
                # no ``_active_run is not None`` escape hatch. That exemption
                # left a real hole: a run clears ``_active_run`` when it ends, so
                # a duplicate callback arriving while the application is idle
                # found ``None``, skipped the check and went on to clear state it
                # no longer owned. A generation is monotonic and never reused, so
                # "not the current run" is the whole test -- including when the
                # current run is no run at all.
                return
            # No local copy of the session id is needed any more: every terminal
            # publisher below is handed ``run`` and reads ``run.session_id`` from
            # the identity it was given, so no branch here can clear it first.
            self._poll_typed_transcript()
            self.agent_busy = False
            self.agent_process = None
            self.query_one("#composer", Input).disabled = False
            self.query_one("#busy", LoadingIndicator).styles.display = "none"
            if self._stop_requested:
                self._stop_requested = False
                self._active_run = None
                self._publish_agent_cancelled(run)
                # Cancellation outranks every outcome below and returns before
                # the resolver runs, so it names its own reason here.
                self._finish_activity(ACTIVITY_STOPPED, "cancelled")
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
            else:
                safe = output.strip() or f"Agent exited with code {code}."
                self._write_notice(safe, "error")
            # One terminal lifecycle event for the run, decided from the
            # structured result rather than from the words drawn above. A run
            # that produced no parseable report failed, whatever it printed.
            status, reason, error_code = self._run_outcome(report, code)
            # A2. The summary and the published event are now decided by one
            # resolver rather than by two ladders of `report.get` that could --
            # and did -- disagree. A run could be drawn green as "completed and
            # verified" while the event bus was told it failed on a contract
            # mismatch, because the words read `verified` and the event read
            # both `verified` and the exit code. One source, one verdict.
            activity_kind = _ACTIVITY_OUTCOME_KINDS.get(status, ACTIVITY_FAILED)
            if status == STATUS_STOPPED and reason in _BENIGN_STOP_REASONS:
                # A bounded stop that changed nothing wrong is an outcome, not
                # an abort: a plain question or a no-op task must not wear the
                # same warning-coloured "Stopped" as a user cancellation or a
                # blown step budget.
                activity_kind = ACTIVITY_COMPLETED
            self._finish_activity(
                activity_kind,
                reason,
                files=(
                    tuple(report.get("changed_files") or ())
                    if isinstance(report, dict)
                    else ()
                ),
            )
            # The run is over as far as this process is concerned, so a further
            # callback naming it is a duplicate and the terminal claim below
            # rejects it.
            self._active_run = None
            if status == STATUS_COMPLETED:
                self._publish_agent_completed(run, report)
            elif status == STATUS_STOPPED:
                self._publish_agent_stopped(run, reason)
            else:
                self._publish_agent_failed(run, reason, code=error_code)
            self._refresh_status()
            self.query_one("#composer", Input).focus()

        @staticmethod
        def _run_outcome(
            report: Optional[Dict[str, Any]], code: int
        ) -> Tuple[str, str, str]:
            """Resolve one run into a lifecycle status and a reason identifier.

            The exit code alone cannot answer this. ``karox agent`` returns
            ``0 if report.verified else 1`` (see :mod:`karox.cli`), so a run that
            simply changed nothing exits non-zero even though nothing went
            wrong. Reading only the code would paint a plain question red;
            reading only ``verified`` would call a crashed run finished. Both are
            read, and the two disagreeing is itself reported rather than guessed.

            Returns the status to publish, a machine-readable reason, and the
            error code an ERROR event would carry (empty when none is published).
            The caller owns cancellation, which outranks every outcome here.
            """

            if not isinstance(report, dict):
                # No parseable structured result. Whatever the child printed, the
                # contract was not honoured, so this is not allowed to look green.
                return (
                    STATUS_FAILED,
                    f"malformed_report exit_code={int(code)}",
                    ERROR_AGENT_MALFORMED,
                )
            status = _identifier(report.get("status"))
            reason = _identifier(report.get("reason"))
            verified = bool(report.get("verified"))
            if verified:
                # ``verified`` is the only outcome the CLI encodes as exit 0. A
                # non-zero code beside it means the two halves disagree.
                if int(code) != 0:
                    return STATUS_FAILED, "contract_mismatch", ERROR_AGENT_CONTRACT
                return STATUS_COMPLETED, reason or status, ""
            if status == STATUS_FAILED:
                return STATUS_FAILED, reason or status, ERROR_AGENT_FAILED
            if int(code) == 0:
                # Exit 0 without ``verified`` contradicts the CLI contract.
                return STATUS_FAILED, "contract_mismatch", ERROR_AGENT_CONTRACT
            # A bounded stop: no verified change and no evidence-backed answer,
            # but the session is saved and resumable. Neutral, never an error.
            return STATUS_STOPPED, reason or status or "stopped", ""

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

        def _start_new_task(self) -> None:
            """/new: a fresh conversation and a fresh session on submit.

            Log-plus-screen state only: the transcript is cleared, the
            on-screen session binding and any /compact continuation context are
            dropped. Durable SessionRecords are never touched -- /resume
            returns to them.
            """

            if self.agent_busy:
                self._write_notice(
                    self._label(
                        "KaroX ещё работает. Текущее действие показано над строкой ввода.",
                        "KaroX is still working. The current action is shown above the input.",
                    ),
                    "warning",
                )
                return
            self.action_clear_log()
            self.active_session = None
            self._history_seen = 0
            self._history_fingerprint = None
            self._continuation_context = None
            self._refresh_status()
            self._write_notice(
                self._label(
                    "Новая задача: следующее сообщение начнёт свежую сессию. "
                    "Прежние сессии сохранены — /resume вернёт к ним.",
                    "New task: the next message starts a fresh session. "
                    "Existing sessions are kept - /resume returns to one.",
                ),
                "success",
            )

        def _resume_session(self, target: str) -> None:
            """/resume: the Session Browser act, typed.

            Without an argument this opens the Session Browser -- one screen
            owns the list. With an id it performs exactly what choosing
            ``resume`` on a row performs: make that session the one on screen
            and show its detail. Nothing here starts an agent; resuming the
            work itself stays an explicit task the person types.
            """

            if not target:
                self.action_session_browser()
                return
            try:
                row = self._view_store.summary(target)
            except Exception:
                row = None
            if row is None:
                self._write_notice(
                    self._label(
                        f"Сессия {escape(target)} не найдена. /sessions покажет, что существует.",
                        f"Session {escape(target)} was not found. /sessions shows what exists.",
                    ),
                    "error",
                )
                return
            self.active_session = target
            self._history_seen = 0
            self._history_fingerprint = None
            self._refresh_status()
            self._open_session_detail(target)

        def _compact_conversation(self) -> None:
            """/compact: handoff document + continuation context.

            Per SESSION-MODEL.md the durable record is never compacted; what is
            compacted here is the conversation surface. The structured handoff
            is built by the backend (bounded and redacted by ``build_handoff``),
            the transcript is replaced by its summary, and the next submitted
            task carries the bounded continuation preamble exactly once.
            """

            if self.agent_busy:
                self._write_notice(
                    self._label(
                        "KaroX ещё работает. Текущее действие показано над строкой ввода.",
                        "KaroX is still working. The current action is shown above the input.",
                    ),
                    "warning",
                )
                return
            target = self.active_session
            if not target:
                self._write_notice(
                    self._label(
                        "Нет активной сессии для сжатия. /resume выберет её.",
                        "No active session to compact. /resume picks one.",
                    ),
                    "warning",
                )
                return
            self.query_one("#busy", LoadingIndicator).styles.display = "block"
            self._set_activity(
                self._label("Готовлю handoff…", "Building the handoff…"),
                "working",
            )
            argv = [
                "session",
                "handoff",
                target,
                "--json",
                "--repository",
                str(self.repository),
            ]

            def execute() -> None:
                code, output = _capture_cli(argv)
                self.call_from_thread(self._compact_finished, target, code, output)

            self.run_worker(execute, thread=True, exclusive=True, group="inspection")

        def _compact_finished(self, session_id: str, code: int, output: str) -> None:
            self.query_one("#busy", LoadingIndicator).styles.display = "none"
            self._reset_activity()
            if code != 0:
                self._write_notice(
                    self._label(
                        f"Handoff для {escape(session_id)} не построен (код {code}).",
                        f"The handoff for {escape(session_id)} could not be built (exit {code}).",
                    ),
                    "error",
                )
                detail = output.strip()
                if detail:
                    self._write(f"[#e0a3a3]{escape(detail[:2000])}[/]")
                return
            try:
                document = json.loads(output)
            except json.JSONDecodeError:
                self._write_notice(
                    self._label(
                        "Вывод handoff не является корректным JSON.",
                        "The handoff output was not valid JSON.",
                    ),
                    "error",
                )
                return
            context = _continuation_from_handoff(document)
            self._continuation_context = context or None
            self._transcript().clear()
            if context:
                self._write(
                    f"[bold #e0dccc]{self._label('Продолжение', 'Continuation')}[/]\n"
                    + escape(context)
                )
            self._write_notice(
                self._label(
                    "Диалог сжат: следующее сообщение продолжит работу с этим контекстом.",
                    "Conversation compacted: the next message continues with this context.",
                ),
                "success",
            )

        def _run_inspection(self, argv: Sequence[str], label: str) -> None:
            loading = {
                "/models": ("Получаю список моделей…", "Loading models…"),
                "/sessions": ("Получаю список сессий…", "Loading sessions…"),
                "/mcp": ("Получаю список MCP-серверов…", "Loading MCP servers…"),
                "/doctor": ("Проверяю KaroX…", "Checking KaroX…"),
            }
            message = loading.get(label, ("Выполняю команду…", "Running command…"))
            self.query_one("#busy", LoadingIndicator).styles.display = "block"
            # The composer hint row is gone; the one-line activity surface is
            # exactly what a transient "Loading models…" belongs on.
            self._set_activity(
                message[1] if self.language == "en" else message[0],
                "working",
            )

            def execute() -> None:
                code, output = _capture_cli(argv)
                self.call_from_thread(self._inspection_finished, label, code, output)

            self.run_worker(execute, thread=True, exclusive=True, group="inspection")

        def _inspection_finished(self, label: str, code: int, output: str) -> None:
            self.query_one("#busy", LoadingIndicator).styles.display = "none"
            self._reset_activity()
            color = "#b3a990" if code == 0 else "#e0a3a3"
            message = _inspection_text(label, code, output, self.language)
            if label == "/sessions":
                live = self._live_session_block()
                if live:
                    message = f"{message}\n\n{live}"
            self._write(f"[{color}]{escape(message)}[/]")

        def _live_session_block(self) -> str:
            """The typed rows `/sessions` cannot get from a child process.

            The list above this block comes from ``karox session list --json``
            run in a subprocess: it answers what exists and what each run
            changed, and it is a snapshot of the files as they were when the
            command was typed. The view store answers what a session is doing
            *now* and what it is waiting on, from events that arrived after that
            snapshot -- so the two are complementary rather than duplicates.

            Only event-backed rows are listed. A session the store merely
            backfilled from the same durable records would repeat the block
            above without adding a fact.

            Fail-soft by construction: a store fault costs this block, never the
            command output the user asked for.
            """

            try:
                rows = [
                    row
                    for row in self._view_store.summaries()
                    if row.last_event_seq > 0
                ]
            except Exception:
                return ""
            if not rows:
                return ""
            english = self.language != "ru"
            lines = ["Live:" if english else "Сейчас:"]
            lines.extend(_session_row_text(row, english) for row in rows)
            return "\n".join(lines)

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


# --------------------------------------------------------------- activity line
#
# One human-readable line, and the only ordinary-mode indicator of what the
# agent is doing.
#
# What this replaces: a stack of up to six raw tool lines --
# `X repo.read_file 2s  ok - path=src/karox/tui.py` -- one per call of the turn.
# Three things were wrong with it and all three are structural. It named
# internal functions the reader cannot call, so the screen taught vocabulary
# instead of state. It interpolated `result["data"]` straight into rendered
# text, so anything a tool returned -- a path, a prompt, an escape sequence, a
# token that happened to sit in a diff -- was drawn verbatim. And it grew: four
# rows describing plumbing, directly above a conversation that in a small
# terminal had about eight.
#
# The rule that makes the leak impossible rather than merely unlikely: nothing
# free-form reaches the screen. A line is assembled from the catalogs below plus
# integers, and no branch in this layer interpolates a string taken from a tool
# result. A hostile payload therefore has no path to rendered text -- not one
# that is escaped or filtered, but none at all.
#
# The technical history is not lost, only moved: Session Detail still holds
# every call, its arguments and its result.

ACTIVITY_READING = "reading"
ACTIVITY_SEARCHING = "searching"
ACTIVITY_EDITING = "editing"
ACTIVITY_TESTING = "testing"
ACTIVITY_WAITING = "waiting"
ACTIVITY_COMPLETED = "completed"
ACTIVITY_STOPPED = "stopped"
ACTIVITY_FAILED = "failed"
# The fail-soft destination. An action nobody has taught this layer about still
# has to say something true, and "working" is true of every one of them.
ACTIVITY_WORKING = "working"

# One ordinary line. The second is reserved for a critical failure, where the
# reason is worth a row of the conversation and nothing else is.
ACTIVITY_MAX_LINES = 2

_ACTIVITY_WORDS: Dict[str, Tuple[str, str]] = {
    ACTIVITY_READING: ("Читает код", "Reading code"),
    ACTIVITY_SEARCHING: ("Ищет причину ошибки", "Finding the cause"),
    ACTIVITY_EDITING: ("Изменяет код", "Updating code"),
    ACTIVITY_TESTING: ("Проверяет тесты", "Running tests"),
    ACTIVITY_WAITING: ("Ожидает подтверждения", "Waiting for confirmation"),
    ACTIVITY_COMPLETED: ("Готово", "Done"),
    ACTIVITY_STOPPED: ("Остановлено", "Stopped"),
    ACTIVITY_FAILED: ("Ошибка проверки", "Check failed"),
    ACTIVITY_WORKING: ("Работает", "Working"),
}

# Where the technical history went, said in the one place a person will look
# for it. Only a failure earns this: on a good run it is an instruction to go
# reading for nothing.
_ACTIVITY_DETAILS_WORDS = ("открыть подробности", "open details")

# The whole of the tool vocabulary the screen is allowed to know. A name absent
# from this table is not an error and is not printed -- it resolves to
# ACTIVITY_WORKING, so adding a tool to the core never leaks its identifier into
# the interface while somebody gets around to classifying it.
_TOOL_ACTIVITY_KINDS: Dict[str, str] = {
    "repo.read_file": ACTIVITY_READING,
    "repo.read_lines": ACTIVITY_READING,
    "repo.list_files": ACTIVITY_READING,
    "git.status": ACTIVITY_READING,
    "git.diff": ACTIVITY_READING,
    "git.log": ACTIVITY_READING,
    "repo.search": ACTIVITY_SEARCHING,
    "repo.write_file": ACTIVITY_EDITING,
    "repo.edit_file": ACTIVITY_EDITING,
    "git.commit": ACTIVITY_EDITING,
    "checks.run": ACTIVITY_TESTING,
}

# Why a run ended, in words. Keyed by the identifiers the agent already
# publishes, so a reason with no entry contributes nothing rather than printing
# `budget_exceeded:output_tokens` at somebody.
_ACTIVITY_REASON_WORDS: Dict[str, Tuple[str, str]] = {
    "step_limit": ("достигнут лимит шагов", "step limit reached"),
    "max_steps": ("достигнут лимит шагов", "step limit reached"),
    "time_limit": ("достигнут лимит времени", "time limit reached"),
    "budget_exceeded": ("исчерпан бюджет", "budget spent"),
    "cancelled": ("остановлено вами", "cancelled by you"),
    "no_changes": ("изменений не потребовалось", "no change was needed"),
    "answer": ("вопрос без правки кода", "a question, not an edit"),
    "malformed_report": ("агент не вернул результат", "the agent returned nothing"),
    "contract_mismatch": ("противоречивый результат", "a contradictory result"),
}


# Kinds that are still running, and so have an elapsed number worth showing. An
# outcome does not: "Done - 43s" times the run, which is a different fact from
# the one this line reports and belongs to Session Detail.
_ACTIVITY_IN_PROGRESS = frozenset(
    {
        ACTIVITY_READING,
        ACTIVITY_SEARCHING,
        ACTIVITY_EDITING,
        ACTIVITY_TESTING,
        ACTIVITY_WORKING,
    }
)

# Colour is a second channel saying the same thing as the words, for the glance
# that does not read them. Waiting borrows the warning colour because it is the
# one state that will not resolve itself.
_ACTIVITY_STYLES: Dict[str, str] = {
    ACTIVITY_COMPLETED: "success",
    ACTIVITY_FAILED: "error",
    ACTIVITY_STOPPED: "warning",
    ACTIVITY_WAITING: "warning",
}

# The published lifecycle status, and the word the line ends on. Keyed by the
# same identifiers `_run_outcome` returns, so the summary a person reads and the
# event the bus receives cannot describe two different runs.
_ACTIVITY_OUTCOME_KINDS: Dict[str, str] = {
    STATUS_COMPLETED: ACTIVITY_COMPLETED,
    STATUS_STOPPED: ACTIVITY_STOPPED,
    STATUS_FAILED: ACTIVITY_FAILED,
}

# Bounded stops that are outcomes rather than aborts: the task finished as an
# answer or genuinely needed no change. These render as Done with their reason
# instead of the warning-coloured Stopped a real abort earns.
_BENIGN_STOP_REASONS = frozenset({"no_changes"})

# Margin, padding and the left rule the `#activity` pane spends before a
# character of text: 2 + 2 margin, 1 + 1 padding, 1 border. Measured against the
# window instead, a line fits the terminal and still wraps inside its own frame.
_ACTIVITY_CHROME_COLUMNS = 7


def _activity_kind_for_tool(name: Any) -> str:
    """What a tool call *means*, never what it is called."""

    return _TOOL_ACTIVITY_KINDS.get(
        _canonical_tool_name(_identifier(name)), ACTIVITY_WORKING
    )


def _activity_reason_words(reason: Any, english: bool) -> str:
    """Localize an outcome reason, or say nothing at all.

    Unlike :func:`_waiting_reason_text`, an unknown identifier here yields the
    empty string rather than itself. That function feeds a diagnostic row where
    a raw identifier beats a blank; this one feeds the single line an ordinary
    user reads, where it would be exactly the internal vocabulary this layer
    exists to keep off the screen.
    """

    identifier = _identifier(reason)
    if not identifier:
        return ""
    words = _ACTIVITY_REASON_WORDS.get(identifier)
    if words is None:
        # `budget_exceeded:output_tokens` is a real published value.
        words = _ACTIVITY_REASON_WORDS.get(identifier.split(":", 1)[0])
    if words is None:
        return ""
    return words[1] if english else words[0]


def _russian_plural(count: int, one: str, few: str, many: str) -> str:
    """Russian needs three forms, and "2 файлов" reads as a bug in the tool."""

    if 11 <= count % 100 <= 14:
        return many
    remainder = count % 10
    if remainder == 1:
        return one
    if 2 <= remainder <= 4:
        return few
    return many


def _activity_files_words(count: int, english: bool) -> str:
    if english:
        return f"{count} file" if count == 1 else f"{count} files"
    return f"{count} {_russian_plural(count, 'файл', 'файла', 'файлов')}"


def _activity_tests_words(count: int, english: bool) -> str:
    if english:
        return f"{count} test passed" if count == 1 else f"{count} tests passed"
    noun = _russian_plural(count, "тест", "теста", "тестов")
    verb = "прошёл" if noun == "тест" else "прошли"
    return f"{count} {noun} {verb}"


def _activity_elapsed_words(seconds: float, english: bool) -> str:
    total = max(int(seconds), 0)
    if total < 60:
        return f"{total}s" if english else f"{total} с"
    minutes, remaining = divmod(total, 60)
    if english:
        return f"{minutes}m {remaining}s"
    return f"{minutes} мин {remaining} с"


@dataclass(frozen=True)
class ActivityAction:
    """One thing the agent is doing, in the only vocabulary the screen speaks.

    Every field is either a catalog key or a number. There is deliberately no
    field for a path, a command, a tool name or a message: the type is the
    boundary, so a caller cannot push free-form text through it by mistake.
    """

    kind: str = ACTIVITY_WORKING
    files: Optional[int] = None
    tests_passed: Optional[int] = None
    elapsed_seconds: Optional[float] = None
    reason: str = ""


def _activity_lines(action: ActivityAction, english: bool) -> Tuple[str, ...]:
    """Render one action into at most :data:`ACTIVITY_MAX_LINES` lines.

    One line for everything that is going well: a reader of "Running tests - 28s"
    already has the only fact they wanted, which is whether to keep waiting. Two
    only for a failure, where the reason is the thing they now have to act on.
    """

    kind = action.kind if action.kind in _ACTIVITY_WORDS else ACTIVITY_WORKING
    russian, plain_english = _ACTIVITY_WORDS[kind]
    parts: List[str] = [plain_english if english else russian]

    files = _optional_positive_int(action.files)
    if files and kind in {ACTIVITY_EDITING, ACTIVITY_COMPLETED}:
        parts.append(_activity_files_words(files, english))
    passed = _optional_positive_int(action.tests_passed)
    if passed:
        parts.append(_activity_tests_words(passed, english))
    elapsed = action.elapsed_seconds
    # Below a second the number is mostly the polling interval, and printing it
    # presents the interface's own latency as the agent's.
    if isinstance(elapsed, (int, float)) and float(elapsed) >= 1.0:
        parts.append(_activity_elapsed_words(float(elapsed), english))

    if kind == ACTIVITY_FAILED:
        parts.append(_ACTIVITY_DETAILS_WORDS[1 if english else 0])
        reason = _activity_reason_words(action.reason, english)
        first = " · ".join(parts)
        return (first, reason) if reason else (first,)

    if kind in {ACTIVITY_STOPPED, ACTIVITY_COMPLETED}:
        reason = _activity_reason_words(action.reason, english)
        if reason:
            parts.append(reason)
    return (" · ".join(parts),)


def _result_changed_paths(result: Any) -> Tuple[str, ...]:
    """Which files a tool result says it changed, for counting only.

    A path is read from the result and never rendered: it is put in a set so
    the same file edited three times counts once, and only the size of that set
    reaches the screen. `changed` has to be true for the path to count, because
    `repo.edit_file` reports the path it examined whether or not it rewrote it,
    and "3 files" over three reads that changed nothing is a lie the user would
    act on.
    """

    if not isinstance(result, dict):
        return ()
    data = result.get("data")
    if not isinstance(data, dict) or not data.get("changed"):
        return ()
    path = data.get("path")
    return (_identifier(path),) if path else ()


def _result_tests_passed(result: Any) -> Optional[int]:
    """How many tests passed, when the result actually counted them.

    An integer or nothing. A check that reports no count contributes no number,
    rather than a zero that would read as "everything failed".
    """

    if not isinstance(result, dict):
        return None
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("passed", "tests_passed"):
        count = _optional_positive_int(data.get(key))
        if count:
            return count
    return None


def _fit_activity_line(line: str, width: int) -> str:
    """Keep one line one line, in a pane that is sometimes forty columns wide.

    Wrapping is what would break the contract: a wrapped line is two rows taken
    from the conversation, and the narrow terminals where those rows are
    scarcest are exactly the ones where wrapping happens.
    """

    if width <= 1 or len(line) <= width:
        return line
    return line[: max(1, width - 1)].rstrip() + "…"


def _activity_text(action: ActivityAction, *, english: bool, width: int = 0) -> str:
    lines = _activity_lines(action, english)[:ACTIVITY_MAX_LINES]
    if width > 0:
        lines = tuple(_fit_activity_line(line, width) for line in lines)
    return "\n".join(lines)


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
    for command, description in _commands("en").items():
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
            cmd_head = value.split(" ", 1)[0]
            if cmd_head in _LINE_INTERACTIVE_ONLY:
                out(
                    "The command " + cmd_head + " needs the interactive KaroX terminal. "
                    "Run `karox` without redirecting stdin.\n"
                )
                continue
            suggestion = _suggest_command(cmd_head, "en")
            if suggestion:
                out(f"Unknown command: {cmd_head}. Type /help. {suggestion}\n")
            else:
                out(f"Unknown command: {cmd_head}. Type /help.\n")
            continue
        selected = _selected_model()
        if selected is None:
            out(
                "No API model is configured. Run karox in an interactive terminal "
                "and press Ctrl+S, or use `karox provider list` / `karox model list`.\n"
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
