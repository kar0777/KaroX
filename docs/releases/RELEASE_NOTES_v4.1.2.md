# KaroX v4.1.2 — cloudflared устанавливается сам

## Исправлено

- **Новая установка больше не упирается в «cloudflared не найден».** Раньше `install.ps1` вообще не скачивал cloudflared (только копировал его из старой установки RepoPilotBridge, если она была), а сообщение об ошибке советовало перезапустить install.ps1 — что не помогало. Теперь:
  - `install.ps1` при отсутствии cloudflared спрашивает `[Y/n]` (Enter = да) и скачивает официальный бинарь с github.com/cloudflare/cloudflared в `%LOCALAPPDATA%\KaroX\bin` — без winget и без админ-прав.
  - Лаунчер делает то же самое при запуске: если выбран Cloudflare Tunnel, а cloudflared отсутствует — один Enter, и туннель поднимается.
  - Если прямое скачивание не удалось, используется fallback `winget install Cloudflare.cloudflared`.
- `Find-Cloudflared` теперь ищет бинарь и в `%LOCALAPPDATA%\KaroX\bin`, а текст ошибки подсказывает автоскачивание и ручную команду winget вместо тупикового совета.

## Обновление

```powershell
karox update
```
