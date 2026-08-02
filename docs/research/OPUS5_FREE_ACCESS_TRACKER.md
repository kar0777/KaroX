# Claude Opus 5 — проверка бесплатного доступа для вайбкодинга

**Последнее обновление:** 2026-08-02 13:00 Europe/Warsaw  
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
| Keyplex | API | Страница рекламирует Opus 5; signup route отдавал 404 с встроенной формой | Не подтверждён | REJECTED | Пользователь проверил и сообщил, что сервис не подходит; реальный API-запрос и $20+ не подтверждены. Не предлагать снова. |
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
| Augment Code | IDE / JetBrains / CLI agent | Полноценный агент; trial pool 30,000 credits | Trial требует valid credit card; официальный список пока до Opus 4.7 | REJECTED | Карта обязательна и Opus 5 не подтверждён. |
| Ona | Cloud coding agent / CLI environments | Полноценный agent workflow; free tier includes OCUs | Hosted agent сейчас на Opus 4.6; custom Anthropic требует Enterprise и свой API key | REJECTED | Бесплатные OCUs не дают hosted Opus 5. OSS program требует заявки и не считается instant route. |
| AWS Educate | Cloud labs | Регистрация с 13 лет без карты | Anthropic в Bedrock требует valid payment method для AWS Marketplace | REJECTED | Бесплатные учебные labs не дают свободный Opus 5 API. |
| JetBrains AI Trial | IDE agent | Trial даёт эквивалент $20 AI Credits | Может требовать карту; официальный список: Fable 5, Sonnet 5, Opus 4.8, но не Opus 5 | REJECTED | Сумма подходит, но модель отсутствует и критерий без карты не гарантирован. |
| APIPod → Brainbase Labs | Cloud coding agent / Claude Code harness / API | Google OAuth успешно создал Brainbase-организацию и агента `Claude Code` с выбранной моделью `Claude Opus 5`; доступны tasks, filesystem, browser, tools и cloud sandbox | $25 signup credit, план `Kafka - Free`, карта не добавлена, срок действия отсутствует | REJECTED | Формально проходит денежный и модельный фильтр, но APIPod неожиданно ведёт в Brainbase, а пользователь уже использовал продукт и признал его неудобным для нормального вайбкодинга. Не предлагать снова. Проверено 2026-08-02 через KaroX Billing и agent UI. |
| Cosmic | Cloud code agent / GitHub workflow / API | Opus 5 заявлен на всех планах; бесплатный план без карты поддерживает 1 агента, GitHub-репозиторий, ветки и PR | 300k input + 300k output tokens в месяц, примерно до $9 по прямой цене Opus 5 | REJECTED | Технически хороший near-miss, но бесплатный объём меньше обязательного порога $20. |
| MindsHub | Agent platform / API | Opus 5 доступен как платная/BYOK-модель | Бесплатные 5M tokens относятся только к собственному open-model alias MindsHub Air | REJECTED | Free quota нельзя тратить на Opus 5. |
| ilisai | Web AI workspace | Opus 5 доступен на бесплатном плане | 400 credits ≈ €4 в месяц | REJECTED | Ниже $20 и нет полноценного IDE/CLI/API coding-agent workflow. |
| GitHub Copilot Student | IDE / coding agent | Студенческий план содержит AI credits; Opus 5 доступен только на более высоких коммерческих планах | 200 AI credits; модель выбирается автоматически | REJECTED | Нельзя гарантированно выбрать Opus 5, а денежный эквивалент ниже порога. |
| DigitalOcean Student Pack | Cloud credits / inference API | Student Pack даёт $200 cloud credits; DigitalOcean Inference поддерживает Opus 5 | Anthropic и другие сторонние AI-модели исключены из использования student credits | REJECTED | Большой баланс существует, но его нельзя расходовать на целевую модель. |
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
| GitHub Copilot | IDE / CLI / cloud agent | Аккаунт активен, функции Copilot включены | План Copilot Free | REJECTED | Opus 5 доступен только на более высоких платных планах; текущий аккаунт — Free. Проверено 2026-08-02 через GitHub Copilot settings. |
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
