# Checkpoint — KaroX 5, Three.js #35003, 8 ноября 2026

## Контрольная точка

Начальная ветка goal/gpt6-sol-triad-20261108, исходный HEAD a4fe74c.
На этой же ветке обнаружен новый проверенный коммит d950e59, сохранивший
предыдущие результаты. Чужие локальные изменения, в том числе новый
security-subprocess тест, не сброшены и не перезаписаны.

Полный модульный regression gate: 1575 collected; 1557 passed, 18 skipped,
0 failed, семь последовательных завершённых групп. Один монолитный pytest
был TIMEOUT и не считается PASS. Плюс отдельный security e2e тест другого
локального изменения за пределами этого фиксированного покрытия: не
смешивать в исходную сводку до проверки его отдельного независимого лога.

Ранее восстановленный Karo wheel SHA256:
485c8ebfbcff994941c09375eda7907c29ad99fd74bedb542b448c2b0affbdc0.
В WSL он офлайн распакован и проверен по wheel RECORD в отдельную лабораторную
папку karox-candidate-wheel-20261108; настоящий штатный CLI с этим
PYTHONPATH загрузил версию 5.0.0 и корректный модуль web_document.
Базовый WSL venv оставлен нетронутым. Это не герметичная dependency install.
Подготовлен адаптер audit/triad_linux_candidate.py, ещё не запускавший
платную модель.

## Реальная 3D-задача

Three.js OrbitControls issue #35003, revision r186. Исходный
grader v1 неверно спрашивал только WebGL1 при созданном WebGL2 и признан
INVALID; raw лог сохранён. Независимый DEV grader v2
SHA256 9dd7db762590796de132a7f3d719bf2f3032a9c88e9f8f78de501cb6dd3cc445
подтвердил настоящее RED на исходнике (TypeError reading x после
pen-down/mouse-up), а отдельный минимальный reference patch дал GREEN.
Обычные mouse, pen, touch: 3/3 PASS на обоих вариантах.

Новый изолированный task task_id real-three-orbitcontrols-35003-v2
использует идентичный оригиналу fixture (9 файлов) и только frozen
grader v2. SHA задания:
98f2f823a681050adf4a82a8df16af281ba2f477b335ff5a39d1e754b210f312.
Это DEV_UNPROTECTED, не секретный HOLDOUT.

В WSL eval-venv отсутствуют Playwright и Chromium. Поэтому внешнее
Windows Chromium оценивание объявлено *до* будущих trial:
audit/THREE_35003_EPOCH_PROTOCOL_20261108.json и
audit/windows_regrade_three_35003.py. Контрольный evaluator
запущен на исходнике (FAIL) и reference (PASS), без изменения самих
workspace; подлинные WebGL PNG и JSON событий сохранены.
Версии: Windows Chromium 134.0.6998.35, Playwright 1.51.0;
SHA браузера/Three.js/grader в audit/THREE_35003_ENVIRONMENT_20261108.json.

Все версии, task hash, одинаковая модель W&B DeepSeek-V4.1-Flash и
три участника заморожены в audit/THREE_35003_EPOCH_PROTOCOL_20261108.json:
KaroX 5, официальный OpenCode V2 2.2.6 и официальный
DeepSeek Harness 0.2.2-rc.1 (prerelease). Никаких новых платных прогонов
по этой задаче не было; 0/3 scored, лидерство не доказано.

## Сохранность и ограничения

Архив TRIAD_EVIDENCE_35003_READY_20261108.zip содержит 52 whitelisted
файла нового DEV-этапа, включая оба исходных дерева, браузерные изображения,
аудиторские скрипты и grader. SHA256:
6457190bdafd38cb4cdbec4208df64a60b0799e10036de90d53a91a194d49e4b.
Внутренний манифест SHA всех файлов проверен. Он дополняет, но не
заменяет архивы исходного triad evidence (846 assets) и supplemental
(48 assets). ZIP хранится на D: и не добавляется в Git.

Статика новых аудиторских скриптов ruff и compileall — PASS.
Точечный mypy для web_document.py — PASS, но глобальный mypy
с 399 ранее существовавшими TUI ошибками — FAIL.

Ни user install, ни official OpenCode/DSH, ни grader v1, ни старые
raw runs, ни личные файлы не изменены. Нет push или публикации.
C: резерв не менее 7 GiB, D: не менее 15 GiB, тяжёлые процессы
всегда последовательны.

Дальнейший платный DEV triad запуск возможен только если среда
разрешает и проходит актуальная transport readiness; прежнюю
OpenAI-tool safety блокировку нельзя обходить. Иначе доступная
работа — независимые security/type/parity fixes и новые реальные задачи.
