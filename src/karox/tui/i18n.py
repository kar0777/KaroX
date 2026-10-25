"""One message catalogue, two languages.

The classic shell chose between English and Russian at every call site, which
doubled the size of every screen and let the two languages drift. Here the
choice is made once per render and a missing key falls back to English, then to
the key itself, so a typo is visible and never crashes a screen.
"""

from __future__ import annotations

import locale
import os
from typing import Any

_RU: dict[str, str] = {
    "app.title": "KaroX",
    "app.tagline": "локальный агент",
    "welcome.title": "Готов к работе",
    "welcome.body": (
        "Опишите задачу обычным текстом и нажмите Enter.\n"
        "Агент читает репозиторий, правит файлы и проверяет результат.\n\n"
        "F1 справка · F2 модели · F3 сессии · F4 MCP · F5 состояние сессии\n"
        "/provider подключить модель · /skills навыки репозитория"
    ),
    "composer.placeholder": "Опишите задачу…  (/help — команды)",
    "status.idle": "готов",
    "status.running": "работаю",
    "status.stopping": "останавливаю…",
    "status.hint": "Enter отправить · Ctrl+J новая строка · /help · Esc стоп",
    "status.tokens": "{count} ток.",
    "status.session": "сессия {id}",
    "status.no_model": "модель не выбрана",
    "notice.no_model": (
        "Модель не настроена. Откройте /provider, чтобы добавить провайдера, "
        "или выполните karox provider setup."
    ),
    "notice.already_running": "Агент уже работает. Esc — остановить.",
    "notice.stopped": "Остановлено. Сессию можно продолжить позже.",
    "notice.backend_error": "Ошибка запуска: {error}",
    "notice.no_repository": "Каталог не существует: {path}",
    "notice.session_repo": "Эта сессия привязана к другому репозиторию: {repository}",
    "notice.clear": "Диалог очищен.",
    "notice.new_session": "Новая сессия {id}.",
    "notice.session_selected": "Сессия {id} выбрана. Следующая задача продолжится в ней.",
    "notice.no_sessions": "Сохранённых сессий для этого репозитория нет.",
    "notice.models_empty": "Модели не настроены. Добавьте провайдера через /provider.",
    "notice.mcp_empty": "MCP-серверы не настроены. Нажмите a, чтобы добавить.",
    "notice.mcp_selected": "MCP-сервер {server} выбран; разрешено инструментов: {count}.",
    "notice.mcp_deselected": "MCP-сервер {server} отключён от этой сессии.",
    "notice.mcp_probe": "Проверяю {server}…",
    "notice.mcp_probe_ok": "{server}: доступен, инструментов {count}.",
    "notice.mcp_probe_failed": "{server}: ошибка — {error}",
    "notice.verify_added": "Проверка добавлена: {command}",
    "notice.verify_cleared": "Список проверок сброшен.",
    "notice.effort": "Effort: {level}",
    "notice.mode": "Режим: {mode}",
    "notice.language": "Язык: {language}",
    "result.verified": "готово · проверено",
    "result.answer": "готово · ответ",
    "result.failed": "не завершено",
    "result.steps": "{count} шаг(ов)",
    "result.files": "файлов: {count}",
    "result.checks": "проверки: {passed}/{total}",
    "result.tokens": "токенов: {count}",
    "result.duration": "{seconds:.1f} с",
    "result.reason": "причина: {reason}",
    "result.changed": "изменено: {files}",
    "tool.running": "выполняется",
    "tool.ok": "готово",
    "tool.failed": "ошибка",
    "reasoning": "размышление · {count} симв.",
    "reasoning.title": "Размышление модели",
    "mcp.title": "MCP-серверы",
    "mcp.hint": "a добавить · d удалить · p проверить · s выбрать для сессии · Esc закрыть",
    "mcp.add_title": "Новый MCP-сервер",
    "mcp.server_id": "ID сервера",
    "mcp.namespace": "Пространство имён",
    "mcp.transport": "Транспорт",
    "mcp.command": "Команда (stdio)",
    "mcp.url": "URL (streamable_http)",
    "mcp.args": "Аргументы через пробел",
    "mcp.env": "Переменные окружения (NAME=VALUE; …)",
    "mcp.headers": "HTTP-заголовки (NAME=VALUE; …)",
    "mcp.credential_ref": "Ссылка на секрет (os-keyring:mcp/name)",
    "mcp.credential_target": "Куда подставлять секрет (header/env)",
    "mcp.oauth": "OAuth для streamable_http (да/нет)",
    "mcp.read_only": "Read-only инструменты, через запятую",
    "mcp.timeout": "Таймаут, сек",
    "mcp.added": "MCP-сервер {server} сохранён.",
    "mcp.removed": "MCP-сервер {server} удалён.",
    "mcp.select_confirm": "Выбрать инструменты сервера {server}?",
    "mcp.select_confirm_body": (
        "Read-only инструменты будут разрешены, изменяющие — помечены ask и "
        "потребуют отдельного решения. Точный выбор: karox mcp session select."
    ),
    "models.title": "Модели",
    "models.hint": "Enter выбрать · Esc закрыть",
    "models.selected": "Модель: {model}",
    "sessions.title": "Сессии этого репозитория",
    "sessions.hint": "Enter продолжить · n новая · Esc закрыть",
    "sessions.created": "обновлена {when}",
    "provider.title": "Подключение провайдера",
    "provider.preset": "Провайдер",
    "provider.id": "ID провайдера",
    "provider.base_url": "Base URL",
    "provider.model": "ID модели",
    "provider.key": "API-ключ (хранится в системном хранилище)",
    "provider.save": "Проверить и сохранить",
    "provider.cancel": "Отмена",
    "provider.required": "Заполните обязательные поля.",
    "provider.key_required": "Для удалённого провайдера нужен API-ключ.",
    "provider.saved": "Провайдер сохранён, модель {model} выбрана.",
    "provider.failed": "Не удалось сохранить: {error}",
    "help.title": "Команды и клавиши",
    "help.body": (
        "/provider        подключить провайдера и модель\n"
        "/model           выбрать модель\n"
        "/sessions        список сессий и продолжение\n"
        "/new             начать новую сессию\n"
        "/mcp             MCP-серверы и инструменты\n"
        "/skills          навыки репозитория\n"
        "/session         состояние сохранённой сессии\n"
        "/verify <cmd>    добавить команду проверки\n"
        "/unverify        сбросить проверки\n"
        "/effort <level>  уровень усилий\n"
        "/mode <mode>     build | plan | ideate\n"
        "/lang ru|en      язык интерфейса\n"
        "/status          состояние сессии\n"
        "/doctor          проверить провайдера, ключи, MCP и сессии\n"
        "/bridge          сохранённые мосты ChatGPT/Claude/HyperAgent\n"
        "/clear           очистить экран\n"
        "/stop            остановить агента\n"
        "/quit            выйти\n\n"
        "F1 справка · F2 модели · F3 сессии · F4 MCP · F5 состояние\n"
        "Enter отправить · Ctrl+J новая строка · Esc остановить · Ctrl+Q выход"
    ),
    "activity.step": "шаг {n} · жду модель",
    "activity.running": "{tool} · выполняется",
    "activity.ok": "{tool} · готово",
    "activity.failed": "{tool} · ошибка",
    "activity.phase": "фаза · {phase}",
    "activity.compacted": "контекст сжат",
    "activity.edited": "правка · {path}",
    "activity.read": "чтение · {path}",
    "activity.test_started": "проверка · {tool} запущена",
    "activity.test_ok": "проверка · {tool} пройдена",
    "activity.test_failed": "проверка · {tool} провалена",
    "activity.artifact": "артефакт · {name}",
    "notice.compacted": "Контрольная точка контекста: {summary}",
    "status.saved": "сохранено {when}",
    "status.step": "шаг {n}",
    "status.seconds": "{seconds:.0f} с",
    "status.checks_live": "✓{ok}/✗{bad}",
    "status.failed": "ошибка",
    "status.stopped": "остановлено",
    "status.checkpoint": "контрольная точка: {when}",
    "status.no_checkpoint": "сессия ещё не сохранена",
    "status.run_counters": (
        "прошлый запуск: чтения {reads} · правки {edits} · проверки ✓{ok}/✗{bad} · "
        "mcp {mcp} · навыки {skills} · суб-агенты {sub}"
    ),
    "session.title": "Состояние сессии",
    "session.empty": (
        "Сохранённой сессии пока нет. Выполните одну задачу — KaroX сохраняет "
        "возобновляемую контрольную точку автоматически."
    ),
    "session.section.meta": "сессия",
    "session.section.task": "задача",
    "session.section.plan": "план",
    "session.section.checkpoints": "контрольные точки",
    "session.section.verification": "проверки",
    "session.section.provider": "провайдер",
    "session.section.mcp": "MCP-серверы",
    "session.section.skills": "навыки",
    "session.section.subagents": "суб-агенты",
    "session.section.files": "изменённые файлы",
    "session.none": "нет",
    "session.history": "+{count} предыдущих хода",
    "session.plan_tally": "к выполнению {todo} · выполняется {doing} · готово {done}",
    "session.checkpoints_line": "{count} сохранено · последняя {when}",
    "session.checkpoint_files": "в точке: {tracked} отслеживаемых, {untracked} новых файлов",
    "session.checks_line": "{passed}/{total} пройдено",
    "session.verified_at": "проверено на шаге {step}",
    "session.provider_line": "{requests} запросов · {tokens} токенов",
    "session.cost": "стоимость {cost}",
    "session.subagents_line": "заданий: {count}",
    "session.files_line": "изменено файлов: {count}",
    "skills.title": "Навыки репозитория",
    "skills.hint": "Enter открыть · Esc закрыть",
    "notice.skills_empty": "Навыки для этого репозитория не найдены.",
    "notice.skill_failed": "Не удалось прочитать навык {name}.",
    "skill.detail_title": "Навык {name}",
    "skill.version": "версия",
    "skill.when_to_use": "Когда использовать",
    "skill.permissions": "Права",
    "skill.files": "Файлы",
    "tool.detail_title": "Вызов инструмента",
    "welcome.facts.session": "сессия {id}",
    "welcome.facts.verification": "проверки: {commands}",
    "language.ru": "ru",
    "language.en": "en",
}

_EN: dict[str, str] = {
    "app.title": "KaroX",
    "app.tagline": "local agent",
    "welcome.title": "Ready",
    "welcome.body": (
        "Describe a task in plain text and press Enter.\n"
        "The agent reads the repository, edits files and verifies the result.\n\n"
        "F1 help · F2 models · F3 sessions · F4 MCP · F5 session state\n"
        "/provider connect a model · /skills repository skills"
    ),
    "composer.placeholder": "Describe a task…  (/help for commands)",
    "status.idle": "idle",
    "status.running": "working",
    "status.stopping": "stopping…",
    "status.hint": "Enter send · Ctrl+J newline · /help · Esc stop",
    "status.tokens": "{count} tok",
    "status.session": "session {id}",
    "status.no_model": "no model selected",
    "notice.no_model": (
        "No model is configured. Open /provider to add one, "
        "or run karox provider setup."
    ),
    "notice.already_running": "The agent is already running. Press Esc to stop it.",
    "notice.stopped": "Stopped. The session can be resumed later.",
    "notice.backend_error": "Run failed: {error}",
    "notice.no_repository": "Directory does not exist: {path}",
    "notice.session_repo": "That session belongs to another repository: {repository}",
    "notice.clear": "Conversation cleared.",
    "notice.new_session": "New session {id}.",
    "notice.session_selected": "Session {id} selected. The next task continues in it.",
    "notice.no_sessions": "No saved sessions for this repository.",
    "notice.models_empty": "No models configured. Add a provider through /provider.",
    "notice.mcp_empty": "No MCP servers configured. Press a to add one.",
    "notice.mcp_selected": "MCP server {server} selected; tools allowed: {count}.",
    "notice.mcp_deselected": "MCP server {server} detached from this session.",
    "notice.mcp_probe": "Probing {server}…",
    "notice.mcp_probe_ok": "{server}: reachable, {count} tools.",
    "notice.mcp_probe_failed": "{server}: failed — {error}",
    "notice.verify_added": "Verification added: {command}",
    "notice.verify_cleared": "Verification list cleared.",
    "notice.effort": "Effort: {level}",
    "notice.mode": "Mode: {mode}",
    "notice.language": "Language: {language}",
    "result.verified": "done · verified",
    "result.answer": "done · answer",
    "result.failed": "not completed",
    "result.steps": "{count} step(s)",
    "result.files": "files: {count}",
    "result.checks": "checks: {passed}/{total}",
    "result.tokens": "tokens: {count}",
    "result.duration": "{seconds:.1f}s",
    "result.reason": "reason: {reason}",
    "result.changed": "changed: {files}",
    "tool.running": "running",
    "tool.ok": "ok",
    "tool.failed": "failed",
    "reasoning": "reasoning · {count} chars",
    "reasoning.title": "Model reasoning",
    "mcp.title": "MCP servers",
    "mcp.hint": "a add · d remove · p probe · s select for session · Esc close",
    "mcp.add_title": "New MCP server",
    "mcp.server_id": "Server ID",
    "mcp.namespace": "Namespace",
    "mcp.transport": "Transport",
    "mcp.command": "Command (stdio)",
    "mcp.url": "URL (streamable_http)",
    "mcp.args": "Arguments, space separated",
    "mcp.env": "Environment (NAME=VALUE; …)",
    "mcp.headers": "HTTP headers (NAME=VALUE; …)",
    "mcp.credential_ref": "Secret reference (os-keyring:mcp/name)",
    "mcp.credential_target": "Injection target (header/env)",
    "mcp.oauth": "OAuth for streamable_http (yes/no)",
    "mcp.read_only": "Read-only tools, comma separated",
    "mcp.timeout": "Timeout, seconds",
    "mcp.added": "MCP server {server} saved.",
    "mcp.removed": "MCP server {server} removed.",
    "mcp.select_confirm": "Select tools of {server}?",
    "mcp.select_confirm_body": (
        "Read-only tools are allowed; mutating tools are marked ask and need a "
        "separate decision. Exact choices: karox mcp session select."
    ),
    "models.title": "Models",
    "models.hint": "Enter select · Esc close",
    "models.selected": "Model: {model}",
    "sessions.title": "Sessions in this repository",
    "sessions.hint": "Enter continue · n new · Esc close",
    "sessions.created": "updated {when}",
    "provider.title": "Connect a provider",
    "provider.preset": "Provider",
    "provider.id": "Provider ID",
    "provider.base_url": "Base URL",
    "provider.model": "Model ID",
    "provider.key": "API key (stored in the OS keyring)",
    "provider.save": "Verify and save",
    "provider.cancel": "Cancel",
    "provider.required": "Fill the required fields.",
    "provider.key_required": "A remote provider needs an API key.",
    "provider.saved": "Provider saved, model {model} selected.",
    "provider.failed": "Could not save: {error}",
    "help.title": "Commands and keys",
    "help.body": (
        "/provider        connect a provider and model\n"
        "/model           choose a model\n"
        "/sessions        list and continue sessions\n"
        "/new             start a new session\n"
        "/mcp             MCP servers and tools\n"
        "/skills          repository skills\n"
        "/session         durable session state\n"
        "/verify <cmd>    add a verification command\n"
        "/unverify        clear verification commands\n"
        "/effort <level>  effort level\n"
        "/mode <mode>     build | plan | ideate\n"
        "/lang ru|en      interface language\n"
        "/status          session state\n"
        "/doctor          check provider, keys, MCP and sessions\n"
        "/bridge          saved ChatGPT/Claude/HyperAgent bridges\n"
        "/clear           clear the screen\n"
        "/stop            stop the agent\n"
        "/quit            exit\n\n"
        "F1 help · F2 models · F3 sessions · F4 MCP · F5 session state\n"
        "Enter send · Ctrl+J newline · Esc stop · Ctrl+Q quit"
    ),
    "activity.step": "step {n} · waiting for the model",
    "activity.running": "{tool} · running",
    "activity.ok": "{tool} · ok",
    "activity.failed": "{tool} · failed",
    "activity.phase": "phase · {phase}",
    "activity.compacted": "context compacted",
    "activity.edited": "edited · {path}",
    "activity.read": "read · {path}",
    "activity.test_started": "test · {tool} started",
    "activity.test_ok": "test · {tool} ok",
    "activity.test_failed": "test · {tool} failed",
    "activity.artifact": "artifact · {name}",
    "notice.compacted": "Context checkpoint: {summary}",
    "status.saved": "saved {when}",
    "status.step": "step {n}",
    "status.seconds": "{seconds:.0f}s",
    "status.checks_live": "✓{ok}/✗{bad}",
    "status.failed": "failed",
    "status.stopped": "stopped",
    "status.checkpoint": "session checkpoint: {when}",
    "status.no_checkpoint": "no session checkpoint yet",
    "status.run_counters": (
        "last run: reads {reads} · edits {edits} · checks ✓{ok}/✗{bad} · "
        "mcp {mcp} · skills {skills} · subagents {sub}"
    ),
    "session.title": "Session state",
    "session.empty": (
        "No durable session record yet. Run one task; KaroX saves a resumable "
        "checkpoint automatically."
    ),
    "session.section.meta": "session",
    "session.section.task": "task",
    "session.section.plan": "plan",
    "session.section.checkpoints": "checkpoints",
    "session.section.verification": "verification",
    "session.section.provider": "provider",
    "session.section.mcp": "MCP servers",
    "session.section.skills": "skills",
    "session.section.subagents": "subagents",
    "session.section.files": "changed files",
    "session.none": "none",
    "session.history": "+{count} earlier turn(s)",
    "session.plan_tally": "todo {todo} · doing {doing} · done {done}",
    "session.checkpoints_line": "{count} saved · last {when}",
    "session.checkpoint_files": "in checkpoint: {tracked} tracked, {untracked} untracked files",
    "session.checks_line": "{passed}/{total} passed",
    "session.verified_at": "verified at step {step}",
    "session.provider_line": "{requests} requests · {tokens} tokens",
    "session.cost": "cost {cost}",
    "session.subagents_line": "jobs: {count}",
    "session.files_line": "changed files: {count}",
    "skills.title": "Repository skills",
    "skills.hint": "Enter open · Esc close",
    "notice.skills_empty": "No skills discovered for this repository.",
    "notice.skill_failed": "Could not read skill {name}.",
    "skill.detail_title": "Skill {name}",
    "skill.version": "version",
    "skill.when_to_use": "When to use",
    "skill.permissions": "Permissions",
    "skill.files": "Files",
    "tool.detail_title": "Tool call",
    "welcome.facts.session": "session {id}",
    "welcome.facts.verification": "verification: {commands}",
    "language.ru": "ru",
    "language.en": "en",
}

_CATALOGUE: dict[str, dict[str, str]] = {"ru": _RU, "en": _EN}


def detect_language() -> str:
    """Russian only when the host actually says so; English otherwise."""

    override = os.environ.get("KAROX_LANGUAGE", "").strip().lower()
    if override in _CATALOGUE:
        return override
    try:
        name = locale.getlocale()[0] or ""
    except (ValueError, TypeError):
        name = ""
    return "ru" if name.lower().startswith("ru") else "en"


def normalise_language(value: object) -> str | None:
    return value if value in _CATALOGUE else None


def tr(lang: str, key: str, **values: Any) -> str:
    catalogue = _CATALOGUE.get(lang, _EN)
    template = catalogue.get(key) or _EN.get(key) or key
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return template