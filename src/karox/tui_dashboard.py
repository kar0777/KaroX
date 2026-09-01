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
from typing import Any, Collection, Iterable, Optional

from .agent_modes import DEFAULT_MODE, MODES, mode_display_name, mode_summary
from .effort import (
    AUTO_EFFORT,
    EFFORT_LEVELS,
    effort_display_name,
    effort_summary,
    effort_user_summary,
)
from .paths import config_dir, session_dir
from .registry import ProviderRegistry
from .sessions import SessionStore
from .usage_analytics import UsageSummary, summarize_records

try:  # pragma: no cover - mirrors the guarded TUI import style
    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.screen import ModalScreen
    from textual.widgets import Input, OptionList, Static
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


def _usage_snapshot(
    session_id: Optional[str],
) -> tuple[UsageSummary, UsageSummary, dict[str, Any]]:
    """Load persisted sessions once for every Usage card.

    The old screen called ``SessionStore.list`` twice on every open and refresh:
    once for Today and again for Current session. On a long-lived KaroX install
    that made a tiny modal feel like a database screen. One immutable snapshot is
    enough because all cards represent the same refresh instant. The third
    element is the current session's raw usage dict, which carries the
    newest economy event for the labeled report card.
    """

    try:
        records = SessionStore(session_dir()).list()
    except Exception:
        return UsageSummary(), UsageSummary(), {}
    today = summarize_records(records, since=_today_start_timestamp())
    current = (
        summarize_records(records, session_id=session_id)
        if session_id
        else UsageSummary()
    )
    current_usage: dict[str, Any] = {}
    if session_id:
        for record in records:
            if record.session_id == session_id and isinstance(record.usage, dict):
                current_usage = record.usage
                break
    return today, current, current_usage


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


# ---------------------------------------------------------------------------
# Model browser helpers. Pure, presentation-only, unit-testable.
#
# Every helper keeps the registry's true/false/unknown contract: a capability
# or price the provider never published renders as "?" and never satisfies a
# positive filter. Nothing here invents metadata.
# ---------------------------------------------------------------------------

MODEL_BROWSER_FILTERS: tuple[str, ...] = (
    "free",
    "tools",
    "vision",
    "structured_output",
    "streaming",
)

# Compact capability legend: T tools, V vision, J structured output (JSON),
# S streaming. A letter means an explicit "true", "-" an explicit "false",
# and "?" an honest unknown.
_CAP_LETTERS: tuple[tuple[str, str], ...] = (
    ("tools", "T"),
    ("vision", "V"),
    ("structured_output", "J"),
    ("streaming", "S"),
)


def model_free_state(record: Any) -> str:
    """"true"/"false"/"unknown" free verdict from published prices only."""

    pricing = getattr(record, "pricing", None)
    if pricing is None:
        return "unknown"
    try:
        free = (
            float(pricing.input_per_million) == 0.0
            and float(pricing.output_per_million) == 0.0
        )
    except (TypeError, ValueError):
        return "unknown"
    return "true" if free else "false"


def _filter_state(record: Any, name: str) -> str:
    if name == "free":
        return model_free_state(record)
    value = str(getattr(record, name, "unknown"))
    return value if value in ("true", "false") else "unknown"


def apply_model_filters(
    models: Iterable[Any], query: str = "", active: Collection[str] = ()
) -> list[Any]:
    """Rows matching the query and every active filter.

    A filter passes only on an explicit "true": unknown metadata never
    satisfies a capability request, so a filtered list cannot overpromise.
    The query matches the provider id, the model id, and the published
    display name, case-insensitively.
    """

    text = (query or "").strip().casefold()
    wanted = set(active)
    active_names = [name for name in MODEL_BROWSER_FILTERS if name in wanted]
    result: list[Any] = []
    for item in models:
        haystack = (
            f"{getattr(item, 'provider_id', '')}/{getattr(item, 'model_id', '')} "
            f"{getattr(item, 'display_name', None) or ''}"
        ).casefold()
        if text and text not in haystack:
            continue
        if any(_filter_state(item, name) != "true" for name in active_names):
            continue
        result.append(item)
    return result


def model_browser_budget(width: int) -> int:
    """Usable characters in one browser row at a given terminal width."""

    return max(28, min(int(width * 0.9), 120) - 6)


def model_browser_columns(width: int) -> tuple[str, ...]:
    """The responsive column plan: identity first, richer metadata wider.

    Tested against the mandated terminal sizes from 40x12 to 160x45. A small
    terminal keeps identity plus essential capabilities; pricing, cache rate,
    display name, and provenance appear only when they honestly fit.
    """

    budget = model_browser_budget(width)
    if budget < 40:
        return ("id", "caps")
    if budget < 56:
        return ("id", "caps", "free")
    if budget < 64:
        return ("id", "provider", "caps", "free")
    if budget < 80:
        return ("id", "provider", "caps", "free", "context")
    if budget < 100:
        return ("id", "provider", "caps", "free", "context", "price")
    return (
        "id",
        "name",
        "provider",
        "caps",
        "free",
        "context",
        "price",
        "cache",
        "provenance",
    )


def _clip(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text.ljust(limit)
    if limit == 1:
        return "…"
    return text[: limit - 1] + "…"


def _context_cell(record: Any) -> str:
    value = getattr(record, "context_window", None)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return "?"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"{value // 1_000}k"
    return str(value)


def _price(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "?"
    return f"${number:g}"


def _price_cell(record: Any) -> str:
    pricing = getattr(record, "pricing", None)
    if pricing is None:
        return "$?"
    return (
        f"{_price(pricing.input_per_million)}/"
        f"{_price(pricing.output_per_million)}"
    )


def _cache_cell(record: Any) -> str:
    pricing = getattr(record, "pricing", None)
    value = getattr(pricing, "cache_read_per_million", None) if pricing else None
    return _price(value) if value is not None else "$?"


def _caps_cell(record: Any) -> str:
    cells = []
    for name, letter in _CAP_LETTERS:
        value = str(getattr(record, name, "unknown"))
        cells.append(letter if value == "true" else "-" if value == "false" else "?")
    return "".join(cells)


def _free_cell(record: Any) -> str:
    state = model_free_state(record)
    return {"true": "free", "false": "paid"}.get(state, "?")


_COLUMN_WIDTHS: dict[str, int] = {
    "name": 18,
    "provider": 12,
    "caps": 4,
    "free": 4,
    "context": 6,
    "price": 11,
    "cache": 7,
    "provenance": 4,
}


def model_row_text(record: Any, width: int, selected: bool = False) -> str:
    """One browser row, responsive to the terminal width, nothing invented."""

    columns = model_browser_columns(width)
    budget = model_browser_budget(width)
    flexible = budget - 2
    for column in columns:
        if column != "id":
            flexible -= _COLUMN_WIDTHS[column] + 2
    cells: list[str] = []
    for column in columns:
        if column == "id":
            cells.append(
                _clip(str(getattr(record, "model_id", "")), max(flexible, 12))
            )
        elif column == "name":
            cells.append(
                _clip(
                    str(getattr(record, "display_name", None) or "?"),
                    _COLUMN_WIDTHS["name"],
                )
            )
        elif column == "provider":
            cells.append(
                _clip(str(getattr(record, "provider_id", "")), _COLUMN_WIDTHS["provider"])
            )
        elif column == "caps":
            cells.append(_clip(_caps_cell(record), _COLUMN_WIDTHS["caps"]))
        elif column == "free":
            cells.append(_clip(_free_cell(record), _COLUMN_WIDTHS["free"]))
        elif column == "context":
            cells.append(_clip(_context_cell(record), _COLUMN_WIDTHS["context"]))
        elif column == "price":
            cells.append(_clip(_price_cell(record), _COLUMN_WIDTHS["price"]))
        elif column == "cache":
            cells.append(_clip(_cache_cell(record), _COLUMN_WIDTHS["cache"]))
        elif column == "provenance":
            cells.append(
                _clip(str(getattr(record, "provenance", "?"))[:4], _COLUMN_WIDTHS["provenance"])
            )
    mark = "✓ " if selected else "  "
    return (mark + "  ".join(cells)).rstrip()


def model_simple_row_text(record: Any, selected: bool = False) -> str:
    """Calm default model row: identity first, technical metadata on demand."""

    model_id = str(getattr(record, "model_id", "") or "?")
    provider_id = str(getattr(record, "provider_id", "") or "?")
    suffix = " · free" if model_free_state(record) == "true" else ""
    mark = "✓ " if selected else "  "
    return f"{mark}{model_id}  ·  {provider_id}{suffix}"


def _verdict(value: str, language: str) -> str:
    if value == "true":
        return _label(language, "да", "yes")
    if value == "false":
        return _label(language, "нет", "no")
    return "?"


def model_detail_lines(record: Any, language: str) -> list[str]:
    """Full honest metadata for one highlighted model, provenance included."""

    name = str(getattr(record, "display_name", None) or "") or _label(
        language, "не указано", "not published"
    )
    caps = " · ".join(
        f"{label} {_verdict(str(getattr(record, field, 'unknown')), language)}"
        for field, label in (
            ("tools", _label(language, "Инструменты", "Tools")),
            ("vision", _label(language, "Зрение", "Vision")),
            ("structured_output", "JSON"),
            ("streaming", _label(language, "Стриминг", "Streaming")),
        )
    )
    unknown = _label(language, "неизвестно", "unknown")
    pricing = getattr(record, "pricing", None)
    if pricing is None:
        price_line = _label(
            language,
            "Цены не опубликованы",
            "No published pricing",
        )
        source = ""
    else:
        cache = getattr(pricing, "cache_read_per_million", None)
        cached = f"Cached {_price(cache)}/M · " if cache is not None else ""
        price_line = (
            f"In {_price(pricing.input_per_million)}/M · "
            + cached
            + f"Out {_price(pricing.output_per_million)}/M"
        )
        source = f" · {getattr(pricing, 'source', '')}"
    context = getattr(record, "context_window", None)
    max_out = getattr(record, "max_output_tokens", None)
    return [
        f"{getattr(record, 'provider_id', '?')}/{getattr(record, 'model_id', '?')}"
        f" · {name}",
        f"{caps} · {_free_cell(record)}",
        _label(language, "Контекст", "Context")
        + f": {context if context else unknown} · "
        + _label(language, "макс. ответ", "max output")
        + f": {max_out if max_out else unknown} · {price_line} · "
        + _label(language, "источник", "origin")
        + f": {getattr(record, 'provenance', 'manual')}{source}",
    ]


class ModelPickerScreen(ModalScreen[Optional[str]]):
    """One focused model browser: search, metadata, refresh, and selection.

    Model choice and Effort are deliberately separate product actions. A model
    row returns ``model:<provider>:<id>`` and the connections row returns
    :data:`MODEL_PICKER_CONNECT`. Effort lives in :class:`EffortPickerScreen`.

    The list renders honest responsive metadata columns (see
    :func:`model_browser_columns`), filters only on explicit "true" verdicts,
    and can rediscover the catalog in place over the exact wire the wizard
    and ``/models refresh`` use.
    """

    BINDINGS = [
        Binding("escape", "back", "Back", show=False),
        Binding("c", "connections", "Connections", show=False),
        Binding("d", "toggle_details", "Details", show=False),
        Binding("slash", "focus_search", "Search", show=False),
        Binding("f", "toggle_free", "Free", show=False),
        Binding("t", "toggle_tools", "Tools", show=False),
        Binding("v", "toggle_vision", "Vision", show=False),
        Binding("o", "toggle_structured", "JSON", show=False),
        Binding("s", "toggle_streaming", "Streaming", show=False),
        Binding("ctrl+r", "refresh_models", "Refresh", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    ModelPickerScreen { align: center middle; background: rgba(0,0,0,0.35); }
    #model-picker { width: 90%; max-width: 120; height: auto; max-height: 92%;
      padding: 1 2; background: #181511; border: round #6b5c3e; }
    #model-picker-title { text-style: bold; color: #e5e5e5; }
    #model-picker-current { color: #c6bca8; margin-top: 1; }
    #model-picker-search { margin-top: 1; }
    #model-picker-filters { color: #d4b676; }
    #model-picker-list { height: auto; max-height: 14; background: #1a1712; border: none; }
    #model-picker-details { color: #c6bca8; height: auto; max-height: 4; }
    #model-picker-hint { color: #8a7e6a; margin-top: 1; }
    """

    def __init__(self, language: str) -> None:
        super().__init__()
        self.language = language
        self.show_advanced = False
        self._query = ""
        self._filters: set[str] = set()
        self._status = ""
        self._selected: Any = None
        self._models: list[Any] = []
        self._visible: list[Any] = []

    def _read_registry(self) -> None:
        registry = ProviderRegistry(config_dir() / "vnext" / "providers.json")
        try:
            self._selected = registry.selected_model()
            providers = {item.provider_id: item for item in registry.providers()}
            self._models = [
                item
                for item in registry.models()
                if providers.get(item.provider_id) is None
                or bool(getattr(providers[item.provider_id], "enabled", True))
            ]
        except Exception:
            self._selected = None
            self._models = []

    def _width(self) -> int:
        try:
            return int(self.app.size.width)
        except Exception:
            return 80

    def compose(self) -> ComposeResult:
        self._read_registry()
        current = (
            f"{self._selected.provider_id}/{self._selected.model_id}"
            if self._selected is not None
            else _label(self.language, "не выбрана", "not selected")
        )
        with Vertical(id="model-picker"):
            yield Static(
                _label(self.language, "Модели", "Models"),
                id="model-picker-title",
            )
            yield Static(
                _label(self.language, "Сейчас", "Current") + f": {current}",
                id="model-picker-current",
            )
            yield Input(
                placeholder=_label(
                    self.language,
                    "Поиск: id, имя, провайдер…",
                    "Search: id, name, provider…",
                ),
                id="model-picker-search",
            )
            yield Static("", id="model-picker-filters")
            yield OptionList(id="model-picker-list")
            yield Static("", id="model-picker-details")
            yield Static(
                _label(
                    self.language,
                    "Enter — использовать · / — поиск · D — детали · Ctrl+R — обновить · C — подключения · Esc — назад",
                    "Enter — use · / — search · D — details · Ctrl+R — refresh · C — connections · Esc — back",
                ),
                id="model-picker-hint",
            )

    def on_mount(self) -> None:
        self._rebuild()
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
            options.highlighted = 0 if options.option_count else None
        options.focus()

    def _rebuild(self) -> None:
        options = self.query_one("#model-picker-list", OptionList)
        options.clear_options()
        self._visible = apply_model_filters(self._models, self._query, self._filters)
        width = self._width()
        if self._visible:
            for item in self._visible:
                active = (
                    self._selected is not None
                    and self._selected.provider_id == item.provider_id
                    and self._selected.model_id == item.model_id
                )
                row_text = (
                    model_row_text(item, width, selected=active)
                    if self.show_advanced
                    else model_simple_row_text(item, selected=active)
                )
                options.add_option(
                    Option(row_text, id=f"model:{item.provider_id}:{item.model_id}")
                )
        elif not self._models:
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
        else:
            options.add_option(
                Option(
                    _label(
                        self.language,
                        "Ничего не найдено — измените поиск или фильтры",
                        "No matches — change the search or filters",
                    ),
                    disabled=True,
                )
            )
        self._render_filters()
        self._render_details()

    def _render_filters(self) -> None:
        if not self.show_advanced:
            self.query_one("#model-picker-filters", Static).update(self._status)
            return
        labels = {
            "free": "Free",
            "tools": "Tools",
            "vision": "Vision",
            "structured_output": "JSON",
            "streaming": "Stream",
        }
        parts = [
            ("[x] " if name in self._filters else "[ ] ") + labels[name]
            for name in MODEL_BROWSER_FILTERS
        ]
        text = (
            _label(self.language, "Фильтры", "Filters")
            + ": "
            + "  ".join(parts)
        )
        if self._status:
            text += "  ·  " + self._status
        self.query_one("#model-picker-filters", Static).update(text)

    def _render_details(self) -> None:
        details = self.query_one("#model-picker-details", Static)
        if not self.show_advanced:
            details.update("")
            return
        record = self._highlighted_record()
        if record is None:
            details.update("")
            return
        details.update("\n".join(model_detail_lines(record, self.language)))

    def _highlighted_record(self) -> Optional[Any]:
        options = self.query_one("#model-picker-list", OptionList)
        index = options.highlighted
        if index is None:
            return None
        try:
            option = options.get_option_at_index(index)
        except Exception:
            return None
        option_id = str(getattr(option, "id", "") or "")
        if not option_id.startswith("model:"):
            return None
        _, provider_id, model_id = (option_id.split(":", 2) + ["", ""])[:3]
        for item in self._visible:
            if item.provider_id == provider_id and item.model_id == model_id:
                return item
        return None

    def on_input_changed(self, event: Any) -> None:
        if str(getattr(getattr(event, "input", None), "id", "") or "") != (
            "model-picker-search"
        ):
            return
        self._query = str(getattr(event, "value", "") or "")
        self._rebuild()
        if self._query.strip():
            options = self.query_one("#model-picker-list", OptionList)
            for index, option in enumerate(options.options):
                if str(getattr(option, "id", "") or "").startswith("model:"):
                    options.highlighted = index
                    break
            self._render_details()

    def on_option_list_option_highlighted(self, event: Any) -> None:
        self._render_details()

    def on_option_list_option_selected(self, event: Any) -> None:
        option_id = str(getattr(event.option, "id", "") or "")
        self.dismiss(option_id or None)

    def _toggle(self, name: str) -> None:
        self.show_advanced = True
        if name in self._filters:
            self._filters.discard(name)
        else:
            self._filters.add(name)
        self._rebuild()

    def action_toggle_details(self) -> None:
        record = self._highlighted_record()
        self.show_advanced = not self.show_advanced
        self._rebuild()
        if record is not None:
            target = f"model:{record.provider_id}:{record.model_id}"
            options = self.query_one("#model-picker-list", OptionList)
            for index, option in enumerate(options.options):
                if str(getattr(option, "id", "") or "") == target:
                    options.highlighted = index
                    break
        self._render_details()

    def action_toggle_free(self) -> None:
        self._toggle("free")

    def action_toggle_tools(self) -> None:
        self._toggle("tools")

    def action_toggle_vision(self) -> None:
        self._toggle("vision")

    def action_toggle_structured(self) -> None:
        self._toggle("structured_output")

    def action_toggle_streaming(self) -> None:
        self._toggle("streaming")

    def action_focus_search(self) -> None:
        self.query_one("#model-picker-search", Input).focus()

    def action_refresh_models(self) -> None:
        """Rediscover the catalog in place, over the /model refresh wire.

        Uses the provider of the highlighted model, else the selected model,
        else the first configured one. Honest counts; errors are redacted;
        a failure never clears the list that was already on screen.
        """

        provider_id = self._refresh_provider_id()
        if provider_id is None:
            self._status = _label(
                self.language,
                "Нет провайдера для обновления",
                "No provider to refresh",
            )
            self._render_filters()
            return
        self._status = _label(
            self.language,
            f"Обновляю {provider_id}…",
            f"Refreshing {provider_id}…",
        )
        self._render_filters()

        def execute() -> None:
            try:
                from .tui import _provider_controller
                from .tui_connections import discover_models_for_provider

                summary = discover_models_for_provider(
                    _provider_controller(), provider_id
                )
            except Exception as exc:
                try:
                    from .tui import redact

                    detail = str(redact(exc))
                except Exception:
                    detail = "error"
                status = (
                    _label(
                        self.language,
                        "Ошибка обновления",
                        "Refresh failed",
                    )
                    + f": {detail}"
                )
            else:
                added = len(summary.get("added") or [])
                discovered = summary.get("discovered", 0)
                status = _label(
                    self.language,
                    f"Найдено: {discovered}, новых: {added}",
                    f"Discovered {discovered}, {added} new",
                )
            try:
                self.app.call_from_thread(self._after_refresh, status)
            except Exception:
                return

        self.run_worker(
            execute, thread=True, exclusive=True, group="model-browser-refresh"
        )

    def _refresh_provider_id(self) -> Optional[str]:
        record = self._highlighted_record()
        if record is not None:
            return str(record.provider_id)
        if self._selected is not None:
            return str(self._selected.provider_id)
        if self._models:
            return str(self._models[0].provider_id)
        return None

    def _after_refresh(self, status: str) -> None:
        self._status = status
        self._read_registry()
        self._rebuild()

    def action_connections(self) -> None:
        self.dismiss(MODEL_PICKER_CONNECT)

    def action_back(self) -> None:
        self.dismiss(None)


class ModePickerScreen(ModalScreen[Optional[str]]):
    """Human-first Build/Plan/Ideate picker used by ``/mode``."""

    BINDINGS = [Binding("escape", "back", "Back", show=False)]

    DEFAULT_CSS = """
    ModePickerScreen { align: center middle; background: rgba(0,0,0,0.35); }
    #mode-picker { width: 62; max-width: 94%; height: auto; padding: 1 2;
      background: #181511; border: round #6b5c3e; }
    #mode-picker-title { text-style: bold; color: #e5e5e5; }
    #mode-picker-current { color: #c6bca8; margin-top: 1; }
    #mode-picker-list { height: auto; max-height: 5; margin-top: 1;
      background: #1a1712; border: none; }
    #mode-picker-details { color: #c6bca8; height: auto; min-height: 2; }
    #mode-picker-hint { color: #8a7e6a; margin-top: 1; }
    """

    def __init__(self, language: str, *, mode: str = DEFAULT_MODE) -> None:
        super().__init__()
        self.language = language
        self.mode = mode if mode in MODES else DEFAULT_MODE

    def compose(self) -> ComposeResult:
        with Vertical(id="mode-picker"):
            yield Static(
                _label(self.language, "Как KaroX должен работать?", "How should KaroX work?"),
                id="mode-picker-title",
            )
            yield Static(
                _label(self.language, "Сейчас", "Current")
                + f": {mode_display_name(self.mode, self.language)}",
                id="mode-picker-current",
            )
            yield OptionList(id="mode-picker-list")
            yield Static("", id="mode-picker-details", markup=False)
            yield Static(
                _label(
                    self.language,
                    "Enter — применить · ↑/↓ — выбрать · Esc — назад",
                    "Enter — apply · ↑/↓ — choose · Esc — back",
                ),
                id="mode-picker-hint",
            )

    def on_mount(self) -> None:
        options = self.query_one("#mode-picker-list", OptionList)
        for value in MODES:
            options.add_option(
                Option(
                    ("✓ " if value == self.mode else "  ")
                    + mode_display_name(value, self.language),
                    id=value,
                )
            )
        options.highlighted = next(
            (
                index
                for index, option in enumerate(options.options)
                if getattr(option, "id", None) == self.mode
            ),
            0,
        )
        options.focus()
        self._render_details()

    def on_option_list_option_highlighted(self, _event: Any) -> None:
        self._render_details()

    def on_option_list_option_selected(self, event: Any) -> None:
        value = str(getattr(event.option, "id", "") or "")
        self.dismiss(value or None)

    def _render_details(self) -> None:
        options = self.query_one("#mode-picker-list", OptionList)
        index = options.highlighted
        if index is None:
            return
        try:
            value = str(getattr(options.get_option_at_index(index), "id", "") or "")
        except Exception:
            return
        text = mode_summary(value, self.language) if value in MODES else ""
        self.query_one("#mode-picker-details", Static).update(text)

    def action_back(self) -> None:
        self.dismiss(None)


class EffortPickerScreen(ModalScreen[Optional[str]]):
    """Small dedicated Effort picker; model selection lives in /models."""

    BINDINGS = [
        Binding("escape", "back", "Back", show=False),
        Binding("d", "toggle_details", "Details", show=False),
    ]

    DEFAULT_CSS = """
    EffortPickerScreen { align: center middle; background: rgba(0,0,0,0.35); }
    #effort-picker { width: 58; max-width: 94%; height: auto; padding: 1 2;
      background: #181511; border: round #6b5c3e; }
    #effort-picker-title { text-style: bold; color: #e5e5e5; }
    #effort-picker-current { color: #c6bca8; margin-top: 1; }
    #effort-picker-list { height: auto; max-height: 9; margin-top: 1;
      background: #1a1712; border: none; }
    #effort-picker-details { color: #c6bca8; height: auto; min-height: 2; }
    #effort-picker-hint { color: #8a7e6a; margin-top: 1; }
    """

    def __init__(self, language: str, *, effort: str = AUTO_EFFORT) -> None:
        super().__init__()
        self.language = language
        self.effort = effort if effort in {AUTO_EFFORT, *EFFORT_LEVELS} else AUTO_EFFORT
        self.show_advanced = False

    def compose(self) -> ComposeResult:
        with Vertical(id="effort-picker"):
            yield Static(_label(self.language, "Effort", "Effort"), id="effort-picker-title")
            yield Static(
                _label(self.language, "Сейчас", "Current")
                + f": {effort_display_name(self.effort, self.language)}",
                id="effort-picker-current",
            )
            yield OptionList(id="effort-picker-list")
            yield Static("", id="effort-picker-details", markup=False)
            yield Static(
                _label(
                    self.language,
                    "Enter — применить · ↑/↓ — выбрать · D — детали · Esc — назад",
                    "Enter — apply · ↑/↓ — choose · D — details · Esc — back",
                ),
                id="effort-picker-hint",
            )

    def on_mount(self) -> None:
        options = self.query_one("#effort-picker-list", OptionList)
        for value in (AUTO_EFFORT, *EFFORT_LEVELS):
            options.add_option(
                Option(
                    ("✓ " if value == self.effort else "  ")
                    + effort_display_name(value, self.language),
                    id=value,
                )
            )
        options.highlighted = next(
            (index for index, option in enumerate(options.options) if getattr(option, "id", None) == self.effort),
            0,
        )
        options.focus()
        self._render_details()

    def on_option_list_option_highlighted(self, _event: Any) -> None:
        self._render_details()

    def on_option_list_option_selected(self, event: Any) -> None:
        value = str(getattr(event.option, "id", "") or "")
        self.dismiss(value or None)

    def _render_details(self) -> None:
        options = self.query_one("#effort-picker-list", OptionList)
        index = options.highlighted
        if index is None:
            return
        try:
            value = str(getattr(options.get_option_at_index(index), "id", "") or "")
        except Exception:
            return
        if value in {AUTO_EFFORT, *EFFORT_LEVELS}:
            text = (
                effort_summary(value, self.language)
                if self.show_advanced and value != AUTO_EFFORT
                else effort_user_summary(value, self.language)
            )
        else:
            text = ""
        self.query_one("#effort-picker-details", Static).update(text)

    def action_toggle_details(self) -> None:
        self.show_advanced = not self.show_advanced
        self._render_details()

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
            yield Static("", id="usage-report", classes="usage-card")
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
        today, current, current_usage = _usage_snapshot(self.session_id)
        try:
            self.app.call_from_thread(
                self._render_snapshot, today, current, current_usage
            )
        except Exception:
            return

    def _render_snapshot(
        self,
        today: UsageSummary,
        current: UsageSummary,
        current_usage: Optional[dict[str, Any]] = None,
    ) -> None:
        self.query_one("#usage-today", Static).update(
            _label(self.language, "Сегодня", "Today") + "\n  " + "\n  ".join(_summary_lines(today, self.language))
        )
        session_title = _label(self.language, "Текущая сессия", "Current session")
        self.query_one("#usage-session", Static).update(
            session_title + "\n  " + "\n  ".join(_summary_lines(current, self.language))
        )
        # Mandate-shaped labeled report for the current session: every value
        # carries exactly one of MEASURED / ESTIMATED / UNAVAILABLE, and a
        # counter nobody reported renders as an em dash, never a zero.
        from .usage_report import build_usage_report, last_economy_event

        report = build_usage_report(
            self.language,
            runtime_line=f"{self.model_text}  ·  Effort {self.effort}",
            summary=current,
            economy=last_economy_event(current_usage or {}),
        )
        self.query_one("#usage-report", Static).update("\n".join(report))
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
    "MODEL_BROWSER_FILTERS",
    "MODEL_PICKER_CONNECT",
    "MODEL_PICKER_EFFORT",
    "EffortPickerScreen",
    "ModePickerScreen",
    "ModelPickerScreen",
    "UsageCostScreen",
    "apply_model_filters",
    "model_browser_budget",
    "model_browser_columns",
    "model_detail_lines",
    "model_free_state",
    "model_row_text",
]
