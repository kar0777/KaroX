# DSH Mission 06 — Honest release gates (2026-11-06)

**Verdict. ACCEPT CANDIDATE по миссии; release-ready не заявляется.**
Рассинхронизация потолков устранена без отключения guard'а: `scripts/check_tree.py`
и `tests/test_tree.py` теперь читают одно объявление (`MAX_PACKAGE_LINES =
88_000`), один счётчик и одно решение `count >= ceiling`. Пакет измерен в
**86,996** строк, запас до потолка — **1,004** строки (~1.15%). Три
pre-existing Ruff F401 удалены тремя import-only правками. Негативная
регрессия доказывает отказ на нижнем test-only пороге; позитивная — совпадение
корня/счётчика/потолка у обоих интерфейсов. Полный pytest, wheel, live-модели,
браузер, bridge/tunnel и платные API не запускались; push/merge не выполнялись.

## 1. Git reality

| Поле | Значение |
|---|---|
| Абсолютный путь | `D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\win` |
| Git top-level | тот же путь (linked worktree, `.git` — файл) |
| Исходная ветка | `audit/dsh-gitignored-review-20261106` (совпала с ожидаемой) |
| Исходный HEAD | `dd636baa3030a3715590c2e2f492b54c5427dae7` (полный OID сверен; совпал с ожидаемым Mission 06) |
| Предыдущий code commit | `4a8ee48045dbbc8bb17a47259a6f2f5adb97b668` (совпал) |
| Состояние на старте | `git status --porcelain` пуст |
| Рабочая ветка | `audit/dsh-release-gates-20261106` (создана от HEAD) |
| Code + tests commit | `e55a22adfc044d65186475b6ee9c90b92bb60964` (parent `dd636ba`) |
| Report/docs commit | отдельный docs-only коммит с этим файлом, `CHANGELOG.md` и `DEVELOPMENT.md` (hash — в сообщении о завершении) |
| Финал | `git status --short` пуст после коммита отчёта |
| Другие деревья | не изменялись; `git worktree list` прочитан, работа шла только в `b\win` |

Проверка до любых изменений:

```text
git rev-parse --show-toplevel  -> D:/проекты/KaroX-v5/lab/kernel-duel-20261025/b/win
git branch --show-current      -> audit/dsh-gitignored-review-20261106
git rev-parse HEAD             -> dd636baa3030a3715590c2e2f492b54c5427dae7
git status --porcelain         -> (пусто)
git log -6 --oneline           -> dd636ba docs..., 4a8ee48 engine..., 6ef51d8 docs...,
                                  108b530 engine..., eefa1cc tests..., 84c7b65 docs...
```

Прочитаны `AGENTS.md`, `docs/DEVELOPMENT.md`, `scripts/check_tree.py`,
`tests/test_tree.py`, `tests/conftest.py`, `pyproject.toml` и отчёт Mission 05
`docs/audits/DSH_GITIGNORED_REVIEW_20261106.md`. Секреты, `.env`, keyring и
приватные конфиги не читались и не печатались.

## 2. Root cause расхождения 80k / 87k

На исходном HEAD обе проверки считали **одну и ту же величину одним и тем же
выражением**, но сравнивали её с **разными числами**:

- `scripts/check_tree.py:58` — `MAX_LINES = 80_000`, затем
  `if lines >= MAX_LINES` (`:131` на исходном HEAD);
- `tests/test_tree.py:158` — `assert lines < 87_000`;
- оба корня: `Path(__file__).resolve().parents[1] / "src" / "karox"`;
- оба счётчика: `sum(len(path.read_text(encoding="utf-8").splitlines()) for path in PACKAGE.rglob("*.py"))`.

То есть расхождение было **не в определении счёта, а только в константе**: скрипт
падал на 80k, тест проходил с запасом 4 строки (86,996 < 87,000).

Хронология дрейфа (проверено `git show --stat`; каждый коммит менял **только**
`tests/test_tree.py`):

| Commit | Дата | Потолок теста | `MAX_LINES` скрипта | Строк пакета |
|---|---|---|---|---|
| `a2332a1` | — | 80,000 | 80,000 | — |
| `108fc1d` | 2026-11-03 | **83,000** | 80,000 | 81,409 |
| `193e247` | 2026-11-04 | **85,000** | 80,000 | 83,278 |
| `630be7c` | 2026-11-05 | **86,000** | 80,000 | 85,228 |
| `44c46f1` | 2026-11-05 | **87,000** | 80,000 | 86,338 |
| `dd636ba` (старт) | 2026-11-06 | 87,000 | 80,000 | 86,996 |

`108fc1db197fbdc0957dac945fa9a31dd9119708`: `+5/−2` только в тесте; сообщение
коммита прямо говорит «The src line-count guard goes from 80k to 83k». Так же
`193e247` (`+7/−2`), `630be7c` (`+7/−2`), `44c46f1` (`+8/−2`). Скрипт после
`a2332a1` не обновлялся ни разу, хотя комментарий в тесте фиксировал каждое
повышение. Миссии 01–05 это видели и оставляли как pre-existing
(`DSH_VERIFIED_AUDIT`, `DSH_REUSE_INTEGRITY`, `DSH_RUN_BASELINE`,
`DSH_GITIGNORED_REVIEW`), потому что это был отдельный release-hygiene дефект.

### 2.1. Аудит определения счёта (до правки)

Проверено на рабочем дереве `dd636ba`:

- 162 файла `*.py`; ровно **86,996** строк обоими счётчиками;
- symlink'ов нет; не-UTF-8 файлов нет; `.py` внутри `__pycache__` нет;
- скрытых каталогов/файлов, которые один счётчик включал бы, а другой нет, нет —
  оба используют один `rglob("*.py")` от одного корня;
- дубликатов содержимого нет: 162 уникальных SHA-256 (ни одна копия не
  повторяется); крупнейший файл — `browser/session.py` (3,642 строки);
- сгенерированных/вендоренных «сюрпризов» нет.

Вывод: считать «два разных определения» не нужно — определения совпадали,
разъезжалась только константа.

### 2.2. Аудит роста пакета

`src/karox` измерен на ключевых коммитах (git plumbing, без checkout):

| Commit | Что | Строк | Δ |
|---|---|---|---|
| `44c46f1` | потолок теста 87k | 86,338 | — |
| `ae9f96b` | closing page | 86,374 | +36 |
| `67271be` | Mission 01 code | 86,403 | +29 |
| `f831a9a` | Mission 02 code | 86,174 | −229 |
| `b9911ae` | Mission 03 code | 86,531 | +357 |
| `108b530` | Mission 04 code | 86,869 | +338 |
| `4a8ee48` | Mission 05 code | 86,996 | +127 |
| `dd636ba` | Mission 05 docs | 86,996 | 0 |

Missions 01–05 дали **+622** net-строки; суммарно с `ae9f96b` — **+658**.
`git diff --stat 44c46f1..4a8ee48 -- src/karox` (до этой миссии) показывает
8 файлов, `911 insertions(+), 253 deletions(-)`, один новый модуль
`src/karox/git_baseline.py` (206 строк), и ничего похожего на копию пакета,
сгенерированный дамп или случайно закоммиченный вендор. Рост deliberate:
verification reuse, whole-change review, durable run baseline, ignored coverage,
session persistence. Маскировать его повышением потолка «на глаз» не
требовалось — требовалось одно осознанное число.

## 3. Методология счёта, потолок и запас

Единственное объявление (`scripts/check_tree.py:69`):

```python
MAX_PACKAGE_LINES = 88_000
```

Единственный счётчик (`scripts/check_tree.py:72`) — все `*.py` рекурсивно под
`src/karox`, UTF-8, `splitlines()` (без лишней пустой строки в конце):

```python
def package_line_count(package: Path) -> int:
    return sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in package.rglob("*.py")
    )
```

Единственное решение (`scripts/check_tree.py:80`):

```python
def line_budget_problem(lines: int, ceiling: int = MAX_PACKAGE_LINES) -> str | None:
    if lines >= ceiling:
        return f"package grew to {lines} lines (ceiling {ceiling})"
    return None
```

`main()` (`scripts/check_tree.py:155`) использует именно её и по-прежнему
возвращает 1, если `problem is not None`. `tests/test_tree.py:143` вызывает те
же функции из загруженного скрипта, а не свою копию выражения.

**Измерено: 86,996 строк. Потолок: 88,000. Запас: 1,004 строки (~1.15%).**

Почему 88k, а не произвольное число:

- прежний тестовый потолок 87k давал 4 строки запаса — фактически выключенный
  guard для любого следующего коммита;
- Missions 01–05 добавили 622 строки за 5 миссий (~124/миссию); 1,004 — это
  примерно 1.6× наблюдаемого роста пяти миссий и ~8 средних инкрементов;
- это малое круглое число над фактическим размером, а не 100k/120k: копия
  пакета (≈87k) или сгенерированный дамп (тысячи строк) по-прежнему валит
  гейт мгновенно;
- если следующий крупный инкремент превысит 1,004 строки, гейт покраснеет, и
  потолок придётся поднимать отдельным осознанным решением с новым аудитом —
  это и есть требуемое поведение.

## 4. Почему это не «отключённый guard»

- Проверка остаётся **hard failure** в обоих интерфейсах: `check_tree.py`
  возвращает 1, `tests/test_tree.py` падает на assert. Warning-режима нет.
- Не добавлено ни одного `noqa`, `# type: ignore`, исключения каталога,
  `--fix` по репозиторию или отключённого правила Ruff; `LEGACY`,
  `ENGINE_MODULES`, internal-import, pycache- и content-проверки
  `check_tree.py` не тронуты.
- Число теперь объявлено **один раз**. Тест загружает сам скрипт
  (`tests/test_tree.py:54`) и берёт из него `PACKAGE`, `package_line_count`,
  `line_budget_problem`, `MAX_PACKAGE_LINES`; расхождение 80k/87k структурно
  невозможно.
- Позитивный тест дополнительно фиксирует намеренное значение
  (`assert _RELEASE_GATE.MAX_PACKAGE_LINES == 88_000`,
  `tests/test_tree.py:164`): поднять потолок «молча» нельзя — придётся править
  и объявление, и assert, и это будет видно в diff.

## 5. Удалённые импорты

Перед удалением каждый name проверен поиском по файлу: встречается **только**
в строке импорта. Никаких runtime-обращений, аннотаций или `get_type_hints` с
этим именем нет.

| Файл:строка | Удалено | Доказательство |
|---|---|---|
| `src/karox/session_export.py:24` | `typing.Optional` | единственное вхождение — сам импорт; в модуле есть `from __future__ import annotations`, остальные имена (`Any, Dict, Iterable, List, Mapping`) используются |
| `tests/test_tool_shape_fixes.py:10` | `os` | единственное вхождение в файле; `os.` нигде нет |
| `tests/test_tool_shape_fixes.py:20` | `core.models.CoreCommand` | единственное вхождение; `AccessProfile, Capability, Origin, OriginKind` из той же строки используются |

Правки — три точечных удаления имён; `ruff check src tests scripts --no-cache`
зелёный, `tests/test_tool_shape_fixes.py` (12 passed) и
`tests/test_session_export.py` (4 passed) проходят. Широкий `ruff --fix` не
запускался.

## 6. Before / after и регрессии

### 6.1. До правки (`dd636ba`, read-only)

```text
$ python scripts/check_tree.py
check_tree: package grew to 86996 lines (ceiling 80000)
exit=1

$ python -m pytest tests/test_tree.py -q -o addopts=""
6 passed in 0.96s

$ python -m ruff check src tests scripts --no-cache
F401 ... src\karox\session_export.py:24:56 `typing.Optional`
F401 ... tests\test_tool_shape_fixes.py:10:8 `os`
F401 ... tests\test_tool_shape_fixes.py:20:53 `karox.models.CoreCommand`
Found 3 errors.
exit=1
```

Других красных проверок `check_tree.py` не было — только строка бюджета.

### 6.2. После правки (`e55a22a` + docs)

```text
$ python scripts/check_tree.py
check_tree: ok · 162 modules · 86996 lines · no legacy surface
exit=0

$ python -m pytest tests/test_tree.py -q -o addopts=""
8 passed in 1.08s          # 6 прежних + 2 новых

$ python -m ruff check src tests scripts --no-cache
All checks passed!
exit=0

$ python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui
Success: no issues found in 13 source files
exit=0

$ python -m pytest tests/test_tool_shape_fixes.py -q -o addopts=""
12 passed in 0.78s

$ python -m pytest tests/test_session_export.py -q -o addopts=""
4 passed in 1.31s

$ git diff --check
(чисто)
```

### 6.3. Негативная регрессия

Тест `tests/test_tree.py:169`
`test_line_budget_fails_at_a_lower_test_only_ceiling`:

```python
lines = _RELEASE_GATE.package_line_count(PACKAGE)              # 86,996
failure = _RELEASE_GATE.line_budget_problem(lines, ceiling=lines)
assert failure == f"package grew to {lines} lines (ceiling {lines})"
assert _RELEASE_GATE.line_budget_problem(lines, ceiling=lines + 1) is None
```

То есть реальный измеренный пакет против **test-only** потолка, равного его
собственному размеру, обязан упасть; одна строка запаса — пройти. Реальный
проект и объявленный бюджет не меняются, глобальная константа не мутирует.
Одноразовая демонстрация того же решения вне pytest:

```text
measured package lines : 86996
declared ceiling       : 88000
headroom               : 1004
normal policy          : None                                  # проходит
test-only ceiling=count: package grew to 86996 lines (ceiling 86996)   # падает
test-only ceiling=count+1: None                                # проходит
```

Граница ровно требуемая: `count >= ceiling` → fail, `count < ceiling` → pass;
прежнее `>=` в скрипте сохранено, не ослаблено.

### 6.4. Позитивная регрессия

Тест `tests/test_tree.py:152`
`test_release_gate_and_tree_test_use_one_budget` проверяет, что оба интерфейса
согласны:

- `_RELEASE_GATE.PACKAGE == PACKAGE` — один и тот же корень;
- `package_line_count(PACKAGE) == independent` — счётчик скрипта совпадает с
  независимым выражением, которым исторически считал тест (86,996);
- `MAX_PACKAGE_LINES == 88_000` и `> lines` — объявленный потолок и
  положительный запас;
- `line_budget_problem(independent) is None` — по умолчанию (тот же потолок)
  решение проходит.

`test_python_line_count_stays_bounded` теперь тоже вызывает
`_RELEASE_GATE.package_line_count` и `_RELEASE_GATE.line_budget_problem`, так
что в тесте не осталось второго числа и второй формулы.

## 7. Стоимость, длительности, что не запускалось, красные гейты

Все команды запускались последовательно, по одному процессу, без `-n`/xdist;
локальные CPU-тесты, `PYTHONDONTWRITEBYTECODE=1`, `-o addopts=""`.

| Команда | Результат | Время |
|---|---|---|
| `python scripts/check_tree.py` | exit 0, 162 modules, 86,996 lines | 1.01 s |
| `python -m pytest tests/test_tree.py -q -o addopts=""` | 8 passed | 1.08 s |
| `python -m ruff check src tests scripts --no-cache` | All checks passed | 0.28 s |
| `python -m mypy ...` | Success, 13 files | 0.34 s (warm cache) |
| `python -m pytest tests/test_tool_shape_fixes.py ...` | 12 passed | 0.78 s |
| `python -m pytest tests/test_session_export.py ...` | 4 passed | 1.31 s |

Стоимость изменения: новых зависимостей нет, product/runtime-кода не
добавлено (правка только release-gate, тестов и трёх импортов), гейт остаётся
прежним по IO (один рекурсивный обход `src/karox`). Model turns, сеть, Git
subprocess'ы, keyring, bridge и provider-пути не затронуты.

**Не запускалось (осознанно, по resource cap миссии):**

- полный `python -m pytest` — в наборе есть live browser/3D/desktop-тесты;
  задание запрещает автоматический полный прогон;
- `python -m build --wheel` и установка wheel — не запускались;
- live-модели/платные API — **0**;
- Chromium/Playwright, 3D, bridge/tunnel, Docker/WSL, бенчмарки — **0**;
- push, merge, rebase, reset, clean, tag, deploy — **0**; другие worktree не
  трогались.

**Красные гейты:** среди названных в миссии детерминированных проверок красных
нет. Полный release checklist (`docs/DEVELOPMENT.md`: full pytest, wheel,
live smoke, bridge diagnostics) не выполнялся, поэтому «release-ready» не
заявляется.

## 8. Остаточные риски и judgement

1. **Тест загружает скрипт по пути** (`scripts/check_tree.py`). Если файл
   переименуют/перенесут, тест упадёт на collection — это громко, а не тихо;
   зато связка «тест → скрипт» намеренная.
2. **Потолок закреплён в позитивном тесте** (`== 88_000`). Любое повышение
   теперь требует двух согласованных правок и видно в diff; это осознанное
   трение против «тихого» повышения.
3. **Запас 1,004 строки конечен.** Следующий крупный инкремент покраснеет —
   это и есть смысл guard'а; поднимать потолок следует новым аудитом, а не
   «до 100k на всякий случай».
4. **Исторические документы** Mission 05 (`CHANGELOG.md`, отчёты аудитов)
   по-прежнему цитируют 86,996/87,000 и «check_tree red»; это описание своего
   момента, а не текущего состояния. Переписывать историю не стал; новая
   запись в `CHANGELOG.md` явно описывает переход.
5. **Бюджет — tripwire, а не дизайн-ревью.** Он ловит копию/дамп/массовый
   импорт, но осмысленность нового кода всё равно проверяется ревью.
6. **Полный suite/wheel/live smoke не проверялись** в этой миссии; зелёные
   точечные гейты не заменяют полный release checklist.

**Judgement: ACCEPT CANDIDATE** по существу миссии. Детерминированные
release-hygiene гейты согласованы и зелёные, негативный сценарий доказан,
поведение продукта не менялось. **NEEDS FURTHER WORK для релиза** только в
смысле невыполненного полного release checklist (full pytest, wheel, live
smoke) — это вне бюджета Mission 06.

## 9. Команды для независимого ревью

```text
git log --oneline -4
git show --stat e55a22a
git diff dd636ba..e55a22a

python scripts/check_tree.py                                          # exit 0
python -m pytest tests/test_tree.py -q -o addopts=""                  # 8 passed
python -m ruff check src tests scripts --no-cache                     # All checks passed
python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui # Success
python -m pytest tests/test_tool_shape_fixes.py tests/test_session_export.py \
  -q -o addopts=""                                                     # 16 passed
```

Рекомендация ревьюеру: (1) проверить, что в `tests/test_tree.py` не осталось
литерала потолка, кроме намеренного assert'а, и что guard вызывает функции
скрипта; (2) что `line_budget_problem` действительно fail-closed на
`count >= ceiling`, а негативный тест использует реальный измеренный count;
(3) что `MAX_PACKAGE_LINES = 88_000` — единственное операционное объявление и
запас 1,004 обоснован ростом Missions 01–05; (4) что удалены ровно три
неиспользуемых имени и нет `noqa`/игноров; (5) что остальные проверки
`check_tree.py` и поведение продукта не изменены.

---

## Machine-readable summary

```yaml
mission: dsh-release-gates-20261106
repository: D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\win
starting_branch: audit/dsh-gitignored-review-20261106
starting_head: dd636baa3030a3715590c2e2f492b54c5427dae7
branch: audit/dsh-release-gates-20261106
code_commit: e55a22adfc044d65186475b6ee9c90b92bb60964
code_commit_parent: dd636baa3030a3715590c2e2f492b54c5427dae7
report_commit: docs-only follow-up commit containing this file (hash in completion message)
worktree_clean_at_start: true
worktree_clean_after_report: true
drift_root_cause: "one count definition, two constants: check_tree.py:58 MAX_LINES=80000 vs test_tree.py:158 assert < 87000; test raised 83k/85k/86k/87k in 108fc1d/193e247/630be7c/44c46f1 without the script"
single_source_of_truth: "scripts/check_tree.py: MAX_PACKAGE_LINES + package_line_count + line_budget_problem"
test_reads_the_gate: "tests/test_tree.py:54 loads scripts/check_tree.py via importlib"
ceiling: 88000
measured_lines: 86996
headroom_lines: 1004
headroom_percent: 1.15
mission_01_05_net_lines: 622
growth_since_87k: 658
ruff_f401_before: 3
ruff_f401_after: 0
check_tree_before: "exit 1: package grew to 86996 lines (ceiling 80000)"
check_tree_after: "exit 0: ok · 162 modules · 86996 lines · no legacy surface"
tree_tests_before: "6 passed"
tree_tests_after: "8 passed"
negative_regression: "count=86996, ceiling=86996 -> failure message; ceiling=86997 -> pass"
positive_regression: "same PACKAGE root, shared count == independent oracle 86996, MAX_PACKAGE_LINES=88000, default decision passes"
mypy: "Success: no issues found in 13 source files"
tool_shape_tests: "12 passed"
session_export_tests: "4 passed"
full_suite_run: false
wheel_built: false
paid_api_calls: 0
push_performed: false
merge_performed: false
other_worktrees_touched: false
release_ready: false
verdict: ACCEPT CANDIDATE
remaining_red_gates: []
not_run: ["full pytest", "wheel build", "live provider smoke", "browser/3D", "bridge tunnel", "benchmarks"]
```
