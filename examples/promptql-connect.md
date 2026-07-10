# Connect KaroX to PromptQL / Подключение KaroX к PromptQL

## Recommended flow / Рекомендуемый сценарий

1. Open a LIVE KaroX session card / Откройте карточку LIVE-сессии.
2. Press `V` to verify `/session`, `/health`, `/git/status`, `/context/brief`, `repoRoot`, and branch locally.
3. Optionally press `M` to review warnings and the recommended next action.
4. Press `C` and paste the generated localized connection prompt into PromptQL.
5. Create the **personal** custom integration requested by the prompt.
6. Press `K` and enter the session-specific `X-API-Key` only in PromptQL's protected connection card.
7. Let the AI repeat preflight through the tunnel.
8. Send the real task as a separate message.

1. Нажмите `V` для локальной проверки точной сессии.
2. При необходимости нажмите `M`, чтобы просмотреть предупреждения и рекомендуемый следующий шаг.
3. Нажмите `C` и вставьте сгенерированный prompt в PromptQL.
4. Создайте запрошенную **личную** custom integration.
5. Нажмите `K` и вставьте ключ только в защищённую карточку подключения.
6. Дождитесь повторного preflight через туннель.
7. Пришлите реальное ТЗ отдельным сообщением.

## Preflight contract

- `GET /session`
- `GET /health`
- `GET /git/status`
- `GET /context/brief`
- exact match: `repoRoot`, `branch`, `mode`, `commitAllowed`, `pushAllowed`

If PromptQL reports `URL is not allowed`, the integration still points to another tunnel host. Create or update only the provider ID printed by the current session.

Если PromptQL сообщает `URL is not allowed`, интеграция всё ещё привязана к другому tunnel host. Создайте или обновите только provider ID текущей сессии.

Never paste `X-API-Key` into chat. Never reuse another active session's credential.
Никогда не отправляйте `X-API-Key` в чат и не переиспользуйте credential другой активной сессии.

## Mission Control contract / Контракт Mission Control

`GET /context/brief` is read-only and returns no `X-API-Key`. It consolidates the active task, exact repository and branch, permissions, changed paths, project-context entry points, warnings, and `recommendedNextAction`.

`GET /context/brief` работает только на чтение и не возвращает `X-API-Key`. Он объединяет активную задачу, точный репозиторий и ветку, разрешения, изменённые пути, точки поиска контекста, предупреждения и `recommendedNextAction`.

Treat `stop_and_report_branch_mismatch` as a hard stop. For `inspect_existing_changes`, review the diff before editing. The brief complements—never replaces—the exact preflight and human review.
