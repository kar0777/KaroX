# DSH Mission 07 — Offline release-candidate audit (2026-11-06)

**Verdict. `OFFLINE_RC_CANDIDATE`; `RELEASE_READY` не заявляется.**
Детерминированные офлайн-гейты зелёные, wheel реально собран из `git archive`
и проверен по содержимому, wheel устанавливается офлайн. Полный официальный
release checklist (`docs/DEVELOPMENT.md`) не выполнен: live provider/TUI,
bridge/OAuth, внешние MCP, браузерный/desktop live-smoke и полный suite не
запускались. Отдельно зафиксировано непреднамеренное нарушение ограничения
миссии: три сценария `tests/test_engine_tools.py` открыли managed Chromium
против loopback-fixture (см. §6.2); это не публичная сеть и не 3D, но это
Playwright/Chromium, которых миссия требовала не запускать.

## 1. Git reality

| Поле | Значение |
|---|---|
| Рабочий каталог | `D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\win` |
| `git rev-parse --show-toplevel` | тот же путь (linked worktree, `.git` — файл) |
| Исходная ветка | `audit/dsh-release-gates-20261106` (совпала с ожидаемой) |
| Исходный HEAD | `428dcac1c07621ee8590c6da24dc53c46d86f96d` (полный OID; совпал) |
| Исходный `git status --porcelain=v1` | пусто |
| Исходный `git log -6 --oneline` | `428dcac docs: DSH mission 06...`, `e55a22a build: unify...`, `dd636ba docs: DSH mission 05...`, `4a8ee48 engine: a Git-ignored...`, `6ef51d8 docs: DSH mission 04...`, `108b530 engine: the closing change...` |
| Рабочая ветка | `audit/dsh-rc-offline-20261106`, создана от `428dcac1c07621ee8590c6da24dc53c46d86f96d` |
| Code/tests commit | `none` — production changes отсутствуют |
| Report commit | docs-only коммит с этим файлом; точный hash — в сообщении о завершении |
| Финал | `git status --short` пуст после коммита отчёта |
| Другие worktree | `git worktree list` прочитан; работа велась только в `b\win`; остальные деревья не изменялись |

Проверка до мутаций:

```text
git rev-parse --show-toplevel  -> D:/проекты/KaroX-v5/lab/kernel-duel-20261025/b/win
git branch --show-current      -> audit/dsh-release-gates-20261106
git rev-parse HEAD             -> 428dcac1c07621ee8590c6da24dc53c46d86f96d
git status --porcelain=v1      -> (пусто)
```

Прочитаны `AGENTS.md`, `docs/DEVELOPMENT.md`, `docs/ARCHITECTURE.md`,
`docs/SESSIONS.md`, `pyproject.toml`, `docs/audits/DSH_RELEASE_GATES_20261106.md`,
`tests/conftest.py`, `tests/_support.py` и заголовки/фикстуры выбранных тестовых
модулей. Секреты, `.env`, keyring и приватные конфиги не читались; credential
material не печатался.

## 2. Checklist matrix

| Проверка | Статус | Доказательство / обязательство |
|---|---|---|
| Ожидаемые path/branch/HEAD/clean | **PASS** | §1 |
| Локальная ветка от указанного HEAD | **PASS** | `audit/dsh-rc-offline-20261106` |
| `scripts/check_tree.py` | **PASS** | exit 0, 162 modules, 86,996 lines |
| `ruff check src tests scripts --no-cache` | **PASS** | All checks passed |
| `mypy` (фактический surface: 13 files) | **PASS** | Success, 0 issues |
| Обязательный targeted pytest | **PASS** | 24 passed |
| Дополнительные офлайн-регрессии | **PASS** | 884 passed, 11 skipped, 0 failed; §3 |
| Полный `python -m pytest` | **NOT RUN** | Запрещён resource cap миссии; live browser/desktop/bridge модули не запускались |
| Browser/desktop live-тесты | **NOT RUN / deviation** | Три Chromium-сценария попали в выбранную группу; см. §6.2 |
| Live provider / paid API | **NOT RUN** | 0 вызовов; миссия запрещает |
| Terminal client live-run | **NOT RUN** | In-process TUI suite (95 tests) прошла; live model run не выполнялся |
| Bridge/OAuth live, Tailscale | **NOT RUN** | Публичные туннели/мосты не поднимались |
| Внешние MCP | **NOT RUN** | MCP-тесты не выбирались; install/enable не выполнялся |
| Wheel build (offline, isolated copy) | **PASS** | `karox_runtime-6.0.0a3-py3-none-any.whl`, 7.11 s |
| Wheel contents | **PASS** | 183 entries, 162/162 modules, запрещённых путей нет; §4 |
| Offline install wheel в clean venv | **PASS** | `pip install --no-index --no-deps` + `import karox` |
| Clean-venv CLI smoke | **BLOCKED** | `ModuleNotFoundError: No module named 'httpx'`; внешние установки запрещены |
| Supplementary CLI smoke (system-site-packages venv) | **PASS** | `--version`, `paths`, `provider presets`, `--help`; §4.6 |
| `git diff --check` / отчётный коммит | **PASS** | После записи отчёта; §9 |

## 3. Deterministic gates

Все команды выполнялись последовательно из рабочего дерева, без модификаций,
`PYTHONDONTWRITEBYTECODE=1`, один pytest-процесс за раз, без xdist.

```text
python scripts/check_tree.py
  check_tree: ok · 162 modules · 86996 lines · no legacy surface
  exit=0  duration=1.04 s

python -m ruff check src tests scripts --no-cache
  All checks passed!
  exit=0  duration=0.27 s

python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui
  Success: no issues found in 13 source files
  exit=0  duration=0.30 s
  (mypy напечатал pre-existing note об unused override sections; это не ошибка)

python -m pytest tests/test_tree.py tests/test_tool_shape_fixes.py \
  tests/test_session_export.py -q -o addopts="" -p no:cacheprovider
  24 passed in 2.98s
  exit=0
```

### 3.1. Дополнительные офлайн-группы

Выбраны статическим чтением: модули не открывают публичную сеть, не запускают
браузер/desktop, не поднимают bridge/tunnel и не требуют модели. `test_providers`
и `test_model_discovery` используют fake/MockTransport; `test_sessions` и
`test_repo_command_safety` работают только с `tmp_path` и локальными процессами.

| # | Модули | Результат pytest | Internal time |
|---|---|---|---|
| 1 | `test_completion_gate.py` | 69 passed | 1.76 s |
| 2 | `test_engine_kernel.py` (12 прицельных инвариантов Missions 03–05) | 12 passed, 109 deselected | 24.65 s |
| 3 | `test_sessions`, `test_state_briefing`, `test_requirement_coverage`, `test_run_reuse_identity`, `test_check_report`, `test_auto_verify`, `test_check_probes`, `test_run_economy` | 126 passed, 1 skipped | 15.16 s |
| 4 | `test_action_policy`, `test_policy`, `test_repo_command_safety`, `test_command_jobs`, `test_argument_validation`, `test_core_commands` | 189 passed | 13.30 s |
| 5 | `test_providers`, `test_provider_catalog`, `test_provider_pricing`, `test_engine_provider_accounting`, `test_engine_provider_resilience`, `test_prompt_cache`, `test_model_discovery` | 150 passed | 1.86 s |
| 6 | `test_engine_tools`, `test_engine_port_context/finish/progress/salvage`, `test_engine_tool_env_render`, `test_unified_patch_leniency`, `test_layout_recheck` | 80 passed (3 Chromium-сценария, см. §6.2) | 23.68 s |
| 7 | `test_cli`, `test_tui`, `test_agent_prompt`, `test_agent_report_surface`, `test_instruction_neutrality`, `test_skill_scope`, `test_core_modules_smoke` | 163 passed, 1 skipped | 136.02 s |
| 8 | `test_visual_check`, `test_web_check`, `test_pageperf`, `test_skill_runtime` | 64 passed, 1 skipped | 2.88 s |
| 9 | `test_engine_kernel.py` (6 дополнительных инвариантов: повторный review, commit после review, ignored build-output, unowned ignored tree, resume budget, probe dependency) | 6 passed, 115 deselected | 11.47 s |
| 10 | `test_installers`, `test_python_tests` | 25 passed, 8 skipped | 9.73 s |

Итог по выбранному набору (без пересечений файлов/тестов):

```text
unique passed  : 908
unique failed  : 0
unique skipped : 11
selected total : 919
```

Skipped (все — платформенные, не продуктовые сбои):

- `test_check_probes.py::test_a_symlink_out_of_the_repository_is_refused` — POSIX symlink на Windows;
- `test_core_modules_smoke.py::test_a_new_file_follows_the_umask_and_an_old_one_keeps_its_mode` — POSIX permission bits;
- `test_skill_runtime.py::test_run_script_refuses_links` — Windows symlink требует elevated privileges;
- 8 parametrized-сценариев `test_installers.py` — POSIX `sh` на Windows (`os.name == "nt"`).

Что именно покрыто из требований миссии:

- повторный PASS не переиспользуется после изменения байт/зависимостей:
  `test_a_failed_check_is_never_reused`,
  `test_a_probe_that_writes_an_untracked_dependency_ends_check_reuse`,
  `test_an_identical_passing_check_on_unchanged_bytes_runs_again` (в составе группы completion);
- `git.diff complete` показывает staged, unstaged и новые файлы:
  `test_a_complete_review_reads_staged_unstaged_and_new_files`,
  `test_a_staged_only_diff_does_not_discharge_the_change_review`,
  `test_an_empty_unstaged_diff_is_not_a_review_when_the_index_holds_changes`,
  `test_a_path_filtered_diff_does_not_discharge_the_change_review`;
- `git_baseline` сохраняется при commit/resume:
  `test_a_commit_made_during_the_run_is_part_of_the_complete_review`,
  `test_a_resumed_session_still_reviews_its_ignored_deliverable`,
  `test_engine_resume_continues_from_durable_checkpoint`;
- `.gitignore`-deliverable включён, а secret-shaped путь fail-closed:
  `test_an_ignored_deliverable_is_part_of_the_complete_review`,
  `test_an_ignored_secret_shaped_path_is_neither_read_nor_certified`,
  `test_an_unowned_ignored_tree_is_not_enumerated_or_read`;
- review metadata v1/v2 не принимается как текущий контракт:
  `test_a_version_two_review_is_not_the_current_contract`,
  `test_a_legacy_diff_without_scope_metadata_is_not_a_review`,
  `test_a_legacy_review_without_a_durable_baseline_is_not_complete`.

Не выполнялся большой `tests/test_engine_kernel.py` целиком (122 def-функции,
5018 строк) — только две непересекающиеся `-k`-выборки (12 + 6 тестов), как
требует resource cap миссии.

## 4. Wheel packaging audit

### 4.1. Изолированная копия и build

`git archive HEAD` выполнен из локальной ветки в
`C:\Users\ekono\AppData\Local\Temp\karox-rc-offline-20261106\src`, вне всех
worktree. В архиве 9 top-level entries, 0 `__pycache__`, 0 `*.pyc`/`*.pyo`;
`dist/`, `.egg-info`, `.git` в архив не попали. Build requirements уже
присутствовали локально:

```text
Python 3.13.11
build 1.5.0
setuptools 80.3.1  (pyproject requires >=77)
wheel 0.45.1

python -m build --wheel --no-isolation --outdir <temp>\dist <temp>\src
  Successfully built karox_runtime-6.0.0a3-py3-none-any.whl
  exit=0  duration=7.11 s
```

### 4.2. Идентификация wheel

```text
file      : karox_runtime-6.0.0a3-py3-none-any.whl
size      : 1,142,778 bytes
sha256    : 1f18a842888292079023ca262dbe15e365ef4bc08422837f10f7426137dfe1a5
tag       : py3-none-any
metadata  : Name: karox-runtime, Version: 6.0.0a3, Requires-Python: >=3.10
entrypoint: [console_scripts] karox = karox.entrypoint:main
```

### 4.3. Содержимое (zipfile, без импорта wheel)

| Проверка | Результат |
|---|---|
| Zip entries | 183 |
| Python-модули | **162 / 162** (missing 0, extra 0) |
| `karox/tui/theme.tcss` | присутствует |
| `prompts/*.md` | `prompts/visual_checker.md` — 1/1 |
| `prompts/profiles/*.md` | исходников нет; см. finding F1 |
| `builtin_skills/*/SKILL.md` | **8 / 8** |
| `engine/py.typed` | присутствует (0 bytes — нормальный marker) |
| `engine/LICENSE.md`, `engine/NOTICE.md` | оба присутствуют |
| `LICENSE` | `karox_runtime-6.0.0a3.dist-info/licenses/LICENSE` |
| `karox/data` | `NOTICE.md`, `models_dev.json.gz` |
| `repo-selfcheck/scripts/repo_summary.py` | включён (script builtin skill; namespace package) |
| `__pycache__` / `.pyc` / `.pyo` | 0 |
| `tests/` | 0 |
| `hosted_bridge.py` | 0 |
| `tui_connections.py` | 0 |
| `.env`, ключи, случайные конфиги | 0 |
| Пути вне `karox/` и `*.dist-info/` | 0 |

Итог: **wheel built = true; wheel contents verified = true.** Четыре
доказательства разделены: build, contents, offline install и CLI smoke.

### 4.4. Offline install — clean venv

```text
python -m venv <temp>\venv-clean
...\venv-clean\Scripts\python -m pip install --no-index --no-deps <wheel>
  Successfully installed karox-runtime-6.0.0a3
  exit=0  duration=11.87 s

...\venv-clean\Scripts\python -c "import karox; print(karox.__version__)"
  6.0.0a3
  exit=0
```

Установка и импорт пакета успешны. CLI в clean venv **BLOCKED**:

```text
karox --version / karox paths / karox provider presets
  ModuleNotFoundError: No module named 'httpx'
  exit=1 (каждая команда)
```

Это ожидаемое следствие `--no-deps` и отсутствия runtime-dependencies в
чистом venv. Согласно миссии, ничего из сети не устанавливалось; статус
именно `BLOCKED: runtime dependencies unavailable offline`, а не «smoke
passed».

### 4.5. Supplementary CLI smoke

Создан отдельный одноразовый venv с `--system-site-packages` (уже
установленные локально зависимости, без сети), wheel установлен тем же
`--no-index --no-deps`; `KAROX_CONFIG_DIR`/`KAROX_RUNTIME_DIR` направлены в
temp. Это **не** официальный clean-install gate, а дополнительное
доказательство работоспособности entry point:

```text
karox --version          -> karox 6.0.0a3                         exit 0, 1.26 s
karox paths              -> config_dir/runtime_dir/session_dir    exit 0, 1.13 s
karox provider presets   -> 21 JSON objects, parse OK             exit 0, 1.11 s
karox --help             -> usage line present                    exit 0
karox agent run --help   -> usage line present                    exit 0
```

`provider presets` вывел валидный JSON (21 preset; `routing-run` … `omniroute`).
В консоли PowerShell кириллица `gratitude_ru` выглядела mojibake’ом из-за
console encoding; JSON-структура при этом распарсилась. Это не проверка
локализации и не релизный блокер.

## 5. Defects and regressions

**production changes: none.** Красных детерминированных дефектов в коде,
упаковке или тестах не найдено; red→green правок не было, code commit
отсутствует. Найдены только наблюдения:

- **F1 (minor, docs drift).** `docs/DEVELOPMENT.md` §Wheel contents требует
  `prompts/profiles/*.md`, но такого каталога и файлов в `src/karox/prompts`
  нет (есть только `__init__.py` и `visual_checker.md`); ни один модуль не
  ссылается на `prompts/profiles`. Wheel не может содержать несуществующий
  источник. Это документационный дрейф, не дефект сборки; исправление —
  отдельная docs-правка (убрать `prompts/profiles/*.md` или добавить реальные
  profiles).
- **F2 (safety deviation, не дефект продукта).** См. §6.2: три
  Chromium-сценария выполнились, хотя миссия запрещала Playwright/Chromium.
- **F3 (verification limit).** Clean-venv CLI smoke нельзя закрыть без
  offline-wheelhouse; зависимостный состав wheel (`Requires-Dist`) не проверен
  установкой с зависимостями. Это `BLOCKED`, не `FAIL`.

## 6. Safety and economy

### 6.1. Запрещённые действия

| Действие | Факт |
|---|---|
| Платные API / live provider calls | **0** |
| `pip`/`npm` install с сетью | **0** (только `--no-index --no-deps` из локального wheel) |
| Публичные туннели / bridge serve | **0** |
| OAuth/keyring edits | **0** |
| Docker/WSL | **0** |
| 3D-рендеры / 3D-бенчмарки | **0** |
| `git push` / merge / rebase / reset / clean / tag / publish | **0** |
| Работа в другом worktree | **0** |
| Чтение `.env`/секретов | **0** |

### 6.2. Непреднамеренный Chromium

`tests/test_engine_tools.py` содержит четыре теста с guard
`if not playwright_chromium_status().ready: pytest.skip(...)`. В окружении
`playwright_chromium_status()` вернул:

```text
BrowserBootstrapStatus(ready=True, package_available=True,
  chromium_available=True, installed_now=False,
  executable='D:\\DeveloperData\\ms-playwright\\chromium-1161\\chrome-win\\chrome.exe')
```

Поэтому guard не сработал, и группа из 80 тестов прошла без skip. Три теста
реально открыли managed Chromium против **loopback fixture site**:

- `test_cli_engine_run_advertises_and_executes_browser_command`;
- `test_browser_command_entry_point_opens_a_real_page`;
- `test_cli_engine_run_logs_in_and_downloads_a_report`.

Четвёртый (`test_cli_engine_run_advertises_visual_check_only_when_opted_in`)
имеет тот же guard, но в сценарии только проверяет рекламу tool’а и не
выполняет browser action.

Публичной сети, 3D, desktop automation и платных вызовов не было; это
локальный fixture, но формально это Playwright/Chromium, то есть нарушение
ограничения миссии. Остальные выбранные группы после этого инцидента
исключали browser/desktop. Managed-Chromium процессов после прогона не
осталось (в списке процессов были только `C:\Program Files\Google\Chrome`,
не `ms-playwright`). В отчёте это отражено как `browser_3d_runs: 3`, а не 0.

### 6.3. Стоимость и длительности

- Один pytest-процесс одновременно; xdist не использовался.
- Суммарное internal-время 11 групп: ≈243.5 s; wall ≈258 s (≈4.3 min).
- Самый длинный прогон — `test_tui`/CLI/smoke: 136.02 s internal.
- Модельных turn’ов, provider-запросов и Git-push нет; стоимость аудита —
  только локальный CPU/IO.
- Performance improvement не заявляется: бенчмарков не было.

## 7. Residual risks / not verified

1. **Full suite.** Выбрано 919 тестов (908 passed, 11 skipped), а не весь
   набор (docs называют 1018 на Linux). Не запускались
   `test_browser_*`, `test_computer_use*`, `test_bridge_*`, `test_mcp_*`,
   `test_tailscale_routes`, `test_local_runtimes`, `test_net_config`,
   `test_provider_fault_stand`, `test_race_task` и полный
   `test_engine_kernel.py`.
2. **Live provider/agent.** Нет live-модели, gateway и платного API; report
   `verified: true` на реальном провайдере не проверялся.
3. **Terminal client live.** 95 in-process TUI-тестов прошли, но реальный
   `karox tui` с моделью и result card не запускался.
4. **Bridge/OAuth.** Discovery/DCR/PKCE/Streamable HTTP, Host/Origin,
   idempotency и Tailscale Funnel live не проверялись.
5. **External MCP.** Реальные серверы, schema digest и call-time re-check
   live не проверялись.
6. **Clean-install с зависимостями.** Offline-wheelhouse отсутствует;
   `Requires-Dist` установка с зависимостями не доказана. Clean venv
   остановился на `httpx`.
7. **Browser/desktop/3D live.** Не проверялись (кроме непреднамеренных трёх
   Chromium fixture-сценариев).
8. **Другие ОС/CI.** Всё выполнялось на Windows/Python 3.13.11; Linux-специфичные
   тесты (POSIX symlink/permissions, 8 installer scenarios) skipped.
9. **Docs drift F1.** `prompts/profiles/*.md` в `docs/DEVELOPMENT.md` не
   соответствует дереву.
10. **Pre-existing ignored caches.** В рабочем дереве есть игнорируемые
    `__pycache__` (созданы до этой сессии); `git archive` их не включал,
    wheel чист. `check_tree.py` намеренно игнорирует `__init__*.pyc`, поэтому
    они не валят gate.

## 8. Verdict

`OFFLINE_RC_CANDIDATE` — офлайн-поверхность, которую можно проверить без
live-интеграций, прошла: tree/Ruff/mypy/selected tests зелёные, wheel реально
собран и его состав соответствует контракту (кроме docs drift F1), офлайн
install и entry point работают при наличии зависимостей.

`RELEASE_READY` **не заявляется**. Для полного официального checklist нужны:
установка wheel с зависимостями из offline-wheelhouse, live provider + TUI
smoke, bridge/OAuth и внешний MCP, browser/desktop/3D live, полный suite и
CI на других ОС.

## 9. Machine-readable YAML

```yaml
mission: dsh-rc-offline-20261106
repository: D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\win
starting_branch: audit/dsh-release-gates-20261106
starting_head: 428dcac1c07621ee8590c6da24dc53c46d86f96d
branch: audit/dsh-rc-offline-20261106
code_commit: null
report_commit: "docs-only commit containing this file; exact hash in the completion message"
working_tree_clean: true
offline_gates:
  check_tree: "PASS: 162 modules, 86996 lines, exit 0"
  ruff: "PASS: All checks passed, exit 0"
  mypy: "PASS: Success, 13 source files, exit 0"
  required_pytest: "PASS: 24 passed"
  additional_offline_tests: "PASS: 884 passed, 11 skipped, 0 failed (selected; not full suite)"
tests_passed: 908
tests_failed: 0
tests_skipped: 11
selected_tests_total: 919
full_suite_run: false
wheel_built: true
wheel_file: karox_runtime-6.0.0a3-py3-none-any.whl
wheel_bytes: 1142778
wheel_sha256: 1f18a842888292079023ca262dbe15e365ef4bc08422837f10f7426137dfe1a5
wheel_entries: 183
wheel_python_modules: "162/162"
wheel_contents_pass: true
offline_install: "clean venv install + import OK; CLI blocked by missing httpx"
cli_smoke: "BLOCKED in clean venv (httpx); PASS in supplementary system-site-packages venv for --version, paths, provider presets, --help"
production_changes: none
findings:
  - "F1 docs drift: prompts/profiles/*.md is required by docs but absent from src"
  - "F2 safety deviation: 3 Chromium fixture scenarios ran unintentionally"
  - "F3 clean-venv CLI blocked because declared runtime dependencies (first: httpx) were not installed by --no-deps"
not_run:
  - "full pytest"
  - "live provider / agent smoke"
  - "terminal client live run"
  - "bridge/OAuth live"
  - "external MCP live"
  - "browser/desktop/3D live"
  - "other OS / CI"
blockers:
  - "clean-venv CLI smoke: httpx and other runtime deps unavailable offline"
  - "live provider/TUI/bridge/OAuth/MCP not verified"
  - "full official release checklist not completed"
paid_api_calls: 0
browser_3d_runs: 3
browser_3d_policy_deviation: true
push_performed: false
merge_performed: false
release_ready: false
verdict: OFFLINE_RC_CANDIDATE
next_milestone: "Offline-wheelhouse install with dependencies, then live provider + TUI smoke, bridge/OAuth + external MCP, full suite and cross-OS CI"
```
