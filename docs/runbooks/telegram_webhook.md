# Telegram Bot Webhook

GK-220 adds an opt-in Telegram webhook transport for the bot. Polling remains
the default because it is the safest local/dev mode and does not need a public
HTTPS URL.

## When to Enable

Enable webhook mode only after the target host is reachable over HTTPS and the
production bot token is configured:

```env
BOT_UPDATE_MODE=webhook
TG_WEBHOOK_BASE_URL=https://community.example.com
TG_WEBHOOK_PATH=/tg-webhook/bot
TG_WEBHOOK_SECRET=<openssl rand -hex 32>
TG_WEBHOOK_LISTEN_HOST=0.0.0.0
TG_WEBHOOK_LISTEN_PORT=8080
```

If `TG_WEBHOOK_BASE_URL` is empty, the bot uses `PUBLIC_BASE_URL`.

## Deploy

From `membership_saas/deploy/`:

```bash
docker compose -p membership_saas up -d --build bot caddy
docker compose -p membership_saas logs -f bot caddy
```

Expected bot log:

```text
starting in webhook mode
```

Caddy routes `/tg-webhook/*` to the `bot:8080` service. The bot registers the
exact `TG_WEBHOOK_PATH` with Telegram via `setWebhook` and sends
`TG_WEBHOOK_SECRET` as the Telegram `secret_token`, so incoming updates must
carry Telegram's `X-Telegram-Bot-Api-Secret-Token` header.

## Verify

1. Confirm the bot container is listening:

```bash
docker compose -p membership_saas exec bot python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8080/tg-webhook/health', timeout=5).read())"
```

2. Confirm the public Caddy route returns health:

```bash
curl -fsS https://community.example.com/tg-webhook/health
```

3. Send a real Telegram message to the bot and watch `docker compose -p membership_saas
logs -f bot`. For the final launch check, use the production bot token and
confirm the update reaches the handler through the webhook path.

## Roll Back to Polling

Set:

```env
BOT_UPDATE_MODE=polling
```

Then restart the bot:

```bash
docker compose -p membership_saas up -d --build bot
docker compose -p membership_saas logs -f bot
```

On polling startup the bot calls `deleteWebhook(drop_pending_updates=False)`.
That removes Telegram's old webhook registration without dropping queued
updates, then starts `start_polling(...)`.
