# KaroX — Quick start / Быстрый старт

## 1. Install / Установите

**Windows**
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1 | iex"
```

**macOS / Linux**
```bash
curl -fsSL https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.sh | bash
```

## 2. Launch / Запустите

```bash
karox
```

On first launch choose **English** or **Русский**. KaroX saves the choice. Change it later with `G → L`.

При первом запуске выберите **Русский** или **English**. KaroX сохранит выбор. Сменить язык можно через `G → L`.

Automation / автоматизация: `KAROX_LANGUAGE=en` or / или `KAROX_LANGUAGE=ru`.

The adaptive Flight Deck groups each workspace into a card and keeps all important actions in one command bar. Color improves scanning but is never required to understand status or controls.

Адаптивный Flight Deck объединяет данные каждой сессии в карточку, а основные действия — в единую command bar. Цвет ускоряет чтение, но статус и управление понятны и без него.

## 3. Create a session / Создайте сессию

1. Press `N` / Нажмите `N`.
2. Select a repository / Выберите репозиторий.
3. Select Observe, Build, Resume, or Advanced / Выберите профиль доступа.
4. Wait for `● LIVE` / Дождитесь `● LIVE`.

## 4. Verify and connect / Проверьте и подключите

In the session card / В карточке сессии:

- `V` — local AI-readiness check / локальная проверка готовности;
- `M` — live Mission Control context / актуальный контекст миссии;
- `C` — localized connection prompt / prompt подключения;
- `K` — secret `X-API-Key` / секретный ключ;
- `T` — real-task template / шаблон реального ТЗ;
- `A` — complete handoff / полный handoff.

`V` checks `/session`, `/health`, `/git/status`, `/context/brief`, `repoRoot`, and branch before handoff. `M` shows the same live brief, warnings, and recommended next action without exposing the key.

`V` проверяет `/session`, `/health`, `/git/status`, `/context/brief`, `repoRoot` и ветку до передачи AI. `M` показывает актуальный бриф, предупреждения и следующий шаг без раскрытия ключа.

Paste `C` into the AI client. Enter `K` only in the protected credential card. Never put the key in chat.

Вставьте `C` в AI-клиент. Значение `K` вводите только в защищённую карточку credentials. Не отправляйте ключ в чат.

## 5. Send the real task / Отправьте реальное ТЗ

Wait for the AI preflight to succeed, then send a separate task message. The session label is only history metadata.

Дождитесь успешного preflight AI, затем пришлите задачу отдельным сообщением. Название сессии — только метка истории.

## Safety / Безопасность

- large output / большой вывод: `capture=file`;
- cleanup before commit / очистка перед commit: `/git/cleanup-generated`;
- commit only / commit только: `/git/commit`;
- never push / никогда не выполнять push.
