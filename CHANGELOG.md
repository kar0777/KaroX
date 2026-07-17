# Changelog

## 4.1.0

### KaroX v4.1.0 — Out-of-the-box launch + console redesign

- Zero-setup launch: `start.ps1` runs `scripts/karox_autodeps.py`, which auto-installs the optional playwright/pillow/mss packages and headless Chromium on the first start after an install or update (cached per version; opt out with `KAROX_AUTO_DEPS=0`).
- Watchdog by default: the local API server is started under `scripts/karox_supervisor.py` automatically, so crashes and hangs recover without any manual commands (opt out with `KAROX_NO_WATCHDOG=1`).
- Supervisor v2: new `--pid-file` exports the real server PID after every (re)start; the API key is read from `REPO_TOOLS_API_KEY`/`KAROX_API_KEY` environment variables instead of the command line; guaranteed child-tree cleanup on exit.
- Tree-safe shutdown: `Stop-Session`/`Stop-AllSessions` also terminate the supervised uvicorn child via the session `server.pid`, and `Stop-Old` matches supervisor processes.
- Console redesign: new shared UI kit `scripts/karox_ui.py` (star banner, sections, key-value rows, ✓/!/× status marks, automatic color detection honoring NO_COLOR/KAROX_NO_COLOR); `karox status`, `karox doctor`, `karox update --check`, and the update notice share the new look; the PowerShell launcher shows the installed version in the intro and header.
- Linux/macOS note: auto-deps and the default watchdog wrap are wired into the Windows launcher in this release; on bash run `scripts/karox_autodeps.py` and `scripts/karox_supervisor.py` manually for now.

## 4.0.0

### KaroX v4.0.0 — Universal agent development engine

- Supervisor + watchdog (`scripts/karox_supervisor.py`): 5-second heartbeats and full process-tree restart in under 3 seconds after two failed pings; a JSONL log records every restart.
- Persistent sessions: branch, task, and session permissions are stored on disk and restored via `karox_session(action="resume")` instead of a fresh `promptql/full-<timestamp>` branch after every restart.
- Idempotency keys on mutating requests (exec, write, patch, fs ops, commit-hunks): a replay after a dropped connection does not duplicate the mutation.
- Priority request queue with per-request timeouts answers “busy, queue position N” (503 + `retryable: true`) instead of connection refused under CPU load.
- `karox_exec` accepts a verbatim argv array — no shell wrapper, quotes survive untouched; optional `cmd`/`powershell`/`bash` shells force UTF-8 (`chcp 65001`), and legacy cp866/cp1251 output is normalized.
- Async jobs: `karox_job` start/status (CPU/RAM)/tail with follow-until-pattern/signal (kill|int)/list, plus `karox_wait_for` port/http readiness probes.
- `karox_checks_v2`: command matrix with allow-failure, flaky retries, a summary report, and structured error extraction (javac, kotlin, tsc, eslint, pytest, gradle, gcc) with the first error as a separate field.
- Binary file IO (`karox_bytes`) and `karox_read_image` returning MCP image content — the agent sees screenshots, textures, and favicons itself.
- Guarded file operations (`karox_fs`: move/copy/mkdir/opt-in delete_dir/glob), whole unified-diff `karox_apply_patch`, and instant working-tree checkpoints (`karox_checkpoint` create/restore) without commits.
- `karox_search_v2`: regex, filename search, size/extension limits, and context lines.
- Full local git cycle (`karox_git2`): branches, stash, filtered log, show, blame, restore, revision diffs, local merge/rebase with a conflict report and auto-abort, and partial commits by hunk. `git push` stays hard-blocked.
- Secret-scan v2 on write and commit: token regexes plus Shannon entropy, blocking with exact line numbers.
- Managed dev-servers (`karox_devserver`) with automatic port detection, localhost-only `karox_http_fetch`, and a Playwright headless browser (`karox_browser`: screenshot/dom/console/click/type).
- Allowlisted package managers (`karox_pkg`) with lockfile-diff reporting; publish/auth commands are hard-blocked like push.
- Desktop eyes and hands (`karox_screen`): window/region screenshots, short GIF recording, and input that is strictly opt-in per session and confined to the target window.
- Event notifications (`karox_events`): job exits, dev-server readiness, and failed checks as poll-able events.
- Persistent REPL sessions (`karox_repl` for python/node) and a generic DAP debug bridge (`karox_dap`).
- Project map (`karox_project_map`), per-repo persistent memos (`karox_memo`), and an opt-in multi-repo workspace switch (`karox_workspace`).
- MCP tool calls convert transport failures into structured `retryable: true` results instead of opaque connection errors.
- Security is not weakened in any phase: all new endpoints require the API key, operate inside the repo sandbox, honor the existing hard-blocks, and are fully covered by the audit log.

## 3.16.2

### KaroX v3.16.2 — Process-safe updates and stronger agent workflows

- Windows updates now stop verified live KaroX server, tunnel, and runner processes before replacing application files.
- The updater reads recorded session PIDs, validates their current command lines, and refuses to kill stale or unrelated reused PIDs.
- Orphaned KaroX Uvicorn and tunnel processes are discovered safely while the updater's own ancestor process chain is excluded.
- Installation retries up to four times after transient Windows directory-lock failures instead of aborting immediately on `app\server`.
- The public `karox stop [--session ID] [--json]` command can terminate session processes without loading the larger administrative runtime.
- `karox_list_dir` lists a focused directory without loading the entire repository tree.
- `karox_search` searches source content and returns compact matching snippets.
- `karox_read_file_range` provides bounded, line-numbered context for large files.
- `karox_read_files` batches several context reads into one tool call.
- `karox_apply_edits` performs exact replacements and refuses stale or ambiguous edits before writing.
- `karox_run_checks` runs an ordered build/test/lint checklist and summarizes each result.
- Agent tools live in a separate module so capabilities can evolve without destabilizing the authenticated Streamable HTTP transport.
- Product quality passed on Windows, macOS, and Linux with Python 3.10 and 3.12.
- Provider and release contracts passed.

## 3.16.1

### KaroX v3.16.1 — Reliable Notion MCP transport

- fixes Notion errors such as `SSE error: MCP fetch request failed` and `Failed to connect to MCP server`;
- replaces Starlette `BaseHTTPMiddleware`, which could close FastMCP Streamable HTTP channels, with a raw ASGI authentication and Host-validation gateway;
- switches the provider to stateless Streamable HTTP with JSON responses;
- accepts both `/mcp` and `/mcp/` without authentication-losing redirects;
- returns explicit transport errors instead of opaque connection failures;
- converts local KaroX API failures into structured MCP tool results;
- adds `karox_ping` for immediate transport and health diagnosis;
- bounds excessive `karox_run` timeout and output-tail values.
- `karox notion doctor` now runs a real MCP `initialize` → `tools/list` → `tools/call` handshake;
- CI runs the same handshake on Windows, macOS, and Linux with Python 3.10 and 3.12;
- the product doctor verifies the raw ASGI gateway, stateless JSON mode, required files, and dependency versions;
- the MCP SDK requirement is raised to the maintained v1 line used by the tested transport.

## 3.16.0

### KaroX v3.16.0 — Native Notion Provider

- native Notion provider choice in KaroX;
- automatic Tailscale startup/readiness flow;
- automatic stable Funnel selection for Notion;
- persistent MCP URL and Bearer credential;
- Russian and English provider UI;
- LIVE Notion connection card;
- reusable `N = Notion` action in the session menu;
- Windows PowerShell 5.1, macOS, Linux, Python 3.10 and 3.12 coverage.

## 3.15.1

### KaroX v3.15.1 — Windows updater directory-lock fix

- `karox update` no longer leaves the launcher working directory inside `%LOCALAPPDATA%\KaroX\app`.
- The Windows installer moves out of the installed app directory before replacing application files.
- The generated `karox.ps1` launcher preserves the user's current directory.
- Updating from any ordinary PowerShell directory no longer fails with `Cannot remove ... KaroX\app because it is in use`.
- The localized Notion setup wizard from v3.15.0 remains included.

## 3.15.0

### KaroX v3.15.0 — Localized Notion setup wizard

- Adds one guided `karox notion setup` flow for Windows, macOS, and Linux.
- Reads the language selected in KaroX and shows Russian or English instructions automatically.
- Explicitly explains that Tailscale must be running and show `Connected`.
- Tries to start the Tailscale service and desktop application automatically.
- Opens or triggers Tailscale sign-in when the device is not connected.
- Waits for the stable `*.ts.net` hostname before continuing.
- Prints exact Notion Custom MCP fields and the complete connection sequence.
- Warns users to place the Bearer key only in Notion's protected Token field, never in chat.
- Reminds users to keep Tailscale and the `karox notion` session running while Notion works with files.
- Routes `setup`, `connection`, `status`, `rotate-key`, and `reset-connection` through the same localized wizard.
- Requires and validates the new wizard in installation diagnostics.

## 3.14.2

### KaroX v3.14.2 — Windows PATH migration fix

- Replaces the legacy RepoPilotBridge PATH entry with the new KaroX bin directory.
- Prepends `%LOCALAPPDATA%\KaroX\bin` to the persistent user PATH.
- Installs a temporary compatibility launcher in the old bin directory so the current PowerShell window immediately forwards `karox` to the new installation.
- Keeps only the compatibility shim while old processes are still active.
- Removes the remaining legacy runtime directory automatically after KaroX is launched from a fresh shell that no longer contains the old PATH entry.
- Preserves settings, sessions, repository history, and the persistent Notion credential.
- Adds regression coverage for the runtime rebrand and Windows command migration.

## 3.14.1

### KaroX v3.14.1 — Doctor rebrand fix

- Fixes a false failure in `karox doctor` after upgrading from RepoPilotBridge to KaroX.
- Preserves the legacy-path detector while installed runtime files are rewritten to the KaroX brand.
- Keeps real stale-path detection: launchers that still point to the old runtime directory continue to fail the doctor check.
- Adds regression coverage for Windows, macOS, Linux, Python 3.10/3.12, and runtime rebranding.
- Does not reset settings, sessions, repository history, or the persistent Notion credential.

## 3.14.0

### KaroX v3.14.0 — Native paths and reliable migration

- Windows installations now use `%LOCALAPPDATA%\KaroX` and `%APPDATA%\KaroX`.
- macOS/Linux installations now use `KaroX` configuration and runtime directories.
- Existing settings, repository history, session metadata, logs, and the DPAPI-protected persistent Notion key are migrated automatically.
- Stored absolute paths are rewritten to the new directories.
- Legacy directories are cleaned after the old processes have exited.
- The installer explicitly repairs and verifies `scripts/tailscale_readiness.py` instead of reporting success with an incomplete application.
- Installed PowerShell, Bash, and Python runtime files are rewritten to KaroX paths before the doctor runs.
- Admin, support bundle, Notion profile, generated launchers, shortcuts, and command shims all resolve the same canonical directories.

## 3.13.2

### KaroX v3.13.2

- `karox notion setup` now waits up to 120 seconds after Tailscale login instead of checking only once;
- transitional states such as `NeedsLogin`, `Starting`, and `Running` are reported clearly;
- KaroX can derive the stable device hostname from `HostName + MagicDNSSuffix` when `Self.DNSName` has not appeared yet;
- the Tailscale authentication URL and backend state are included in actionable errors;
- Windows, macOS, and Linux entrypoints use the same readiness probe;
- stale release metadata caches older than the installed KaroX version are removed automatically on Windows;
- the persistent Notion Bearer key is preserved during upgrade.

## 3.13.1

### KaroX v3.13.1

- `start.ps1` no longer contains a non-ASCII arrow in a BOM-less UTF-8 file;
- Windows PowerShell 5.1 no longer misreads that byte sequence as a smart quote and reports a missing string terminator;
- KaroX now starts normally after installing v3.13.x on Windows;
- the CI matrix parses every PowerShell entrypoint with the actual Windows PowerShell 5.1 engine;
- CI rejects a BOM-less `start.ps1` when it contains non-ASCII bytes, preventing this regression from returning.

## 3.13.0

### ★ KaroX v3.13.0 — Persistent Notion Connection

- Adds `karox notion setup` for one-time persistent configuration.
- Uses a stable Tailscale Funnel `*.ts.net` URL instead of a new Quick Tunnel URL for each launch.
- Reuses one persistent Bearer credential across repository sessions and KaroX restarts.
- Protects the credential with Windows DPAPI when available; other platforms use a current-user-only profile file.
- Automatically forces Tailscale for Notion while leaving other KaroX providers unchanged.
- Stops an older Notion session before routing the stable endpoint to a new repository session.
- Adds `connection`, `status`, `rotate-key`, and `reset-connection` management commands.
- Replaces the brittle legacy Windows doctor path check.
- Resolves both `server/repo_tools.py` and the historical installed `server/server/repo_tools.py` layout.
- Makes the Notion provider doctor use the same resolver.
- Adds regression coverage for normal and nested layouts.
- persistent key lifecycle and explicit rotation;
- stable URL preservation;
- Windows PowerShell 5.1 generated-launcher parsing;

## 3.12.3

### ★ KaroX v3.12.3 — Notion MCP 421 Connection Hotfix

- Replaced the SDK's localhost-only Host allowlist with a KaroX tunnel-aware validator.
- Allowed only localhost, `*.trycloudflare.com`, `*.ts.net`, and exact hosts explicitly listed in `KAROX_MCP_ALLOWED_HOSTS`.
- Kept the per-session Bearer token mandatory for every MCP request.
- Switched MCP token comparison to constant-time comparison.
- Added cross-platform regression tests for allowed and rejected Host headers.
- Added a provider contract that verifies the SDK localhost-only protection is not accidentally re-enabled.

## 3.12.2

### ★ KaroX v3.12.2 — Legacy Launcher Migration Hotfix

- Detects cached PowerShell launchers that contain valid UTF-8 text but are missing the UTF-8 BOM.
- Rewrites those legacy files automatically instead of incorrectly treating them as reusable.
- Preserves normal caching once the launcher has the correct encoding.
- Adds a regression test that recreates the exact v3.12.0 cache state, runs the new patcher, and verifies that the BOM is restored.

## 3.12.1

### ★ KaroX v3.12.1 — Windows PowerShell Encoding Hotfix

- Generated `start.notion.generated.ps1` files are now written as UTF-8 with BOM, which Windows PowerShell 5.1 requires for scripts containing Cyrillic and Unicode interface symbols.
- Russian UI text no longer turns into mojibake such as `Р”/РЅ` or breaks the PowerShell parser.
- Cached generated launchers are read back with the same encoding used to write them.
- Provider tests now execute the real generator and verify the BOM, Cyrillic round-trip, shell encoding and absence of mojibake.
- CI now generates and parses the final launcher using Windows PowerShell 5.1, matching the environment used by the public Windows installer.
- Release-contract checks now derive archive and notes names from `VERSION` instead of hardcoding one release number.

## 3.12.0

### ★ KaroX v3.12.0 — Control Center & Reliability

- Added a browser-based **KaroX Control Center** for live repository, task, Git and Mission Control visibility.
- Added the unified commands `karox version`, `status`, `doctor`, `update`, `support` and `dashboard` on Windows, macOS and Linux.
- Added stable-channel self-update with `karox update` and a lightweight update notice.
- Added redacted support bundles that exclude source code and actively scan for leaked session keys.
- Routed ordinary OpenAPI sessions and Notion MCP sessions through the same hardened runtime.
- Added constant-time key comparison and temporary throttling after repeated authentication failures.
- Added request IDs, request-body limits, secure response headers and safer internal-error responses.
- Added bounded, rotating and redacted audit logs.
- Added authenticated `/meta`, `/capabilities` and `/security/status` endpoints.
- Added cached launcher generation to reduce repeated startup work.
- Expanded CI to validate the product on Windows, macOS and Linux.
- constant-time credential comparison;
- 30-failure-per-minute temporary client throttling;
- a configurable 30 MB request-body limit;

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
