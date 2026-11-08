# KaroX 5 — независимая проверка происхождения артефактов и маршрута модели

Дата: 2026-11-08, ветка goal/gpt6-sol-triad-20261108.
Этап только DEV/offline, без новых оплачиваемых трёхсторонних запусков.

## Реальный новый блокер, исправленный до scoring

Первый вариант внешнего Windows regrader для Three.js #35003 сравнивал
`hash_tree`, вычисленный через `sorted(Path)`, с хешем, полученным в WSL Linux.
Windows сортирует имена без учёта регистра, Linux — с учётом. Для
*одинакового* оригинального Three.js r186 получались разные SHA:

- Windows old ordering: d018b1342e5d04ee7660c1d4a0288a30f0f44d9a0dec68ee4b9cab88b5f2a7c1
- Actual Linux sorted pathlib: 82d6a6dea04e36314e472d56a2edf73ceadbea774f02bee8316ba57d8d0e9134

Из WSL Linux получен настоящий контрольный файл
`audit/LINUX_ORIGINAL_TREE_HASHES_20261108.json`.
Новый Windows regrader использует буквальный POSIX relative-path sort
(тот же порядок, что оригинальный Linux runner), а не Windows Path ordering.

## Запрет ложных побед из некорректной записи

Исходный `quality_classification.status="INVALID"` теперь не может быть
повышен до clean_user_success даже при внешнем WebGL PASS. Если Linux
runner зафиксировал инфраструктурную блокировку, это не поражение.
Отдельно требуется ненулевой реальный staged patch (task требует изменение
кода), подтверждённое успешное завершение CLI, целостность config.
Официальный model ID и W&B provider endpoint проверяются на точное
совпадение с замороженным протоколом перед Chromium:

- model: deepseek-ai/DeepSeek-V4.1-Flash
- provider: https://api.inference.wandb.ai/v1

Серверная идентичность внутреннего model checkpoint и реальные API invoices
по-прежнему неизвестны; эти проверки не создают такого доказательства.

## Git provenance перед оценкой браузером

Добавлена локальная только-читающая утилита
`audit/verify_saved_run_provenance.py`, которая проверяет:

1. Официальное имя arm из ровно KaroX 5 / OpenCode V2 / DSH.
2. SHA immutable task prompt, initial fixture и независимого grader.
3. Содержимое Git HEAD baseline через Git blob SHA файлов против оригинала.
4. Сохранённый `changes.patch` против `result.json` (SHA и длина).
5. Git staged patch против реального saved `changes.patch`.
6. **Все** рабочие файлы против содержимого Git stage по Git blob OID.
7. Отсутствие незаписанных в Git untracked файлов и unsafe symlinks.

Это не полагается на Windows `git diff` working tree: на историческом
Rich #4246 Windows `core.autocrlf=true` показывал ложные изменения
около 100 файлов, хотя их байты и Git blob SHA были абсолютно идентичны.
Проверка provenance **PASS для всех трёх реальных артефактов Rich #4246**
(KaroX/OpenCode/DSH). Отрицательные тесты на *копиях* этих артефактов
успешно отвергают подменённый patch, изменённый workspace и чужой task id.

Внешний Windows regrader теперь вызывает эту проверку **перед** реальным
Chromium. Если provenance сломан, оценка блокируется как
`EvidenceIntegrityError`, а не засчитывается конкуренту как ошибка кода.
Общая трёхсторонняя оценка требует также integrity, точный model route
и отсутствие setup block.

## Независимые проверки

- Первоначальный намеренный RED контракт regrader:
  1 failed / 4 passed, зафиксирован в
  `audit/THREE_35003_HOST_REGRADE_RED_20261108.log`.
- После исправления: 5/5 PASS, исходный RED лог сохранён.
- Проверка сохранённых Rich #4246: 3/3 реальные arms provenance PASS.
- Отрицательные тесты provenance и другие контракты:
  `audit/THREE_35003_EVALUATOR_FINAL_GATE_20261108.json`:
  **12 passed, 0 failed**, ruff PASS, py_compile PASS.
- `audit/PROVENANCE_REGRESSION_20261108.json`:
  6 PASS, включая 3 реальных arm и 3 отказа на изменённых копиях.
- Исходный Three.js v2 grader SHA
  9dd7db762590796de132a7f3d719bf2f3032a9c88e9f8f78de501cb6dd3cc445
  и его RED/GREEN визуальные результаты **не переписывались**.

## Другие ограничения

Полный исходный regression охват Karo: 1 575 collected / 1 557 PASS /
18 SKIP / 0 FAIL в семи отдельных завершённых группах; один новый
security test отдельно PASS. Глобальный mypy TUI по-прежнему FAIL:
399 диагностик, распределение записано в
`audit/MYPY_TUI_DEBT_BASELINE_20261108.json`.
Сама сборка wheel ранее прошла ruff/compile/checker/браузерные проверки.

Трёхстороннего запуска по новой #35003 **нет**, платные прогоны
были заблокированы средой и не обходились. Единственный scored matched
Linux real SWE trio — исторический Rich #4246 (3/3 correct).
Никакой глобальной победы KaroX не утверждается.

Нельзя менять grader после наблюдения ответов участников ради улучшения
таблицы. Все изменения этого этапа — исправления **оценочной
инфраструктуры до следующей эпохи**, а не требования к решению задачи.
Пользовательская установка и архивы предыдущих эпох не затронуты.
