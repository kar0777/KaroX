"""One message catalogue, two languages.

The classic shell chose between English and Russian at every call site, which
doubled the size of every screen and let the two languages drift. Here the
choice is made once per render and a missing key falls back to English, then to
the key itself, so a typo is visible and never crashes a screen. Every string
the screen shows comes from here, so a Russian screen has no English leftovers.
"""

from __future__ import annotations

import locale
import os
from typing import Any

_RU: dict[str, str] = {
    "app.title": "KaroX",
    # ------------------------------------------------------------ welcome
    "welcome.title": "Добро пожаловать в KaroX",
    "welcome.body": "/help — справка · /model — модель · /connect — подключить провайдера",
    "welcome.facts.verification": "проверки: {commands}",
    "welcome.facts.suggested": "совет: /verify {commands} — проверять правки этой командой",
    # ----------------------------------------------------------- composer
    "composer.placeholder": "Опишите задачу…",
    "composer.filter": "Печатайте для поиска · ↑↓ выбрать · Enter подтвердить · Esc отмена",
    "secret.placeholder": "API-ключ {provider} ({env}) · Enter подключить · Esc отмена",
    "secret.placeholder_plain": "API-ключ {provider} · Enter подключить · Esc отмена",
    "secret.placeholder_saved": (
        "API-ключ {provider} · пустой Enter — оставить сохранённый · Esc отмена"
    ),
    "secret.variable": "{name} для {provider} · Enter далее · Esc отмена",
    "menu.palette_hint": "Tab дополнить · Enter выполнить · Esc закрыть",
    "menu.picker_hint": "↑↓ выбрать · Enter подтвердить · Esc отмена",
    "menu.empty": "ничего не найдено",
    "menu.more": "ещё {count} — уточните поиск",
    # ------------------------------------------------------------- status
    "status.loading": "загружаю…",
    "status.no_model": "модель не выбрана",
    # ----------------------------------------------------------- commands
    "cmd.help": "команды и клавиши",
    "cmd.connect": "подключить провайдера по API-ключу",
    "cmd.model": "выбрать модель",
    "cmd.sessions": "сессии этого репозитория, продолжить",
    "cmd.new": "новая сессия",
    "cmd.session": "сохранённое состояние сессии",
    "cmd.status": "модель, сессия, проверки, счётчики",
    "cmd.mcp": "MCP-серверы: проверить, разрешить, удалить, добавить",
    "cmd.skills": "навыки репозитория",
    "cmd.verify": "добавить команду проверки",
    "cmd.unverify": "сбросить проверки",
    "cmd.effort": "глубина рассуждений",
    "cmd.mode": "режим: build, plan, ideate",
    "cmd.lang": "язык интерфейса",
    "cmd.reasoning": "размышления модели за последний ответ",
    "cmd.doctor": "диагностика провайдера, ключей, MCP и сессий",
    "cmd.clear": "очистить экран",
    "cmd.stop": "остановить агента",
    "cmd.quit": "выйти",
    # -------------------------------------------------------------- help
    "help.title": "Команды",
    "help.keys_title": "Клавиши",
    "help.keys": (
        "Enter отправить · Ctrl+J новая строка · ↑↓ история · ? подсказки\n"
        "/ список команд · Tab дополнить · Esc закрыть список или прервать агента\n"
        "Shift+Tab сменить режим · Ctrl+O подробности инструментов · Ctrl+R размышления\n"
        "PageUp/PageDown прокрутка · Ctrl+N новая сессия · Ctrl+L очистить\n"
        "Ctrl+C прервать или выйти · Ctrl+Q выйти · щелчок по инструменту — подробности"
    ),
    # ----------------------------------------------------------- notices
    "notice.no_model": (
        "Модель не выбрана. /connect — подключить провайдера по API-ключу, "
        "/model — выбрать модель."
    ),
    "notice.already_running": "Агент уже работает. Esc — остановить.",
    "notice.stopped": "Остановлено. Сессию можно продолжить позже.",
    "notice.backend_error": "Ошибка запуска: {error}",
    "notice.session_repo": "Эта сессия привязана к другому репозиторию: {repository}",
    "notice.session_missing": "Сессия не найдена: {id}",
    "notice.clear": "Экран очищен.",
    "notice.new_session": "Новая сессия {id}.",
    "notice.session_selected": "Сессия {id} выбрана. Следующая задача продолжится в ней.",
    "notice.session_task": "задача: {task}",
    "notice.no_sessions": "Сохранённых сессий для этого репозитория нет.",
    "notice.models_empty": "Моделей нет. Подключите провайдера: /connect.",
    "notice.unknown_command": "Неизвестная команда /{command}. Наберите «/» — появится список.",
    "notice.verify_added": "Проверка добавлена: {command}",
    "notice.verify_cleared": "Список проверок сброшен.",
    "notice.verify_none": "Проверок нет. Добавьте: /verify <команда>.",
    "notice.verify_list": "Проверки: {commands}",
    "notice.verify_parse": "Не удалось разобрать команду: {error}",
    "notice.verify_custom": "Допишите команду после /verify — задача начнётся сразу после неё.",
    "verify.title": "Чем проверять правки? Без одобренной проверки агент не запускается",
    "verify.detected": "найдена в проекте",
    "verify.custom": "Своя команда…",
    "verify.custom_detail": "/verify <команда>",
    "verify.hint": "↑↓ выбрать · Enter одобрить и начать задачу · Esc вернуть задачу",
    "notice.effort": "Глубина рассуждений: {level}",
    "notice.effort_invalid": "Допустимые уровни: {levels}",
    "notice.mode": "Режим: {mode}",
    "notice.mode_invalid": "Допустимые режимы: {modes}",
    "notice.language": "Язык: {language}",
    "notice.language_invalid": "Язык: ru или en",
    "notice.compacted": "Контекст сжат: {summary}",
    "notice.no_reasoning": "Для последнего ответа размышлений нет.",
    "notice.skills_empty": "Навыки для этого репозитория не найдены.",
    "notice.skill_failed": "Не удалось прочитать навык {name}.",
    "notice.mcp_empty": "MCP-серверов нет. Добавьте: /mcp add.",
    "notice.mcp_probe": "Проверяю {server}…",
    "notice.mcp_probe_ok": "{server}: доступен, инструментов {count}.",
    "notice.mcp_probe_failed": "{server}: ошибка — {error}",
    "notice.mcp_selected": "MCP-сервер {server} выбран; разрешено инструментов: {count}.",
    "notice.mcp_selecting": "Выбираю инструменты {server}…",
    "notice.mcp_added": "MCP-сервер {server} сохранён.",
    "notice.mcp_removed": "MCP-сервер {server} удалён.",
    "notice.catalog_failed": "Каталог провайдеров недоступен: {error}",
    "notice.connecting": "Подключаю {provider}…",
    "notice.connected": "{provider} подключён как {id}: моделей {models}, с ценами {priced}.",
    "notice.connect_failed": "Не удалось подключить {provider}: {error}",
    "notice.connect_pick_model": "Выберите модель {id}.",
    "notice.key_empty": "Ключ не введён — подключение отменено.",
    "notice.variable_empty": "{name} не указан — подключение отменено.",
    "notice.cancelled": "Отменено.",
    "notice.provider_saved": "Провайдер сохранён, модель {model} выбрана.",
    # ------------------------------------------------------------ pickers
    "models.title": "Модель",
    "models.current": "текущая",
    "models.selected": "Модель: {model}",
    "models.context": "контекст {tokens}",
    "models.price": "${input}/${output} за 1M",
    "models.no_price": "цена неизвестна",
    "sessions.title": "Сессии этого репозитория",
    "sessions.current": "текущая",
    "connect.title": "Подключить провайдера",
    "connect.models": "моделей {count}",
    "connect.connected": "подключён",
    "connect.key_env": "ключ {env}",
    "connect.no_key": "без ключа",
    "connect.custom": "Свой OpenAI-совместимый endpoint…",
    "connect.custom_detail": "base URL, модель и ключ вручную",
    "effort.title": "Глубина рассуждений",
    "mode.title": "Режим",
    "mode.build": "правит файлы и проверяет результат",
    "mode.plan": "только план, без правок",
    "mode.ideate": "идеи и варианты, без правок",
    "lang.title": "Язык интерфейса",
    "skills.title": "Навыки репозитория",
    "mcp.title": "MCP-серверы",
    "mcp.add_item": "Добавить сервер…",
    "mcp.add_detail": "stdio-команда или Streamable HTTP URL",
    "mcp.actions_title": "MCP · {server}",
    "mcp.action.probe": "Проверить подключение",
    "mcp.action.select": "Подключить к сессии: все инструменты",
    "mcp.action.select_detail": "агент сможет вызывать любой инструмент сервера",
    "mcp.action.remove": "Удалить сервер",
    "mcp.confirm_remove": "Удалить {server}?",
    "mcp.confirm_yes": "Да, удалить",
    "mcp.confirm_no": "Отмена",
    # ---------------------------------------------------- MCP add form
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
    "mcp.oauth": "OAuth для streamable_http",
    "mcp.read_only": "Read-only инструменты, через запятую",
    "mcp.timeout": "Таймаут, сек",
    "mcp.error_id": "Нужны ID сервера и пространство имён.",
    "mcp.error_command": "Для stdio-сервера нужна команда.",
    "mcp.error_url": "Для удалённого сервера нужен URL.",
    "mcp.error_timeout": "Таймаут должен быть числом.",
    "form.save": "Сохранить",
    "form.cancel": "Отмена",
    "form.saving": "сохраняю…",
    "form.yes": "да",
    "form.no": "нет",
    # ------------------------------------------------ custom provider form
    "provider.title": "Свой провайдер",
    "provider.preset": "Тип",
    "provider.id": "ID провайдера",
    "provider.base_url": "Base URL",
    "provider.model": "ID модели",
    "provider.key": "API-ключ (хранится в системном хранилище)",
    "provider.save": "Проверить и сохранить",
    "provider.cancel": "Отмена",
    "provider.required": "Заполните обязательные поля.",
    "provider.key_required": "Для удалённого провайдера нужен API-ключ.",
    "provider.failed": "Не удалось сохранить: {error}",
    "provider.testing": "проверяю endpoint…",
    "provider.no_presets": "нет доступных типов провайдеров",
    # ------------------------------------------------------------- result
    "result.verified": "Проверено",
    "result.answer": "Готово",
    "result.failed": "Не завершено",
    "result.checks": "проверки: {passed}/{total}",
    "result.tokens": "{count} ток.",
    "result.duration": "{seconds:.1f} с",
    "result.cost": "{cost}",
    "result.reason": "причина: {reason}",
    "result.changed": "изменено: {files}",
    "result.unmet": "не выполнено: {items}",
    # ----------------------------------------------------------- activity
    "activity.connecting": "подключаю {provider}…",
    # --------------------------------------------------------- tool lines
    "tool.running": "выполняется",
    "tool.failed": "ошибка",
    "tool.call": "вызов {id}",
    "tool.category": "тип {category}",
    "reasoning.line": "размышления · {count} симв.",
    "reasoning.title": "Размышления модели",
    # ------------------------------------------------------- status block
    "info.title": "Состояние",
    "info.repository": "репозиторий",
    "info.branch": "ветка",
    "info.model": "модель",
    "info.session": "сессия",
    "info.settings": "глубина {effort} · режим {mode} · язык {language}",
    "info.tokens": "токены",
    "info.verification": "проверки",
    "info.suggested": "предложенные проверки",
    "info.none": "нет",
    "info.checkpoint": "сохранено: {when}",
    "info.no_checkpoint": "сессия ещё не сохранена",
    "info.run_counters": (
        "последний запуск: чтения {reads} · правки {edits} · проверки ✓{ok}/✗{bad} · "
        "mcp {mcp} · навыки {skills}"
    ),
    "doctor.title": "Диагностика",
    "doctor.running": "проверяю провайдера, ключи, MCP и сессии…",
    # ------------------------------------------------------ session state
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
    "session.section.files": "изменённые файлы",
    "session.none": "нет",
    "session.meta_detail": "ревизия {revision} · обновлена {when}",
    "session.meta_branch": "ветка {branch}",
    "session.history": "+{count} предыдущих хода",
    "session.plan_tally": "к выполнению {todo} · выполняется {doing} · готово {done}",
    "session.checkpoints_line": "{count} сохранено · последняя {when}",
    "session.checkpoint_files": "в точке: {tracked} отслеживаемых, {untracked} новых файлов",
    "session.checks_line": "{passed}/{total} пройдено",
    "session.verified_at": "проверено на шаге {step}",
    "session.provider_line": "{requests} запросов · {tokens} токенов",
    "session.cost": "стоимость {cost}",
    "session.cost_unknown": "стоимость неизвестна ({reason})",
    "session.files_line": "изменено файлов: {count}",
    # -------------------------------------------------------------- skills
    "skill.detail_title": "Навык {name}",
    "skill.version": "версия",
    "skill.when_to_use": "Когда использовать",
    "skill.permissions": "Права",
    "skill.files": "Файлы",
}

_EN: dict[str, str] = {
    "app.title": "KaroX",
    # ------------------------------------------------------------ welcome
    "welcome.title": "Welcome to KaroX",
    "welcome.body": "/help for help · /model to pick a model · /connect to add a provider",
    "welcome.facts.verification": "verification: {commands}",
    "welcome.facts.suggested": "tip: /verify {commands} to check every change with it",
    # ----------------------------------------------------------- composer
    "composer.placeholder": "Describe a task…",
    "composer.filter": "Type to search · ↑↓ move · Enter choose · Esc cancel",
    "secret.placeholder": "{provider} API key ({env}) · Enter connect · Esc cancel",
    "secret.placeholder_plain": "{provider} API key · Enter connect · Esc cancel",
    "secret.placeholder_saved": "{provider} API key · empty Enter keeps the saved key · Esc cancel",
    "secret.variable": "{name} for {provider} · Enter next · Esc cancel",
    "menu.palette_hint": "Tab complete · Enter run · Esc close",
    "menu.picker_hint": "↑↓ move · Enter choose · Esc cancel",
    "menu.empty": "no matches",
    "menu.more": "{count} more — narrow the search",
    # ------------------------------------------------------------- status
    "status.loading": "loading…",
    "status.no_model": "no model selected",
    # ----------------------------------------------------------- commands
    "cmd.help": "commands and keys",
    "cmd.connect": "connect a provider with its API key",
    "cmd.model": "choose a model",
    "cmd.sessions": "sessions in this repository, continue one",
    "cmd.new": "start a new session",
    "cmd.session": "durable session state",
    "cmd.status": "model, session, checks, counters",
    "cmd.mcp": "MCP servers: probe, allow, remove, add",
    "cmd.skills": "repository skills",
    "cmd.verify": "add a verification command",
    "cmd.unverify": "clear verification commands",
    "cmd.effort": "reasoning effort",
    "cmd.mode": "mode: build, plan, ideate",
    "cmd.lang": "interface language",
    "cmd.reasoning": "the model's reasoning for the last answer",
    "cmd.doctor": "check provider, keys, MCP and sessions",
    "cmd.clear": "clear the screen",
    "cmd.stop": "stop the agent",
    "cmd.quit": "exit",
    # -------------------------------------------------------------- help
    "help.title": "Commands",
    "help.keys_title": "Keys",
    "help.keys": (
        "Enter send · Ctrl+J newline · ↑↓ history · ? shortcuts\n"
        "/ command list · Tab complete · Esc close the list or interrupt the agent\n"
        "Shift+Tab cycle mode · Ctrl+O tool details · Ctrl+R reasoning\n"
        "PageUp/PageDown scroll · Ctrl+N new session · Ctrl+L clear\n"
        "Ctrl+C interrupt or exit · Ctrl+Q exit · click a tool line for its detail"
    ),
    # ----------------------------------------------------------- notices
    "notice.no_model": (
        "No model selected. /connect adds a provider with its API key, "
        "/model picks a model."
    ),
    "notice.already_running": "The agent is already running. Press Esc to stop it.",
    "notice.stopped": "Stopped. The session can be resumed later.",
    "notice.backend_error": "Run failed: {error}",
    "notice.session_repo": "That session belongs to another repository: {repository}",
    "notice.session_missing": "Session not found: {id}",
    "notice.clear": "Screen cleared.",
    "notice.new_session": "New session {id}.",
    "notice.session_selected": "Session {id} selected. The next task continues in it.",
    "notice.session_task": "task: {task}",
    "notice.no_sessions": "No saved sessions for this repository.",
    "notice.models_empty": "No models yet. Connect a provider: /connect.",
    "notice.unknown_command": "Unknown command /{command}. Type / to see the list.",
    "notice.verify_added": "Verification added: {command}",
    "notice.verify_cleared": "Verification list cleared.",
    "notice.verify_none": "No verification commands. Add one: /verify <command>.",
    "notice.verify_list": "Verification: {commands}",
    "notice.verify_parse": "Cannot parse the command: {error}",
    "notice.verify_custom": "Finish the command after /verify; the task starts right after it.",
    "verify.title": "How should the changes be checked? The agent starts only with an approved check",
    "verify.detected": "found in the project",
    "verify.custom": "Another command…",
    "verify.custom_detail": "/verify <command>",
    "verify.hint": "↑↓ move · Enter approve and start the task · Esc give the task back",
    "notice.effort": "Reasoning effort: {level}",
    "notice.effort_invalid": "Effort levels: {levels}",
    "notice.mode": "Mode: {mode}",
    "notice.mode_invalid": "Modes: {modes}",
    "notice.language": "Language: {language}",
    "notice.language_invalid": "Language: ru or en",
    "notice.compacted": "Context compacted: {summary}",
    "notice.no_reasoning": "No reasoning recorded for the last answer.",
    "notice.skills_empty": "No skills discovered for this repository.",
    "notice.skill_failed": "Could not read skill {name}.",
    "notice.mcp_empty": "No MCP servers. Add one: /mcp add.",
    "notice.mcp_probe": "Probing {server}…",
    "notice.mcp_probe_ok": "{server}: reachable, {count} tools.",
    "notice.mcp_probe_failed": "{server}: failed — {error}",
    "notice.mcp_selected": "MCP server {server} selected; tools allowed: {count}.",
    "notice.mcp_selecting": "Selecting the tools of {server}…",
    "notice.mcp_added": "MCP server {server} saved.",
    "notice.mcp_removed": "MCP server {server} removed.",
    "notice.catalog_failed": "The provider catalog is unavailable: {error}",
    "notice.connecting": "Connecting {provider}…",
    "notice.connected": "{provider} connected as {id}: {models} models, {priced} with prices.",
    "notice.connect_failed": "Could not connect {provider}: {error}",
    "notice.connect_pick_model": "Choose a {id} model.",
    "notice.key_empty": "No key entered — connection cancelled.",
    "notice.variable_empty": "{name} is empty — connection cancelled.",
    "notice.cancelled": "Cancelled.",
    "notice.provider_saved": "Provider saved, model {model} selected.",
    # ------------------------------------------------------------ pickers
    "models.title": "Model",
    "models.current": "current",
    "models.selected": "Model: {model}",
    "models.context": "{tokens} context",
    "models.price": "${input}/${output} per 1M",
    "models.no_price": "no published price",
    "sessions.title": "Sessions in this repository",
    "sessions.current": "current",
    "connect.title": "Connect a provider",
    "connect.models": "{count} models",
    "connect.connected": "connected",
    "connect.key_env": "key {env}",
    "connect.no_key": "no key",
    "connect.custom": "Custom OpenAI-compatible endpoint…",
    "connect.custom_detail": "base URL, model and key by hand",
    "effort.title": "Reasoning effort",
    "mode.title": "Mode",
    "mode.build": "edits files and verifies the result",
    "mode.plan": "a plan only, no edits",
    "mode.ideate": "ideas and options, no edits",
    "lang.title": "Interface language",
    "skills.title": "Repository skills",
    "mcp.title": "MCP servers",
    "mcp.add_item": "Add a server…",
    "mcp.add_detail": "stdio command or Streamable HTTP URL",
    "mcp.actions_title": "MCP · {server}",
    "mcp.action.probe": "Probe the connection",
    "mcp.action.select": "Use in this session: all tools",
    "mcp.action.select_detail": "the agent may call any tool of the server",
    "mcp.action.remove": "Remove the server",
    "mcp.confirm_remove": "Remove {server}?",
    "mcp.confirm_yes": "Yes, remove",
    "mcp.confirm_no": "Cancel",
    # ---------------------------------------------------- MCP add form
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
    "mcp.oauth": "OAuth for streamable_http",
    "mcp.read_only": "Read-only tools, comma separated",
    "mcp.timeout": "Timeout, seconds",
    "mcp.error_id": "Server ID and namespace are required.",
    "mcp.error_command": "A stdio server needs a command.",
    "mcp.error_url": "A remote server needs a URL.",
    "mcp.error_timeout": "The timeout must be a number.",
    "form.save": "Save",
    "form.cancel": "Cancel",
    "form.saving": "saving…",
    "form.yes": "yes",
    "form.no": "no",
    # ------------------------------------------------ custom provider form
    "provider.title": "Custom provider",
    "provider.preset": "Kind",
    "provider.id": "Provider ID",
    "provider.base_url": "Base URL",
    "provider.model": "Model ID",
    "provider.key": "API key (stored in the OS keyring)",
    "provider.save": "Verify and save",
    "provider.cancel": "Cancel",
    "provider.required": "Fill the required fields.",
    "provider.key_required": "A remote provider needs an API key.",
    "provider.failed": "Could not save: {error}",
    "provider.testing": "testing the endpoint…",
    "provider.no_presets": "no installable provider kinds",
    # ------------------------------------------------------------- result
    "result.verified": "Verified",
    "result.answer": "Done",
    "result.failed": "Not finished",
    "result.checks": "checks: {passed}/{total}",
    "result.tokens": "{count} tokens",
    "result.duration": "{seconds:.1f}s",
    "result.cost": "{cost}",
    "result.reason": "reason: {reason}",
    "result.changed": "changed: {files}",
    "result.unmet": "unmet: {items}",
    # ----------------------------------------------------------- activity
    "activity.connecting": "connecting {provider}…",
    # --------------------------------------------------------- tool lines
    "tool.running": "running",
    "tool.failed": "failed",
    "tool.call": "call {id}",
    "tool.category": "kind {category}",
    "reasoning.line": "reasoning · {count} chars",
    "reasoning.title": "Model reasoning",
    # ------------------------------------------------------- status block
    "info.title": "Status",
    "info.repository": "repository",
    "info.branch": "branch",
    "info.model": "model",
    "info.session": "session",
    "info.settings": "effort {effort} · mode {mode} · language {language}",
    "info.tokens": "tokens",
    "info.verification": "verification",
    "info.suggested": "detected checks",
    "info.none": "none",
    "info.checkpoint": "saved: {when}",
    "info.no_checkpoint": "no session checkpoint yet",
    "info.run_counters": (
        "last run: reads {reads} · edits {edits} · checks ✓{ok}/✗{bad} · "
        "mcp {mcp} · skills {skills}"
    ),
    "doctor.title": "Diagnostics",
    "doctor.running": "checking provider, keys, MCP and sessions…",
    # ------------------------------------------------------ session state
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
    "session.section.files": "changed files",
    "session.none": "none",
    "session.meta_detail": "rev {revision} · updated {when}",
    "session.meta_branch": "branch {branch}",
    "session.history": "+{count} earlier turn(s)",
    "session.plan_tally": "todo {todo} · doing {doing} · done {done}",
    "session.checkpoints_line": "{count} saved · last {when}",
    "session.checkpoint_files": "in checkpoint: {tracked} tracked, {untracked} untracked files",
    "session.checks_line": "{passed}/{total} passed",
    "session.verified_at": "verified at step {step}",
    "session.provider_line": "{requests} requests · {tokens} tokens",
    "session.cost": "cost {cost}",
    "session.cost_unknown": "cost unknown ({reason})",
    "session.files_line": "changed files: {count}",
    # -------------------------------------------------------------- skills
    "skill.detail_title": "Skill {name}",
    "skill.version": "version",
    "skill.when_to_use": "When to use",
    "skill.permissions": "Permissions",
    "skill.files": "Files",
}

_RU.update(
    {
        # -------------------------------------------------- welcome (box)
        "welcome.cwd": "папка: {path}",
        "welcome.model": "модель: {model}",
        # ---------------------------------------------------------- footer
        "footer.hint": "? — подсказки",
        "footer.mode": "⏸ режим {mode} · shift+tab — сменить",
        "footer.verbose": "подробно",
        # ----------------------------------------------------- working line
        "working.thinking": "Думаю…",
        "working.reasoning": "Размышляю…",
        "working.writing": "Пишу ответ…",
        "working.stopping": "Останавливаю…",
        "working.facts": "({elapsed} · {tokens} ток. · esc — прервать)",
        "working.facts_quiet": "({elapsed} · esc — прервать)",
        "working.next": "⎿ Шаг {index} из {total}: {item}",
        "working.queued": "› В очереди: {text}",
        "working.queued_more": "› В очереди: {text} (+{count})",
        "working.retry": "⎿ Повторяю запрос к модели: {detail}",
        "toolres.interrupted": "Прервано",
        # ------------------------------------------------- kernel warnings
        "warn.engine_repair_prompt": "Проверка не прошла — агент делает ещё одну попытку исправить.",
        "warn.harness_final_checks": "Финальные проверки: {detail}",
        "warn.regression_restored": "Правка сломала проверку и откатена: {detail}",
        "warn.context_floor_exceeded": "Контекст почти заполнен и не сжимается дальше: {detail}",
        "warn.fenced_command_refused": "Команда в тексте не выполняется: KaroX вызывает только инструменты.",
        "warn.unproducible_requirement": "Оставшееся доказательство нельзя получить разрешённой командой.",
        # ------------------------------------------------------ mcp states
        "mcp.state.not_selected": "не в сессии",
        "mcp.state.current": "в сессии · разрешено {count}",
        "mcp.state.stale": "выбор устарел — подключите заново",
        "mcp.state.pending": "подключится с первой задачей",
        "mcp.live.not_probed": "не проверен",
        "mcp.live.live": "доступен",
        "mcp.live.failed": "недоступен",
        "mcp.cred.missing": "нет секрета",
        # ---------------------------------------------------------- verbs
        "verb.read": "Читаю {target}…",
        "verb.read.bare": "Читаю файл…",
        "verb.list": "Смотрю файлы {target}…",
        "verb.list.bare": "Смотрю файлы…",
        "verb.search": "Ищу {target}…",
        "verb.search.bare": "Ищу по проекту…",
        "verb.inspect": "Изучаю проект: {target}…",
        "verb.inspect.bare": "Изучаю проект…",
        "verb.edit": "Правлю {target}…",
        "verb.edit.bare": "Правлю файлы…",
        "verb.write": "Записываю {target}…",
        "verb.write.bare": "Записываю файл…",
        "verb.run": "Запускаю {target}…",
        "verb.run.bare": "Запускаю команду…",
        "verb.test": "Запускаю тесты {target}…",
        "verb.test.bare": "Запускаю тесты…",
        "verb.check": "Проверяю: {target}…",
        "verb.check.bare": "Запускаю проверку…",
        "verb.plan": "Обновляю план…",
        "verb.plan.bare": "Обновляю план…",
        "verb.web": "Загружаю {target}…",
        "verb.web.bare": "Загружаю страницу…",
        "verb.browser": "Браузер: {target}…",
        "verb.browser.bare": "Работаю в браузере…",
        "verb.visual": "Смотрю на результат: {target}…",
        "verb.visual.bare": "Смотрю на результат…",
        "verb.mcp": "MCP {target}…",
        "verb.git": "Git: {target}…",
        "verb.skill": "Навык {target}…",
        "verb.other": "{target}…",
        # --------------------------------------------------- tool results
        "toolres.running": "Выполняется…",
        "toolres.read": "Прочитано {lines}",
        "toolres.truncated": " (обрезано)",
        "toolres.read_range": "Строки {start}–{end} из {total}",
        "toolres.found": "Найдено {matches}",
        "toolres.found_in": "Найдено {matches} · {files}",
        "toolres.no_matches": "Совпадений нет",
        "toolres.listed": "Найдено {files}",
        "toolres.edited": "Изменено: +{added} −{removed}",
        "toolres.created": "Создан · {lines}",
        "toolres.overwritten": "Перезаписан · {lines}",
        "toolres.appended": "Дописано {lines}",
        "toolres.patched": "Патч: {files}, +{added} −{removed}",
        "toolres.files_changed": "Изменено: {files}",
        "toolres.plan": "План: {items}, готово {done}",
        "toolres.job_started": "Запущено в фоне · {job}",
        "toolres.job_status": "Статус: {status}",
        "toolres.exit": "Код выхода {code}",
        "toolres.timed_out": "Превышено время ожидания",
        "toolres.tests_ok": "Тесты прошли",
        "toolres.tests_failed": "Тесты упали (код {code})",
        "toolres.check_ok": "Проверка пройдена",
        "toolres.check_failed": "Проверка не пройдена (код {code})",
        "toolres.done": "Готово",
        "toolres.failed": "Ошибка",
        "toolres.error": "Ошибка: {detail}",
        "toolres.reused": "Без изменений с шага {step} — прежний результат",
        "toolres.more": "… ещё {lines} · ctrl+o — развернуть",
        # -------------------------------------------------------- thinking
        "thinking.live": "Размышляю…",
        "thinking.done": "Размышлял {elapsed} · ctrl+r — показать",
        "thinking.summary": "Ход мысли",
        # -------------------------------------------------------- shortcuts
        "shortcuts.text": (
            "/ — команды          ctrl+j — новая строка     ctrl+r — размышления\n"
            "esc — прервать       ↑↓ — история              ctrl+o — подробности\n"
            "shift+tab — режим    ctrl+n — новая сессия     ctrl+c — выход"
        ),
        # -------------------------------------------------------------- time
        "time.seconds": "{s} с",
        "time.minutes": "{m} мин {s} с",
        "time.hours": "{h} ч {m} мин",
        # --------------------------------------------------------------- mcp
        "mcp.action.select_read": "Подключить к сессии: только чтение",
        "mcp.action.select_read_detail": "инструменты без пометки read-only будут запрещены",
        "mcp.action.deselect": "Отключить от сессии",
        "notice.mcp_pending": (
            "MCP-сервер {server} будет подключён к сессии при первой задаче "
            "(инструментов: {count})."
        ),
        "notice.mcp_deselected": "MCP-сервер {server} отключён от сессии.",
        "notice.mcp_apply_failed": "Не удалось подключить MCP-сервер {server}: {error}",
    }
)

_EN.update(
    {
        # -------------------------------------------------- welcome (box)
        "welcome.cwd": "cwd: {path}",
        "welcome.model": "model: {model}",
        # ---------------------------------------------------------- footer
        "footer.hint": "? for shortcuts",
        "footer.mode": "⏸ {mode} mode · shift+tab to cycle",
        "footer.verbose": "verbose",
        # ----------------------------------------------------- working line
        "working.thinking": "Thinking…",
        "working.reasoning": "Reasoning…",
        "working.writing": "Writing…",
        "working.stopping": "Stopping…",
        "working.facts": "({elapsed} · {tokens} tokens · esc to interrupt)",
        "working.facts_quiet": "({elapsed} · esc to interrupt)",
        "working.next": "⎿ Step {index} of {total}: {item}",
        "working.queued": "› Queued: {text}",
        "working.queued_more": "› Queued: {text} (+{count})",
        "working.retry": "⎿ Retrying the model call: {detail}",
        "toolres.interrupted": "Interrupted",
        # ------------------------------------------------- kernel warnings
        "warn.engine_repair_prompt": "A check failed — the agent gets one more turn to fix it.",
        "warn.harness_final_checks": "Final checks: {detail}",
        "warn.regression_restored": "An edit broke a check and was rolled back: {detail}",
        "warn.context_floor_exceeded": "The context is nearly full and cannot shrink further: {detail}",
        "warn.fenced_command_refused": "Commands in text are not run: KaroX only calls tools.",
        "warn.unproducible_requirement": "The remaining proof cannot come from an approved command.",
        # ------------------------------------------------------ mcp states
        "mcp.state.not_selected": "not in this session",
        "mcp.state.current": "in session · {count} allowed",
        "mcp.state.stale": "selection is stale — select again",
        "mcp.state.pending": "joins with the first task",
        "mcp.live.not_probed": "not probed",
        "mcp.live.live": "reachable",
        "mcp.live.failed": "unreachable",
        "mcp.cred.missing": "secret missing",
        # ---------------------------------------------------------- verbs
        "verb.read": "Reading {target}…",
        "verb.read.bare": "Reading a file…",
        "verb.list": "Listing {target}…",
        "verb.list.bare": "Listing files…",
        "verb.search": "Searching {target}…",
        "verb.search.bare": "Searching the project…",
        "verb.inspect": "Inspecting the project: {target}…",
        "verb.inspect.bare": "Inspecting the project…",
        "verb.edit": "Editing {target}…",
        "verb.edit.bare": "Editing files…",
        "verb.write": "Writing {target}…",
        "verb.write.bare": "Writing a file…",
        "verb.run": "Running {target}…",
        "verb.run.bare": "Running a command…",
        "verb.test": "Running tests {target}…",
        "verb.test.bare": "Running tests…",
        "verb.check": "Checking: {target}…",
        "verb.check.bare": "Running a check…",
        "verb.plan": "Updating the plan…",
        "verb.plan.bare": "Updating the plan…",
        "verb.web": "Fetching {target}…",
        "verb.web.bare": "Fetching a page…",
        "verb.browser": "Browser: {target}…",
        "verb.browser.bare": "Working in the browser…",
        "verb.visual": "Looking at the result: {target}…",
        "verb.visual.bare": "Looking at the result…",
        "verb.mcp": "MCP {target}…",
        "verb.git": "Git: {target}…",
        "verb.skill": "Skill {target}…",
        "verb.other": "{target}…",
        # --------------------------------------------------- tool results
        "toolres.running": "Running…",
        "toolres.read": "Read {lines}",
        "toolres.truncated": " (truncated)",
        "toolres.read_range": "Lines {start}–{end} of {total}",
        "toolres.found": "Found {matches}",
        "toolres.found_in": "Found {matches} in {files}",
        "toolres.no_matches": "No matches",
        "toolres.listed": "Listed {files}",
        "toolres.edited": "Updated: +{added} −{removed}",
        "toolres.created": "Created · {lines}",
        "toolres.overwritten": "Overwritten · {lines}",
        "toolres.appended": "Appended {lines}",
        "toolres.patched": "Patched {files}, +{added} −{removed}",
        "toolres.files_changed": "Changed {files}",
        "toolres.plan": "Plan: {items}, {done} done",
        "toolres.job_started": "Started in background · {job}",
        "toolres.job_status": "Status: {status}",
        "toolres.exit": "Exit code {code}",
        "toolres.timed_out": "Timed out",
        "toolres.tests_ok": "Tests passed",
        "toolres.tests_failed": "Tests failed (exit {code})",
        "toolres.check_ok": "Check passed",
        "toolres.check_failed": "Check failed (exit {code})",
        "toolres.done": "Done",
        "toolres.failed": "Error",
        "toolres.error": "Error: {detail}",
        "toolres.reused": "Unchanged since step {step} — previous result stands",
        "toolres.more": "… +{lines} · ctrl+o to expand",
        # -------------------------------------------------------- thinking
        "thinking.live": "Thinking…",
        "thinking.done": "Thought for {elapsed} · ctrl+r to show",
        "thinking.summary": "Thinking",
        # -------------------------------------------------------- shortcuts
        "shortcuts.text": (
            "/ for commands        ctrl+j newline           ctrl+r reasoning\n"
            "esc to interrupt      ↑↓ history               ctrl+o tool details\n"
            "shift+tab cycle mode  ctrl+n new session       ctrl+c exit"
        ),
        # -------------------------------------------------------------- time
        "time.seconds": "{s}s",
        "time.minutes": "{m}m {s}s",
        "time.hours": "{h}h {m}m",
        # --------------------------------------------------------------- mcp
        "mcp.action.select_read": "Use in this session: read-only tools",
        "mcp.action.select_read_detail": "tools not marked read-only stay denied",
        "mcp.action.deselect": "Remove from this session",
        "notice.mcp_pending": (
            "MCP server {server} will join the session with the first task "
            "({count} tools)."
        ),
        "notice.mcp_deselected": "MCP server {server} removed from the session.",
        "notice.mcp_apply_failed": "Could not attach MCP server {server}: {error}",
    }
)

#: Noun forms for counted phrases: English singular/plural, Russian one/few/many.
_NOUNS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "line": ("line", "lines"),
        "file": ("file", "files"),
        "match": ("match", "matches"),
        "item": ("item", "items"),
        "step": ("step", "steps"),
        "tool": ("tool", "tools"),
    },
    "ru": {
        "line": ("строка", "строки", "строк"),
        "file": ("файл", "файла", "файлов"),
        "match": ("совпадение", "совпадения", "совпадений"),
        "item": ("пункт", "пункта", "пунктов"),
        "step": ("шаг", "шага", "шагов"),
        "tool": ("инструмент", "инструмента", "инструментов"),
    },
}


def plural(lang: str, count: int, noun: str) -> str:
    """``40 строк`` / ``1 line``: the count with the form its language needs."""

    forms = _NOUNS.get(lang, _NOUNS["en"]).get(noun) or _NOUNS["en"].get(noun) or (noun, noun)
    if len(forms) == 3:
        tail, tens = count % 10, count % 100
        if tail == 1 and tens != 11:
            word = forms[0]
        elif 2 <= tail <= 4 and not 12 <= tens <= 14:
            word = forms[1]
        else:
            word = forms[2]
    else:
        word = forms[0] if count == 1 else forms[1]
    return f"{count} {word}"

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
