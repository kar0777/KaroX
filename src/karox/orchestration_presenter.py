"""Human-first presentation helpers for KaroX orchestration.

The runtime remains fully structured and machine-readable. This module is only
the calm human surface: raw DAG metadata and endpoint IDs stay available in
JSON/details instead of becoming the first thing a person has to understand.
"""

from __future__ import annotations

from typing import Any, Mapping

_PRESET_LABELS = {
    "en": {
        "maximum_quality": "Maximum quality — strongest models on difficult stages",
        "balanced": "Balanced — strong results without unnecessary expensive calls",
        "maximum_economy": "Maximum economy — already-paid and local models first",
        "custom": "Custom — your choices",
    },
    "ru": {
        "maximum_quality": "Максимум качества — сильнейшие модели на сложных этапах",
        "balanced": "Баланс — сильный результат без лишних дорогих запросов",
        "maximum_economy": "Максимум экономии — сначала уже оплаченные и локальные модели",
        "custom": "Свой режим — ваши настройки",
    },
}
_ROLE_LABELS = {
    "en": {"orchestrator": "Orchestrator", "planner": "Planner", "scout": "Project scout", "implementer": "Implementer", "tester": "Tester", "reviewer": "Independent reviewer", "security": "Security reviewer", "summarizer": "Summarizer", "ui": "UI verifier"},
    "ru": {"orchestrator": "Оркестратор", "planner": "Планировщик", "scout": "Разведка проекта", "implementer": "Исполнитель", "tester": "Тестировщик", "reviewer": "Независимая проверка", "security": "Проверка безопасности", "summarizer": "Сводка", "ui": "Проверка интерфейса"},
}
_STATUS_LABELS = {
    "en": {"passed": "passed", "failed": "failed", "running": "running", "pending": "waiting", "blocked": "blocked", "skipped": "skipped", "stopped": "stopped"},
    "ru": {"passed": "готово", "failed": "ошибка", "running": "работает", "pending": "ожидает", "blocked": "заблокировано", "skipped": "пропущено", "stopped": "остановлено"},
}
_SOURCE_LABELS = {
    "en": {"api": "API", "subscription": "subscription", "local": "local", "external": "external app"},
    "ru": {"api": "API", "subscription": "подписка", "local": "локально", "external": "внешнее приложение"},
}
_SOURCE_PRIORITY = {"subscription": 0, "local": 1, "external": 2, "api": 3}


def preset_label(value: object, language: str = "en") -> str:
    key = str(value or "balanced")
    return _PRESET_LABELS["ru" if language == "ru" else "en"].get(
        key, key.replace("_", " ")
    )


def role_label(value: object, language: str = "en") -> str:
    key = str(value or "worker")
    return _ROLE_LABELS["ru" if language == "ru" else "en"].get(
        key, key.replace("_", " ").replace("-", " ").title()
    )


def status_label(value: object, language: str = "en") -> str:
    key = str(value or "unknown")
    return _STATUS_LABELS["ru" if language == "ru" else "en"].get(
        key, key.replace("_", " ")
    )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _endpoint_name(value: object) -> str:
    endpoint = _mapping(value)
    return str(endpoint.get("display_name") or endpoint.get("endpoint_id") or "—")


def render_intelligence_list(
    endpoints: list[Mapping[str, Any]],
    language: str = "en",
    *,
    details: bool = False,
    limit: int = 24,
) -> str:
    """Render a bounded human overview of the unified intelligence pool."""

    russian = language == "ru"
    rows = sorted(
        endpoints,
        key=lambda item: (
            _SOURCE_PRIORITY.get(str(item.get("source_kind") or ""), 9),
            str(item.get("display_name") or item.get("endpoint_id") or "").casefold(),
        ),
    )
    shown = rows if details else rows[: max(1, limit)]
    lines = ["Доступный AI" if russian else "Available AI"]
    if not shown:
        lines.append(
            "  Пока ничего не подключено. Откройте karox и используйте /connect."
            if russian
            else "  Nothing connected yet. Open karox and use /connect."
        )
        return "\n".join(lines)

    labels = _SOURCE_LABELS["ru" if russian else "en"]
    for item in shown:
        source = str(item.get("source_kind") or "")
        name = str(item.get("display_name") or item.get("endpoint_id") or "—")
        badges = [labels.get(source, source or "AI")]
        if not bool(item.get("enabled", True)):
            badges.append("отключено" if russian else "disabled")
        elif bool(item.get("already_paid", False)):
            badges.append("уже оплачено" if russian else "already paid")
        quota = _mapping(item.get("quota"))
        fraction = quota.get("remaining_fraction")
        if isinstance(fraction, (int, float)) and not isinstance(fraction, bool):
            percent = max(0.0, min(100.0, float(fraction) * 100.0))
            badges.append(
                f"лимит {percent:.0f}%" if russian else f"quota {percent:.0f}% left"
            )
        line = f"  {name} · " + " · ".join(badges)
        if details:
            endpoint_id = str(item.get("endpoint_id") or "")
            if endpoint_id:
                line += f"\n    id: {endpoint_id}"
            roles = [str(value) for value in (item.get("roles") or ())]
            capabilities = [str(value) for value in (item.get("capabilities") or ())]
            if roles:
                line += "\n    roles: " + ", ".join(roles)
            if capabilities:
                line += "\n    capabilities: " + ", ".join(capabilities)
        lines.append(line)

    hidden = len(rows) - len(shown)
    if hidden > 0:
        lines.append(
            (
                f"  … ещё {hidden}. Полный каталог моделей: karox models; машинные данные: karox agents --json"
                if russian
                else f"  … {hidden} more. Full model catalog: karox models; machine data: karox agents --json"
            )
        )
    return "\n".join(lines)


def render_plan(
    plan: Mapping[str, Any], language: str = "en", *, details: bool = False
) -> str:
    """Render a plan without making the user read the raw DAG."""

    russian = language == "ru"
    policy = _mapping(plan.get("policy"))
    orchestrator = _endpoint_name(plan.get("orchestrator_endpoint"))
    lines = [
        "План KaroX" if russian else "KaroX plan",
        f"Оркестратор: {orchestrator}" if russian else f"Orchestrator: {orchestrator}",
        (
            f"Режим: {preset_label(policy.get('preset'), language)}"
            if russian
            else f"Mode: {preset_label(policy.get('preset'), language)}"
        ),
        "Команда:" if russian else "Team:",
    ]
    for row in plan.get("steps", []):
        if not isinstance(row, Mapping):
            continue
        step = _mapping(row.get("step"))
        endpoint = _mapping(row.get("endpoint"))
        role = role_label(step.get("role") or step.get("step_id"), language)
        line = f"  {role} → {_endpoint_name(endpoint)}"
        if details:
            line += f" · effort {str(row.get('effort_level') or 'medium')}"
            step_id = str(step.get("step_id") or "")
            if step_id:
                line += f" · {step_id}"
        lines.append(line)
    if details and plan.get("run_id"):
        lines.append(f"Run ID: {plan['run_id']}")
    return "\n".join(lines)


def render_run(
    result: Mapping[str, Any], language: str = "en", *, details: bool = False
) -> str:
    """Render a result around outcome and money rather than raw telemetry."""

    russian = language == "ru"
    plan = _mapping(result.get("plan"))
    role_by_step: dict[str, str] = {}
    endpoint_name_by_id: dict[str, str] = {}
    for row in plan.get("steps", []):
        if not isinstance(row, Mapping):
            continue
        step = _mapping(row.get("step"))
        endpoint = _mapping(row.get("endpoint"))
        step_id = str(step.get("step_id") or "")
        endpoint_id = str(endpoint.get("endpoint_id") or "")
        if step_id:
            role_by_step[step_id] = role_label(step.get("role") or step_id, language)
        if endpoint_id:
            endpoint_name_by_id[endpoint_id] = _endpoint_name(endpoint)

    lines = [
        f"KaroX: {status_label(result.get('status'), language)}",
        "Команда:" if russian else "Team:",
    ]
    for row in result.get("steps", []):
        if not isinstance(row, Mapping):
            continue
        step_id = str(row.get("step_id") or "worker")
        endpoint_id = str(row.get("endpoint_id") or "")
        role = role_by_step.get(step_id, role_label(step_id, language))
        endpoint_name = endpoint_name_by_id.get(endpoint_id, endpoint_id or "—")
        lines.append(
            f"  {status_label(row.get('status'), language)} · {role} → {endpoint_name}"
        )

    cost = float(result.get("total_cost_usd") or 0.0)
    receipt = result.get("savings_receipt")
    actual_metric = _mapping(receipt.get("actual_cost")) if isinstance(receipt, Mapping) else {}
    cost_evidence = str(actual_metric.get("evidence") or "")
    if cost_evidence == "estimated":
        lines.append(
            f"Оценка стоимости AI: ${cost:.4f}"
            if russian
            else f"AI cost estimate: ${cost:.4f}"
        )
    elif cost_evidence == "measured":
        lines.append(
            f"Измеренная стоимость AI: ${cost:.4f}"
            if russian
            else f"Measured AI cost: ${cost:.4f}"
        )
    else:
        lines.append(
            f"Дополнительная стоимость: ${cost:.4f}"
            if russian
            else f"Incremental cost: ${cost:.4f}"
        )
    if isinstance(receipt, Mapping) and receipt.get("savings_percent") is not None:
        saving = receipt["savings_percent"]
        savings_metric = _mapping(receipt.get("savings"))
        savings_evidence = str(savings_metric.get("evidence") or "")
        if savings_evidence == "measured":
            label = "Измеренная экономия" if russian else "Measured saving"
        elif savings_evidence == "estimated":
            label = "Оценка экономии" if russian else "Estimated saving"
        else:
            label = "Экономия" if russian else "Saving"
        lines.append(f"{label}: {saving}%")
    if details:
        lines.append(f"Tokens: {int(result.get('total_tokens') or 0)}")
        if plan.get("run_id"):
            lines.append(f"Run ID: {plan['run_id']}")
    return "\n".join(lines)


def render_started(
    result: Mapping[str, Any],
    *,
    objective: str = "",
    language: str = "en",
    details: bool = False,
) -> str:
    """Render detached launch state without leading with process internals."""

    russian = language == "ru"
    lines = ["KaroX запущен" if russian else "KaroX started"]
    if objective.strip():
        lines.append(objective.strip())
    mission_command = str(result.get("mission_command") or "").strip()
    if mission_command.startswith("karox mission-control show "):
        mission_command = "karox mission " + mission_command.removeprefix(
            "karox mission-control show "
        )
    if mission_command:
        lines.append(
            f"Следить: {mission_command}" if russian else f"Track: {mission_command}"
        )
    if details:
        if result.get("run_id"):
            lines.append(f"Run ID: {result['run_id']}")
        if result.get("pid") is not None:
            lines.append(f"PID: {result['pid']}")
        if result.get("launch_mechanism"):
            lines.append(f"Launch: {result['launch_mechanism']}")
        if result.get("identity_provable") is not None:
            lines.append(
                "Process identity: "
                + ("verified" if bool(result.get("identity_provable")) else "unverified")
            )
    return "\n".join(lines)


def render_mission(
    snapshot: Mapping[str, Any],
    *,
    endpoint_names: Mapping[str, str] | None = None,
    language: str = "en",
    details: bool = False,
) -> str:
    """Render Mission Control around task progress; raw runtime IDs stay in details."""

    russian = language == "ru"
    names = endpoint_names or {}

    def display_endpoint(endpoint_id: object) -> str:
        key = str(endpoint_id or "")
        if key in names and names[key].strip():
            return names[key].strip()
        return key if details and key else ("подключённый AI" if russian else "configured AI")

    objective = str(snapshot.get("objective") or "").strip()
    progress = float(snapshot.get("progress_percent") or 0.0)
    lines = ["KaroX Mission Control"]
    if objective:
        lines.append(objective)
    lines.append(
        (
            f"Статус: {status_label(snapshot.get('status'), language)} · Прогресс {progress:.0f}%"
            if russian
            else f"Status: {status_label(snapshot.get('status'), language)} · Progress {progress:.0f}%"
        )
    )
    orchestrator_id = str(snapshot.get("orchestrator_endpoint_id") or "")
    if orchestrator_id:
        lines.append(
            ("Оркестратор: " if russian else "Orchestrator: ")
            + display_endpoint(orchestrator_id)
        )

    agents = [item for item in snapshot.get("agents", []) if isinstance(item, Mapping)]
    lines.append("Воркеры:" if russian else "Workers:")
    if not agents:
        lines.append("  пока нет" if russian else "  none yet")
    for item in agents:
        role = role_label(item.get("role") or item.get("step_id"), language)
        endpoint = display_endpoint(item.get("endpoint_id"))
        state = status_label(item.get("status"), language)
        line = f"  {state} · {role} → {endpoint}"
        activity = str(item.get("activity") or "").strip()
        if activity:
            line += f" · {activity}"
        lines.append(line)
        if details:
            endpoint_id = str(item.get("endpoint_id") or "")
            if endpoint_id:
                lines.append(f"    endpoint: {endpoint_id}")
            if item.get("evidence_count") is not None:
                lines.append(f"    evidence: {int(item.get('evidence_count') or 0)}")

    lines.append(
        (
            f"Стоимость: ${float(snapshot.get('actual_cost_usd') or 0.0):.4f}"
            if russian
            else f"Incremental cost: ${float(snapshot.get('actual_cost_usd') or 0.0):.4f}"
        )
    )
    cache = snapshot.get("cache_hit_rate")
    if isinstance(cache, (int, float)) and not isinstance(cache, bool):
        percent = max(0.0, min(100.0, float(cache) * 100.0))
        lines.append(f"Кэш: {percent:.0f}%" if russian else f"Cache: {percent:.0f}%")
    if details:
        if snapshot.get("run_id"):
            lines.append(f"Run ID: {snapshot['run_id']}")
        if snapshot.get("task_id"):
            lines.append(f"Task ID: {snapshot['task_id']}")
        if snapshot.get("recipe"):
            lines.append(f"Recipe: {snapshot['recipe']}")
        lines.append(f"Tokens: {int(snapshot.get('total_tokens') or 0)}")
        lines.append(
            f"Context reused: {int(snapshot.get('context_reused_chars') or 0)} chars"
        )
        if orchestrator_id:
            lines.append(f"Orchestrator endpoint: {orchestrator_id}")
    return "\n".join(lines)


__all__ = ["preset_label", "render_intelligence_list", "render_mission", "render_plan", "render_run", "render_started", "role_label", "status_label"]
