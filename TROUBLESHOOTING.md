# Диагностика

## HTTP 401

Неверный `X-API-Key`.

Скопируйте ключ из лаунчера заново и переподключите интеграцию. Не отправляйте ключ обычным сообщением в чат.

Если PromptQL пишет `Failed to resolve integration credentials` ещё до HTTP-запроса, значит ключ не дошёл до KaroX: в защищённой карточке PromptQL не сохранился `X-API-Key`. Откройте подключение этого же provider id заново и вставьте значение из `K = X-API-Key`, а не provider id/name/base_url. Начиная с `3.8.12`, OpenAPI KaroX также явно описывает стандартную auth-схему `KaroXApiKey` для заголовка `X-API-Key`.

Для Tailscale Funnel host `*.ts.net` обычно остаётся тем же между запусками. Начиная с `3.8.13`, KaroX добавляет session suffix в provider id. Если PromptQL пытается использовать старый provider id без session suffix, создайте интеграцию именно с новым provider id из свежей карточки KaroX.

## HTTP 403

Действие заблокировано политикой KaroX.

Частые причины:

- выбран режим только для чтения;
- попытка прочитать `.env` или другой чувствительный файл;
- путь выходит за пределы выбранного репозитория;
- агент пытается выполнить прямой `git commit` вместо `/git/commit`;
- агент пытается выполнить `git push`;
- команда не разрешена в режиме `Автопилот`.

## HTTP 530 / Cloudflare 1033

URL туннеля устарел или локальный сервер/туннель не запущен.

Перезапустите Star For KaroX и обновите OpenAPI-интеграцию новым tunnel URL.

## AI-инструмент спрашивает подтверждение на каждый HTTP-вызов

Для интеграции `repo-tools` выберите постоянное разрешение в текущем чате/проекте, если ваш AI-инструмент это поддерживает.

## URL туннеля постоянно меняется

Быстрые Cloudflare tunnels временные. Это нормально. Каждый новый запуск может выдавать новый URL, его нужно обновить в интеграции.

## В списке много серых stopped-сессий

Это сохранённые истории старых сессий. В главном меню нажмите `U`, чтобы удалить все остановленные истории. Чтобы удалить одну историю, откройте её номером и нажмите `D`; команда доступна только для stopped-сессий.

## Tailscale Funnel не запускается

В настройках KaroX (Windows) выберите `I = установить Tailscale через winget`, если Tailscale ещё не установлен. Затем нажмите `L = войти / запустить Tailscale CLI`, чтобы KaroX выполнил `tailscale up` и открыл логин. После входа нажмите `R`, чтобы проверить статус.

На macOS / Linux установите Tailscale вручную:

```bash
brew install --cask tailscale   # macOS
# или sudo apt install tailscale (Linux)
```

Затем в настройках KaroX нажмите `L`, чтобы выполнить `tailscale up`. CLI Tailscale на macOS находится внутри `.app`: `/Applications/Tailscale.app/Contents/MacOS/Tailscale`.

Для внешних AI-инструментов KaroX использует Tailscale Funnel, а не приватный Tailscale Serve. Funnel публикует публичный HTTPS URL `*.ts.net`, поэтому в tailnet должны быть включены MagicDNS, HTTPS certificates и разрешение Funnel в policy.

Если Tailscale просит включить Funnel при первом запуске, KaroX покажет ссылку `login.tailscale.com/f/funnel`, скопирует её в буфер обмена и попробует открыть браузер. Подтвердите Funnel в Tailscale и запустите сессию KaroX ещё раз.

## Сервер запустился, но AI-инструмент не подключается

Проверьте, что открыты оба окна:

- `KaroX: сервер`;
- `KaroX: туннель`.

Также проверьте, что в интеграции указан:

- `base_url`: tunnel URL без `/openapi.json`;
- `api_docs_url`: tunnel URL + `/openapi.json`;
- header auth: `X-API-Key`.

## Команда `karox` не найдена

### Windows

После установки откройте новое окно PowerShell. Если не помогло, запустите:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\RepoPilotBridge\bin\karox.ps1"
```

### macOS / Linux

KaroX ставит основную команду в `~/.local/bin/karox`; `repopilot` остаётся compatibility alias. Если терминал не видит его, проверьте, что `~/.local/bin` в `$PATH`:

```bash
echo $PATH | tr ':' '\n' | grep -q "$HOME/.local/bin" && echo "OK" || echo "НУЖНО ДОБАВИТЬ"
```

Если нет, добавьте в `~/.zshrc` (macOS по умолчанию) или `~/.bashrc`/`~/.bash_profile`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Затем откройте новую вкладку Terminal. Либо запустите напрямую:

```bash
bash ~/.local/share/RepoPilotBridge/app/start.sh
```

## Doctor падает

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File .\doctor.ps1
```

### macOS / Linux

```bash
bash ./doctor.sh
```

В конце будет путь к отчёту. При обращении за помощью приложите строку `[FAIL]` и путь к отчёту.

## macOS: `pbcopy` / буфер обмена не работает

KaroX использует `pbcopy` для копирования промптов/ключей. Если `pbcopy` недоступен (например, в SSH-сессии без GUI), KaroX выведет содержимое файла прямо в терминал — скопируйте его вручную. Для работы `pbcopy` нужен локальный сеанс macOS Terminal.

## macOS: ярлык `.command` не запускается по двойному клику

Если macOS показывает «не удается открыть, поскольку не удалось проверить разработчика»: правый клик по `~/Desktop/KaroX.command` → «Открыть» → «Открыть» в диалоге. Это разовое подтверждение для Quartz Gatekeeper.

## macOS Apple Silicon vs Intel: путь Homebrew

Homebrew на Apple Silicon ставит бинарники в `/opt/homebrew/bin`, на Intel — в `/usr/local/bin`. KaroX ищет `cloudflared` и `tailscale` в обоих местах. Если инструмент установлен, но не находится, проверьте:

```bash
which cloudflared
ls /opt/homebrew/bin/cloudflared /usr/local/bin/cloudflared 2>/dev/null
```

Если бинарник в нестандартном месте, добавьте его каталог в `$PATH`.

## Mission Control показывает блокировку или предупреждение

Откройте карточку сессии и нажмите `M`. Поле `recommendedNextAction` объясняет безопасный следующий шаг:

- `stop_and_report_branch_mismatch` — остановитесь: ветка процесса не совпадает с карточкой;
- `wait_for_or_start_real_task` — метка сессии не является задачей; сначала отправьте реальное ТЗ;
- `inspect_existing_changes` — в рабочем дереве уже есть изменения; сначала изучите diff;
- `inspect_project_context_then_execute_task` — preflight согласован, можно изучить проект и выполнять задачу.

`GET /context/brief` не исправляет состояние автоматически и не содержит `X-API-Key`. Если endpoint недоступен у уже запущенной сессии после обновления KaroX, остановите её и создайте новую: работающий процесс использует код той версии, с которой был запущен.
