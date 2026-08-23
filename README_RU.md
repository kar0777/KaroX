# KaroX 5 Preview

**Запускайте AI-агентов на локальных Git-репозиториях, не отдавая одному
провайдеру контроль над разрешениями, сессиями и доказательствами работы.**

KaroX — локальный контрольный слой для ChatGPT, Claude, API-моделей и
совместимых MCP-клиентов. Каждое локальное действие проходит через один runtime,
привязанный к выбранному репозиторию: с явными правами, безопасными повторными
операциями, сохраняемыми сессиями, разрешёнными проверками, Git evidence и
фильтрацией секретов.

KaroX не является моделью, IDE или полноценной системной песочницей. Разрешённый
процесс всё ещё выполняется с правами пользователя, запустившего KaroX. Проект
ограничивает полномочия через `repoRoot`, capabilities, allowlist, leases,
idempotency и жёсткие запреты, но не виртуализирует операционную систему.

> **Статус релиза:** packaged runtime имеет версию `5.0.0.dev0`. Локальные
> контракты и transport-paths покрыты тестами, но реальные live-проверки ChatGPT
> Web, Claude Web и обязательных API-провайдеров ещё не завершены. Смотри
> [scope KaroX 5.0](docs/V5_RELEASE_SCOPE.md),
> [release checklist](docs/RELEASE_CHECKLIST.md),
> [план внешней беты](docs/BETA_TEST_PLAN.md),
> [миграцию 4.x → 5](docs/MIGRATION_V4_TO_V5.md) и
> [live conformance records](docs/conformance/README.md).

## Главный сценарий KaroX 5

Стабильный релиз определяется одним законченным путём:

1. Установить KaroX и запустить `karox` внутри Git-репозитория.
2. Выбрать язык и профиль доступа.
3. Подключить ChatGPT Web, Claude Web или API-провайдера.
4. Поручить агенту ограниченное изменение репозитория.
5. Разрешить конкретную команду проверки.
6. Получить результаты проверки, Git status, Git diff и evidence report.
7. Перезапустить KaroX и продолжить сессию без повторного применения уже
   выполненной мутации.

Функции, которые не нужны для этого пути, остаются Preview, Experimental или
Legacy до появления собственных доказательств.

## Установка preview-версии

Публичные bootstrap-команды ветки `main` устанавливают последний стабильный
релиз 4.x. Для проверки KaroX 5 переключись на preview-ветку и запусти локальный
установщик из checkout.

Windows:

```powershell
.\install.karox.ps1
```

macOS или Linux:

```bash
./install.karox.sh
```

После установки открой новый терминал внутри нужного репозитория и запусти:

```bash
karox
```

На Windows терминал, открытый до установки, сохраняет старый `PATH`. Открой
новое окно, используй ярлык KaroX или проверь `Get-Command karox -All`.

## Терминальный клиент

Команда `karox` открывает полноэкранный клиент. Обычный текст считается задачей
для агента и не передаётся в `argparse`.

- При первом запуске выбирается русский или английский язык.
- `/connect` настраивает API, hosted client или оба варианта.
- Ключи провайдеров сохраняются в системном keyring.
- `F5` запрашивает список моделей, когда провайдер это поддерживает.
- `F10` выполняет минимальную живую проверку до активации route.
- `Ctrl+B` открывает настройку hosted bridge.
- `/language`, `/help`, `/quit` управляют базовым интерфейсом.

TUI показывает текущий репозиторий, модель, сессию и bridge. Он не обходит Core
policy и использует те же сервисы, что CLI и JSON automation.

## Подключение ChatGPT, Claude и HyperAgent

```bash
karox bridge connect chatgpt-web --repository . --write
karox bridge connect claude-web --repository . --write
karox bridge connect hyperagent-web --repository . --write
```

Без `--write` используется read-only набор инструментов. KaroX создаёт
привязанную к репозиторию сессию, временный approval credential, локальный MCP
endpoint и HTTPS tunnel, затем выводит MCP URL, пароль подтверждения и
инструкции.

Для HyperAgent оставь «Bring my own OAuth app» выключенным: KaroX публикует OAuth
discovery и поддерживает Dynamic Client Registration, поэтому Client ID и Client
Secret вводить вручную не нужно. Полные шаги — в `docs/CONNECTIVITY.md` (раздел
HyperAgent).

Пароль вводится только на странице KaroX, открытой во время OAuth. Не вставляй
его в chat, connector settings, API key field или GitHub issue.

Cloudflare Quick Tunnel меняет URL после перезапуска. Чтобы не вставлять каждый
раз огромную команду, сохрани только несекретную policy подключения:

```bash
karox bridge saved create full-dev --target-profile chatgpt-web --repository . --write --tunnel tailscale --deadline-preset full-suite --language ru
karox bridge connect --saved full-dev
```

Перед добавлением `checks.run` явно укажи `--tool` и
`--verification-command`. Режим Tailscale использует стабильный `.ts.net`
hostname, когда tailnet разрешает Funnel, отказывается заменять чужие routes и
при остановке завершает только свой foreground process. Команды
`karox bridge saved validate full-dev --json` и
`karox bridge connect --saved full-dev --diagnostics-only` показывают effective
tools, причины отключения, verification allowlist, deadline, tunnel, стабильность
URL и срок жизни сессии до публикации. Реальная проверка аккаунта Tailscale пока
остаётся pending по `docs/TAILSCALE_LIVE_RUNBOOK.md`.

Для легальной проверки внешних HTTPS-сервисов создай отдельное подключение
`browser_control` с `--browser-external-https`, allowlist доменов, видимым окном
для takeover и при необходимости безопасной network inspection. Видимый режим
использует отдельный постоянный профиль Chrome и локальное Manifest V3-расширение
KaroX; headless-проверки сохраняют изолированный Playwright backend с pinned
proxy. Ни один режим не возвращает cookies/credentials и не выдаёт запись в
репозиторий. Подробности: [External HTTPS browser](docs/EXTERNAL_BROWSER.md).

## Ellipsis Opus 5 как remote reasoning backend

Экспериментальный режим `karox agent` оставляет выбранный Git-репозиторий только
на ПК пользователя. Ellipsis получает Claude Opus 5, минимальный
`karox-remote`, строгую инструкцию и write-only connection variables, но не
получает Git origin, clone или cloud workspace проекта. Чтение, изменения,
команды, тесты, процессы, браузер, screenshots, Git evidence, checkpoints и
rollback выполняются локальным KaroX. Подробности и live-gate:
[`docs/ELLIPSIS_LOCAL_AGENT.md`](docs/ELLIPSIS_LOCAL_AGENT.md).

## API-модели

Реализованы контракты адаптеров для:

- OpenAI Responses;
- Anthropic Messages;
- Gemini GenerateContent;
- совместимых OpenAI streaming endpoints.

Настройка выполняется через `/connect` или явные CLI-команды provider, model и
credential. Секреты хранятся как непрозрачные ссылки на keyring и не должны
попадать в config, session state, logs, support bundle или MCP descriptors.

KaroX не должен незаметно менять провайдера, privacy boundary или поведение
мутаций. Fallback разрешён только для узко классифицированных ошибок и явно
настроенных routes.

## Профили доступа

Понятные названия UI соответствуют стабильным идентификаторам policy:

- **Observe** (`read_only`) — чтение репозитория и Git state без изменений.
- **Browser** (`browser_control`) — чтение репозитория/Git и отдельный
  session-isolated browser/network policy без записи в репозиторий, process run
  или локального commit.
- **Build** (`workspace_write`) — изменение файлов, явно разрешённые process и
  checks, Git status/diff evidence и выбранные MCP calls. Build не выдаёт
  `git.commit`.
- **Advanced** (`elevated`) — явно добавляет защищённый локальный commit, а также
  browser, desktop-input и network capabilities elevated policy.

Ни один стабильный профиль не выдаёт Git push, package publishing или
authentication commands. Продолжение работы — действие над существующей durable
session, а не отдельный уровень прав. Во время миграции старый UI может ещё
показывать профиль Resume.

## CLI для автоматизации

```text
karox paths | session | credential | provider | model | skill | mcp | bridge
      | pack | target | tool | integration | agent | migrate | doctor
```

Примеры:

```bash
karox --version
karox doctor
karox provider list
karox model list
karox session list
karox bridge list
karox migrate --json
karox migrate --apply --json
```

`karox migrate` по умолчанию выполняет dry-run. Запись migration destination
происходит только с `--apply`.

`karox-vnext` временно остаётся compatibility alias. Новые инструкции и скрипты
должны использовать `karox`.

## Честные статусы интеграций

| Поверхность | Текущие доказательства | Статус продукта |
| --- | --- | --- |
| Core Runtime | unit, integration и benchmark coverage | Release-critical Preview |
| Native agent | локальный HTTP/SSE E2E с evidence verification | Contract tested |
| OpenAI/Anthropic/Gemini adapters | deterministic adapter и transport tests | Contract tested; live pending |
| ChatGPT Web bridge | OAuth/DCR/PKCE и локальный MCP wire E2E | Experimental; live pending |
| Claude Web bridge | OAuth/DCR/PKCE и локальный MCP wire E2E | Experimental; live pending |
| Ellipsis Opus 5 local workspace | repository-free contract и local bridge E2E | Experimental; live pending |
| Generic Streamable HTTP MCP | authenticated local wire E2E | Protocol compatible |
| Notion gateway | regression старого transport | Legacy / Preview |
| PromptQL | локальный contract и mocked outbound tests | Experimental |
| Skills и Packs | строгая валидация и lifecycle tests | Preview |

Интеграция называется **live tested** только после появления датированной записи
в [`docs/conformance/`](docs/conformance/README.md). Локальные fake servers
доказывают контракт KaroX, но не текущее поведение стороннего продукта.

## Безопасность

Каждое shipping-действие проверяется относительно origin identity, capability,
выбранного репозитория, активной сессии, mutation lease/fencing token и
idempotency key.

Дополнительно применяются redaction, path/link confinement, allowlist hosted
tools, отдельные keyring namespaces и жёсткие запреты на Git push и package
publishing. Неудачный check нельзя превратить в подтверждённый успех одним
текстом модели.

Полные правила: [SECURITY.md](SECURITY.md). Диагностика:
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Проверка качества

Основной runner:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

Полный suite содержит 3136 тестов. CI дополнительно проверяет зависимости,
версии, опубликованный test count, release contract, release workflow ordering,
lint, types, coverage, сборку wheel и кроссплатформенную установку.

```bash
python scripts/check_dependencies.py
python scripts/check_versions.py
python scripts/check_test_count.py
python scripts/check_v5_release.py
python scripts/check_release_workflow.py
python -m ruff check src tests scripts
python -m mypy src/karox
```

`python scripts/check_v5_release.py --strict` предназначена для release candidate
и должна падать, пока обязательные live records и external beta не имеют статус
`passed`.

## Каноническая документация

- [Быстрый старт](QUICKSTART.md)
- [Scope KaroX 5](docs/V5_RELEASE_SCOPE.md)
- [Release checklist](docs/RELEASE_CHECKLIST.md)
- [План внешней беты](docs/BETA_TEST_PLAN.md)
- [Live test runbook](docs/LIVE_TEST_RUNBOOK.md)
- [Миграция 4.x → 5](docs/MIGRATION_V4_TO_V5.md)
- [Connectivity](docs/CONNECTIVITY.md)
- [External HTTPS browser](docs/EXTERNAL_BROWSER.md)
- [Ellipsis local agent](docs/ELLIPSIS_LOCAL_AGENT.md)
- [Implementation status](docs/IMPLEMENTATION_STATUS.md)
- [Live conformance records](docs/conformance/README.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Security](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [English README](README.md)
