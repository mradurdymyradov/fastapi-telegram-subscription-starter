# Архитектура membership_saas

## Компоненты

```
┌────────────┐ polling (launch) ┌──────────────────────┐
│  Telegram  │ ◄──────────────► │   bot (aiogram 3)    │
│ users +    │  опц. webhook    │   + APScheduler      │
│ канал/чат  │                  └──────────┬───────────┘
└────────────┘                             │
                                           ▼
                                ┌──────────────────────┐
                                │  PostgreSQL 16       │
                                │  Redis 7 (FSM, кэш)  │
                                └──────────┬───────────┘
                                           ▲
        ┌──────────────┐  HTTPS  ┌─────────┴──────────┐  JWT   ┌──────────────────┐
        │ Stripe/Lava  │ ───────►│  api (FastAPI)     │◄───────│ admin (Next.js)  │
        │ (+ AmoCRM/   │◄────────│  /webhooks /api    │        └──────────────────┘
        │  Make/Zapier │ outgoing└─────────┬──────────┘        ┌──────────────────┐
        │  за флагом)  │                   │  magic-link/JWT   │ portal (Next.js) │
        └──────────────┘                   └───────────────────│ Vimeo embed      │
                                           ▲                    └──────────────────┘
                                    ┌──────┴────────┐
                                    │   Caddy 2     │  community.example.com (портал)
                                    │   auto-SSL    │  <ip>.sslip.io (админка)
                                    └───────────────┘
```

`docker compose -p membership_saas up -d` поднимает `db · redis · migrate · api ·
bot · admin · portal · caddy`. **Bot и API — один Python-образ**
(`backend/Dockerfile`), две точки входа: `app.bot.main` и `app.api.main`. Правка
`app.db.models` или `app.services.*` требует пересборки **обоих**.

---

## Ключевые потоки данных

### 1. Оплата через Stripe
```
user /subscribe → выбор тарифа (+ опц. промокод) → Stripe Checkout (recurring)
                                       ↓
                       checkout.session.completed / invoice.paid
                                       ↓
                              POST /webhooks/stripe (проверка подписи)
                                       ↓
                              fulfill_payment()  ← ЕДИНАЯ идемпотентная точка:
                                  - create/extend Subscription
                                  - инвайты в ОБА ресурса (канал + чат проработок)
                                  - реф-бонус: +7 дней рефереру / +3 приглашённому
                                  - dispatch('payment.succeeded') → outgoing webhooks
                                    (AmoCRM/Make/Zapier — только если флаг включён)
                                       ↓
                              bot.send_message(user, обе инвайт-ссылки)
```
Stripe — `mode: subscription` с предсозданными Price ID (Месяц/6 мес/Год).
`invoice.paid` идемпотентно создаёт платёж продления и снова зовёт
`fulfill_payment`; `invoice.payment_failed` / `customer.subscription.deleted`
управляют grace/отменой.

### 2. USDT (авто) и ручная оплата
```
USDT: user → «USDT TRC20/ERC20» → бот даёт адрес+сумму → user шлёт tx hash
      ↓ usdt_verifier: сеть, официальный контракт USDT, адрес, сумма, статус, 3 подтв.
      ↓ анти-повтор по (network, tx_hash)
      ✓ валидно → fulfill_payment() (тот же путь, что Stripe)   ✗ → ручная проверка

Ручная: user шлёт подтверждение → Payment(status=awaiting_review)
      ↓ админка → «Модерация» → «Одобрить»
      ↓ POST /api/payments/{id}/moderate → fulfill_payment()
(Zelle на запуск скрыт за ENABLE_ZELLE.)
```

### 3. Веб-портал (magic-link)
```
бот (после проверки активной подписки) → одноразовая magic-ссылка (TTL 15 мин)
      ↓ portal /auth/magic → portal_auth: redeem токена
      ↓ secure HttpOnly cookie-сессия (скользящие ~30 дней)
      ↓ защищённые роуты перепроверяют has_portal_access (та же подписка, что и TG)
модули → видео → плеер player.vimeo.com (domain-level privacy)
vimeo_sync тянет метаданные из Vimeo API; ручной оверрайд в админке.
```

### 4. Просрочка / снятие доступа (APScheduler внутри bot)
```
каждый час  kick_expired_job:  expire подписок → kick из ОБОИХ ресурсов (ban+unban),
                               идемпотентно, с учётом Telegram retry_after на ресурс
каждый день 10:00 UTC          remind_expiring_job: напоминание тем, у кого ≤3 дней
полный возврат                 end_access_for_refund: доступ к порталу + TG снимается сразу
```

---

## Структура кода (backend/)

```
app/
├── config.py              # pydantic-settings, читает .env (вкл. фиче-флаги)
├── db/
│   ├── session.py         # async engine + async_session, Base
│   └── models.py          # 28 таблиц (см. ниже)
├── services/              # бизнес-логика (общая для бота и API)
│   ├── subscription.py        # create_or_extend, expire, expiring_soon, end_access_for_refund
│   ├── referral.py            # коды, лидерборд, attribution
│   ├── referral_ledger.py     # комиссии: pending/vested/cancel + adjust_commission_for_refund
│   ├── promo.py               # промокоды: validate/redeem + discount_for_checkout (промо > реферал)
│   ├── channel_access.py      # инвайты/кик по ВСЕМ настроенным TG-ресурсам
│   ├── portal_auth.py         # magic-link issue/redeem + сессии портала
│   ├── vimeo_sync.py          # синк метаданных архива из Vimeo API
│   ├── reconciliation.py      # ежедневная сверка Stripe/Lava/USDT vs локальная БД
│   ├── google_sheets_export.py# CRM-выгрузка (за кредами сервис-аккаунта)
│   ├── support_ai.py          # адаптер: mock (FAQ) / openai / anthropic — на запуск mock
│   ├── webhooks.py            # outgoing dispatcher (AmoCRM/Make/Zapier, за флагом)
│   ├── notifications.py / billing_notifications.py  # сообщения/рассылки в TG
│   ├── security.py / totp.py  # bcrypt, JWT, referral_code, TOTP 2FA
│   ├── rate_limit.py          # лимит на /auth/login
│   ├── url_safety.py          # SSRF-guard для исходящих webhook URL
│   └── audit.py               # запись в audit_log
├── payments/
│   ├── base.py                # CheckoutResult, PaymentEvent
│   ├── stripe_provider.py     # checkout (subscription) + parse_webhook
│   ├── lava_provider.py       # X-Api-Key/Basic webhook lifecycle (live checkout за флагом)
│   ├── manual_provider.py     # ручная оплата / USDT
│   ├── usdt_verifier.py       # on-chain проверка TRC20/ERC20 (TronGrid/Etherscan)
│   ├── refund.py              # create_refund (Stripe авто / Lava за флагом / USDT-Zelle вручную)
│   └── fulfillment.py         # ЕДИНАЯ fulfill_payment() для всех путей
├── bot/                       # aiogram entry (main, middlewares, keyboards, handlers, tasks)
└── api/                       # FastAPI entry
    ├── main.py                # роутеры + bootstrap (default admin + plans)
    ├── deps.py                # get_db, current_admin
    └── routers/               # 18 роутеров (auth, metrics, users, subscriptions,
                               # payments[+refunds], referrals, promocodes, broadcasts,
                               # support, archive, portal, reconciliation, crm_export,
                               # integrations, plans, config, webhooks_in)
```

### Таблицы (28)
users, plans, subscriptions, payments, refunds, payment_provider_events,
reconciliation_runs, reconciliation_items, referral_attributions, referrals,
referral_payout_batches, referral_commissions, referral_adjustments, promo_codes,
promo_redemptions, gifts, broadcasts, support_messages, admin_users,
integration_webhooks, webhook_log, settings, archive_modules, archive_videos,
archive_video_modules, portal_magic_links, portal_sessions, audit_log.

## Структура кода (admin/ и portal/)

- `admin/` — Next.js 14: login (JWT) + `(dashboard)` с AuthGuard: dashboard,
  users, subscriptions, payments (+ manual/refund), referrals, promocodes, gifts,
  broadcasts, support, integrations, reconciliation, settings; shadcn-style ui,
  recharts, TanStack Query.
- `portal/` — Next.js 14: лендинг, `/me`, `/archive` (модули), `/archive/m/[id]`,
  `/archive/v/[vimeoId]` (плеер), `/archive/orphans`. Свой Caddy-блок с
  relaxed referrer-policy + Vimeo CSP.

---

## Решения и почему

| Решение | Почему |
|---|---|
| **Bot + API в одном образе** | Общие модели/сервисы; два контейнера с разным CMD — минимум дублирования. |
| **`fulfill_payment` — единая точка** | Stripe / Lava / USDT / ручная модерация ходят через одну функцию → идемпотентность и единое место логики (инвайты, реф-бонус, webhook). |
| **Портал отдельным приложением** | Свой referrer-policy и Vimeo-CSP, не ослабляя жёсткую posture админки. |
| **Vimeo domain-privacy вместо DRM** | Закрытый портал управляет доступом в нашей БД; провайдерский DRM/OTT — Phase 2 (утечка прямой ссылки принята как ограничение запуска). |
| **Launch feature flags** | Zelle / AmoCRM-Make-Zapier / внешний AI / live-Lava скрыты по умолчанию; включаются по подтверждению, без выката кода. |
| **USDT авто по tx hash** | On-chain проверка (контракт/адрес/сумма/статус/3 подтв.) + анти-повтор по `(network, tx_hash)`; ручной фолбэк для неоднозначных. |
| **Один VPS / один процесс бота** | На запуск без HA; побочные эффекты идемпотентны и в БД. Бэкапы/мониторинг/restore обязательны. |
| **JWT в localStorage (админка)** | Простота; портал использует secure HttpOnly-сессии. |

---

## Безопасность

- Секреты только в `.env` (gitignored); в репозитории — лишь `.env.example`.
- bcrypt для админских паролей (`bcrypt<4.1`, см. CLAUDE.md), JWT HS256
  (по умолчанию 4 ч), опциональная 2FA (TOTP) для админов.
- В `APP_ENV=prod`: дефолтный admin-пароль и CORS `*` отклоняются на старте.
- Stripe webhook — проверка подписи, **fail-closed** без `STRIPE_WEBHOOK_SECRET`;
  Lava webhook — `X-Api-Key`/Basic с constant-time сравнением.
- Rate limiting на `/auth/login`; SSRF-guard (`url_safety`) для исходящих webhook;
  HMAC-подпись исходящих webhook (`X-Membership-Signature`); audit_log в админке.
- Bot не доверяет `message.from_user` для авторизации — `UserMiddleware` по `tg_id`.
- Портал: одноразовый short-lived magic-token, HttpOnly cookie, перепроверка
  подписки на защищённых роутах.
