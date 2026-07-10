# Как развивать Star For KaroX

Спасибо за интерес к проекту. Главный принцип Star For KaroX: удобство для пользователя не должно ослаблять защитные ограничения.

## Локальная проверка

Перед отправкой изменений запустите:

```powershell
powershell -ExecutionPolicy Bypass -File .\doctor.ps1 -NoPause
```

Минимально также проверьте синтаксис Python:

```powershell
python -m py_compile .\server\repo_tools.py
```

Если Python-пакеты установлены только во внутренний runtime KaroX, используйте:

```powershell
& "$env:LOCALAPPDATA\RepoPilotBridge\.venv\Scripts\python.exe" -m py_compile .\server\repo_tools.py
```

## Что важно сохранять

- Не добавляйте endpoints, которые читают секреты или выходят за пределы выбранного репозитория.
- Не разрешайте `git push` через API.
- Не обходите `/git/commit` прямыми `git add` / `git commit` в режиме `Автопилот`.
- Все пользовательские тексты, подсказки и ключевая документация должны сохранять English/Русский parity.
- Технические имена endpoint-ов и JSON-полей можно оставлять английскими ради совместимости OpenAPI-клиентов.

## Стиль изменений

- Держите PowerShell-сценарии понятными для обычного Windows-пользователя.
- Ошибки должны объяснять, что случилось и что делать дальше.
- Для новых возможностей обновляйте `README.md`, `QUICKSTART.md` или `TROUBLESHOOTING.md`, если это влияет на пользователя.
- Если меняется поведение безопасности, обновляйте `SECURITY.md`.
