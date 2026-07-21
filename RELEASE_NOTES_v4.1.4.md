# ★ KaroX v4.1.4 — безопасное атомарное обновление

## Критический hotfix

- **Обновление больше не удаляет рабочую `app` до полной проверки новой версии.** Новый build сначала копируется в отдельную staging-папку, проходит генерацию PowerShell/POSIX launcher, интеграцию Notion, PowerShell parser и product doctor — и только после этого атомарно активируется.
- **Предыдущая установка сохраняется для rollback.** Если активация не удалась, KaroX возвращает прежнюю `app`; незавершённый rollback восстанавливается при следующем запуске installer.
- **Исправлено зависание на `app\server is being used by another process`.** Update guard теперь распознаёт `app_entry:app`, watchdog `karox_supervisor.py` и любые процессы, реально запущенные из установленной `app`.
- **Updater больше не ждёт installer внутри процесса, который сам загружен из удаляемой `app`.** Installer запускается отдельно, ждёт завершения старого updater и только затем освобождает файлы.
- **Исправлена несовместимость `patch_notion_provider.py` с per-session выбором туннеля из v4.1.3.** Точная комбинация `start.core` + patcher теперь проверяется до активации.
- Добавлен режим `install.karox.ps1 -ValidateOnly`: он собирает и полностью проверяет staging-кандидат, не меняя текущую установку.

## Проверено на реальной Windows-установке

- staging build и runtime rebrand;
- генерация Notion PowerShell/POSIX launchers;
- Windows PowerShell parser;
- полный product doctor staging-кандидата;
- atomic installer/update regression suite;
- tunnel/session regression suite;
- KaroX unit tests и Tailscale readiness tests.

## Обновление

Пользователям v4.1.2 рекомендуется дождаться публикации v4.1.4 и выполнить:

```powershell
karox update
```

Версию v4.1.3 повторно устанавливать не нужно.
