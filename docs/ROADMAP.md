# Roadmap: от MVP до production

> **Launch-scope update 2026-05-25:** canonical launch planning now lives in
> `../../application/implementation_tasks.md` and
> `../../application/audit_summary_2026-05-25.md`. The old `~95h` total below is
> a prototype-era estimate, not the current production-launch estimate. Current
> launch decisions: one VPS/one bot process, clean production DB, AmoCRM removed
> from launch, Vimeo closed portal without DRM/OTT, Lava behind feature flag until
> GK-060 live-contract validation, and USDT TRC20+ERC20 only.
>
> **Launch feature flags (GK-120, 2026-05-28):** production boots with three
> surfaces hidden by default — `ENABLE_ZELLE=false` (Zelle removed from the bot
> payment picker), `ENABLE_WEBHOOK_INTEGRATIONS=false` (AmoCRM/Make/Zapier
> create/test endpoints return 403 and the outgoing dispatcher no-ops), and
> `ENABLE_AI_SUPPORT=false` (the bot only serves the local FAQ; no OpenAI /
> Anthropic calls). Each flag can be flipped per-environment via `.env`; flip
> only after the corresponding integration is signed off by the product owner.
> Existing DB rows for hidden surfaces stay readable (settings page, admin
> integrations list, support messages) so legacy data can be audited.

> Прототип сознательно ограничен в скоупе — собран за 2 дня. Ниже — честный список того, что осталось доделать. Каждый пункт оценён в реальных часах, чтобы можно было планировать.

## Платежи

| Что | Сколько | Зачем |
|---|---|---|
| Lava Top — live checkout enablement | 2ч | GK-061 implements `POST /api/v3/invoice` against the verified membership_saas offer, asks for the buyer email required by Lava, and handles initial/recurring/fail/cancel webhooks with `X-Api-Key`/Basic auth plus idempotent fulfillment. `ENABLE_LAVA_LIVE_CHECKOUT` stays off until one intentional unpaid-invoice smoke is approved. The fixed-price offer cannot represent local promo/referral discounts or non-recurring gifts, so those combinations fail closed and direct the buyer to Stripe/USDT. Lava refunds do not emit webhooks in the public docs; handle them through reconciliation/admin review until the live account proves otherwise. |
| USDT — автоматическая on-chain проверка | 4ч | Сейчас юзер шлёт скрин, админ модерит вручную. С on-chain проверкой (TronGrid API для TRC20, Etherscan API для ERC20) можно автоматически подтверждать после N подтверждений. |
| Stripe Subscriptions (recurring) вместо one-time | 6ч | Перейти с `mode: payment` на `mode: subscription`, добавить обработку `invoice.payment_succeeded` и `customer.subscription.deleted`. |
| Refunds через UI админки | 2ч | Endpoint `/payments/{id}/refund` + кнопка в Платежах. |

## CRM / Интеграции

| Что | Сколько | Зачем |
|---|---|---|
| AmoCRM OAuth flow в UI | 8ч | Сейчас только outgoing webhooks. Полная интеграция — авторизация владельца аккаунта AmoCRM через OAuth2, создание лидов через REST API. |
| AmoCRM 2-way sync (статус сделки → продление подписки) | 8ч | Если в AmoCRM меняется статус — отразить в боте. |
| Notion / Google Sheets экспорт | 4ч | Готовые шаблоны webhook → Notion DB и Sheets row. |

## Бот UX

| Что | Сколько | Зачем |
|---|---|---|
| Мультиязычность (ru/en) | 4ч | i18n через aiogram-i18n + Babel. Тексты уже в одном месте — `handlers/*` и `keyboards.py`. |
| Уведомления при изменении тарифа из админки | 1ч | После update Plan — рассылка активным юзерам. |
| ~~Promo-коды~~ ✅ GK-210 | — | Реализовано. Таблицы `promo_codes` + `promo_redemptions`, сервис `app/services/promo.py` (валидация: активность, окно `valid_from/valid_until`, лимит `max_redemptions`, одно использование на пользователя, привязка к тарифам), процентные и фиксированные скидки, admin CRUD (`/api/promocodes`) + страница `/promocodes`, ввод промокода в боте на шаге выбора оплаты. **Provider amount handling:** скидка считается на момент создания checkout одним хелпером `discount_for_checkout` (промокод имеет приоритет над реферальной скидкой и не складывается с ней). Stripe — скидка как одноразовый купон (`duration:"once"`, id кодирует значение), поэтому продления остаются по полной цене; line item остаётся полной ценой. Lava/USDT/Zelle — `Payment.amount` сразу хранит цену со скидкой (одноразовый платёж). Фикс-скидка применяется только если валюта купона совпадает с валютой checkout (USD для Stripe/USDT/Zelle, RUB для Lava); иначе скидка пропускается без расхода использования. Инфлюенсер-код (`referrer_user_id`) при погашении создаёт `ReferralAttribution(source=promo_code)`. **Известное ограничение:** использование засчитывается в момент создания checkout, поэтому брошенный неоплаченный checkout расходует одно использование промокода (админ может увеличить лимит или удалить запись). |
| Реферальный второй уровень (multi-tier) | 4ч | Сейчас flat: +7 дней рефереру. Добавить +3 дня рефереру реферера. |
| Trial-период (3 дня бесплатно) | 4ч | Новый source `trial`, отдельный CTA в `/start`. |

## Админка

| Что | Сколько | Зачем |
|---|---|---|
| Dark mode toggle в UI | 1ч | CSS уже готов (`.dark` class). Нужен toggle и сохранение в localStorage. |
| Экспорт CSV (юзеры, платежи) | 2ч | Кнопка → endpoint, отдающий streaming CSV. |
| Графики: разбивка по тарифам, источникам, странам | 4ч | Pie chart по `plan`, bar chart по `provider`. |
| Notification center (real-time через SSE) | 4ч | Когда приходит manual payment — показывать toast без F5. |
| Активность пользователя (timeline) | 4ч | На странице юзера — все его события (логин, оплаты, диалоги, инвайты). |

## Качество

| Что | Сколько | Зачем |
|---|---|---|
| pytest интеграционные тесты на флоу оплат | 6ч | Mock Stripe webhook + fulfill_payment + проверка БД и инвайта. |
| Sentry для бот + API | 1ч | Crash reports. |
| Структурные логи (structlog уже в зависимостях) | 2ч | JSON в stdout → ловить через Loki. |
| GitHub Actions CI: lint + tests + build images | 3ч | Защита от регрессий. |

## Production-ready

| Что | Сколько | Зачем |
|---|---|---|
| Bot webhook smoke на VPS | 1ч | GK-220 добавил opt-in `BOT_UPDATE_MODE=webhook`, Caddy `/tg-webhook/*`, `setWebhook(secret_token=...)` и polling rollback. После VPS/домена остаётся live smoke: отправить Telegram update и проверить доставку через webhook. |
| Backup PostgreSQL → S3 / B2 | 2ч | `pg_dump` cron в отдельном контейнере. |
| Rate limiting на `/auth/login` (slowapi) | 1ч | Защита от brute force. |
| HMAC на исходящие webhooks (мы шлём `X-Membership-Signature`, нужно подписывать payload) | 1ч | Чтобы получатели могли валидировать. |
| 2FA для админов | 4ч | TOTP через pyotp. |
| Аудит-лог (кто что менял в админке) | 3ч | Новая таблица + middleware. |

---

## Итого до production

~95 часов (примерно 12 рабочих дней). Большая часть — интеграции (AmoCRM OAuth, Stripe Subscriptions, on-chain USDT) и тестирование. Базовая архитектура и UI готовы и протестированы.
