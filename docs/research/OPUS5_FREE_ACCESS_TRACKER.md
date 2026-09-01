# Claude Opus 5 — проверка бесплатного доступа для вайбкодинга

**Последнее обновление:** 2026-08-15 Europe/Warsaw  
**Цель:** найти легальный доступ к Claude Opus 5 через IDE, CLI или API с практической ценностью не ниже $20, без банковской карты, пригодный для полноценного coding-agent workflow.

## Жёсткие критерии

Кандидат считается `PASS` только когда подтверждены все пункты:

1. Реальный Claude Opus 5, а не старый Opus, псевдоним или marketing page.
2. IDE/CLI/API либо встроенный агент, который умеет читать и изменять репозиторий, запускать команды и тесты.
3. Бесплатный лимит эквивалентен минимум $20.
4. Карта не требуется.
5. Можно использовать сейчас, без ожидания гранта, ручного одобрения или конкурса.
6. Модель и лимит подтверждены страницей аккаунта, журналом расхода либо реальным запросом.

Статусы: `PASS`, `TESTING`, `REJECTED`, `BLOCKED`.

## Проверено через KaroX Browser

| Сервис | Тип | Что подтверждено | Лимит/баланс | Вердикт | Причина |
|---|---|---|---:|---|---|
| Keyplex | API | `CHANGED` 2026-08-18: current signup/API docs are live; exact `anthropic/claude-opus-5` + `anthropic/claude-fable-5`; OpenAI-compatible API | Free Trial docs now explicitly publish 20 RPM / 40k TPM / **100,000 tokens/day**; no card required. Trial duration and authenticated premium-model debit still need production confirmation. | TESTING / CHANGED | Old 404/signup evidence is stale, but do not call WORKS until a new account successfully calls Opus 5/Fable 5 and trial duration is observed. Sources: https://keyplex.ai/ ; https://keyplex.ai/documentation |
| CometAPI | OpenAI-compatible API | В каталоге есть Claude Opus 5; аккаунт и dashboard открылись | $0.00 | REJECTED | Нет бесплатного баланса. |
| AIMLAPI | API + playground | В аккаунте виден Claude Opus 5 и API-раздел | $0.00 | REJECTED | Нет бесплатного баланса. |
| EvoLink | API | Регистрация через GitHub без карты; dashboard и credits работают | 10 внутренних credits ≈ $0.15 по курсу 650 credits = $10 | REJECTED | Намного меньше $20. |
| TokenLab | API/MCP | Claude 5 заявлен; dashboard работает | $1.00 signup credit | REJECTED | Меньше $20. |
| Poe | Anthropic/OpenAI-compatible API + chat | В чате есть официальный Claude-Opus-5; API-каталог содержит 331 модель | 300 points; Opus 5 отсутствовал в API-каталоге | REJECTED | Чат не превращается в полноценный Opus 5 API/Claude Code route. |
| Factory | IDE/cloud coding agent | Полноценный агент и repository workflow; аккаунт открыт | Бесплатного usage-баланса нет; Activate Paid Plan | REJECTED | Trial без карты не активирован. |
| Qodo | IDE/PR agent | Регистрация без карты; code-review platform работает | Trial заявлен, но точный Opus 5 coding-agent лимит не подтверждён | REJECTED | Главный продукт — review/governance, а не полноценный вайбкодинг Opus 5. |
| NinjaChat | Web agent/chat/API pages | Есть marketing page Claude Opus 5 и dashboard | Платные планы; бесплатный $20+ лимит не найден | REJECTED | Не подтверждён IDE/CLI/API лимит $20+. |
| LLM Gateway | API gateway | Opus 5 присутствует в публичном реестре провайдеров | Не подтверждён | BLOCKED | OAuth через GitHub падал; бесплатный $20+ баланс не подтверждён. |
| OpenCode Zen | CLI/API provider | Opus 5 присутствует в публичном реестре моделей | Не подтверждён | BLOCKED | GitHub authorization page не завершалась; free credit не подтверждён. |
| CrossModel | API gateway | Opus 5 присутствует в публичном реестре моделей | Не подтверждён | BLOCKED | GitHub OAuth не завершался; free credit не подтверждён. |
| Vibany | Web app | Реальный продукт открыт | 0 credits | REJECTED | Это генератор изображений/видео, не coding-agent. |
| Codebuff | CLI coding agent | Полноценный terminal agent; умеет менять файлы, запускать команды, typecheck и tests; официальные docs указывают Default/Max на Claude Opus 5 | В аккаунте 0 renewing credits и 0 other credits; минимальная покупка $10 | REJECTED | Нормальный CLI, но бесплатного лимита нет и порог $20 не выполнен. Проверено 2026-08-02 через `/usage`. |
| Kilo Code | VS Code / JetBrains / CLI / Cloud agent | Полноценный coding-agent во всех нужных интерфейсах; Opus 5 доступен через Kilo Gateway | В кабинете $0.00 available, promotional expiry отсутствует | REJECTED | Инструмент подходит технически, но бесплатных Opus 5 credits нет; бонус Kilo Pass требует покупки. Проверено 2026-08-02 через `/credits`. |
| Amp | CLI / remote coding agent | Полноценный агент с локальными и remote/orb workflows; раньше существовал daily free frontier grant | Новый аккаунт: Balance $0, payment method отсутствует, free grant не начислен | REJECTED | Старое предложение до $10/day не доступно новому аккаунту; без оплаты пользоваться frontier-agent нельзя. Проверено 2026-08-02 через `/settings/billing`. |
| VM0 Zero | CLI / sandboxed agent runtime | Официально заявлены 7-day trial, starter credits, встроенный `claude-opus-5`, CLI и изолированное выполнение | Не удалось прочитать | BLOCKED | Web app в текущем Karo Chrome показывает `Update Chrome to continue`; размер starter credits и реальный Opus 5 run не подтверждены. Не считать найденным способом до повторной проверки. |
| Windsurf / Devin Desktop | IDE / CLI / cloud coding agent | Аккаунт создан; доступны Windsurf Editor и Devin CLI; в Devin есть полноценные cloud sessions, terminal и repository workflow | План Free; trial `Cancelled`; `You don't have an active subscription`; Opus 5 и $20+ лимит не подтверждены | REJECTED | Кнопка Start Free Trial привела к аккаунту без активной подписки. В кабинете указано `Trial ended, $20/mo after`, а Windsurf profile показывает Free. Проверено 2026-08-02 через Devin Usage & Limits и Windsurf Profile. |
| Cursor | IDE / CLI / cloud coding agent | Opus 5 официально доступен; новый аккаунт и onboarding прошли | Trial ведёт на Stripe Checkout с обязательными полями карты, срока и CVC | REJECTED | Без карты trial не активируется. Проверено 2026-08-02 до страницы оплаты; платёжные данные не вводились. |
| Zed | IDE / agent | Pro trial даёт $20 hosted-model tokens на 14 дней | Claude Opus исключён из trial | REJECTED | Сумма подходит, но целевая модель недоступна в бесплатном trial. |
| Warp | Terminal coding agent | Полноценный terminal agent и hosted models | Free plan — BYOK; собственные monthly credits начинаются с платного Build | REJECTED | Нет бесплатного hosted Opus 5 лимита. |
| Qoder | IDE / coding agent | Бесплатный trial даёт 300 credits | Существенно меньше эквивалента $20; точный Opus 5 не подтверждён | REJECTED | Не проходит порог ценности и модельный критерий. |
| TRAE | IDE / coding agent | Полноценный agent workflow и trial | Для активации trial требуется payment method | REJECTED | Не проходит критерий без карты. |
| Databricks Express Trial | API / model serving | Публично заявлены trial credits и endpoint `databricks-claude-opus-5` | Практически недоступен для пользователя | REJECTED | Пользователь проверил маршрут и сообщил, что Databricks не подходит. Не предлагать снова. |
| Puter | OpenAI-compatible API / browser auth | Реальный `anthropic/claude-opus-5`; в кабинете виден ресурсный баланс | Баланс оплачивается самим пользователем по модели User-Pays | REJECTED | Это не бесплатный кредит сервиса: расходы списываются с пользовательского баланса. Пользователь подтвердил, что это его деньги. Не предлагать снова. |
| Replit Starter | Cloud IDE / Agent / AI Integrations | Есть бесплатные ежедневные Agent credits и ограниченные monthly cloud credits | Точный эквивалент не раскрыт; Opus 5 официально не заявлен | REJECTED | Не доказаны ни $20+, ни точная модель. Самые мощные модели и $25 monthly credits относятся к Core/Pro. |
| Clarifai | OpenAI-compatible API / model marketplace | Даёт до двух welcome bonuses по $5 после SMS; Anthropic-модели в каталоге есть | Максимум $10 при personal + org; Opus 5 не найден | REJECTED | Ниже порога $20 и нет подтверждённого Opus 5 endpoint. |
| Augment Code | IDE / JetBrains / CLI / Cosmos coding agent | Полноценный coding workflow; historical/current credit-plan blog still says signup trial pool 30,000 credits with valid card | Opus 5 is live in Cosmos, but current public pricing/UI no longer exposes the old trial clearly; additionally Augment Privacy Policy says the Service is not intended for anyone under 18 | REJECTED_FOR_CURRENT_USER / NEEDS_LIVE_ACCOUNT_CHECK_FOR_18+ | Технически сильный CHANGED-кандидат для взрослого пользователя, но для текущего пользователя не подходит по age gate. Даже для 18+ не повышать до WORKS без live-account confirmation: Billing=30,000 trial credits + Opus 5 in model picker. Проверено 2026-08-17. |
| Ona | Cloud coding agent / CLI environments | Полноценный agent workflow; free tier includes OCUs | Hosted agent сейчас на Opus 4.6; custom Anthropic требует Enterprise и свой API key | REJECTED | Бесплатные OCUs не дают hosted Opus 5. OSS program требует заявки и не считается instant route. |
| AWS Educate | Cloud labs | Регистрация с 13 лет без карты | Anthropic в Bedrock требует valid payment method для AWS Marketplace | REJECTED | Бесплатные учебные labs не дают свободный Opus 5 API. |
| JetBrains AI Trial | IDE / AI Assistant / Junie | 30-дневный AI Pro trial; individual trial = 10 AI Credits ($10-equivalent), organization trial = 20 credits | Junie Marketplace release notes 2026-07-23 подтверждают `Support for Anthropic Opus 5`; AI Trial даёт 30 дней AI Pro и Junie входит в JetBrains AI subscription. Но current AI Assistant supported-model table всё ещё не перечисляет Opus 5 среди hosted JetBrains AI service models | CHANGED / NEEDS_PRODUCTION_CHECK / BELOW_TARGET_VALUE | Сильный mainstream backup, но не называть WORKS: first-party surfaces расходятся/отстают, поэтому после активации Trial надо лично подтвердить Opus 5 в Junie model selector. Individual quota также только $10, ниже желаемых $20. Проверено 2026-08-17. |
| APIPod → Brainbase Labs | Cloud coding agent / Claude Code harness / API | Google OAuth успешно создал Brainbase-организацию и агента `Claude Code` с выбранной моделью `Claude Opus 5`; доступны tasks, filesystem, browser, tools и cloud sandbox | $25 signup credit, план `Kafka - Free`, карта не добавлена, срок действия отсутствует | REJECTED | Формально проходит денежный и модельный фильтр, но APIPod неожиданно ведёт в Brainbase, а пользователь уже использовал продукт и признал его неудобным для нормального вайбкодинга. Не предлагать снова. Проверено 2026-08-02 через KaroX Billing и agent UI. |
| Cosmic | Cloud code agent / GitHub workflow / API | Opus 5 заявлен на всех планах; бесплатный план без карты поддерживает 1 агента, GitHub-репозиторий, ветки и PR | 300k input + 300k output tokens в месяц, примерно до $9 по прямой цене Opus 5 | REJECTED | Технически хороший near-miss, но бесплатный объём меньше обязательного порога $20. |
| MindsHub | Agent platform / API | Opus 5 доступен как платная/BYOK-модель | Бесплатные 5M tokens относятся только к собственному open-model alias MindsHub Air | REJECTED | Free quota нельзя тратить на Opus 5. |
| ilisai | Web AI workspace | Opus 5 доступен на бесплатном плане | 400 credits ≈ €4 в месяц | REJECTED | Ниже $20 и нет полноценного IDE/CLI/API coding-agent workflow. |
| GitHub Copilot Student | IDE / coding agent | Студенческий план содержит AI credits; Opus 5 доступен только на более высоких коммерческих планах | 200 AI credits; модель выбирается автоматически | REJECTED | Нельзя гарантированно выбрать Opus 5, а денежный эквивалент ниже порога. |
| DigitalOcean Student Pack | Cloud credits / inference API | Student Pack даёт $200 cloud credits; DigitalOcean Inference поддерживает Opus 5 | Anthropic и другие сторонние AI-модели исключены из использования student credits | REJECTED | Большой баланс существует, но его нельзя расходовать на целевую модель. |
| DigitalOcean ordinary new-user credit | OpenAI-compatible inference / Claude Code / Cline / OpenCode | Exact `anthropic-claude-opus-5` есть в managed Inference и coding-agent integration официально поддерживается | С 2026-07-15 standard new-account promo снижен до $5/90 days; кроме того, Inference Tier 1/2 не имеют доступа к Anthropic, а serverless inference требует positive prepaid balance | CHANGED / REJECTED_LOW_VALUE_AND_ACCESS | Старые упоминания $200 new-user trial устарели. Даже нынешние $5 ниже порога, и новый low-tier account не получает Anthropic model access. Не предлагать как Opus 5 trial. Проверено 2026-08-17. |
| SVRTR | Claude-compatible API / Claude Code route | Точный Opus 5 и инструкции для Claude Code/Cline/Continue | Бесплатного баланса нет; при нуле API возвращает 402, пополнение через USDT | REJECTED | Не бесплатный маршрут и неудобная схема оплаты. |
| EcomAgent | Coding agent | Free trial существует | Trial даёт Opus 4.8/Sonnet 5; Opus 5 только на платном плане | REJECTED | Целевая модель исключена из бесплатного trial. |
| Henry | Slack/Teams agent + GitHub/custom MCP | Публично заявлены $100 trial credits, Opus 5, GitHub и custom MCP | Не подтверждён | REJECTED | Регистрация не работает; пользователь уже проверял сервис ранее. Не предлагать снова. |
| Forge Gateway | OpenAI-compatible API gateway | В существующем аккаунте подтверждены $59.99 FREE, активные API-ключи и успешные запросы | $59.99, без карты | REJECTED | Живой каталог содержит GPT-5.6 Sol, Sonnet 5, GLM-5.2 и Opus 4.5, но `claude-opus-5` отсутствует. Полезный резерв, но не решение задачи. Проверено 2026-08-02 через KaroX dashboard и Models Catalog. |
| FSZK API | API gateway | Ранее заявлялся крупный стартовый баланс | Недоступен | REJECTED | Домен `ai.fszk-api.pl` не разрешается в DNS. Оффер мёртв. Проверено 2026-08-02. |
| OpenHands Cloud | Cloud IDE / CLI coding agent | Полноценный репозиторный агент, GitHub-интеграция, терминал, MCP и настраиваемые LLM-профили | $0.00 в реальном аккаунте | REJECTED | Старый автоматический бонус $20 больше не начисляется. Проверено 2026-08-02 через KaroX Billing. |
| WorldRouter | OpenAI-compatible API / Claude Code / OpenCode route | Точный `claude-opus-5`, API и CLI-инструкции публично доступны | `0 Credits` в реальном аккаунте | REJECTED | No-card аккаунт существует, но стартового баланса нет. Проверено 2026-08-02 через KaroX dashboard. |
| Anthropic Console | First-party API / Claude Code | Официальный Opus 5 API и Claude Code; организация создана | $0.00, карта не добавлена | REJECTED | Стартовый API-кредит аккаунту не начислен; для использования требуется покупка credits. Проверено 2026-08-02 через Claude Platform Billing. |
| Tembo | Cloud coding agent / hosted models | Free tier существует | 10 credits ≈ $10; free tier ограничен open-source моделями | REJECTED | Ниже порога $20, а Opus 5 относится к платным/BYOK-маршрутам. |
| MonoRouter | Claude-compatible proxy / CLI route | Бесплатная сервисная маршрутизация и поддержка Claude Code/OpenCode | Требует уже оплаченную Claude Pro/Max-подписку | REJECTED | Не предоставляет собственный бесплатный доступ к модели; лишь проксирует существующую платную подписку. |
| Kiro | IDE / CLI / web coding agent | В аккаунте активен Kiro Free; доступны IDE/CLI/Web, MCP и 50 credits/месяц | 50 free credits; Opus 5 отсутствует в model picker | REJECTED | Платный период закончился. Доступны Auto, Sonnet 4.5/4, Haiku 4.5, DeepSeek, MiniMax, GLM и Qwen, но не Opus 5. Проверено 2026-08-02 через Account и model picker. |
| GitHub Copilot | IDE / CLI / cloud agent | Exact Opus 5 теперь официально доступен в Pro+/Max/Business/Enterprise и полноценном VS Code/JetBrains/CLI/cloud-agent workflow | Current new-user free trial route отсутствует: GitHub paused all Copilot Pro trials 2026-04-13; paid individual signups later reopened, но current plans/docs не показывают возобновлённый general-user trial | CHANGED / REJECTED_NO_TRIAL | Модель появилась 2026-07-24, но это не создаёт бесплатный маршрут: Copilot Free не включает exact Opus 5, а Pro+/Max требуют оплаты. Не предлагать как trial, пока GitHub first-party явно не объявит reopening trials. Проверено 2026-08-17. |
| TokenHub | OpenAI/Anthropic-compatible API / Claude Code route | Точный Opus 5 присутствует в каталоге и есть полноценная консоль | $0.00 | REJECTED | Новый/существующий аккаунт не получил starter credits; dashboard прямо требует пополнить баланс. Проверено 2026-08-02. |
| OfoxAI | OpenAI/Anthropic-compatible API / Claude Code route | Есть Claude Code integration и точный Opus 5 в каталоге | Общий баланс $0; bonus $0; gift $0 | REJECTED | Бесплатного кредита нет; реферальный бонус только $2. Проверено 2026-08-02 через Wallet. |
| Kelly AI | OpenAI/Anthropic-compatible API | Внутренний `/api/pricing` подтверждает точный `claude-opus-5` в Anthropic и OpenAI форматах; публично обещаны $20 без карты | Не начислен | REJECTED | Регистрация через Google ломается с `Error 400: invalid_request`; пользователь подтвердил скриншотом. Пока OAuth не исправлен, маршрут нерабочий. Проверено 2026-08-02. |
| Claude Pro $100 promo | Claude Code / web / Opus 5 | Официальная акция $100 usage credits существует для пользователей, у которых был Pro на 2026-07-19 | Аккаунт пользователя — Free; eligibility banner отсутствует | REJECTED | Аккаунт не соответствует условию акции; активировать нечего. Проверено 2026-08-02 через Claude Billing. |
| Zo Computer | Cloud computer / coding agent / files / terminal | Машинный каталог подтверждает точный `zo:anthropic/claude-opus-5`, контекст 1M и агентную среду | Opus 5 имеет тип `subscribers`; free allowance на него не распространяется | REJECTED | Бесплатными помечены другие модели, но Opus 5 требует подписку. Проверено 2026-08-02 напрямую через `/models/catalog`. |
| AiFuse | Multimodel web workspace | 14-day trial без карты и 40 сообщений/день | Opus 5 не входит в список бесплатных trial-моделей | REJECTED | Кроме отсутствия бесплатного Opus 5, это не полноценный IDE/CLI/API репозиторный workflow. |
| Kelly AI OAuth recheck | API gateway | `claude-opus-5` и обещание $20 подтверждены | Получить бонус невозможно | REJECTED | Google OAuth возвращает invalid request; кнопка GitHub также ошибочно ведёт в Google callback. Новая регистрация отключена администратором. |
| Cloudflare AI | Unified API / Workers / Agents | Официальный каталог содержит `anthropic/claude-opus-5` | Бесплатные 10,000 neurons/day относятся только к Workers AI-моделям `@cf/*` | REJECTED | Opus 5 является third-party моделью и списывается через Unified Billing из заранее купленных credits; бесплатная Workers AI квота на него не действует. |
| AgentRouter (без дефиса) | Claude Code relay / New API gateway | Живой каталог содержит `claude-opus-5`; сайт заявляет Claude Code, Codex, Roo Code и Qwen Code; публичное объявление обещает $25 за check-in | Потенциально $25 без карты | BLOCKED | Сервис описывает себя как публичный resource-sharing relay, сообщает о массовых upstream-блокировках Claude-аккаунтов и нестабильности. Домен помечен внешними системами как рискованный. OAuth просит только `user:email`, но юридическая авторизация upstream не доказана; старый GitHub не подключать. Проверено 2026-08-02 через `/api/status`, `/pricing` и frontend bundle. |
| Agent Router (с дефисом) | MCP marketplace | Бесплатный вызов специализированных MCP-агентов | Builder bonus только $5 | REJECTED | Это другой домен и другой продукт; он не предоставляет hosted Claude Opus 5 и не связан с заявленными $150. |
| Verdent | IDE / parallel coding agents | Opus 5 поддерживается; 7-day trial даёт 100 credits | Примерно $5.90 по тарифной сетке | REJECTED | Ниже обязательного порога $20. |
| Medham | Routing / observability | Поддерживает Opus 5 и no-card trial | BYOK | REJECTED | Работает на пользовательских provider keys, собственные inference credits не выдаёт. |
| MitDotKey | OpenAI/Anthropic-compatible API | Точный Opus 5 и регистрация без карты | Free plan только для free-tier models | REJECTED | Премиум-модели, включая Opus 5, требуют пополнения. |
| UnifyLLM | API gateway | Может выдавать до $1000 trial credits | Только после заявки и ручного review | REJECTED | Не является мгновенным маршрутом; нарушает критерий без ожидания гранта/одобрения. |
| Cosine Genie | CLI / IDE / cloud coding agent | Trial даёт 2M credits; полноценный repository workflow | ≈ $9.50 относительно Starter 4M/$19 | REJECTED | Ниже $20; точный бесплатный Opus 5 не доказан. |
| Blackbox AI | IDE / CLI / cloud/API agents | Полноценный agent workflow | Бесплатно только MiniMax; frontier credits после покупки Pro | REJECTED | $20 model allowance относится к платному первому месяцу, а не бесплатному no-card trial. |
| Sweep | JetBrains coding agent / API | Бесплатный trial | $5 API credits | REJECTED | Ниже $20. |
| GitHub Education status | Student benefits | Проверен существующий аккаунт `kar0777` | Benefits не активированы | REJECTED | Страница предлагает только `Start an application`; мгновенного student benefit нет, новая заявка требует ручного рассмотрения. |
| Hidden $350 Google Doc | Referral/secret-site lead | Публичный документ обещал скрытый сервис с Opus 5 и $350 | Не подтверждено | REJECTED | Документ скрывает единственную ссылку в canvas и не раскрывает компанию/условия; выглядит как referral bait. Авторизацию или переход на скрытый сайт не выполнять. |

## Уже известные неподходящие категории

- Web-chat без репозиторного агента.
- Review-only инструменты без самостоятельного редактирования и запуска команд.
- BYOK gateways с нулевым балансом.
- Signup bonus $1–$10.
- Trial, который требует карту.
- Гранты, startup/OSS/research applications и ожидание ручного решения.
- Каталог модели без доказанного вызова и записи списания.

## Следующий поисковый фильтр

Искать только:

- новые IDE/CLI coding agents с hosted inference;
- terminal agents с собственным балансом, а не BYOK;
- API-провайдеры с автоматическим signup credit >= $20;
- временные launch promotions >= $20;
- официальные bundles/partner perks, активируемые сразу без карты;
- сервисы, где можно доказать Opus 5 через model ID, response metadata и usage log.

## Формат будущих записей

Каждая проверка обязана содержать:

- URL и дату проверки;
- способ регистрации;
- нужна ли карта;
- точный баланс в долларах или расчёт эквивалента;
- точный model ID;
- наличие file edit / terminal / tests;
- доказательство реального запроса;
- окончательный статус.

## Проверка 2026-08-04 — новая волна после полной дедупликации

### Исправление процесса

- GitLab Duo не является новым брендом: GitLab уже присутствовал в `OUTREACH_GMAIL_TRACKER.md`, старых research-волнах и дедуп-аудите. **Но статус маршрута изменился 2026-08-17:** current first-party GitLab docs теперь подтверждают обычный **30-day Ultimate Trial без карты**, который для Free-tier пользователя включает **24 GitLab Credits** на Duo Agent Platform; отдельный repo-wide search по `GitLab Ultimate trial|24 GitLab credits|Duo Agent Platform trial` дал **0 совпадений**, поэтому конкретный trial→credits→Opus5 маршрут раньше не был зафиксирован.
- Новый обязательный gate: до внешней проверки каждое название, домен, former brand и parent company сначала искать по всему репозиторию KaroX, а не только по этому файлу.

### Aura — `TESTING / STRONG_NEW_LEAD`

- **URL:** `https://aura.ai/`
- **Новизна:** поиск `Aura`, `Aura AI` и `aura.ai` по всем 1 655 файлам KaroX дал 0 совпадений до этой записи.
- **Тип:** open-source desktop coding-agent / agent OS; есть Code и Plan modes, project/agent workflows, команды и sandboxed execution; доступны Windows, macOS и Linux builds.
- **Точные модели:** текущий `aura-os` model catalog содержит `aura-claude-opus-5` и `aura-claude-fable-5`; `aura-router` напрямую мапит их на Anthropic `claude-opus-5` и `claude-fable-5`.
- **Бонус:** production billing code использует default signup grant `5000` cents/credits = **$50** через `SIGNUP_GRANT_CREDITS`; registration test plan также ожидает 5 000 welcome credits. Free/Mortal account дополнительно рассчитан на 2 500 monthly allowance и 50 daily credits.
- **Регистрация:** официальный login surface показывает `Create Account`; changelog подтверждает open signup и восстановление signup-credit delivery в stable desktop builds. Карта относится к отдельной покупке/апгрейду и не фигурирует в обычном account-creation flow.
- **Возраст:** Terms of Service требуют 18 лет или возраст совершеннолетия в юрисдикции. Егор не должен регистрировать аккаунт от своего имени; маршрут допустим только через родителя/совершеннолетнего владельца аккаунта без ложных данных.
- **Не доказано:** production env может переопределить default `SIGNUP_GRANT_CREDITS`; реальный новый аккаунт, dashboard balance, успешный Opus 5 запрос и usage debit ещё не наблюдались.
- **Статус:** `TESTING`, не `PASS`, пока не подтверждены реальный баланс и запрос в production.
- **Следующий безопасный тест:** совершеннолетний владелец создаёт один обычный аккаунт без карты, проверяет Credit History на `signup_grant = 5000`, выбирает Opus 5, запускает маленькую coding-задачу и сохраняет model/usage evidence. Не создавать повторные аккаунты и не использовать referral abuse.

### Другие действительно новые названия, не прошедшие фильтр

- **CCVibe:** заявляет no-card trial, Claude Fable 5, API gateway и поддержку Польши, но публично не раскрывает размер signup credit. `BLOCKED` до доказанного баланса >= $20.
- **Modelis:** live OpenAI-compatible endpoint содержит `anthropic/claude-fable-5`, однако автоматический бонус >= $20 и no-card условия не доказаны. `REJECTED/UNPROVEN`.
- **XPersona:** Fable 5 и OpenCode-compatible API подтверждены, но доступ начинается с платного плана/пополнения. `REJECTED`.

## Исправление дедуп-процесса 2026-08-11

Пользователь подтвердил, что следующие сервисы уже встречались в прежних списках/проверках и **не должны предлагаться повторно**, даже если конкретное старое упоминание не находится текущим текстовым индексом репозитория:

- **Foxl** — `foxl.ai` — `KNOWN / DO_NOT_SUGGEST_AGAIN`.
- **FastRouter** — `fastrouter.ai` — `KNOWN / DO_NOT_SUGGEST_AGAIN`.
- **AgentsFlare** — `agentsflare.com` — `KNOWN / DO_NOT_SUGGEST_AGAIN`.
- **Zyloo** — `zyloo.io` — `KNOWN / DO_NOT_SUGGEST_AGAIN`; также найден в `OUTREACH_GMAIL_TRACKER.md`.
- **Logen** — `KNOWN_LOW_VALUE / DO_NOT_SUGGEST_AGAIN`.

### Обязательный gate после ошибки 2026-08-11

- Перед выдачей кандидата искать название + домен + former brand/parent company по всему KaroX repository; успешный gate требует `files_scanned > 0`.
- Дополнительно проверять master/outreach/research trackers и явный пользовательский denylist выше.
- Если KaroX временно возвращает network error или `files_scanned: 0`, кандидат не считается новым.
- Не выдавать как сильную находку сервисы с неопределённым welcome credit, request-access вместо мгновенного trial, BYOK, либо бонусом порядка $1–$10.
- Результаты будущего поиска показывать только после внешней first-party проверки exact `claude-opus-5`, бесплатного no-card entitlement и практического agent/API/CLI workflow.

### Проверенные новые кандидаты — 2026-08-11, строгая волна

Перед этой записью один полный regex-проход KaroX по `Tokenly|tokenly.llc|Xyris|xyris.site|WyberAi|wyberai.com|anymize|anymize.ai` завершился успешно: `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. То есть до записи ниже эти названия не встречались в индексируемых файлах репозитория.

- **WyberAi** — `https://wyberai.com/` — `REJECTED_LOW_VALUE`. Полноценный AI app-builder с real-code workflow, GitHub и MCP; no-card signup и 50 free credits. Платный Starter даёт 150 credits за $29, поэтому 50 бесплатных credits дают лишь около **$9.67 plan-equivalent**, ниже обязательного порога $20. Не предлагать как сильный Opus 5 route.
- **Tokenly** — `https://www.tokenly.llc/` — `REJECTED_LOW_VALUE`. OpenAI-compatible API, exact Claude Opus 5 и 50 стартовых credits без карты; дальнейшие пополнения привязаны к surveys/ads/sponsor actions. Мгновенный стартовый объём не доказан как эквивалент $20+, поэтому не предлагать.
- **Xyris** — `https://api.xyris.site/` — `REJECTED_LOW_VALUE`. OpenAI-compatible API + Claude Code route, exact Opus 5, no-card. Публичная страница противоречит сама себе (`$15 Free Credit` в заголовке и `$8.00` instant credit ниже); оба значения меньше $20. Не предлагать.
- **anymize** — `https://anymize.ai/` — `BLOCKED_MODEL_UNPROVEN`. 14-day full trial, all models/all features, no card; есть OpenAI-compatible API, MCP и Developer Console. Однако публичная документация на момент проверки показывает Anthropic examples до `claude-opus-4.7`, а unauthenticated `GET /api/v1/llm/models` возвращает `401`; exact Opus 5 в trial не доказан. Не выдавать как Opus 5 route, пока модель не подтверждена из authenticated model list или first-party документации.
- **CheapCredits** — `https://cheapcredits.ai/` — `BLOCKED_MODEL_UNPROVEN`. Полный KaroX scan до записи: `files_scanned: 1753`, `match_count: 0`. Signup обещает до **$20 free credits** после email verification/API-key setup, но требует work email (Gmail/Outlook не поддерживаются). Public first-party price table на момент проверки показывает Claude Opus 4.7/Haiku 4.5 и не содержит `claude-opus-5`; exact Opus 5 не доказан. Не предлагать до появления first-party model listing или authenticated `/models` evidence для Opus 5.
- **Accio Work** — `https://www.accio.com/work/app` — `BLOCKED_MODEL_UNPROVEN`. Полный KaroX scan до записи по `Accio|accio.com|Accio Work|work.accio` завершился `files_scanned: 1753`, `match_count: 0`. First-party Accio материалы подтверждают **500 free credits для нового аккаунта без карты** и полноценный Coder/agent workflow с файлами, terminal/commands, browser tools и запуском tests. Однако публичные страницы на момент проверки называют Claude/Claude 4 Opus, а exact `claude-opus-5` не подтверждён; неавторизованный live app показывает только selector `Auto` и требует login для списка моделей. Не предлагать как Opus 5 route до authenticated model-picker/model-ID evidence.
- **Moxt** — `https://moxt.ai/` — `REJECTED_LOW_VALUE`. Полный KaroX scan до записи: `files_scanned: 1753`, `match_count: 0`. First-party pricing подтверждает exact Claude Opus 5, autonomous AI Teammates/cloud sandbox, no-card signup и автоматические **1,000 free credits**. Курс сервиса: `$1 = 100 credits`, то есть стартовый баланс = **$10**, ниже обязательного порога $20. Не предлагать повторно как сильный бесплатный Opus 5 route.
- **KernelFold** — `https://www.kernelfold.com/` — `REJECTED_MODEL_MISSING`. Полный KaroX scan до записи по `KernelFold|kernelfold.com|Oumi|oumi.ai`: `files_scanned: 1753`, `match_count: 0`. No-card free tier и developer/API/Claude-Code-style workflow выглядят сильными, но live first-party model list на момент проверки содержит Claude Fable 5 и Opus 4.8, **не exact Opus 5**.
- **Oumi** — `https://oumi.ai/` — `REJECTED_BYOK`. Тот же валидный KaroX scan: `files_scanned: 1753`, `match_count: 0`. Есть no-card credits до $50, но доступ к frontier providers требует добавить собственные provider API keys; для Claude это BYOK, поэтому не подходит.
- **Calljmp** — `https://calljmp.com/` — `REJECTED_BYOK`. Полный repo-wide KaroX scan до записи завершился `files_scanned: 1753`, совпадений по Calljmp не было. First-party pricing даёт **$25 trial credits без карты** и полноценные agents/workflows/CLI/REST, но premium/frontier models подключаются через provider API keys. Для Claude маршрут BYOK.
- **Ragnerock** — `https://ragnerock.com/` — `BLOCKED_MODEL_UNPROVEN`. Полный repo-wide KaroX scan до записи: `files_scanned: 1753`, совпадений по Ragnerock не было. First-party pricing даёт **20 free credits без карты**; один credit коммерчески стоит $20 и включает большой token/compute/search budget, плюс есть Python SDK/API и custom agents. Но публичная документация не раскрывает exact model ID `claude-opus-5`; не считать Opus 5 route до first-party/authenticated model evidence.
- **Team Assistant** — `https://teamassistant.com/` — `BLOCKED_MODEL_MISSING`. Полный KaroX dedup по `Team Assistant|teamassistant.com|FNTIO`: `files_scanned: 1753`, `match_count: 0`. First-party pricing даёт **25 free credits = $25**, no-card, без expiry; платформа имеет настоящий isolated Linux sandbox, file read/write, Bash/Python/Node, package install и app-building workflow. Однако live first-party Anthropic model list на 2026-08-11 содержит Fable 5, Opus 4.8/4.7, Sonnet 5/4.6 и Haiku 4.5, но **не Claude Opus 5**. Не предлагать до появления exact model.
- **LetzAI** — `https://letz.ai/` — `REJECTED_NO_FREE_CREDITS`. До записи полный KaroX scan по `LetzAI|letz.ai|Letz AI|CodingFleet|codingfleet.com` завершился `files_scanned: 1753`; совпадение было только по уже известному CodingFleet, не LetzAI. First-party pricing содержит exact Claude Opus 5, но текущий Free plan = **0 credits/month**; отдельное first-party объявление сообщает, что бесплатные credits для новых аккаунтов прекращены с 2026-01-15. API keys доступны только на платных планах. Не предлагать.
- **Tonkotsu** — `https://tonkotsu.ai/` — `BLOCKED_STALE_BETA_MODEL_UNPROVEN`. До записи полный KaroX scan по `Tonkotsu|tonkotsu.ai` завершился `files_scanned: 1753`, `match_count: 0`. Это реальный repository coding-agent, ранее бесплатный во время open beta и использующий Claude-family agents, но текущая first-party информация не подтверждает active no-card ≥$20 entitlement и exact `claude-opus-5`; старые материалы указывают преимущественно Sonnet. Не выдавать как текущий Opus 5 route.
- **ToAPIs** — `https://toapis.com/en/pricing` — `REJECTED_LOW_VALUE`. До записи полный KaroX scan по `ToAPIs|toapis.com|To APIs` завершился `files_scanned: 1753`, `match_count: 0`. First-party pricing/model pages содержат exact Claude Opus 5 и no-card signup; публичные страницы противоречат по welcome grant (10 vs 200 credits), но даже верхняя оценка 200 credits намного ниже $20 по собственной Opus 5 credit-price table. Не предлагать как сильный бесплатный route.
- **Supercode / SuperCLI** — `https://supercodeai.vercel.app/pricing` — `REJECTED_PRODUCTION_NO_FREE_OPUS5`. **Новизна была подтверждена до первой записи полным KaroX scan** по `Supercode|supercodeai|supercodeai.vercel.app`: `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. Это реальный open-source SWE/CLI coding agent; first-party changelog/source подтверждают exact `anthropic/claude-opus-5`, но модель относится к Ultra. **Production-проверка пользователем 2026-08-11 после GitHub login опровергла полезность маршрута:** Billing Studio показывает `No active plan`; текущий бесплатный доступ — `Open models`, ~10K requests/month, 16K context и только **$5 monthly credits + deals**. Upgrade cards предлагают Spark Premium `$1/month` с `$10 monthly credits`, Pro `$12/month` international / ₹659 India с `$30 monthly credits`, и Ultra `$100/month` / `$1000/year` с `$150 monthly credits`; на всех платных вариантах отображается `Subscribe`, а отдельного активируемого no-card Ultra trial в production account не показано. Значит бесплатный аккаунт не даёт Ultra/Opus 5 и не проходит порог $20. Не предлагать повторно как бесплатный Opus 5 route, даже если marketing/FAQ или public source упоминают trial.
- **Poyo AI** — `https://poyo.ai/` — `REJECTED_MODEL_COMING_SOON_LOW_VALUE`. До записи полный KaroX scan по `Poyo|poyo.ai|Poyo AI` завершился успешно: `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. First-party model catalog/pricing на 2026-08-11 помечают **Claude Opus 5 как `Coming Soon`**, поэтому exact Opus 5 сейчас вызвать нельзя. Free tier даёт только **20 credits**; по собственной pricing-конверсии сервиса это существенно меньше $20. Не предлагать повторно как текущий бесплатный Opus 5 route.
- **Nous Research / YHack / Nous Portal / Hermes Agent** — `https://nousresearch.github.io/yhack/` / `https://portal.nousresearch.com/` — `TESTING / STRONG_NEW_LEAD`. До записи полный KaroX dedup по `Nous Research|Nous Portal|portal.nousresearch.com|YHack|nousresearch.github.io/yhack|Hermes Agent|hermes-agent` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. Официальная публичная страница YHack предлагает one-code-per-person **$20 Starter** и **$50 Power** credits для Nous Portal/Hermes Agent. Live Nous Portal содержит exact Claude Opus 5/Opus 5 Fast и реальный API/agent workflow. **Не PASS:** видимые `26 left / 10 left` на YHack-странице считаются client-side/localStorage и не доказывают глобальный остаток; назначение страницы связано с YHack и общая eligibility не доказана; текущая линейка Portal-планов не полностью совпадает со старой инструкцией `$50 Power`; реальный Stripe checkout с применённым кодом, итогом `$0` и без карты ещё не наблюдался. Проверять только обычным Claim My Code flow, один код на человека, без извлечения/перебора кодов из frontend.
- **AdaL / Sylph AI** — `https://adalagent.ai/` / `https://adal.sylph.ai/sign-up` — `TESTING / STRONG_NEW_LEAD`. Точный dedup выполнен через обновлённый journaled KaroX plan по `\bAdaL\b|Sylph AI|sylph.ai|docs.sylph.ai|adal.sylph.ai`: `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. Это полноценный CLI/Agentic IDE/headless coding agent с file edit, Bash/commands, tests, worktrees и browser-use. First-party changelog/models подтверждают exact `anthropic-claude-opus-5`; BYOAK необязателен — без собственного ключа запросы идут через AdaL proxy и тратят AdaL credits. Текущий сайт показывает **Pro $20/month** и **7-day free trial**, а docs уточняют, что eligible new users увидят trial banner после sign-in. **Не PASS:** размер credit pool текущего 7-дневного trial публично не раскрыт; eligibility условная; отсутствие карты при активации trial после login не доказано; initial signup page карты не просит, но production Dashboard ещё не проверен. Старые `$5` signup promos не считать доказательством текущего trial-баланса.
- **RelayGPU / RelayCode / OpenGPU Network** — `https://relaygpu.com/integrations/vscode` / `https://dashboard.relaygpu.com/models` — `TESTING / VERY_STRONG_NEW_LEAD`. До записи journaled KaroX dedup по `RelayCode|RelayGPU|relaygpu.com|OpenGPU Network|opengpu.network` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. RelayGPU официально operated by **OpenGPU Management S.a r.l. (Luxembourg)**; proprietary frontier models идут через Direct Mode/trusted upstream hosting providers, а decentralized OpenGPU Community Tier используется для open-weight models. Live production Model Marketplace повторно проверен 2026-08-11 через KaroX Browser и прямо показывает exact **`claude-opus-5`** по `$5 in / $25 out per 1M` и `claude-sonnet-5`. Текущая first-party RelayCode integration page + FAQ прямо говорят: **new users get $100 worth of promo credits automatically when they sign up, no credit card required**; credits cover both Claude Code routing and Master Key usage, а live balance должен показываться в RelayCode sidebar. RelayCode маршрутизирует Claude Code через RelayGPU; сам RelayGPU также имеет unified OpenAI-compatible API. Дополнительная production-проверка `/credits` на уже существующей KaroX browser session `farm` открылась успешно, но `Active Promos` там пуст — это не опровержение signup offer, потому что FAQ ограничивает его **new users**. **Не PASS:** новый eligible production account ещё не создан, поэтому фактический `$100` balance, expiry/limitations promo и успешный `claude-opus-5` debit из promo-credit pool не наблюдались. Проверить первым через один обычный новый signup; не покупать/top-up credits и не создавать повторные аккаунты для abuse.
- **EAGM API Gateway** — `https://api.eagmgroup.com/pricing` — `BLOCKED_MODEL_MISSING`. До записи полный KaroX dedup по `EAGM|EAGM Gateway|api.eagmgroup.com|eagmgroup` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. First-party pricing/FAQ обещают **24-hour unlimited free trial**, без карты, с API для coding clients; однако текущие first-party docs/register перечисляют Claude до Opus 4.1/Sonnet 4.5, а signup прямо рекламирует trial с Claude Sonnet 4. Exact `claude-opus-5` в live model list/trial не доказан. Не предлагать как Opus 5 route до появления точного model ID.
- **Omnifact** — `https://omnifact.ai/pricing` — `REJECTED_LOW_VALUE`. До записи полный KaroX dedup по `Omnifact|omnifact.ai` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. First-party release notes подтверждают добавление Claude Opus 5 и OpenAI-compatible API Gateway; production signup показывает **10-day free trial**, а pricing указывает no-card trial. Однако опубликованный allowance для premium models — только **€5 usage credits/user/month**, существенно ниже обязательного порога $20. Не предлагать как сильный бесплатный Opus 5 route.
- **PatewayAI** — `https://pateway.ai/` — `REJECTED_LOW_VALUE`. До записи общий KaroX dedup по `RunAPI|runapi.ai|FennoAI|fenno.ai|PatewayAI|pateway.ai|Unity2.ai|Shengsuanyun|AIGoCode` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. Current partner/signup promo даёт лишь **$3 trial credit**, ниже порога $20. Не предлагать.
- **FennoAI** — `https://api.fenno.ai/coding-plan` — `REJECTED_NOT_FREE`. Тот же pre-record KaroX dedup: `1753/0`. Production pricing через KaroX Browser показывает 7-day trial с API quota около **$50/week**, но activation стоит **$1.99**. Интеграции Claude Code/OpenCode/OpenAI/Anthropic-compatible есть, однако это не бесплатный route. Не предлагать как free Opus 5.
- **RunAPI** — `https://runapi.ai/pricing` — `BLOCKED_FREE_AMOUNT_TOO_SMALL_OR_UNPROVEN`. Тот же pre-record KaroX dedup: `1753/0`. First-party catalog имеет Anthropic models и developer surfaces (CLI/MCP/API/Claude Code); site обещает free first calls/small trial credit without requiring card to register, но сумма автоматического кредита >=$20 не опубликована и не доказана. Не предлагать как сильный route до production balance evidence.
- **Unity2.ai** — `https://unity2.ai/models` — `REJECTED_MODEL_MISSING`. Тот же pre-record KaroX dedup: `1753/0`. Live production model catalog через KaroX Browser 2026-08-11 содержит только 5 моделей DeepSeek/Moonshot (Kimi); Claude/Opus 5 отсутствует. Не предлагать.
- **Shengsuanyun / 胜算云** — `https://www.shengsuanyun.com/` — `REJECTED_LOW_VALUE`. Тот же pre-record KaroX dedup: `1753/0`. Платформа имеет Claude/OpenAI-compatible gateway и интеграции Claude Code/OpenCode/Cline; current promotion для нового пользователя порядка **¥10**, а не $20+. Не предлагать как сильный бесплатный Opus 5 route.
- **AIGoCode** — `https://aigocode.app/` — `REJECTED_NO_FREE_ENTITLEMENT`. Тот же pre-record KaroX dedup: `1753/0`. Live production homepage показывает только платные 4-week plans и pay-as-you-go purchase (`¥50 → $50` balance); бесплатного starter/trial entitlement не видно, partner promo относится к бонусу/скидке после пополнения. Не предлагать как free route.
- **Aevon** — `https://aevon.sh/` — `REJECTED_MODEL_MISSING`. До записи полный KaroX scan по `Aevon|aevon.sh|AEVON` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. Live first-party site действительно обещает **$100 free credits instantly on signup**, no card, OpenAI-compatible API и Claude Code/Cursor/Continue; однако live `/models` Anthropic catalog содержит Claude Sonnet 4.5, Haiku 4.5, Opus 4 и Sonnet 3.5 — exact Opus 5 отсутствует. Не предлагать до появления `claude-opus-5` в production catalog.
- **Kwoat** — `https://www.kwoat.ai/` — `REJECTED_FREE_TIER_NO_OPUS`. До записи общий KaroX dedup по `Kwoat|kwoat.ai|Axiom Studio|axiomstudio.dev|DarkWave Studios` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. Public beta даёт 50 free credits/no card и настоящий autonomous coding workflow, но Claude Sonnet/Opus относятся к Pro; бесплатный tier не даёт доказанного Opus 5. Не предлагать как free route.
- **Axiom Studio** — `https://axiomstudio.dev/` — `REJECTED_MODEL_MISSING_WHITELIST`. Тот же pre-record KaroX dedup: `1753/0`. Реальный multi-agent IDE с filesystem/terminal и 50 free credits, но current model roster указывает Claude Opus 4.8/Fable 5/Sonnet 4.6, не exact Opus 5; beta access также зависит от whitelist. Не предлагать.
- **ClaudeAPI / apito.ai** — `https://apito.ai/` — `REJECTED_LOW_VALUE_OLD_MODEL`. До записи общий KaroX dedup по `apito.ai|claudeapi.com|console.apito.ai|A6API|a6api|Code0|code0.ai` завершился `files_scanned: 1753`; единственное совпадение было по другому домену `getclaudeapi.com`, не этой сущности. Signup promotion порядка **$0.5–1.5**, а публичный Claude catalog отстаёт до Opus 4.x. Ни модель, ни сумма не проходят фильтр.
- **Code0.ai** — `https://code0.ai/` — `REJECTED_LOW_VALUE`. Тот же pre-record KaroX dedup не нашёл exact Code0 entity. First-party docs дают новым пользователям **$5 consumption credit** и OpenAI-compatible access; ниже обязательного порога $20, exact free Opus 5 не доказан. Не предлагать.
- **A6API** — `REJECTED_NO_FREE_ENTITLEMENT_RISKY_MARKETPLACE`. Тот же pre-record KaroX dedup не нашёл exact A6API entity. Current model marketplace может содержать `claude-opus-5`, но автоматический no-card signup credit >=$20 не доказан; merchant/account-pool structure не соответствует требованию чистого first-party hosted route. Не предлагать.
- **NexusAI / Karesupport** — `https://www.karesupport.tech/` — `REJECTED_MODEL_MISSING`. До записи общий KaroX scan по `NexusAI|karesupport.tech|SrijonX|srijonx.com|Vireana|vireana.com|No9Ai|no9ai.in` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. First-party page даёт **100 free credits instantly, no card**, OpenAI-compatible API, но current Anthropic model list заканчивается на `claude-opus-4.6`. Exact Opus 5 отсутствует.
- **SrijonX** — `https://www.srijonx.com/` — `REJECTED_MODEL_MISSING_FREE_TIER_RESTRICTED`. Тот же pre-record KaroX dedup: `1753/0`. 100 free credits/no card и agent/code workspace существуют, но public catalog/FAQ перечисляют Claude Opus 4.8; free plan фактически ограничен базовыми моделями. Exact free Opus 5 отсутствует.
- **Vireana** — `https://vireana.com/` — `REJECTED_MODEL_MISSING_FREE_TIER_RESTRICTED`. Тот же pre-record KaroX dedup: `1753/0`. Есть 100 credits/week, no card и Developer API, но current frontier roster показывает Claude Opus 4.8; free plan доступен только к 18 Free+Standard models. Exact Opus 5 отсутствует.
- **No9Ai** — `https://no9ai.in/` — `REJECTED_MODEL_MISSING`. Тот же pre-record KaroX dedup: `1753/0`. Полноценный AI project builder/Agent Mode с 100 free credits/no card, shell/git/web-search, но current model roster содержит Claude Opus 4.5/Sonnet 4.5, не Opus 5. Не предлагать.
- **Playcode** — `https://playcode.io/` — `BLOCKED_TRIAL_VALUE_UNPROVEN`. До проверки общий KaroX scan по `Playcode|playcode.io|MultipleChat|multiple.chat|NinjaChat|ninjachat.ai|AiFuse|aifuse.app|Clawdy|clawdy.app` завершился `files_scanned: 1753`; совпадения были по уже известным AiFuse/MultipleChat/NinjaChat, но Playcode до этой записи отсутствовал. First-party changelog 2026-07-24 переводит Quality-tier на exact Claude Opus 5; Free signup no-card и стартовые AI credits заявлены. Однако размер trial credit pool публично не раскрыт, а help/pricing surfaces расходятся по доступности AI credits на Free. Не предлагать как >=$20 route без production balance evidence.
- **Clawdy** — `https://clawdy.app/` — `REJECTED_MODEL_MISSING`. Тот же pre-record KaroX scan не содержал Clawdy. Signup no-card и 5,000 free credits подтверждаются current first-party homepage, но current model picker показывает Claude Opus 4.6, GPT 5.4, Gemini 3.1 и Kimi K2.5; exact Opus 5 отсутствует. Не предлагать.
- **Vorflux** — `https://vorflux.com/` — `REJECTED_MODEL_MISSING`. До записи полный KaroX scan по `Vorflux|vorflux` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. Current signup/pricing promises strong free credits (up to $200 with work email / $70 personal), and product is an autonomous coding-agent platform, but current Anthropic pricing/model table lists Fable 5 and Opus 4.8/4.7/4.6 without exact Opus 5. Не предлагать до появления `claude-opus-5` в production catalog.
- **Bonsai / trybons.ai** — `https://trybons.ai/` — `BLOCKED_MODEL_ID_HIDDEN`. До записи общий KaroX scan по `Bonsai|trybons.ai|trybonsai|Freebuff|freebuff.com|Overbrilliant|overbrilliant.com` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. Bonsai предлагает free/no-card terminal coding workflow и frontier coding models, но intentionally hides concrete provider/model identities. Exact Claude Opus 5 невозможно first-party подтвердить — не выдавать как Opus 5 route.
- **Freebuff** — `REJECTED_MODEL_MISSING`. Тот же valid pre-record KaroX scan: `1753/0`. Free inference focuses on open-weight/free models; exact hosted Claude Opus 5 entitlement >=$20 не найден.
- **Overbrilliant** — `REJECTED_FREE_TIER_MODEL_MISSING`. Тот же valid pre-record KaroX scan: `1753/0`. Public free-model access exists, but frontier/premium access is not an automatic no-card Opus 5 credit pool. Не предлагать.
- **DataChi / WorkChi Router** — `https://benchmarks.datachi.ai/` — `REJECTED_LOW_VALUE_CATALOG_AMBIGUOUS`. До записи exact KaroX scan по `DataChi|datachi.ai|benchmarks.datachi.ai` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. Live site has a real OpenAI-compatible API and benchmark catalog containing exact Claude Opus 5. However the separate gateway has only 50+ models while the benchmark catalog contains 579+, so Opus 5 gateway entitlement is not independently proven. More importantly live `/llm-api/pricing` says Free = **10,000 tokens/month**, all 50+ models, no card — far below $20 at Opus 5 pricing. Homepage marketing saying `10K free requests/month` conflicts with the live pricing surface; use the lower concrete token allowance. Не предлагать.
- **GitHub Models** — `DEAD_RETIRED`. До проверки method-level KaroX scan по `GitHub Models|models.github.ai|github model catalog` завершился `files_scanned: 1753`, `match_count: 0`. GitHub Models stopped accepting new customers 2026-06-16 and was fully retired 2026-07-30, including catalog/playground/inference API/BYOK. Exact Opus 5 is available in GitHub Copilot only on higher paid tiers, not as a free GitHub Models API route. Не предлагать.
- **Google Cloud $300 Free Trial → Claude Opus 5** — `REJECTED_PARTNER_MODEL_CREDITS_EXCLUDED`. Method-level KaroX scan over cloud/student-credit terms completed with `files_scanned: 1753`; no prior exact route match was found. Card-backed trials are now allowed by the user, so the old card objection is obsolete. However, current Google first-party Free Program documentation explicitly says the $300 Free Trial credit **cannot be used for generative-AI partner models offered as managed APIs (MaaS)**. Claude Opus 5 is a managed Anthropic partner model on Google Cloud Agent Platform/Model Garden, so the ordinary $300 trial cannot fund it. Do not present this route again unless Google changes that partner-model exclusion.
- **Ace Data Cloud / AceDataCloud** — `https://acedata.cloud/` / `https://platform.acedata.cloud/` — `BLOCKED_FREE_AMOUNT_UNPROVEN`. До записи exact KaroX scan по `AceDataCloud|acedata.cloud|platform.acedata.cloud|Ace Data Cloud` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. Current platform/docs confirm unified AI gateway, OpenAI-compatible APIs, first-application free quotas on many services, and third-party current announcement surfaces exact Claude Opus 5/Claude Code. But first-party docs do not publish the Claude-specific signup quota; public platform economics show 100 credits selling around $15, so >=$20 automatic no-card entitlement is not proven. Не выдавать как strong route until production Claude service balance and model ID are observed.
- **code2ai / Multi-Model Gateway** — `https://flex.code2ai.codes/` — `REJECTED_MANUAL_ACTIVATION`. До записи полный KaroX scan по `code2ai|code2ai.codes|flex.code2ai.codes|Multi-Model Gateway` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. First-party page confirms exact `claude-opus-5`, official Claude Code configuration and a 3-day trial, but trial activation requires adding customer service on WeChat, sending the account ID and waiting for staff to activate it manually. This violates the instant/no-manual-grant criterion. Не предлагать как qualifying route.
- **Grapple PR** — `https://www.grapple-pr.com/` — `REJECTED_MODEL_MISSING`. До записи exact KaroX scan over `Grapple PR|grapple-pr.com` completed within a combined query with `files_scanned: 1753`, `match_count: 0`. First-party beta is free/no-card and provides a real GitHub code-review agent, but current published model agents are Claude Opus 4.6 rather than exact Opus 5. Не предлагать.
- **Azure for Students → Claude Opus 5 in Microsoft Foundry** — `REJECTED_CREDIT_SUBSCRIPTION_UNSUPPORTED`. До записи exact method-level KaroX scan over `Azure for Students|Microsoft Foundry.*Opus 5|Claude.*Azure for Students` завершился `files_scanned: 1753`, `match_count: 0`. Azure for Students may provide $100 without a card to eligible students, but Microsoft first-party documentation states Claude partner models in Foundry do not support student/free-trial/startup credit-based subscriptions and require eligible pay-as-you-go billing. Thus the student credits cannot be used as the requested free Opus 5 route.
- **SubRouter** — `https://www.subrouter.ai/` — `BLOCKED_AMOUNT_MODEL_UNPROVEN`. До записи exact KaroX scan in a combined query over `SubRouter|subrouter.ai|APIKEY.FUN|apikey.fun|TeamoRouter|teamorouter.com` завершился `files_scanned: 1753`, `match_count: 0`. First-party site advertises no-card signup, free registration credits and Claude Code/Cursor/OpenClaw support, but does not publish a >=$20 automatic credit amount and public Claude examples do not establish exact Opus 5 production entitlement. Не выдавать until live balance and model ID are proven.
- **APIKEY.FUN** — `https://apikey.fun/` — `REJECTED_NO_FREE_ENTITLEMENT`. Тот же valid pre-record KaroX scan: `1753/0`. Live KaroX Browser shows a Claude Code-compatible gateway with all Claude models advertised, but the production pricing is pay-as-you-go/top-up; the available partner promotion is a top-up discount rather than an automatic >=$20 free credit grant. Не предлагать as free route.
- **TeamoRouter** — `https://teamorouter.com/` — `REJECTED_NO_FREE_ENTITLEMENT`. Тот же valid pre-record KaroX scan: `1753/0`. First-party site supports exact `claude-opus-5`, Anthropic/OpenAI-compatible APIs and Claude Code, but usage starts by buying credits; current promotions are purchase/top-up discounts, not automatic no-card >=$20 credits. Не предлагать.
- **devinone** — `https://www.devinone.com/pricing` — `BLOCKED_AMOUNT_UNPROVEN`. До записи полный KaroX dedup по `devinone|devinone.com` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. Live first-party pricing через KaroX Browser подтверждает exact `claude-opus-5` через AWS Bedrock, unified gateway и автоматические **free credits immediately after signup, no credit card**, причём бесплатный баланс можно тратить на любую модель без отдельного free-tier model restriction. Однако production pricing/DOM не публикует точный размер стартового баланса. До доказанного balance >=$20 не выдавать как qualifying route.
- **OpenStarry** — `https://api.openstarry.com/` — `REJECTED_FREE_QUOTA_EXCLUDES_OPUS5`. До записи полный KaroX dedup по `OpenStarry|openstarry.com|api.openstarry.com` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. Homepage/model list contains exact `claude-opus-5` and advertises **200 free model calls instantly**, but first-party usage/legal rules state the free 200-call Coding Plan is limited to domestic registered models such as MiniMax/DeepSeek/Kimi; Claude/other overseas models are available only via prepaid Token Plan. Therefore the free quota cannot be spent on Opus 5. Не предлагать.
- **Octopus Review** — `https://octopus-review.ai/` — `BLOCKED_AMOUNT_UNPROVEN`. До записи полный KaroX dedup по `Octopus Review|octopus-review.ai|octopusreview` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. Current first-party pages confirm exact Claude Opus 5 in managed cloud, CLI/code-review workflow and no-card signup with promotional cloud credits. Однако официальный сайт не публикует размер автоматического credit grant; >=$20 не доказано. Не выдавать как strong route until production balance evidence exists.
- **BeforeTomorrow** — `https://beforetomorrow.io/` — `REJECTED_MODEL_MISSING`. До записи полный KaroX dedup по `BeforeTomorrow|beforetomorrow.io|Before Tomorrow` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. First-party platform offers a 30-day no-card trial, 200 credits, API/webhooks and autonomous expert agents, but current published agent stack still specifies Claude Opus 4.6/Sonnet 4.6 rather than exact Opus 5. Не предлагать до production model update.
- **TokenTable** — `https://tokentable.asia/` — `PASS_FREE_RECURRING_OPUS5_API` (public-production verified, account-level request pending user signup). До записи полный KaroX dedup по `TokenTable|tokentable.asia|Token Table` завершился `files_scanned: 1753`, `files_skipped: 247`, `match_count: 0`. Live first-party production page 2026-08-11 lists exact selectable `claude-opus-5` (1M context) as a Main model and explicitly supports one API key with Claude Code, Cursor, Cline, Aider, OpenClaw and other OpenAI-compatible agents at base URL `https://tokentable.asia/v1`. Starter is `$0`, no card/no contract. Current live KaroX Browser DOM shows a new-user kickoff of **up to 20 flagship Main calls in the first 72 hours**, then a recurring free allowance shown in the pricing block as **5 calls/day: 2 Main + 3 Side**; the embedded live Advisor separately describes 5 main-model messages/day, so use the more conservative published pricing allocation of 2 Main/day after kickoff until account usage proves otherwise. Exact Opus 5 is marked `selectable`, therefore Main quota can target it according to current public product rules. Live signup page is active with name/email/password or Google and no payment fields. Security/terms identify operators TAIZE HONGKANG TRADING CO., LIMITED (Hong Kong BR 77386279) and ALCHEMY DIGITAL ASSETS CO., LTD. (Taiwan UBN 50751049); security page says upstream provider keys remain server-side, Western providers including Anthropic use Singapore egress, and the service claims legitimate channels rather than shared/cracked accounts. This is a **real recurring free Opus 5 API route**, not merely the separate `$5 signup gift`. Caveat: a real authenticated `claude-opus-5` API request was not executed because that requires creating/signing into a user account; do not claim account-level debit evidence until user signs up and tests one call.

## Полный повторный аудит — 2026-08-15

### Новая схема статусов

С 2026-08-15 этот tracker использует пользовательскую схему статусов: `WORKS`, `CHANGED`, `EXPIRED`, `DEAD`, `NEEDS ACTION`, `WAITING`, `NEW OPPORTUNITY`. Старые `PASS/TESTING/REJECTED/BLOCKED` оставлены выше как исторические verdicts и не удаляются. В текущем аудите статус описывает состояние маршрута **сейчас**, а отдельная пометка `TARGET GATE` говорит, проходит ли он требования exact Opus 5 / Fable 5 / GLM-5.3.

Также снято старое процессное ограничение «не считать способы с заявкой/ожиданием»: текущая задача специально включает startup / OSS / research / early-access programs. Такие способы теперь сохраняются как `NEEDS ACTION` или `WAITING`, а не выбрасываются.

### Канонические model anchors на 2026-08-15

- **Claude Opus 5:** exact API ID `claude-opus-5`, 1M context, 128K max output, thinking on by default, effort `low / medium / high / xhigh / max`, стандартная цена Anthropic `$5 / $25` за 1M input/output. Source: https://platform.claude.com/docs/en/about-claude/models/overview и https://claude.com/pricing
- **Claude Fable 5:** exact API ID `claude-fable-5`, 1M context, стандартная цена `$10 / $50`. На Claude Free модели нет. На Max / Team Premium она входит в план в пределах до 50% weekly limit; прежнее временное окно закончилось 2026-07-19. Source: https://support.claude.com/en/articles/15424964-claude-fable-5-on-your-plan
- **GLM-5.3:** публичного API/model ID на 2026-08-15 ещё нет. Z.ai сообщил о модели 2026-08-14; публичный релиз заявлен примерно через две недели после security assessment, initial access — select launch partners. Sensitive cyber functions планируются через trusted access. Source: Reuters 2026-08-14 https://www.reuters.com/technology/chinas-zai-says-new-model-nears-anthropics-mythos-5-cyber-defence-tests-2026-08-14/ ; current public Z.ai docs still stop at earlier GLM releases: https://docs.z.ai/release-notes/new-released

### Full sweep старых записей

Все старые строки сохранены. Там, где нельзя честно подтвердить account-level balance/model picker без входа, статус не повышался до `WORKS`: такие записи оставлены `NEEDS ACTION`.

#### `WORKS` — live, но только перечисленные ниже target-qualified

- **TokenTable** — `WORKS` — exact Opus 5, recurring free API quota, OpenAI-compatible coding clients. `TARGET GATE: PASS Opus 5`; account-level debit всё ещё стоит проверить одним обычным signup/request.
- **KernelFold** — `CHANGED / WORKS_FABLE5 / OPUS5_NEEDS_PRODUCTION_CHECK` — current first-party homepage advertises **Claude Fable 5 and Claude Opus 5** with Free = **1.5M tokens/month**, no card, all models, and an OpenAI-compatible API. However current developer docs expose exact `claude-fable-5` but still list Claude Opus **4.8** rather than an exact `claude-opus-5` API ID. The docs explicitly allow pinning `claude-fable-5`; therefore `TARGET GATE: PASS Fable 5 / NEEDS_PRODUCTION_CHECK Opus 5`. Do not promote KernelFold as exact Opus 5 until live `GET /v1/models` or an authenticated request proves that ID. Sources checked 2026-08-18: https://www.kernelfold.com/ ; https://www.kernelfold.com/docs .
- **Team Assistant** — `WORKS` — 25 free credits = $25, no card, credits no expiry, Fable 5 доступен; exact Opus 5 не найден. `TARGET GATE: PASS Fable 5 / FAIL Opus 5`.
- **Vorflux** — `WORKS` — autonomous coding-agent platform; текущая frontier-линейка включает Fable 5, а signup promotion остаётся существенно выше большинства мелких trial routes (personal/work-email tiers). `TARGET GATE: PASS Fable 5 / FAIL Opus 5`.
- **Kiro, Kilo, JetBrains AI, CometAPI, AIMLAPI, EvoLink, Factory, Qodo, NinjaChat, Codebuff, Windsurf, Devin, Cursor, Zed, Warp, Qoder, TRAE, Puter, Replit, Clarifai, Augment, Ona, AWS/Bedrock, GitHub Copilot, Cloudflare Unified AI, Poe, Keyplex, TokenLab, OpenHands, WorldRouter, Anthropic Console, Tembo, MonoRouter, Zo Computer, AiFuse, Medham, MitDotKey, Cosine Genie, Blackbox, Sweep, PatewayAI, FennoAI, AIGoCode, APIKEY.FUN, TeamoRouter, LetzAI, ToAPIs, Omnifact, Moxt, XPersona, Modelis, CCVibe, DataChi, A6API, Code0, Freebuff, Overbrilliant** — `WORKS` as services/products, but their historical free route still fails at least one requested target gate: exact target model, useful free volume, no-BYOK, no-payment, or coding/API suitability. Они не считаются новыми находками.

#### `CHANGED`

- **Kiro** — `CHANGED / REJECTED_FOR_MINOR_USER`: Opus 5 теперь есть в IDE/CLI/Web и поддерживает effort `low…max`; `$20` first-paid-upgrade signup credit + prorated first billing period технически могут покрыть первый неполный период Pro, карта обязательна. Однако Kiro usage is governed by the AWS Customer Agreement, где Customer представляет, что он lawfully able to enter contracts (example: **not a minor**). Поэтому этот consumer route нельзя считать универсальным обычным способом для несовершеннолетнего пользователя и для текущего пользователя его не предлагать. Sources checked 2026-08-17: https://kiro.dev/pricing/ ; Kiro billing/signup-bonus docs ; https://aws.amazon.com/agreement/ .
- **CometAPI** — `CHANGED`: public catalog теперь содержит exact Opus 5/Fable 5 и сайт снова рекламирует free trial credits/no-card signup, но старый реальный аккаунт показывал `$0`; размер текущего automatic grant не доказан. `NEEDS ACTION` на новом eligible account.
- **AIMLAPI** — `CHANGED`: exact `anthropic/claude-opus-5` и signup free credits теперь явно опубликованы; automatic amount не указан. Source: https://aimlapi.com/models/claude-opus-5 .
- **EvoLink** — `CHANGED`: текущий LLM catalog содержит Opus 5 и Fable 5, homepage заявляет free credits/no credit card; старый фактический баланс был слишком маленьким, новый размер не доказан. Source: https://evolink.ai/
- **Vibany** — `CHANGED`: вместо старого `0 free credits / image-video only` публичная версия теперь показывает free credit allowance и target Claude models. Однако это всё ещё не доказанный OpenCode/Claude-Code/API coding route, поэтому не повышать до target-qualified.
- **Cursor** — `CHANGED`: старый Pro trial больше нельзя считать стабильным free route; current free tier остаётся, но premium Opus 5 entitlement не доказан.
- **JetBrains AI** — `CHANGED / GENERAL-USER OPUS5 CANDIDATE` (re-check 2026-08-17): старый reject по отсутствию Opus 5 устарел. Current JetBrains docs подтверждают **30-day AI Pro trial** для новых пользователей через обычный `Start Free Trial`; individual trial currently includes **10 AI Credits ($10-equivalent)**, organization trial 20 credits. Current JetBrains Marketplace release notes for Junie explicitly добавили **Support for Anthropic Opus 5**. Trial может требовать linking payment card, что допустимо по текущему пользовательскому фильтру. Это mainstream hosted JetBrains AI, не BYOK/OSS/student/startup/invite. Не повышать до `WORKS`, пока в production Trial account не подтверждено, что Opus 5 виден в model selector. Sources: https://www.jetbrains.com/help/ai-assistant/licensing-and-subscriptions.html ; https://www.jetbrains.com/help/ai-assistant/jetbrains-ai-subscription.html ; https://plugins.jetbrains.com/plugin/26104-junie-the-ai-coding-agent-by-jetbrains
- **Claude Fable subscription promo** — `CHANGED/EXPIRED PROMO`: временное включение Fable 5 на paid plans закончилось 2026-07-19. С 2026-07-20 Max/Team Premium имеют Fable 5 как часть plan limits, Pro/Team Standard — usage credits. Source: https://support.claude.com/en/articles/15424964-claude-fable-5-on-your-plan
- **ZCode / GLM-5.2 launch offer** — `CHANGED/EXPIRED PROMO`: старый 5-day free trial/1.5x quota campaign был привязан к периоду до 2026-07-31; current public Coding Plan уже показывает paid Lite/Pro/Max. Source: https://zcode.z.ai/en/docs/welcome and https://z.ai/subscribe

- **AWS Activate** — `CHANGED / HIGH VALUE`: это **не NEW** (до аудита уже был в `OUTREACH_GMAIL_TRACKER.md`), но старое описание «generic cloud application route» теперь существенно устарело. Current AWS first-party pages explicitly confirm that Activate Credits can pay for **third-party Anthropic models on Amazon Bedrock**, and AWS separately launched **Claude Opus 5 on Bedrock** on 2026-07-24. Current Activate tiers advertise Founders starting at **$1,000** with possible increase up to **$5,000**, Portfolio up to **$200,000**, plus additional invite-only AI-startup credits; AWS Startups also explicitly documents using **Claude Code on Bedrock with AWS Activate credits**. `TARGET GATE: PASS exact Opus 5 via Bedrock if approved and model access is enabled`. This is an application/business route, not an instant no-card consumer trial: current eligibility requires a startup, functioning company website and a Paid Tier AWS account; Portfolio needs an Activate Provider Org ID. Sources: https://aws.amazon.com/startups/credits/ ; https://aws.amazon.com/blogs/startups/aws-activate-credits-now-accepted-for-third-party-models-on-amazon-bedrock/ ; https://aws.amazon.com/about-aws/whats-new/2026/07/claude-opus-5-aws/ ; https://aws.amazon.com/startups/start-building
- **Kiro Startup Credits** — `CHANGED / NOT FOR EGOR`: Kiro relaunched a one-year Pro+ startup-credit program and Opus 5 is now a premium Kiro model, but the 2026 terms require **18+**, an AWS Account/startup corporate-domain email and explicitly **exclude Poland**. Therefore this specific startup promotion is not usable by Егор and must not be suggested as a Polish workaround. Sources: https://kiro.dev/startups/terms/ ; https://kiro.dev/startups/ ; https://kiro.dev/pricing/

#### `EXPIRED`

- **Old Fable 5 temporary weekly-limit window** — `EXPIRED` 2026-07-19.
- **Old ZCode x GLM-5.2 limited campaign** — `EXPIRED` 2026-07-31 unless a specific account retained a separate entitlement.
- **Amp old new-user/free frontier allowance** — `EXPIRED` for new users; do not reuse the historical free claim.
- **Old Claude Pro / Fable one-time promo claims** — `EXPIRED / eligibility-specific`; not a general new-user route now.

#### `DEAD`

- **FSZK API** — `DEAD` — DNS/domain route was dead in prior production check; no evidence of revival.
- **GitHub Models inference product** — `DEAD` — retired for new customers 2026-06-16 and fully retired 2026-07-30; Copilot is a separate product.
- **Hidden `$350` Google-doc referral lead** — `DEAD` as a trustworthy route: no stable first-party entitlement evidence; retain historical note only.

#### `WAITING`

- **Anthropic External Researcher Access** — `WAITING` — already present in `OUTREACH_GMAIL_TRACKER.md`; existing application route, do not send a duplicate. Current official program offers approved safety/alignment researchers API credits, but approval for KaroX is not recorded.
- **UnifyLLM manual trial grant** — `WAITING` if prior application remains open; do not count until granted.
- **GitHub Education / student entitlement** — `WAITING` where verification was submitted; no duplicate application.
- **Poyo AI Opus 5** — `WAITING` — public catalog previously marked Opus 5 `Coming Soon`; free allowance was low even if model arrives.

#### `NEEDS ACTION` — existing candidates that still require authenticated/manual evidence

- **RelayGPU / RelayCode** — `NEEDS ACTION / TOP PRIORITY`: first-party page still says **$100 promo credits automatically for new users, no credit card**, and live model pricing lists exact `claude-opus-5` at `$5/$25`; route works with Claude Code and Master Key/OpenAI-compatible API. Existing old account had no promo, which does not disprove a new-user offer. Next action: one normal eligible signup and one tiny Opus 5 debit. Sources: https://relaygpu.com/integrations/vscode and https://relaygpu.com/pricing
- **Aura** — `NEEDS ACTION`: source/model mapping still supports exact Opus 5/Fable 5 and signup-credit mechanics, but production grant still needs account evidence. Terms are 18+, so Егор cannot register personally; do not create a fake-age account.
- **Nous Portal / YHack / Hermes Agent** — `NEEDS ACTION`: exact Opus 5 exists; official public promo codes historically offered $20/$50, but remaining eligibility/current checkout outcome still needs ordinary claim-flow verification. No code scraping/bruteforce.
- **AdaL / Sylph AI** — `NEEDS ACTION`: exact Opus 5 supported, real CLI/headless coding workflow, 7-day trial advertised; trial credit amount/card step still not public.
- **LLM Gateway, OpenCode Zen, CrossModel, VM0 Zero, anymize, CheapCredits, Accio Work, Ragnerock, RunAPI, Playcode, AceDataCloud, SubRouter, devinone, Octopus Review** — `NEEDS ACTION`: each still depends on authenticated balance/model-picker evidence or a public credit amount that is not currently sufficient to claim `WORKS` under the target gate.
- **Henry, Kelly AI** — `NEEDS ACTION`: historical generous claims existed but registration/OAuth was broken; only revive if production signup starts working and exact target model is observed.

### NEW — first found in this 2026-08-15 audit

Перед записью каждой сущности выполнен repo-wide KaroX dedup. Для `Claude for Open Source / Project Glasswing`, `Z.ai Startups Program`, `GLM-5.3 / Open Source Shield`, `Atoms / atoms.dev`, `Standard Compute / standardcompute.com` успешные scans прошли с `files_scanned: 1753`; exact сущности до этой волны не были записаны в tracker/history. Cocobox, Deepest, PiAPI, OpenCodex и другие surfaced names были исключены как уже известные через KaroX.

#### 1. Claude for Open Source — `NEW OPPORTUNITY` — `NEW`

- **Service/domain:** Anthropic / `claude.com`, `anthropic.com`
- **Model:** complimentary **Claude Max 20x**; current Max has access to current Claude models/Claude Code. Opus 5 is a current Opus model; Fable 5 is included on Max within its plan rules.
- **What they give:** **6 months of Claude Max 20x at $0** if approved.
- **Type:** subscription / Claude Code / OSS program.
- **Cost/free volume:** $0 base subscription for six consecutive months; Max 20x plan limits apply rather than a fixed token credit pool. Overage usage, if enabled, is separate.
- **Term:** 6 months after activation; activation link expires after 90 days.
- **Limits/eligibility:** rolling review, up to 10,000 approved recipients unless increased. Main objective thresholds include 500 dependent repos, 100 dependent packages, 200k monthly package downloads, 100 merged PRs in repos you do not own in 12 months, 20 external contributors, or OpenSSF criticality >=0.4. Discretionary ecosystem-impact track exists.
- **Card:** program itself is complimentary; terms do not require purchase to receive the base benefit. Existing paid plan billing may resume afterward. Overage can be chargeable.
- **KYC/business:** natural person; GitHub OAuth; GitHub account must be >=2 years old; recent public OSS activity required. Business account not required.
- **Guardian/age:** **18+ mandatory** (or age of majority if higher). Benefit is personal, non-transferable, and account sharing is prohibited. **Егор, 16, cannot legally apply/use somebody else’s awarded subscription.**
- **Poland:** program is worldwide where Claude.ai is offered, subject to sanctions/legal restrictions; Poland is not singled out as excluded.
- **Exact/no fallback:** Opus 5 can be selected as a named current model on paid Claude plans; Fable 5 has its own safeguard behavior and should not be used for strict benchmark claims if Anthropic reroutes a safety-triggered request.
- **Effort:** Claude Code/Opus 5 supports effort controls including `low / medium / high / xhigh / max`.
- **Direct link:** https://claude.com/contact-sales/claude-for-oss
- **Verification sources:** https://claude.com/contact-sales/claude-for-oss ; https://www.anthropic.com/claude-for-oss-terms ; https://claude.com/pricing ; https://support.claude.com/en/articles/15424964-claude-fable-5-on-your-plan
- **Last checked:** 2026-08-15
- **Status:** `NEW OPPORTUNITY`
- **Next action:** do **not** submit under false age or via a parent’s account. Keep for future eligibility, or for an independently qualifying adult maintainer to use personally. KaroX itself can be referenced only truthfully as OSS/local-first guarded coding-agent infrastructure.
- **NEW:** yes.

#### 2. Z.ai Startups Program — `WAITING` — `NEW`

- **Service/domain:** Z.ai / `startup.z.ai`
- **Model:** GLM family; program promises **early API access to new models/features**. GLM-5.3 is not explicitly guaranteed in the program text and is not yet a public API model as of 2026-08-15.
- **What they give:** **up to 1B free GLM tokens**, volume pricing, priority support/rate limits, early model/API access.
- **Type:** API / startup program.
- **Cost/free volume:** $0 if approved; up to 1,000,000,000 tokens.
- **Term:** not publicly fixed on the program page.
- **Limits:** application → review → onboarding; exact token award and rate limits depend on approval.
- **Card:** application page does not state a card requirement.
- **KYC/business:** requires Z.ai login and startup/application details; intended for startups/founders.
- **Guardian/age:** no age rule was found on the public program page in this check; do not assume a minor can sign commercial/startup terms without a guardian/legal entity review.
- **Poland:** global program language; Poland not explicitly excluded on the public page, but approval is discretionary.
- **Exact model:** `GLM-5.3` **not yet guaranteed**; target value is the explicit early-access channel to new models.
- **Effort/reasoning:** current GLM APIs expose thinking controls; GLM-5.3 parameter surface is not public yet.
- **Direct link:** https://startup.z.ai/
- **Verification source:** https://startup.z.ai/ ; current docs https://docs.z.ai/release-notes/new-released ; release timing cross-check Reuters 2026-08-14.
- **Last checked:** 2026-08-15
- **Status:** `WAITING`
- **Next action:** submit one honest KaroX application only if the applicant can truthfully fit the startup/project context; explicitly ask whether early GLM-5.3 API evaluation is available. Do not claim access before approval.
- **NEW:** yes.

#### 3. Z.ai GLM-5.3 launch partners / Open Source Shield — `WAITING` — `NEW`

- **Service/domain:** Z.ai / public GLM ecosystem.
- **Model:** **GLM-5.3**.
- **What they give:** Z.ai says initial model access goes to a select launch-partner group; it also announced **Open Source Shield** to audit selected OSS projects, provide model access for defensive work, and add code-auditing capability to ZCode.
- **Type:** private/early model access / OSS security program / coding-agent security tooling.
- **Cost/free volume/term:** not publicly specified yet.
- **Limits:** selection-based; public release stated as ~2 weeks after 2026-08-14 security assessment. Sensitive cyber capabilities planned under trusted access.
- **Card/KYC/business:** not published for this program yet; verified-user requirements are expected for sensitive cyber functions.
- **Poland:** not specified.
- **Original/exact model:** yes, this is Z.ai’s own GLM-5.3 announcement, but there is **no public API endpoint/model ID yet**.
- **Effort/reasoning:** not publicly documented yet.
- **Direct source:** https://www.reuters.com/technology/chinas-zai-says-new-model-nears-anthropics-mythos-5-cyber-defence-tests-2026-08-14/
- **Verification:** Reuters reports Z.ai’s launch-partner statement and Open Source Shield; Z.ai current docs still do not expose GLM-5.3 publicly.
- **Last checked:** 2026-08-15
- **Status:** `WAITING`
- **Next action:** watch for first-party application/contact page. KaroX’s open-source permission-boundary/security benchmark is a relevant defensive use-case, but that fit is an inference, not an approval.
- **NEW:** yes.

#### 4. Atoms — `NEW OPPORTUNITY` with strict-model caveat — `NEW`

- **Service/domain:** Atoms by MetaGPT LLC / `atoms.dev`
- **Model:** exact selectable **Claude Fable 5** in normal use.
- **What they give:** **15 free credits every day**, no card; full multi-agent vibe-coding/app-building workflow, code editor, build/test/deploy flow and GitHub export/import-style project workflow.
- **Type:** coding agent / vibe-coding platform / hosted IDE-like builder.
- **Cost/free volume:** $0; 15 credits/day. Terms say free credits are trial/personal-use and cap accumulation; there is no public USD/token conversion sufficient for an API-equivalent estimate.
- **Term:** recurring daily free allowance under current terms; quotas can change.
- **Limits:** not a raw OpenAI/Anthropic API route; no public RPM/TPM. Free credit abuse/multi-accounting prohibited.
- **Card:** no card required.
- **KYC/business:** normal email/Google signup; no business account requirement published.
- **Guardian/age:** minimum 13; **under 18 requires parent/legal-guardian permission**.
- **Poland:** Polish localized product pages are live; no Poland exclusion found.
- **Original/exact model caveat:** Atoms’ Fable 5 page identifies Claude Fable 5, but Atoms also documents Anthropic safeguard behavior where flagged safety requests may be routed to **Opus 4.8** with notice. Therefore **fails the user’s absolute no-substitution rule** for benchmark-grade exactness, even though ordinary coding requests can select Fable 5.
- **Effort/reasoning:** Atoms does not expose the full raw Anthropic effort API surface publicly on this page.
- **Direct link:** https://atoms.dev/models/claude-fable-5
- **Verification sources:** https://atoms.dev/models/claude-fable-5 ; https://atoms.dev/terms-of-service ; https://atoms.dev/pl/ai-agents ; https://atoms.dev/pl/usecases/ai-app-builder
- **Last checked:** 2026-08-15
- **Status:** `NEW OPPORTUNITY`
- **Next action:** usable for vibe coding only if safety fallback is acceptable. Do **not** use it to claim a strict all-requests Fable 5 benchmark route.
- **NEW:** yes.

#### 5. Standard Compute — `NEW OPPORTUNITY` but fails exact-model gate — `NEW`

- **Service/domain:** Standard Compute / `standardcompute.com`
- **Model:** marketing supports Fable 5-class/frontier use, but client configuration uses a **`standardcompute` smart-routing model**, not a pinned `claude-fable-5` endpoint.
- **What they give:** free tier/no-card and OpenAI-compatible integration usable from coding clients such as OpenCode/Cline/Aider/Cursor/Claude Code-compatible workflows.
- **Type:** API/router / coding-agent backend.
- **Cost/free volume:** free tier advertised; useful exact free allowance was not public in the evidence reviewed.
- **Term/limits:** router chooses model and may fail over; therefore exact provider/model guarantee is absent.
- **Card:** no card advertised for free start.
- **KYC/business/guardian/Poland:** not sufficiently documented in the public material reviewed; do not assume.
- **Exact/no fallback:** **FAIL**. First-party changelog explicitly describes automatic model selection/failover; user cannot rely on exact Fable 5 every request.
- **Direct link:** https://standardcompute.com/
- **Verification source:** Standard Compute integration/changelog pages describing `standardcompute` routing and failover.
- **Last checked:** 2026-08-15
- **Status:** `NEW OPPORTUNITY` only as a generic router, **not qualifying** for the requested exact-model target.
- **Next action:** ignore for exact benchmark/use unless Standard Compute introduces a pinned Fable 5 model ID with no fallback.
- **NEW:** yes.

### Dedup exclusions discovered in this wave

- **Cocobox** — already `DUPLICATE_SENT` in `OUTREACH_GMAIL_TRACKER.md`; not new.
- **Deepest** — already present in outreach/history; not new.
- **PiAPI** — already present in outreach tracker; not new.
- **OpenCodex** — already present in KaroX provider research; not new.
- **GhostCLI** — known current project/outreach context; do not re-add as a new free route; its Free tier does not give Opus 5 API usage.
- **Standard Compute** and **Atoms** had zero pre-existing repo matches before this audit and were recorded above for the first time.

### Priority after audit

1. **Opus 5:** authenticate/test **RelayGPU new-user $100 promo** first; then **TokenTable** recurring free exact Opus 5 route. New Claude-for-OSS Max 20x route is officially excellent but unavailable to Егор personally while under 18.
2. **Fable 5:** strongest already-known usable routes are **KernelFold**, **Team Assistant**, **Vorflux**; new **Atoms** is easy/no-card but fails absolute no-fallback requirement on safety-triggered requests. Official Claude Max includes Fable 5 but is paid unless obtained through a legitimate program.
3. **GLM-5.3:** there is no public exact API route yet on 2026-08-15. Best new legitimate opportunities are **Z.ai Startups Program early API access** and the still-forming **launch partner/Open Source Shield** path; otherwise wait for the public release expected around late August and immediately re-audit ZCode/Coding Plan/providers once the first-party model ID exists.

## Дополнительная строгая волна — 2026-08-15 20:07 Europe/Warsaw

По прямому указанию пользователя в этой волне **не искались и не засчитывались OSS/startup/research/grant/application программы**. Фильтр: только практический signup/free tier/trial/API/IDE/CLI/coding-agent доступ, который можно начать использовать без ожидания ручного гранта. Перед записью новых сущностей выполнен @kar repo-wide dedup; текущий рабочий индекс: `files_scanned: 1093`, `files_skipped: 240`.

### BlazeAPI — `NEW OPPORTUNITY / STRONG_FABLE_LEAD` — `NEW`

- **Service/domain:** BlazeAPI / `free.blazeapi.org`, `blazeapi.org`.
- **Model:** exact requested model ID `claude-fable-5` показан first-party прямо в рабочем free-endpoint примере; Anthropic перечислен как provider. Exact `claude-opus-5` на публичной free-странице не найден.
- **Что дают:** **300 API requests/day**, refill ежедневно в 00:00 UTC; failed requests не считаются; один message = один request = полный streamed completion.
- **Тип:** OpenAI-compatible API; подходит для клиентов, умеющих custom OpenAI base URL/model ID (OpenCode/Cline/Aider-подобные клиенты требуют отдельного compatibility smoke-test).
- **Стоимость / бесплатный объём:** `$0`; free product заявлен как free forever. 300 requests/day recurring, не одноразовый signup credit.
- **Срок:** recurring daily allowance; публичного expiry акции не указано.
- **Ограничения:** бесплатная линия имеет свой model directory; публичный пример доказывает Fable 5 route, но authenticated `/models`/реальный response metadata ещё не сняты. Не считать доказанным Opus 5 route.
- **Карта:** free signup surface ведёт через Discord и не показывает checkout/card step; отдельный paid product существует на `blazeapi.org`, но free endpoint заявлен отдельно.
- **KYC/business/guardian:** KYC/business account не заявлены. Отдельного возрастного условия на проверенных product pages не найдено; не делать более сильный вывод без Terms.
- **Польша:** публичного region block для Польши не найдено; production signup из польской сессии ещё не выполнен.
- **Original/exact:** caller explicitly sends `model: claude-fable-5`; Anthropic указан как provider. Но strict `original Anthropic upstream / no hidden substitution` должен быть подтверждён response metadata/usage log после signup.
- **Effort/reasoning:** публичная free-page не документирует отдельные Fable effort controls.
- **@kar dedup:** `BlazeAPI|blazeapi.org|free.blazeapi.org` → `files_scanned: 1093`, `match_count: 0` до этой записи.
- **Источник:** https://free.blazeapi.org/
- **Последняя проверка:** 2026-08-15.
- **Что делать дальше:** создать один обычный Discord-linked аккаунт, получить key, проверить authenticated model directory и сделать один минимальный request на `claude-fable-5`; сохранить response model/headers + dashboard per-model usage. Если exact модель и quota подтверждаются — повысить до `WORKS`.

### CodeEz — `NEW OPPORTUNITY / OPUS5_LEAD_NEEDS_BALANCE` — `NEW`

- **Service/domain:** CodeEz / `codeez.ai`.
- **Models:** first-party live page перечисляет **Claude Opus 5** и **Claude Fable 5**.
- **Что дают:** `Free trial credits on sign-up`; конкретная сумма публично **не раскрыта**.
- **Тип:** Windows/macOS desktop AI workspace + coding terminal + developer API. Публично заявлена поддержка Claude Code, Codex CLI, OpenCode и Hermes; API — OpenAI/Anthropic compatible; desktop/API делят один balance.
- **Стоимость:** signup/free trial `$0`; после trial pay-as-you-go, no activation fee/monthly fee/minimum spend по публичной странице.
- **Бесплатный объём / срок / limits:** amount, expiry, RPM/TPM публично не опубликованы — поэтому не считать сильным бесплатным route до account-level evidence.
- **Карта/KYC/business:** публичная signup copy говорит email registration/free trial credits, но отдельный card/KYC step после signup не проверен.
- **Польша:** region eligibility публично не доказана и не опровергнута.
- **Original/exact:** first-party прямо утверждает `No fake or degraded models`, `Pick Opus, and Opus is what runs`, и `certified commercial API channels`. Это сильное заявление, но production response metadata/usage debit всё равно нужен для строгого подтверждения.
- **Effort/reasoning:** отдельные Opus 5 effort controls на публичной странице не описаны.
- **@kar dedup:** `CodeEz|codeez.ai` → `files_scanned: 1093`, `match_count: 0` до этой записи.
- **Источник:** https://codeez.ai/
- **Последняя проверка:** 2026-08-15.
- **Что делать дальше:** обычный signup без оплаты; первым делом записать точный welcome balance, expiry, card requirement и model picker. Если free pool >=$20 и Opus 5 действительно списывается из него — это может стать лучшей новой Opus 5 находкой этой волны.

### Node AI Gateway — `NEW / BLOCKED_FOR_EGOR_AND_UPSTREAM_PROOF` — `NEW`

- **Service/domain:** Node Digital / `node.uk`, API `api.node.uk`.
- **Model:** exact `claude-fable-5`, 1M context; exact Opus 5 на проверенных Node pages не найден.
- **Что дают:** **£25 free credit on signup**.
- **Тип:** OpenAI-compatible metered API gateway.
- **Стоимость / бесплатный объём:** £25 signup credit; no subscription/no minimum. Fable 5 published route is partner-routed and priced per tokens.
- **Срок/RPM/TPM:** expiry/rate-limit details не найдены на model page.
- **Карта:** model page обещает free credit but не даёт достаточного доказательства `no card required`; считать `UNKNOWN` до signup.
- **Польша:** UK business; публичного Poland block не найден, но account eligibility не проверена.
- **Original/exact:** Node explicitly says requests are **partner-routed**, credentials held server-side, routing based on cost/availability with possible failover to another route. Это не model substitution по их заявлению, но upstream не прозрачно first-party Anthropic; strict original-upstream gate не пройден.
- **Возраст:** current Node privacy policy прямо говорит, что компания **не собирает personal data relating to children**. Для Егора (16) этот route не рекомендовать без явного разрешения Node/законного процесса для minor account.
- **Effort/reasoning:** публичная model page не показывает Fable effort controls.
- **@kar dedup:** `node.uk|NODE Gateway|Node AI Gateway` → 0 prior matches в этой волне.
- **Источники:** https://node.uk/ai/models/claude-fable-5/ ; https://node.uk/about-us/legal-and-privacy/
- **Последняя проверка:** 2026-08-15.
- **Что делать дальше:** не регистрировать несовершеннолетнего пользователя без подтверждения Node; route оставить для dedup/reference only.

### Сильное изменение уже известного: BlockRun — `CHANGED / TOP_PRACTICAL_OPUS5`

- **Не NEW:** @kar нашёл существующую запись `RECEIVED_ACTIVE`: ранее от BlockRun уже были получены **100 USDC on Base** для model/benchmark testing. Не публиковать wallet/hash в tracker.
- **Что изменилось:** first-party changelog от **2026-07-24** говорит, что `anthropic/claude-opus-5` добавлен live on launch day и был **verified live before listing**. Current model API/pricing также содержит exact `anthropic/claude-opus-5`, 1M context, `$5/M input + $25/M output` provider rate (gateway fee сверху согласно текущему pricing).
- **Тип:** OpenAI-compatible gateway + Anthropic-style endpoint, pay-per-call in USDC; current public page также имеет no-card/no-signup free try.
- **Практический вывод:** если ранее полученные 100 USDC всё ещё доступны в пользовательском кошельке, это уже примерно `$100` реального бюджета на exact Opus 5 и потенциально сильнее большинства новых signup offers. **Текущий wallet balance в этой волне не проверен**, поэтому не утверждать, что все $100 остались.
- **Effort/reasoning:** changelog описывает Opus 5 adaptive thinking; конкретные exposed effort parameters надо smoke-test/API-doc проверить перед интеграцией.
- **Источники:** https://blockrun.ai/docs/resources/changelog ; https://blockrun.ai/docs/api-reference/models ; https://blockrun.ai/pricing
- **Последняя проверка:** 2026-08-15.
- **Что делать дальше:** первым практическим действием проверить текущий BlockRun/USDC balance; если средства есть — сделать tiny explicit call на `anthropic/claude-opus-5` и затем подключить endpoint к coding workflow. Не использовать auto-router для доказательства exact model.

### Новые, но отсеянные в этой волне — сохранять только для dedup

- **Caila (`caila.io`)** — `REJECTED_LOW_VALUE`: exact Opus 5/Fable 5 + OpenAI-compatible/coding clients, но welcome bonus около 500 RUB (~$6), слишком мало для сильной находки.
- **Project Ares (`projectares.ai`)** — `REJECTED_AGE_AND_SUBSTITUTION`: no-card trial/Fable route есть, но Terms 18+ и platform routing допускает vendor/model substitutions; не для Егора и не exact-gate.
- **Qubax (`qubax.ai`)** — `REJECTED_LOW_VALUE`: Opus 5 surface найден, starter credit порядка ~$1 — мусор по текущему фильтру.
- **MapleFlow (`mapleflow.io`)** — `REJECTED_MODEL_MISSING`: free credits есть, но exact Opus 5/Fable 5/GLM-5.3 не подтверждены.
- **FreeOpen AI (`freeopen.ai`)** — `REJECTED_MODEL_MISSING`: крупнее-looking free credit claim, но current target Claude catalog не дошёл до Opus 5/Fable 5.
- **Gab AI (`gab.ai`)** — `REJECTED_CHAT_ONLY_FOR_TARGET`: starter credits/chat exist, but developer API access is paid; не бесплатный coding/API route.
- **Zylo AI (`zyloai.net`)** — `REJECTED_FREE_TIER_EXCLUDES_TARGET`: Fable-class premium model не входит в free tier.
- **The Claw Bay (`theclawbay.com`)** — `REJECTED_MODEL/ENTITLEMENT_UNPROVEN`: trial/coding-client claims есть, но current target availability не доказана.
- **Yaya API (`yayaok.com` / `claudefabel.com`)** — `REJECTED_RISKY_SHARED_SUBSCRIPTION_ROUTE`: ~$20 signup/Fable claim выглядит привлекательнее, но first-party description говорит о `multi-subscription routing` через Claude Code. Это не соответствует требованию прозрачного легального commercial Anthropic upstream; не рекомендовать.
- **Phaseo (`phaseo.app`)** — `REJECTED_FREE_ENTITLEMENT_UNPROVEN`: current first-party catalog содержит Claude Opus 5, но автоматический бесплатный Opus 5 credit pool достаточного размера не подтверждён.

### Обновлённый практический приоритет после этой волны

1. **Opus 5 прямо сейчас:** сначала проверить оставшийся **BlockRun 100 USDC** — это не новая халява, а уже полученный пользователем бюджет + теперь live exact Opus 5.
2. **Новая Opus 5 находка:** **CodeEz** — технически очень сильный lead (exact/original claim + coding workspace/API/OpenCode/Claude Code), но сперва нужен production balance; без суммы trial не повышать до `WORKS`.
3. **Новая Fable 5 находка:** **BlazeAPI** — лучший genuinely-new instant route этой волны: 300 API requests/day recurring. Нужен один authenticated exact-model smoke test.
4. **Node £25/Fable 5** финансово интересен, но для Егора блокируется возрастной/privacy неопределённостью и не проходит strict transparent-upstream gate.
5. **GLM-5.3:** на 2026-08-15 публичного exact API route по-прежнему не подтверждено. По текущему указанию пользователя application/OSS/startup routes больше не считать; ждать public model ID и сразу проверять instant API/free-tier providers.

## Strict no-repeat verifier wave — 2026-08-15 21:xx Europe/Warsaw

По прямому требованию пользователя эта волна исключает всё, что уже встречалось где-либо в KaroX. Для каждого названия ниже **до записи** выполнен `@kar` repo-wide search (`files_scanned: 1093`, `files_skipped: 240`, `match_count: 0`). OSS/startup/research/grant/application routes не рассматривались. Цель — instant signup/free tier/trial/API/coding-agent routes к exact Opus 5 → Fable 5 → GLM-5.3.

| Сервис | Статус | Почему не выдавать как сильную находку / что ещё нужно |
|---|---|---|
| Skyfall Agents — `agents.skyfall.consulting` | `BLOCKED_EXACT_MODEL_VERSION` | Сильный free offer: 10,000 trial credits на 90 дней, no-card, полноценный repo→tests→PR engineering agent, 1 credit=$0.0025 (~$25 nominal). Но first-party публично пишет только `Haiku → Opus`; exact `claude-opus-5` model ID не доказан. Не повышать до target-qualified до authenticated model-picker/response evidence. Source: https://agents.skyfall.consulting/ |
| GoRouter — `gorouter.app` | `REJECTED_UPSTREAM_UNPROVEN` | Community claims large signup/daily rewards and Opus 5, но first-party public surface не раскрывает model provenance/upstream; independent Claude identity evidence слабое. Не рекомендовать как original exact route. |
| SeekAi — `seekai.cc` | `REJECTED / USER_PRODUCTION_CHECK_FAILED` | Community advertising claimed very large signup/daily rewards and many Claude models, but the user's live new-account check on 2026-08-17 received only about **$0.0001** of balance. The large advertised reward is therefore not a real current general-user entitlement. Model catalog breadth does not rescue the route. **DO NOT SUGGEST AGAIN** unless a later fresh account independently proves a materially large automatic balance and a successful Opus 5/Fable 5 debit. |
| MaxPlus AI — `maxplus-ai.cc` | `REJECTED_ROUTING_PROVENANCE` | Публично заявляет Opus/Fable через смешанные Kiro/AWS/Claude pools и auto-failover; independent match evidence слабое. Не считать transparent original route. |
| MoClaw — `moclaw.ai` | `REJECTED_AGE` | Current first-party pages advertise Opus 5 + free credits/cloud coding environment, but Terms require 18+. Не использовать как маршрут для несовершеннолетнего пользователя; не предлагать обход. |
| BluesMinds — `api.bluesminds.com` | `REJECTED_TARGET_MISSING` | Free signup/daily quota exists, but current free/live model catalog does not provide target Opus 5/Fable 5/GLM-5.3 coding route. |
| Aerolink — `aerolink.lat` | `REJECTED_MODEL_IDENTITY` | Large-looking signup/rolling-credit claims exist, but exact Opus 5 independent verification failed badly; not safe to label original. |
| FreeLLMAPI — `freellmapi.co` | `REJECTED_TARGET_MISSING` | Very large recurring free aggregate allowance, but current free catalog focuses on open/free models and lacks target Opus 5/Fable 5/GLM-5.3. |
| Koozhan / 酷站AI — `koozhan.com` | `BLOCKED_FREE_AMOUNT_UPSTREAM` | Exact Fable 5 + Claude Code/OpenAI/Anthropic-compatible surfaces and high independent Claude-authenticity scores. New accounts get trial balance, but amount is not public; pricing is far below official and upstream economics are insufficiently transparent. Не считать strong free route without production credit/model debit evidence. |
| DeRouter — `derouter.ai` | `REJECTED_NOT_FREE` | Exact Opus 5/Fable 5 and very high independent identity scores, but current use requires paid top-up; no qualifying automatic free entitlement found. |
| `code28.ccwu.cc` / Neko-style relay | `REJECTED_OPERATOR_RISK` | Opus 5 samples showed valid Anthropic thinking signatures/high identity score, but operator/legal provenance and automatic >=$20 free entitlement are not transparent enough. |
| `api.fan` | `BLOCKED_FREE_AMOUNT` | Opus 5 independent signature evidence is strong, but no first-party >=$20 free signup/recurring allowance found. |
| PackyAPI — `packyapi.ai` | `REJECTED_LOW_VALUE` | Independent Opus 5 identity evidence is decent; signup bonus around $1 only. |
| NekoAPI — `api.999555999.com` | `REJECTED_NO_STRONG_FREE` | High independent Claude identity score and coding API, but no qualifying >=$20 automatic free entitlement found. |
| JizhiAPI — `jizhiapi.site` | `REJECTED_LOW_VALUE_ROUTING` | High-ish identity score but signup only about $5 and first-party describes Kiro reverse-proxy route. |
| X-LLM — `x-llm.net` | `BLOCKED_FREE_AMOUNT` | Opus 5 identity evidence exists, but no strong >=$20 free entitlement confirmed. |
| CCSub — `ccsub.net` | `REJECTED_PAID/MODEL_MISMATCH` | Signup/top-up flow is paid and public model tables are inconsistent; no qualifying current free exact Opus 5 route. |
| CoderPlan — `coderplan.ai` | `BLOCKED_FREE_AMOUNT` | Exact Claude Opus 5/Fable 5 now listed; Claude Code/Cursor/Codex gateway and trial-credit language are current. However visible `$50/$150/$350/$700 API usage credit` tiers are paid top-ups (e.g. $50 credit costs ¥50), while automatic new-user trial amount is not publicly specified. Do **not** misreport `$50` as free. Sources: https://coderplan.ai/pricing ; https://coderplan.ai/en/pricing |
| EasyToken — `easy-token.com` | `REJECTED_TARGET/NO_FREE` | Fable 5 exists and Claude Code/OpenCode tooling is supported, but pricing is recharge-based; exact Opus 5 absent from current catalog and no strong free entitlement confirmed. Source: https://easy-token.com/zh/pricing |
| `code.x-aio.com` / Code Plan | `BLOCKED_EVIDENCE` | New to KaroX in this wave, but no sufficient first-party evidence captured for exact target model + >=$20 free entitlement. Keep for dedup only until verified. |

### Result of this verifier wave

- **No new exact Opus 5 route passed all gates yet.** Candidates with impressive nominal credits failed exact-model/provenance/age/free-entitlement checks rather than being padded into the recommendation list.
- **Skyfall Agents** is the strongest still-open *technical* lead because the free allowance and repository-agent workflow are real and substantial, but exact Opus 5 is not publicly exposed by model ID, so it remains blocked.
- **Koozhan / api.fan / DeRouter / NekoAPI** provide useful independent Claude-identity evidence, but none currently satisfies the required combination of transparent legal upstream + automatic strong free allowance.
- **GLM-5.3:** do not substitute GLM-5.2. No public instant exact route confirmed in this wave.

## Card-trial expansion — 2026-08-16

User explicitly allowed trials that require a payment card. OSS/startup/research/application routes remain excluded. New candidates below were absent from the fully re-read KaroX outreach/research stop-lists (`OUTREACH_GMAIL_TRACKER.md`, Top50 dedup, Batch4, Batch5, Wave3) and from the uploaded master tracker. Primary repo-wide `@kar` search was attempted repeatedly but the connector returned `We couldn't connect your account`; therefore these entries are marked NEW_BY_TRACKER_DEDUP rather than claiming a fresh all-repo `match_count: 0`.

### AI·Collab — `NEW OPPORTUNITY / FABLE5_CODING_API` — `NEW_BY_TRACKER_DEDUP`

- **Domain:** `aicollab.app` (4rce.com Digital Technologies GmbH, Germany).
- **Target model:** exact Claude Fable 5 is live and selectable. First-party documentation says it is available to all users.
- **Free entitlement:** 3,000 credits on signup; no card required; credits can be used on all 300+ paid models. PAYG top-ups start at €10 and credits do not expire.
- **Coding/API:** first-party API keys use `https://api.aicollab.app/api/v1`; documented integrations include Cursor, VS Code + Continue/Cline, Python and any OpenAI-compatible client. API usage debits the same AI·Collab credit pool and logs model/tokens/cost.
- **Important exactness caveat:** Fable 5 inherits Anthropic safety classifiers; when a classifier triggers, AI·Collab says the request is automatically answered by Claude Opus 4.8 and the user is informed. Therefore this route is not a 100% no-fallback route for flagged cyber/bio/distillation prompts.
- **Region/legal:** Terms describe worldwide service under German law; no Poland block found. No explicit 18+ minimum was found on the current public Terms page; do not infer more than that.
- **Sources checked 2026-08-16:** `https://aicollab.app/pricing/`, `https://aicollab.app/blog/api-keys/`, `https://aicollab.app/blog/claude-fable-5/`, `https://aicollab.app/terms/`.
- **Next action:** signup normally, confirm `Claude Fable 5` in the authenticated model picker/API model list, create an API key, set a small daily limit and run one Cline/Continue smoke test while saving model + usage evidence.

### Chunk — `NEW OPPORTUNITY / EXACT_OPUS5_TRIAL` — `NEW_BY_TRACKER_DEDUP`

- **Domain/app:** `chunkapp.com`, App Store app by Curious Minds Software, LLC.
- **Target model:** current App Store release text explicitly lists Anthropic Claude Opus 5.
- **Trial:** Chunk Pro unlocks every frontier model and the annual plan currently shows a free trial. Public App Store metadata does not expose the exact trial duration; do not invent one. Apple payment method may be required and is now acceptable under the user's updated rules.
- **Availability:** Polish App Store listing exists; current Terms require at least 13 years old or the local minimum age for data-processing consent, whichever is higher.
- **Workflow:** web/iPhone/iPad/Mac knowledge/research/chat workspace with programming/debugging support. It is NOT an OpenCode/Claude Code/API provider, so classify it as exact-model access rather than a primary vibe-coding backend.
- **Sources checked 2026-08-16:** `https://apps.apple.com/pl/app/chunk-ai-research-assistant/id6472682763`, current US/JP/CA App Store release listings, `https://www.chunkapp.com/tos`.
- **Next action:** install/open Chunk, verify the live Pro model picker explicitly shows Claude Opus 5 before starting the annual-plan free trial; note trial duration/renewal price shown by Apple before confirming.

### dLazy — `NEW LEAD / OPUS5_FABLE5_TRIAL_WORKSPACE` — `NEW_BY_TRACKER_DEDUP`

- **Domain:** `dlazy.com`.
- **Models:** current first-party pricing lists Claude Opus 5 and Claude Fable 5 under built-in Text & Understanding models.
- **Trial plans:** Basic 2,000 credits/month, Pro 9,000 credits/month and Ultimate 20,000 credits/month each show `Start free trial`; Pro also lists API access. Public page does not expose trial duration in parsed content; do not assume a duration.
- **Developer tooling:** dLazy documents API keys, CLI and MCP authentication, but the public CLI/MCP model reference retrieved in this audit is primarily for dLazy generation tools. A standalone `claude-opus-5` LLM endpoint suitable as an OpenCode/Cline backend was NOT proven.
- **Status:** useful exact-model trial lead, but do not call it a confirmed Claude-Code/OpenCode inference route until the authenticated API/model list proves a direct text-model endpoint.
- **Sources checked 2026-08-16:** `https://dlazy.com/en-US/pricing`, `https://dlazy.com/en-US/docs/installation/authentication`, `https://dlazy.com/en-US/docs/cli`.
- **Next action:** inspect trial checkout/account dashboard first; record trial duration/card/renewal and whether the generated API key exposes a direct text LLM endpoint/model ID for Opus 5.


### Follow-up no-repeat sweep — 2026-08-15

All names below were @kar-deduped before investigation (`files_scanned: 1093`, `match_count: 0` at the time of search) and are recorded **only so they never resurface as “new” later**.

- **Tila (`tila.ai`)** — `REJECTED_MODEL_UNPROVEN`: free account has 450 welcome credits + 50/day, but current first-party model documentation did not establish exact Claude Fable 5/Opus 5 availability; third-party claims are insufficient.
- **TeamDay (`teamday.ai`)** — `REJECTED_LOW_VALUE`: legitimate no-card 7-day coding-agent trial, real Claude Code/Fable 5 workflow and 20 work runs, but first-party pricing caps included provider usage at only **$5**, below the strong-route threshold.
- **BizCoder (`bizcoder.ai`)** — `BLOCKED_FREE_AMOUNT`: real repository coding agent with exact Fable 5/current Claude models and a no-card starter credit, but the automatic free credit amount is not publicly disclosed; do not promote until account-level balance is known.
- **Ava Supernova (`ava-supernova.com`)** — `REJECTED_BYOK_TARGET`: free account has 300 monthly platform credits, but current pricing/model surfaces mark Claude Fable 5 as BYOK; free platform credits do not prove hosted Fable access.
- **OIKON (`oikon.io`)** — `REJECTED_AGE`: unusually strong free offer (3,000 Cycles/month + 5,000 welcome on Claude Fable 5, no card), but current Terms require users to be **18+**. Do not recommend an age workaround.
- **Ropilot (`ropilot.ai`)** — `BLOCKED_FREE_AMOUNT/NICHE`: current product exposes Claude Code workflows and now references Opus 5/Fable 5, but the managed new-user starter credit amount is not public; the large free allowance belongs to BYO-provider Actions and the product is Roblox-specific. Not a strong general model-access route.
- **Skyfall/GoRouter/SeekAi/MaxPlus/MoClaw/BluesMinds/Aerolink/FreeLLMAPI/Koozhan/DeRouter/api.fan/PackyAPI/NekoAPI/JizhiAPI/X-LLM/CCSub/CoderPlan/EasyToken/code.x-aio.com** remain dedup-only per the strict verifier table above; do not rediscover or surface them as new.

**Sweep result:** no genuinely new instant route found in this follow-up that simultaneously passes (1) exact target model, (2) meaningful free volume, (3) coding/API usability, (4) transparent/original-enough upstream, (5) no age/application/card blocker. Do not pad future answers with near-misses.

## Browser-verified follow-up — 2026-08-16

### Claude Code Guest Pass `/passes` — `NEW / FIRST_PARTY / STRONGEST_CURRENT_ROUTE`

- **Dedup:** fresh repo-wide `@kar` search for `Claude Code guest pass|/passes|free week of Claude Code|Share a free week` scanned **1096 files** and returned **0 matches** before this entry.
- **First-party proof:** current Anthropic Claude Code command reference documents `/passes` as `Share a free week of Claude Code with friends`; command is only visible on eligible sender accounts.
- **What it gives:** an official **7-day Claude Code** entitlement through an Anthropic referral/guest-pass flow, not a third-party proxy and not an OSS/application program.
- **Current live supply:** a community-curated live directory checked on 2026-08-16 showed **2 active referral codes**, with the newest added about **2 hours** before the check. Treat availability as first-come-first-served and re-check immediately before redemption.
- **Model relevance:** Claude Code is first-party Anthropic. Current Claude product/docs expose Opus 5 as the current Opus model; however, Anthropic's public `/passes` command reference does **not** explicitly promise a fixed per-pass model entitlement. Therefore after redemption, verify `/model`/`/status` and select Opus 5 before calling this exact-Opus-5 proven at account level.
- **Card/renewal:** offer-specific redemption terms can vary; third-party reports disagree on whether payment details are required. User has explicitly allowed card-backed trials, but the actual Anthropic redemption screen is authoritative. Record renewal/cancellation terms before confirming.
- **Sources checked:** `https://code.claude.com/docs/en/commands`, current Claude pricing/help pages, and a live rotating guest-pass directory.
- **Next action:** redeem a currently active pass on an eligible account, open Claude Code, run `/model` and `/status`, and save account-level evidence for `claude-opus-5` before using it for a long coding session.

### llmapi.pro — `REJECTED_PROVENANCE`

- **Why it looked strong:** live signup works; first-party plans page offers a **¥1 one-time trial with 40 real requests**, all models, and lists `claude-opus-5` + `claude-fable-5`; Claude Code setup is documented and card/PayPal payment is offered.
- **Why rejected after browser inspection:** the service's own About page says it is an **independent Claude-compatible API relay, not affiliated with Anthropic, routing to Claude-compatible upstreams**. Exact model names therefore do not prove original Anthropic inference.
- **Dedup:** fresh repo-wide `@kar` search scanned **1096 files** and returned **0 matches** before this entry.
- **Do not recommend** under the user's strict original-model requirement; keep only for dedup so its attractive trial copy does not resurface as a false positive.

## General-user exact Opus 5 wave — 2026-08-17

### VoltAPI — `REJECTED / USER_PRODUCTION_CHECK_FAILED`

- **Domain:** `https://voltapi.ai/`; docs: `https://doc.voltapi.ai/`.
- **@kar dedup before original entry:** repo-wide search `VoltAPI|voltapi.ai|support@claude-api.org` scanned **1099 files**, skipped 240, **match_count: 0**.
- **Why it looked promising:** public docs claimed $10 trial credit after Telegram binding, exact `claude-opus-5`, and Claude Code/OpenCode/Cline support.
- **Production result (user, 2026-08-17):** the live site/account did **not** provide the claimed useful free entitlement and the expected models were **not available in the account UI**.
- **Conclusion:** documentation/marketing evidence was insufficient and contradicted the live user experience. **Do not recommend VoltAPI again** as a free Opus 5 route unless a later production re-check proves both visible `claude-opus-5` access and a real usable free balance.
- **Lesson for future candidates:** do not promote a route to `WORKS` or present it as a normal solution from docs alone; require live-account/model-catalog evidence or equivalent first-party production proof.

### Ellipsis — `CHANGED / RECEIVED_ACTIVE_HISTORY / EXACT_OPUS5_NOW_DEFAULT`

- **Not new:** `OUTREACH_GMAIL_TRACKER.md` already records Ellipsis (`team@ellipsis.dev`) as `RECEIVED_ACTIVE`; an initial organization credit was previously confirmed for KaroX. Fresh repo-wide dedup for `ellipsis.dev` scanned **1103 files** and found that existing outreach record.
- **Material change checked 2026-08-17:** current first-party Ellipsis agent config now uses **`claude-opus-5` as the default model** for managed coding-agent sessions. `fallback_model` defaults to `null`, so a normal config does not silently fall back unless a fallback is explicitly configured.
- **Current public credit economics:** Ellipsis pricing now says every **new organization gets a one-time $100 credit**, while individual accounts get $10. This is a normal product credit, not the separate case-by-case OSS offer. The already-existing KaroX account predates this re-check, so do not assume its current remaining balance equals $100; dashboard balance must be checked before calling it usable today.
- **Coding workflow:** real cloud coding agent with CLI/API/dashboard, repo cloning, isolated sandboxes, file/code changes, tests/commands, GitHub workflows, IDE opening, per-session budgets and exact model/effort configuration.
- **Why this matters:** although Ellipsis itself is old for the user, the combination `existing received organization credit + Opus 5 now being the default hosted model` is a genuine CHANGED route and can be more valuable than a new $20 trial if the old credit remains.
- **Status:** `CHANGED / CHECK_EXISTING_BALANCE_FIRST`. Do not present Ellipsis as NEW. If existing credit is non-zero, it becomes a first-tier exact Opus 5 coding route; if balance is zero, retain only as historical/dedup evidence.
- **First-party sources checked:** `https://www.ellipsis.dev/pricing`, `https://www.ellipsis.dev/docs/reference/agent-config`, `https://www.ellipsis.dev/docs/reference/cli`, `https://www.ellipsis.dev/docs/get-started/quickstart`.

### GitLab Ultimate Trial → Duo Agent Platform — `ALREADY_USED / DO_NOT_SUGGEST_AGAIN`

- **What changed in research:** current GitLab first-party docs still support the technical chain `30-day Ultimate Trial → 24 GitLab Credits → selectable Claude Opus 5 → Duo Agent Platform/CLI/IDE`.
- **User production/history override (2026-08-17):** user explicitly confirmed this GitLab route **already was used/checked before**. That history takes precedence over treating the route as a new recommendation.
- **Repository corroboration:** KaroX already contains `.gitlab/duo/` integration artifacts and `PROJECT_CONTEXT.md` explicitly refers to GitLab Duo profiles, so GitLab clearly was not a genuinely fresh route for this project.
- **Status:** `ALREADY_USED / DO_NOT_SUGGEST_AGAIN`. Keep only for dedup/history. Do not surface it as 🥇/🥈/🥉 unless the user explicitly asks to revisit GitLab because its entitlement materially changed again.
- **First-party sources last checked 2026-08-17:** GitLab Ultimate trial docs/page, GitLab Credits docs, Opus 5 launch/model-selection docs.

### Snowflake CoCo — `CHANGED / GENERAL_USER / $40_INFERENCE / EXACT_OPUS5`
- Individual-developer signup is a normal 30-day product trial, not an OSS/student/startup/referral program.
- First-party signup currently allocates $40 specifically to CoCo inference (CLI/Desktop/Web), plus $360 for other Snowflake usage. Current Snowflake Opus 5 launch/docs place Claude Opus 5 in CoCo coding workflows.
- Trial converts to a $20/month CoCo subscription unless cancelled. Production-check any payment/card step before activation.
- Fresh KaroX scans before this entry found 0 matches for the specific `Snowflake CoCo + Opus 5` route.

### Arena.ai / LMArena — `NEW / GENERAL_USER / FREE / EXACT_OPUS5 / CODE_ARENA`
- Public Direct/Code Arena route; no special-status program, referral or BYOK required by the normal flow.
- Current Code Arena contains live `claude-opus-5-max` and `claude-opus-5-high`; Direct mode supports selecting a specific currently available model.
- Free research platform; live availability/rate limits can rotate. Do not paste secrets/private code because Arena may use/share conversations for research.
- Fresh KaroX scan across 1099 files found no prior Arena.ai/LMArena route (only unrelated Design Arena).

### AWS Free Tier credits -> Bedrock Opus 5 — `REJECTED_MARKETPLACE_CREDITS`
- Do not recommend ordinary AWS $100/$200 Free Tier credits for Anthropic Bedrock: generic promotional credits exclude Marketplace charges unless specifically authorized, while AWS separately grants a Bedrock third-party exception to Activate credits. Activate is a special startup program and is outside the allowed filter.

### DoCode — `NEW / REJECTED_LOW_VALUE / EXACT_OPUS5 / CLAUDE_CODE`
- **Dedup:** fresh KaroX repo-wide search on 2026-08-17 for `DoCode|docode.cc` completed with `files_scanned: 1103`, `files_skipped: 240`, `match_count: 0`; treat this as genuinely new relative to the current repository history.
- **Current first-party offer:** `https://docode.cc/` currently advertises **150 signup units**, **5 units/day** from check-in, and a recharge ratio of **¥1 -> 50 units**. Older July community posts advertised 300 signup units; do **not** use 300 as the current claim because the official page has since reduced it to 150.
- **Exact target proof:** independent Veridrop testing on 2026-07-27 recorded a real Anthropic-protocol request to `claude-opus-5` at `docode.cc` with **100/100**, including a valid Anthropic thinking signature. Historical aggregate remains strong, although separate probes have found model substitution on some non-Opus models and token-billing anomalies, so re-test the exact channel before sensitive work.
- **Coding workflow:** first-party site explicitly supports Claude Code, Cursor, Codex CLI and Gemini CLI; API base is `https://api.docode.cc` and the Anthropic/OpenAI-compatible workflow is intended for coding clients.
- **Value:** current 150 signup units are **low value**, not a $150 grant. The site sells 500 units for ¥10, so 150 signup units correspond to roughly ¥3 of its internal purchased quota (about US$0.4 at ordinary FX), far below the user's >=$20 target. The `$` glyph on the page/community posts must not be interpreted as USD. Keep only as dedup/low-value evidence.
- **Status:** `TEST_FIRST`. Register normally, confirm the 150 balance is visible, confirm `claude-opus-5` is selectable, make one tiny request, inspect balance delta/model response, then run a Veridrop/identity check before using larger tasks. Use only non-sensitive/public code until the relay is independently trusted.
- **Sources checked 2026-08-17:** DoCode first-party homepage/pricing; Veridrop `docode.cc` aggregate and Opus-5 report; current community posts confirming the signup/check-in mechanism.

### ApiBasis — `NEW / TEST_FIRST / $50_NEW_USER_WELFARE / EXACT_OPUS5_LISTED / CLAUDE_CODE_API`
- **Dedup:** fresh KaroX repo-wide search on 2026-08-17 for `ApiBasis|apibasis.com` completed with `files_scanned: 1103`, `files_skipped: 240`, `match_count: 0`; this domain/route is genuinely new relative to the current KaroX research history.
- **Free entitlement:** the current site notice says a **new user can receive $50 USD welfare** by registering, joining QQ group `1061506850`, and sending the registered email. If an account is legitimately bound through an invitation relationship, both sides receive an additional $25; do not rely on referrals for the base finding. This is a manual community claim step, not an automatic dashboard grant.
- **Exact target:** current public model table includes exact `claude-opus-5`. The cheapest listed `Kiro 90% cache` channel is around **¥0.35/M input + ¥1.75/M output**, while a `Claude MAX` channel is around ¥4.5/M + ¥22.5/M. At the cheap channel's stated price, $50 of platform quota would represent a very large amount of coding inference if the entitlement can actually spend on that group.
- **Coding/API:** normal endpoint `https://apibasis.com/v1`; integration endpoint `https://cdn.apibasis.com/v1`. The service advertises Claude/API developer use and recent users report latest models working.
- **Critical authenticity caveat:** no independent Veridrop/ModelVerify exact `apibasis.com + claude-opus-5` cryptographic identity report was found during this audit. The ultra-cheap channel is labeled Kiro, so treat it as `TEST_FIRST`, not proven original Anthropic routing. Claim only through the site's published community process, then run one tiny exact Opus-5 request and an independent identity/thinking-signature test before using meaningful code or quota.
- **Privacy/risk:** third-party relay; use public/non-sensitive repositories until upstream provenance and retention behavior are independently trusted.
- **Sources checked 2026-08-17:** current Hvoy ApiBasis profile/site notice and public model-price table; KaroX repo-wide dedup.

## Current-quota verifier wave — 2026-08-18

### 分享奇点 — `NEW / RISKY / FABLE5_LARGE_EFFECTIVE_BALANCE`
- **Dedup:** repo-wide regex over `分享奇点|proxy-gls.de5.net|GuysCode|guyscode.com|Sozin|sozin.ai` completed before these entries with `files_scanned: 1103`, `files_skipped: 240`, `match_count: 0`.
- **Current community index:** current Sk-Free snapshot advertises roughly **$30 signup balance + $5–8/day check-in**, exact `claude-fable-5`, and an advertised Fable multiplier around **0.25x**. Direct signup redirects to `api-public.proxy-gls.de5.net`.
- **Why it is not WORKS:** independent ModelVerify evidence for this provider is weak (about 33% aggregate across the available probes) and does not independently validate Fable 5 specifically. Treat the attractive quota as `TEST_FIRST`; do not use private code until a real Fable request and model-identity check pass.

### GuysCode — `NEW / CHANGED_LIVE_EVENT / RETEST_NOW`
- **Current signal:** the current Sk-Free snapshot describes a limited-time **full Fable 5 free-use event** (`满血Fable5，免费蹬`) plus daily check-in at `guyscode.com`.
- **Conflict:** an older Hvoy profile described the station as GPT-only and had no Fable production probe, so the current Fable event is a material change but not independently verified yet.
- **Status:** `RETEST_NOW`, not `WORKS`. Verify current model picker/API model ID, make one tiny Fable request, then inspect quota/debit and model identity.

### Sozin — `NEW / REJECTED_AGE`
- **Offer:** current public pricing advertises a free no-card web-chat tier with **20 messages/day** and access to all models, including Claude Fable 5.
- **Blocker:** current Terms require users to be **18+**. Do not recommend a fake-age account for Egor; retain only for dedup/history.

## Strict Claude Opus 5 API + Hvoy wave — 2026-08-25

Strict success rule for this wave: `VERIFIED_FREE` requires free balance/quota, a real API key, and a successful Hvoy.ai request to exact Opus 5. No API keys are stored here.

### Requesty — `PROMISING / LIVE_FREE_CREDITS_VERIFIED / HVOY_AUTH_FLOW_BLOCKED`
- First-party offer: **$10 free signup credits, no card**, exact Opus 5 routes and an EU gateway.
- Live Browser Use Karo: authenticated workspace showed **Available Credits $7.95**; Billing History showed **No payment history found**.
- Live Playground on the same account exposed Opus 5 deployments from Anthropic, Bedrock and Vertex; `claude-opus-5@eu-central-1` was selected for a tiny test.
- API-key management is live, but fresh-key creation requires a credential-protected name field. Karo correctly blocks agent-side credential-flow entry; existing key pages do not reveal secrets again. Do not bypass this guard.
- Hvoy was initially 502/522, then recovered and loaded normally. The final Hvoy key test still could not be completed because a fresh secret could not be transferred without crossing the credential boundary.
- Therefore this is **not `VERIFIED_FREE`**. Best current practical candidate; next retest is user-completed key creation → Hvoy exact Opus 5 → post-request balance check.

### KernelFold — `CHANGED / PROMISING / EXACT_CLAUDE_OPUS_5`
- Material change: current first-party docs now list literal **`claude-opus-5`**; the older tracker objection that exact Opus 5 was absent is obsolete.
- Free tier: **$0, 1.5M tokens/month, no card**, shared across API/models.
- OpenAI-compatible and Anthropic-compatible `/v1/messages` APIs are documented, suitable for Hvoy and OpenCode/Cline/Aider.
- Live signup page loaded with no payment step; auth could not be completed through the currently available Karo credential reference. Keep `PROMISING` until dashboard quota + key + Hvoy pass.

### Vercel AI Gateway — `PROMISING / $5_EVERY_30_DAYS`
- First-party Opus 5 page lists `anthropic/claude-opus-5`; free users who have never paid receive **$5 every 30 days**.
- Live Browser Use Karo signup reached the one-time-code gate. No OTP/OAuth bypass was attempted. Needs completed signup + key + Hvoy.

### Anthropic Claude Platform — `CHANGED / PROMISING_OFFICIAL`
- Official pricing FAQ now says **new users receive a small amount of free credits to test the API**; amount is not published.
- Live browser profile was not authenticated into Claude Platform, so account balance/key were not verified. Normal usage after promo credits is paid/prepaid.

### Keyplex / TokenTable — `PROMISING`
- Keyplex docs list `anthropic/claude-opus-5`, no-card Free Trial quota **20 RPM / 40k TPM / 100k tokens/day**; signup surface was reached but auth/Hvoy were not completed.
- TokenTable remains a no-card exact-Opus-5 API candidate with a small Starter quota; signup form was reached but auth could not be completed without credential/OAuth takeover.

### Other findings / exclusions
- APIVALE: exact Opus 5, no-card **$0.20** starter credit; `LOW_VALUE / PROMISING` only.
- Atoms: Opus 5 with **15 hosted-app credits/day**, but no proven raw API key; exclude from strict API winners.
- Sozdai / you.bot: technically interesting, but current terms/format make them unsuitable for this strict route; never fake eligibility.
- TaBiToken/TaBiAI and similar public-benefit relays: `RISKY / DO_NOT_USE`; upstream provenance is not strong enough for this task.

### Strict shortlist after 2026-08-25
1. **Requesty** — strongest live free-balance/account evidence; Hvoy blocked only by protected fresh-key flow.
2. **KernelFold** — strongest literal `claude-opus-5` free-tier paper route; 1.5M tokens/month, no card.
3. **Vercel AI Gateway** — strongest recurring mainstream offer; $5 every 30 days.
4. **Anthropic Claude Platform** — official new-user test credits, amount/account entitlement still needs live proof.
5. **Keyplex / TokenTable** — small no-card candidates awaiting auth + Hvoy.

**No route is `VERIFIED_FREE` until Hvoy succeeds and the post-request free balance/quota is checked.**

## Late strict verifier update — 2026-08-25

### Requesty — `LIVE_OPUS5_CONFIRMED / VERIFIED_CREDITS / AGE_RESTRICTED / HVOY_NOT_COMPLETED`
- Re-used the already authenticated production workspace; no duplicate signup was created.
- Live balance remained **$7.95** with no payment history. The same account exposes exact Opus 5 routes from Anthropic, Bedrock and Vertex.
- Browser Use Karo sent one tiny Playground prompt through **`bedrock/claude-opus-5@eu-central-1`**. It returned normally in **0.5 s**, used **23 tokens** and debited **$0.0008**; this is direct proof that the promotional balance can fund an Opus 5 route.
- Hvoy.ai recovered from its earlier 502/522 outage and the API Connectivity Test is live again. However, Requesty only reveals a newly created API secret once, while Karo currently classifies the harmless `api-key-name` field as credential-sensitive. Existing keys cannot be revealed again. Do not bypass this secret boundary merely to satisfy the tester.
- Current Requesty Terms require users to be **18+**. Therefore this technically strongest account proof is **not a legal route for the current user** and must not be promoted as the winner.
- Final strict status remains below `VERIFIED_FREE`: Hvoy exact `claude-opus-5` did not run.

### Boundless API — `NEW / STRONG_PROMISING / $5_NO_CARD / EXACT_OPUS5`
- First-party production pages advertise **$5 signup credits with no card** and list exact **`claude-opus-5`** with an OpenAI-compatible base URL. However, the marketing pages are internally inconsistent about trial eligibility: the English homepage says roughly **100–200 requests “to try any model”**, while the FAQ limits free/welcome accounts to **Starter models** and another localized page describes a **new-user model pool**. Therefore the $5 balance is real marketing, but spending it on Opus 5 is **not proven** until the live dashboard/request succeeds.
- Browser Use Karo reached the real registration service at `oneapi.boundlessapi.com`, including GitHub/Google or username signup, then the username/password/email-verification form. No payment gate appeared before account creation.
- Signup was not completed because password/OAuth/email-code steps are protected user-auth actions; no bypass was attempted.
- Terms allow users 13+, but users between 13 and their local age of majority require parent/legal-guardian permission. Treat as usable only when that condition is legitimately satisfied.
- Status stays `PROMISING` until dashboard balance, API key, exact Opus 5 and Hvoy all pass.

### Flatkey — `NEW / PROMISING / $1_BASE_FREE / UP_TO_$40_MARKETING / EXACT_OPUS5`
- Current first-party homepage exposes exact Claude Opus 5, unified API/official endpoint claims and **“Get Up to $40 in Free Credits.”** Pricing is more conservative: ordinary new users start with **$1 free credit**; a separate Discord promotion advertises $5.
- Browser Use Karo reached the live signup page headed **“Create API key, get free credits”**, with Google/GitHub or email verification and no card shown in the initial flow.
- Do not record `$40` as guaranteed. The only normal public base amount currently supported is $1 until the production dashboard proves more.
- Terms require 13+ and parent/legal-guardian permission under 18. Keep `PROMISING`, not `VERIFIED_FREE`, pending legitimate signup + dashboard + key + Hvoy.

### Vercel AI Gateway — `CONFIRMED_PROMISING / $5_EVERY_30_DAYS / EXACT_OPUS5`
- Fresh first-party model page confirms exact **`anthropic/claude-opus-5`**, Anthropic Messages/OpenAI-compatible API support, and explicitly states that **free users who have never made a payment receive $5 of credits every 30 days**.
- Current Vercel Terms require at least 16; AI Product Terms additionally require parent/legal-guardian permission for users under 18.
- Existing Karo signup attempt already reached the one-time-code gate; no OTP bypass was attempted. Still needs dashboard/key/Hvoy proof.

### Hvoy.ai — `RECOVERED`
- During this wave Hvoy returned 502 and then 522, but a later fresh Browser Use Karo tab loaded the full **API Connectivity Test** normally.
- It supports custom model ID override and Anthropic Messages/OpenAI-compatible formats, so exact `claude-opus-5` remains testable once a newly issued key can be transferred through a legitimate secret-input path.

### RelayGPU — `EXISTING_ACCOUNT_ZERO / PROMO_NOT_PRESENT`
- Rechecked the already-existing authenticated account instead of creating another account. Production Credits page shows **Balance $0**, **Remaining Promo —**, **No active promos**, Total Promo $0 and no invoices, while the live model catalog does contain Claude Opus 5.
- Public `$100 promo` marketing therefore cannot be treated as an entitlement on this account. Do not create duplicate accounts to chase it.

### Fresh rejects / downgrades
- **Tokenator** — `BILLING_REQUIRED`: although it advertises a free Opus 5 route/quota, first-party integration flow and live Karo page say **Sign up → Buy a bundle → Use the key**. A usable key is payment-gated, so it is not an absolutely free route.
- **Composite (lucidity.sh)** — `BILLING_REQUIRED_FOR_OPUS5`: free/no-card account exists, but free-model filtering is for open models; Opus 5 belongs to paid usage.
- **FreeRouter** — `BILLING_REQUIRED_FOR_OPUS5`: free account is not equivalent to free Opus 5; full-model access is tied to paid plans.
- **UseAIToken** — `PROMISING / UNVERIFIED_AMOUNT`: public pages list exact Opus 5, OpenAI-compatible `/v1`, no-card signup credits usable across models, but do not publish the amount; live signup reaches CAPTCHA, so no autonomous bypass was attempted.
- **TaBiToken/TaBiAI** — keep `RISKY`: production catalog lists exact Opus 5, but live pricing contradicted circulating social claims and upstream provenance remains insufficient for this strict task.

### Updated strict shortlist — 2026-08-25
1. **KernelFold** — strongest recurring paper entitlement: 1.5M tokens/month, no card, exact `claude-opus-5`; still needs real account/key/Hvoy.
2. **Vercel AI Gateway** — mainstream recurring $5 every 30 days with exact `anthropic/claude-opus-5`; legitimate under-18 use requires guardian permission.
3. **Boundless API** — $5 no-card signup credit and exact Opus 5 are both public, but contradictory Starter/new-user-pool wording means Opus 5 eligibility for the promo is still unproven; legitimate under-majority use requires guardian permission.
4. **Keyplex** — 100k tokens/day free trial and exact Opus 5; still needs account/key/Hvoy.
5. **Flatkey** — normal $1 new-user credit (larger promos conditional), exact Opus 5; still needs account/key/Hvoy.

**Strict result after this wave: `VERIFIED_FREE = none`. Requesty has direct live free-credit Opus 5 proof but is age-restricted and still lacks the required Hvoy exact-model test.**

## Non-Requesty continuation — 2026-08-25

Per the user's explicit instruction, Requesty is excluded from all further testing in this continuation.

### KernelFold — `VERIFIED_EMAIL / FREE_PLAN / KEY_CREATED / HVOY_SECRET_PASTE_GATE`
- Browser Use Karo is logged into the real KernelFold account; Billing shows the **Free** plan with **1.5M tokens/month**, no payment/card requirement, and the dashboard no longer shows the email-verification block.
- Live dashboard Model Health exposes **Claude Opus 5** as `UP`; first-party docs list exact `claude-opus-5` through both OpenAI-compatible and Anthropic-compatible APIs.
- A dedicated least-privilege **chat-scoped** key named `hvoy-test` was created successfully. The key value was never written to chat/tracker/logs; it was copied only to the local browser clipboard.
- Hvoy has already been configured with endpoint `https://api.kernelfold.com`, Custom Model ID `claude-opus-5`, Anthropic Messages, and a 2-character test prompt. Agent-side secret insertion is correctly blocked by Karo; user takeover was requested only to paste the copied key into Hvoy's API-key field.
- Next strict action after that local paste: resume agent control → Start Test → record HTTP/model/latency/tokens → return to KernelFold Billing/Dashboard and verify the free token-pool debit. If Hvoy returns 200 on exact Opus 5, this can become the first `VERIFIED_FREE` route.

### Anthropic Claude Platform — `BILLING_REQUIRED_ON_LIVE_ACCOUNT`
- Fresh production check of the authenticated Claude Console account shows **Credits $0.00** and **Buy credits to get started**.
- Therefore the official Anthropic route is not a currently usable free Opus 5 API route for this account, regardless of generic documentation mentioning small test credits for some new users.

### you.bot — `REJECTED_AGE / LOW_VALUE`
- First-party site advertises exact Claude Opus 5, one developer API key, **50 free credits**, no card.
- First-party pricing defines **1 credit = $0.01**, so the signup grant is only **$0.50**.
- Current privacy/terms surfaces say the service is intended for users **18+**. Do not use a fake-age account.

### AgentSky — `PROMISING_LOW_VALUE`
- First-party production pages advertise exact **Claude Opus 5**, a real cloud-agent API/API key, **$3 free credit**, and **no card required**.
- This is enough for a small Opus 5 test at published $5/M input + $25/M output, but it remains below the stronger recurring routes and still needs account/key/Hvoy verification.

### Synthorai — `PROMISING_LOW_VALUE`
- First-party Opus 5 page advertises exact `claude-opus-5`, OpenAI-compatible API, **10 trial calls / up to $1 free credit**, no card.
- Keep as a small fallback candidate; do not call it verified until signup/key/Hvoy succeeds.

### Node AI Gateway — `CARD_REQUIRED`
- First-party Opus 5 page confirms exact `claude-opus-5`, OpenAI-compatible API and **£25 free credit on signup**.
- The route is partner-routed and account activation/payment verification may require a card; therefore it is not the preferred absolutely-free/no-card route. Do not label it `VERIFIED_FREE` without a production signup check proving otherwise.

### Fresh hype that stays rejected
- **AgentRouter / GoRouter**: recent social/referral posts advertise very large free balances and Opus 5, but the tracker already records unresolved upstream/provenance concerns. Do not upgrade them from `RISKY` based on affiliate/community claims alone.
- **Qubax**: exact Opus 5 exists, but current route is prepaid crypto top-up; `BILLING_REQUIRED`.

### Non-Requesty priority now
1. **KernelFold** — live Free account is already created; only legitimate email verification stands between it and the decisive Hvoy test.
2. **Vercel AI Gateway** — $5 every 30 days for eligible never-paid Free users; exact Opus 5; auth gate still unfinished.
3. **Keyplex** — exact Opus 5 + published free-trial request quota; authenticated proof still missing.
4. **AgentSky** — $3 no-card credit + exact Opus 5 cloud API; good small fallback.
5. **Synthorai / Flatkey** — $1-class no-card fallback routes.

**Current strict result excluding Requesty: `VERIFIED_FREE = none` until an Hvoy exact-Opus-5 request succeeds.**

## Fresh non-Requesty discovery wave — 2026-08-25 13:xx Europe/Warsaw

### Run BiOS — `STRONG_PROMISING / $10_NO_CARD / EXACT_OPUS5`
- Fresh repo-wide dedup before this entry found no prior `Run BiOS|runbios.ai` match.
- First-party Opus 5 page lists exact `claude-opus-5` at the official `$5/M input + $25/M output` rate and says **new accounts start with $10 welcome credit**.
- Main site and signup page explicitly say **$10 free credit · no card required**; Browser Use Karo reached the real `platform.runbios.ai/register` form with name/email/password fields and no payment step before account creation.
- Run BIOS INC. is identified as the operator (San Francisco; Delaware governing law). Current Terms checked in this wave do not publish an explicit minimum-age clause.
- One OpenAI-compatible inference platform/API is documented; API keys are created in the dashboard and shown once. Do not create or expose a secret outside the protected key flow.
- **Next strict action:** legitimate signup/auth → confirm $10 wallet → create key → Hvoy exact `claude-opus-5` → post-request wallet delta.

### Kyma API — `PROMISING_LIMITED / $0.50_NO_CARD / EXACT_OPUS5`
- First-party pages advertise exact `claude-opus-5`, OpenAI-compatible `https://kymaapi.com/v1`, and **$0.50 signup credit with no card**.
- Browser Use Karo reached the real signup form; no payment step appeared before account creation.
- Operator is Affitor LLC (Wyoming). Terms allow 13+; users below local age of majority require legitimate parent/legal-guardian acceptance. One account per person; do not farm trials.
- Small but potentially enough for a strict Hvoy smoke test. Keep below Run BiOS/Vercel/KernelFold until account-level debit is proven.

### Tokenly — `PROMISING_LIMITED / REAL_PHONE_VERIFY / EARNABLE_CREDITS`
- Current `tokenly.us` signup is live and includes Poland (+48). Signup grants **50 credits**, and additional credits can be earned through surveys/offerwall participation; no card is advertised.
- OpenAI-compatible API and Claude Opus 5 are advertised. Terms require accurate information, one account per person and real phone verification; SMS must use user takeover, never disposable numbers.
- Historical tracker entry for `tokenly.llc` rejected the offer under an older `$20+` threshold. That threshold does not apply to the present strict task, so the current route should be treated as a limited candidate rather than automatically discarded, pending proof that the current domains/service are the same operator and Opus 5 consumes the free credit pool.

### Mume AI — `BILLING_REQUIRED_FOR_TARGET`
- Free plan genuinely provides a recurring small model credit, but Browser Use Karo queried the public live model endpoint for `anthropic/claude-opus-5`; the returned model object has **`plus_exclusive: true`**.
- Therefore the free recurring credit cannot establish free Opus 5 API access. Do not rediscover Mume as a target route unless this entitlement changes.

### Stormfire — `REJECTED_TARGET_MISSING`
- Fresh first-party marketing still advertises a **$5 signup bonus**, no deposit/no KYC, and an OpenAI-compatible gateway.
- Browser Use Karo inspected the current live **55-model** catalog across all three pages. Claude entries top out at Opus 4 / Opus 4.6 aliases (`wf-max`, `wf-opus-prev`, `wf-ultra` etc.); **literal `claude-opus-5` is absent**.
- Free money exists, but the target model does not. Keep rejected until the live catalog materially changes.

### Sozdai — `REJECTED_AGE`
- First-party pages advertise exact `claude-opus-5`, both OpenAI- and Anthropic-compatible APIs, and free trial credits without a card.
- Current first-party Privacy Policy states the services are **not directed to individuals under 18**. Do not create an ineligible account or fake age.

### Synthorai — `REJECTED_AGE` (downgrade)
- Earlier entry correctly recorded exact Opus 5 plus **10 trial calls / up to $1 free credit, no card**.
- Fresh first-party privacy check says the services are not directed to individuals under 18 and the Terms require account holders to be at least 18. Downgrade the previous `PROMISING_LOW_VALUE` status for the current-user route; do not attempt signup.

### OneHop — `REJECTED_AGE / CHANGED_FREE_OFFER`
- Live Browser Use Karo confirms exact `anthropic/claude-opus-5`, native Anthropic Messages endpoint `https://api.onehop.ai/anthropic`, a registered Singapore operator (DEEPQUEST PTE. LTD.), and current production traffic statistics.
- The current new-user offer is **not the older automatic $5/$10 claim**: the live page now says **join the Telegram group and contact the owner to claim $1**.
- Current Terms require users to be at least 18 (or the higher local age of majority). Do not use for the current-user route.

### Zrelay — `RISKY / PRIVATE_$2_TRIAL`
- Live page offers a generated **$2 / 7-day** private trial key and exact Claude-family access through native-format endpoints; no account/email/KYC is advertised.
- Karo refused the `Generate trial key` action as an ambiguous free-trial action and correctly required user takeover. No bypass was attempted.
- Legal/operator transparency remains insufficient for a recommended route. Keep only as a risky research lead, not a winner.

### Cloud routes — fresh hard rejections
- **Microsoft Foundry Free Trial:** Microsoft first-party rate-limit table gives `claude-opus-5` **0 RPM / 0 TPM** on Free Trial; Claude partner-model access requires eligible pay-as-you-go billing. `BILLING_REQUIRED`.
- **Google Cloud Express:** 90-day no-card Express Mode is restricted to Google-published Gemini models (`publishers/google/models/*`); it does not expose Claude Opus 5. `TARGET_UNAVAILABLE`.

### Updated non-Requesty priority
1. **KernelFold** — live Free account + 1.5M tokens/month; only legitimate email verification blocks key/Hvoy.
2. **Run BiOS** — strongest fresh automatic-dollar offer: $10, no card, exact Opus 5, normal API.
3. **Vercel AI Gateway** — recurring $5 every 30 days for eligible never-paid Free users, exact `anthropic/claude-opus-5`.
4. **Keyplex** — published no-card free-trial quota + exact Opus 5; authenticated debit still unproven.
5. **Kyma / AgentSky / Flatkey / Tokenly** — smaller or workflow-limited backups worth smoke-testing if higher-priority auth gates remain blocked.

**Strict result remains `VERIFIED_FREE = none` until Hvoy succeeds on exact Opus 5 and the free balance/quota debit is checked.**

## Search continuation — 2026-08-25 14:xx Europe/Warsaw

This continuation excludes Requesty from any further interaction per the user's explicit instruction. Shared/public API keys are also excluded even when a provider publishes them; only a personal signup entitlement is eligible.

### PEKPIK LLM / `aiapiv2.pekpik.com` — `STRONG_FREE_ENTITLEMENT / EXACT_OPUS5 / REJECTED_AGE_FOR_CURRENT_ROUTE`
- Fresh repo-wide dedup before this entry returned no prior PEKPIK match.
- First-party free-credit page says a personal account receives **$1 immediately after email verification**, then daily claims build the welcome pack to **$20 during the first 40 days**; the personal key is permanent and no card is required.
- First-party live `model_catalog.json` lists literal **`claude-opus-5`**, provider Anthropic, with the model in the **`free`** group and live channels. The free-credit page explicitly says the personal route reaches Claude Opus 5.
- Operator is **LAND4X4 PTY LTD** (Australia). Terms disclose third-party upstream routing; Privacy says prompt/completion content is not stored by PEKPIK but is forwarded to upstream AI providers.
- The site also publishes shared/demo keys. **Never use those**: the user forbids public/shared keys and the strict task requires a personal signup route.
- Current Privacy says the service is not directed at individuals under 18. Therefore do not open an ineligible account or fake age; retain only as a strong technical/reference route for eligible adults.

### LTN AI / `ltnproxy.com` — `STRONG_PROMISING / $10_NO_CARD / EXACT_OPUS5 / PROVENANCE_NEEDS_WORK`
- Fresh repo-wide dedup returned no prior LTN match.
- First-party homepage advertises **$10 credit for new Gmail signups**, no card, one API key, native Anthropic + OpenAI-compatible protocols, and literal `anthropic/claude-opus-5`.
- Documented base URL is `https://ltnproxy.com/v1`; integrations include Claude Code, Codex, OpenCode and other coding clients.
- Main blocker is legal/upstream transparency: no sufficiently clear corporate/legal identity or provider-provenance documentation was found in this wave. Keep below mainstream/transparent candidates until signup, dashboard debit, Hvoy and operator/upstream checks pass.

### apiToken.sale — `PROMISING / $5_NO_CARD / EXACT_OPUS5 / LEGAL_CAPACITY_CAVEAT`
- Fresh repo-wide dedup returned no prior match.
- First-party docs advertise a **$5 platform bonus** for eligible new B2C accounts created via Google/GitHub, no card, and say the bonus can be spent on Claude/GPT/Gemini.
- Router exposes native Anthropic `/v1/messages` plus OpenAI-compatible endpoints and lists literal `claude-opus-5`.
- Anti-fraud rules can withhold the bonus from duplicate/linked accounts; never create multiple accounts to farm it.
- Terms/Privacy use legal-capacity/children restrictions; do not use this route where the account holder does not satisfy them legitimately.

### ModelAPI / `aimodelapi.ai` — `PROMISING_LIMITED / $1_NO_CARD / EXACT_OPUS5 / LICENSED_UPSTREAM_CLAIM`
- Fresh repo-wide dedup returned no prior match.
- Current first-party site says email signup auto-creates an API key and gives **$1 welcome/test credit with no card**.
- Literal `claude-opus-5` is live behind the OpenAI-compatible base `https://api.aimodelapi.ai/v1`; integrations cover common coding clients.
- First-party model/platform material claims licensed B2B upstreams rather than scraped consumer accounts or subscription splitting. This is a useful provenance signal but still needs a real account-level Opus 5 debit + Hvoy before promotion.

### Orq.ai AI Gateway — `MAINSTREAM_PROMISING_LIMITED / €1_NO_CARD / EXACT_OPUS5`
- Fresh repo-wide dedup returned no prior Orq.ai match.
- First-party AI Gateway says signup gives **€1/$1 of free credit, no card**, then one API key and an OpenAI-compatible endpoint.
- Current provider catalog lists Anthropic **`claude-opus-5`** and Bedrock variants such as `eu.anthropic.claude-opus-5`; pricing describes Orq-managed model usage from the same credit system.
- Orq is an established EU gateway with GDPR/ISO-oriented documentation and transparent pay-as-you-go pricing. Best new low-value mainstream candidate from this continuation; exact free-credit debit on Opus 5 still needs live signup + Hvoy.

### LLMAI / `llmai.dev` — `PROMISING_LIMITED / $2_MANUAL_TRIAL / EXACT_OPUS5`
- Fresh repo-wide dedup returned no prior match.
- First-party docs list exact `claude-opus-5`, OpenAI-compatible `https://api.llmai.dev/v1`, and say models are available across accounts.
- Provider offers **$2 free trial credit on request** rather than an automatic signup grant. No card is required for the account itself.
- Privacy indicates service intended for users 16+. Because the credit is manual/requested, keep below automatic-credit candidates until the entitlement actually appears in dashboard.

### KissAPI / `kissapi.ai` — `PROMISING_LOW_VALUE / $1 / EXACT_OPUS5 / AGE_18+`
- Fresh repo-wide dedup returned no prior match.
- First-party site lists exact Opus 5 through an OpenAI-compatible endpoint and offers **$1 free credit** usable for testing; credits do not expire.
- Current Terms require users to be at least 18. Do not use an ineligible account; retain as an adult-only low-value reference route.

### AssetMeld — `BETA_PROMISING / $0.50_COMMUNITY_CLAIM_UNVERIFIED`
- Fresh repo-wide dedup returned no prior match.
- First-party site exposes OpenAI-compatible `https://api.assetmeld.com/v1`, API keys and literal `claude-opus-5`; it also claims external model verification through cctest.ai/Hvoy.ai.
- A fresh builder/community post claims **$0.50 early-test credit with no card**, but the current first-party pricing surface still looks prepaid and does not publish that bonus. Do not count the $0.50 until a live dashboard proves it.

### Aerolink — `CHANGED / $35_WEEK_TRIAL / EXACT_OPUS5_CLAIM / IDENTITY_ROUTE_CONFLICT`
- This is not new; the old tracker rejected Aerolink after poor Opus identity evidence. The financial offer has materially changed.
- Current first-party pricing advertises a **1-week free Starter trial, $5 per rolling 5h / $35 per week, no payment**, with API key management/logs.
- Current social/route material advertises exact `claude-opus-5`, but independent identity checks conflict sharply by endpoint: a recent `capi.aerolink.lat` probe scored extremely poorly while a separate `api.aerolink.lat` Opus 5 probe scored strongly and passed Anthropic thinking-signature checks.
- Therefore the new free allowance does **not** clear the strict model-identity gate. Retest the exact endpoint issued to a real free account through Hvoy before any recommendation.

### EveryAPI — `LOW_PRIORITY / $2_NO_CARD / USER_CHANNEL_ROUTING / AGE_18+`
- Fresh repo-wide dedup returned no prior match.
- First-party site offers **$2 signup credit**, no card, balance does not expire, one API key and multi-provider routing; public material includes Claude Opus 5.
- Terms require adulthood and disclose that requests can route through platform-operated or approved user-supplied upstream-credit channels. Because the strict task rejects gray/shared-subscription-style provenance, keep this out of the winner list despite transparent disclosure.

### Kadegate — `REJECTED_TARGET_MISSING / $5_FREE`
- First-party site advertises **$5 credit to every new account, no card**, but the current public model/pricing surface does not expose Claude Opus 5; Claude listings top out at older Opus generations.
- Terms also require adulthood. Keep rejected until literal Opus 5 appears in the live model catalog.

### Official cloud routes — strengthened hard rejection
- **Microsoft Foundry:** current Microsoft documentation requires an eligible paid Azure subscription/PAYG for Claude partner models; ordinary free/student/startup-credit-only subscriptions are unsupported, and Marketplace/third-party-branded use is excluded from generic free Azure credit. `BILLING_REQUIRED`.
- **Google Cloud Vertex AI:** Google Free Program documentation excludes generative-AI partner model managed APIs / Marketplace from the $300 Welcome Credit. Claude on Vertex is a partner-model route. `BILLING_REQUIRED_FOR_CLAUDE`.
- **AWS Bedrock ordinary Free Tier:** keep the existing rejection; generic promo credits do not make normal Anthropic Marketplace/third-party charges free. Separate AWS Activate startup treatment is a special-program route, not general signup.

### Official Anthropic programs — legitimate but not instant general-user routes
- **External Researcher Access Program:** official Anthropic program can grant about **$1,000 API credits** to approved prioritized AI-safety/alignment research; monthly review/application, not instant signup.
- **AI for Science:** official program can grant up to **$20,000 API credits for six months** to qualifying academic/nonprofit high-impact scientific research; application/review required.
- **Startups / Community Ambassador / selected community events:** legitimate API-credit routes exist through eligibility/application/event participation. Recent Claude community workshops have advertised event API credits, but these are not guaranteed general-user signup offers.

### New practical priority after this search continuation
1. **KernelFold** — live Free account already exists; email verification is the only current gate before key → Hvoy.
2. **Run BiOS** — $10 automatic, no card, exact Opus 5 and normal API; cleanest fresh dollar offer awaiting signup/Hvoy.
3. **Vercel AI Gateway** — recurring $5/30 days on eligible never-paid Free accounts; mainstream route.
4. **Orq.ai** — only €1, but mainstream EU gateway, exact Opus 5, no card, one-key API; excellent Hvoy smoke-test candidate.
5. **LTN AI** — $10 and exact Opus 5, but operator/upstream transparency must improve before recommendation.
6. **ModelAPI / Kyma / AgentSky / Flatkey / Tokenly / LLMAI** — smaller backups that can still satisfy a strict exact-model smoke test if their live signup entitlements hold.
7. **PEKPIK / KissAPI / EveryAPI / other age-restricted routes** — technical/reference only where account eligibility is legitimately satisfied; never fake eligibility.

**No new route is `VERIFIED_FREE` yet because Browser Use remains paused at the legitimate KernelFold email-verification takeover. Continue discovery meanwhile; resume Browser Use only after the user completes that auth step.**

## Discovery continuation — 2026-08-25 15–16:xx Europe/Warsaw

Requesty is explicitly excluded from all further interaction per user instruction.

### Lightning Deals / `lightningdeals.store` — `PAUSED / DEAD_CURRENT_TRIAL`
- Fresh repo-wide dedup before this entry returned no prior match.
- Public docs/marketing advertise an OpenAI-compatible gateway, literal `claude-opus-5`, and **1,000,000 free tokens with no card and no account**.
- Browser Use Karo opened the live `/trial` page. Production UI explicitly says **“Free trials aren’t open right now”** and **“There is nothing to claim at the moment.”**
- Therefore this is not a current free route despite the still-visible marketing. Retest only if the live claim page reopens.

### AskAI.free — `BILLING_REQUIRED_API`
- Fresh repo-wide dedup returned no prior match.
- Current docs list exact `claude-opus-5`, but API keys are issued to paid Pro/Max accounts only after the first payment clears; the web trial does not include API access.
- Do not present the chat trial as a free Opus 5 API route.

### GlobalModels.ai — `PAUSED / SIGNUPS_CLOSED`
- Fresh repo-wide dedup returned no prior match.
- First-party homepage exposes exact `claude-opus-5`, an OpenAI-compatible gateway and a `$2 trial credit` claim.
- Browser Use Karo reached the live sign-in page; it currently states **“New sign-ups are temporarily closed.”**
- Keep as a retest lead only. No current signup means no current free API route.

### NexAIX / `nexaix.net` — `PROMISING_BUT_UNPROVEN / MARKETING_CONSOLE_MISMATCH / LEGAL_DOCS_404`
- Fresh repo-wide dedup returned no prior match.
- First-party marketing lists exact `claude-opus-5`, a claimed **official commercial channel**, OpenAI-compatible access, zero-log/no-dilution commitments, and says new accounts get trial credit sufficient for an eval suite.
- Browser Use Karo reached the real signup form, but the public production console `/pricing` currently exposes only seven open-weight models (DeepSeek/GLM/MiniMax/Doubao) and does **not** expose Claude/GPT before authentication.
- First-party legal links currently return a branded 404 instead of Terms/Privacy content. Operator identity and the exact trial amount were not established.
- Keep below transparent candidates until a real account shows the trial balance + Opus 5 entitlement and Hvoy succeeds.

### Stark Relay / `starkrelay.bond` — `RISKY / $20_LIVE_CLAIM / OPERATOR_UPSTREAM_UNPROVEN`
- Fresh repo-wide dedup returned no prior match.
- Browser Use Karo live page currently advertises **$20 New Starknet user credit** after registering with a Starknet wallet, and the live model table lists exact `claude-opus-5`, Anthropic, `$5/$25` per 1M, 1M context.
- The product uses wallet-based signup and one relay key. Wallet connection is an auth action and was not automated.
- No sufficient company/legal/privacy identity or transparent Anthropic upstream proof was found on the live site; visible contact surfaces are X/Telegram.
- Attractive amount, but not a strict legal/provenance winner until operator/upstream evidence is much stronger.

### lightningapi.pro — `RISKY / LEGAL_ENTITY_PLACEHOLDER / UPSTREAM_UNPROVEN`
- Separate service from `lightningdeals.store`.
- First-party pages advertise a one-day / 1M-token no-payment trial and exact Opus 5.
- Current Terms/Privacy leave the business identity as a literal placeholder (`[BUSINESS LEGAL NAME]`) and describe third-party upstream/proxy operation rather than a clearly documented Anthropic commercial channel.
- Keep out of the winner list despite the nominal free quota.

### NeuralSpace / `neuralspace.pro` — `PROMISING_FREE_DELAYED / 7_TOKENS_DAY / API_MIN_BALANCE_50`
- Fresh repo-wide dedup returned no prior match.
- First-party operator is publicly identified as **ИП Козлов Андрей Анатольевич** with business identifiers/address in Bishkek, Kyrgyzstan.
- First-party site gives **7 free balance tokens per day** (1 token = 1 RUB), no subscription; bonus issuance can be changed/stopped by the operator.
- Public API docs expose exact **`claude-opus-5`**, Anthropic-compatible `/v1/messages`, OpenAI-compatible `/v1/chat/completions`, API keys and the same unified account balance.
- Critical gate: API docs require **minimum account balance 50 tokens to access text models**. A fresh free account with 7 tokens therefore cannot call Opus 5 immediately.
- If daily bonus tokens persist and accumulate, this could become a legitimate recurring free API route after roughly 8 days. **Accumulation/expiry and whether bonus tokens count toward the 50-token API minimum are not yet proven.** Do not call it WORKS until that is established in a real account.

### Venice — `CONTRADICTORY_MARKETING / FREE_PLAN_PREMIUM_API_NOT_PROVEN`
- First-party Opus 5 model page says new accounts include a free daily allowance and 500 welcome credits with no card, but the current main pricing table shows **Free: 0 monthly credits and no welcome credits**, while 500 welcome credits begin on paid Pro.
- Current help/API material says premium API usage is metered and access is obtained via Pro, DIEM or deposited USD; free web-app prompts are not equivalent to free premium API use.
- Therefore do not count the Opus-page “500 welcome credits” copy as a general free Opus 5 API entitlement without a production-account proof.

### CometAPI — `CHANGED_MARKETING / RETEST_EXISTING_ACCOUNT_ONLY`
- Historical live account check in this tracker showed `$0.00`, so it remains rejected by production evidence.
- Fresh first-party 2026 material now mentions test credits / typical `$1–$5` new-user credits with no card and exact Opus 5.
- Treat as a changed marketing signal, not a new entitlement. Recheck the existing account only; never create a duplicate account to chase a new-user bonus.

### Fresh rejects from this continuation
- **QuickSilver Pro** — free trial is browser/chat access; API key requires purchase. `BILLING_REQUIRED_API`.
- **UVC (`uvc.lol`)** — free 30-minute trial exposes Sonnet-class models; Opus 5 is on the paid Builder tier. `FREE_TIER_EXCLUDES_TARGET`.
- **Piramyd** — Opus trial requires a payment card. `CARD_REQUIRED`.
- **LarpRouter** — small free key exists, but the free pool is explicitly Kiro/cache-routed and current routing surfaces expose Kiro/Claude-Max/Copilot-style pools; transparent original-upstream gate fails. `RISKY`.
- **Relay (`relaythe.app`)** — hosted frontier usage requires a paid licence/provider-cost billing; local free mode is BYOK. `BILLING_REQUIRED_FOR_HOSTED_OPUS`.
- **Cyberzai** — exact Opus 5 is in the paid catalog, but the $0 tier's $3.50 credit is limited to free models and does not allow API-key generation; Opus 5 starts on paid tiers. `BILLING_REQUIRED_FOR_OPUS5`.
- **Magicdoor.ai** — first-party Opus 5 page says Opus 5 requires an active $6/month subscription and is unavailable on the free tier. `BILLING_REQUIRED_FOR_TARGET`.
- **Cloudflare AI Gateway** — the gateway/control plane can be free, but third-party model inference requires loaded Unified Billing credits or BYOK provider credentials. `BILLING_REQUIRED_OR_BYOK`.
- **JBridge** — a free account/API key can be created, but inference endpoints explicitly deduct prepaid credit; active bonuses require a paid top-up/referral top-up. `BILLING_REQUIRED_FOR_INFERENCE`.
- **EcoRouter** — personal Trial advertises 5M tokens, no expiry and all 18 models including `ecorouter/claude-opus-5-b`, but the same first-party plan table sets Trial to **0 requests/min**. Shared promo key is prohibited by this task. Treat the personal free tier as `ZERO_RPM / UNUSABLE` until a live account proves a non-zero request allowance.

### Priority after this continuation
1. **KernelFold** — email verified, live Free account + 1.5M tokens/month, Opus 5 `UP`, dedicated chat-scoped key created; Hvoy is fully configured and waits only for the local secret paste before Start Test.
2. **Vercel AI Gateway** — recurring $5/30 days for eligible never-paid Free accounts; mainstream exact Opus 5 route.
3. **Orq.ai** — €1 free credit, no card, exact Opus 5 on a transparent mainstream EU gateway.
4. **ModelAPI** — $1 automatic test credit, no card, exact `claude-opus-5`; first-party site claims licensed upstream routing.
5. **Kyma** — $0.50 automatic, no card, exact Opus 5; useful small smoke-test route.
6. **Tokenly** — 50 signup credits and earn-to-refill path, exact Opus 5 via OpenAI-compatible API.
7. **Run BiOS** — $10 welcome wallet and Playground Opus 5 are real, but fresh external `bios-...` keys currently fail `/v1/chat/completions` with 401; `BROKEN_EXTERNAL_API_AUTH`.
8. **NeuralSpace** — potentially recurring zero-payment route if 7/day bonus tokens accumulate to the 50-token API minimum; needs account-level proof.
9. **Keyplex / AgentSky / Flatkey / LLMAI** — smaller fallbacks awaiting live entitlement + Hvoy; LLMAI's $2 trial is manual-on-request rather than automatic.
10. **NexAIX / Stark Relay / LTN AI** — interesting quota/model claims but provenance/legal/account evidence is insufficient for a recommendation.

**Strict result excluding Requesty remains: `VERIFIED_FREE = none` until the prepared KernelFold Hvoy request returns successfully and the free-quota debit is confirmed.**

## Run BiOS serverless verification — 2026-08-25 17:xx Europe/Warsaw

### Run BiOS — `PLAYGROUND_WORKS / $10_WELCOME_CREDIT / EXTERNAL_API_KEY_AUTH_BROKEN`
- Live authenticated dashboard exposes a dedicated **Serverless** product and lists **Claude Opus 5** with 1M context alongside `bios-adaptive`, Sonnet 5, DeepSeek V4 Pro, GLM-5.2 and Kimi K2.7 Code.
- Live Billing shows **Wallet Balance $10.00**, **No card saved**, `Welcome credit / Signup credit +$10.00`, Tier 1 serverless limits of **600 requests/min, 200K tokens/min, 60M tokens/day**.
- Live Analytics proves the free wallet funded successful **Claude Opus 5** Serverless requests in the web Playground, so the target model and free wallet entitlement themselves are real.
- The live Playground's successful generation was observed as `POST https://platform.runbios.ai/v1/chat/completions` under the authenticated web session. Run BiOS docs explicitly say web-console calls may use short-lived JWT Bearer auth, which is separate from normal API-key auth and is not a stable external credential for OpenCode.
- Current live API Keys UI creates ordinary `bios-...` keys; both Read Only and Full Access include the `serverless` scope. This disproves the earlier hypothesis that a separate `sk-bios-sl-...` credential class is required.
- Two independent fresh `bios-...` keys were introspected as `active` with `serverless=True`; `/v1/models` accepts the saved key, but `/v1/chat/completions` returns **HTTP 401 `invalid_api_key`** for `bios-adaptive`, `glm-5.2`, and exact `claude-opus-5`.
- Bearer, `X-API-Key`, both headers together, explicit `X-Org-ID`/`X-Workspace-ID`, both `api.runbios.ai` and `platform.runbios.ai`, and the official OpenAI Node SDK quickstart were tested. The official SDK still returns the same 401 for all three models.
- Therefore the problem is not OpenCode, model selection, balance, scopes, or a stale key: the current external Serverless API-key authentication path is inconsistent with Run BiOS's own live quickstart. Treat it as **`BROKEN_EXTERNAL_API_AUTH`** until Run BiOS fixes the gateway.
- Do **not** use/extract the web JWT as an OpenCode credential: it is a short-lived browser-session token refreshed by the console, not a documented long-lived API key.
- Do **not** create a paid GPU Deployment for Claude: dedicated Deployments are a separate per-second GPU product and are unnecessary for hosted Serverless Opus 5.

**Status:** Run BiOS proves that the $10 welcome credit can pay for Opus 5 in its web Playground, but it currently fails the strict task because a fresh normal API key cannot authenticate external `/v1/chat/completions`.

## Fresh non-China instant-credit services — 2026-08-26

- **InferenceHub** — newly GA on 2026-07-16; operated by **InferenceHub Labs, Inc.** New accounts start with **$1 free credit, no card required**. The API key reaches every model over Anthropic and OpenAI-compatible wires. Exact `claude-opus-5` went live on 2026-07-26 at Anthropic list pricing. Strongest fresh candidate because first-party docs explicitly state the free API credit is usable over the API on every model; hosted-chat gating does not apply to API keys. Status: `PROMISING / NEW / $1_AUTO / NO_CARD / EXACT_OPUS5`.
- **OpusGate** — EU-based gateway; signup gives **$1 welcome credit with no card**, and its live catalog exposes exact `claude-opus-5`. Supports both native Anthropic `/v1/messages` and OpenAI `/v1/chat/completions`, with explicit OpenCode setup docs. Pricing claims are unusually aggressive, so provenance still needs a live Hvoy check before trust. Status: `PROMISING / NEW / $1_AUTO / NO_CARD / EXACT_OPUS5 / PROVENANCE_RECHECK`.
- **Standard Compute** — a very new service whose current terms say new accounts can start a **real API free trial with no card** and its frontier pool includes Claude Opus 5. However its public API uses the router model `standardcompute` rather than guaranteeing exact `claude-opus-5` per request, so it does **not** yet satisfy the strict exact-model requirement. Status: `FREE_TRIAL / NON_CHINA / TARGET_NOT_PINNABLE`.
- **CodeGateway** — UK company WHITEDIT LTD and exact Opus 5 support, but first-party pages conflict: registration advertises `$2 to explore on signup`, while the FAQ says free credit requires binding a card. Reject under the strict no-card rule until the live account proves otherwise. Status: `CARD_REQUIREMENT_CONFLICT`.
- **EvoLink.ai** — automatic free credits/no card and exact Opus 5, but the operator is EVO GLOBAL TECHNOLOGIES LIMITED in Hong Kong. Excluded from the user's non-China filter.
- **April API** — advertises $1 free credit and exact Opus 5, but the domain is extremely new, operator identity is opaque, prices are implausibly far below list, and independent reputation signals are weak. Do not recommend without stronger provenance. Status: `RISKY / REJECT_FOR_NOW`.

### New priority
1. **InferenceHub** — first live signup/Hvoy target.
2. **OpusGate** — second, but only after provenance sanity check.
3. Continue discovery for additional US/EU services with automatic signup balance; do not pad the list with BYOK, cards, surveys, waitlists, Hong Kong/mainland-China operators, or non-pinnable routers.
