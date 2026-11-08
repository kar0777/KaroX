# HARD20_FINAL — фактический срез HARD20, 8 ноября 2026

> **НЕ ЗАВЕРШЁННАЯ 20-задачная гонка.** Это проверяемый отчёт по реально проведённым API-вызовам и независимым тестам, а не список планов. Первоначальный пустой Phase0-отчёт сохранён как `audit/HARD20_FINAL_PHASE0_INTERIM_ARCHIVED_20261108.md`.

## Авторизация и ограничения

- Новый приоритетный приказ: `docs/benchmarks/HARD20_SOL_EXECUTE_MAX_20261108.md`, SHA-256 `830ebf15818db2fd61722f0e65328278cdf006435f2e44d14706ef228139d659`.
- Действующая финансовая политика `HARD20_BUDGET_POLICY.json`: `OBSERVE_ONLY_NO_HARD_CAP`. Старые жёсткие $3/$30, токены, steps и API-call бюджетные лимиты отменены. $30 — только ориентир, **не** cap.
- Старое бюджетное решение сохранено в `audit/HARD20_BUDGET_POLICY_PHASE0_ARCHIVED_20261108.json`. Правила запрета Anthropic, личных проектов, push, утечки секретов и disk/RAM safeguards продолжают действовать.
- Модель всех реальных вызовов: `deepseek-ai/DeepSeek-V4.1-Flash`, W&B `https://api.inference.wandb.ai/v1`. Точный удалённый checkpoint hash не доказан. W&B max effort подтверждался отдельным HTTP 200 smoke, но битовая эквивалентность фактического hidden reasoning между CLI **не доказана**.
- Основной Karo-PC Git branch при проверке: `karox-v6`, HEAD `fd1e9cf`. Рабочее дерево огромно/грязно ещё до продолжения; чужие изменения не сброшены. Лабораторные файлы локализованы внутри `lab/kernel-duel-20261025/b/gpt6-sol-triad-20261108/`.

## Реальное выполнение

| Показатель | Состояние |
|---|---|
| Кандидатов в `HARD20_TASK_MANIFEST.json` | 20 |
| Полностью замороженных с локальным RED/GREEN в данной программе | **1** |
| Задача с реальными платными прогонами | Three.js OrbitControls #35003, `real-three-orbitcontrols-35003-v2` |
| Сохранённых terminal `result.json` HARD20 на этой задаче | **13** (включая no-route/setup ошибки) |
| Из них terminal records с токенами модели | **5** |
| Дополнительный запуск с безопасной остановкой до записи terminal result | **1** (OpenCode `hard20-sol-max-opencode-20261108-004`) |
| Независимые Chromium PASS по подтверждённым patched workspaces | KaroX candidate, OpenCode V2, DeepSeek Harness |
| Подтверждённых clean CLI + independent grader PASS | **DeepSeek Harness**, минимум 2 DEV попытки |
| Завершённых строгих 20-task/60-run blind HOLDOUT гонок | **0** |
| Подтверждённый провайдером W&B invoice | **нет данных** |

Не смешивать повторные DEV attempts с независимыми публичными задачами. Строгая тройка с тремя clean терминальными исходами в одном замороженном и заранее выданном порядке пока **не получена**. Поэтому никакой общий win rate или победа Karo **не заявляется**.

### Точная независимая оценка на неизменённом DEV grader v2

Источник: GitHub Three.js issue #35003, upstream tag `r186` (commit `9b4a2ac29c63ccb43fd51c5661f2f873ac2c39b8`), MIT. Задание SHA-256 `98f2f823a681050adf4a82a8df16af281ba2f477b335ff5a39d1e754b210f312`. Grader SHA-256 `9dd7db762590796de132a7f3d719bf2f3032a9c88e9f8f78de501cb6dd3cc445`; RED исходника exit 1, GREEN закрытого reference exit 0, `grader` и reference недоступны агентам. Chromium/WebGL использовался отдельно на Windows, поскольку в WSL eval-venv нет Playwright.

| Арм и идентификатор прогона | CLI / время | Внешний Chromium | Clean success |
|---|---|---|---|
| **KaroX 5 repaired wheel** — `paired-linux/hard20-max-35003-20261108-002` | exit 1, ~116.5 сек | **PASS** | **Нет**: `unproducible_requirement`, агент не признал свою задачу проверенной |
| **Official OpenCode V2** — `paired-linux/hard20-max-35003-20261108-003` | exit -9, ~737.4 сек | **PASS** | **Нет**: resource guard убил процесс, но patch уже был корректным |
| **Genuine DeepSeek Harness** — `paired-linux/hard20-max-35003-20261108-002` | exit 0, ~266.5 сек | **PASS** | **Да** |
| **Genuine DeepSeek Harness** — `hard20-max-runs/hard20-slot13-three35003-max-20261108-a` | exit 0, ~465.4 сек | **PASS** | **Да** |

Это **разные попытки одной DEV задачи**, а не четыре независимых задачи. Linux `grader_exit=2` для существующих прогонов — ошибка инфраструктуры `ModuleNotFoundError: playwright`, не семантический FAIL. Соответствующий независимый Windows regrade хранится отдельно и не переписывает исходные результаты. У OpenCode 003 `quality_classification=INFRA_SETUP_BLOCKED` из-за памяти, несмотря на external artifact PASS.

## Причины ошибок и корректирующие действия

1. **KaroX false-negative completion.** У repaired candidate Karo есть реальный patch, публичные тесты (`2 passed`) и независимый Browser PASS. Видимый tool trace показывает повторные неудачные попытки запуска собственного browser.command. В результате agent exit 1 `unproducible_requirement` при фактически правильных байтах. Это **не false VERIFIED** и **не чистое решение**; механизм — разрыв между достижением качества кода и доступностью собственного доказательства. Нельзя просто снимать visual gate, нужно изучить fallback independent evidence.
2. **OpenCode route mismatch.** Ранние попытки выходили через `provider.no-route` / `Variant unavailable: max`. Offline `opencode models --standalone` с временным тестовым ключом подтвердил обнаружение кастомной модели `wandb-triad/deepseek-ai/DeepSeek-V4.1-Flash`; последующий реальный #max 003 успешно обращался к W&B. MAX-конфигурация и сравнение целостности config исправлены в лабораторных адаптерах. No-route не является проигрышем качества.
3. **OpenCode memory limit.** 003 был убит из-за устаревшего лимита RSS ~1 GiB, хотя независимый grader проверил верный патч. Лабораторный монитор теперь имеет явную настройку HARD20 до 2 GiB RSS с сохранением резервов C/D и отдельной проверкой Windows RAM до запуска.
4. **Остановка 004 для безопасности пользователя.** После ~496 секунд Windows `MemAvailable` уменьшилась до ~1.55 GiB (занято 93.5%). Вызван cancel **только owned command job** `job-81b204a5e8bfc7f84e9c`; подтверждён статус `cancelled`, затем Windows RAM восстановилась >3 GiB и активных WSL `opencode/dsh/karox/node` не обнаружено. 004 не получил terminal ledger и его потенциальные оплаченные provider calls **не равны нулю**; стоимость неизвестна. Постоянное свидетельство безопасной остановки: `audit/HARD20_RESOURCE_CANCEL_OPENCODE_004_20261108.json` (owned job, RAM-триггер, recovery, отсутствие terminal результата). Ресурсный safety stop не является бюджетным watchdog и не считается quality FAIL.
5. **Нет 20 замороженных задач.** 19 из 20 кандидатов ещё не имеют локально проверенного независимого RED/GREEN, точной установленной среды или изолированного grader. SWE/Terminal Docker images нельзя без предварительного resource gate бездумно вытягивать на текущем C:. Не подменять их лёгкими smoke fixtures и не называть DEV blind holdout.

## Прозрачная стоимость (не invoice)

Офлайн воспроизводимый `audit/hard20_cost_accounting.py` прочёл 13 окончательных `result.json`, из которых у пяти есть token usage. Журнал и SHA каждого результата: `HARD20_ACCOUNTING_20261108.json`.

- При output-only трактовке отдельного OpenCode reasoning поле: сумма тарифных **оценок** по этим пяти файлам **$0.44856069**.
- Если отдельно сообщённые 42 903 reasoning tokens OpenCode 003 **добавлять** к billable output, альтернативная оценка **$0.47644764**.
- У восьми terminal записей без usage и остановленного 004 точный расход неизвестен. Ни одна оценка не является W&B invoice; поэтому нельзя утверждать полный total cost или cost-per-clean-solved всех 20 задач.
- Тарифная формула по политике: `(0.20 * input_uncached + 0.03 * input_cached + 0.65 * output) / 1_000_000` USD. Кэш и reasoning представлены раздельно там, где доступны.

## Просмотрщик, архивы, ресурсы

- Запущена генерация автономного локального `hard20-max-runs/HARD20_TRACE_VIEWER_20261108.html` через `audit/hard20_trace_viewer.py`: offline HTML на D:, выбор run/task/arm, tool timeline, patch, stderr, grader и usage. При актуальном восстановлении просмотрщика обнаружено 35 исторических terminal artifacts, но **они не считаются 34 независимыми HARD20 задачами**. Скрытое reasoning, которое API не отдаёт, не выдумывается; сырые redacted event streams остались в архивах.
- Известные архивы `*.HARD20_EVIDENCE.zip` содержат SHA-манифесты и проверку roundtrip; Windows regrade сохраняет PNG/event_result hash. **Реализована safety-only уборка для последующих HARD20 запусков:** `audit/hard20_temp_ownership_cleanup.py` записывает перед агентом точные disposable temp paths с inode/device и после ZIP roundtrip/SHA удаляет исключительно два зарегистрированных workspace/profile дерева. При нехватке результата, повреждённом архиве, symlink, ином inode, отсутствии snapshot или изменённом temp parent очистка отказывается. Изменён `audit/hard20_live_one.py`, чтобы архивировать перед уборкой. Для старых и незарегистрированных аварийных корней 004 уборка **не проводилась**: неизвестные файлы не удалять. Запрещены `git clean`, `git reset --hard`, чужие папки.
- Ограничения: свободное C: >=7 GiB, D: >=15 GiB, один тяжёлый CLI/Chromium за раз, Windows RAM guard. После cancel временная память Windows восстанавливается. Никаких Anthropic запросов, GitHub push или модификаций личных проектов.
- Проверки после правок: `pytest audit/test_hard20_safe_cleanup.py audit/test_hard20_preflight.py` — **25 passed** (4 safety-negative/positive cleanup + 21 financial/resource tests); `ruff check` всех изменённых Python-файлов — **PASS**; Python compile и offline OpenCode model inventory — PASS.

## Следующий честный milestone

Добиться завершения official OpenCode без RAM depletion на отдельном resource-balanced хосте или с менее тяжёлой конфигурацией, сохраняя максимальный доступный reasoning и исходные критерии. Затем заморозить **новую сложную публичную задачу** с реальным RED/GREEN и провести на ней полную последовательную тройку. Повторять вплоть до 20 задач только в пределах реальной безопасности и ресурсов. Улучшения Karo на DEV — исключительно после фиксации исходных доказательств; новые изменения проверять на новых независимых задачах.

**Дата записи: 2026-11-08. Это честная история реальных запусков, включая неудачи, а не утверждение о завершённых 60 тестах.**
