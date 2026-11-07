# KaroX 5.0

Локальный кодирующий агент с компактным терминальным клиентом, типизированными
инструментами, устойчивыми сессиями, MCP в обе стороны и bridge для ChatGPT
Web / Claude Web / HyperAgent.

Это стабильная линия **5.0.0**. Она сохраняет проверенное ядро агента,
capability/risk-policy, доказательства выполнения, outbound MCP, inbound
OAuth/MCP bridge, browser/desktop control и skills. Лабораторные
orchestration/autonomy-эксперименты v5 и legacy shell в стабильную поверхность
не входят.

## Установка локального кандидата

Публичного релиза нет. Устанавливайте только проверенный wheel в отдельное
окружение:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .\dist\karox_runtime-5.0.0-py3-none-any.whl
karox --version
```

Существующую установку `karox` не заменяйте во время проверки. Для миграции
сначала сделайте резервную копию `%APPDATA%\KaroX` и
`%LOCALAPPDATA%\KaroX\vnext`; порядок описан в `docs/INSTALL.md` и
`docs/MIGRATION.md`.

## Первый запуск

```text
karox provider presets
karox provider local
karox provider setup <provider> --model <model> --stdin-key
karox doctor
karox
```

Ключи хранятся только как ссылки OS keyring; не передавайте raw credentials в
workspace, логи или benchmark-задачи.

## Команды

```text
karox agent run --repository . --task "..." --verification-command '["python","-m","pytest","-q"]'
karox tui
karox session list --json
karox bridge connect --help
karox mcp --help
```

`karox-vnext` — совместимый alias, ведущий в тот же CLI. Старые
`karox-agent-mcp` / orchestration-команды в этой стабильной линии не
восстанавливаются; это осознанная граница, а не скрытая потеря.

## Безопасность и ограничения

- capability/risk-policy проверяет изменяющие инструменты; это не заменяет
  OS-sandbox.
- Bridge слушает loopback, пока явно не заданы TLS и `--allow-network-bind`;
  Tailscale Funnel остаётся единственным управляемым публичным входом.
- Credentials не пишутся в сессии, benchmark-задачи и отчёты.
- Windows — основная проверенная платформа; Linux-проверки ограничены
  доступным runner; macOS **NOT TESTED**, если отдельно не указано иное.
- Публичный release, push, PyPI/GitHub deployment и замена установленного
  Karo выполняются только с отдельного разрешения владельца.

## Документация

- `docs/ARCHITECTURE.md` — слои и dependency direction.
- `docs/CLI.md` — команды и JSON-контракты.
- `docs/MIGRATION.md` — миграция с v5/6 alpha.
- `docs/INSTALL.md` — установка, backup и rollback.
- `docs/PARITY.md` — паритет возможностей.
- `docs/refactor/` — отчёты goal mode, LOC, dead code, parity/security gates,
  benchmark ledger и release checklist.
