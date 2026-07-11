# KaroX — Quick start / Быстрый старт

## 1. Install / Установите

The installer uses the latest stable release. Run the same command again to update.

Установщик использует последний стабильный релиз. Для обновления запустите ту же команду повторно.

**Windows**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1 | iex"
```

**macOS / Linux**

```bash
curl -fsSL https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.sh | bash
```

## 2. Launch / Запустите

Normal provider selection / обычный выбор клиента:

```bash
karox
```

Launch directly for Notion / сразу запустить для Notion:

```bash
karox notion
```

On first launch choose **English** or **Русский**. KaroX saves the choice. Change it later with `G → L`.

При первом запуске выберите **Русский** или **English**. KaroX сохранит выбор. Сменить язык можно через `G → L`.

Automation / автоматизация: `KAROX_LANGUAGE=en` or / или `KAROX_LANGUAGE=ru`.

## 3. Create a session / Создайте сессию

1. Press `N` / Нажмите `N`.
2. Select a repository / Выберите репозиторий.
3. Select Observe, Build, Resume, or Advanced / Выберите профиль доступа.
4. Wait for `● LIVE` / Дождитесь `● LIVE`.

## 4A. Connect Notion / Подключите Notion

A Notion workspace with Custom Agents and custom Streamable HTTP MCP support is required.

Нужен Notion workspace с Custom Agents и поддержкой собственного Streamable HTTP MCP.

1. Press `C` and paste the generated connection prompt into the Notion Custom Agent.
2. Press `K` and place the key only in Notion's protected Bearer-token field.
3. Let the agent call `karox_preflight`.
4. Send the real task as a separate message.

1. Нажмите `C` и вставьте готовый connection prompt в Notion Custom Agent.
2. Нажмите `K` и вставьте ключ только в защищённое поле Bearer token.
3. Дождитесь вызова `karox_preflight`.
4. Отправьте реальное ТЗ отдельным сообщением.

Full guide / полная инструкция: [NOTION.md](NOTION.md).

## 4B. Connect another AI client / Подключите другой AI-клиент

In the session card / В карточке сессии:

- `V` — local AI-readiness check / локальная проверка готовности;
- `M` — live Mission Control context / актуальный контекст миссии;
- `C` — localized connection prompt / prompt подключения;
- `K` — secret session key / секретный ключ сессии;
- `T` — real-task template / шаблон реального ТЗ;
- `A` — complete handoff / полный handoff.

Paste `C` into the AI client. Enter `K` only in the protected credential card. Never put the key in chat.

Вставьте `C` в AI-клиент. Значение `K` вводите только в защищённую карточку credentials. Не отправляйте ключ в чат.

## 5. Verify and send the task / Проверьте и отправьте ТЗ

`V` checks `/session`, `/health`, `/git/status`, `/context/brief`, `repoRoot`, and branch. After preflight succeeds, send a separate real task. The session label is only history metadata.

`V` проверяет `/session`, `/health`, `/git/status`, `/context/brief`, `repoRoot` и ветку. После успешного preflight отправьте отдельное реальное ТЗ. Название сессии — только метка истории.

## Notion diagnostics / Диагностика Notion

```bash
karox notion doctor
```

## Safety / Безопасность

- large output / большой вывод: `capture=file`;
- cleanup before commit / очистка перед commit: `/git/cleanup-generated`;
- commit only through KaroX / commit только через KaroX;
- never push / никогда не выполнять push;
- never paste the session key into chat / никогда не отправлять ключ сессии в чат.
