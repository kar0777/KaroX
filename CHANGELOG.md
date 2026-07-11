# Changelog

## 3.11.0

### Notion Custom Agent provider

- Added Notion as a first-class KaroX AI target over protected Streamable HTTP MCP.
- Added preflight, context, file, command, Git review, safe commit, and completion-report tools.
- Added `karox notion` setup, diagnostics, status, and documentation commands.
- Pinned public one-command installers to the stable `v3.11.0` tag.
- Added automated release archives, checksums, and bilingual release documentation.

## Public GitHub release

- Established `https://github.com/kar0777/KaroX` as the canonical public repository.
- Rebuilt the bilingual GitHub landing page with a concise product story, platform badges, safety model, and direct documentation routes.
- Updated Windows and macOS/Linux one-command installers to download from `kar0777/KaroX` on `main`.
- Fixed uninstall and troubleshooting paths to match the compatibility-preserving `RepoPilotBridge` runtime used by the installers.
- Kept `karox` as the primary command and `repopilot` as a compatibility alias.

## Adaptive terminal design system

- Rebuilt the Windows and POSIX Flight Deck around one adaptive 52–88-column visual system.
- Added scannable workspace cards, status badges, section rails, command bars, and consistent progress/success/warning/error/empty states.
- Redesigned repository selection, access profiles, AI target, settings, session controls, AI readiness, and Mission Control in English and Russian.
- Mission Control now translates internal recommendation codes into clear next actions while retaining the raw code for diagnostics.
- Preserved keyboard-first navigation, monochrome readability, security contracts, endpoint compatibility, and the no-push workflow.

## Mission Control and live AI context

- Added read-only `GET /context/brief`: a secret-free operating brief combining session identity, real-task state, permissions, Git cleanliness, detected project context, workflow guardrails, warnings, and a recommended next action.
- Added session action `M` on Windows and POSIX to inspect the same live context before AI handoff.
- Extended readiness action `V` and generated connection prompts to include `/context/brief`.
- Added explicit branch-mismatch, no-real-task, dirty-tree, and read-only guidance without automatic repository mutation.
- Added doctor contract coverage for read-only and active-task briefs, hard guardrails, correct recommendations, and absence of the API key.
- Kept all existing endpoints and security behavior compatible; push remains blocked.

## Bilingual Flight Deck and AI readiness

- Added a first-launch English/Русский selector persisted in `settings.json`.
- Added `G → L` language switching without reinstalling or recreating sessions.
- Localized the primary Windows and POSIX Flight Deck, session, repository, profile, settings, and AI-handoff flows.
- Connection prompts and real-task templates are generated in the selected language.
- Added session action `V` for local AI-readiness checks: `/session`, `/health`, `/git/status`, plus `repoRoot` and branch matching.
- Preserved the OpenAPI, authentication, branch, commit, and no-push security contracts.
- Split the main English and Russian documentation while keeping a bilingual quick start.

## Star For KaroX

- Новый бренд: Star For KaroX; короткое имя продукта — KaroX.
- Добавлена кроссплатформенная анимация полёта и приземления звезды при запуске.
- Добавлена основная команда `karox`; `repopilot` оставлен compatibility alias.
- Переработан полный путь установки, запуска, выбора проекта и подключения к PromptQL.
- Исправлены лишние символы в Bash TUI после ранней генерации интерфейса.
- API identity обновлён до KaroX Local Agent API без изменения endpoints и security contract.



## PromptQL Local Workspace redesign

- Полностью переработан terminal session manager для Windows, macOS и Linux.
- Добавлен самостоятельный визуальный язык PromptQL Local Workspace: workspace-карточки, статусы, иерархия и разделение безопасных/опасных действий.
- PromptQL стал активным рекомендуемым AI-клиентом вместо disabled-state.
- Обновлены onboarding, выбор профиля доступа, репозитория, настройки и карточка сессии.
- Публичная продуктовая идентичность обновлена без изменения OpenAPI compatibility contract RepoPilot Bridge API.
- Штатный doctor успешно проверяет read-only, autopilot, full, auth, Unicode, cleanup и безопасный commit.


## 3.10.1

### letaido.com connect prompt — preflight wording fix

- The letaido.com connect prompt previously said "preflight через api-proxy (он подставит X-API-Key в заголовок)", which misled agents into probing the api-proxy for a URL prefix (`/proxy/`, `/http/`, `/outbound/`, `/<domain>/`, …) before falling back to a direct `curl` to the tunnel. The api-proxy is a transparent egress proxy that injects `X-API-Key` on the wire for allowlisted domains — there is no special path to call it through.
- Rephrased the preflight instruction to make this explicit: agents should issue ordinary HTTP requests to the tunnel endpoints (`https://<tunnel>/session`, `/health`, `/git/status`); the api-proxy intercepts outbound traffic transparently and injects the header, with no proxy URL prefix needed.
- No behavioral change to tunnels, sessions, or other clients; only the letaido.com connect-prompt wording in `build_prompts` / `Build-Prompts` (mirrored in `start.sh` and `start.ps1`).

### Version bump

- Bumped bootstrap links and API version to `3.10.1`.

## 3.10.0

### AI client selection (where to connect)

- Added a "Where do we connect" selection screen shown before the session manager on first launch (or whenever `settings.json` has no `aiClient` field). Three options: `letaido.com`, `prompt.ql.app` (currently inactive), and `Сторонний сервис` (third-party service). The choice is persisted in `settings.json` next to `tunnelProvider` and can be changed later via `G → A` in settings.
- `prompt.ql.app` is shown but marked "(Сейчас не активно)"; selecting it prints "Сервис prompt.ql.app сейчас не активен" and returns to the selection.
- The connect prompt is now native per client instead of a single PromptQL-style text:
  - **letaido.com** — uses letaido's native `request_domain_access` + `request_secret` (header `X-API-Key`) instead of a non-existent "custom API integration / provider id". letaido has a fixed connector catalog, so there is no `repo-tools` provider to register.
  - **prompt.ql.app** — keeps the existing PromptQL-style prompt (custom API integration with `provider id` / `base_url` / `api_docs_url` / header auth). Available for future activation.
  - **Сторонний сервис** — a generic, client-agnostic prompt: `base_url` + OpenAPI at `/openapi.json` + header `X-API-Key` + preflight, with no tool-specific vocabulary.
- `session.json` now records `aiClient`; the session menu and manager header show the AI client label next to the tunnel.
- `task_prompt` remains shared across all clients.

### Version bump

- Bumped bootstrap links and API version to `3.10.0`.

## 3.9.1

- Removed the ocean sunset splash animation (`scripts/ocean.py`) and its integration in `start.sh` / `start.ps1`. The animation rendered poorly in many terminals and detracted from the launch experience. RepoPilot now starts directly into the session manager, as before 3.9.0.
- Bumped bootstrap links and API version to `3.9.1`.

## 3.9.0

Major release: RepoPilot Bridge is now a cross-platform tool with first-class macOS and Linux support, alongside the existing Windows experience.

### Cross-platform core

- `server/repo_tools.py` now auto-detects the OS via `sys.platform` (`IS_WINDOWS` / `IS_MACOS`).
- `run_cmd` routes shell commands through `cmd.exe /d /s /c` on Windows and `/bin/sh -c` on POSIX systems.
- The autopilot metacharacter guard is now platform-aware: Windows blocks `[&|;^<>%\r\n]`, POSIX blocks `[&|;<>\`$()\r\n]` (backticks and command substitution, without false positives on `%` and `^`).
- Expanded `HARD_BLOCK_COMMAND_PATTERNS` and autopilot blocked patterns with macOS/POSIX-dangerous commands: `sudo`, `dd of=/dev/`, `mkfs`, `diskutil eraseDisk/partitionDisk`, `launchctl load/unload/bootout/remove`, `defaults write`, `csrutil`, `nvram`, `pmset`, fork bomb `:(){:|:&};:`, `> /dev/sd*`, `killall` of system processes, recursive `chmod`/`chown` on system paths.
- Added `python3`, `./mvnw`, `cat` to `AUTOPILOT_ALLOWED_PREFIXES`.

### New POSIX shell tooling (parallel to the PowerShell scripts)

- `start.sh` — full interactive session manager (TUI): repo/mode selection, uvicorn + tunnel launch, prompt generation, session management, settings.
- `install.sh` — installer that finds Python 3.10+ (Homebrew on Apple Silicon/Intel, python.org, system `python3`), creates a venv in `~/.local/share/RepoPilotBridge`, installs the `repopilot` shim in `~/.local/bin`, and creates a desktop shortcut (`.command` on macOS, `.desktop` on Linux).
- `doctor.sh` — diagnostic harness that exercises every endpoint in read_only/autopilot/full modes, including the new POSIX command-block checks (`sudo`, `dd`, `launchctl`, `&&`/`;` metacharacters).
- `uninstall.sh` — removes runtime, config, shim, desktop shortcut, and PATH markers.
- `bootstrap.sh` — one-shot `curl | bash` installer that downloads the GitHub archive and runs `install.sh --start`.
- Cross-platform process management: tracks PIDs via `$!` and falls back to `pgrep`, `lsof`, `fuser`, or `taskkill`/`netstat` (Windows Git Bash) to free ports between test runs.
- UTF-8-safe HTTP in `doctor.sh` via `curl --data-binary @tmpfile` (avoids `--data` mangling non-ASCII on some platforms).

### Ocean sunset splash animation

- New `scripts/ocean.py`: a terminal animation of a sea with a sunset, tide, and waves, shown on launch.
- Auto-detects color support: 24-bit truecolor, 256-color, 16-color, and monochrome fallbacks.
- Enables VT processing on Windows 10+ via `ctypes` so ANSI sequences render in `cmd.exe`.
- Integrated into `start.sh` and `start.ps1`; skippable via `REPOPILOT_NO_SPLASH=1`.
- Modes: `--long`, `--loop`, `--no-banner`, `--test`.

### Smoke testing

- `Dockerfile.smoke` + `scripts/smoke-test.sh`: a Linux container that runs `doctor.sh` to validate the POSIX `run_cmd` path and blocklists without needing a real Mac.

### Hardening

- `start.sh` and `doctor.sh` now check for `curl` and `git` up front, with install hints for macOS/Linux.
- No bash 4+ features used (`mapfile`, `${var,,}`, `declare -A`) — compatible with macOS default bash 3.2.
- `mktemp` calls have `2>/dev/null || mktemp` fallbacks for GNU/BSD differences.

### Documentation

- `README.md`, `QUICKSTART.md`, `TROUBLESHOOTING.md` now cover Windows, macOS, and Linux side by side: Homebrew setup, `pbcopy`/`open`, Apple Silicon vs Intel Homebrew paths, `.command` shortcut Gatekeeper notes, XDG data paths.
- `.editorconfig` adds `[*.{sh,command}]` rules (LF, UTF-8 without BOM).

- Bumped bootstrap links and API version to `3.9.0`.

## 3.8.13

- Tailscale Funnel provider IDs now include the RepoPilot session id, so a stable `*.ts.net` host does not reuse an old PromptQL provider with stale or empty credentials.
- Connection prompts now explicitly say that the listed provider id is unique for the session and old providers with the same base URL must not be reused.
- Bumped bootstrap links and API version to `3.8.13`.

## 3.8.12

- OpenAPI now publishes a standard `RepoPilotApiKey` security scheme for `X-API-Key` instead of exposing the key as a plain repeated header parameter.
- RepoPilot accepts the session key through `X-API-Key` and compatible `Authorization: Bearer` headers, with whitespace/prefix normalization.
- Auth failures now log safe diagnostics such as whether a credential was supplied and its length, without logging the key.
- Connection prompts now explicitly warn that PromptQL's protected credential card must receive the `K = X-API-Key` value, not the provider id.
- Doctor now verifies OpenAPI auth metadata and both supported auth header styles.
- Bumped bootstrap links and API version to `3.8.12`.

## 3.8.11

- Tailscale Funnel setup now detects the `Funnel is not enabled on your tailnet` response immediately instead of waiting for the full timeout.
- RepoPilot extracts the Tailscale Funnel enable URL, copies it to the clipboard, and opens it in the browser when approval is required.
- Updated Tailscale setup docs to explain the in-CLI install/login flow and browser approval step.
- Bumped bootstrap links and API version to `3.8.11`.

## 3.8.10

- Added `tailscale up` login/start flow directly in RepoPilot settings and session startup.
- Tailscale Funnel now starts with `--yes` and an explicit `http://127.0.0.1:<port>` target to avoid hidden interactive prompts.
- Tailscale URL wait is shorter and now returns an actionable hint instead of looking stuck.
- Bumped bootstrap links and API version to `3.8.10`.

## 3.8.9

- Added one-click Tailscale installation from the RepoPilot settings screen via `winget`.
- Selecting Tailscale Funnel now offers installation automatically when `tailscale.exe` is missing.
- Settings now show `I = установить Tailscale через winget` when Tailscale is selected but not installed.
- Bumped bootstrap links and API version to `3.8.9`.

## 3.8.8

- Added session history cleanup to the CLI manager: stopped gray sessions can be removed individually from the session card or in bulk from the main menu.
- Stopped history deletion is guarded by live PID checks and only removes directories inside RepoPilot's sessions folder.
- Bumped bootstrap links and API version to `3.8.8`.

## 3.8.7

- Added tunnel provider settings in the CLI manager: Cloudflare Tunnel remains the default, and Tailscale Funnel can be selected via `G = настройки`.
- New sessions save `tunnelProvider` in `session.json`, show the selected provider in the manager, and generate provider-specific logs and prompts.
- Tailscale mode starts `tailscale funnel <port>`, waits for a public `*.ts.net` URL, and checks that Tailscale is installed and logged in before starting a session.
- Documented that Tailscale integration uses public Tailscale Funnel, not private Serve, and requires a configured tailnet with Funnel enabled.

## 3.8.6

- Убран runtime `run-server.ps1` из запуска локального API: кириллический путь проекта больше не сериализуется в промежуточный PowerShell-файл и не может превратиться в `D:\Ð¿Ñ...`.
- Локальный API стартует напрямую через `Start-Process python` с найденным `ServerDir`, `PYTHONPATH` и `uvicorn --app-dir`.
- `REPO_ROOT` и текстовые поля сессии дополнительно передаются в сервер через UTF-8 base64 env-переменные, чтобы Windows PowerShell не портил Unicode при передаче окружения.
- Проверка `/health` и `/session` в лаунчере теперь декодирует JSON из raw stream строго как UTF-8, поэтому `repoRoot` больше не превращается в mojibake при самопроверке сессии.
- PID сессии теперь снова указывает прямо на настоящий процесс `uvicorn`, без runner-прослойки.

## 3.8.5

- Лаунчер теперь сам находит реальную папку с `repo_tools.py`: поддерживаются layout `app\server`, `app\server\server` и запуск прямо из корня проекта.
- Runtime runner дополнительно выставляет `PYTHONPATH` на найденную папку сервера, чтобы импорт `repo_tools` не зависел от поведения `Start-Process -WorkingDirectory`.
- Установщик после копирования файлов проверяет, что серверный модуль действительно найден, и показывает путь `Server module`.
- Это устраняет ошибку `Could not import module "repo_tools"` в установках, где архив/копирование дали лишний уровень вложенности `server\server`.

## 3.8.4

- Исправлена кодировка runtime runner-файла `run-server.ps1`: он теперь записывается как UTF-8 with BOM, поэтому Windows PowerShell не ломает кириллические пути вроде `D:\проекты\...`.
- Это устраняет ошибку вида `Cannot find path 'D:\Ð¿Ñ...'` при запуске проекта из папки с русскими символами.

## 3.8.3

- Исправлен запуск скрытого локального API: сервер теперь стартует через per-session runner с явной рабочей папкой `server`, поэтому `uvicorn` стабильно импортирует `repo_tools`.
- Если локальный API падает до готовности, менеджер больше не выглядит зависшим: он сразу показывает хвост `uvicorn`/runner-логов с реальной причиной ошибки.
- Сессия сохраняет PID настоящего `uvicorn` и PID runner-процесса, поэтому остановка выбранной сессии корректно прибирает оба фоновых процесса.

## 3.8.2

- Новая сессия теперь проходит самопроверку изоляции через собственные `/health` и `/session`: RepoPilot сверяет `repoRoot`, ветку и режим до выдачи данных подключения.
- Prompt подключения стал строже для CLI: нужно создавать отдельную integration под текущий provider id и не обновлять интеграцию другой активной сессии.
- При попытке запустить вторую writable-сессию на тот же самый путь проекта RepoPilot предупреждает, что для параллельной работы в одном репозитории нужен отдельный clone/worktree, и не переключает ветку до подтверждения.

## 3.8.1

- Исправлено падение менеджера сессий `Cannot overwrite variable pid`, вызванное конфликтом с read-only переменной PowerShell `$PID`.
- После создания новой сессии менеджер сразу открывает её карточку, чтобы можно было сразу скопировать prompt подключения, `X-API-Key`, provider id или весь пакет через `C/T/K/P/A`.

## 3.8.0

- Лаунчер стал единым менеджером сессий: повторный запуск `repopilot` показывает список сессий, позволяет создать второй проект, открыть нужную сессию, копировать prompt/key/provider id, смотреть логи и останавливать выбранную сессию.
- Сервер RepoPilot и Cloudflare tunnel теперь запускаются скрыто в фоне. На экране остаётся один терминал-менеджер, а сессии продолжают работать после выхода из менеджера.
- Для каждой сессии сохраняется `session.json`, prompt-файлы, ключ, provider id и логи в `%LOCALAPPDATA%\RepoPilotBridge\sessions\<session-id>`.

## 3.7.2

- Убраны пугающие длинные команды `copy-*.ps1` из окна туннеля и прекращена генерация этих helper-скриптов. Для копирования теперь показываются только понятные клавиши меню: `C`, `T`, `K`, `P`, `A`, `L`, `Q`.

## 3.7.1

- Исправлен лишний пустой терминал `cloudflared.exe` при запуске туннеля в многосессионном режиме: процесс туннеля теперь стартует скрыто, а управление и копирование остаются в окне туннеля RepoPilot.

## 3.7.0

- Лаунчер больше не останавливает предыдущие RepoPilot-сессии перед новым запуском: каждая сессия получает свой localhost-порт, папку runtime, логи, runs и Cloudflare tunnel.
- Окно туннеля стало управляющим окном с копированием prompt подключения, шаблона задачи, `X-API-Key`, provider id и полного пакета данных текущей сессии.
- Добавлены per-session helper-скрипты `copy-connect.ps1`, `copy-task.ps1`, `copy-key.ps1`, `copy-provider.ps1` и `copy-all.ps1` в `%LOCALAPPDATA%\RepoPilotBridge\sessions\<session-id>`.
- Режим остановки теперь явно останавливает все серверы, туннели и управляющие окна RepoPilot.

## 3.6.9

- Закрыт обход autopilot-белого списка через chaining-метасимволы cmd (`&&`, `||`, `|`, `;`, `^`, `<`, `>`, `%`): команды вида `npm run build && del ...` больше не проходят.
- `/git/commit` теперь возвращает `unstagedByCommit` — список файлов, которые были застейджены вручную и сняты перед коммитом (раньше `git reset --` происходил молча).
- Фильтр чувствительных фрагментов в командах (`password`, `.env`, `id_rsa` и т.п.) матчит по границам токена — меньше ложных срабатываний (`check_password_policy.py`, `build-environment` больше не блокируются).
- Pre-commit проверка конфигурируется через env `REPO_TOOLS_PRECHECK_CMD` (по умолчанию прежнее `npm run compile && npm test`).
- Сканер секретов больше не срабатывает на собственные маркеры в `repo_tools.py` (маркеры собираются конкатенацией строк).
- Глобальный exception handler: любая необработанная ошибка возвращает структурированный JSON (`ok`, `detail`, `hint`), пишется в audit и не влияет на дальнейшую работу сервера.
- `/run` возвращает структурированный результат вместо HTTP 500, если команда не найдена (`exitCode: 127`) или не запускается (`exitCode: 126`).
- `POST /file` и `DELETE /file` возвращают понятный HTTP 409, если файл занят другим процессом.
- `/files/batch-write` валидирует все пути до записи и возвращает пофайловые ошибки, не оставляя частичную запись при отказе валидации.

## 3.6.8

- API запрещает создавать и запускать служебные helper-скрипты для agent orchestration, commit/check/push, например `commit2.py` и `push_and_check.py`.
- Такие helper-файлы также считаются generated и не могут попасть в `/git/commit`.
- Prompt подключения и шаблон задачи явно запрещают helper-скрипты и направляют агента к `/run`, `/git/cleanup-generated` и `/git/commit`.

## 3.6.7

- Установщик больше не падает, когда уже найденный `cloudflared.exe` лежит в `%LOCALAPPDATA%\RepoPilotBridge\bin` и совпадает с целевым файлом.
- Повторный запуск bootstrap/install теперь корректно переиспользует установленный `cloudflared.exe`.
- PowerShell-скрипты выставляют UTF-8 console codepage, чтобы русский текст не превращался в нечитаемые символы.

## 3.6.6

- Лаунчер теперь спрашивает `Название сессии`, а не `Название задачи`: это только метка для истории и возврата к запуску.
- API `/health` и `/session` явно отдают `sessionTitle` и `taskNote`, чтобы AI не принимал название сессии за реальное ТЗ.
- Prompt подключения просит дождаться отдельного сообщения с задачей и не начинать работу по названию сессии.
- Prompt задачи стал шаблоном: пользователь вставляет настоящее ТЗ в отдельный блок перед отправкой AI.

## 3.6.5

- Лаунчер явно показывает tunnel-specific `provider id` рядом с URL туннеля.
- В меню копирования добавлен пункт `P` для копирования provider id.
- Prompt подключения объясняет, что ошибка `Allowed base URLs` означает старый provider/credential и требует новый provider id из текущего запуска.

## 3.6.4

- Prompt подключения теперь генерирует уникальный `provider id` на основе текущего trycloudflare host.
- Это обходит stale credential binding в PromptQL, когда старый ключ привязан к прошлому tunnel URL и прокси отклоняет новый host.

## 3.6.3

- Лаунчер принимает прямой путь к обычной папке проекта.
- Если в выбранной папке нет `.git`, RepoPilot предлагает выполнить `git init` и создаёт empty commit, чтобы diff, ветки и `/git/commit` работали без ручной подготовки.

## 3.6.2

- Команды установки в документации переведены на release tag, чтобы не зависеть от кэша `raw.githubusercontent.com/main`.
- `bootstrap-clean.ps1` теперь подтягивает `bootstrap.ps1` из release tag и устойчиво очищает BOM перед запуском вложенного скрипта.

## 3.6.1

- `bootstrap.ps1` и `bootstrap-clean.ps1` сохранены без UTF-8 BOM для запуска через `irm ... | iex`.
- `bootstrap-clean.ps1` очищает BOM у вложенного `bootstrap.ps1` перед созданием scriptblock.

## 3.6.0

- Русифицирован пользовательский путь: установщик, лаунчер, doctor, документация и примеры.
- Установщик переписан без дублирующихся блоков и создаёт глобальную команду `repopilot`.
- Лаунчер ждёт живой локальный API перед запуском Cloudflare Tunnel.
- Установщик поддерживает `-Start`: установка и запуск одной командой.
- Добавлены `bootstrap.ps1` и `bootstrap-clean.ps1` для установки/чистой переустановки напрямую с GitHub одной командой.
- Добавлен корневой endpoint `/` с понятным статусом RepoPilot Bridge.
- Обновлены OpenAPI-название и описание.
- Добавлен режим `doctor.ps1 -NoPause` для автоматических проверок.
- Зависимости Python закреплены совместимыми диапазонами версий.
- Добавлены `CHANGELOG.md` и `CONTRIBUTING.md`.

## 3.5.0

- Добавлены task sessions, audit logs, task reports, safe `/git/commit`, очистка generated-файлов и расширенные endpoints для чтения/поиска файлов.
