# DSH Mission 08 — Offline dependency closure & browser-free preflight (2026-11-06)

**Verdict. `DEPENDENCY_METADATA PASS`; `BROWSER-FREE QUICK GATE PASS`;
`CLEAN-VENV DEPENDENCY INSTALL BLOCKED`; `RELEASE_READY` не заявляется.**

Миссия закрыла два узких пробела Mission 07: wheel теперь проверен по
`Requires-Dist` и содержимому, а выборочный regression gate имеет явный
audited allowlist, который не может незаметно включить браузерные сценарии.
Третий пункт — docs drift `prompts/profiles/*.md` — исправлен без создания
фиктивных файлов. Полная установка wheel **с зависимостями** остаётся
`BLOCKED`: в репозитории и настройке проекта нет документированного
offline-wheelhouse, а сетевые установки запрещены. Поэтому `RELEASE_READY`
не заявляется.

## 1. Git reality

| Поле | Значение |
|---|---|
| Рабочий каталог | `D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\win` |
| `git rev-parse --show-toplevel` | тот же путь (linked worktree, `.git` — файл) |
| Исходная ветка | `audit/dsh-rc-offline-20261106` (совпала с ожидаемой) |
| Исходный HEAD | `10ac7179884f38c5715f78d8f639e114425e68bf` (полный OID; совпал) |
| Исходный `git status --porcelain` | пусто |
| Исходный `git log -5 --oneline` | `10ac717 docs: DSH mission 07…`, `428dcac docs: DSH mission 06…`, `e55a22a build: unify…`, `dd636ba docs: DSH mission 05…`, `4a8ee48 engine: a Git-ignored…` |
| Рабочая ветка | `audit/dsh-rc-deps-headless-20261106`, создана от `10ac717` |
| Tests/docs commit | `cf0482f69b57dfbbc8b855edf1674e971879611d` |
| Report commit | docs-only коммит с этим файлом; точный hash — в сообщении о завершении |
| Финал | `git status --short` пуст после коммита отчёта |
| Другие worktree | `git worktree list` прочитан; работа велась только в `b\win`; остальные деревья не изменялись |

Проверка до мутаций:

```text
git rev-parse --show-toplevel -> D:/проекты/KaroX-v5/lab/kernel-duel-20261025/b/win
git branch --show-current      -> audit/dsh-rc-offline-20261106
git rev-parse HEAD             -> 10ac7179884f38c5715f78d8f639e114425e68bf
git status --porcelain         -> (пусто)
```

Прочитаны `AGENTS.md`, `pyproject.toml`, `docs/DEVELOPMENT.md`,
`docs/audits/DSH_RC_OFFLINE_AUDIT_20261106.md` (особенно §§4–7),
`scripts/check_tree.py`, `tests/conftest.py`, `tests/_support.py` и исходники
всех тестовых модулей, попавших в allowlist либо в статический risk-map.
Секреты, `.env`, keyring и credential storage не читались.

## 2. Plan A — packaging dependency closure

### 2.1. Исходные объявления (A1)

`pyproject.toml` объявляет build backend `setuptools.build_meta` с
`setuptools>=77` и 19 runtime-зависимостей:

```text
anyio, httpx, keyring, SecretStorage (linux), jeepney (linux), mcp,
starlette, uvicorn, psutil, playwright, pydantic, PyYAML, textual, rich,
Pygments, markdown-it-py, python-dotenv, platformdirs, Jinja2
```

`[project.scripts]` объявляет `karox = "karox.entrypoint:main"`. `httpx`
входит в required dependencies и **не удалялся**. `pip install --no-index
--no-deps` устанавливает только сам пакет и не является проверкой runtime-
зависимостей; `ModuleNotFoundError: httpx` в этом сценарии ожидаем, пока не
доказано иное.

### 2.2. Офлайн-сборка wheel (A2)

`git archive HEAD` выполнен в `C:\Users\ekono\AppData\Local\Temp\
karox-rc-deps-headless-20261106\src`, вне всех worktree. Архив:
1,705,230 bytes, sha256 `700f2947…87c9`; 9 top-level entries; 0 `*.pyc`;
0 `__pycache__`; `dist/`, `.git`, `.egg-info` не попали.

Build requirements уже были локально, сеть не использовалась:

```text
Python 3.13.11
build 1.5.0
setuptools 80.3.1
wheel 0.45.1

python -m build --wheel --no-isolation --outdir <temp>\dist <temp>\src
  Successfully built karox_runtime-6.0.0a3-py3-none-any.whl
  exit=0  duration=6.17 s
```

Wheel: `karox_runtime-6.0.0a3-py3-none-any.whl`, 1,142,778 bytes, sha256
`89f144330c2a5906bc1777dbcdad8684178f8e5fbc92cde9a7cdbe9bcf085ca7`.
Размер совпадает с Mission 07; байтовый sha отличается, потому что zip-записи
несут build timestamps. Содержимое проверено независимо: 183 entries,
162/162 модуля, RECORD-дайджесты. Byte-for-byte reproducibility wheel не
заявляется.

### 2.3. METADATA, entry points, теги (A2)

Проверка выполнена через `zipfile`/`email`, без импорта wheel и без правки
`METADATA`.

| Поле | Значение |
|---|---|
| Name / Version | `karox-runtime` / `6.0.0a3` |
| Metadata-Version | `2.4` |
| Requires-Python | `>=3.10` |
| License-Expression | `MIT` |
| Wheel tag | `py3-none-any` |
| `entry_points.txt` | `[console_scripts]` / `karox = karox.entrypoint:main` |
| `top_level.txt` | `karox` |
| `Requires-Dist` | 23 строки: 19 runtime + 4 `dev` |
| `Provides-Extra` | `dev` |

Фактические runtime-объявления:

```text
anyio<4.15,>=4.4
httpx<1.0,>=0.27
keyring<27,>=25
SecretStorage<4,>=3.3; sys_platform == "linux"
jeepney<1,>=0.8; sys_platform == "linux"
mcp<2.0,>=1.27
starlette<2.0,>=0.37
uvicorn<1.0,>=0.30
psutil<8.0,>=5.9
playwright<2.0,>=1.50
pydantic<3.0,>=2.7
PyYAML<7.0,>=6.0
textual<7.0,>=0.89
rich<15.0,>=13.7
Pygments<3.0,>=2.17
markdown-it-py<5.0,>=3.0
python-dotenv<2.0,>=1.0
platformdirs<5.0,>=4.0
Jinja2<4.0,>=3.1
```

`httpx` классифицирован корректно: он объявлен, поэтому его отсутствие в
`--no-deps` venv — не дефект wheel. Маркеры Linux-зависимостей сохранены;
`dev` extra не входит в runtime closure.

### 2.4. Содержимое wheel и RECORD (A2)

| Проверка | Результат |
|---|---|
| Zip entries | **183** |
| Python-модули | **162 / 162** (missing 0, extra 0) |
| `karox/tui/theme.tcss` | присутствует |
| `karox/prompts/*.md` | `visual_checker.md` — 1/1 |
| `karox/prompts/profiles/*.md` | **нет источника и нет записи в wheel**; см. Plan C |
| `karox/builtin_skills/*/SKILL.md` | **8 / 8** |
| `karox/engine/py.typed` | присутствует (0 bytes — нормальный marker) |
| `karox/engine/LICENSE.md`, `NOTICE.md` | оба присутствуют |
| `karox/data/models_dev.json.gz`, `NOTICE.md` | оба присутствуют |
| `LICENSE` | `…dist-info/licenses/LICENSE` |
| `repo-selfcheck/scripts/repo_summary.py` | присутствует |
| `__pycache__` / `.pyc` / `.pyo` | **0** |
| `tests/` | **0** |
| `hosted_bridge.py`, `tui_connections.py` | **0** |
| `.env`, `.pem`, `.key` | **0** |
| Пути вне `karox/` и `*.dist-info/` | **0** |
| RECORD | 183 строки, 183 файла, **0** digest mismatch, 0 unrecorded entries |

Итог: **wheel build PASS; wheel contents PASS; wheel metadata PASS.**

### 2.5. Установка и CLI smoke (A3)

Поиск offline-wheelhouse ограничен разрешёнными местами: репозиторий,
`pyproject.toml`/docs и настройка pip. `wheelhouse`, `wheels`, `vendor`,
`dist`, `offline` в корне репозитория отсутствуют; `git grep` находит
`wheelhouse` только в тексте Mission 07; `pip config list` содержит только
`:env:.cache-dir`; `PIP_FIND_LINKS` не задан. Широкий обход `HOME` не
выполнялся. Следовательно:

```text
BLOCKED: no complete offline wheelhouse
```

Сетевые установки не предпринимались. Чистый venv проверен только на
`--no-deps`, чтобы отделить установку пакета от dependency closure:

```text
python -m venv <temp>\venv-clean                         exit=0  5.26 s
venv\Scripts\python -m pip install --no-index --no-deps <wheel>
  Successfully installed karox-runtime-6.0.0a3           exit=0
python -c "import karox; print(karox.__version__)"       -> 6.0.0a3, exit=0
pip list                                                 -> karox-runtime==6.0.0a3, pip==25.3
python -c "import httpx"                                 -> ModuleNotFoundError, exit=1
karox --version                                          -> ModuleNotFoundError: httpx, exit=1
```

`ModuleNotFoundError` указывает на `karox/providers.py:34` (`import httpx`) —
ожидаемое следствие `--no-deps`, а не отрицательный тест METADATA. Статусы
разделены явно:

- **wheel metadata PASS**;
- **wheel isolated install (no deps) PASS**;
- **clean-venv dependency install BLOCKED**;
- **clean-venv CLI smoke BLOCKED**.

Дополнительный smoke в одноразовом venv с `--system-site-packages` (явно
помечен как supplementary, не заменяет clean-install gate): wheel установлен
тем же `--no-index --no-deps`, `KAROX_CONFIG_DIR`/`KAROX_RUNTIME_DIR`
направлены в temp.

```text
karox --version          -> karox 6.0.0a3                    exit=0
karox paths              -> config_dir/runtime_dir/session_dir, exit=0
karox provider presets   -> 21 JSON object (JSON Lines), exit=0
karox --help             -> usage present, exit=0
karox agent run --help   -> usage present, exit=0
```

`provider presets` выводит по одному JSON-объекту на строку; первый разбор
ошибочно ожидал один JSON-массив и упал на «Extra data». После корректного
разбора — 21 объект, exit 0. Это артефакт метода проверки, не дефект CLI.
pip также предупредил о pre-existing невалидной дистрибуции
`~arox-runtime` в глобальном site-packages; warning относится к окружению
supplementary venv и к clean-install доказательству не относится.

## 3. Plan B — browser-free deterministic selection

### 3.1. Статический аудит источников

По 66 файлам `tests/test_*.py` построена risk-map: AST-импорты плюс токены
`playwright`, `chromium`, `browser`, `computer_use`, `socket`, `http(s)://`,
`subprocess`, `httpx`, `uvicorn`, `visual_check`, `web_check`, `pageperf`,
`live`. Затем **полностью прочитаны** 12 модулей allowlist. Ни один из них
не импортирует Playwright/Selenium/`karox.browser`/`karox.computer_use` и не
содержит внешних URL. `tests/test_tool_shape_fixes.py` использует только
локальный `git init` и локальный LSP-вызов (без сети); `tests/test_session_export.py`
вызывает CLI in-process; `tests/test_skill_scope.py` читает SKILL.md и каталог
скиллов.

Модули с прямым browser/network surface (`test_browser_*`,
`test_engine_tools`, `test_computer_use*`, `test_cli`, `test_bridge_*`,
`test_mcp_*`, `test_net_config`, `test_providers`, `test_policy`,
`test_tui`, `test_web_check`, `test_visual_check`, `test_pageperf`,
`test_session(s)`, `test_installers`, `test_auto_verify` и др.) в allowlist
не входят.

### 3.2. Allowlist и точная команда

```text
tests/test_tree.py
tests/test_tool_shape_fixes.py
tests/test_session_export.py
tests/test_agent_prompt.py
tests/test_agent_report_surface.py
tests/test_engine_provider_accounting.py
tests/test_engine_tool_env_render.py
tests/test_run_reuse_identity.py
tests/test_skill_scope.py
tests/test_unified_patch_leniency.py
tests/test_package_data.py
tests/test_headless_selection.py
```

Первые три файла — отправные точки Mission 07 (24 теста); остальные девять
добавлены только после чтения исходников. Команда зафиксирована в
`docs/DEVELOPMENT.md` и намеренно перечисляет файлы, без `-k` и glob:

```text
python -m pytest -o addopts="" -p no:cacheprovider tests/test_tree.py tests/test_tool_shape_fixes.py tests/test_session_export.py tests/test_agent_prompt.py tests/test_agent_report_surface.py tests/test_engine_provider_accounting.py tests/test_engine_tool_env_render.py tests/test_run_reuse_identity.py tests/test_skill_scope.py tests/test_unified_patch_leniency.py tests/test_package_data.py tests/test_headless_selection.py
```

### 3.3. Положительный контроль

```text
collected 64 items
64 passed in 3.19s
exit=0   wall=4.4 s
```

0 failed, 0 skipped, 0 deselected. Прогон последовательный, без `-n`/xdist,
с `-o addopts=""`, `-p no:cacheprovider` и `PYTHONDONTWRITEBYTECODE=1`.
Post-run проверка `Win32_Process` по `ExecutablePath -like '*ms-playwright*'`
вернула **0** managed-browser процессов. Первичный грубый поиск по
`CommandLine` дал ложные совпадения (собственная команда поиска и
pre-existing `chrome-headless-shell`, созданный 2026-11-03 12:50), поэтому
вывод построен на executable path и времени создания. Ни один браузер,
Chromium, Playwright, desktop automation или 3D-сценарий не запускался.

### 3.4. Отрицательный контроль

Три сценария, которые в Mission 07 непреднамеренно открыли managed Chromium,
определены только в `tests/test_engine_tools.py`:

```text
tests/test_engine_tools.py:110:def test_cli_engine_run_advertises_and_executes_browser_command(
tests/test_engine_tools.py:277:def test_browser_command_entry_point_opens_a_real_page(
tests/test_engine_tools.py:448:def test_cli_engine_run_logs_in_and_downloads_a_report(
```

Статическое доказательство, без их исполнения:

- `test_engine_tools.py` **не входит** в ALLOWLIST;
- ни одна `def` в 12 allowlisted-модулях не совпадает с этими именами;
- файл `test_engine_tools.py` не исполнялся (только `read_text`);
- `tests/test_headless_selection.py` падает, если три имени появятся в
  allowlist, если файл получит прямой browser-импорт, если в allowlisted-
  исходниках появится `playwright`/`chromium`/`selenium`/`karox.browser`/
  `karox.computer_use`/`http(s)://`, либо если команда в
  `docs/DEVELOPMENT.md` разойдётся с ALLOWLIST.

`tests/test_headless_selection.py` — неисполняющий manifest-guard: 5 тестов,
только чтение файлов и AST. Он не заменяет полный suite и не скрывает
браузерные тесты в CI: полный gate остаётся прежним.

### 3.5. Ограничение

Quick gate — подмножество, а не полный релизный gate. Полный `pytest` в этой
миссии запрещён resource cap; browser/desktop/bridge/live-модули не
запускались. Маркеры pytest не добавлялись и `skip` браузерных тестов в
официальном CI не вводился.

## 4. Plan C — docs drift `prompts/profiles`

До правки: `docs/DEVELOPMENT.md` §Wheel contents требовал
`prompts/*.md` **и** `prompts/profiles/*.md`. В дереве
`src/karox/prompts/` есть только `__init__.py` и `visual_checker.md`;
каталога `profiles/` нет; ни один модуль `src/karox` не ссылается на
`prompts/profiles`. Bridge saved profiles — это runtime JSON
(`web-bridge-profiles.json`), а не пакетируемые prompt-файлы.

После правки требование удалено, оставлено `prompts/*.md`, добавлено
пояснение о runtime JSON. Фиктивный пустой `profiles/` не создавался.
`README.md` таких ресурсов не обещает; `docs/ARCHITECTURE.md` и
`docs/MIGRATION.md` упоминают только реальный `prompts/`.

Добавлен `tests/test_package_data.py` (5 тестов): проверяет непустые
`prompts/visual_checker.md`, `tui/theme.tcss`, engine license/notice,
`data/NOTICE.md`, `models_dev.json.gz`, наличие `engine/py.typed` (может быть
пустым), 8/8 `builtin_skills/*/SKILL.md`, сохранение package-data
объявлений в `pyproject.toml` и отсутствие в docs требования несуществующего
`prompts/profiles` (проверяются только пункты списка Wheel contents, чтобы
пояснительный текст не считался требованием).

## 5. Обязательные безопасные гейты

Все команды — последовательно, из рабочего дерева, `PYTHONDONTWRITEBYTECODE=1`.

| Гейт | Команда | Результат |
|---|---|---|
| Tree hygiene | `python scripts/check_tree.py` | **PASS**: 162 modules, 86,996 lines, exit 0 |
| Ruff | `python -m ruff check src tests scripts --no-cache` | **PASS**: All checks passed, exit 0 |
| Mypy (release surface) | `python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui` | **PASS**: Success, 13 source files, exit 0 (pre-existing unused-override note) |
| Quick gate | команда §3.2 | **PASS**: 64 passed in 3.19s, exit 0 |
| Whitespace | `git diff --check` | **PASS**: exit 0 |
| Full `pytest` | — | **NOT RUN** (запрещён миссией) |
| Live smoke | — | **NOT RUN** (запрещён миссией) |

Package-line ceiling не менялся: единый `MAX_PACKAGE_LINES = 88_000`,
фактически 86,996 строк. `src/karox` не изменялся: `git diff
10ac717..HEAD --stat -- src` пуст.

## 6. Стоимость и безопасность

| Действие | Факт |
|---|---|
| Browser/Chromium/Playwright запуски | **0** |
| Managed-browser процессы, запущенные миссией | **0** |
| Desktop automation / 3D | **0** |
| Внешние API / модели / платные вызовы | **0** |
| Сетевые установки (`pip`/`npm`/PyPI/GitHub) | **0** |
| Bridge/tunnel/открытые порты | **0** |
| `git push` / merge / rebase / reset / clean / tag / publish | **0** |
| Работа в другом worktree | **0** |
| Чтение `.env`/секретов/keyring | **0** |
| Изменение `src/karox` | **0** |
| Изменение `pyproject.toml` зависимостей | **0** |

Основная стоимость — локальный CPU/IO: wheel build 6.17 s, quick gate 3.19 s
pytest / 4.4 s wall, остальные гейты — секунды. Live provider-вызовов и
модельных turn'ов не было.

### POLICY DEVIATIONS

**none.** Ни один browser/Chromium/Playwright/desktop/3D сценарий не был
запущен; сеть не использовалась; wheel ставился только локально с
`--no-index`; другие worktree, секреты и продуктовый runtime не менялись.

## 7. Before / after

| Область | Before | After |
|---|---|---|
| Wheel dependency metadata | `Requires-Dist` не был доказан установкой; F3 Mission 07 оставался открытым | 23 `Requires-Dist` проверены; `httpx` объявлен; METADATA/entry points/содержимое/RECORD PASS |
| Wheel build | собран в Mission 07 | пересобран офлайн из `10ac717`; 183 entries, 162/162 модуля |
| Clean-venv install | `BLOCKED` на `httpx` | **по-прежнему BLOCKED**: offline-wheelhouse отсутствует; `--no-deps` install PASS не маскирует это |
| Test selection | Mission 07 выбрала 919 тестов; 3 browser-сценария стартовали Chromium | 12 файлов / 64 теста, явный allowlist; 0 browser-процессов; negative control для трёх инцидентов |
| Docs | `docs/DEVELOPMENT.md` требовал несуществующий `prompts/profiles/*.md` | требование удалено; добавлено пояснение и headless quick gate; drift защищён тестом |
| Product runtime | без изменений | без изменений (`git diff -- src` пуст) |
| Package data | проверялся вручную | `tests/test_package_data.py` пиннит реальные ресурсы и docs-консистентность |

## 8. Блокеры и residual risks

1. **Clean-venv dependency install / CLI smoke — BLOCKED.** В репозитории и
   pip-конфигурации нет документированного offline-wheelhouse с полным
   dependency closure; сетевые установки запрещены. `--no-deps` установка
   и импорт пакета успешны, но это не dependency proof. Для снятия блокера
   нужен путь к полному локальному wheelhouse и разрешение на его
   использование; затем `pip install --no-index --find-links <wheelhouse>
   <wheel>` без `--no-deps` и CLI smoke в чистом venv.
2. **Live provider / terminal client.** Модель, gateway и `karox tui` с
   реальным provider не запускались; `verified: true` на live-модели не
   проверялся.
3. **Bridge/OAuth и внешний MCP.** Live discovery/DCR/PKCE/Streamable HTTP,
   Tailscale Funnel и реальные MCP-серверы не проверялись.
4. **Browser/desktop/3D live.** Не проверялись; в этой миссии запрещены, и
   ни один такой сценарий не исполнялся.
5. **Full suite и cross-OS CI.** Полный `pytest` и Linux/macOS CI не
   запускались (resource cap миссии и Windows-окружение).
6. **Wheel byte reproducibility.** sha256 wheel не совпадает с Mission 07
   из-за zip timestamps; содержимое, модули и RECORD совпадают. Это
   ограничение метода, не дефект пакета.
7. **Quick gate — подмножество.** 64 теста не заменяют полный release gate;
   официальный CI по-прежнему должен запускать полный suite.

## 9. Final decision

`OFFLINE_RC_CANDIDATE` сохраняется. В рамках Mission 08 доказано:

- wheel корректно **объявляет** runtime-зависимости, включая `httpx`;
- wheel собирается офлайн, содержит 162/162 модуля и обязательные package
  data, не содержит cache/tests/retired modules;
- выборочный regression gate **явно browser-free**, воспроизводим и имеет
  отрицательный контроль для трёх инцидентов Mission 07;
- docs drift `prompts/profiles/*.md` исправлен без фиктивных файлов.

`RELEASE_READY` **не заявляется**: clean-venv install с зависимостями
`BLOCKED`, live-интеграции и полный suite не выполнялись. Следующий шаг для
review — предоставить документированный полный offline-wheelhouse (или
разрешить контролируемую установку зависимостей) и отдельно разрешить live
smoke/bridge/MCP проверки.

## 10. Machine-readable YAML

```yaml
mission: dsh-rc-dependency-headless-20261106
starting_head: 10ac7179884f38c5715f78d8f639e114425e68bf
branch: audit/dsh-rc-deps-headless-20261106
final_head: "docs-only report commit; exact hash in the completion message"
code_commit: cf0482f69b57dfbbc8b855edf1674e971879611d
report_commit: "docs-only report commit; exact hash in the completion message"
worktree_clean: true
wheel_build: PASS
wheel_metadata: PASS
clean_venv_dependency_install: BLOCKED
clean_venv_cli_smoke: BLOCKED
browser_free_test_count: 64
browser_free_test_result: "64 passed, 0 failed, 0 skipped"
browser_processes_started: 0
paid_api_calls: 0
network_installs: 0
browser_3d_runs: 0
full_suite_run: false
changed_product_runtime: false
release_ready: false
policy_deviations: []
remaining_blockers:
  - "clean-venv dependency install and CLI smoke: no complete offline wheelhouse; network installs forbidden"
  - "live provider/TUI, bridge/OAuth and external MCP smoke not run"
  - "browser/desktop/3D live not run (forbidden in this mission)"
  - "full pytest and cross-OS CI not run"
report_path: docs/audits/DSH_RC_DEPENDENCY_HEADLESS_20261106.md
```
