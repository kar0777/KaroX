<div align="center">

# ★ Star For KaroX

### Ваш локальный код. Ваши правила. AI с правильным контекстом.

**KaroX превращает локальный Git-репозиторий в безопасное рабочее пространство для PromptQL, Notion Custom Agents и других AI-клиентов.**

[![Release](https://img.shields.io/badge/release-v3.11.0-7C3AED)](https://github.com/kar0777/KaroX/releases/latest)
[![Notion](https://img.shields.io/badge/Notion-Custom%20Agent-000000?logo=notion)](NOTION.md)
[![Windows](https://img.shields.io/badge/Windows-PowerShell-0078D4?logo=windows)](#установка-одной-командой)
[![macOS](https://img.shields.io/badge/macOS-Bash-000000?logo=apple)](#установка-одной-командой)
[![Linux](https://img.shields.io/badge/Linux-Bash-FCC624?logo=linux&logoColor=black)](#установка-одной-командой)
[![CI](https://github.com/kar0777/KaroX/actions/workflows/notion-provider.yml/badge.svg)](https://github.com/kar0777/KaroX/actions/workflows/notion-provider.yml)

[Быстрый старт](QUICKSTART.md) · [Подключение Notion](NOTION.md) · [English](README.md) · [Безопасность](SECURITY.md) · [Диагностика](TROUBLESHOOTING.md)

</div>

## Установка одной командой

Команда устанавливает последний стабильный релиз KaroX, все Python-зависимости, команду `karox` и системный launcher. Для обновления запустите ту же команду повторно.

### Windows

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1 | iex"
```

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.sh | bash
```

Обычный запуск:

```bash
karox
```

Запуск сразу с выбранным провайдером Notion:

```bash
karox notion
```

## Notion Custom Agent за три шага

> Нужен Notion workspace, в котором Custom Agents умеют подключать собственный Streamable HTTP MCP-сервер и хранить защищённый Bearer-токен.

1. Запустите `karox notion`, выберите репозиторий и профиль доступа, затем дождитесь `● LIVE`.
2. Нажмите `C` и вставьте готовый connection prompt в Notion Custom Agent. Нажмите `K` и вставьте ключ только в защищённое поле Bearer token.
3. Дождитесь вызова `karox_preflight`, после чего отправьте реальное ТЗ отдельным сообщением.

Notion-агент получает специализированные инструменты для чтения проекта, изменения файлов, запуска сборки и тестов, проверки Git diff, безопасного commit и финального отчёта. Ограничения KaroX при этом не обходятся. Подробности — в [полной инструкции Notion](NOTION.md).

## Почему KaroX

- **Local-first:** исходный код остаётся на вашем компьютере, сервер ограничен выбранным репозиторием.
- **Явные права:** Observe, Build, Resume и Advanced показывают реальный уровень доступа.
- **Сначала контекст:** Mission Control передаёт агенту свежий бриф без секретов.
- **Безопасный Git workflow:** проверка ветки, очистка generated-файлов, контролируемый commit и жёсткий запрет push.
- **Несколько провайдеров:** нативные handoff для PromptQL и Notion, универсальный OpenAPI и совместимость с letaido.
- **Для ежедневной работы:** двуязычный адаптивный Flight Deck, история сессий, диагностика и обновление одной командой.

## Поддерживаемые AI-клиенты

| Клиент | Подключение | Лучше всего подходит для |
|---|---|---|
| **PromptQL** | Custom OpenAPI integration | Совместная работа с кодом |
| **Notion Custom Agent** | Защищённый Streamable HTTP MCP | Кодинг и проекты прямо из Notion |
| **Другой клиент** | Универсальный OpenAPI + `X-API-Key` | Любой совместимый AI-инструмент |
| **letaido.com** | Режим совместимости с защищённым header | Существующие letaido-сценарии |

Клиент выбирается при первом запуске и меняется через `G → A`. Команда `karox notion` временно выбирает Notion только для текущего запуска.

## Создание рабочего пространства

1. Нажмите `N`.
2. Выберите локальный Git-репозиторий или вставьте полный путь.
3. Выберите профиль:
   - **Observe** — анализ только для чтения;
   - **Build** — отдельная ветка `promptql/*`, изменения, проверки и безопасный commit;
   - **Resume** — продолжение текущей рабочей ветки;
   - **Advanced** — расширенные команды внутри выбранного репозитория.
4. Укажите метку сессии для истории. Она никогда не считается задачей для AI.
5. Дождитесь `● LIVE`.

KaroX запускает ограниченный репозиторием API и открывает его через Cloudflare Tunnel или Tailscale Funnel. Сгенерированный handoff уже содержит URL, ветку, режим, preflight и правила безопасности.

## Управление сессией

| Клавиша | Действие |
|---|---|
| `V` | Локально проверить готовность AI |
| `M` | Открыть актуальный Mission Control |
| `C` | Скопировать connection prompt |
| `T` | Скопировать шаблон реального ТЗ |
| `K` | Отдельно скопировать ключ сессии |
| `P` | Скопировать provider ID |
| `A` | Скопировать полный handoff |
| `L` | Посмотреть логи |
| `S` | Остановить сессию |

Перед передачей AI нажмите `V`. KaroX проверит `/session`, `/health`, `/git/status`, `/context/brief`, а также точный репозиторий и ветку.

## Диагностика Notion

```bash
karox notion doctor
```

Другие команды провайдера:

```bash
karox notion install
karox notion status
karox notion docs
```

## Контракт безопасности

- каждый endpoint требует уникальный ключ текущей сессии;
- доступ не может выйти за выбранный `repoRoot`;
- секретные пути и path traversal блокируются;
- Observe всегда остаётся read-only;
- опасные команды и публикация пакетов блокируются;
- большой вывод можно сохранить в generated-файл;
- commit выполняется только через защищённый endpoint KaroX;
- KaroX никогда не выполняет `git push`.

HTTP endpoints остаются обратно совместимыми. Команда `repopilot` сохранена как compatibility alias.

## Документация

- [Быстрый старт / Quick start](QUICKSTART.md)
- [Провайдер Notion Custom Agent](NOTION.md)
- [Подключение PromptQL](examples/promptql-connect.md)
- [Решение проблем](TROUBLESHOOTING.md)
- [Безопасность](SECURITY.md)
- [История изменений](CHANGELOG.md)
