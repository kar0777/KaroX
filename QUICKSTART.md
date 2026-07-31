# KaroX 5 Preview — Quick start / Быстрый старт

This guide covers the preview runtime `5.0.0.dev0`. The public bootstrap scripts
on `main` install the latest stable 4.x release. To test KaroX 5, use a checkout
of the preview branch and its local installer.

Эта инструкция относится к preview runtime `5.0.0.dev0`. Публичные bootstrap-
скрипты ветки `main` устанавливают последний стабильный релиз 4.x. Для проверки
KaroX 5 используй checkout preview-ветки и локальный установщик из неё.

## 1. Install / Установка

Windows:

```powershell
.\install.karox.ps1
```

macOS or Linux / macOS или Linux:

```bash
./install.karox.sh
```

Open a new terminal inside a disposable Git repository for the first test.

Открой новый терминал внутри тестового Git-репозитория. Первый запуск лучше не
делать на единственной копии важного проекта.

## 2. Start / Запуск

```bash
karox
```

Choose English or Русский. Normal text in the full-screen client is an agent
task. Use `/connect` to configure a model or hosted client.

Выбери язык. Обычный текст в полноэкранном клиенте считается задачей для агента.
Команда `/connect` открывает настройку модели или hosted client.

## 3. Choose permissions / Выбор прав

The UI uses friendly names; the durable/CLI identifiers are shown in code.

- **Observe** (`read_only`) — read repository files and Git state without
  mutation / чтение файлов и Git state без изменений.
- **Build** (`workspace_write`) — edit files, run explicitly approved checks,
  collect Git status/diff evidence, and call selected MCP tools. Build does not
  grant `git.commit` / изменение файлов, явно разрешённые проверки, Git
  status/diff evidence и выбранные MCP tools. Build не выдаёт `git.commit`.
- **Advanced** (`elevated`) — explicitly adds guarded local commit plus the
  browser, desktop-input, and network capabilities allowed by policy / явно
  добавляет защищённый локальный commit и разрешённые policy browser,
  desktop-input и network capabilities.

No stable profile grants Git push, package publishing, or authentication
commands. Ни один стабильный профиль не выдаёт Git push, package publishing или
authentication commands.

Start with Observe. Use Build only in a repository where the intended change is
understood. KaroX is not an operating-system sandbox; an approved process runs
with the current user's OS rights.

Начинай с Observe. Используй Build только там, где понимаешь ожидаемое изменение.
KaroX не является полной системной песочницей: разрешённый процесс выполняется с
правами текущего пользователя.

## 4A. Connect ChatGPT Web / Подключение ChatGPT Web

```bash
karox bridge connect chatgpt-web --repository . --write
```

Omit `--write` for read-only access. KaroX prints an MCP URL and an OAuth
approval password. Follow the displayed ChatGPT instructions, enter the password
only on the KaroX approval page, and keep the KaroX process open.

Без `--write` используется read-only доступ. KaroX выведет MCP URL и пароль OAuth
approval. Следуй инструкциям для ChatGPT, вводи пароль только на странице KaroX
и не закрывай процесс KaroX во время работы connector.

A Quick Tunnel URL changes after restart. Update the saved connector URL or use
a separately provisioned stable HTTPS origin.

URL Quick Tunnel меняется после перезапуска. Обнови URL сохранённого connector
или используй отдельно настроенный стабильный HTTPS origin.

## 4B. Connect Claude Web / Подключение Claude Web

```bash
karox bridge connect claude-web --repository . --write
```

Add the printed MCP URL as a custom Claude connector, complete KaroX OAuth
approval, and keep the bridge process open. Use read-only mode first when testing
a new account or repository.

Добавь выведенный MCP URL как custom connector Claude, заверши OAuth approval в
KaroX и оставь bridge запущенным. Для первого теста аккаунта или репозитория
используй read-only режим.

## 4C. Connect an API model / Подключение API-модели

Use `/connect` and choose OpenAI Responses, Anthropic Messages, Gemini, or a
generic OpenAI-compatible endpoint. Provider secrets are stored in the OS
keyring. `F10` performs a minimal request before the route is activated.

Открой `/connect` и выбери OpenAI Responses, Anthropic Messages, Gemini или
совместимый OpenAI endpoint. Секрет провайдера сохраняется в системном keyring.
`F10` выполняет минимальный запрос до активации route.

Do not paste API keys into chat, project files, issue reports, or conformance
records.

Не отправляй API keys в чат, файлы проекта, issue или conformance records.

## 5. Run the release scenario / Основной сценарий

Give the agent one bounded task, for example changing a small test fixture or a
document in a disposable repository. The successful path must include:

1. one real file change;
2. one explicitly approved verification command;
3. successful check evidence;
4. Git status;
5. Git diff;
6. a final evidence-backed report.

Дай агенту одну ограниченную задачу, например изменить небольшой fixture или
документ в тестовом репозитории. Успешный путь должен содержать реальное
изменение файла, разрешённую проверку, evidence успешного check, Git status, Git
diff и финальный отчёт с доказательствами.

A model saying “done” is not verification. KaroX must report the durable evidence
chain.

Текст модели «готово» не является проверкой. KaroX должен показать durable
evidence chain.

## 6. Restart and resume / Перезапуск и продолжение

Stop KaroX, start it again in the same repository, and resume the saved session.
Confirm that the completed mutation is not applied a second time and that Git
state and evidence remain available.

Останови KaroX, снова запусти его в том же репозитории и продолжи сохранённую
сессию. Убедись, что выполненная мутация не применяется повторно, а Git state и
evidence сохранились.

## 7. Diagnose / Диагностика

```bash
karox doctor
python scripts/check_v5_release.py --json
```

The release-contract command may report pending live conformance and external
beta records on a development build. That is expected. `--strict` is for
release-candidate rehearsal and fails until all required evidence is passed.

На development build release-contract может честно показать незавершённые live
conformance и external beta records. Это ожидаемо. Режим `--strict`
предназначен для release candidate и падает, пока все обязательные доказательства
не имеют статус `passed`.

## More / Подробнее

- [KaroX 5 release scope](docs/V5_RELEASE_SCOPE.md)
- [Current implementation status](docs/IMPLEMENTATION_STATUS.md)
- [Live test runbook](docs/LIVE_TEST_RUNBOOK.md)
- [Migration from 4.x](docs/MIGRATION_V4_TO_V5.md)
- [Live conformance records](docs/conformance/README.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Security](SECURITY.md)
