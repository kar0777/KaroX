# DSH Mission 01 — Verification reliability audit (2026-11-06)

**Verdict.** Проверяемая гипотеза «gate принимает устаревшее *semantic evidence*» на текущем HEAD **не подтверждена**: поиск, чтение и `git.diff`-рассуждение не являются доказательством завершения, а единственный semantic-слой (attestation по требованиям) с 2026-11-05 полностью advisory. Зато найден и воспроизведён **соседний дефект того же класса**: одобренная проверка может быть переиграна из кэша reuse после того, как probe изменил не отслеживаемый identity файл, и её `completion_identity` перепривязывается к новым байтам. В результате прогон заявляет `VERIFIED`, хотя одобренная проверка на финальных байтах падает. Дефект исправлен минимально, regression test красный до фикса и зелёный после. Полный suite и платные API не запускались; `scripts/check_tree.py` красный **до** этого изменения (см. §4).

---

## 1. Repository reality

| Поле | Значение |
|---|---|
| Абсолютный путь | `D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\win` |
| Git top-level | тот же путь (отдельный worktree, `.git` — файл) |
| Исходная ветка | `v6/win-20261105` |
| Исходный HEAD | `ae9f96b011e697abed8eff27c5c9c79e05bea07d` |
| Рабочая ветка | `audit/dsh-verified-20261106` (создана от HEAD; не пушилась) |
| Проверенный code head | `67271be59b3bafe11089c8dc248f2e444bf0e345` |
| Коммиты | `67271be` — fix + regression tests; следом docs-only коммит с этим отчётом |
| Чистота | на старте `git status --short` пуст; на code head пуст; на финале — пуст после коммита отчёта |
| Другие деревья | не изменялись; push, reset, clean, удаления и установки не выполнялись |

Проверено до изменений:

```text
git rev-parse --show-toplevel  -> D:/проекты/KaroX-v5/lab/kernel-duel-20261025/b/win
git branch --show-current      -> v6/win-20261105
git rev-parse HEAD             -> ae9f96b011e697abed8eff27c5c9c79e05bea07d
git status --short             -> (пусто)
```

`AGENTS.md`, `README.md`, `docs/DEVELOPMENT.md`, `docs/ARCHITECTURE.md` прочитаны; реальные модули и точки входа (`engine_kernel.py`, `engine_tool_env.py`, `completion_gate.py`, `requirement_coverage.py`, `session_analysis.py`) подтверждены по исходникам. Секреты не читались и не печатались; `.env`, keyring и конфигурации с токенами не открывались.

## 2. Root cause

### 2.1. Исходная гипотеза: **опровергнута**

Гипотеза: «агент получил semantic evidence (поиск старого имени, проверка всех use-site, semantic review), затем изменил файлы; старое evidence осталось принято, и gate выдал `VERIFIED`».

На HEAD это не так:

- `repo.search`, `repo.read_file`, `repo.read_lines`, `repo.inspect`, `repo.list_files`, `git.status`, `git.log` **не входят** в доказательства `completion_gate.floor` (`completion_gate.py:811-1056`). Они нужны только для `answer_basis` у read-only ответа, когда `record.changed_files` пуст (`session_analysis.py:267-348`); при любом изменении файлов этот путь закрыт (`engine_kernel.py:2176-2192`).
- «Change review» — это `git.diff`, привязанный к `completion_gate.identity()` (`completion_gate.py:670-707`, `:863-867`). Identity хеширует task, ledger требований, все changed/read файлы; изменение байтов меняет identity, и старый review перестаёт быть текущим. Это закреплено новым counter-case тестом `test_a_review_of_old_bytes_is_not_current_after_an_edit` (`tests/test_completion_gate.py:567`).
- Attestation по требованиям с коммита `44c46f1` **advisory**: `ATTESTATION_GATES_VERIFICATION = False` (`requirement_coverage.py:63`), `COVERAGE_REFUSAL_LIMIT = 0` (`engine_tool_env.py:197`), а `floor_reasons(gating_only=True)` оставляет только собственный `unmet` модели. Устаревшая цитата даёт advisory-строку, но сама по себе не может включить `VERIFIED`.
- Оба «ложных VERIFIED» из benchmark-документа объяснены владельцем как разброс выборки модели (`docs/benchmarks/sbx-20261105.md`, разделы f1 `swe-pytest-10356` и f3 `eng-diff-patch`); в их сессиях механизм «старое semantic evidence принято» не зафиксирован.

Иными словами, gate не принимает semantic-рассуждение как proof — он требует прохождения проверки/визуального вердикта на текущих байтах. Это и есть корректное поведение; добавлять поиск в proof нельзя (и задание это прямо запрещает).

### 2.2. Подтверждённый дефект: reuse переигрывает проверку после probe, изменившего untracked dependency

Точная цепочка (все звенья воспроизведены офлайн):

1. `checks.run` входит в `_RUN_NEUTRAL_TOOLS` (`engine_tool_env.py:63-90`), поэтому **probe** не увеличивает `_state_epoch`.
2. Probe допускается `probe_python_command` (`core_process.py:232-275`) и исполняет произвольный код в рабочем каталоге репозитория; результат помечен `probe: True` (`core_process.py:940-941`), но файлы, которые probe записал, **не попадают** в `record.changed_files` (`core.py:1204-1216` — для `checks.run` пишется только запись проверки).
3. Ранее прошедшая одобренная проверка сохраняется в `_passed_runs` (`engine_tool_env.py:1374-1412`) с identity, покрывающей только changed/read файлы.
4. Probe пишет файл, который прогон не читал и не менял (в тесте — `spec.txt`). `identity()` (`completion_gate.py:670-707`) его не содержит; `same_run_inputs` (`:710-744`) видит те же байты и разрешает reuse.
5. Повторная одобренная проверка попадает в `_reuse_passed_run` (`engine_tool_env.py:1415-1493`): возвращает старый `CoreResult`, ставит `reused_from` (`:1444`) и **перепривязывает** `completion_identity` к текущему identity (`:1454-1456`). Проверка реально не исполнялась.
6. `floor` принимает перепривязанный результат (`completion_gate.py:930-939`); harness добивает evidence chain чтением `git.status`/`git.diff` (`engine_kernel.py:1892-1970`), `_completed` истинно (`:2206-2211`), отчёт — `verified: true`.

Это ровно альтернатива из задания «последняя мутация не инвалидирует нужную проверку», только мутацию выполнил probe, а не обычный edit. Ошибка системная: она не зависит от формулировки задачи, языка, фреймворка или конкретного benchmark. Она также объясняет, почему модель может «поверить» зелёной проверке: инструмент сам пишет `[checks.run exit 0 · reused]` и «nothing that a run can see has changed since».

## 3. Evidence

### 3.1. Ключевые места

| Файл:строка | Роль |
|---|---|
| `src/karox/engine_tool_env.py:59` | `_REUSABLE_RUNS = {"checks.run", "tests.run"}` |
| `src/karox/engine_tool_env.py:63-90` | `_RUN_NEUTRAL_TOOLS`; probe здесь считается нейтральным |
| `src/karox/engine_tool_env.py:337-352` | новый `_ran_unchecked_code`: probe/inline-wildcard = произвольный код |
| `src/karox/engine_tool_env.py:923-928` | новый epoch-bump после сохранения pass: отменяет reuse всех более ранних проверок |
| `src/karox/engine_tool_env.py:1374-1412` | `_remember_passed_run` — снимок identity и epoch |
| `src/karox/engine_tool_env.py:1415-1493` | `_reuse_passed_run` — replay + перепривязка identity |
| `src/karox/core_process.py:232-275` | `probe_python_command`: `-c`, `-m`, repo-скрипт допускаются как probe |
| `src/karox/core_process.py:940-941` | `result["probe"] = True` |
| `src/karox/core_process.py:986-1014` | `_run_inline_check`: код исполняется из temp-файла с cwd репозитория |
| `src/karox/completion_gate.py:670-707` | `identity()` — только changed/read/requirement-source файлы |
| `src/karox/completion_gate.py:710-744` | `same_run_inputs` — проверяет только файлы прежнего identity |
| `src/karox/completion_gate.py:811-1056` | `floor`; `:863` — review; `:930` — check identity |
| `src/karox/requirement_coverage.py:63` | attestation advisory |
| `src/karox/engine_tool_env.py:197` | `COVERAGE_REFUSAL_LIMIT = 0` |
| `tests/test_engine_kernel.py:3470` | новый regression test |
| `tests/test_completion_gate.py:567` | counter-case: review старых байтов не текущий |

### 3.2. Пример до/после (без секретов)

Fixture: закоммичен `spec.txt` = `state=ok`; одобренная проверка читает `spec.txt` и выходит 0 только при `state=ok`. Модель пишет `hello.txt`, проверка проходит. Затем probe:

```python
("python", "-c", "open('spec.txt','w',encoding='utf-8').write('state=bad\\n')")
```

и та же одобренная проверка вызывается ещё раз.

**До фикса** (scratch-прогон на `ae9f96b`; в нём файл назывался `config.json`, в regression test — `spec.txt`):

```text
verified: True
config.json: {"ok": false}          # тот же смысл: spec.txt = state=bad
check ... reused= {'call_id': 'c2', 'step': 2}   exit= 0
```

Проверка не запускалась, её вердикт `exit 0` перепривязан к текущим байтам; `floor` пуст; итог `VERIFIED`.

**После фикса**:

```text
verified: False
check ... reused= None   exit= 3
```

Третья проверка реально исполнилась и увидела `state=bad`, поэтому прогон не может закрыться `VERIFIED`. В regression test (с корректным repair-ходом) модель восстанавливает `spec.txt`, повторная проверка проходит, и только тогда отчёт `verified: true`; `reused_from` ни у одной из этих проверок нет. Это доказывает, что фикс не ломает нормальное завершение, а только запрещает replay поверх изменённых untracked байтов.

## 4. Tests

Все команды запускались **последовательно**, без `-n`, без браузера, без live-моделей; каждая — с `-o addopts=""`, чтобы получить итоговый счётчик.

| Команда | Результат |
|---|---|
| `python -m pytest tests/test_engine_kernel.py -q -o addopts=""` | **94 passed** (101.89 s) |
| `python -m pytest tests/test_completion_gate.py tests/test_requirement_coverage.py tests/test_run_reuse_identity.py tests/test_engine_tool_env_render.py tests/test_engine_tools.py tests/test_tool_shape_fixes.py -q -o addopts=""` | **95 passed** (21.53 s) |
| `python -m pytest tests/test_auto_verify.py tests/test_python_tests.py tests/test_core_commands.py tests/test_repo_command_safety.py tests/test_engine_port_finish.py tests/test_engine_port_context.py tests/test_engine_port_progress.py tests/test_engine_port_salvage.py -q -o addopts=""` | **93 passed** (8.19 s) |
| `python -m pytest tests/test_tree.py -q -o addopts=""` | 6 passed (0.94 s) |
| `python -m ruff check src/karox/engine_tool_env.py tests/test_engine_kernel.py tests/test_completion_gate.py` | All checks passed |
| `python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui` | Success: no issues found in 13 source files (одно informational note об unused overrides) |
| `git diff --check` | чисто |

**Красный до фикса по правильной причине.** На `ae9f96b` новый тест падал так:

```text
assert "reused_from" not in after_probe["data"]
E  assert 'reused_from' not in {..., 'reused_from': {'call_id': 'c2', 'step': 2}, ...}
tests/test_engine_kernel.py:3525: AssertionError
```

То есть повторная проверка была именно replay-ем, а не исполнением. После фикса — `1 passed`, затем весь `test_engine_kernel.py` — 94 passed.

**Не запускалось (и не заявляется как пройденное):**

- полный `python -m pytest` — в наборе есть live browser/3D/desktop тесты; задание запрещает автоматический полный прогон без оценки нагрузки;
- live smoke с провайдером/моделью, платные API — 0 вызовов;
- 3D-арена, Chromium/Playwright live, bridge/tunnel и load-тесты — 0;
- `python -m build --wheel` — не запускался: изменение не трогает упаковку, а build оставил бы `dist/`/egg-info в дереве; это явно ограничение проверки, а не «build passed»;
- benchmark-прогоны — 0.

**Существующий красный гейт (не вызван этим изменением).** `python scripts/check_tree.py` завершается кодом 1:

```text
check_tree: package grew to 86403 lines (ceiling 80000)
```

На исходном `ae9f96b` было 86 374 строки — тоже больше 80 000. `tests/test_tree.py:158` при этом ожидает `< 87_000` и проходит. Это рассинхронизация потолков (тест и комментарий поднимали потолок до 86k/87k, а `MAX_LINES` в `scripts/check_tree.py` остался 80k), существовавшая до аудита. Я её не правил, чтобы не смешивать несвязанное изменение с фиксом; она перечислена в §7.

## 5. Changes

Один атомарный локальный коммит с fix + tests: `67271be59b3bafe11089c8dc248f2e444bf0e345`.

| Файл | Δ | Назначение |
|---|---|---|
| `src/karox/engine_tool_env.py` | +29 | `_ran_unchecked_code` и epoch-bump после probe/inline-wildcard check: любая более ранняя pass-запись перестаёт быть reuse-кандидатом; комментарий к `_RUN_NEUTRAL_TOOLS` объясняет исключение |
| `tests/test_engine_kernel.py` | +66 | `Harness(allow_probes=...)` и `test_a_probe_that_writes_an_untracked_dependency_ends_check_reuse`: probe пишет `spec.txt`, повторная проверка обязана исполниться (exit 3), repair-ход восстанавливает файл, финальная проверка проходит |
| `tests/test_completion_gate.py` | +38 | `test_a_review_of_old_bytes_is_not_current_after_an_edit`: review, привязанный к старым байтам, перестаёт быть текущим после edit (counter-case, проходит и на старом коде) |
| `docs/audits/DSH_VERIFIED_AUDIT_20261106.md` | new | этот отчёт (docs-only follow-up commit) |

`git status` на code head чист; на финале чист после коммита отчёта. Никакие другие файлы не менялись.

## 6. Risk

- **Дополнительные исполнения проверок.** После probe (и после inline-проверки, допущенной wildcard-правилом) ранее прошедшая одобренная проверка больше не берётся из кэша и исполняется ещё раз. Это стоимость CPU/времени, но **не** дополнительный model turn: модель не вызывается. Нормальная последовательность «edit → approved check → finish» и «read → identical check» по-прежнему использует reuse (`test_an_identical_passing_check_on_unchanged_bytes_is_reused`, `test_a_read_between_identical_checks_still_reuses_the_pass`).
- **Правила `VERIFIED` не ослаблены и не усилены для обычных прогонов.** Проверка всё так же обязана пройти на текущих байтах; visual/read-only пути не менялись. Изменение лишь не позволяет подменить реальный запуск replay-ем после произвольного кода.
- **Probe остаётся probe.** Он по-прежнему не является evidence (`verification_eligible=False`) и не даёт `VERIFIED`; исправление не добавляет self-reported text в proof.
- **Зависимость от маркеров.** Фикс срабатывает по `result.data["probe"]` / `["inline_code_from_file"]`. Если появится новый путь допуска произвольного кода без этих маркеров, дефект вернётся; regression test зафиксирует только текущие две формы.
- **`tests.run` и `git.diff`-scope не изменены** (см. §7): это сознательно оставленный периметр минимального фикса.

## 7. Remaining issues

1. **`scripts/check_tree.py` красный до аудита.** `MAX_LINES = 80_000` (`scripts/check_tree.py:58`), фактически 86 403 строки, при `< 87_000` в `tests/test_tree.py:158`; на `ae9f96b` уже было 86 374. Это существующая рассинхронизация release-gate, не связанная с фиксом; для «release-ready» её нужно синхронизировать отдельным осознанным коммитом.
2. **`tests.run` — тот же класс дыры.** `tests.run` входит и в `_REUSABLE_RUNS`, и в `_RUN_NEUTRAL_TOOLS` (`engine_tool_env.py:59,80`). Тесты репозитория могут писать untracked fixture/config; после такого прогона повторная одобренная проверка всё ещё может быть replay-нута. Фикс не расширен на `tests.run`, чтобы не убирать измеренную оптимизацию без отдельного решения.
3. **`git.diff` review не проверяет полноту.** `floor` принимает любой successful `git.diff` с текущим identity (`completion_gate.py:863`), включая `staged=true` (пустой unstaged diff) и `paths=[...]` (частичный diff, `core_tools.py:1460-1493`). Если это единственный review после последнего edit, harness может не запустить свой полный `git.diff`, потому что `_verification` уже истинно. Это не stale-байты, но неполное review.
4. **Semantic-покрытие требований не гейтит.** `ATTESTATION_GATES_VERIFICATION = False` и `COVERAGE_REFUSAL_LIMIT = 0`: для web-задачи требование, не покрытое ни одной проверкой, может остаться невыполненным, а прогон — `VERIFIED`, если модель не написала `unmet`. Это осознанный компромисс после diag1/smoke1, но остаётся false-positive риск; чинить его простым «снова включить attestation» нельзя (вернутся ложные unverified).
5. **Неотслеживаемые пути записи вне default-профиля.** `dev.command` (только `ELEVATED`, `policy.py:66-84`) и фоновые `checks.start`/`dev.command_start` могут писать в репозиторий без `record.changed_files`; evidence-chain не сбрасывается на `dev.command` (он не в `MUTATION_TOOLS`). В `WORKSPACE_WRITE` (дефолт CLI) `dev.command` недоступен, поэтому это риск для elevated/bypass-прогонов, но не для обычного. Плюс `is_scratch_artifact` (`web_check.py:1352`) исключает из identity реальные файлы с именами вроде `tmp_utils.py`; это узкая, но подтверждённая эвристика.

## 8. Next engineering milestone

**Один приоритетный шаг:** расширить то же правило «произвольное исполнение завершает reuse» на `tests.run` (и на `dev.command`, `skill.run`, `checks.start`, когда они доступны), затем добавить офлайн engine-тест, в котором тест-раннер пишет untracked fixture, а повторная одобренная проверка обязана исполниться. Это закрывает остаток класса, не добавляя semantic-сканеров и не ослабляя `VERIFIED`. После этого — отдельно — синхронизировать `check_tree.py` с фактическим потолком и `tests/test_tree.py`.

---

## Machine-readable summary

```yaml
mission: dsh-verified-quality-20261106
repository: "D:\\проекты\\KaroX-v5\\lab\\kernel-duel-20261025\\b\\win"
starting_branch: "v6/win-20261105"
work_branch: "audit/dsh-verified-20261106"
starting_head: "ae9f96b011e697abed8eff27c5c9c79e05bea07d"
final_head: "67271be59b3bafe11089c8dc248f2e444bf0e345"
working_tree_clean: true
root_cause_status: confirmed
reproducible_before_fix: true
minimal_fix_applied: true
targeted_tests_passed: 282
targeted_tests_failed: 0
full_suite_run: false
commits: ["67271be59b3bafe11089c8dc248f2e444bf0e345"]
benchmark_runs: 0
paid_api_calls: 0
report_path: docs/audits/DSH_VERIFIED_AUDIT_20261106.md
next_milestone: "end reuse on every non-literal approved execution surface (tests.run first) and add an offline engine test for a test-written untracked fixture"
```

`final_head` — проверенный code head: отчёт добавлен отдельным docs-only коммитом после него. `working_tree_clean: true` подтверждён `git status --short` после коммита отчёта.
