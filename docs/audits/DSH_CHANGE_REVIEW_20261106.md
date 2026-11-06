# DSH Mission 03 — Honest final change review (2026-11-06)

**Verdict.** Дефект подтверждён и воспроизведён офлайн через реальный путь
`CoreToolEnvironment → CoreRuntime`: `completion_gate.floor` принимал любой
успешный `git.diff` с текущей `completion_identity` как «change review», хотя
эта команда показывает только часть изменения. Четыре сценария на исходном коде
заканчивались `VERIFIED` (три — без единого harness-финального Git-чтения, один
— с пустым `git diff`), причём файл, который изменил сам прогон, в review не
появлялся. Внесён один новый Core-режим `git.diff {"complete": true}`: Core сам
сравнивает `HEAD` с рабочим деревом (staged **и** unstaged) и добавляет каждый
новый untracked-файл как new-file diff, а в результат пишет фактически
исполненный охват (`data.review`). `floor` теперь принимает только успешный
review с текущей identity и доверенными метаданными о полном охвате; частичные
diff остаются инструментом чтения, но больше не закрывают обязательство.
Harness получает полный review автоматически один раз, без нового turn'а модели
и с тем же одним вызовом `git.diff`. Полный pytest, wheel, live-модели,
браузер/3D, bridge/tunnel и бенчмарки не запускались; `scripts/check_tree.py`
красный **до** этого изменения (см. §6).

---

## 1. Git reality

| Поле | Значение |
|---|---|
| Абсолютный путь | `D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\win` |
| Git top-level | тот же путь (linked worktree, `.git` — файл) |
| Исходная ветка | `audit/dsh-reuse-integrity-20261106` |
| Исходный HEAD | `b43bfbc6b6bde326b187ab18fa54035d58938efb` |
| Состояние на старте | `git status --short` пуст |
| Рабочая ветка | `audit/dsh-change-review-20261106` (создана от HEAD) |
| Code commit | `b9911ae4e586a1cc67cb5003d193369560bd8477` |
| Report commit | отдельный docs-only коммит с этим файлом и записью в `CHANGELOG.md` |
| Финал | `git status --short` пуст после коммита отчёта |
| Другие деревья | не изменялись; push, merge, rebase, reset, clean, удаления, установки не выполнялись |

Проверено до любых изменений:

```text
git rev-parse --show-toplevel  -> D:/проекты/KaroX-v5/lab/kernel-duel-20261025/b/win
git branch --show-current      -> audit/dsh-reuse-integrity-20261106
git rev-parse HEAD             -> b43bfbc6b6bde326b187ab18fa54035d58938efb
git status --short             -> (пусто)
git log -5 --oneline
  b43bfbc docs: DSH mission 02 reuse-integrity audit (2026-11-06)
  f831a9a engine: a check or test that executes code never answers from a cache
  040882d docs: DSH mission 01 verification audit (2026-11-06)
  67271be engine: arbitrary-code probes end approved-check reuse
  ae9f96b v6: the closing look judges the run's own page; ...
```

Задание ожидало ровно эту ветку и HEAD — расхождений нет, поэтому ветка создана
сразу. Прочитаны `AGENTS.md`, `docs/DEVELOPMENT.md`, оба отчёта Mission 01/02,
`docs/CHANGELOG.md` и исходники перечисленных ниже модулей. Секреты, `.env`,
keyring и приватные конфиги не читались и не печатались.

## 2. Что именно небезопасно (доказано, не предположено)

Команда `git.diff` имеет четыре разных охвата, и все они возвращали одну и ту же
`completion_identity` (она хеширует task, ledger и changed/read-файлы, но не
описывает, что показала команда):

| # | Команда | Что реально показывает | Опасность |
|---|---|---|---|
| 1 | `git.diff {"paths": ["a.py"]}` | один путь, unstaged | **подтверждено:** run изменил `a.py` и `b.py`, review показал только `a.py` |
| 2 | `git.diff {"staged": true}` | только индекс | **подтверждено:** staged-правка `a.py`; собственный новый файл прогона `c.py` не показан вообще |
| 3 | `git.diff {}` (по умолчанию) | только unstaged | **подтверждено:** при staged-правке `a.py` результат `exit 0` и пустой stdout; `c.py` тоже нет |
| 4 | любой `git.diff` | новые untracked-файлы не показываются | **подтверждено:** новый deliverable `hello.txt` закрывался пустым diff'ом, содержимое ни разу не попало в review |

Ни один из этих результатов не является полным review относительно `HEAD`, но
каждый несёт текущую identity и потому закрывал `floor`. Отдельно проверены
крайние случаи:

- **failed/refused `git.diff`** — не доказательство: `floor` и раньше отбрасывал
  `ok is False`; новый предикат дополнительно требует `review.complete is True`.
  Покрыто unit-тестом.
- **пустой diff** — опасен именно как случай 3: `exit 0` + пустой stdout не
  доказывает, что что-то просмотрено. Теперь пустой *полный* review допустим
  только как «изменений относительно `HEAD` нет» и не выдаётся за просмотр
  untracked-файлов (они добавляются отдельно).
- **truncated diff** — при превышении потолка захвата (1 МБ) середина вывода
  вырезается; такой review не может утверждать, что показал всё изменение.
  Новый предикат требует `review.truncated is False`, иначе run не верифицируется
  (честный false negative, см. §8).
- **untracked-файлы** — по умолчанию не видны ни в одном diff; `git.status`
  показывает только имя. Исправлено добавлением new-file diff'ов (см. §5), с
  оговорками для бинарных/слишком больших файлов (§8).

## 3. Красное воспроизведение на исходном коде

На копии исходного дерева (`git archive b43bfbc` во временный каталог, чтобы не
трогать рабочее дерево) с финальными тестовыми файлами:

```text
python -m pytest tests/test_engine_kernel.py -q -o addopts="" \
  -k "does_not_discharge_the_change_review or empty_unstaged_diff_is_not_a_review
      or complete_review"
-> 7 failed, 100 deselected in 9.67s
```

```text
python -m pytest tests/test_completion_gate.py -q -o addopts="" \
  -k "incomplete_diff or complete_review_of_the_current_bytes or excluded_path
      or failed_diff_is_never or legacy_diff_without_scope or review_of_old_bytes"
-> 9 failed, 5 passed, 33 deselected in 0.42s
```

Красные по правильной причине. Типичные ассерты:

```text
# путь отфильтрован, полного review в истории нет:
E  AssertionError: {}
E  assert None == 'complete'

# review старой версии после нового edit — второго полного review не появилось:
E  AssertionError: []
E  assert 0 == 2

# неполный diff не должен закрывать floor (9 параметров, включая
# unstaged-only, staged-only, path-filtered, truncated, legacy-без-метаданных):
E  assert 'missing current change review' in ['missing current structural evidence: pkg/mod.py', ...]
```

Пять зелёных в `test_completion_gate.py` — положительные контроли (полный review
принимается при `base=HEAD` и `base=index`), уже существующий guard для failed
diff, excluded-путь, который прогон не менял, и старый stale-review тест: он
проходит и на исходном коде, потому что проверяет смену identity после edit, а не
охват diff'а (после фикса он обновлён на полный review).

**Фактическое поведение до/после на тех же scripted-прогонах** (реальный
`Harness → ExtendedCoreRuntime`, временные Git-репозитории, без модели и сети):

```text
OLD (b43bfbc)
partial-paths:       verified=true, requests=5, diffs=[paths=["a.py"]], harness_final=[]
staged-only:         verified=true, requests=4, diffs=[staged],          harness_final=[]
default-unstaged:    verified=true, requests=4, diffs=[stdout=""],       harness_final=[]
new-untracked:       verified=true, requests=3, diffs=[stdout=""],       harness_final=[git.status, git.diff]

NEW (b9911ae)
partial-paths:       verified=true, requests=5, diffs=[worktree paths=["a.py"], complete],
                     complete.stdout содержит b.py
staged-only:         verified=true, requests=4, diffs=[staged, complete],
                     complete.stdout содержит "C = 3" (файл самого прогона)
default-unstaged:    verified=true, requests=4, diffs=[worktree stdout="", complete],
                     complete.stdout содержит "C = 3"
new-untracked:       verified=true, requests=3, diffs=[complete],
                     complete.stdout содержит "+hi"
```

То есть до фикса `VERIFIED` получался при неполном review, а строка
`harness_final=[]` показывает, что собственный финальный сбор harness'а был
пропущен (ранний выход `_harness_final_calls` при `_verification == True`).
После фикса каждый прогон получает полный review; при этом число запросов к
модели не изменилось.

## 4. Root cause и точные доверенные метаданные

`completion_gate.floor` брал последний успешный `git.diff` из
`record.provider_history` и сравнивал только
`result.data.completion_identity == completion_gate.identity(core, record)`.
Identity — хеш task, requirement-ledger и байтов changed/read-файлов; она ничего
не говорит о том, **какой срез дерева** показала команда. Все четыре среза
возвращают одну и ту же identity, поэтому partial-review закрывал
обязательство. Дальше `_verification(record)` становился истинным, и
`_harness_final_calls` возвращал `()` — финальные `git.status`/`git.diff`
harness'а не исполнялись, хотя evidence-chain (write → check → status → diff)
формально была замкнута самим моделью.

Ядро проблемы: доказательство описывало *байты известных файлов*, но не
*охват наблюдения*. Модель не могла подделать identity (её пишет Core), но могла
выбрать узкую команду, которая эту identity сохраняет.

Теперь Core пишет в результат то, что исполнил. Реальный результат полного
review (временный репозиторий: staged-правка `tracked.txt` + новый `hello.txt`):

```json
{
  "argv": ["git", "diff", "--no-ext-diff", "HEAD"],
  "paths": [],
  "exit_code": 0,
  "review": {
    "version": 1,
    "mode": "complete",
    "base": "HEAD",
    "paths": [],
    "complete": true,
    "truncated": false,
    "untracked": {
      "listed": 1,
      "shown": ["hello.txt"],
      "limited": [],
      "excluded": [],
      "truncated": false
    }
  }
}
```

`floor` читает только `review` (а не аргумент модели `complete=true`):
`mode == "complete"`, `base in {"HEAD", "index"}`, `paths == []`,
`truncated is False`, `complete is True`; затем проверяет, что
`completion_identity == current` и что review не исключил файл, который прогон
сам изменил (`untracked.excluded ∩ changed_files == ∅`). Старые persisted
результаты без поля `review` (resume старой сессии) fail closed.

## 5. Дизайн и изменённые файлы

Один новый необязательный аргумент Core `git.diff {"complete": true}` и один
доверенный блок метаданных в результате. Никаких task-specific сканеров,
нового FSM, кэшей и изменений правил проверок/визуального proof.

| Файл | Δ | Назначение |
|---|---|---|
| `src/karox/core_git.py` | +257/−7 | `_git_diff_result` (частичные режимы + `review.mode/base/paths`), `_complete_change_review` (`git diff --no-ext-diff HEAD`, либо `--cached` при отсутствии коммита; `git ls-files --others --exclude-standard -z`; новый файл — new-file diff с `+`-строками), `_append_untracked_review`, `_untracked_review_section`/`_untracked_review_text` (границы, redaction, маркеры для binary/oversized/limited), `REVIEW_MAX_UNTRACKED_*` |
| `src/karox/completion_gate.py` | +79/−10 | `_complete_change_review` — предикат доверенного охвата; `_review_skips_a_change` — исключённый файл, который изменил сам прогон, не считается review; `floor` выбирает последний успешный `git.diff` и требует полного охвата |
| `src/karox/core_tools.py` | +25/−8 | `_git_diff_paths`: поддержка `complete`, запрет комбинаций с `staged`/`paths`, делегирование в общий `_git_diff_result`; схема инструмента обновлена |
| `src/karox/core.py` | +13/−1 | базовая схема `git.diff` (для `CoreRuntime` без `ExtendedCoreRuntime`) получает `complete` |
| `src/karox/engine_kernel.py` | +7/−1 | `_harness_final_calls` просит `("git.diff", {"complete": True})` один раз в конце |
| `tests/test_engine_kernel.py` | +329 | 7 end-to-end regression-тестов (см. §6) |
| `tests/test_completion_gate.py` | +177/−40 | 12 unit-тестов предиката охвата + обновлённый stale-review тест |

Почему решение общее и минимальное:

- Охват выводится из **фактически исполненной команды** в доверенном Core
  (`argv`, `paths`, `base`, `truncated`, `untracked`), а не из текста модели или
  её аргумента. Модель может попросить `complete=true`, но не может расширить
  то, что команда реально прочитала.
- Partial/staged/path-filtered diff'ы остаются полезными инструментами: они
  просто не закрывают обязательство review.
- Harness сам добирает недостающий полный review ровно один раз, поэтому
  обычный прогон не получает ни нового model turn'а, ни второго tool-вызова
  `git.diff` (см. §7).
- Tracked-часть — это `HEAD`-relative diff, единственный, который содержит и
  staged, и unstaged правки; для репозитория без коммита базой становится индекс,
  а остальное добирают untracked-файлы.
- Новые файлы показываются как обычные new-file diff'ы (`diff --git`,
  `new file mode`, `+`-строки), а не как «прочитанные файлы»: чужой `repo.read_file`
  не становится доказательством.
- Границы и redaction сохранены: `safe_path` (конфайнмент, запрет symlink),
  `MAX_FILE_BYTES`, `redact` для untracked-байтов (они не проходят через
  `_run`'s stdout-редакцию), лимиты 200 файлов / 20 000 символов на файл /
  400 000 символов на review.
- Identity, `same_run_inputs`, правила checks/visual и Mission 02 (никакого
  кэша вердиктов) не менялись.

Отвергнутые варианты:

- *Поменять поведение `git diff` по умолчанию на `git diff HEAD` и считать это
  полным review* — молча меняет смысл существующего инструмента и всё равно не
  покрывает untracked-файлы.
- *Path-filtered `git diff HEAD -- paths=changed_files`* — не полный review
  дерева и зависит от списка, который передал вызывающий.
- *Показывать все untracked-файлы репозитория без границ* — `node_modules`/
  build-деревья превращают закрывающий review в неограниченное чтение.
- *Отдельный новый core-tool `git.change_review`* — лишняя поверхность и сломал
  бы существующие контракты harness-вызовов; хватило одного флага у `git.diff`.

## 6. Тесты: красное → зелёное

Все команды запускались **последовательно**, без `-n`/xdist, с
`-o addopts=""`, только локальные CPU-тесты.

### 6.1. Красное на исходном коде

См. §3: **7 failed** в `tests/test_engine_kernel.py` и **9 failed** (+5 passed)
в `tests/test_completion_gate.py` на копии `b43bfbc`.

### 6.2. Зелёное на финальном дереве

| Команда | Результат |
|---|---|
| `python -m pytest tests/test_engine_kernel.py -q -o addopts=""` | **107 passed** (145.48 s) |
| `python -m pytest tests/test_completion_gate.py tests/test_core_commands.py tests/test_engine_tools.py tests/test_state_briefing.py tests/test_engine_tool_env_render.py tests/test_engine_port_finish.py tests/test_run_reuse_identity.py tests/test_tool_shape_fixes.py tests/test_argument_validation.py tests/test_requirement_coverage.py -q -o addopts=""` | **141 passed** (27.66 s) |
| `python -m ruff check src/karox/core_git.py src/karox/core_tools.py src/karox/core.py src/karox/completion_gate.py src/karox/engine_kernel.py tests/test_engine_kernel.py tests/test_completion_gate.py` | All checks passed |
| `git diff --check` | чисто |
| `python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui` | Success: no issues found in 13 source files |

Итого **248** уникальных тестов (107 + 141, наборы файлов не пересекаются):
**0 failed, 0 skipped**. Время: 145.48 s + 27.66 s.

### 6.3. Что именно проверяют новые тесты

1. `test_a_path_filtered_diff_does_not_discharge_the_change_review` — два
   изменённых tracked-файла, модель закрывает chain диффом `paths=["a.py"]`;
   финальный review — `complete`, содержит и `a.py`, и `b.py`; model requests
   остаётся 5.
2. `test_a_staged_only_diff_does_not_discharge_the_change_review` — staged
   `a.py` + новый `c.py` прогона; staged-only diff не закрывает review, полный
   содержит и staged-правку, и `+C = 3`.
3. `test_an_empty_unstaged_diff_is_not_a_review_when_the_index_holds_changes` —
   default `git.diff` возвращает `ok=True` с пустым stdout; это не review;
   полный содержит staged-правку и новый файл.
4. `test_a_complete_review_is_enough_and_the_harness_adds_nothing` — модель сама
   просит `complete=true` после check/status; harness ничего не добавляет
   (`harness_final == []`), review показывает `hello.txt` как new file.
5. `test_a_complete_review_works_before_the_first_commit` — свежий `git init`
   без коммита: `base == "index"`, полный review всё равно верифицирует run.
6. `test_a_complete_review_shows_new_files_and_names_ignored_ones` — Core
   действительно пишет охват: `shown=["hello.txt"]`,
   `excluded=["dist/bundle.js"]` для файла в build-каталоге.
7. `test_a_complete_review_before_an_edit_is_retaken_on_the_final_bytes` — review
   первого варианта не принимается после edit; появляется второй полный review
   с финальной identity и `+ho`.
8. `tests/test_completion_gate.py` — параметризованный предикат: 7 неполных
   вариантов (unstaged/staged/path-filtered/scope-incomplete/truncated/
   complete-with-filter/wrong-base) и excluded-deliverable не закрывают floor;
   `HEAD` и `index` принимаются; failed diff не доказательство;
   legacy-результат без `review` fail closed; excluded-путь, который прогон не
   менял, review не блокирует; существующий stale-review тест обновлён под
   полный review.

### 6.4. Что не запускалось

- полный `python -m pytest` — в наборе есть live browser/3D/desktop-тесты;
  задание запрещает автоматический полный прогон;
- live smoke с провайдером/моделью, платные API — **0**;
- Chromium/Playwright, 3D-арена, bridge/tunnel, Docker/WSL, установки — **0**;
- `python -m build --wheel` — не запускался: изменение не трогает упаковку, а
  build оставил бы `dist/`/egg-info в дереве;
- бенчмарки/нагрузочные прогоны — **0**.

### 6.5. Pre-existing красные гейты (записано, не исправлялось)

1. **`scripts/check_tree.py`:** `MAX_LINES = 80_000`, на исходном `b43bfbc`
   пакет — **86 174** строки, на финале — **86 531** (+357 строк этого
   изменения: новый код и докстринги). `tests/test_tree.py` при этом ожидает
   `< 87_000`. Рассинхронизация потолков существовала до миссии; потолок не
   поднимался, это отдельное осознанное решение.
2. **`python -m ruff check src tests scripts`:** **3 pre-existing F401** в
   файлах, которые это изменение не трогает:
   `src/karox/session_export.py:24` (`typing.Optional`),
   `tests/test_tool_shape_fixes.py:10` (`os`), `:20` (`karox.models.CoreCommand`).
   Те же строки есть в `HEAD`; мои файлы проходят чисто.

## 7. Экономика обычного случая

Один и тот же scripted-сценарий (write `hello.txt` → approved check → finish)
исполнен на исходных (`b43bfbc`) и новых исходниках:

```text
OLD: verified=true, model_requests=3, git.diff calls=1 (harness_final), stdout=""
NEW: verified=true, model_requests=3, git.diff calls=1 (harness_final), stdout="+hi"
```

- **Model turns: +0.** Полный review собирает harness, а не модель.
- **`git.diff` tool calls: +0** в обычном случае. В случае, когда модель сама
  смотрела частичный diff, появляется второй вызов (её частичный + финальный
  полный), но **model turn'ов по-прежнему +0**:
  `old: requests=5, diffs=1, harness_final=[]` → `new: requests=5, diffs=2`.
- **Внутренняя работа:** вместо одного процесса `git diff` полный review
  исполняет три (`git rev-parse --verify HEAD`, `git diff --no-ext-diff HEAD`,
  `git ls-files --others --exclude-standard -z`) и делает по одному
  ограниченному чтению на untracked-файл (≤2 МБ на файл, ≤20 000 символов
  показывается, ≤400 000 символов на review, ≤200 файлов). Это CPU/IO, не
  токены; на дереве с большим числом untracked-файлов цена выше — границы
  явные, см. §8.
- **Точность `VERIFIED`:** до — 4 из 4 офлайн-сценариев давали `VERIFIED` с
  неполным review; после — 0 из 4 (все получают полный review), плюс частичные
  diff'ы больше не закрывают обязательство.

## 8. Остаточные риски (честно)

1. **Truncated tracked diff.** Если `git diff HEAD` превышает потолок захвата
   (1 МБ, head+tail elision), `review.complete=False`, и run заканчивается
   unverified, хотя проверка могла пройти. Это осознанный fail-closed: середина
   diff'а не была показана; для очень больших «грязных» деревьев это false
   negative.
2. **Бинарные/слишком большие untracked-файлы.** Файл >2 МБ, binary или
   symlink за границей readable tree представлен маркером с размером (и sha256,
   если прочитан), а не текстом. Такой файл считается «ограниченно
   представленным» (`limited`), а не полностью просмотренным; для картинок
   остаётся visual proof.
3. **Gitignored-файлы.** `git ls-files --others --exclude-standard` не видит
   пути, исключённые `.gitignore`; такой deliverable не попадает ни в diff, ни в
   untracked-часть, и review о нём молчит. Продуктовые ignore-каталоги
   (`node_modules`, `dist`, `build`, …) ловятся как `excluded` и **блокируют**
   верификацию, если прогон сам изменил такой файл; gitignored-случай остаётся
   документированной дырой этого milestone.
4. **`git diff` и textconv.** Как и в Mission 02 §7.5, `git diff` может
   исполнять `diff.<driver>.textconv` из локального git config; `--no-ext-diff`
   отключает только внешние diff-программы. Новый код не добавляет shell/escape:
   пути проходят `safe_path`, вывод redacted.
5. **Identity видит только известные файлы.** Если после полного review
   побочный эффект запишет новый untracked-файл, identity не изменится, и review
   его не увидит (тот же класс, что Mission 02). В нормальном пути harness-review
   — последний вызов перед завершением; probe после него потребует нового check,
   но не нового review сам по себе.
6. **Elevated `git.commit` во время прогона.** Полный review — `HEAD`-relative;
   после коммита рабочее дерево чисто, review пуст, и `floor` его принимает (это
   не partial-diff обман, но и не просмотр коммита). `git.commit` доступен
   только в `ELEVATED`-профиле; для default `WORKSPACE_WRITE` недоступен.
   Кандидат в следующий milestone.
7. **Дополнительная durable-запись.** Содержимое untracked-файлов (ограниченное
   и redacted) теперь попадает в `provider_history`/`git_state` сессии — туда же,
   куда попадает `repo.read_file`; новых мест хранения нет.
8. **`limited` ≠ полный текстовый review.** Файл, представленный маркером,
   считается покрытым по пути, но не по всем байтам. Это записано в метаданных
   (`untracked.limited`) и в CHANGELOG; гейт не утверждает «весь текст
   просмотрен».

## 9. Коммиты и команды для независимого ревью

- Code + tests: `b9911ae4e586a1cc67cb5003d193369560bd8477`
  (`engine: the closing change review reads the whole change`).
- Report/docs: отдельный docs-only коммит с этим файлом и `docs/CHANGELOG.md`
  (hash — в сообщении о завершении).

```text
git log --oneline -3
git show --stat b9911ae
git diff b43bfbc..b9911ae

python -m pytest tests/test_engine_kernel.py -q -o addopts=""
python -m pytest tests/test_completion_gate.py tests/test_core_commands.py \
  tests/test_engine_tools.py tests/test_state_briefing.py \
  tests/test_engine_tool_env_render.py tests/test_engine_port_finish.py \
  tests/test_run_reuse_identity.py tests/test_tool_shape_fixes.py \
  tests/test_argument_validation.py tests/test_requirement_coverage.py \
  -q -o addopts=""

python -m ruff check src/karox/core_git.py src/karox/core_tools.py \
  src/karox/core.py src/karox/completion_gate.py src/karox/engine_kernel.py \
  tests/test_engine_kernel.py tests/test_completion_gate.py
python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui
python scripts/check_tree.py     # known red: 86 531 > 80 000 (pre-existing)
```

## 10. Решение и следующий milestone

**GO — кандидат на независимый аудит, не release-ready.** Дефект подтверждён
на исходном коде четырьмя офлайн-репродукциями, исправление минимально и
общее (доверенный охват в Core + предикат в `floor`), red→green доказан,
обычный прогон не подорожал ни model turn'ом, ни tool-вызовом. Остаточные риски
§8 названы явно; два pre-existing красных гейта (`check_tree` ceiling и три
F401) не связаны с этим изменением и не исправлялись.

**Единственный следующий milestone:** записывать HEAD на старте сессии и делать
финальный review относительно этой базовой ревизии (`git diff <base>` +
untracked, с теми же метаданными охвата), чтобы коммит, сделанный во время
прогона, не мог скрыть изменение от review. Gitignored-deliverables — отдельное
решение после этого.

---

## Machine-readable summary

```yaml
mission: dsh-change-review-20261106
starting_head: b43bfbc6b6bde326b187ab18fa54035d58938efb
branch: audit/dsh-change-review-20261106
code_commit: b9911ae4e586a1cc67cb5003d193369560bd8477
report_commit: docs-only follow-up commit containing this file
worktree_clean: true
confirmed_false_verified_cases: 4
fixed_cases: 4
untracked_deliverables_covered: true
red_before_fix: "7 failed tests/test_engine_kernel.py; 9 failed tests/test_completion_gate.py on b43bfbc"
green_after_fix: "107 passed tests/test_engine_kernel.py; 141 passed targeted neighbours; 0 failed"
model_turn_delta_on_normal_case: 0
full_suite_run: false
paid_calls: 0
push_performed: false
release_ready: false
remaining_risks:
  - "tracked diff truncated past the 1 MB capture ceiling is not a complete review"
  - "gitignored untracked deliverables stay outside diff and untracked listing"
  - "binary/oversized untracked files are represented by a size/digest marker, not text"
  - "HEAD-relative review cannot see an elevated commit made during the run"
  - "identity does not cover new untracked side effects written after the review"
  - "check_tree ceiling 80000 vs 86531 package lines (tests allow <87000), pre-existing"
  - "three pre-existing ruff F401s in untouched files"
next_milestone: "record the run-start HEAD and review git diff <base> so a commit during the run cannot hide the change"
```
