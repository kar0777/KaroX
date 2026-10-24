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
        "F1 справка · F2 модели · F3 сессии · F4 MCP · /provider подключить модель"
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
    "mcp.added": "MCP-сервер {server} сохранён.",
    "mcp.removed": "MCP-сервер {server} удалён.",
    "mcp.select_confirm": "Разрешить все инструменты сервера {server}?",
    "mcp.select_confirm_body": (
        "Инструменты MCP выполняются от вашего имени. Разрешайте только серверы, "
        "которым доверяете. Можно выбрать позже через karox mcp session select."
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
    "provider.saved": "Провайдер сохранён, модель {model} выбрана.",
    "provider.failed": "Не удалось сохранить: {error}",
    "help.title": "Команды и клавиши",
    "help.body": (
        "/provider        подключить провайдера и модель\n"
        "/model           выбрать модель\n"
        "/sessions        список сессий и продолжение\n"
        "/new             начать новую сессию\n"
        "/mcp             MCP-серверы и инструменты\n"
        "/verify <cmd>    добавить команду проверки\n"
        "/unverify        сбросить проверки\n"
        "/effort <level>  уровень усилий\n"
        "/mode <mode>     build | plan | ideate\n"
        "/lang ru|en      язык интерфейса\n"
        "/status          состояние сессии\n"
        "/clear           очистить экран\n"
        "/stop            остановить агента\n"
        "/quit            выйти\n\n"
        "F1 справка · F2 модели · F3 сессии · F4 MCP\n"
        "Enter отправить · Ctrl+J новая строка · Esc остановить · Ctrl+Q выход"
    ),
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
        "F1 help · F2 models · F3 sessions · F4 MCP · /provider connect a model"
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
    "mcp.added": "MCP server {server} saved.",
    "mcp.removed": "MCP server {server} removed.",
    "mcp.select_confirm": "Allow every tool of {server}?",
    "mcp.select_confirm_body": (
        "MCP tools run with your authority. Allow only servers you trust. "
        "You can choose later with karox mcp session select."
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
    "provider.saved": "Provider saved, model {model} selected.",
    "provider.failed": "Could not save: {error}",
    "help.title": "Commands and keys",
    "help.body": (
        "/provider        connect a provider and model\n"
        "/model           choose a model\n"
        "/sessions        list and continue sessions\n"
        "/new             start a new session\n"
        "/mcp             MCP servers and tools\n"
        "/verify <cmd>    add a verification command\n"
        "/unverify        clear verification commands\n"
        "/effort <level>  effort level\n"
        "/mode <mode>     build | plan | ideate\n"
        "/lang ru|en      interface language\n"
        "/status          session state\n"
        "/clear           clear the screen\n"
        "/stop            stop the agent\n"
        "/quit            exit\n\n"
        "F1 help · F2 models · F3 sessions · F4 MCP\n"
        "Enter send · Ctrl+J newline · Esc stop · Ctrl+Q quit"
    ),
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