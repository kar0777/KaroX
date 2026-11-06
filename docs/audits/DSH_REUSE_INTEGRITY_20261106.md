# DSH Mission 02 — Verification cache integrity after code execution (2026-11-06)

**Verdict.** Mission 01 закрыла probe/inline, но оставила тот же дефект на двух
поверхностях: проект, выполняемый `tests.run`, и буквальный одобренный argv в
`checks.run` (он тоже исполняет код проекта: `conftest.py`, тесты, скрипты).
Офлайн, через реальный путь `CoreToolEnvironment → CoreRuntime`, воспроизведены
**четыре** сценария, в которых прогон заканчивался `VERIFIED`, хотя одобренная
проверка на финальных байтах падает, — потому что её повтор был отвечен из кэша
и `completion_identity` перепривязана к текущим байтам. Дефект системный, не
зависящий от задачи: кэш считал равенство известных идентичности файлов
доказательством отсутствия побочных эффектов, а у исполняющего прогона такого
доказательства быть не может. Кэш вердиктов удалён: повтор проверки/тестов
всегда исполняется. Гейт `completion_gate`, `identity()` и правила `VERIFIED` не
менялись. Все четыре сценария после фикса не верифицируются, repair-путь
по-прежнему даёт честный `VERIFIED`. Полный pytest, live-браузер, 3D, сборка
wheel и платные API не запускались; `scripts/check_tree.py` красный **до** этого
изменения (см. §7).

---

## 1. Git reality

| Поле | Значение |
|---|---|
| Абсолютный путь | `D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\win` |
| Git top-level | тот же путь (linked worktree, `.git` — файл) |
| Исходная ветка / HEAD | `audit/dsh-verified-20261106` / `040882d08fbac5fdd5ce7e31d198ff061b56fb38` |
| Состояние на старте | `git status --short` пуст |
| Рабочая ветка | `audit/dsh-reuse-integrity-20261106` (создана от HEAD) |
| Code commit | `f831a9a2201264cfc941bf0c29760e6a55e583f6` |
| Report commit | отдельный docs-only коммит с этим файлом и записью в `CHANGELOG.md` |
| Финал | `git status --short` пуст после коммита отчёта |
| Другие деревья | не изменялись; push, merge, reset, clean, удаления, установки не выполнялись |

Проверено до любых изменений:

```text
git rev-parse --show-toplevel  -> D:/проекты/KaroX-v5/lab/kernel-duel-20261025/b/win
git branch --show-current      -> audit/dsh-verified-20261106
git rev-parse HEAD             -> 040882d08fbac5fdd5ce7e31d198ff061b56fb38
git status --short             -> (пусто)
git log -6 --oneline --decorate
  040882d (HEAD -> audit/dsh-verified-20261106) docs: DSH mission 01 verification audit (2026-11-06)
  67271be engine: arbitrary-code probes end approved-check reuse
  ae9f96b (v6/win-20261105) v6: the closing look judges the run's own page; ...
```

Прочитаны `AGENTS.md`, `docs/audits/DSH_VERIFIED_AUDIT_20261106.md`,
`docs/CHANGELOG.md`, исходники перечисленных ниже модулей и тестовые хелперы.
Секреты, `.env`, keyring, W&B и приватные конфиги не читались и не печатались.

## 2. Execution surface matrix (до фикса)

«Нейтрален» = входил в `_RUN_NEUTRAL_TOOLS` (`engine_tool_env.py:63-90` на
`040882d`) и потому **не** завершал reuse; «в кэше» = входил в `_REUSABLE_RUNS`
(`:59`) и его PASS можно было переиграть. После фикса кэша вердиктов нет ни на
одной поверхности, поэтому столбец «влияние» описывает только состояние до него.

| # | Tool | Исполняет произвольный код | Может писать вне `changed_files` | До fix: нейтрален / в кэше | Доступен в `WORKSPACE_WRITE` | До fix: влияние на reuse/`VERIFIED` | Чем подтверждено |
|---|---|---|---|---|---|---|---|
| 1 | `checks.run`, буквальный одобренный `argv` | да — pytest импортирует `conftest.py`, скрипт проекта исполняется | да — fixture, state/lock-файл, ignored dependency | да / да | да (`CHECKS_RUN`) | повтор отвечался из кэша, proof перепривязывался → **ложный `VERIFIED`** | новый test §5.3.5 (`…whose_own_write…`) |
| 2 | `checks.run`, probe (`python -c/-m/скрипт`) | да | да | да / да | да (если probes не выключены) | Mission 01: epoch-bump после прогона → повтор исполнялся | `test_a_probe_that_writes_an_untracked_dependency_ends_check_reuse` (green до и после) |
| 3 | `checks.run`, inline под wildcard-правилом | да | да | да / да | да | Mission 01: bump по `inline_code_from_file` | тот же тест; код `core_process.py:986-1014` |
| 4 | `tests.run` | да — pytest/node-скрипт проекта в cwd репозитория | да | да / да | да (`CHECKS_RUN`) | повтор одобренной проверки после него отвечался из кэша → **ложный `VERIFIED`** | новые тесты §5.3.1-4 |
| 5 | `repo.command`, `repo.apply_patch` | нет (файловые операции, не shell) | да, но пишет `changed_files` | нет / нет | да (`REPO_WRITE`) | epoch-bump до запуска → старый pass не переигрывался | `tests/test_core_commands.py`, `test_repo_command_safety.py` |
| 6 | `skill.run` | да (`PROCESS_RUN`, python/sh-скрипт скилла; cwd — scratch, но путь в репо ничем не запрещён) | да | нет / нет | да (`PROCESS_RUN` входит в `WORKSPACE_WRITE`) | bump до запуска → старый pass не переигрывался; кода-пути с кэшем нет | код `skill_runtime.py:605-696`; отдельных тестов на reuse не было |
| 7 | `dev.command`, `dev.command_start` | да | да | нет / нет | **нет** — только `ELEVATED` (`DEV_COMMAND`) | bump до запуска | код `tool_schemas.py:459-474`, `policy.py:66-84` |
| 8 | `checks.start` (durable job) | да — тот же одобренный `argv` в фоне | да, **после** возврата вызова | нет / нет | зарегистрирован только при `command_jobs=True`, т.е. только в hosted-сборе (`hosted_core.py:409,510`) | в engine env недоступен; hosted-мост вызывает core напрямую и кэша вердиктов не имеет | код `tool_schemas.py:122-141`, `core_tools.py:358,380-385` |
| 9 | `visual.check` | рендерит страницу в Chromium | пишет скриншоты/артефакты (вне identity), JS страницы — нет | да / **нет** | да (`REPO_READ`, checker инжектится) | переигран быть не мог (не в `_REUSABLE_RUNS`); как нейтральный сосед не обнулял чужой pass | существующие visual-тесты |
| 10 | `lsp.diagnostics` | да — запускает language server (`pyright-langserver`) в cwd репо | может писать кэши/`__pycache__` | да / нет | да (`DIAGNOSTICS_READ`) | **остаточная дыра класса**: нейтральный исполнитель, мог не обнулить чужой pass | код `core_search.py:489-509`, `lsp_diagnostics.py:208-231`; отдельного теста нет |
| 11 | `git.diff/status/log` | опосредованно — `git diff` исполняет `diff.<driver>.textconv` из локального git config (`.gitattributes` — из репо) | нет tracked-байт; `--no-ext-diff` отключает только внешние diff-программы | да / нет | да (`GIT_READ`) | review-путь; кэша вердиктов не было | код `core_tools.py:…_git_diff_paths`; §7.2 |
| 12 | `web.fetch` | нет (загрузка файла) | да, записывает `changed_files` | нет / нет | да (`CODE_FETCH`) | bump до запуска | существующие тесты web.fetch |
| 13 | `mcp.*` (мутирующие) | внешняя система | да | нет / нет | да (`MCP_CALL`) | bump до запуска | `test_engine_kernel.py` MCP-тесты |
| 14 | `browser.command` | страница/браузер | может скачивать/писать | нет / нет | да (`BROWSER_READ`; input-действия дополнительно гейтятся `BROWSER_INPUT`) | bump до запуска | существующие browser-тесты |

Вывод: до фикса reuse был безопасен для поверхностей 5-7 и 12-14 (они обнуляли
epoch; 8 в engine env вообще не зарегистрирован). Опасными были 1 и 4 (кэш
вердикта) и 10 (нейтральный исполнитель, который мог изменить входы, не обнуляя
чужой pass).

## 3. Root cause

Точная цепочка (воспроизведена офлайн через `Harness → ExtendedCoreRuntime →
CoreToolEnvironment`, временный Git-репозиторий, никакой модели):

1. `checks.run` и `tests.run` входят и в `_REUSABLE_RUNS`
   (`engine_tool_env.py:59` на `040882d`), и в `_RUN_NEUTRAL_TOOLS` (`:63-90`).
2. `core.py:1204-1216` для `checks.run` и `core_tools.py:1010-1023` для
   `tests.run` пишут в историю только запись проверки; файлы, которые эти
   команды записали, **не** попадают в `record.changed_files`.
3. PASS запоминается в `_passed_runs` (`engine_tool_env.py:1374-1413`) вместе с
   `completion_gate.identity()` (`completion_gate.py:670-707`), а идентичность
   содержит только changed/read файлы.
4. Другой исполнитель (`tests.run`, probe, сам approved argv) пишет `spec.txt`,
   которого прогон не читал и не менял: identity не меняется.
5. Повтор той же проверки попадает в `_reuse_passed_run`
   (`engine_tool_env.py:1415-1493`): возвращает старый `CoreResult`, ставит
   `reused_from` (`:1444`) и **перепривязывает** `completion_identity` к текущей
   идентичности (`:1454-1456`). `same_run_inputs` (`completion_gate.py:710-744`)
   сравнивает только прежние известные файлы, поэтому разрешает replay.
6. `floor` (`completion_gate.py:930-939`) принимает результат с
   `completion_identity == current`; `_verification` (`engine_kernel.py:2171`) и
   `_completed` (`:2206`) дают `verified: true`.

Фактическое поведение **до** фикса (первый прогон нового теста):

```text
AgentReport(status='verified', phase='verification', verified=True,
            reason='submitted', ...)     # spec.txt при этом = state=bad
тот же argv, исполненный реально на этих байтах: exit_code=3
```

Второй воспроизведённый случай шире, чем `tests.run`: **буквальный одобренный
argv** сам создаёт ignored state-файл и проходит только на первом запуске.
Повтор из кэша вернул `exit 0`, а настоящий повтор даёт `exit 3`; прогон
отчитался `VERIFIED`. Значит literal approved argv не является read-only.

Третий и четвёртый случаи: `tests.run`, который записал `spec.txt` и **упал**
(exit 1), и `tests.run`, который записал и **был убит по timeout**. Ни падение,
ни timeout не отменяли уже записанный файл, а кэш оставался валидным (оба
вызова нейтральны), поэтому повтор проверки снова переигрывался.

## 4. Code diff

Один локальный коммит `f831a9a2201264cfc941bf0c29760e6a55e583f6`:

| Файл | Δ | Назначение |
|---|---|---|
| `src/karox/engine_tool_env.py` | +17/−254 | удалён кэш вердиктов и вся его обвязка: `_REUSABLE_RUNS`, `_RUN_NEUTRAL_TOOLS`, `_EXACT_EDIT_TOOLS`, `_ran_unchecked_code`, `_wrote_anything`, `_run_key`, `_state_epoch`/`_passed_runs`, `_remember_passed_run`, `_reuse_passed_run` и epoch-книгодержание в `_execute` (ветка reuse, pre-bump, restore для «ничего не записавшего» edit, bump после probe). Вместо них — короткое документированное правило: повтор всегда исполняется |
| `src/karox/completion_gate.py` | +13/−5 | только docstring `same_run_inputs`: равенство известных входов — **не** лицензия на reuse; поведение и сигнатура не менялись, вызовов в production больше нет |
| `tests/test_engine_kernel.py` | +354/−38 | 6 новых regression-тестов, 3 переписанных (reuse → «исполняется заново»), обновлён docstring Mission 01-теста |

**Почему выбран этот подход.** Принцип из задания: PASS можно переиспользовать
только при доказанной неизменности входов и отсутствии недоказанных побочных
эффектов. Для прогона, который исполняет код репозитория, такого доказательства
нет и быть не может: `identity()` видит только известные KaroX файлы, а
`changed_files` не пополняется из побочных записей команды. Значит допустимая по
заданию стратегия — «консервативно отказаться от reuse для исполнения, чистоту
которого нельзя гарантировать»: кэш удалён, а не ослаблен эвристикой.

**Отвергнутые альтернативы.**

- *Bump epoch после каждого фактического запуска* — даёт то же поведение (кэш
  никогда не попадает), но оставляет мёртвый код и мнимую оптимизацию; удаление
  честнее и меньше.
- *Reuse только для «двух подряд настоящих PASS»* — второй прогон тоже пишет и
  тоже может изменить входы; стоимость удваивается, гарантии не появляется.
- *Reuse как подсказка модели, но real final run как proof* — требует снимать
  proof у ранее записанного результата в durable-записи, т.е. менять семантику
  `floor`/`VERIFIED`; задание прямо просит этого не делать, если причину можно
  закрыть в cache/epoch.
- *Whitelist «read-only» классов команд* (`structural`, `git diff --check`) —
  `python -m build` и `python -m pytest` попадают в те же классы и исполняют код
  проекта; правило было бы хрупким и task-specific.
- *Полное хеширование дерева до/после каждого прогона* — задание запрещает
  (сотни тысяч файлов, `node_modules`), дорого и всё равно racy.

**Что не менялось:** `floor`, `identity()`, `same_run_inputs()` (кроме
докстринга), `engine_kernel` (финальные вызовы и auto-finish), схема
инструментов, политика и риск-движок, `pytest`-путь `tests.run`.

## 5. Tests

Сначала red, затем green; всё через реальный `CoreToolEnvironment`, временные
Git-репозитории в `tmp_path`, без модели и сети.

### 5.1. Red до фикса (на `040882d` + новые тесты)

```text
python -m pytest tests/test_engine_kernel.py -q -o addopts="" \
  -k "hidden_dependency or wrote_a_dependency or whose_own_write or every_identical_check \
      or unchanged_bytes_runs_again or still_runs_the_check or untracked_dependency"
-> 9 failed, 1 passed, 90 deselected in 22.44s
```

Красные по правильной причине (три независимых свидетельства):

```text
# ложный VERIFIED на неверных байтах (spec.txt = state=bad):
E  AssertionError: assert True is False
E   +  where True = AgentReport(..., status='verified', ...,
E            verified=True, reason='submitted', ...).verified

# повтор одобренной проверки после tests.run — replay, а не исполнение:
E  assert 'reused_from' not in {..., 'reused_from': {'call_id': 'c2', 'step': 2}, ...}

# число реальных исполнений (файл-счётчик внутри проверки):
E  AssertionError: 1 real executions, expected 3
```

Единственный зелёный в этом наборе — Mission 01 regression test: probe-путь был
закрыт ещё `67271be` и остаётся закрытым.

### 5.2. Green после фикса (финальное дерево, один процесс за раз)

| Команда | Результат |
|---|---|
| `python -m pytest tests/test_engine_kernel.py -q -o addopts=""` | **100 passed** (120.15 s) |
| `python -m pytest tests/test_run_reuse_identity.py tests/test_completion_gate.py tests/test_requirement_coverage.py tests/test_engine_tool_env_render.py tests/test_engine_tools.py tests/test_tool_shape_fixes.py -q -o addopts=""` | **95 passed** (21.66 s) |
| `python -m pytest tests/test_auto_verify.py tests/test_python_tests.py tests/test_core_commands.py tests/test_repo_command_safety.py tests/test_engine_port_finish.py tests/test_engine_port_context.py tests/test_engine_port_progress.py tests/test_engine_port_salvage.py tests/test_tree.py -q -o addopts=""` | **99 passed** (9.41 s) |
| `python -m pytest tests/test_engine_kernel.py -q -o addopts="" -k "<те же 7 маркеров>"` | **10 passed** (25.21 s) |
| `python -m ruff check src/karox/engine_tool_env.py src/karox/completion_gate.py tests/test_engine_kernel.py` | All checks passed |
| `python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui` | Success: no issues found in 13 source files |
| `git diff --check` | чисто (exit 0) |

Итого **294** уникальных теста (100 + 95 + 99; набор из 10 входит в первый
прогон): **0 failed, 0 skipped**. Полный `pytest` не запускался (см. §5.5).

### 5.3. Что именно проверяют новые тесты

1. `test_a_test_run_that_writes_a_hidden_dependency_is_not_verified` — полный
   сценарий из §2: write → approved check PASS → `tests.run` пишет `spec.txt` и
   проходит → повтор проверки → finish. Требование: `verified is False`, у
   повтора нет `reused_from`, `exit_code == 3`, байты `state=bad`.
2. `test_a_test_run_that_writes_a_hidden_dependency_ends_check_reuse` — тот же
   вход + repair (`repo_write_file spec.txt`), затем новая **настоящая**
   успешная проверка и `verified is True`; у проверок нет `reused_from`.
3. `test_a_failed_test_run_that_wrote_a_dependency_ends_check_reuse` — `tests.run`
   падает (exit 1) уже после записи; повтор всё равно обязан исполниться.
4. `test_a_timed_out_test_run_that_wrote_a_dependency_ends_check_reuse` —
   `tests.run timeout_seconds=3` с зависающим тестом: `timed_out is True`,
   запись уже произошла, повтор обязан исполниться. (Core-уровень отдельно
   подтверждён пробой без модели: `timed_out=true, exit_code=null`,
   `spec.txt=state:bad`.)
5. `test_an_approved_check_whose_own_write_changes_its_verdict_runs_again` —
   буквальный approved argv пишет ignored `state.dep` и проходит только один раз;
   повтор обязан исполниться и упасть (exit 3), прогон — `False`.
6. `test_every_identical_check_executes_and_the_count_is_visible` — политика:
   три одинаковые проверки с `repo.read_file` между ними дают ровно три реальных
   исполнения (счётчик — сам файл-побочка, а не маркер `reused_from`).
7. Переписаны: `…unchanged_bytes_runs_again`, `…read…still_runs_the_check`,
   `…failed_edit…still_runs_the_check` — повтор всегда исполняется, нормальное
   завершение (`write → approved check → finish`, `verified: True`) не сломано.
8. Mission 01 test (`test_a_probe_that_writes_an_untracked_dependency_ends_check_reuse`)
   продолжает проходить: probe-повтор исполняется и видит `state=bad`.

### 5.4. Replay/resume-контракт

Не менялся и не требует нового теста: кэш вердиктов жил только в памяти
`CoreToolEnvironment` (создаётся заново на каждый прогон, `engine_kernel.py:1036`),
durable-записи о нём не было. Resume-прогон и раньше не мог переиграть вердикт
из прошлого процесса; после фикса — не может тем более. Существующие
детерминированные resume-тесты (`test_engine_resume_continues_from_durable_checkpoint`,
`test_resumed_transcript_keeps_tool_results_next_to_their_calls`,
`test_engine_resume_synthesizes_results_for_interrupted_calls`) входят в 100
passed.

### 5.5. Что не запускалось

- полный `python -m pytest` — в наборе есть live browser/3D/desktop-тесты;
  задание запрещает автоматический полный прогон без оценки нагрузки;
- live smoke с провайдером/моделью и платные API — **0 вызовов**;
- Chromium/Playwright/3D-арена, bridge/tunnel, Docker/WSL, benchmark — **0**;
- `python -m build --wheel` — не запускался (изменение не трогает упаковку, а
  build оставил бы `dist/`/egg-info в дереве); это ограничение проверки, а не
  «build passed»;
- `python -m ruff check src tests scripts` — запускался: **3 pre-existing F401**
  в файлах, которые это изменение не трогает (`src/karox/session_export.py:24`,
  `tests/test_tool_shape_fixes.py:10,20`; те же строки есть в `HEAD`). Мои три
  файла проходят чисто. Это отдельный pre-existing красный release-gate.

## 6. Economy

Шесть синтетических офлайн-последовательностей, один и тот же вход (запись
deliverable + одобренная проверка), счёт — фактические исполнения `checks.run` /
`tests.run` (не `reused_from`-маркеры, кроме случаев, где он и есть наблюдение):

| Сценарий | Реальных запусков до | после | Переигранных до | после |
|---|---|---|---|---|
| identical check ×2 | 1 | 2 | 1 | 0 |
| check → read → check | 1 | 2 | 1 | 0 |
| check → failed edit → identical write → check | 1 | 2 | 1 | 0 |
| check ×3 с read между ними (файл-счётчик) | 1 | 3 | 2 | 0 |
| check → tests.run (пишет) → check → repair → check | 3 | 4 | 1 | 0 |
| probe → check (Mission 01) | 4 | 4 | 0 | 0 |
| **Итого** | **11** | **17** | **6** | **0** |

- **Точность `VERIFIED` при скрытой мутации:** до — 4 из 4 hidden-mutation
  сценариев давали `verified: True` при падающей одобренной проверке; после —
  0 из 4 (все четыре заканчиваются `verified: False`, а repair-путь снова даёт
  честный `VERIFIED`).
- **Reuse hit/miss на неизменённых байтах:** до — 6 hits в этих шести
  последовательностях; после — 0 hits (политика), 17 реальных исполнений, 0
  переигранных вердиктов.
- **Длительность узких тестов:** red-прогон 22.44 s, green-прогон 25.21 s на том
  же наборе (разница ≈ дополнительно исполненные команды; ориентир, не
  сравнение сред). Полный `tests/test_engine_kernel.py` — 120.15 s.
- **Запросы к модели:** +0. Кэш никогда не экономил запросы: повтор проверки —
  это tool-вызов, а не новый provider-раунд.

Цена честная и измеренная: повторная команда стоит wall clock, а не токены, а
`timeout`/бюджет прогона теперь может закончиться там, где раньше кэш «экономил»
минуту. Это сознательный обмен в пользу корректности `VERIFIED`.

## 7. Residual risks (НЕ закрыто этой миссией)

1. **Одобренная проверка, которая сама портит дерево и возвращает 0.** Если
   финальное настоящее исполнение пишет вредные байты и выходит с кодом 0,
   гейт всё равно верит команде оператора: `VERIFIED` останется, а дерево будет
   испорчено. Это уже не stale-cache, а «лгущая проверка»; отдельный класс,
   требует другого механизма (детект записи внутри прогона), не reuse-кэша.
2. **`git.diff` partial/staged review.** `floor` принимает любой успешный
   `git.diff` с текущей identity (`completion_gate.py:863-867`), включая
   `staged=true` (`--cached`, пустой unstaged diff) и `paths=[...]`
   (частичный diff, `core_tools.py:…_git_diff_paths`). Если это единственный
   review после последнего edit, harness может не запустить полный `git.diff`,
   потому что `_verification` уже истинно. Это Mission 03, в этот коммит
   сознательно не включено (задание §3).
3. **`check_tree` ceiling (pre-existing).** `scripts/check_tree.py:58`
   `MAX_LINES = 80_000`, фактически **86 174** строк пакета (на `040882d` было
   86 403; это изменение убрало 229 строк net: −237 в `engine_tool_env.py`,
   +8 в докстринге `completion_gate.py`). `tests/test_tree.py:158` ожидает
   `< 87_000` и проходит. Рассинхронизация потолков существовала до миссии,
   механически не поднималась; для «release-ready» нужен отдельный осознанный
   коммит (Mission 03).
4. **`ruff` pre-existing.** `src/karox/session_export.py:24` и
   `tests/test_tool_shape_fixes.py:10,20` — неиспользуемые импорты в `HEAD`; не
   мои файлы, не трогал (не менять unrelated production modules).
5. **Нейтральные исполнители вне кэша.** `lsp.diagnostics` запускает language
   server в cwd репозитория и мог менять входы; `git.diff` может исполнять
   `diff.<driver>.textconv` из локального git config. После удаления кэша это
   больше не может «оживить» старый вердикт, но остаётся исполнением кода в
   дереве — учитывать в будущих механизмах доказательств.
6. **Hosted-путь** (`checks.start`, durable jobs) вызывает core напрямую, без
   `CoreToolEnvironment` и без этого кэша; гейт `VERIFIED` к нему не относится,
   но фоновая команда может писать в репозиторий после возврата вызова. Это
   свойство hosted-режима, не регрессия миссии.
7. **No-progress эвристика.** Реальные повторные прогоны дают разные тексты
   (например, тайминги pytest), поэтому «repeated identical step» теперь реже
   срабатывает на одинаковых проверках; шаговый/временной бюджет по-прежнему
   ограничивает цикл. Это поведенческое следствие, не риск ложного `VERIFIED`.
8. **`visual.check`** не входит в `_REUSABLE_RUNS` и не переигрывался; его proof
   по-прежнему привязан к identity через `completion_gate.py:930-939`, не
   менялся.

## 8. Decision

**ACCEPT CANDIDATE.** Причина установлена и воспроизведена до фикса (4
`VERIFIED` на неверных байтах), исправление минимально в рамках cache/epoch,
не переписывает `completion_gate` и не меняет семантику `VERIFIED`, red→green
подтверждён реальными исполнениями, цена измерена (11 → 17 исполнений, 0
дополнительных запросов к модели). Остаточные риски §7 не относятся к классу
«stale reuse»; два из них (`git.diff` scope, `check_tree` ceiling) — известные
блокеры следующих миссий и названы честно.

## 9. Next mission

**Mission 03 (одна задача):** сделать «change review» доказательством всего
финального дерева, а не произвольного `git.diff`: результат `staged=true` или
`paths=[...]` не должен закрывать обязательство review (или должен нести
owner-подписанный scope, проверяемый в `floor`), плюс офлайн-тест, где staged или
частичный diff после edit не даёт `VERIFIED`, а полный unstaged diff даёт.
`check_tree` ceiling синхронизировать отдельным коммитом после этого.

---

## Machine-readable summary

```yaml
mission: dsh-reuse-integrity-20261106
repository: "D:\\проекты\\KaroX-v5\\lab\\kernel-duel-20261025\\b\\win"
starting_branch: "audit/dsh-verified-20261106"
work_branch: "audit/dsh-reuse-integrity-20261106"
starting_head: "040882d08fbac5fdd5ce7e31d198ff061b56fb38"
code_commit: "f831a9a2201264cfc941bf0c29760e6a55e583f6"
report_commit: "docs-only follow-up commit containing this file"
working_tree_clean: true
root_cause_status: confirmed
reproducible_before_fix: true
false_verified_scenarios_before: 4
false_verified_scenarios_after: 0
cache_removed_surfaces: ["checks.run", "tests.run"]
minimal_fix_applied: true
production_files_changed: ["src/karox/engine_tool_env.py", "src/karox/completion_gate.py"]
tests_added: 6
tests_rewritten: 3
targeted_tests_passed: 294
targeted_tests_failed: 0
targeted_tests_skipped: 0
full_suite_run: false
real_executions_before: 11
real_executions_after: 17
replay_hits_before: 6
replay_hits_after: 0
model_requests_added: 0
benchmark_runs: 0
paid_api_calls: 0
push_performed: false
merge_performed: false
release_ready: false
remaining_blockers:
  - "git.diff staged/partial review can discharge the change-review obligation (Mission 03)"
  - "scripts/check_tree.py ceiling 80000 vs 86174 lines (tests/test_tree.py expects < 87000)"
  - "three pre-existing ruff F401s in files untouched by this mission"
hitl_required: false
report_path: docs/audits/DSH_REUSE_INTEGRITY_20261106.md
next_milestone: "make the change review prove the whole final tree: staged or partial git.diff must not discharge the review obligation, with an offline engine test"
```
