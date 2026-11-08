# HARD20 DEV: OpenCode V2 восстановлен до clean PASS

Дата: 2026-11-08. **Это повторение публичного DEV slot 13, а не новая задача или blind holdout.**
Не включать этот запуск одновременно со старым в matched leaderboard.

## Фактическая последовательность

- Предыдущий OpenCode run `hard20-max-35003-20261108-003`: patch 1417 B, независимая Windows Chromium-перепроверка PASS, но CLI exit -9 после ограничения процессного дерева 1.25 GiB RSS. Не считать quality FAIL или clean PASS.
- Отдельный контрольный запуск `hard20-slot13-opencode-ram2g-20261108-004`: официальный OpenCode V2, W&B `deepseek-ai/DeepSeek-V4.1-Flash` и замороженная задача Three.js issue #35003, patch 1728 B. Запуск через изолированную WSL-конфигурацию с процессным RSS guard 2 GiB при соблюдении Windows RAM и порогов дисков C >= 7 GiB / D >= 15 GiB. Все API-вызовы обычные платные; бюджетный режим `OBSERVE_ONLY_NO_HARD_CAP`.
- CLI завершился **exit 0**, `agent_timed_out=false`, `terminal_seen=true`, `error_events=0`. Agent wall **699.1311 s**; 348 terminal events.
- Исходный WSL grader exit 2 из-за отсутствующего Playwright — **INFRA**, не quality FAIL. Независимый неизменённый Windows Chromium grader затем завершился **exit 0 / PASS**, с настоящим screenshot и `clean_user_success=true`. Проверка не переписывала patch.
- Оригинальный и новый артефакты остаются разными immutable labels; критерии не изменены.

## Воспроизводимые идентификаторы

- Task: `real-three-orbitcontrols-35003-v2`, публичная upstream [Three.js #35003](https://github.com/mrdoob/three.js/issues/35003), лицензия MIT, DEV_PUBLIC_NOT_BLIND.
- Исходный task SHA-256 `98f2f823a681050adf4a82a8df16af281ba2f477b335ff5a39d1e754b210f312`.
- Замороженный внешний browser grader SHA-256 `9dd7db762590796de132a7f3d719bf2f3032a9c88e9f8f78de501cb6dd3cc445`.
- CLI `result.json` SHA-256 `718cb8e53b0a951c5ab0d03d4742747f826c33dc1f183c9df26990bd257cbeab`.
- Новый patch `changes.patch` SHA-256 `bfce85ab0f4b55c5b81eb6444fc87c3426350afcd9cbb7a533a995588fd9b9f3`.
- Independent `host_chromium_regrade.json` SHA-256 `d2222065d740c59235f45767a1849b446d32c4dd4c18eed67d9c426e30dafb73`.
- Lossless ZIP первоначальных данных `opencode_v2.HARD20_EVIDENCE.zip` SHA-256 `9477ab5ee737bcf44dbd502a4030f049fef9da82e7bce352feab2288516839be`.
- ZIP независимой проверки `real-three-orbitcontrols-35003-v2__opencode_v2__host_regrade.zip` SHA-256 `560971be113c90da0621db7aae01a472a1d8698e21ccb766bd354a968248da4c`.

Архивы находятся в `paired-linux/hard20-slot13-opencode-ram2g-20261108-004/real-three-orbitcontrols-35003-v2/`.

## Телеметрия и ориентир расходов

Из оригинального потока OpenCode: input **300295**, cache-read **6057472**, output **22547**, reasoning **65080** токенов; заявлен `#max`. Это не доказывает точный серверный reasoning-effort по wire или криптографический checkpoint W&B. При тарифах из актуального HARD20 документа оценка модели составляет **$0.25643871** без отдельного прибавления reasoning или **$0.29874071** при дополнительной тарификации 65080 reasoning как output. Это *сценарии расчёта*, не подтверждённый счёт W&B; избегать двойного счёта.

## Безопасность, прозрачность и очистка

Оба зарегистрированных disposable-workspace `opencode_v2` labels 003 и 004 сохранены в отдельных lossless ZIP с SHA-манифестами и независимо проверенным browser proof. Удалялись **только workspace** после успешной проверки архивов: label 003 освободил 2711552 байта D:, label 004 — 2830336 байт D:, всего **5541888 байт**. Старые `result.json`, `changes.patch`, ZIP, host-grade и отдельные журналы сохранены.

Первая попытка удаления 003 наткнулась на read-only Windows ACL/флаги `.git/objects`. Очистка была дополнена защищённым `--resume-interrupted`, проверяющим каждый сохранившийся файл против исходного архива. Повтор прошёл, чужих путей не затронуто, потери доказательств нет.

Репорт `HARD20_RECONCILED_LIVE_20261108.json` выводит отдельно `PATCH_PASS_BUT_CLI_INCOMPLETE` и `CLEAN_PASS`, с SHA-привязкой внешней оценки; 4/4 теста `audit/test_hard20_reconcile_live.py` PASS. По всем сохранённым DEV attempts: **14 attempted arms**, из них в результате posthoc regrade `karox5` — 1 external patch PASS / 0 clean; `opencode_v2` — 2 external patch PASS / 1 clean; `deepseek_harness` — 2 external patch PASS / 2 clean. Большинство остальных попыток unjudged / infra, а не quality fail. Это не сравнимые запуски одной независимой серии.

**Следующая задача:** отдельный публичный сложный upstream issue с новым независимым frozen RED/GREEN, сохранив исходные правила, ротацию CLI и изоляцию. Не переводить DEV recovery в победу какого-либо агента.
