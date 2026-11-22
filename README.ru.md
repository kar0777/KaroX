# KaroX 6

Небольшой быстрый CLI-агент для программирования: одно ядро агента для headless-режима
(поток JSON), лёгкого TUI и MCP-сервера; провайдеры API — OpenAI-совместимые, Anthropic и
Gemini. Работает на Windows, Linux и macOS. [English](README.md)

Статус: в разработке — см. [PROGRESS.md](PROGRESS.md) и [DECISIONS.md](DECISIONS.md).

## Использование

```bash
karox chat --model МОДЕЛЬ           # интерактивный чат (или просто `karox` в терминале)
karox run --model МОДЕЛЬ "задача"   # одна задача без интерфейса; --json — поток событий JSON
karox mcp --model МОДЕЛЬ            # KaroX как MCP-сервер по stdio (--http ПОРТ — по HTTP)
karox update                        # поставить свежую сборку karox6/main (в чате: /update)
```

KaroX ещё не установлен или не запускается: `scripts/install_latest.ps1` (Windows) или
`scripts/install_latest.sh` создаёт venv и ставит в него свежую сборку.

Провайдер: `--provider openai-chat|openai-responses|anthropic|gemini`, `--base-url`,
`--api-key-env` (имя переменной окружения с ключом; сам ключ в аргументы не передаётся).
Внешние MCP-серверы становятся инструментами агента, если перечислены в
`~/.karox6/mcp.json` (или `--mcp-config`) в разделе `mcpServers` (`command`/`args` или `url`).
`pip install karox6[tui]` добавляет в чат редактирование строки и историю.

В чате: Ctrl+C останавливает текущий ход, `/diff` показывает изменения, `/resume`
продолжает сохранённую сессию, `/help` — все команды. Опасные команды (удаление файлов,
force push и т. п.) выполняются только после подтверждения.

Умолчания для флагов хранятся в `~/.karox6/config.toml` (или `$KAROX_HOME/config.toml`);
флаги и переменные `KAROX_MODEL` / `KAROX_PROVIDER` / `KAROX_BASE_URL` важнее файла,
`--profile ИМЯ` накладывает таблицу `[profiles.ИМЯ]`:

```toml
model = "deepseek-chat"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"   # имя переменной, а не сам ключ

[profiles.local]
model = "qwen3-coder"
base_url = "http://127.0.0.1:11434/v1"
```

Полное руководство (чат, запуск без интерфейса и коды выхода, лимиты, безопасность, MCP в обе
стороны): [docs/guide.ru.md](docs/guide.ru.md). KaroX из ChatGPT, Claude и других веб-клиентов (туннель Tailscale встроен): [docs/chatgpt.ru.md](docs/chatgpt.ru.md).

## Разработка

```bash
uv sync
uv run pytest
uv run karox --version
```

Лицензия: MIT. Сторонние материалы: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
