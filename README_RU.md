# KaroX 5

<div align="center">

**Локальный control plane для автономной AI-разработки — один runtime, много агентов, ваш репозиторий.**

![Status](https://img.shields.io/badge/status-beta-f59e0b)
![CI](https://github.com/kar0777/KaroX/actions/workflows/ci.yml/badge.svg?branch=main)
![Product quality](https://github.com/kar0777/KaroX/actions/workflows/quality.yml/badge.svg?branch=main)
![Release](https://img.shields.io/github/v/release/kar0777/KaroX?include_prereleases&label=release)
![Runtime](https://img.shields.io/badge/runtime-5.0.0rc4-2563eb)
![Python](https://img.shields.io/badge/python-%3E%3D3.10-3776ab)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-64748b)
![Protocol](https://img.shields.io/badge/protocol-MCP-7c3aed)
[![Supporters](https://img.shields.io/badge/поддержавшие_проект-33-ec4899)](SUPPORTERS_RU.md)

Запускайте AI-агентов на локальных Git-репозиториях, не отдавая одному
провайдеру контроль над разрешениями, сессиями и доказательствами работы.

**KaroX 5 beta теперь основная линия `main` · `5.0.0rc4`**

### 🤝 Проект поддержали

<table>
<tr>
<td align="center"><a href="https://routing.run"><img src="https://www.google.com/s2/favicons?domain=routing.run&sz=128" width="38" height="38" alt="routing.run"><br><sub><b>routing.run</b></sub></a></td>
<td align="center"><a href="https://vivgrid.com"><img src="https://www.google.com/s2/favicons?domain=vivgrid.com&sz=128" width="38" height="38" alt="Vivgrid"><br><sub><b>Vivgrid</b></sub></a></td>
<td align="center"><a href="https://puter.com"><img src="https://www.google.com/s2/favicons?domain=puter.com&sz=128" width="38" height="38" alt="Puter"><br><sub><b>Puter</b></sub></a></td>
<td align="center"><a href="https://www.verda.com"><img src="https://www.google.com/s2/favicons?domain=verda.com&sz=128" width="38" height="38" alt="Verda"><br><sub><b>Verda</b></sub></a></td>
<td align="center"><a href="https://tinfoil.sh"><img src="https://www.google.com/s2/favicons?domain=tinfoil.sh&sz=128" width="38" height="38" alt="Tinfoil"><br><sub><b>Tinfoil</b></sub></a></td>
<td align="center"><a href="https://www.stepfun.com"><img src="https://www.google.com/s2/favicons?domain=stepfun.com&sz=128" width="38" height="38" alt="StepFun"><br><sub><b>StepFun</b></sub></a></td>
</tr>
<tr>
<td align="center"><a href="https://browser-use.com"><img src="https://www.google.com/s2/favicons?domain=browser-use.com&sz=128" width="38" height="38" alt="Browser Use"><br><sub><b>Browser Use</b></sub></a></td>
<td align="center"><a href="https://tavily.com"><img src="https://www.google.com/s2/favicons?domain=tavily.com&sz=128" width="38" height="38" alt="Tavily"><br><sub><b>Tavily</b></sub></a></td>
<td align="center"><a href="https://sentry.io"><img src="https://www.google.com/s2/favicons?domain=sentry.io&sz=128" width="38" height="38" alt="Sentry"><br><sub><b>Sentry</b></sub></a></td>
<td align="center"><a href="https://wandb.ai"><img src="https://www.google.com/s2/favicons?domain=wandb.ai&sz=128" width="38" height="38" alt="W&B"><br><sub><b>W&amp;B</b></sub></a></td>
<td align="center"><a href="https://openrouter.ai"><img src="https://www.google.com/s2/favicons?domain=openrouter.ai&sz=128" width="38" height="38" alt="OpenRouter"><br><sub><b>OpenRouter</b></sub></a></td>
<td align="center"><a href="https://www.blockrun.ai"><img src="https://www.google.com/s2/favicons?domain=blockrun.ai&sz=128" width="38" height="38" alt="BlockRun"><br><sub><b>BlockRun</b></sub></a></td>
</tr>
</table>

**Все 33 поддержавшие проект организации:** [routing.run](https://routing.run) · [Vivgrid](https://vivgrid.com) · [Puter](https://puter.com) · [OmniaKey](https://omniakey.com) · [Browser Use](https://browser-use.com) · [Verda](https://www.verda.com) · [Tinfoil](https://tinfoil.sh) · [fal](https://fal.ai) · [Tavily](https://tavily.com) · [Cohere](https://cohere.com) · [Chutes](https://chutes.ai) · [EmpirioLabs](https://empiriolabs.ai) · [Langfuse](https://langfuse.com) · [AIReiter](https://aireiter.com) · [Scout APM](https://scoutapm.com) · [APIMaster](https://apimaster.ai) · [Merge Gateway](https://gateway.merge.dev) · [OpenRouter](https://openrouter.ai) · [Weights & Biases (W&B)](https://wandb.ai) · [BlockRun](https://www.blockrun.ai) · [UnifyLLM](https://www.unifyllm.com) · [Novita AI](https://novita.ai) · [BazaarLink](https://bazaarlink.ai) · [CostRouter](https://www.costrouter.ai) · [Ellipsis](https://www.ellipsis.dev) · [Advanced Installer](https://www.advancedinstaller.com) · [Bump.sh](https://bump.sh) · [Sentry](https://sentry.io) · [Socket](https://socket.dev) · [RouterPlex](https://routerplex.com) · [LangWatch](https://langwatch.ai) · [LLMTR / Knowhy](https://www.knowhy.ai) · [StepFun](https://www.stepfun.com)

**[Все 33 благодарности — полная стена логотипов и подробности →](SUPPORTERS_RU.md)**

</div>

KaroX — локальный контрольный слой для ChatGPT, Claude, API-моделей и
совместимых MCP-клиентов. Каждое локальное действие проходит через один runtime,
привязанный к выбранному репозиторию: с явными правами, безопасными повторными
операциями, сохраняемыми сессиями, разрешёнными проверками, Git evidence и
фильтрацией секретов.

KaroX не является моделью, IDE или полноценной системной песочницей. Разрешённый
процесс всё ещё выполняется с правами пользователя, запустившего KaroX. Проект
ограничивает полномочия через `repoRoot`, capabilities, allowlist, leases,
idempotency и жёсткие запреты, но не виртуализирует операционную систему.

> **Статус релиза:** packaged runtime имеет версию `5.0.0rc4`. Локальные
> контракты и transport-paths покрыты тестами, но реальные live-проверки ChatGPT
> Web, Claude Web и обязательных API-провайдеров ещё не завершены. Смотри
> [scope KaroX 5.0](docs/V5_RELEASE_SCOPE.md),
> [release checklist](docs/RELEASE_CHECKLIST.md),
> [план внешней беты](docs/BETA_TEST_PLAN.md),
> [миграцию 4.x → 5](docs/MIGRATION_V4_TO_V5.md) и
> [live conformance records](docs/conformance/README.md).

## Почему KaroX

| Что нужно | Как ведёт себя KaroX |
| --- | --- |
| **Автономная разработка без спама подтверждениями** | Чтение, обычные правки, тесты, проверки, dev-команды и локальные коммиты выполняются без лишних approval-циклов. |
| **Одна граница безопасности для всех агентов** | ChatGPT, Claude, API-модели, MCP-клиенты и локальные workers работают через один repository-scoped Core. |
| **Редкие и осмысленные подтверждения** | В Protected-режиме подтверждение нужно для разрушительного удаления исходников и реальных внешних commit points; Bypass разрешает автономное удаление только внутри репозитория. |
| **Один gate не останавливает всю работу** | Опасное действие откладывается, а агент продолжает независимые шаги и спрашивает пользователя только когда без решения дальше нельзя. |
| **Кроссплатформенная проверка** | Windows, macOS и Linux CI проверяют одинаковый wheel, тесты, release gates, Git-контракты и secret scan. |

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

## Установка

Кандидат публичной беты — **`v5.0.0rc4`**. Для первой установки рекомендуется
**preview portable bootstrap**: заранее устанавливать Python, pipx или uv не
нужно; права администратора не требуются. Bootstrap проверяет SHA-256 release-
архива, устанавливает его в каталог пользователя и через встроенный `uv`
загружает managed Python и запускает KaroX. Нужен доступ в интернет для архива,
Python и Python-пакетов.

**Windows — вставь в PowerShell:**

```powershell
& ([scriptblock]::Create((Invoke-RestMethod -Uri 'https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1' -TimeoutSec 30))) -Channel preview
```

**macOS / Linux — терминал с Bash и curl:**

```bash
curl --proto '=https' -fsSL --max-time 30 https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.sh | bash -s -- --channel preview
```

Portable-архивы предназначены для Windows, macOS и Linux x64/ARM64. По умолчанию
KaroX сразу запускается; `KAROX_NO_START=1` отключает автозапуск. Предыдущая
portable-установка сохраняется до готовности проверенной замены. Ошибка TLS,
таймаут, несовпадение checksum или небезопасный архив **останавливают установку**,
а не запускают непроверенные исходники. Исправь указанную причину и повтори.
Артефакты выбранного релиза должны быть опубликованы. Канал по умолчанию читает
`RELEASE.json`, канал `preview` — `PREVIEW.json`.

**Windows PATH — без ожидания нового окна:** bootstrap обновляет PATH своего
процесса PowerShell и сохраняет пользовательский PATH. Если установка запущена
дочерним процессом, вставь в **исходное** окно (для стандартного каталога):

```powershell
$env:Path = "$env:LOCALAPPDATA\KaroX;$env:Path"
karox
# Прямой запуск работает и без обновления PATH:
& "$env:LOCALAPPDATA\KaroX\KaroX.cmd"
```

При `KAROX_INSTALL_ROOT` используй точную команду PATH из вывода установщика.
На macOS/Linux, если команда не найдена, выполни
`export PATH="$HOME/.local/bin:$PATH"` или запусти `~/.local/bin/karox` напрямую.
Затем запускай `karox` внутри нужного Git-репозитория;
`karox quickstart` покажет следующий шаг настройки.

При автоматическом выборе туннеля используется установленный Tailscale,
иначе — Cloudflare. Ошибки входа/Funnel в Tailscale не переключают провайдера
незаметно. Явные `--tunnel tailscale`, `--tunnel cloudflare`, `--tunnel custom`
и `--cloudflared PATH` сохраняют свой смысл. Для Cloudflare используется уже
установленный cloudflared либо лениво загружается закреплённая версия с проверкой
checksum в локальный runtime-кеш пользователя. Автозагрузка поддерживает Windows
x64 и macOS/Linux x64/ARM64; в закреплённом upstream-релизе нет Windows ARM64
исполняемого файла. На неподдерживаемой платформе или без сети укажи
`--cloudflared PATH` либо используй Tailscale. Лимит загрузки — 96 MiB, бюджет
передачи — 120 секунд, таймаут отдельного I/O — 15 секунд. На macOS из проверенного
архива копируется только один обычный исполняемый файл. Системные службы туннеля
не устанавливаются. Учётные данные по-прежнему требуют OS keyring; plaintext-
fallback отсутствует.

**Альтернатива: готовое Python-окружение.** После публикации версии в PyPI:

```bash
pipx install "karox-runtime==5.0.0rc4"
# или
uv tool install "karox-runtime==5.0.0rc4" --prerelease=allow
```

**Исходники / разработка:** используй проверенный checkout точного тега и запусти
`./install.karox.sh` или `.\install.karox.ps1`, либо:

```bash
pipx install --force "git+https://github.com/kar0777/KaroX.git@v5.0.0rc4"
```

Если portable-архива нет (HTTP 404) или `KAROX_BOOTSTRAP_REF` указывает не на
релиз, source-fallback bootstrap разрешён только с независимо доверенным
`KAROX_SOURCE_SHA256` исходного архива. На POSIX этот путь требует Python 3.
Не подставляй checksum непроверенной загрузки только для обхода ошибки проверки.

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
привязанную к репозиторию сессию, локальный MCP endpoint и HTTPS tunnel. В консоль
выводятся MCP URL и инструкции, а approval credential остаётся в системном keyring.
Если клиенту нужен OAuth approval password, текущий пароль можно безопасно
скопировать локально командой `karox bridge oauth approval-password --saved NAME --copy`.

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

## Оркестрация интеллекта и экономия

KaroX 5 умеет объединять API-модели, уже оплаченные подписочные агенты, локальные
модели и явно подключённые внешние agents в один **Intelligence Pool**. Пользователь
выбирает одного оркестратора, а роли planner/implementer/tester/reviewer могут
выполняться разными endpoints. Автоматический роутинг учитывает качество только
по локальным результатам KaroX, которые одновременно были accepted и verified.

Экономия строится не на тихом даунгрейде модели, а на shared context, context
delta, стабильном prompt prefix, повторном использовании project map, quota
reserve, already-paid capacity и независимой проверке. Savings Receipt показывает
процент/доллары только при наличии измеренного baseline.

На компьютере можно обнаружить установленные subscription CLIs:

```bash
karox intelligence discover-agents --apply
```

Встроенный adapter Codex допускает implementation только внутри отдельного KaroX
worktree с workspace-write sandbox. Встроенный Claude Code adapter намеренно
read/review-only и использует safe mode с `Read,Glob,Grep`. Gemini CLI и OpenCode
могут отображаться как найденные, но не запускаются автоматически, пока KaroX не
может доказать эквивалентную границу записи.

```bash
karox orchestrate plan --objective "Исправить retry semantics" --recipe bug-fix
karox orchestrate run --objective "Исправить retry semantics" --recipe bug-fix \
  --isolate-implementers \
  --verification-command '["python","-m","pytest","-q"]'
```

Выбранный orchestrator реально участвует в работе: planner по умолчанию закреплён
за ним, а в конце `orchestrator-judge` получает evidence от tester/reviewer и
выносит финальное решение. В TUI доступны `/orchestrate`, `/agents` и `/mission`,
при этом slash-menu ограничен восемью основными командами; advanced-команды не
удалены и продолжают работать при ручном вводе.

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

Ни один стабильный профиль не выдаёт постоянное право на Git push,
package publishing или authentication commands. В Advanced durable ChatGPT Web
bridge может быть доступен отдельный `karox.git.push`, но каждый push требует
машинно-проверяемого одноразового подтверждения пользователя, привязанного к
точным remote/branch; `command.run` не может обойти этот gate. Продолжение работы
— действие над существующей durable session, а не отдельный уровень прав. Во
время миграции старый UI может ещё показывать профиль Resume.

## CLI для автоматизации

```text
karox paths | session | credential | provider | model | intelligence | skill | mcp
      | bridge | orchestrate | mission-control | economy | pack | target | tool
      | integration | agent | migrate | doctor
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
tools, отдельные keyring namespaces, жёсткий запрет произвольного Git push/package
publishing через developer commands и точный одноразовый user gate для отдельной
push-поверхности. Неудачный check нельзя превратить в подтверждённый успех одним
текстом модели.

Полные правила: [SECURITY.md](SECURITY.md). Диагностика:
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Спонсоры и поддержавшие проект

KaroX теперь публично благодарит **33 уникальных поддержавших проекта** за реальную помощь: model/API access, compute, browser capacity, search, observability, security tooling, developer infrastructure, лицензии и research tooling. В расширенный список добавлен StepFun и подтверждённая поддержка из восстановленной outreach/Gmail истории проекта; дубли объединены.

Те же 33 имени входят в sponsor registry KaroX 5, который использует команда `/sponsors`. Полная [стена поддержки](SUPPORTERS_RU.md) содержит логотипы, ссылки, категории поддержки и благодарности. Упоминание — это благодарность, а не утверждение KaroX о безопасности, приватности, ценах или моделях сервиса.

## Проверка качества

CI запускает полный набор pytest, включая отдельные функции и параметризованные
сценарии. Быстрый локальный запуск без пропуска benchmark-проверок:

```bash
python -m pip install --group test
python -m pytest tests -n 6 --dist=loadfile
```

Распределение целых файлов сохраняет порядок тестов и итоговую проверку benchmark.
Число unittest-методов ниже отслеживается отдельно для сравнения с историческими
отчётами; это не количество всех сценариев pytest.

Исторический unittest runner:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

Полный suite содержит 3560 тестов. CI дополнительно проверяет зависимости,
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
