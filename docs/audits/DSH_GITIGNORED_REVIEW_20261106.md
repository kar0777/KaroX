# DSH Mission 05 — Git-ignored deliverables в закрывающем review (2026-11-06)

**Verdict. NEEDS FURTHER WORK перед релизом; ACCEPT CANDIDATE по существу миссии.**
Дефект воспроизведён офлайн на `6ef51d8` через реальный путь
`Harness → ExtendedCoreRuntime`: прогон создаёт `result.generated.txt`, имя
которого исключено закоммиченным `.gitignore`; closing review v2 показывает
пустой stdout и `untracked.listed=0`, при этом `verified=true`. То же самое
происходит с `.env` (секрет) и с `dist/bundle.js` (рабочий каталог): оба
варианта завершались `verified=true` с пустым review. Минимальный фикс:
закрывающий review получает третий, доверенный блок охвата `review.ignored`,
кандидаты берутся **только** из durable `changed_files` сессии, проверяются
одним `git ls-files --others --ignored --exclude-standard` на ограниченную
пачку путей с literal-pathspec, а найденный ignored deliverable проходит через
тот же ограниченный и redacted new-file pipeline, что и untracked-файлы.
Секретные/запрещённые/нечитаемые кандидаты не читаются и валят review в
fail-closed. Red→green доказан; обычный прогон не подорожал ни model turn'ом,
ни `git.diff`-вызовом. Полный pytest, live-модели, браузер, wheel, bridge,
push/merge не запускались.

## 1. Git reality

| Поле | Значение |
|---|---|
| Абсолютный путь | `D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\win` |
| Git top-level | тот же путь (linked worktree, `.git` — файл) |
| Исходная ветка | `audit/dsh-run-baseline-20261106` |
| Исходный HEAD | `6ef51d8f387f8a51ae5c0280434bd153d73cb5d4` (полный OID сверен; совпал с ожидаемым Mission 05) |
| Состояние на старте | `git status --short` пуст |
| Рабочая ветка | `audit/dsh-gitignored-review-20261106` (создана от HEAD) |
| Code + tests commit | `4a8ee48045dbbc8bb17a47259a6f2f5adb97b668` (parent `6ef51d8`) |
| Report/docs commit | отдельный docs-only коммит с этим файлом, `CHANGELOG.md` и `SESSIONS.md` (hash — в сообщении о завершении) |
| Финал | `git status --short` пуст после коммита отчёта |
| Другие деревья | не изменялись; push, merge, rebase, reset, clean, установки, туннели и правки env/keyring не выполнялись |

Проверено до любых изменений:

```text
git rev-parse --show-toplevel  -> D:/проекты/KaroX-v5/lab/kernel-duel-20261025/b/win
git branch --show-current      -> audit/dsh-run-baseline-20261106
git rev-parse HEAD             -> 6ef51d8f387f8a51ae5c0280434bd153d73cb5d4
git status --short             -> (пусто)
git worktree list               -> остальные 8 worktree не тронуты
```

Прочитаны `AGENTS.md`, `docs/DEVELOPMENT.md`, отчёты Mission 03/04
(`docs/audits/DSH_CHANGE_REVIEW_20261106.md`, `DSH_RUN_BASELINE_20261106.md`),
`docs/CHANGELOG.md`, `docs/SESSIONS.md` и исходники перечисленных ниже модулей.
Секреты, реальные `.env`, keyring и приватные конфиги не читались и не
печатались.

## 2. Root cause (точный путь, доказанный прогоном)

Полный review собирался из двух представлений дерева:

1. `git diff --no-ext-diff <start-oid>` — только **tracked** файлы (staged +
   unstaged относительно durable-базы Mission 04);
2. `git ls-files --others --exclude-standard -z` — только untracked файлы,
   **не** исключённые Git'ом.

`git ls-files --others --exclude-standard` по определению применяет
`.gitignore`, `.git/info/exclude` и global exclude. Поэтому путь, созданный
агентом как заказанный deliverable, но совпавший с шаблоном ignore, не попадал
ни в diff, ни в untracked-часть. `review.complete` при этом оставался `true`
(никакой ошибки чтения не было), `completion_gate.floor` видел
`review.complete is True`, совпадающую `completion_identity` и пустое
пересечение `untracked.excluded ∩ changed_files` — и снимал обязательство
review. Итог: `VERIFIED` с пустым closing stdout.

Ключевое: доказательство описывало только те представления, которые Core
прочитал, но не требовало ответить на вопрос «а какие ещё пути этот прогон
изменил, если Git их скрывает?». `changed_files` (durable, Core-maintained)
содержал ровно `result.generated.txt`, но review его не использовал.

Минимальный контрпример на исходном HEAD (offline, реальный
`Harness → ExtendedCoreRuntime`, disposable Git repo, без модели и сети):

```text
.gitignore: "*.generated.txt"             (закоммичен)
/c1: repo.write_file  result.generated.txt = "generated hello"
/c2: finish
```

```text
verified            : True
reason              : submitted
unmet               : ()
model_requests      : 2
git.diff calls      : 1   (harness_final)
closing stdout      : ''
review.version      : 2
review.complete     : True
review.untracked    : {"excluded": [], "limited": [], "listed": 0, "shown": [], "truncated": false}
review.ignored      : null
changed_files       : ['result.generated.txt']
file exists         : True
git ls-files --others --exclude-standard -z -> []
git ls-files --others --ignored --exclude-standard -z -- result.generated.txt
                                              -> ['result.generated.txt']
```

То есть путь существует, Git его игнорирует, сессия его записала — а review
молчит и сертифицирует прогон. Аналогично до фикса:

| Сценарий | `.gitignore` | Путь | До фикса |
|---|---|---|---|
| обычный ignored deliverable | `*.generated.txt` | `result.generated.txt` | `verified=true`, stdout `''`, `ignored=null` |
| секрет | `.env` | `.env` (toy-строка `PLACEHOLDER=...`) | `verified=true`, stdout `''`, содержимое нигде не показано |
| build-каталог | `dist/` | `dist/bundle.js` | `verified=true`, stdout `''` |

## 3. Before / after с точными выводами

Сценарии выполнены одним и тем же скриптом на копии `HEAD` (`git archive` во
временный каталог, read-only, без checkout) и на финальном дереве. Все
прогоны — `tmp_path`-репозитории, без сети, без модели.

### 3.1. Обычный ignored deliverable

```text
BEFORE (6ef51d8)                       AFTER (4a8ee48)
verified            : True             verified        : True
model_requests      : 2                model_requests  : 2
git.diff calls      : 1                git.diff calls  : 1
git subprocesses    : 3                git subprocesses: 4
review.version      : 2                review.version  : 3
review.complete     : True             review.complete : True
review.ignored      : null             review.ignored  : {"checked": 1, "shown": ["result.generated.txt"],
                                                          "limited": [], "excluded": [], "missing": [],
                                                          "truncated": false, "complete": true}
review.untracked    : listed=0,        review.untracked: listed=1,
                      shown=[]                           shown=["result.generated.txt"]
closing stdout      : ''               closing stdout  :
  \n diff --git a/result.generated.txt b/result.generated.txt
  new file mode 100644
  --- /dev/null
  +++ b/result.generated.txt
  @@ -0,0 +1,1 @@
  +generated hello
```

`changed_files=['result.generated.txt']` и `baseline.commit == start` в обоих
прогонах; `verified=true` после фикса получен **только** потому, что review
реально показал deliverable.

### 3.2. Секрет и build-каталог (fail-closed)

```text
AFTER: .env (gitignored, toy content "PLACEHOLDER=not-a-real-secret")
verified            : False
reason              : verification_failed
unmet               : ('missing current change review',)
closing stdout      : '[karox: this is not a complete review: ignored coverage is incomplete]\n'
review.complete     : False
review.untracked    : {"excluded": [".env"], "shown": []}
review.ignored      : {"checked": 1, "shown": [], "limited": [],
                       "excluded": [".env"], "missing": [], "truncated": false, "complete": false}
содержимое .env в выводе: отсутствует

AFTER: dist/bundle.js (gitignored как dist/)
verified            : False
unmet               : ('missing current change review',)
review.untracked    : {"excluded": ["dist/bundle.js"], "shown": []}
review.ignored      : {"checked": 1, "excluded": ["dist/bundle.js"], "complete": false}
```

До фикса оба сценария давали `verified=true` и пустой review (§2). После
фикса путь назван в метаданных, но не прочитан; прогон остаётся unverified с
явной причиной.

## 4. Дизайн и инварианты

Ни одного нового tool'а, FSM, кэша или сканера дерева. Один новый блок
доверенных метаданных и одна проверка тех путей, которые Core уже записал.

| Файл | Δ (net) | Назначение |
|---|---|---|
| `src/karox/git_baseline.py` | +18/−3 | `REVIEW_VERSION = 3`; ContextVar `_CHANGED` со snapshot `record.changed_files`, заполняется существующим `session_baseline(record)`; `changed_paths()` |
| `src/karox/core_git.py` | +129/−25 | `_known_ignored_paths` (кандидаты из durable state → bounded `ls-files --others --ignored --exclude-standard -z` с `:(literal)`); `_append_untracked_review` добавляет ignored-пути в тот же new-file pipeline; блок `review.ignored`; secret-name policy; `review.complete` теперь включает `ignored.complete` |
| `src/karox/completion_gate.py` | +12/−4 | `_complete_change_review` требует version 3 и `review.ignored.complete is True` с пустыми `excluded`/`missing`; nothing else изменено |
| `tests/test_engine_kernel.py` | +298 | 7 end-to-end регрессий (positive, unowned tree, secret, build dir, oversize marker, identity freshness, resume) |
| `tests/test_completion_gate.py` | +110/−1 | `_ignored()` fixture, 5 новых negative-кейсов, positive-кейс, marker-кейсы, отказ version-2 review |

Инварианты:

- **Происхождение (trusted provenance).** Кандидаты — только
  `record.changed_files`, снятые в ContextVar в тот момент, когда Core уже
  загрузил durable-запись и обернул handler. Модель не может ни назвать путь,
  ни расширить набор: в её распоряжении только аргумент `complete=true`,
  который влияет на *режим*, но не на список кандидатов. Snapshot означает,
  что запись, сделанная во время команды, не меняет набор.
- **Никакого обхода ignored-деревьев.** Один `git ls-files
  --others --ignored --exclude-standard -z -- :(literal)<path>...` на пачку
  ≤50 путей; literal-pathspec запрещает glob-расширение. Каталог-кандидат
  (если путь стал каталогом) отбрасывается до вызова Git, иначе pathspec
  заставил бы `ls-files` обойти всё поддерево. Всего ≤400 кандидатов; больше —
  `ignored.truncated=true`, review не сертифицируется.
- **Тот же bounded/redacted pipeline.** Найденный ignored-путь добавляется в
  общий список untracked-файлов и проходит `safe_path` (конфайнмент, запрет
  symlink/`.git`/`.karox`), `is_ignored_path`, `MAX_FILE_BYTES`, лимиты
  200 файлов / 20 000 символов на файл / 400 000 на весь review, `redact`.
- **Секреты.** Имена `.env` (кроме шаблонов `.env.example/.sample/.template/.dist`),
  `.git-credentials`, `.netrc`, `.npmrc`, `.pypirc`, `.pgpass`,
  `credentials.json`, `id_rsa/id_dsa/id_ecdsa/id_ed25519` и расширения
  `.key/.keystore/.kdbx/.p12/.pem/.pfx` **не читаются**: путь попадает в
  `untracked.excluded` и `ignored.excluded`, review становится incomplete.
  Существующая политика `is_ignored_path` (`node_modules`, `.venv`, `dist`,
  `build`, …) не менялась. `.git`/`.karox` и symlink-побеги по-прежнему
  отсекает `safe_path`.
- **Метаданные честные.** `review.ignored = {checked, shown, limited, excluded,
  missing, truncated, complete}`. `shown` — весь текст в review; `limited` —
  документированный маркер (binary/oversized/обрезка); `excluded`/`missing` —
  путь не покрыт и review обязан быть incomplete. `review.complete` теперь
  конъюнкция tracked/untracked/ignored; `review.truncated` учитывает и
  ignored-усечение.
- **Gate.** `_complete_change_review` читает только `review`, а не аргумент
  модели: version 3, `mode=complete`, `paths=[]`, `truncated=False`,
  `complete=True`, `base/base_commit` = durable baseline Mission 04, плюс
  `ignored.complete is True` и пустые `ignored.excluded/missing`. Старый
  v1/v2-review и сессия без базы отклоняются, как и раньше. Identity, checks,
  visual и Mission 01/02/03/04 не менялись.
- **Обычная скорость.** Ни одного дополнительного model turn'а и ни одного
  дополнительного tool-вызова `git.diff`; добавляется один локальный
  `git ls-files` на closing review (см. §6).

Отвергнутые варианты:

- *Сканировать все ignored-файлы (`ls-files --ignored` без pathspec)* —
  превращает review в обход `node_modules`/`.venv` и утечку имён чужих файлов.
- *Взять пути из текста модели или из `git.status`* — недоверенное
  происхождение; модель могла бы просто не назвать путь.
- *Сделать `git check-ignore --stdin`* — `_run` намеренно запускает детей с
  `stdin=DEVNULL`, а `-z` у `check-ignore` требует `--stdin`; `ls-files`
  уже в read-only allowlist и даёт то же подмножество существующих путей.
- *Расширить `completion_gate.identity` на всё дерево* — дорого и не относится
  к охвату; identity и так хеширует известные changed-файлы, включая ignored.
- *Отдельный `git.change_review` tool* — лишняя поверхность; хватило
  существующего `complete: true`.

## 5. Тесты: красное → зелёное

Все запуски последовательные, без `-n`/xdist, `-o addopts=""`, локальные
CPU-тесты.

### 5.1. Красное на исходном HEAD (`6ef51d8`)

```text
python -m pytest tests/test_engine_kernel.py -q -o addopts="" \
  -k "ignored_deliverable or ignored_tree or ignored_secret or ignored_build_output
      or oversized_ignored or changed_ignored or resumed_session_still"
-> 7 failed, 114 deselected in 11.38s

python -m pytest tests/test_completion_gate.py -q -o addopts="" \
  -k "ignored or version_two"
-> 4 failed, 5 passed, 60 deselected in 0.36s
```

Красные по правильной причине, а не из-за отсутствующего helper'а: главный
engine-тест падает на `assert "result.generated.txt" in stdout` (stdout `''`),
то есть ровно на исчезнувшем deliverable; `test_a_version_two_review...`
падает потому, что старый gate **принимал** v2 без ignored-блока. Пять
зелёных gate-кейсов на старом коде — это negative-параметры, которые старый
код отклонял целиком из-за version 3; они не являются доказательством
дефекта, доказательство — engine-репродукция §2–3.

### 5.2. Зелёное на финальном дереве (`4a8ee48`)

| Команда | Результат |
|---|---|
| `python -m pytest <7 новых engine-тестов> -q -o addopts=""` | **7 passed** (12.05 s) |
| `python -m pytest tests/test_engine_kernel.py -q -o addopts=""` | **121 passed** (166.77 s) |
| `python -m pytest tests/test_completion_gate.py tests/test_sessions.py tests/test_tree.py -q -o addopts=""` | **101 passed** (10.15 s) — 69 gate + 26 sessions + 6 tree |
| `python -m pytest tests/test_core_commands.py tests/test_engine_tools.py tests/test_state_briefing.py tests/test_engine_tool_env_render.py tests/test_engine_port_finish.py tests/test_run_reuse_identity.py tests/test_argument_validation.py tests/test_requirement_coverage.py -q -o addopts=""` | **82 passed** (22.13 s) |
| `python -m pytest tests/test_session_export.py tests/test_repo_command_safety.py tests/test_check_probes.py tests/test_tool_shape_fixes.py tests/test_engine_port_context.py tests/test_engine_port_progress.py -q -o addopts=""` | **70 passed, 1 skipped** (8.85 s) |
| `python -m ruff check src/karox/core_git.py src/karox/completion_gate.py src/karox/git_baseline.py tests/test_engine_kernel.py tests/test_completion_gate.py` | All checks passed |
| `python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui` | Success: no issues found in 13 source files |
| `git diff --check` | чисто |

Итого уникальных проверенных тестов: 121 + 101 + 82 + 70 = **374**, из них
**373 passed, 1 skipped, 0 failed** (наборы файлов не пересекаются между
строками).

### 5.3. Что именно проверяет матрица

1. **Positive ignored deliverable** —
   `test_an_ignored_deliverable_is_part_of_the_complete_review`: stdout
   содержит `+generated hello`, `ignored.shown == [path]`, `checked == 1`,
   `review.complete=true`, `verified=true`, 2 model request'а, ровно один
   `git.diff`, harness-финал заканчивается `git.diff`.
2. **Unowned ignored tree** —
   `test_an_unowned_ignored_tree_is_not_enumerated_or_read`: 25 файлов в
   ignored `.venv/lib` с маркером; маркер не появляется ни в одном review, в
   argv добавленного `ls-files` нет `.venv`, проверяется только
   `:(literal)hello.txt`; `ignored.checked == 1`, `review.complete=true`.
3. **Секрет** — `test_an_ignored_secret_shaped_path_is_neither_read_nor_certified`:
   toy-содержимое `.env` отсутствует в выводе, `ignored.excluded == [".env"]`,
   `review.complete=false`, `verified=false`, unmet содержит
   `missing current change review`.
4. **Build-каталог** —
   `test_an_ignored_build_output_the_run_wrote_cannot_be_certified`: путь
   `dist/bundle.js` исключён и не показан; прогон unverified.
5. **Over-limit** —
   `test_an_oversized_ignored_deliverable_is_a_marker_not_silence`: при
   понижении `REVIEW_MAX_UNTRACKED_CHARS` до 8 путь попадает в
   `ignored.limited`, в stdout есть явный маркер обрезки, `verified=true`
   (документированная marker-политика).
6. **Identity freshness** —
   `test_a_changed_ignored_deliverable_invalidates_the_complete_review`: после
   правки ignored-файла старый review не принимается, новый содержит
   `+generated second` и не содержит `generated first`.
7. **Resume** —
   `test_a_resumed_session_still_reviews_its_ignored_deliverable`: первый прогон
   останавливается по step limit, resumed-прогон (новый процесс, без собственных
   writes) получает путь из durable record, `base_commit` равен стартовому OID,
   review показывает `+generated hello`, `verified=true`.
8. **Gate-контракт** — 5 новых negative-кейсов (нет ignored-блока, denied,
   unreadable, truncated, incomplete), positive ignored/marker-кейсы, отказ
   v2-review. Существующие кейсы Mission 03/04 (complete/staged/unstaged/
   path-filtered/truncated/excluded/unborn/legacy/corrupt/stale identity)
   остались зелёными без ослабления ассертов.

### 5.4. Что не запускалось

- полный `python -m pytest` — в наборе есть live browser/3D/desktop-тесты;
  задание запрещает автоматический полный прогон;
- live-модели/платные API — **0**;
- Chromium/Playwright, 3D, bridge/tunnel, Docker/WSL, установки — **0**;
- `python -m build --wheel` — не запускался (изменение не трогает упаковку,
  build оставил бы `dist/`/egg-info в дереве);
- push/merge/rebase и изменения других worktree — **0**.

## 6. Экономика и IO

Один и тот же обычный сценарий (write `hello.txt` → finish, без ignored
файлов), замер через обёртку над `core._run`:

```text
OLD 6ef51d8: verified=true, model_requests=2, git.diff calls=1,
             git subprocesses=3
             (status, diff <start>, ls-files --others --exclude-standard)
NEW 4a8ee48: verified=true, model_requests=2, git.diff calls=1,
             git subprocesses=4
             (+ ls-files --others --ignored --exclude-standard -z -- :(literal)hello.txt)
```

- **Model turns: +0**, **`git.diff` tool calls: +0** во всех сценариях.
- **Git-процессов: +1** на closing review в обычном случае; для N известных
  путей — `ceil(N/50)` дополнительных чтений, максимум 8 при потолке 400.
- **Локальные чтения:** только `safe_path`/`stat` по известным changed-путям
  (≤400) и содержимое реально ignored-файлов (в тех же пределах, что untracked:
  ≤2 МБ на файл, ≤20 000 символов показывается, ≤400 000 суммарно). Обхода
  дерева, хеширования дерева и чтения чужих ignored-файлов нет.
- **Durable-запись:** ignored-секции попадают в тот же `provider_history`, что
  и untracked-разделы; новых мест хранения нет. Метаданные выросли на один
  короткий блок `review.ignored`.
- **Платные вызовы: 0.**

## 7. Остаточные риски и release gates

1. **`scripts/check_tree.py` красный (pre-existing):** `86,996 > 80,000`.
   `tests/test_tree.py` зелёный (`86,996 < 87,000`), но запас — **4 строки**.
   Порог не поднимался и не менялся; следующее добавление кода в пакет
   практически наверняка его перешагнёт. Это **release blocker** для владельца,
   отдельное осознанное решение: согласовать 80k-скрипт и 87k-тест.
2. **Три pre-existing ruff F401** в нетронутых файлах
   (`src/karox/session_export.py:24`, `tests/test_tool_shape_fixes.py:10`,
   `:20`); `ruff check src tests scripts` падает на них, изменённые файлы чисты.
3. **Общий content budget.** Ignored-deliverable делит 400 000-символьный
   бюджет с untracked-файлами. Если бюджет уже исчерпан до ignored-пути,
   такой путь помечается `excluded` и review fail-closed (явная проверка
   `budget <= 0`). Если бюджет остался частично, путь показывается обрезанным и
   помечается `limited` — это сохранённая документированная marker-политика
   Mission 03, а не «полный просмотр».
4. **Удалённый ignored-путь.** Git не перечисляет несуществующий файл, поэтому
   путь, созданный и удалённый до review, не попадает в `ignored`. Это не
   дыра: в стартовом дереве такого файла не было, итоговое изменение по нему
   нулевое; `changed_files` его помнит, identity хеширует как `missing`.
   Явного `missing`-маркера для «создан и удалён» нет.
5. **Binary/oversized ignored deliverable** представлен маркером
   (размер/sha256, если прочитан), а не текстом — `limited`, как и для
   untracked; это соответствует документированной политике.
6. **Потолок 400 changed-путей.** Прогон, изменивший больше 400 файлов,
   получает `ignored.truncated=true` и unverified (fail-closed), даже если
   ignored-файлов среди них нет. Для обычных прогонов это недостижимо; для
   массовых рефакторингов — честный false negative.
7. **Секреты по имени, а не по содержимому.** Denylist покрывает типовые
   секретные имена; секрет в ignored-файле с безобидным именем будет прочитан
   и обработан обычным `redact`, как и раньше для untracked. Это не регрессия,
   но и не полная защита.
8. **Version bump на resume.** Сессия, у которой в истории уже лежит v2-review,
   получит от harness новый v3-review (один локальный `git.diff`, без model
   turn'а); старый v2 до этого момента закрывать floor не может. Сессия
   без `changed_files` по-прежнему не должна ничего доказывать.
9. **Не проверялись:** hosted bridge end-to-end (запрещён), wheel,
   `git.commit`-в-сессии с ignored-файлом (коммит не меняет механику: ignored
   файл по-прежнему untracked/невидим для diff, `baseline` не двигается),
   live-модели.

## 8. Команды для независимого ревью

```text
git log --oneline -3
git show --stat 4a8ee48
git diff 6ef51d8..4a8ee48

python -m pytest tests/test_engine_kernel.py -q -o addopts=""          # 121 passed, 166 s
python -m pytest tests/test_completion_gate.py tests/test_sessions.py \
  tests/test_tree.py -q -o addopts=""                                   # 101 passed
python -m ruff check src tests scripts                                  # 3 pre-existing F401
python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui
python scripts/check_tree.py                                            # pre-existing red (86 996 > 80 000)
```

Рекомендация ревьюеру: (1) проверить, что набор кандидатов действительно
происходит только из `git_baseline.changed_paths()` и снимается snapshot'ом до
handler'а; (2) что literal-pathspec + фильтр каталогов не позволяют
`ls-files --ignored` обойти чужое дерево; (3) что секретный путь не читается
ни в одной ветке (ни `_known_ignored_paths`, ни `_untracked_review_section`);
(4) что gate требует именно `review.ignored.complete`, а не аргумент модели;
(5) что v3 не ослабил v2-проверки базы/truncation/exclusion и что обычный
прогон остаётся одним `git.diff` без нового model turn'а.

## 9. Решение и следующий milestone

**ACCEPT CANDIDATE по существу миссии; NEEDS FURTHER WORK перед релизом** из-за
pre-existing `check_tree` (86,996 против 80,000) и трёх F401, а также потому,
что запас `tests/test_tree.py` составляет всего 4 строки. Дефект воспроизведён
на исходном HEAD тремя независимыми сценариями, исправление минимально и
общее (доверенный блок охвата + предикат gate), red→green доказан, обычный
путь не подорожал ни моделью, ни tool-вызовом.

**Рекомендуемый следующий milestone (один):** согласовать line ceilings
(`scripts/check_tree.py` 80k против `tests/test_tree.py` 87k) и убрать три
pre-existing F401 как отдельное release-hygiene решение владельца. После этого
— вернуться к reserved-budget для ignored-deliverables, если ревьюер хочет
гарантировать показ полного текста даже при исчерпанном общем бюджете.

---

## Machine-readable summary

```yaml
mission: dsh-gitignored-review-20261106
repository: D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\win
starting_branch: audit/dsh-run-baseline-20261106
starting_head: 6ef51d8f387f8a51ae5c0280434bd153d73cb5d4
branch: audit/dsh-gitignored-review-20261106
code_commit: 4a8ee48045dbbc8bb17a47259a6f2f5adb97b668
code_commit_parent: 6ef51d8f387f8a51ae5c0280434bd153d73cb5d4
report_commit: docs-only follow-up commit containing this file
worktree_clean: true
repro_before_fix: true
before_fix_false_verified_cases: 3
before_fix_review: 'version 2, complete true, untracked.listed 0, stdout "", verified true'
after_fix_review: 'version 3, complete true, ignored.shown [result.generated.txt], stdout "+generated hello", verified true'
fail_closed_cases_after_fix: ['.env secret-shaped', 'dist/ build output']
new_tests:
  engine_end_to_end: 7
  completion_gate_cases: 9
  baseline_persistence_tests: 0
red_before_fix: "7 failed tests/test_engine_kernel.py; 4 failed + 5 passed test_completion_gate.py (-k ignored/version_two)"
green_after_fix: "121 engine + 101 gate/sessions/tree + 82 neighbours + 70 session/tool = 373 passed, 1 skipped"
model_request_delta: 0
closing_diff_call_delta: 0
git_subprocess_delta: 1
package_lines: 86996
check_tree: "pre-existing red: 86996 > 80000"
test_tree_ceiling: "green, 4-line margin (86996 < 87000)"
pre_existing_ruff_f401: 3
full_suite_run: false
paid_api_calls: 0
wheel_built: false
push_performed: false
release_ready: false
remaining_blockers:
  - "scripts/check_tree.py ceiling 80000 vs 86996 package lines (pre-existing)"
  - "tests/test_tree.py margin is 4 lines; next package addition likely trips it"
  - "three pre-existing ruff F401s in untouched files"
residual_risks:
  - "ignored deliverable shares the 400k content budget with untracked files; starved path is limited (partial) or excluded (fail closed)"
  - "a path deleted before the review is not listed by Git; no net change, no explicit missing marker"
  - "binary/oversized ignored file is a size/digest marker, not text"
  - "more than 400 changed paths fail the ignored coverage closed"
  - "secret detection is by well-known name, not by content"
next_milestone: "reconcile the check_tree 80k/test_tree 87k line ceilings and clear the three pre-existing F401s; then consider a reserved review budget for ignored deliverables"
report_path: docs/audits/DSH_GITIGNORED_REVIEW_20261106.md
```
