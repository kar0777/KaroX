# HARD20 — проверенные реальные результаты (8 ноября 2026)

**Статус: LIVE DEV EXECUTION, не финальные 20×3 и не blind holdout.** Финансовая политика: `OBSERVE_ONLY_NO_HARD_CAP`. Точка маршрута: `https://api.inference.wandb.ai/v1`, модель `deepseek-ai/DeepSeek-V4.1-Flash`; хеш удалённого checkpoint не доказан. Уровни усилия затребованы `ultra / max / max`, on-wire эквивалентность не доказана.

## Реальная задача

Slot 13, [Three.js issue #35003](https://github.com/mrdoob/three.js/issues/35003), upstream r186 (`9b4a2ac29c63ccb43fd51c5661f2f873ac2c39b8`), MIT. Замороженный тест `test_browser.py`, SHA-256 `9dd7db762590796de132a7f3d719bf2f3032a9c88e9f8f78de501cb6dd3cc445`. Независимые эталонные RED exit 1 и GREEN exit 0 сохранены в `HARD20_FREEZE_SLOT13_20261108.json`.

## Результаты не редактируемых оригинальных запусков и независимой перепроверки

| Участник / label | Live CLI | Patch | Исходный WSL grader | Независимый Windows Chromium | Clean CLI + artifact |
|---|---|---|---|---|---|
| DeepSeek Harness / `hard20-slot13-three35003-max-20261108-a` | exit 0, 465.4308 s | 1589 B, sha256 `f7004d90...` | exit 2: **INFRA** — Playwright отсутствовал | **PASS**, exit 0, без изменений patch | **Да**, по независимому posthoc grade |
| KaroX 5 / `hard20-slot13-three35003-max-20261108-c` | exit 1, 169.8647 s, 17 model requests | 4153 B | exit 2: **INFRA** — Playwright отсутствовал | **PASS**, exit 0, неизменный patch | **Нет**: `unproducible_requirement` / internal verification stopped |
| OpenCode V2 / `hard20-max-35003-20261108-003` | exit -9, 737.421 s; memory guard 1.25 GiB | 1417 B, sha256 `112c286d...` | exit 2: **INFRA** — Playwright отсутствовал | **PASS**, exit 0, неизменный patch | **Нет**: Linux process killed by resource guard |
| OpenCode V2 / `hard20-slot13-three35003-max-20261108-a` | exit 1, 1.9266 s | 0 B | exit 2: INFRA | Не оценивался — модель не маршрутизирована (`provider.no-route`) | Нет, инфраструктура |
| KaroX 5 / `...-a`, `...-b` | exit 2 до модели (недопустимые опции CLI) | 0 B | exit 2: INFRA | Не оценивается как неспособность модели | Не состоялись |

Дополнительно OpenCode V2 `#max` на `hard20-max-35003-20261108-003` реально работал 737.421 секунды, записал 174 события и патч 1417 байт (SHA-256 `112c286d6a7dea04748a61a190238a44b18519b791a91fb7e7ceafd218966169`), но завершён с `exit -9` и `INFRA_MEMORY_BUDGET_EXCEEDED`: дерево процессов превысило защищённый порог **1.25 GiB RSS**. После завершения другого тяжёлого процесса сохранённый патч без изменений прошёл **независимый Windows Chromium grader: PASS, exit 0 (1 passed)**. Доказательство: `hard20-max-runs/independent-regrades/slot13-opencode-paired003-windows-20261108-a/result.json`, screenshot и event_results рядом. **Итого все три агента произвели независимо прошедшие кодовые патчи** на #35003, но clean CLI+artifact зафиксирован только у DeepSeek Harness. OpenCode остановлен защитой ресурсов, KaroX — собственным требованием верификации. Запуски относятся к публичному DEV, в эксперименте отмечены пересечения процессов: это **проверенные индивидуальные результаты, но не валидный строго последовательный matched leaderboard**.

### Конкретные ошибки KaroX, обнаруженные вживую

У KaroX сохранён проверенный сторонним grader патч: проблема **не в качестве написанного исправления**. Публичный pytest и `node --check OrbitControls.js` у агента прошли, но его обязательная внутренняя верификация остановилась из-за недоступности допустимого способа добыть независимое браузерное доказательство. Вследствие exit 1 KaroX **не получает clean success**. Пограничные CLI аргументы `--max-steps` допускают максимум 10000, `--max-seconds` максимум 3600 — это технические пределы установленного CLI, не прежние долларовые ограничения. Исправления запуска фиксируются отдельными новыми labels, старые ошибки остаются в append-only evidence.

### Телеметрия и стоимость

Для DSH сохранены 344 JSON-события, 37 успешных shell вызовов, 55 model steps; reported input 198671, cache-read 2167040, output 46779 токенов. Примерная тарифная **оценка** $0.13515175, **не W&B invoice**. KaroX slot13-c: reported prompt 396160, cache-read 297856, completion 15172, оценка $0.03845828 по тем же ставкам — **не invoice**. Доступные текстовые события, патчи, stderr, grader, screenshot hashes и SHA-манифесты находятся в `hard20-max-runs/`; счёт провайдера не подтверждён.

Локальный viewer: `hard20-max-runs/HARD20_TRACE_VIEWER.html`. Независимая перепроверка DSH: `hard20-max-runs/independent-regrades/slot13-dsh-windows-20261108-a/result.json`. Общий журнал Windows-перепроверок: `hard20-max-runs/HARD20_HOST_REGRADE_LEDGER.jsonl`.

**Не утверждается:** 20 завершённых задач, 60 запусков, победа KaroX, сравнимый cost-per-solved, доказанный on-wire MAX для всех клиентов, точный W&B invoice, blind holdout. Старый Phase0 в `HARD20_FINAL.md` является историческим срезом до новых платных прогонов, не актуальной финансовой блокировкой.
