# KaroX v4.0.0 — Универсальный агентский движок разработки

KaroX 4.0 превращает сервер из набора репо-инструментов в полноценный движок автономной разработки: надёжность под нагрузкой, «глаза» (картинки и скриншоты), полный локальный git-цикл, веб- и десктоп-циклы без участия человека.

## Фаза 0 — Надёжность (P0)

- **Supervisor + watchdog** (`scripts/karox_supervisor.py`): heartbeat каждые 5 с (`GET /watchdog/ping`), два подряд провала — рестарт всего дерева процессов быстрее 3 с, JSONL-журнал рестартов.
- **Persistent-сессии и resume**: ветка, задача и разрешения сессии сохраняются на диске; `karox_session(action="resume")` возвращает ту же ветку и задачу после рестарта вместо новой `promptql/full-<timestamp>`.
- **Idempotency-key** на мутирующих запросах (exec, запись файлов, патчи, fs-операции, commit-hunks): повтор после обрыва не дублирует изменение.
- **Очередь под нагрузкой**: приоритеты (заголовок `X-Priority`), per-request timeout (`X-Request-Timeout`), честный ответ «занят, позиция N» (503 + `retryable: true`) вместо connection refused.
- MCP-обёртки переводят транспортные сбои в структурный результат `{"retryable": true}`.

## Фаза 1 — Исполнение (P0)

- **`karox_exec` с argv-массивом**: аргументы передаются дословно, без cmd-обёртки — кавычки больше не ломаются. Опциональный shell `cmd|powershell|bash|sh`, cwd, env (значения секретов маскируются в аудите), stdin.
- **UTF-8 везде**: `chcp 65001` / `PYTHONIOENCODING=utf-8`, нормализация legacy-вывода cp866/cp1251.
- **Асинхронные джобы**: `karox_job` start/status (state, exitCode, uptime, CPU/RAM)/tail с follow-until-pattern/signal (kill|int)/list; `karox_wait_for` port/http.
- **`karox_checks_v2`**: матрица команд с allow_failure, повторами flaky-тестов, суммарным отчётом и первой ошибкой компиляции отдельным полем (парсеры javac, kotlin, tsc, eslint, pytest, gradle, gcc).

## Фаза 2 — Файлы (P0)

- **Бинарные файлы**: `karox_bytes` read/write (base64 + sha256); **`karox_read_image`** возвращает картинку как MCP image content — «глаза» агента.
- **Файловые операции**: move, copy, mkdir, glob; `delete_dir` — только с явным opt-in на сессию, подтверждением и запретом вне репо.
- **`karox_apply_patch`**: приём unified diff целиком с pre-check и скан-блокировкой секретов.
- **Чекпоинты рабочего дерева**: `karox_checkpoint` create/restore — мгновенный откат экспериментов без коммитов (снимки через git write-tree/commit-tree в `refs/karox/checkpoints/*`).
- **Поиск v2**: regex, поиск по именам файлов, ограничения по размеру/расширениям, контекстные строки.

## Фаза 3 — Git полного цикла (P1; push запрещён навсегда)

- `karox_git2`: branch create/switch/list (с авто-stash), stash push/pop/list, log с фильтрами, show, blame, restore, diff между ревизиями, локальные merge/rebase с конфликт-репортом и авто-abort.
- **Secret-scan v2** на write и commit: регэкспы токенов + энтропия Шеннона, блокировка с точным номером строки.
- **Частичный коммит по hunk'ам**: `/git/v2/hunks` + `/git/v2/commit-hunks`.

## Фаза 4 — Веб-проекты (P1)

- **Managed dev-server**: `karox_devserver start` → джоб + автоопределение порта из лога; `stop`.
- **`karox_http_fetch`** — только localhost: статус, заголовки, тело.
- **Headless-браузер (Playwright)**: `karox_browser` screenshot/dom/console/click/type; по умолчанию только localhost.
- **Пакетные менеджеры по allowlist** (npm/pnpm/yarn/pip/poetry/cargo/gradle/maven) с анализом lockfile-диффа; publish/login/deploy — hard-block.

## Фаза 5 — Игры и десктоп (P1)

- **`karox_screen`**: скриншот экрана/окна (по заголовку окна или региону), запись короткого GIF — оценка анимаций.
- **Ввод в окна** — строго opt-in на сессию (`allow_input`), только в границах целевого окна; по умолчанию выключен (non-interference).
- **События** (`karox_events`): «job умер», «dev-server поднялся», «проверки упали».

## Фаза 6 — Отладка и REPL (P2)

- **DAP-мост** (`karox_dap`): breakpoint, step, inspect variables через универсальный Debug Adapter Protocol (python из коробки через debugpy, любой другой адаптер через argv).
- **Персистентные REPL** (`karox_repl`): python/node с сохранением состояния между вызовами.
- **Structured error extraction**: `{file, line, message}` списком, первая ошибка отдельным полем.

## Фаза 7 — Контекст проекта (P2)

- **Project map** (`karox_project_map`): тип проекта, точки входа, команды build/test/run, ключевые директории.
- **Мульти-репо** (`karox_workspace`): регистрация и переключение репозиториев; switch включается только переменной `KAROX_ALLOW_WORKSPACE_SWITCH=1`.
- **Долгая память** (`karox_memo`): key-value заметки per-repo, переживают рестарты и сессии.

## Безопасность

- Hard-blocks не ослаблены: git push, публикация пакетов, системные/креденшл-команды — запрещены навсегда.
- Все новые инструменты работают внутри песочницы репозитория и требуют API-ключ.
- Браузер и http_fetch — только localhost по умолчанию.
- Ввод в окна и удаление директорий — только с явным opt-in на сессию.
- Аудит-лог покрывает 100% новых инструментов.

## Запуск под супервизором

```
python scripts/karox_supervisor.py --port 8765 --api-key <KEY> -- <команда запуска сервера>
```

Или задайте команду сервера через `KAROX_SERVER_CMD`.
