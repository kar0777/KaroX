# KaroX 5 Preview — диагностика

Начинай с команд:

```bash
karox --version
karox paths --json
karox doctor
```

Для checkout preview-ветки также полезно:

```bash
python scripts/check_versions.py
python scripts/check_v5_release.py --json
```

Не публикуй необработанные логи, support bundle, OAuth tokens, API keys, пароль
approval или приватный исходный код.

## Команда `karox` не найдена

### Windows

Терминал, открытый до установки, сохраняет старый `PATH`. Открой новое окно
PowerShell или Terminal. Установщик также создаёт launcher/ярлык, который можно
запустить напрямую.

Проверь, какую команду видит система:

```powershell
Get-Command karox -All
```

Если первой находится старая pip-команда или launcher другой установки, удали
или перемести shadowing entry. Не копируй файлы runtime вручную поверх активной
установки.

### macOS / Linux

Проверь:

```bash
command -v karox
printf '%s\n' "$PATH" | tr ':' '\n'
```

Убедись, что каталог user-local launcher присутствует в `PATH`, затем открой
новую вкладку терминала. Точные каталоги текущей установки показывает
`karox paths --json`.

## Установщик завершился, но запускается старая версия

Сравни:

```bash
karox --version
karox paths --json
```

На Windows используй `Get-Command karox -All`; на POSIX — `type -a karox`.
Старая machine-wide команда может иметь приоритет над новой user-local.
Исправь порядок `PATH` или удали старый launcher. Не меняй VERSION-файлы внутри
установки вручную.

## KaroX не находит `cloudflared`

Проверь:

```bash
cloudflared --version
karox doctor
```

На Windows KaroX ищет `cloudflared.exe` в `PATH`, bundled runtime, WinGet Links,
WindowsApps, WinGet package directories и стандартном каталоге Cloudflare. Если
WinGet установил пакет, но launcher не виден, открой новый терминал и повтори
проверку.

При нестандартной установке выбери явный путь или используй собственный стабильный
HTTPS reverse proxy. Не скачивай бинарник из случайного источника.

## URL Cloudflare Quick Tunnel меняется

Это ожидаемое поведение. Quick Tunnel выдаёт временный URL, который меняется
после перезапуска bridge. Сохранённый connector ChatGPT или Claude перестанет
работать, пока его URL не обновлён.

Для постоянного подключения настрой стабильный HTTPS origin и запускай bridge с
custom tunnel/public URL согласно `karox bridge --help`. Не ослабляй OAuth
resource binding ради старого URL.

## ChatGPT или Claude не завершает OAuth

Проверь по порядку:

1. Bridge всё ещё запущен.
2. Используется свежий MCP URL из текущего запуска.
3. Redirect идёт на страницу KaroX, а не на неизвестный домен.
4. Пароль из строки `OAuth approval password` введён только на странице KaroX.
5. В connector settings пароль не вставлен как client secret.
6. Системное время не сильно отличается от реального.
7. Browser не блокирует redirect или cookies, необходимые самому клиенту.

После изменения public origin старые OAuth registrations и resource-bound grants
могут быть неприменимы. Удали старое подключение и создай его заново с новым URL.

Сохранённый bridge не обязан жить в открытой консоли. Если процесс умер, сам
supervisor поднимет его заново; проверяй и управляешь через отрывные команды:

```bash
karox bridge status --saved chatgpt-dev
karox bridge start --saved chatgpt-dev    # detached relaunch with the same URL
karox bridge restart --saved chatgpt-dev  # old owner waits down, receipt returned
karox bridge stop --saved chatgpt-dev
```

Для диагностики «MCP server does not implement OAuth» сохранённый bridge пишет
только method+path каждого входящего запроса в
`%LOCALAPPDATA%\KaroX\vnext\oauth-bridge\*.request-probe.jsonl` — никаких
заголовков, query-параметров или тел. Если запросы ChatGPT не попадают в файл
вообще, проверь Tailscale Funnel POP (периодический known-external issue),
прежде чем чинить KaroX.

## Страница approval возвращает HTTP 421

Старые preview-сборки могли получать `Origin: null` при POST формы Chromium из-за
слишком строгой Referrer Policy. Обнови KaroX до сборки с исправлением
`same-origin` для собственной approval-формы и повтори подключение.

421 на discovery-запросах (`/.well-known/...`, `/oauth/*`, `/register`) означает,
что приходящий `Host` не совпал с публичным хостом текущей bridge-сессии. KaroX
печатает одну redacted-строку диагностики на каждый такой запрос:

```
[karox-rebind] 421 GET /.well-known/oauth-authorization-server request_id=... host=<...> x-forwarded-host=no ... reason=host_not_allowed
```

Строка показывает метод, путь, нормализованный Host, наличие `X-Forwarded-Host`/
`X-Forwarded-Proto`/`Forwarded`, ожидаемый origin и причину — без заголовка
Authorization, cookie, токенов, пароля approval или содержимого тела регистрации.

Возможные причины:

1. Публичный хост туннеля не передан в bridge. Managed launcher (Tailscale
   Funnel/Cloudflare) передаёт его автоматически; ручной `karox bridge serve`
   должен указать `--public-url` (и при необходимости `--allowed-redirect-hosts`).
2. URL в HyperAgent/ChatGPT/Claude не совпадает с реальным публичным хостом
   туннеля. Скопируй точный MCP URL из текущего окна KaroX.
3. Proxies/TLS-терминаторы переписывают Host. KaroX доверяет `Forwarded`/
   `X-Forwarded-Host` только от loopback-peer (т.е. от локального туннеля),
   поэтому внешний клиент не может подменить Host через эти заголовки.

Не отключай проверку Origin и Host глобально: она защищает от rebinding и должна
оставаться fail-closed.

## HyperAgent: redirect URI mismatch или ошибка DCR

Профиль `hyperagent-web` разрешает redirect только на хост `hyperagent.com`.
Любой другой HTTPS-redirect при регистрации вернёт 400 `invalid_request`.

- Если HyperAgent использует callback на другом домене, это не ошибка KaroX:
  сообщи точный redirect URI и используй `chatgpt-web`/`claude-web` или профиль
  `generic-streamable-http` вместо `hyperagent-web`.
- Wildcard-домены (`https://*.example/cb`), HTTP-redirect для внешнего клиента,
  схемы `javascript:`/`data:`/`file:` и userinfo/fragment в URI отклоняются
  структурно — они никогда не были валидным callback.
- `POST /register` принимает только JSON и ограничивает размер тела (~64 KiB).
- Повторная идентичная регистрация безопасна: тот же redirect + то же имя
  возвращают тот же `client_id`.

Не вставляй внутренний ключ KaroX в поле Client Secret: DCR возвращает public
PKCE-клиент (`token_endpoint_auth_method: none`), и Client Secret не нужен.

## Пароль approval не принимается

Скопируй точное значение из текущего окна KaroX. Оно относится к текущему bridge
запуску и не является API key или OAuth client secret.

Не вводи его:

- в чат;
- в поле API key провайдера;
- в app configuration;
- в GitHub issue;
- в conformance record.

Если окно KaroX было перезапущено, используй новый пароль.

## HTTP 401 или MCP access denied

Возможные причины:

- отсутствует или устарел bearer/access token;
- bridge credential был rotated или revoked;
- клиент использует старый public resource URL;
- refresh-token family отозвана после replay;
- OS keyring недоступен и credential не разрешается.

Не вставляй credential в URL или обычный config. Выполни `karox doctor`, затем
переподключи клиент или безопасно пересоздай credential через соответствующую
CLI-команду.

## HTTP 403 или tool отсутствует

Это обычно политика, а не transport error:

- выбран Observe;
- tool не добавлен в bridge allowlist;
- внешний MCP server или tool не выбран для session;
- capability имеет решение `deny` или ещё не имеет явного `allow`;
- путь чувствительный или выходит за `repoRoot`;
- запрос пытается выполнить push, publish или запрещённую команду;
- schema/identity внешнего MCP tool изменилась после выдачи grant.

Не переключайся сразу в Advanced. Сначала проверь session, profile, selected tools
и минимально необходимую capability.

## `SessionBusy` или отказ mutation lease

Другой процесс уже владеет правом мутации этой session. Это защита от
одновременной записи.

- найди активный процесс KaroX;
- не запускай второй Build-agent на той же session;
- дождись завершения или корректно останови владельца;
- проверь lock state через session CLI;
- emergency revoke используй только после понимания последствий.

Не удаляй lock/state files вручную: stale holder должен быть fenced штатным
механизмом.

## Mutation завершилась с unknown outcome

Transport мог оборваться после отправки mutating call, поэтому KaroX не должен
притворяться, что операция точно не выполнилась.

1. Не повторяй запрос с новым idempotency key сразу.
2. Проверь файл, Git status, Git diff и evidence.
3. Повтори исходный call с тем же idempotency key, когда это поддерживается.
4. Создай новый key только после подтверждения фактического состояния.

## Агент пишет «готово», но session не verified

Для verified результата после изменения нужны:

- реальное изменение файла;
- успешный явно разрешённый check;
- Git status evidence;
- Git diff evidence.

Failed, timed-out или отсутствующий check не считается успехом. Исправь ошибку и
запусти разрешённую проверку заново. Не меняй evidence state вручную.

## Проверка или build выполняет неожиданную команду

KaroX не является OS sandbox. Approved test/build может запустить scripts из
`package.json`, Makefile, Gradle, compiler plugin или другого build config.

Останови процесс, перейди в Observe, изучи build files и выполняй неизвестный
проект в отдельной VM/container или под отдельным системным пользователем.

## Provider key не сохраняется или не читается

KaroX 5 требует работающий OS keyring и не должен падать обратно на plaintext.
Запусти credential/keyring doctor через доступные команды `karox credential
--help` и `karox doctor`.

В headless Linux может отсутствовать настроенный secret-service backend. Настрой
системный keyring или используй подходящую защищённую среду. Не обходи проблему
записью ключа в repository config.

## Model test проходит локально, но реальный provider не работает

Deterministic contract tests не доказывают текущую совместимость конкретного
провайдера. Проверь:

- точный endpoint и protocol adapter;
- model ID;
- account permissions и billing;
- context/output limits;
- streaming и tool-call support;
- provider-specific headers;
- timeout и rate-limit response.

Запиши успешный или неуспешный живой прогон в соответствующий файл
`docs/conformance/`, предварительно удалив secrets и private payloads.

## Миграция 4.x → 5 не проходит

Сначала используй dry-run и JSON report. Не удаляй старую установку, repository,
`.git` или весь runtime directory.

Проверь категории `migrated`, `skipped`, `unsupported`, `requires secret
re-entry` и `failed`. Environment-only secrets должны быть введены заново через
keyring; они не копируются в config.

Полный контракт: [`docs/MIGRATION_V4_TO_V5.md`](docs/MIGRATION_V4_TO_V5.md).

## Обновление прервалось

Updater должен использовать staged install и rollback. Не запускай несколько
обновлений одновременно и не заменяй файлы активного runtime вручную.

Запусти doctor предыдущего launcher, проверь release/update logs и используй
штатный rollback. Если rollback не работает, сохрани sanitized diagnostics до
переустановки.

## Release gate показывает pending records

Для `5.0.0rc1` это ожидаемо:

```bash
python scripts/check_v5_release.py --json
```

Обычный режим проверяет структуру репозитория, но разрешает pending live records
на development version. Режим:

```bash
python scripts/check_v5_release.py --strict
```

предназначен для release-candidate rehearsal и должен падать, пока ChatGPT,
Claude и обязательные provider records не имеют статус `passed`.

## Как собирать диагностику безопасно

Перед отправкой отчёта:

- удали API keys, tokens, cookies, approval passwords и private URLs;
- не прикладывай исходный код без необходимости и разрешения;
- не публикуй OS keyring dumps;
- укажи KaroX version/commit, OS, Python, client, access profile и минимальный
  reproduction в disposable repository;
- сначала отзови credential, который мог быть раскрыт.

Правила disclosure: [SECURITY.md](SECURITY.md).
