# Deployment Runbook (GK-310)

How to deploy membership_saas to the production VPS and make changes
repeatable. This is the operational companion to:

- `ops_baseline.md` - backups, monitoring, alerts, restore (GK-040).
- `telegram_webhook.md` - optional webhook transport (GK-220); launch uses polling.
- `org_access_handoff.md` - who owns what at launch (GK-150).

## Production host (current)

- Provider/shape: Hetzner Cloud CPX32, Nuremberg, Ubuntu 24.04 LTS.
- IP: `127.0.0.1`. Root SSH on port 22 with Murad's ed25519 key.
- Firewall: `ufw` allows inbound `22/tcp` (SSH), `80/tcp`, `443/tcp` only.
  Hetzner Cloud Firewall is not used; the host `ufw` is the single source.
- Docker Engine + Docker Compose v2 are installed.
- App lives under `/opt/membership_saas/`:
  - `releases/<UTC-timestamp>_<author>/` - one directory per uploaded release.
  - `current` - symlink to the active release; compose runs from here.
  - `current/deploy/.env` - the real env file (gitignored, server-only).

DNS: `community.example.com` -> the VPS IP (Cloudflare A record, DNS-only
/ proxy off). The DNS record is created only after the VPS IP exists. The admin
UI is additionally reachable at `https://178-105-224-219.sslip.io` so Caddy can
issue a cert before/independently of the apex domain.

## First-time server setup checklist

1. Confirm SSH: `ssh -i <key> root@127.0.0.1` returns the host shell.
2. Harden firewall:
   ```bash
   ufw default deny incoming
   ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp
   ufw enable && ufw status verbose
   ```
3. Install Docker Engine + Compose plugin (Docker's official apt repo).
4. Create `/opt/membership_saas/releases/` and the `current` symlink target.
5. Put the real `.env` at `current/deploy/.env` (see "Secrets" below).
6. DNS: ask Grant to point `community.example.com` at the IP, then wait
   for propagation before expecting Caddy to obtain the apex cert.

## Secrets (.env) policy

- The `.env` is **server-only and gitignored**. Never commit it; never paste raw
  secrets into chat or task files. `deploy/.env.example` is the documented shape.
- Receive each secret through OneTimeSecret, write it straight into the server
  `.env`, and delete any temporary upload file afterward.
- Before every sync, take a backup: `cp .env .env.bak.<UTC>` so a bad merge can
  be rolled back.
- Critical keys for launch: `BOT_TOKEN`, `PRIVATE_CHANNEL_ID`, `PRACTICE_CHAT_ID`,
  `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, the three `STRIPE_PRICE_*_ID`,
  `LAVA_API_KEY` + `LAVA_WEBHOOK_API_KEY`, `USDT_*_ADDRESS`, `JWT_SECRET`,
  `ADMIN_DEFAULT_*`, `SENTRY_DSN`, `ALERT_CHAT_ID`, and the `BACKUP_*` set.
- Production guardrails (enforced by the app on `APP_ENV=prod`): default admin
  password is rejected, CORS `*` is rejected, and the Stripe webhook fails closed
  without `STRIPE_WEBHOOK_SECRET`.

### Admin accounts, and retiring the seed one (GK-442)

`SEED_DEFAULT_ADMIN` decides whether the API creates the `ADMIN_DEFAULT_*` owner
at startup when that email is absent. It is **off by default**, and that is the
state a live deployment should end in.

1. **First boot only:** `SEED_DEFAULT_ADMIN=true` with `ADMIN_DEFAULT_EMAIL` and
   `ADMIN_DEFAULT_PASSWORD` set. The API creates one `owner`.
2. Log in, create the real admin accounts, verify you can sign in as one of them.
3. **Then retire it:** set `SEED_DEFAULT_ADMIN=false`, delete both
   `ADMIN_DEFAULT_*` lines from `.env`, restart `api` and `bot`, and delete the
   seed account in the panel. It now stays deleted — before this it came back on
   the next API start, and the keys could not be removed either because the
   config audit listed them as always-required and refused to boot without them.

The config audit enforces the pairing: with seeding **on**, both `ADMIN_DEFAULT_*`
must be present and non-empty or the process refuses to start; with seeding
**off**, neither is read and neither is required.

If a deployment ends up with **no** admin at all, the API logs an ERROR at
startup (it still serves traffic) with this command — run it from `deploy/` on
the host:

```bash
docker compose -p membership_saas exec -T api python - <<'PY'
import asyncio
from app.db.models import AdminUser
from app.db.session import async_session
from app.services.security import hash_password
async def main():
    async with async_session() as s:
        s.add(AdminUser(email='you@example.com',
                        password_hash=hash_password('<a strong password>'),
                        role='owner'))
        await s.commit()
asyncio.run(main())
PY
```

Note `password_hash` — not `hashed_password`. The wrong name is accepted by
Python and silently creates nothing usable.
- **New-company payment migration (GK-391, 2026-06-26):** payments move to a NEW
  Stripe account and NEW USDT TRC20/ERC20 wallets on the client's new company.
  Grant delivers the new keys/Price IDs/wallets/products a day ahead; rotate them
  into the server `.env` (`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, the three
  `STRIPE_PRICE_*_ID`, `USDT_*_ADDRESS`) and run the provider smoke against the new
  credentials **before** retiring the old account. Keep the old account untouched
  until the new one is verified — clean cutover, no payment downtime. Secrets stay
  out of git.
- **Final published prices (GK-389, 2026-06-26):** Месяц $19 / 1500₽ · 6 мес
  $79 / 7000₽ · Год $129 / 10000₽. The bot's per-plan USD and RUB must match the
  Lava offer byte-for-byte or the invoice is rejected; EUR is not set.

## Production bot setup

- The production bot is `@membership_bot`. Its token was shared via
  OneTimeSecret and must live **only** in the server `.env`.
- Before any live access smoke, the bot must be **admin in BOTH production
  Telegram resources** (the closed channel and the practice chat) with rights to
  create invite links and to ban/remove members.
- Launch transport is **polling** (`BOT_UPDATE_MODE=polling`). Webhook mode is
  optional and documented separately in `telegram_webhook.md`.
- SAFETY: do not point the bot at the production resources during functional
  testing - that would mass-kick real members (BLK-017). Test on the staging
  channel/chat first; production cutover is the dry-run-gated GK-016 step.
- **Phased launch plan (GK-016 / BLK-017, confirmed 2026-06-26):** **29 June** the
  bot starts accepting payments and granting access; current free members stay in
  the channel until **6 July**; **6 July** closes free access and removes
  non-payers. Start and cutoff are spaced one week apart = the 7-day grace window.
  The dates are configurable (not hardcoded). Starting payment acceptance on 29
  June does **not** trigger enforcement — the 6 July cutoff is a separate, dated
  step that still requires the admin/owner allowlist, a dry-run report, and
  explicit human confirmation before any mass removal.

## Compose rules (do not break these)

- ALWAYS use the project name: `docker compose -p membership_saas ...`. It keeps
  containers/volumes/networks namespaced across every run.
- NEVER run destructive prune/`down -v` on the production host - that deletes the
  Postgres volume. To restart cleanly use `up -d --force-recreate`, not `down -v`.
- The base file is `docker-compose.yml`; `docker-compose.override.yml` carries
  local-dev-only settings and should not be relied on in prod.

## Services and the rebuild matrix

Runtime services: `db`, `redis`, `migrate` (one-shot), `api`, `bot`, `admin`,
`portal`, `caddy`. Profile-gated: `seed`, `test`, `backup`.

`api`, `bot`, `migrate`, `seed`, `test` all build from the **same**
`../backend` image; `admin`, `portal` build from their own contexts.

| You changed | Rebuild + recreate |
|---|---|
| `backend/app/**` (models/services/api/bot) | `build api bot` then `up -d api bot` |
| A new Alembic migration | `build migrate api bot` then `up -d migrate api bot` (migrate runs `alembic upgrade head` on start) |
| `admin/**` | `build admin` then `up -d --no-deps admin` |
| `portal/**` | `build portal` then `up -d --no-deps portal` |
| `deploy/Caddyfile` | `up -d --no-deps --force-recreate caddy` (Caddyfile is mounted read-only) |
| `deploy/docker-compose.yml` | `up -d` (re-reads the compose file) |
| `.env` values | `up -d --no-deps --force-recreate <affected services>` |

Editing `app.db.models` or `app.services.*` affects **both** `api` and `bot`
because they share one image - always rebuild both.

## Deploying a new release

1. Build a release tarball locally and upload it (single-file uploads use
   `_deploy/upload_one.py`; a full release is packed by `_deploy/pack_and_push.py`).
2. On the server, unpack into `releases/<UTC>_<author>/`, sync/verify `.env`
   (back it up first), then repoint `current` to the new release directory.
3. Validate the compose file before starting:
   ```bash
   docker compose -p membership_saas config --quiet   # exit 0 = valid
   ```
4. Bring services up (build only what changed per the matrix above):
   ```bash
   docker compose -p membership_saas up -d --build
   ```
5. Run the smoke test below. Keep the previous release directory so a rollback is
   just repointing `current` back and `up -d`.

## Smoke test (run after every deploy)

```bash
# 1. API health (expect 200 {"status":"ok"} when the bot is running;
#    503 "degraded" with db.ok:true + bot.stale:true means the bot is stopped)
curl -s https://community.example.com/health

# 2. Portal renders (expect 200 + Russian landing HTML)
curl -s -o /dev/null -w '%{http_code}\n' https://community.example.com/

# 3. Admin renders (expect 200)
curl -s -o /dev/null -w '%{http_code}\n' https://178-105-224-219.sslip.io/

# 4. Stripe webhook reachable + signature-verified (expect 200 ignored / 400
#    on bad signature; never a 5xx)
curl -s -X POST https://community.example.com/webhooks/stripe

# 5. Lava webhook auth (expect 401 without the X-Api-Key)
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://community.example.com/webhooks/lava
```

Also confirm manually:

- Admin login works with the configured admin account.
- Bot check: send `/start` to `@membership_bot` (staging config) and get a reply.
- Portal magic-link login completes end-to-end from the bot.
- Payment webhook sanity: a signed test event reaches `fulfill_payment`
  idempotently (re-delivery returns "already", not a double grant).
- Alert delivery: a forced ops alert lands in `ALERT_CHAT_ID` (see ops_baseline.md).

## Current state (2026-06-06)

Partial staging-mode deploy is live: `db`/`redis`/`api`/`admin`/`portal`/`caddy`
running, `APP_ENV=demo`, all launch feature flags OFF, staging Telegram resources
wired. The **bot is intentionally stopped** (so `/health` is 503 degraded) to
avoid accepting live payments before fulfillment is fully verified. Remaining to
reach a full launch: production Telegram channel/chat IDs + bot admin rights
(BLK-016), a controlled test user for the member grant/revoke smoke, and explicit
human cutover confirmation (GK-016).
