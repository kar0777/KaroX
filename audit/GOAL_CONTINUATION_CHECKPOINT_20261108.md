# KaroX 5 — продолжение миссии, контрольная точка 2026-11-08

## Исходная точка и изоляция

- Рабочая ветка: `goal/gpt6-sol-triad-20261108`. Исходный HEAD перед этим этапом: `a4fe74c4a99c4cfa3d4d86f5c6c11d0c2f2c6520`.
- Исправление checker-origin остаётся из commit `487bb7b`; исходники продукта после него в этом этапе **не менялись**.
- Не было push, релиза, изменения пользовательской установленной KaroX, установки новых крупных пакетов или нового платного model run.
- Три конкурента неизменны: KaroX 5, официальный OpenCode V2, официальный DSH. npm 2026-11-08 показывает `@opencode/cli@2.2.6` latest и `@deepseek-ai/dsh@0.2.2-rc.1` latest; DSH является **prerelease**.

## Полный регрессионный охват — реальные исполнения

Прежний один непрерывный `pytest -q` был прерван лимитом **360 секунд**, а первоначальная группа ядра — 180 секунд. Оба случая — TIMEOUT/НЕЗАВЕРШЕНО, не PASS и не доказательство падения.

Из `audit/REGRESSION_SPLIT_PLAN_20261108.json` взяты попарно не пересекающиеся группы модулей. Все 1 575 собранных тестовых случаев затем выполнены последовательно (одна тяжёлая группа одновременно):

| Группа | Прошло | Пропущено | Доказательство |
|---|---:|---:|---|
| Проверка security/parity, 20 модулей | 425 | 1 | `audit/security_parity_gate_20261108.log` |
| 0, browser/bridge | 237 | 3 | `audit/REGRESSION_GROUP_0_20261108.json` |
| 1, CLI/computer use | 140 | 3 | `audit/REGRESSION_GROUP_1_20261108.json` |
| 2, engine/kernel, расширенный лимит | 233 | 9 | `audit/REGRESSION_GROUP_2_EXTENDED_20261108.json` |
| 3, providers/MCP/cache | 257 | 1 | `audit/REGRESSION_GROUP_3_20261108.json` |
| 4, TUI/sessions/skills | 258 | 1 | `audit/REGRESSION_GROUP_4_20261108.json` |
| 5, unified patch | 7 | 0 | `audit/REGRESSION_GROUP_5_20261108.json` |
| **Всего** | **1 557** | **18** | **1 575 collected; 0 failed** |

Причины пропусков известны и отражены в подробных журналах: Windows/POSIX режимы, OpenSSL CLI, повышенные права на symlink и opt-in live/he​​aded/desktop проверки. Это **полный модульный охват в раздельных процессах**, но **не один успешный непрерывный `pytest`**. Результаты не превращают пропущенные проверки в PASS.

Статика: `ruff check src tests scripts/check_tree.py` PASS, `scripts/check_tree.py` PASS (243 Python-модуля, 90 860 строк, без legacy surface), `compileall` PASS. `mypy` **FAIL: 399 ошибок в 4 TUI-файлах**; пути TUI и `pyproject.toml` совпадают с состоянием до `487bb7b`, поэтому эти ошибки нельзя приписывать checker-origin патчу. Но typing gate не завершён успешно и не должен замалчиваться.

## Исправленный wheel Karo — проверка отдельно от предыдущей арены

- Wheel: `dist-triad-candidate/karox_runtime-5.0.0-py3-none-any.whl`; SHA-256 `485c8ebfbcff994941c09375eda7907c29ad99fd74bedb542b448c2b0affbdc0`.
- Новое venv на D: установило именно эту wheel; импорт показал путь внутри нового venv, CLI --help ответил, checker принял корректный документ и отказал заведомо некорректному.
- На **копии старого** Karo 3D-артефакта независимый grader снова прошёл **4/4**, реальный Chromium+SwiftShader WebGL создал 3 изображения и подтвердил движение/отсутствие pageerror: `audit/CANDIDATE_WHEEL_3D_GATE_20261108.json`.
- Это не свежий платный запуск агента и не доказательство нового clean CLI completion. Зависимости нового venv были переиспользованы с хоста: wheel проверен из новой установки, но dependency closure не герметичен.

## Настоящий Three.js #35003 — новая независимая DEV-задача

Публичное обращение `https://github.com/mrdoob/three.js/issues/35003` от 14 октября 2026 действительно существует. Исходные файлы Three.js r186, лицензия и байтовые SHA зафиксированы в `tasks/real-three-orbitcontrols-35003/task.json`. Это воспроизводимая **reduced upstream** задача, а не full-repo monorepo holdout. У upstream maintainer остаётся спор о том, нужно ли исправлять исходный проект либо браузер/драйвер.

Первый оригинальный grader дал ложный early FAIL: проверял только `getContext('webgl')`, хотя canvas уже использовал WebGL2. Его результат — **GRADER_INVALID**, не RED; сырой файл сохранён (`audit/REAL_THREE_35003_RED_20261108.json`).

Создан **отдельный новый DEV grader v2** без переписывания исходного: `audit/real_three_35003_grader_v2.py`, SHA-256 `9dd7db762590796de132a7f3d719bf2f3032a9c88e9f8f78de501cb6dd3cc445`. Он разрешает существующий WebGL2 **или** WebGL1 и сохраняет жёсткую проверку реальных Chrome CDP событий.

- RED v2: исходный OrbitControls.js действительно бросил `Cannot read properties of undefined (reading 'x')` при pen-down / mouse-up, screenshot реального 3D и event log записаны. `audit/REAL_THREE_35003_RED_V2_20261108.json`.
- GREEN v2: небольшой reference patch **только во временной копии** устраняет TypeError и завершает смешанный жест; независимый браузерный grader PASS, `node --check` exit 0. Изменения и их SHA сохранены в `audit/REAL_THREE_35003_GREEN_V2_20261108.json`; эталон не доступен участникам.
- Дополнительный **неоцениваемый** браузерный smoke `audit/real_three_35003_normal_input_smoke.py`: обычные mouse, pen и touch дали **3/3 PASS на оригинале и 3/3 PASS на reference**. Для каждого есть real 3D screenshot, изменение азимута, ровно одно завершение жеста и ноль pageerror. Доказательства: `.tmp/real-three-35003-normal-original-20261108` и `.tmp/real-three-35003-normal-reference-20261108`.
- Формальное решение об исключении v1 и pin v2: `audit/REAL_THREE_35003_ADJUDICATION_20261108.json`. Ни одного конкурентного arm по этой задаче ещё не запускали; это **DEV_UNPROTECTED, не HOLDOUT**. Тест не измеряет FPS или визуальную эстетику.

## Статус соревнования — без новых победных заявлений

Единственный полноценный matched Linux real SWE triad остаётся Rich #4246: 3/3 clean independent PASS. Baseline Karo 11,2341 с / estimated $0,0098110; OpenCode V2 53,4054 с / $0,0097074; DSH 105,2042 с / $0,0154485. Это **оценка API**, не сумма подтверждённого счёта провайдера. Другие Rich real issues всё ещё ждут независимых paid runs.

Старый Windows 3D DEV run остаётся **diagnostic only**: Karo artifact pass, CLI verification_failed; OpenCode artifact fail; DSH infra setup blocked. Кандидатный wheel пока не участвовал в новой тройке. **TRIAD_COMPARISON_VALID для общих выводов = false. KARO_LEADERSHIP_EVIDENCED = false.**

## Сохранность, ресурсы, следующие действия

- Исходный архив 846 evidence assets `TRIAD_EVIDENCE_20261108.zip`: проверенный SHA-256 `069fafcd6e00163230ce8dbd5323b3f7520827eb173b704dc276cd51071757c6`. НЕ перезаписывать.
- Новый supplemental archive должен содержать только allowlisted новые JSON/log/скрипты, reference patch в JSON, проверяемые PNG, candidate wheel, итоговый checkpoint и манифест SHA. Упаковщик `audit/pack_supplement_20261108.py`. Системные venv, чужие файлы и неизвестные каталоги исключены.
- Замер после тестов: Windows C: **9,04 ГиБ** свободно, D: **60,51 ГиБ**, доступно RAM **7,642 ГиБ**. Обязательный резерв C≥7 ГиБ, D≥15 ГиБ.
- Следующие полезные действия: собрать и проверить supplement; точечно устранить mypy debt без ослабления gate (отдельная product итерация после проверки scope), далее серийные реальные SWE/3D DEV прогоны и свежий blind holdout, но не повторять платные вызовы, которые блокирует среда. Без модельных платных запусков лидерство не доказано.
