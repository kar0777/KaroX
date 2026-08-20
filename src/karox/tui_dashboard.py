"""Usage & Cost and model/effort screens for the KaroX TUI.

The ordinary shell is a chat surface. These modal screens answer the questions
that should not require reading bridge internals or registry dumps:

* which model will answer my next task, at which reasoning effort?
* what did model usage cost today and in this session?

They render only persisted/typed state. Missing pricing is displayed as unknown;
no price is inferred from a model name or from the internet.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from .paths import config_dir, session_dir
from .registry import ProviderRegistry
from .providers import REASONING_EFFORTS
from .sessions import SessionStore
from .usage_analytics import UsageSummary, summarize_records

try:  # pragma: no cover - mirrors the guarded TUI import style
    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.screen import ModalScreen
    from textual.widgets import OptionList, Static
    from textual.widgets.option_list import Option
except Exception:  # pragma: no cover
    pass


def _label(language: str, ru: str, en: str) -> str:
    return en if language != "ru" else ru


def _money(values: dict[str, float]) -> str:
    if not values:
        return "—"
    return " · ".join(
        f"{currency} {amount:.4f}" if amount < 1 else f"{currency} {amount:.2f}"
        for currency, amount in sorted(values.items())
    )


def _tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _today_start_timestamp() -> float:
    now = datetime.now().astimezone()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _usage_snapshot(session_id: Optional[str]) -> tuple[UsageSummary, UsageSummary]:
    """Load persisted sessions once for both Usage cards.

    The old screen called ``SessionStore.list`` twice on every open and refresh:
    once for Today and again for Current session. On a long-lived KaroX install
    that made a tiny modal feel like a database screen. One immutable snapshot is
    enough because both summaries represent the same refresh instant.
    """

    try:
        records = SessionStore(session_dir()).list()
    except Exception:
        return UsageSummary(), UsageSummary()
    today = summarize_records(records, since=_today_start_timestamp())
    current = (
        summarize_records(records, session_id=session_id)
        if session_id
        else UsageSummary()
    )
    return today, current


def _summary_lines(summary: UsageSummary, language: str) -> list[str]:
    if summary.requests == 0:
        return [
            _label(
                language,
                "Пока нет измеренных запросов.",
                "No measured model requests yet.",
            )
        ]
    lines = [
        _label(language, "Стоимость", "Cost") + f": {_money(summary.costs) if summary.costs else _label(language, 'нет цены', 'no pricing')}",
        _label(language, "Токены", "Tokens")
        + f": {_tokens(summary.total_tokens)}  ·  "
        + _label(language, "запросы", "requests")
        + f": {summary.requests}",
        _label(language, "Кэш", "Cache")
        + f": {summary.cache_hit_rate * 100:.1f}%  ·  "
        + _label(language, "прочитано", "read")
        + f": {_tokens(summary.cache_read_tokens)}",
    ]
    if summary.cache_savings:
        lines.append(
            _label(language, "Эффект кэша", "Cache effect")
            + f": +{_money(summary.cache_savings)} "
            + _label(language, "сэкономлено", "saved")
        )
    if summary.retries:
        lines.append(
            _label(language, "Повторы", "Retries") + f": {summary.retries}"
        )
    if summary.legacy_unattributed_sessions:
        lines.append(
            _label(
                language,
                "Старые сессии без поминутной истории не включены в период.",
                "Legacy sessions without request timestamps are excluded from the period.",
            )
        )
    return lines


MODEL_PICKER_CONNECT = "__connect__"
MODEL_PICKER_EFFORT = "__effort__"


class ModelPickerScreen(ModalScreen[Optional[str]]):
    """One fast picker for both the model and the reasoning effort.

    A single list holds an Effort section and a Model section, so switching
    either is one screen and one Enter -- the split the user otherwise pays for
    as two modals. Selecting an effort row returns ``effort:<value>``; a model
    row returns ``model:<provider>:<id>``; the connections row returns
    :data:`MODEL_PICKER_CONNECT`.
    """

    BINDINGS = [
        Binding("escape", "back", "Back", show=False),
        Binding("e", "effort_section", "Effort", show=False),
        Binding("c", "connections", "Connections", show=False),
    ]

    DEFAULT_CSS = """
    ModelPickerScreen { align: center middle; background: rgba(0,0,0,0.35); }
    #model-picker { width: 78; max-width: 94%; height: auto; max-height: 88%;
      padding: 1 2; background: #181511; border: round #6b5c3e; }
    #model-picker-title { text-style: bold; color: #e5e5e5; }
    #model-picker-current { color: #c6bca8; margin: 1 0; }
    #model-picker-list { height: auto; max-height: 12; background: #1a1712; border: none; }
    #model-picker-hint { color: #8a7e6a; margin-top: 1; }
    """

    def __init__(self, language: str, *, effort: Optional[str] = None) -> None:
        super().__init__()
        self.language = language
        self.effort = effort

    def compose(self) -> ComposeResult:
        registry = ProviderRegistry(config_dir() / "vnext" / "providers.json")
        try:
            selected = registry.selected_model()
            providers = {item.provider_id: item for item in registry.providers()}
            models = [
                item
                for item in registry.models()
                if providers.get(item.provider_id) is None
                or bool(getattr(providers[item.provider_id], "enabled", True))
            ]
        except Exception:
            selected = None
            models = []
        current = (
            f"{selected.provider_id}/{selected.model_id}"
            if selected is not None
            else _label(self.language, "не выбрана", "not selected")
        )
        effort = self.effort or "auto"
        with Vertical(id="model-picker"):
            yield Static(
                _label(self.language, "Модель и Effort", "Model and Effort"),
                id="model-picker-title",
            )
            yield Static(
                _label(self.language, "Сейчас", "Current")
                + f": {current}  ·  Effort {effort}",
                id="model-picker-current",
            )
            options = OptionList(id="model-picker-list")
            options.add_option(
                Option(
                    _label(self.language, "Effort", "Effort"),
                    disabled=True,
                )
            )
            for value in ("auto", *REASONING_EFFORTS):
                active = effort == value
                options.add_option(
                    Option(
                        ("✓ " if active else "  ") + value,
                        id=f"effort:{value}",
                    )
                )
            options.add_option(
                Option(
                    _label(self.language, "Модель", "Model"),
                    disabled=True,
                )
            )
            if models:
                for item in models:
                    active = (
                        selected is not None
                        and selected.provider_id == item.provider_id
                        and selected.model_id == item.model_id
                    )
                    mark = "✓ " if active else "  "
                    options.add_option(
                        Option(
                            f"{mark}{item.provider_id}/{item.model_id}",
                            id=f"model:{item.provider_id}:{item.model_id}",
                        )
                    )
            else:
                options.add_option(
                    Option(
                        _label(
                            self.language,
                            "Нет настроенных моделей — открыть подключения",
                            "No configured models — open connections",
                        ),
                        id=MODEL_PICKER_CONNECT,
                    )
                )
            yield options
            yield Static(
                _label(
                    self.language,
                    "Enter — выбрать · E — Effort · C — подключения · Esc — назад",
                    "Enter — select · E — Effort · C — connections · Esc — back",
                ),
                id="model-picker-hint",
            )

    def on_mount(self) -> None:
        options = self.query_one("#model-picker-list", OptionList)
        # Start on the currently selected model rather than the first row, so a
        # blind Enter keeps the selection and an arrow key is all a change costs.
        for index, option in enumerate(options.options):
            option_id = str(getattr(option, "id", "") or "")
            if option_id.startswith("model:") and str(
                getattr(option, "prompt", "")
            ).startswith("✓"):
                options.highlighted = index
                break
        else:
            options.highlighted = self._first_effort_index()
        options.focus()

    def _first_effort_index(self) -> int:
        options = self.query_one("#model-picker-list", OptionList)
        for index, option in enumerate(options.options):
            option_id = str(getattr(option, "id", "") or "")
            if option_id.startswith("effort:"):
                return index
        return 0

    def on_option_list_option_selected(self, event: Any) -> None:
        option_id = str(getattr(event.option, "id", "") or "")
        self.dismiss(option_id or None)

    def action_effort_section(self) -> None:
        options = self.query_one("#model-picker-list", OptionList)
        options.highlighted = self._first_effort_index()

    def action_effort(self) -> None:
        self.action_effort_section()

    def action_connections(self) -> None:
        self.dismiss(MODEL_PICKER_CONNECT)

    def action_back(self) -> None:
        self.dismiss(None)


class UsageCostScreen(ModalScreen[None]):
    """Exact persisted usage by period, with honest unknown-price handling."""

    BINDINGS = [
        Binding("escape", "back", "Back", show=False),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    DEFAULT_CSS = """
    UsageCostScreen { align: center middle; background: rgba(0,0,0,0.35); }
    #usage-dialog { width: 82; max-width: 95%; height: auto; max-height: 92%;
      padding: 1 2; background: #181511; border: round #6b5c3e; }
    #usage-title { text-style: bold; color: #e5e5e5; margin-bottom: 1; }
    .usage-card { margin-bottom: 1; padding: 0 1; border-left: thick #4a4338; }
    #usage-today { border-left: thick #8aab7e; }
    #usage-breakdown { color: #c6bca8; }
    #usage-hint { color: #8a7e6a; }
    """

    def __init__(
        self,
        language: str,
        *,
        session_id: Optional[str] = None,
        model_text: str = "—",
        effort: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.language = language
        self.session_id = session_id
        self.model_text = model_text
        self.effort = effort or "auto"

    def compose(self) -> ComposeResult:
        with Vertical(id="usage-dialog"):
            yield Static(_label(self.language, "Usage & Cost", "Usage & Cost"), id="usage-title")
            yield Static(
                f"{self.model_text}  ·  Effort {self.effort}",
                id="usage-runtime",
                classes="usage-card",
            )
            yield Static("", id="usage-today", classes="usage-card")
            yield Static("", id="usage-session", classes="usage-card")
            yield Static("", id="usage-breakdown", classes="usage-card")
            yield Static(
                _label(self.language, "R — обновить · Esc — назад", "R — refresh · Esc — back"),
                id="usage-hint",
            )

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        loading = _label(self.language, "Загрузка…", "Loading…")
        self.query_one("#usage-today", Static).update(loading)
        self.query_one("#usage-session", Static).update("")
        self.query_one("#usage-breakdown", Static).update("")
        self.run_worker(
            self._load_snapshot,
            thread=True,
            exclusive=True,
            group="usage-refresh",
        )

    def _load_snapshot(self) -> None:
        today, current = _usage_snapshot(self.session_id)
        try:
            self.app.call_from_thread(self._render_snapshot, today, current)
        except Exception:
            return

    def _render_snapshot(self, today: UsageSummary, current: UsageSummary) -> None:
        self.query_one("#usage-today", Static).update(
            _label(self.language, "Сегодня", "Today") + "\n  " + "\n  ".join(_summary_lines(today, self.language))
        )
        session_title = _label(self.language, "Текущая сессия", "Current session")
        self.query_one("#usage-session", Static).update(
            session_title + "\n  " + "\n  ".join(_summary_lines(current, self.language))
        )
        rows: list[str] = []
        for key, item in sorted(
            today.by_provider_model.items(),
            key=lambda pair: sum(float(value) for value in (pair[1].get("costs") or {}).values()),
            reverse=True,
        )[:5]:
            costs = {
                str(name): float(value)
                for name, value in (item.get("costs") or {}).items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
            rows.append(
                f"{key}  ·  {_tokens(int(item.get('total_tokens', 0)))} tok  ·  "
                + (_money(costs) if costs else _label(self.language, "нет цены", "no pricing"))
            )
        if not rows:
            rows.append(_label(self.language, "Нет разбивки за сегодня.", "No breakdown for today."))
        self.query_one("#usage-breakdown", Static).update(
            _label(self.language, "Сегодня по моделям", "Today by model") + "\n  " + "\n  ".join(rows)
        )

    def action_refresh(self) -> None:
        self._refresh()

    def action_back(self) -> None:
        self.dismiss(None)


__all__ = [
    "MODEL_PICKER_CONNECT",
    "MODEL_PICKER_EFFORT",
    "ModelPickerScreen",
    "UsageCostScreen",
]
