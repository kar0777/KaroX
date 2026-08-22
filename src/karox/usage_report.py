"""Mandate-shaped /usage and /economy rendering with honest value labels.

Every number this module shows carries exactly one of three labels:

* ``MEASURED``   -- the value was reported by the provider or counted in
  this process; it is a fact.
* ``ESTIMATED``  -- the value was computed from measured counts and
  registry rates that carry provenance; it is an estimate and says so.
* ``UNAVAILABLE`` -- nobody reported the value. It renders as an em dash,
  never as a zero, because a zero would be an invented number: the
  persisted usage schema writes ``0`` for counters a provider never
  reported, so a bare zero is genuinely indistinguishable from "not
  reported" and must not masquerade as a measurement.

The module is pure presentation over persisted state: no I/O, no pricing
lookups of its own, no provider calls. Screens feed it summaries and the
last per-request economy event; it returns lines.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .usage_analytics import USAGE_EVENTS_KEY, UsageSummary

MEASURED = "MEASURED"
ESTIMATED = "ESTIMATED"
UNAVAILABLE = "UNAVAILABLE"

_DASH = "—"


def _label(language: str, ru: str, en: str) -> str:
    return en if language != "ru" else ru


def _tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _line(name: str, rendered: str, label: str) -> str:
    return f"{name}: {rendered} · {label}"


def _measured_count(name: str, value: int) -> str:
    return _line(name, _tokens(value), MEASURED)


def _unavailable(name: str) -> str:
    return _line(name, _DASH, UNAVAILABLE)


def _count_or_unavailable(name: str, value: int, *, confirmed: bool) -> str:
    """A counter that is only a fact when positive or its channel is proven.

    The persisted schema writes 0 for counters the provider never sent, so
    ``0`` alone proves nothing. A positive count is always MEASURED; a zero
    is MEASURED only when the reporting channel itself was confirmed (for
    cache counters: the scheduler saw this provider report cached tokens at
    least once), otherwise it is honestly UNAVAILABLE.
    """

    if value > 0 or confirmed:
        return _measured_count(name, value)
    return _unavailable(name)


def last_economy_event(usage: Mapping[str, Any]) -> dict[str, Any]:
    """The newest per-request event that carries economy counters.

    Kernel economy counters are cumulative within a run, so the latest
    event is the authoritative snapshot; older events would understate.
    Returns an empty dict when the session has no economy-bearing events.
    """

    events = usage.get(USAGE_EVENTS_KEY)
    if not isinstance(events, list):
        return {}
    for event in reversed(events):
        if isinstance(event, Mapping) and any(
            str(key).startswith("economy_") for key in event
        ):
            return dict(event)
    return {}


def build_usage_report(
    language: str,
    *,
    runtime_line: str,
    summary: UsageSummary,
    economy: Mapping[str, Any],
    estimated_cost: Optional[float] = None,
    estimated_currency: str = "USD",
) -> list[str]:
    """Mandate section-9 report: INPUT / OUTPUT / KAROX ECONOMY / COST."""

    lines: list[str] = [runtime_line]
    if summary.requests == 0:
        lines.append(
            _label(
                language,
                "Пока нет измеренных запросов.",
                "No measured model requests yet.",
            )
        )
        return lines

    cache_confirmed = str(economy.get("economy_cache_capability") or "") == "CONFIRMED"

    # INPUT ---------------------------------------------------------------
    lines.append("INPUT")
    uncached = max(
        0,
        summary.prompt_tokens
        - summary.cache_read_tokens
        - summary.cache_write_tokens,
    )
    lines.append("  " + _measured_count(_label(language, "без кэша", "uncached"), uncached))
    lines.append(
        "  "
        + _count_or_unavailable(
            _label(language, "из кэша", "cached"),
            summary.cache_read_tokens,
            confirmed=cache_confirmed,
        )
    )
    lines.append(
        "  "
        + _count_or_unavailable(
            _label(language, "запись в кэш", "cache writes"),
            summary.cache_write_tokens,
            confirmed=cache_confirmed,
        )
    )

    # OUTPUT --------------------------------------------------------------
    lines.append("OUTPUT")
    visible = max(0, summary.completion_tokens - summary.reasoning_tokens)
    lines.append("  " + _measured_count(_label(language, "видимый", "visible"), visible))
    lines.append(
        "  "
        + _count_or_unavailable(
            _label(language, "рассуждения", "reasoning/thinking"),
            summary.reasoning_tokens,
            confirmed=False,
        )
    )

    # KAROX ECONOMY ---------------------------------------------------------
    lines.append("KAROX ECONOMY")
    lines.extend("  " + item for item in economy_measurement_lines(language, economy))

    # COST ----------------------------------------------------------------
    lines.append(_label(language, "СТОИМОСТЬ", "COST"))
    if summary.costs:
        rendered = " · ".join(
            f"{currency} {amount:.4f}" for currency, amount in sorted(summary.costs.items())
        )
        lines.append("  " + _line(_label(language, "фактическая", "actual"), rendered, MEASURED))
    elif estimated_cost is not None:
        lines.append(
            "  "
            + _line(
                _label(language, "фактическая", "actual"),
                f"{estimated_currency} {estimated_cost:.4f}",
                ESTIMATED,
            )
        )
    else:
        lines.append("  " + _unavailable(_label(language, "фактическая", "actual")))
    if summary.uncached_costs:
        rendered = " · ".join(
            f"{currency} {amount:.4f}"
            for currency, amount in sorted(summary.uncached_costs.items())
        )
        lines.append(
            "  "
            + _line(
                _label(language, "базовая без экономии", "estimated baseline"),
                rendered,
                MEASURED,
            )
        )
    else:
        lines.append(
            "  " + _unavailable(_label(language, "базовая без экономии", "estimated baseline"))
        )
    if summary.cache_savings:
        rendered = " · ".join(
            f"{currency} {amount:.4f}"
            for currency, amount in sorted(summary.cache_savings.items())
        )
        lines.append(
            "  " + _line(_label(language, "сэкономлено", "saved"), rendered, MEASURED)
        )
    else:
        saving = economy.get("economy_cache_saving_estimated_usd")
        if isinstance(saving, (int, float)) and not isinstance(saving, bool):
            lines.append(
                "  "
                + _line(
                    _label(language, "сэкономлено", "saved"),
                    f"USD {float(saving):.4f}",
                    ESTIMATED,
                )
            )
        else:
            lines.append("  " + _unavailable(_label(language, "сэкономлено", "saved")))
    return lines


def _economy_int(economy: Mapping[str, Any], key: str) -> Optional[int]:
    value = economy.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def economy_measurement_lines(
    language: str, economy: Mapping[str, Any]
) -> list[str]:
    """The measured economy counters, one line per mandate bullet.

    A counter the runtime never wrote renders UNAVAILABLE; a written
    counter is MEASURED even at zero, because the kernel always writes
    these keys once the subsystem ran.
    """

    def counted(name_ru: str, name_en: str, key: str, unit: str = "") -> str:
        value = _economy_int(economy, key)
        name = _label(language, name_ru, name_en)
        if value is None:
            return _unavailable(name)
        rendered = f"{value}{unit}"
        return _line(name, rendered, MEASURED)

    lines = [
        counted("Context Compiler: элементов", "Context Compiler items", "economy_context_items"),
        counted(
            "повторный контекст убран, символов",
            "duplicate context avoided, chars",
            "economy_reused_chars",
        ),
        counted(
            "схемы инструментов не отправлены, байт",
            "schema bytes avoided",
            "economy_tool_schema_bytes_avoided",
        ),
        counted(
            "повторные чтения из кэша",
            "repeated reads avoided",
            "economy_read_cache_hits",
        ),
        counted(
            "ToolVM: ходов модели сэкономлено",
            "ToolVM model turns avoided",
            "economy_batched_turns_avoided",
        ),
        counted(
            "перенесённые выводы (continuity)",
            "continuity statements carried",
            "economy_continuity_statements",
        ),
    ]
    stable = _economy_int(economy, "economy_prefix_stable_steps")
    total = _economy_int(economy, "economy_prefix_total_steps")
    prefix_name = _label(language, "стабильный префикс", "prefix reuse")
    if stable is None or total is None:
        lines.append(_unavailable(prefix_name))
    else:
        lines.append(_line(prefix_name, f"{stable}/{total}", MEASURED))
    verdict = economy.get("economy_cache_verdict")
    cache_name = _label(language, "кэш провайдера", "provider cache")
    if isinstance(verdict, str) and verdict:
        capability = str(economy.get("economy_cache_capability") or "UNKNOWN")
        lines.append(_line(cache_name, f"{verdict} ({capability})", MEASURED))
    else:
        lines.append(_unavailable(cache_name))
    return lines


def economy_status_lines(
    language: str,
    *,
    economy: Mapping[str, Any],
    economy_mode: bool,
) -> list[str]:
    """Mandate section-10 subsystem status for the /economy command.

    Economy is infrastructure optimization; Effort owns depth/quality.
    Every subsystem row shows whether it is applied or shadow-measuring
    plus its real measurement, and the quality-guard row states the
    contract rather than a number because the contract is the guarantee.
    """

    applied = _label(language, "применяется", "applied")
    shadow = _label(language, "измеряется (без применения)", "shadow-measuring")
    state = applied if economy_mode else shadow

    def row(name: str, detail: str) -> str:
        return f"{name}: {state} · {detail}"

    def count_detail(key: str, unit_ru: str, unit_en: str) -> str:
        value = _economy_int(economy, key)
        if value is None:
            return f"{_DASH} · {UNAVAILABLE}"
        return f"{value} {_label(language, unit_ru, unit_en)} · {MEASURED}"

    lines = [
        _label(
            language,
            f"Экономия: {'включена' if economy_mode else 'выключена'} — модель, Effort и проверки не меняются.",
            f"Economy: {'ON' if economy_mode else 'OFF'} — model, Effort, and verification never change.",
        ),
        row("Context Compiler", count_detail("economy_context_chars_out", "символов после сборки", "chars compiled")),
        row(
            _label(language, "Стабильный префикс", "Stable Prefix"),
            count_detail("economy_prefix_stable_steps", "стабильных шагов", "stable steps"),
        ),
        row(
            _label(language, "Отложенные инструменты", "Deferred Tools"),
            count_detail("economy_tool_schema_bytes_avoided", "байт схем не отправлено", "schema bytes avoided"),
        ),
        row(
            "Evidence Packets",
            count_detail("economy_evidence_bytes_avoided", "байт заменено ссылками", "bytes avoided"),
        ),
        row("ToolVM", count_detail("economy_batched_turns_avoided", "ходов модели сэкономлено", "model turns avoided")),
        row(
            _label(language, "Непрерывность рассуждений", "Continuity"),
            count_detail("economy_continuity_statements", "выводов перенесено", "statements carried"),
        ),
    ]
    verdict = economy.get("economy_cache_verdict")
    capability = str(economy.get("economy_cache_capability") or "UNKNOWN")
    cache_row_name = _label(language, "Кэш провайдера", "Provider Cache")
    if isinstance(verdict, str) and verdict:
        saving = economy.get("economy_cache_saving_estimated_usd")
        saving_text = (
            f" · USD {float(saving):.4f} {ESTIMATED}"
            if isinstance(saving, (int, float)) and not isinstance(saving, bool)
            else ""
        )
        lines.append(f"{cache_row_name}: {verdict} ({capability}) · {MEASURED}{saving_text}")
    else:
        lines.append(f"{cache_row_name}: {_DASH} · {UNAVAILABLE}")
    tier = economy.get("economy_context_tier")
    guard_name = _label(language, "Стоимостной страж", "Cost Guard")
    if isinstance(tier, str) and tier:
        lines.append(f"{guard_name}: {tier} · {MEASURED}")
    else:
        lines.append(f"{guard_name}: {_DASH} · {UNAVAILABLE}")
    lines.append(
        _label(
            language,
            "Страж качества: экономия не понижает модель, Effort и проверки — контракт.",
            "Quality Guard: economy never downgrades model, Effort, or verification — contract.",
        )
    )
    return lines


__all__ = [
    "ESTIMATED",
    "MEASURED",
    "UNAVAILABLE",
    "build_usage_report",
    "economy_measurement_lines",
    "economy_status_lines",
    "last_economy_event",
]
