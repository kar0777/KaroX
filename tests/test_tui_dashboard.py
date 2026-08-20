from __future__ import annotations

from karox.tui_dashboard import _money, _summary_lines, _tokens
from karox.usage_analytics import UsageSummary


def test_dashboard_formatters_are_compact_and_human_readable() -> None:
    assert _tokens(999) == "999"
    assert _tokens(12_300) == "12.3K"
    assert _tokens(2_500_000) == "2.50M"
    assert _money({"USD": 0.125}) == "USD 0.1250"


def test_usage_lines_report_measured_cache_effect_and_retries() -> None:
    summary = UsageSummary(
        requests=3,
        prompt_tokens=1_000,
        completion_tokens=200,
        total_tokens=1_200,
        cache_read_tokens=750,
        transport_retries=1,
        route_retries=2,
        costs={"USD": 0.12},
        uncached_costs={"USD": 0.30},
        cache_savings={"USD": 0.18},
    )
    text = "\n".join(_summary_lines(summary, "ru"))
    assert "USD 0.1200" in text
    assert "75.0%" in text
    assert "USD 0.1800" in text
    assert "Повторы: 3" in text


def test_zero_usage_is_not_rendered_as_zero_cost() -> None:
    assert _summary_lines(UsageSummary(), "ru") == ["Пока нет измеренных запросов."]


def test_measured_usage_without_pricing_explains_the_missing_cost() -> None:
    text = "\n".join(_summary_lines(UsageSummary(requests=1, total_tokens=100), "ru"))
    assert "нет цены" in text
    assert "Стоимость: —" not in text
