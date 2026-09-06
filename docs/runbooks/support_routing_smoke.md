# Support routing live smoke (GK-378 / issue #5)

Verifies the end-to-end support path once Grant's curator chat id is wired:

```
user message in bot  →  notification in curator chat  →  admin reply in panel
                     →  delivery back to the user  →  full transcript on the user card
```

Code for this flow (GK-378) is already merged and unit-covered
(`backend/tests/test_support_routing_history.py`, 18 tests). This runbook is the
**live** verification — it must run on the staging-mode host with the staging bot,
DB, and Redis up, using a **test Telegram account only**. Never aim this at the
production audience (Danger rule #1).

## Inputs (issue #5)

- Curator chat: `SUPPORT_ROUTING_CHAT_ID = -1003778334018`
  (`@membership_bot` is an admin there with read/send).
- The value is set in the **server `.env` only** — never committed.
  `deploy/.env.example` documents the knob; the real id lives on the host.

## 1. Set the env on the host (not in git)

```bash
cd /opt/membership_saas/current/deploy
# Add/update the line in the server .env (server-only, gitignored):
#   SUPPORT_ROUTING_CHAT_ID=-1003778334018
grep -q '^SUPPORT_ROUTING_CHAT_ID=' .env \
  && sed -i 's#^SUPPORT_ROUTING_CHAT_ID=.*#SUPPORT_ROUTING_CHAT_ID=-1003778334018#' .env \
  || echo 'SUPPORT_ROUTING_CHAT_ID=-1003778334018' >> .env

# The bot reads settings at process start → recreate the bot (and api, which sends
# admin replies). Never `down -v` (Danger rule #2).
docker compose -p membership_saas up -d --force-recreate bot api
docker compose -p membership_saas logs --tail=20 bot
```

Confirm the value is live inside the container:

```bash
docker compose -p membership_saas exec bot \
  python -c "from app.config import get_settings; print(repr(get_settings().support_routing_chat_id))"
# expect: '-1003778334018'
```

## 2. Run the smoke (test account only)

### Option A — one tap from GitHub (gated, phone-friendly, recommended)

Once this branch is on `main`, run the **Support routing smoke** workflow
(`.github/workflows/support-routing-smoke.yml`) from the Actions tab / mobile app:
fill `tg_id` (the test account), keep `chat_id=-1003778334018`, type `smoke` to
confirm, then approve the `production` gate. It persists `SUPPORT_ROUTING_CHAT_ID`
in the server `.env` (issue step 1), runs the script in the live `api` container
(steps 2-3), and prints the result block in the run summary. A failed delivery
fails the job. It never starts the intentionally-stopped bot process.

### Option B — one command on the host (scripted)

`deploy/scripts/support_routing_smoke.py` drives the exact production paths
(`route_support_message` → curator chat, then `send_message` → user) against the
live bot/DB and prints a paste-ready result block. The test account must have sent
`/start` once (so a `users` row exists). `<TEST_TG_ID>` is its numeric Telegram id.

```bash
cd /opt/membership_saas/current/deploy
docker compose -p membership_saas cp scripts/support_routing_smoke.py bot:/tmp/smoke.py
docker compose -p membership_saas exec bot python /tmp/smoke.py --tg-id <TEST_TG_ID>
# routing-only (don't message the test account back): add --no-reply
```

Expect `RESULT: PASS` with `routing=routed` and `reply=delivered`, a
`💬 Новый вопрос в поддержку` card in curator chat `-1003778334018`, and the reply
arriving in the test account's bot chat. Then confirm the **user card** (admin →
Пользователи → that user) shows both messages in order. The printed block is your
record for step 3.

### Option C — by hand through the real bot UI

1. From the **test** Telegram account, open `@membership_bot`, tap
   **💬 Поддержка** (or send `/support`), then send a recognizable question,
   e.g. `SMOKE GK-378 <timestamp>`.
2. Expect the acknowledgement `✅ Спасибо! Вопрос получен …` back in the bot.
3. Expect a **`💬 Новый вопрос в поддержку`** card in curator chat
   `-1003778334018`, showing the user handle, `id`, `tg`, and the question text.
4. In the admin panel → **Поддержка**, open that user's conversation and send a
   reply.
5. Expect the reply to **arrive in the test account's bot chat**.
6. On the **user card** (admin → Пользователи → that user), confirm the full
   transcript shows both the inbound question and the outbound reply in order,
   each with its delivery badge.

## 3. Record the result

Delivery outcomes are persisted on each `support_messages` row and surfaced in the
panel/user-card badges. Cross-check via API or DB:

```bash
# Most recent support rows with their delivery_status (newest first):
docker compose -p membership_saas exec db \
  psql -U membership_saas -d membership_saas -c \
  "select id, user_id, role, delivery_status, left(content,40) as content, created_at \
   from support_messages order by id desc limit 6;"
```

Expected `delivery_status` values:

| Step | role | delivery_status |
|---|---|---|
| User question routed to curator chat | `user` | `routed` |
| Admin reply delivered to user | `assistant` | `delivered` |

`skipped` means `SUPPORT_ROUTING_CHAT_ID` was empty (step 1 not applied);
`failed` means the bot could not post — re-check it is an admin of
`-1003778334018` with send rights, and that the id is exact.

Record in `docs/runbooks/deploy_log.md` (and close issue #5 / update GK-378):

```
GK-378 live smoke — <UTC timestamp>, host staging-mode, test account <tg id>
- routing:   support_message id=___ role=user      delivery_status=routed
- reply:     support_message id=___ role=assistant delivery_status=delivered
- user card: full transcript present (question + reply, chronological)  ✅
```
