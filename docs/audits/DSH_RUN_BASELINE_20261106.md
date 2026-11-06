# DSH Mission 04 — Closing review против стартовой ревизии сессии (2026-11-06)

**Verdict.** **GO — кандидат на независимый аудит, не release-ready.** Hole из
Mission 03 §8.6 воспроизведён офлайн на `84c7b65`: ELEVATED-прогон коммитит свою
работу внутри сессии, `git diff HEAD` показывает пустое изменение, и прогон
заканчивается `VERIFIED` с review, который ни разу не показал коммит. Исправление
минимально и общее: сессия один раз записывает точную стартовую ревизию, а полный
review считается против неё; gate принимает только review, назвавший именно эту
ревизию. Red→green доказан, обычный путь не подорожал ни model turn'ом, ни
`git.diff`-вызовом. Полный suite, платные API, bridge/tunnel, push и merge не
запускались.

## 1. Git reality

```text
$ git rev-parse --show-toplevel
D:/проекты/KaroX-v5/lab/kernel-duel-20261025/b/win
$ git branch --show-current        # до начала работы
audit/dsh-change-review-20261106
$ git rev-parse HEAD
84c7b650cde89c611beefdebaf26470b825f3955
$ git status --short
(пусто)
$ git log -5 --oneline
84c7b65 docs: DSH mission 03 honest change-review audit (2026-11-06)
b9911ae engine: the closing change review reads the whole change
b43bfbc docs: DSH mission 02 reuse-integrity audit (2026-11-06)
f831a9a engine: a check or test that executes code never answers from a cache
040882d docs: DSH mission 01 verification audit (2026-11-06)
$ git worktree list
... b/win 84c7b65 [audit/dsh-change-review-20261106]   (остальные 8 worktree не тронуты)
```

Отчёты Mission 01/02/03 на месте (`docs/audits/DSH_VERIFIED_AUDIT_20261106.md`,
`DSH_REUSE_INTEGRITY_20261106.md`, `DSH_CHANGE_REVIEW_20261106.md`), Mission 03 code
`b9911ae` присутствует. Все ожидания миссии совпали; ветка создана от чистого HEAD:
`git switch -c audit/dsh-run-baseline-20261106`.

| Коммит | SHA | Содержимое |
|---|---|---|
| 1. regression tests | `eefa1cc796b8e2c6a3701bc0ffd00ec2968486fb` | только тесты; на `84c7b65` красные |
| 2. fix + contract tests + docs | `108b530d851b040eceab968284b048f9d572507a` | `git_baseline.py`, поле сессии, review v2, gate, CHANGELOG/SESSIONS/ARCHITECTURE |
| 3. report | docs-only коммит с этим файлом | hash — в сообщении о завершении |

## 2. Точный путь данных

```text
CLI/bridge
  └─ SessionStore.create            sessions.py:458
       ├─ git_baseline.capture      sessions.py:483  →  git_baseline.py:98
       │    один `git rev-parse --verify HEAD`
       │    40/64 hex → {"kind":"commit","commit":<oid>}
       │    иначе `git rev-parse --git-dir`:
       │        exit 0 → {"kind":"unborn"}        (репозиторий без коммита)
       │        иначе  → {"kind":"unavailable","reason":...}
       └─ SessionRecord.git_baseline            sessions.py:259
            сериализуется asdict, входит в checksum, пишется атомарно
  └─ EngineKernel.run: sessions.load(session_id)        engine_kernel.py:1005
  └─ tool `git.diff {"complete": true}` (модель или harness_final)
       └─ CoreRuntime.execute: load record, policy/risk/lease
            with git_baseline.session_baseline(record)   core.py:635
            handler → _complete_change_review              core_git.py:110
              ├─ base = durable start oid
              ├─ git diff --no-ext-diff <oid>   (staged+unstaged, delete/rename)
              ├─ _append_untracked_review(...)  (Mission 03, без изменений)
              └─ review v2: base/base_commit/commands/paths/complete/truncated/untracked
       └─ результат в record.provider_history (_persist_engine_tool_result)
  └─ finish → completion_gate.floor                      completion_gate.py:889
       └─ _complete_change_review(record, review)        completion_gate.py:820
            version==2, mode=="complete", paths==[], truncated is False,
            complete is True, base == durable baseline (start+точный oid | unborn)
       └─ identity совпадает, excluded-файл прогона не пропущен
  └─ floor == [] → _verification → VERIFIED
  └─ resume: sessions.load отдаёт тот же git_baseline (capture только в create)
```

`_harness_final_calls` (`engine_kernel.py:1974`) не менялся: обычный прогон, как и
раньше, добирает один `git.diff {"complete": true}` в конце.

## 3. Красное воспроизведение ДО изменения

### 3.1. Тесты, вошедшие в коммит `eefa1cc`, на `84c7b65`

```text
$ python -m pytest tests/test_engine_kernel.py -q -o addopts="" \
    -k "commit_made_during or first_commit_inside or commit_after_the_complete \
        or reads_staged_unstaged or truncated_tracked or review_without_a_start_revision"
5 failed, 1 passed, 107 deselected in 8.94s        (exit 1)

$ python -m pytest tests/test_completion_gate.py -q -o addopts="" \
    -k "start_revision or start_baseline or legacy_review or corrupt \
        or read_only_session_without or unborn_start"
4 failed, 9 passed, 47 deselected in 0.34s          (exit 1)

$ python -m pytest tests/test_sessions.py -q -o addopts="" -k "GitStartRevision"
3 failed, 22 deselected in 1.24s                    (exit 1)
```

### 3.2. Финальный набор тестов на `84c7b65` (независимая проверка, что он ловит дефект)

Старое дерево извлечено только на чтение в `%TEMP%` (`git archive`, без worktree,
без checkout):

```text
$ git archive --format=tar 84c7b650... -o %TEMP%/mission04/old.tar && tar -xf ... 
$ python -m pytest tests/test_engine_kernel.py ... -k "<7 новых тестов>"
6 failed, 1 passed, 107 deselected in 10.97s        (проходит только truncated-guard)

$ python -m pytest tests/test_completion_gate.py -q -o addopts=""
8 failed, 52 passed in 2.26s

$ python -m pytest tests/test_sessions.py -q -o addopts="" \
    -k "GitStartRevision or before_the_start_revision_existed"
4 failed, 22 deselected in 1.27s
```

(Третий тест — миграция старой записи без поля — на старом дереве падает на
`payload.pop("git_baseline")`: поля не существовало.)

### 3.3. Что именно наблюдалось (не рассуждение, а вывод прогона)

Один и тот же скриптовый сценарий: старт `A` → write `hello.txt` → ELEVATED
`git.commit` (HEAD → `C`) → approved check → `git.diff {"complete": true}` → finish.

```text
BEFORE (84c7b65):
  start_head:        A (значение не сохранялось: поля в записи нет)
  head_after_run:    199d9fc... (C != A)
  verified:          true      reason: submitted
  baseline:          null      (поля в записи нет)
  review:            {"version":1,"mode":"complete","base":"HEAD",
                      "complete":true,"truncated":false,
                      "untracked":{...пусто...},"paths":[]}
  tracked stdout:    ""        <-- коммит в review не попал

AFTER (108b530):
  start_head:        b8b7b5f... (A)
  head_after_run:    9b4968b... (C != A)
  verified:          true      reason: submitted
  baseline:          {"version":1,"kind":"commit","commit":"b8b7b5f...","captured_at":...}
  review:            {"version":2,"mode":"complete","base":"start",
                      "base_commit":"b8b7b5f...",
                      "commands":[["git","diff","--no-ext-diff","b8b7b5f..."]],
                      "complete":true,"truncated":false,
                      "untracked":{...пусто...},"paths":[]}
  tracked stdout:    diff --git a/hello.txt b/hello.txt
                     new file mode 100644
                     --- /dev/null
                     +++ b/hello.txt
                     +hi
```

То есть до фикса `floor` принимал review, чей `base` был «тот HEAD, что сейчас»,
и `completion_identity` совпадала (байты файла не менялись с момента коммита).
После фикса review описывает `start -> working tree`, а gate сверяет базу с
durable-значением.

### 3.4. Коммит ПОСЛЕ review

Проверено отдельным тестом
(`test_a_commit_after_the_complete_review_does_not_hide_the_change`): review снят
на финальных байтах, затем пользовательский Git коммитит те же байты, HEAD
двигается, файлы не меняются. `floor` по-прежнему принимает review, и это
**правильно**: ни один байт, который получит пользователь, не изменился после
review; второй review был бы false negative. Доказано: «review-потом-коммит» не
создаёт дыры, потому что review уже охватил эти байты. Не проверялось (и не
утверждается): коммит, меняющий содержимое помимо уже просмотренного diff'а —
это была бы новая правка, и её ловит `completion_identity`, а не база.

## 4. Дизайн

Один новый модуль и один additive-поле; ни одного нового tool'а, FSM, сканера
дерева или кэша.

| Файл | Δ | Назначение |
|---|---|---|
| `src/karox/git_baseline.py` | +191 | `capture` (одно чтение Git при создании сессии), `normalize` (строгая проверка формы, иначе «нет базы»), `session_baseline`/`current` (ContextVar на время одного `execute`), словарь `REVIEW_VERSION=2`, `REVIEW_BASE_START/UNBORN` |
| `src/karox/sessions.py` | +14 | поле `SessionRecord.git_baseline` (default `{}`, backward compatible); capture в `create` до state-lock, ровно один раз |
| `src/karox/core_git.py` | +143/−25 | `_complete_change_review` считает `git diff --no-ext-diff <точный oid>` + untracked; review v2 с `base/base_commit/commands`; fail-closed ветки (нет базы, unborn→первый коммит, объект не читается); `_merge_tracked_diffs` для unborn (index + unstaged) |
| `src/karox/core.py` | +18/−8 | `with git_baseline.session_baseline(record)` вокруг handler'а; схемы `git.diff complete` больше не обещают «against HEAD» |
| `src/karox/core_tools.py` | +10/−8 | те же формулировки схемы для extended runtime |
| `src/karox/completion_gate.py` | +52/−27 | `_complete_change_review(record, review)` — v2 + сверка `base_commit` с durable baseline; v1 и «нет базы» отклоняются |
| `tests/test_engine_kernel.py` | +38 | 7 новых тестов + перевод трёх Mission 03 тестов на v2-контракт |
| `tests/test_completion_gate.py` | +100/−… | 13 новых кейсов + обновление helper'ов и позитивного теста |
| `tests/test_sessions.py` | +22 | 3 теста захвата/персистентности + совместимость старой записи |

### Почему база неизменяема

- Значение читает **KaroX**, а не модель: `capture` исполняет Git сам и пишет в
  запись только `kind` + точный oid. Текст модели в это поле не попадает.
- Захват происходит **только** в `SessionStore.create` (единственная точка
  создания записи; `reactivate` переиспользует существующий файл, `continue_task`
  и `fork`-ребёнок получают своё значение). `resume` вызывает `load`, а не
  `create`, поэтому стартовая ревизия не перезаписывается.
- `git.commit` не трогает поле: мутация меняет HEAD и (для `repo.write_file`)
  `changed_files`, но не `git_baseline`.
- Значение входит в checksum записи: подмена через файл ломает `from_dict`, а не
  подменяет базу.
- База — **точный object id**, не ref и не короткое имя: `git diff` получает
  40/64-hex строку, прошедшую регулярку `\A(40|64 hex)\z`. Ветка/тег с тем же
  именем не может «двигать» базу между записью и review.

### Схема и миграция

`SCHEMA_VERSION` не менялся. `git_baseline` — новое поле dataclass с
`default_factory=dict`; `from_dict` принимает старый JSON без него (поле
отсутствует → default), checksum проверяется по фактически сохранённым полям.
Старая запись с `changed_files` и без базы **не** получает базу из текущего HEAD:
`normalize({})` → `None`, review не сертифицируется (fail closed), прогон
остаётся unverified. Read-only сессия (нет `changed_files`) по-прежнему не должна
ничего доказывать — `floor` выходит раньше. Это осознанная цена миграции: resumed
сессия, начатая до Mission 04, не сможет закрыться полным review; лечится новой
сессией (и это честнее, чем выдать текущий HEAD за стартовый).

### Контракт «нет коммита» (unborn)

- Если репозиторий без коммита и коммита всё ещё нет: tracked-часть — индекс
  (`git diff --cached`) **плюс** unstaged (`git diff`), untracked — как в Mission
  03. `base: "unborn"`, `base_commit: null`.
- Если за время сессии появился первый коммит: сравнивать не с чем (пустое дерево
  не является существующим объектом, а сравнение с текущим HEAD показало бы
  пустое изменение). Review честно помечается `complete:false` с причиной
  «the repository gained its first commit during this session…», прогон не
  получает `VERIFIED`. Это явный и безопасный контракт, а не молчаливая пустота.
- `unavailable` (не git-каталог, нет Git, таймаут) отличается от `unborn` и тоже
  не сертифицируется; однако создание сессии из-за Git никогда не падает.

### Грязное рабочее дерево на старте

`git diff <A>` показывает **все** незакоммиченные изменения относительно `A`,
включая те, что пользователь сделал до сессии (например, staged-правку, которую
сделал не агент). Это то же поведение, что и в Mission 03 (`git diff HEAD`), и это
охват, не авторство: авторство по-прежнему фиксируется только в
`changed_files`/evidence tool-результатов. Никакого нового ложного приписывания
нет.

### Отвергнутые варианты

- *Продолжать `git diff HEAD` и просто сравнивать `HEAD` с базой* — после коммита
  diff пуст; сравнение поймало бы расхождение, но не показало бы изменение, и
  прогон навсегда остался бы unverified без полезного review.
- *Передавать базу через аргументы `git.diff`* — модель смогла бы выбрать базу;
  это ровно тот класс дефекта, который миссия закрывает.
- *Захватывать HEAD при первом tool-вызове/старте kernel'а* — поздно (модель может
  успеть закоммитить) и легко пропускается при resume.
- *Атрибут на `CoreRuntime`* — один runtime может обслуживать несколько сессий
  (hosted bridge), атрибут гонялся бы между ними; ContextVar привязан к контексту
  одного `execute`.
- *Пустое дерево как база для unborn* — объекта `4b825dc...` в свежем репозитории
  может не быть, а если он есть, то покажет как «добавленные» все чужие файлы.
- *Хранить базу в `git_state`* — это rolling-запись последнего status/diff,
  которую перезаписывает каждый вызов; неизменяемое значение там не выживает.
- *Отдельный `git.change_review` tool* — лишняя поверхность; хватило одного
  контракта у существующего `complete`.

## 5. Тесты и quality gates

Все запуски последовательные, без `-n`/xdist, `-o addopts=""`, только локальные
CPU-тесты.

### 5.1. Зелёное на финальном дереве (`108b530`)

| Команда | Результат |
|---|---|
| `python -m pytest <24 новых кейса> -q -o addopts=""` | **24 passed** (11.51 s) |
| `python -m pytest tests/test_engine_kernel.py -q -o addopts=""` | **114 passed** (153.72 s) |
| `python -m pytest tests/test_completion_gate.py tests/test_sessions.py tests/test_tree.py -q -o addopts=""` | **92 passed** (8.63 s) |
| `python -m pytest tests/test_core_commands.py tests/test_engine_tools.py tests/test_state_briefing.py tests/test_engine_tool_env_render.py tests/test_engine_port_finish.py tests/test_run_reuse_identity.py tests/test_tool_shape_fixes.py tests/test_argument_validation.py tests/test_requirement_coverage.py -q -o addopts=""` | **94 passed** (22.18 s) |
| `python -m pytest tests/test_session_export.py tests/test_bridge_core.py tests/test_command_jobs.py tests/test_browser_core.py tests/test_check_probes.py tests/test_repo_command_safety.py tests/test_skill_runtime.py -q -o addopts=""` | **114 passed, 2 skipped** (46.99 s) |
| `python -m ruff check src tests scripts` | **3 pre-existing F401** (exit 1, см. 5.3) |
| `python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui` | **Success: no issues found in 13 source files** |
| `python -m mypy src/karox/git_baseline.py` (вне объявленной mypy-области) | **Success** |
| `git diff --check` | чисто |

Итого уникальных проверенных тестов: 114 + 92 + 94 + 114 = **414** (наборы файлов
не пересекаются между строками; 92 = 60 gate + 26 sessions + 6 tree).

### 5.2. Что проверяет матрица миссии

| Пункт | Тест | Red на `84c7b65`? |
|---|---|---|
| A. commit во время сессии | `test_a_commit_made_during_the_run_is_part_of_the_complete_review` | **да** (stdout пуст) |
| B. чужая база отклоняется | `test_a_head_relative_review_is_not_a_review_against_the_start_revision`, `test_a_legacy_review_without_a_durable_baseline_is_not_complete` | да (v1-«HEAD»-review принимался); `...[later_head]` — guard, старая версия его и так отклоняла |
| C. resume | `GitStartRevisionTests::test_the_start_revision_is_persisted_and_does_not_follow_head` (+ `continue_task`) | да (нет поля) |
| D. обычный путь без коммита | существующий `test_engine_run_verifies_a_change_end_to_end`, `test_a_complete_review_is_enough_and_the_harness_adds_nothing` | нет (guard; те же запросы/вызовы) |
| E. unborn | `test_a_complete_review_works_before_the_first_commit` (base=`unborn`) и `test_a_first_commit_inside_an_unborn_run_cannot_clinch_a_complete_review` | второй — **да** |
| F. staged+unstaged+untracked | `test_a_complete_review_reads_staged_unstaged_and_new_files` | нет (guard; контракт v2) |
| G. fail closed | `test_a_review_without_a_start_revision_is_not_complete`, `test_a_missing_start_object_fails_the_review_closed`, 7 параметров `test_a_corrupt_start_revision_cannot_close_a_complete_review`, `test_a_read_only_session_without_a_baseline_owes_no_review`, `test_a_record_written_before_the_start_revision_existed_still_loads` | частично да |
| H. redaction/бюджет | `test_a_truncated_tracked_diff_is_not_a_complete_review` | нет (guard: поведение сохранено) |

Дополнительно: `test_a_commit_after_the_complete_review_does_not_hide_the_change`
(см. §3.4) и `test_an_unborn_start_revision_matches_only_an_unborn_review`.

### 5.3. Pre-existing красные гейты (записано, не исправлялось)

1. **`python scripts/check_tree.py`:** `package grew to 86869 lines (ceiling 80000)`,
   exit 1. До миссии было 86 531; это изменение добавило **+338** строк пакета.
   `tests/test_tree.py` (`assert lines < 87_000`) **проходит**, запас — **131**
   строка. Порог не поднимался; если следующее изменение перешагнёт 87k, это будет
   отдельным осознанным решением, а не автоматической правкой.
2. **`python -m ruff check src tests scripts`:** те же три F401, что и в Mission 03,
   в файлах, которые миссия не трогала:
   `src/karox/session_export.py:24` (`typing.Optional`),
   `tests/test_tool_shape_fixes.py:10` (`os`), `:20` (`karox.models.CoreCommand`).

## 6. Экономика

Один и тот же сценарный прогон (write `hello.txt` → finish) на старом и новом
дереве; Git-процессы считались обёрткой над `core._run` (только `argv[0] == git`).

```text
no-commit (модель не просит review):
  OLD 84c7b65: verified=true, model_requests=2, git.diff=1, git processes=4
               (status, rev-parse HEAD, diff HEAD, ls-files), stdout="+hi"
  NEW 108b530: verified=true, model_requests=2, git.diff=1, git processes=3
               (status, diff <A>, ls-files), stdout="+hi"

in-session commit (ELEVATED):
  NEW: verified=true, model_requests=3, git.diff=1, git processes=6
       (add, commit, rev-parse HEAD, status, diff <A>, ls-files), review покрывает коммит

модель сама берёт полный review (write → check → status+diff → finish):
  NEW: verified=true, model_requests=4, git.diff=1 (её собственный),
       git processes=3 (status, diff <A>, ls-files), harness_final=[] 

resume (уже verified сессия):
  model_requests=0, reason=already_verified, baseline идентичен записанному
```

- **Model requests: +0** во всех случаях; **`git.diff` calls: +0**.
- **Git-процессов на полный review: −1** (3 → 2), потому что база-коммит не требует
  `rev-parse`; `ls-files` остаётся.
- **Создание сессии: +1 Git-процесс.** Измерено на этой машине: медиана **47.8 мс**
  для репозитория с коммитом; ~99 мс, если путь не является репозиторием (два
  неудачных чтения); таймаут ограничен 15 с. Ошибка/отсутствие Git создание сессии
  не блокируют — пишется `unavailable`.
- **Дополнительные чтения с диска:** только `session.json` (+~150 байт на запись,
  файл и так переписывается каждый шаг). Полного обхода дерева нет, новых файлов
  нет. Untracked-review читает те же файлы, что и раньше, с теми же границами
  (≤2 МБ на файл, ≤20 000 символов показывается, ≤400 000 на review, ≤200 файлов).
- **Digest:** `stdout_sha256` теперь пересчитывается после сборки review (маркер,
  untracked-секции, второй Git-вид), поэтому evidence описывает именно те байты,
  которые отдаются; раньше digest мог отставать при неуспешном tracked-diff.
- **Токены:** новых секций в prompt нет; review-метаданные выросли на два коротких
  поля (`base_commit`, `commands`) в уже сохраняемом результате.

## 7. Остаточные риски и известные проблемы (честно)

1. **Legacy-сессии (созданные до Mission 04) с `changed_files` не сертифицируются:**
   базы нет, `normalize({})` → `None`, review отклоняется, resumed-прогон не
   получит `VERIFIED` без нового review — а новый review тоже не пройдёт, пока
   сессия не пересоздана. Read-only сессии не затронуты. Это осознанный fail-closed
   вместо выдачи текущего HEAD за стартовый.
2. **Пропавший стартовый объект** (reset/GC вне прогона): `git diff <oid>` падает,
   review честно `complete:false` с причиной «start revision could not be read»;
   прогон unverified. Лечится новой сессией.
3. **Truncated tracked diff** (потолок захвата 1 МБ): `complete:false`, прогон
   unverified — политика Mission 03 сохранена. Для очень «грязных» деревьев это
   false negative.
4. **Gitignored deliverables** (документированная дыра Mission 03) не изменились:
   файл, исключённый `.gitignore`, не попадает ни в diff, ни в untracked-список.
   Следующий milestone.
5. **Binary/oversized untracked-файлы** по-прежнему представлены маркером
   (размер/sha256), а не текстом — `limited`, не «показан текст».
6. **Рабочий каталог — подкаталог репозитория:** `git diff` исполняется с `cwd`
   репозитория KaroX и, как и раньше, ограничен префиксом; база при этом — HEAD
   всего репозитория. Поведение не менялось, но reviewer должен знать.
7. **Hosted bridge не запускался** (запрет на bridge/tunnel в миссии). Путь
   проектный: `hosted_core` вызывает тот же `CapabilityCoreRuntime.execute`, где
   база ставится из загруженной записи; отдельного hosted-теста нет.
8. **Платные модели не запускались**, live-прогонов нет; все доказательства —
   офлайновые scripted-прогоны реального Core/engine.
9. **`git.commit` остаётся ELEVATED-only**; профиль по умолчанию не расширен, в
   тестах коммит идёт через `AccessProfile.ELEVATED` + grant `GIT_COMMIT`.
10. **Создание сессии теперь делает одно Git-чтение**: на зависшем монтировании
    это до 15 с задержки (bounded timeout), после чего `unavailable`.
11. **`check_tree` 86 869 > 80 000** и **три F401** — pre-existing, вне миссии
    (§5.3); запас `test_tree` — 131 строка.

## 8. Команды для независимого ревью

```text
git log --oneline -4
git show --stat eefa1cc
git show --stat 108b530
git diff 84c7b65..108b530

python -m pytest tests/test_completion_gate.py tests/test_sessions.py \
  tests/test_tree.py -q -o addopts=""
python -m pytest tests/test_engine_kernel.py -q -o addopts=""        # 153 s
python -m pytest <9 соседних модулей из §5.1> -q -o addopts=""
python -m ruff check src tests scripts                                # 3 pre-existing F401
python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui
python scripts/check_tree.py                                          # pre-existing red (86 869 > 80 000)
```

Рекомендация ревьюеру: (1) проверить, что `git diff <oid>` покрывает staged и
unstaged половины, удаление и переименование (Git делает это нативно, отдельного
кода нет); (2) что `version: 2` + сверка `base_commit` действительно не даёт
закрыть floor review'ом «от текущего HEAD»; (3) что ContextVar ставится из уже
загруженной записи и только вокруг handler'а (нет утечки между сессиями одного
runtime); (4) что unborn-контракт не ломает документированный первый коммит и
явно отказывает после коммита внутри сессии; (5) что legacy-запись без поля
читается, но не сертифицируется.

## 9. Решение и следующий milestone

**GO — кандидат на независимый аудит, не release-ready.** Дефект подтверждён
офлайн до изменения (пустой review при живом коммите), фикс минимален по
поверхности (одно durable-поле + точная база в review + сверка в gate), red→green
доказан, обычный путь не подорожал ни моделью, ни tool-вызовом. Остаточные риски
названы; два pre-existing красных гейта не связаны с этим изменением и не
исправлялись.

**Единственный следующий milestone:** сделать gitignored deliverable видимым для
закрывающего review (или явно объявить его неподдерживаемым на gate), не расширяя
права записи.

---

## Machine-readable summary

```yaml
mission: dsh-durable-run-baseline-20261106
starting_head: 84c7b650cde89c611beefdebaf26470b825f3955
branch: audit/dsh-run-baseline-20261106
code_commits:
  - eefa1cc796b8e2c6a3701bc0ffd00ec2968486fb
  - 108b530d851b040eceab968284b048f9d572507a
report_commit: docs-only follow-up commit containing this file
worktree_clean: true
repro_before_fix: true
same_session_commit_review_before: "verified=true, review.version=1, base=HEAD, tracked stdout empty, HEAD advanced to C"
same_session_commit_review_after: "verified=true, review.version=2, base=start, base_commit=A, stdout shows '+hi', untracked.listed=0"
start_revision_persisted: true
resume_retains_baseline: true
unborn_head_behavior: "ordinary new-file review works (base=unborn, index+unstaged+untracked); a first commit created inside the session fails closed and the run stays unverified"
new_tests: 24
targeted_tests_passed: 24
targeted_tests_failed: 0
model_request_delta: 0
closing_diff_call_delta: 0
full_suite_run: false
paid_api_calls: 0
push_performed: false
remaining_blockers:
  - "scripts/check_tree.py: 86869 package lines > 80000 ceiling (pre-existing)"
  - "three pre-existing ruff F401s in untouched files"
next_milestone: "make a gitignored deliverable visible to the closing review (or explicitly unsupported at the gate)"
report_path: docs/audits/DSH_RUN_BASELINE_20261106.md
```
