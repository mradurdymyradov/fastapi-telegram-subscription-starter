# Ops Baseline — GK-040

This runbook explains how to operate the membership_saas production stack: how
logs and Sentry are wired, how nightly backups work, how the restore
drill is performed, and what monitors page the team on Telegram.

It is the canonical reference. The acceptance criteria for GK-040 live
in [application/implementation_tasks.md](../../../application/implementation_tasks.md).

---

## 1. Logging and Sentry

Both `api` and `bot` containers boot through `app.observability.init_observability(service=…)`.

- **Format:** `LOG_FORMAT=json` in prod, `console` in local dev.
- **Level:** `LOG_LEVEL=INFO` by default.
- **Correlation id:** every API request gets a fresh hex id (or honors
  `X-Request-Id`/`X-Correlation-Id` from upstream). The id is bound to
  the structlog context for the duration of the request and echoed back
  in the response as `X-Request-Id`. Caddy can forward this id and any
  external log shipper can join request, response, and Sentry events.
- **Provider event ids:** Stripe and Lava webhooks bind
  `provider`, `provider_event_id`, and `provider_event_type` into the
  structlog context as soon as the webhook is authenticated, so every
  log line from `fulfill_payment` and downstream services carries them.
- **Stdlib loggers:** all `logging.getLogger(__name__)` calls inherit
  the same JSON handler — no per-module configuration needed.

### Sentry

Sentry is initialized when `SENTRY_DSN` is set. Leave it empty for local
or demo runs.

| Setting | Env var | Default |
|---|---|---|
| DSN | `SENTRY_DSN` | empty (disabled) |
| Environment | `APP_ENV` | `dev` |
| Release | `SENTRY_RELEASE` | empty |
| Trace sample rate | `SENTRY_TRACES_SAMPLE_RATE` | `0.0` |

Integrations enabled when the API boots: FastAPI, Starlette, SQLAlchemy,
stdlib logging. The bot uses logging + SQLAlchemy only.

Sentry tags every event with `service=api` or `service=bot` so you can
filter by component in the Sentry UI.

### Where to look when something breaks

- `docker compose -p membership_saas logs -f api bot` — JSON stream with
  `correlation_id`, `service`, `provider`, `provider_event_id`.
- Sentry → Issues, filter by `service` and/or `environment`.
- For a specific Stripe event: search by `provider_event_id` in logs or
  by tag `provider_event_id` in Sentry.

---

## 2. Health and Heartbeat

- `GET /health` returns 200 with a JSON body when everything is OK,
  503 when the DB ping fails or the bot heartbeat is older than
  `BOT_HEARTBEAT_MAX_AGE_SECONDS` (default 180s = three missed beats).
- The `api` container has a Docker `healthcheck` against `/health`, so
  `docker ps` will show `unhealthy` and `restart: unless-stopped` will
  not mask a chronically degraded service.
- The bot writes a heartbeat to Redis every 60s (`ops:bot:heartbeat`).
- A 15-minute APScheduler job (`scheduler_health_job`) walks every
  scheduled job and alerts Telegram if any `next_run_time` is more
  than 600s in the past — that catches the case where the bot process
  is alive but the scheduler is wedged.

External uptime monitoring (UptimeRobot, healthchecks.io, or similar)
should:

1. Probe `https://community.example.com/health` every 1–5 minutes.
   Pages on `status != ok` or on `degraded`.
2. Probe `https://community.example.com/` (the admin homepage)
   every 5 minutes for visual front-door coverage.
3. Probe a Telegram-side path (e.g. heartbeat-pings.io) if available —
   the bot is best validated by its own beats, not Telegram's API.

---

## 3. Telegram Operational Alerts

`send_ops_alert()` posts to the Telegram chat configured by
`ALERT_CHAT_ID`. The production target is `@alert_membership_community`,
the supergroup "Alerts - Owner Community Bot".

**Use `-1003722846405`, with the leading `-100`.** This page carried
`1003722846405` from 2026-05-27 to 2026-08-16, which is not a valid Telegram
chat id — a supergroup id is `-100` followed by the internal id, and the
positive form addresses nothing. The correct value has been set and delivering
on the host since 2026-07-22 (verified again 07.08 by an alert sent through the
real code path); the bad one stayed here in the documentation, which is where
somebody re-provisioning the host would have copied it from.

- Each alert keys into Redis with a configurable rate-limit window
  (default 5 minutes) so a screaming bug does not flood the channel.
- Severity prefixes: `ℹ️` info, `⚠️` warn, `🚨` error.
- The bot token used to send these alerts is the same `BOT_TOKEN` the
  bot uses for normal customer messages; no separate alert bot.

### When alerts fire

| Condition | Source | Rate limit |
|---|---|---|
| APScheduler job lagging more than 10 min | `scheduler_health_job` | 15 min |
| Backup script fails | `entrypoint.sh` via `BACKUP_ALERT_WEBHOOK_URL` | per attempt |
| Restore drill failed (manual) | Operator runs `restore.sh` and sees non-zero exit | n/a |

Future hooks (out of GK-040 scope) can call `send_ops_alert` from any
service when needed — keep the rate-limit key stable so identical
incidents collapse.

---

## 4. Backups

Service: `deploy/backup/` (compose profile `backup`).

- Image: `alpine:3.19` + `postgresql16-client`, `age`, `rclone`.
- Schedule: in-process loop every `BACKUP_INTERVAL_SECONDS` (default 24h),
  with one initial run on container start so the first backup lands
  without waiting a full day.
- Format: `pg_dump --format=plain --no-owner --no-privileges`, gzipped,
  then `age`-encrypted to a single recipient public key.
- Object storage: `rclone copy` to whatever remote `BACKUP_RCLONE_REMOTE`
  points at. The same image speaks AWS S3, Cloudflare R2, and
  Backblaze B2 — pick by name in `deploy/backup/rclone.conf`.
- Local retention: `BACKUP_RETENTION_DAYS` (default 14) days of
  encrypted dumps remain in the `backup_data` volume for fast rollback.
- Remote retention: enforce via bucket lifecycle policy (out-of-band).
  Do not delete remote objects from this container — a script bug
  should not be able to wipe the archive.

### One-time setup

1. **Generate an age keypair on a trusted workstation:**
   ```bash
   age-keygen -o ops-backup-2026.txt
   cat ops-backup-2026.txt   # records the public key as "public key: age1..."
   ```
   Put the **private key file** (`ops-backup-2026.txt`) in your secret
   vault (1Password, Bitwarden, etc.) plus an offline copy. Without it
   the backups are unreadable forever.

2. **Set the public key in the production `.env`:**
   ```env
   BACKUP_AGE_PUBLIC_KEY=age1...   # the long age1 string only
   ```

3. **Configure the rclone target:**
   ```bash
   cp deploy/backup/rclone.example.conf deploy/backup/rclone.conf
   # edit credentials for [s3] (AWS primary). Add [r2] or [b2] if needed.
   ```
   `rclone.conf` is gitignored. Set `BACKUP_RCLONE_REMOTE` in `.env`:
   ```env
   BACKUP_RCLONE_REMOTE=s3:membership_saas-backups/prod
   ```

4. **Enable the backup service:**
   ```bash
   cd membership_saas/deploy
   docker compose -p membership_saas --profile backup up -d --build backup
   docker compose -p membership_saas logs -f backup
   ```
   First successful upload should land within a few minutes.

5. **Verify in the bucket** that the encrypted object is present, then
   schedule a restore drill (see §5) within 7 days.

### Provider switch (AWS → R2 or B2)

1. Add the relevant `[r2]` or `[b2]` section to `rclone.conf`.
2. Change `BACKUP_RCLONE_REMOTE` in `.env`.
3. `docker compose -p membership_saas up -d backup` (no rebuild needed).
4. Verify the next nightly upload.

Old objects in AWS stay reachable for restore — to clean up, use the
bucket UI / lifecycle policy, not this container.

---

## 5. Restore Drill

A restore is only a restore once you have done it. Schedule the drill
in the first week post-launch and again before each significant DB
change.

### Goal

Restore the most recent encrypted backup into a throwaway PostgreSQL
container, confirm row counts roughly match production, then discard the
target.

### Steps

```bash
cd membership_saas/deploy

# 1. Spin up a sidecar Postgres with a throwaway volume.
docker run -d --rm \
    --name membership_saas-restore-target \
    --network membership_saas_default \
    -e POSTGRES_USER=membership_saas \
    -e POSTGRES_PASSWORD=restore_drill_pw \
    -e POSTGRES_DB=membership_saas_restore_drill \
    postgres:16-alpine

# 2. Run the restore inside the backup container.
#    Replace <OBJECT_NAME> with the latest from the bucket listing:
#       docker compose -p membership_saas --profile backup run --rm backup \
#           rclone lsf "$BACKUP_RCLONE_REMOTE" | tail -1
docker compose -p membership_saas --profile backup run --rm \
    -e POSTGRES_HOST=membership_saas-restore-target \
    -e POSTGRES_USER=membership_saas \
    -e POSTGRES_PASSWORD=restore_drill_pw \
    -e POSTGRES_DB=membership_saas_restore_drill \
    -e BACKUP_AGE_IDENTITY_FILE=/etc/age/identity.txt \
    -e RESTORE_CONFIRM=YES \
    -v /path/to/ops-backup-2026.txt:/etc/age/identity.txt:ro \
    backup \
    ./restore.sh membership_saas-<OBJECT_NAME>.sql.gz.age

# 3. Spot-check the restored data:
docker exec -it membership_saas-restore-target psql -U membership_saas -d membership_saas_restore_drill \
    -c "SELECT count(*) FROM users;" \
    -c "SELECT count(*) FROM payments;" \
    -c "SELECT max(created_at) FROM payments;"

# 4. Tear down the drill target.
docker stop membership_saas-restore-target
```

Record the drill outcome (date, latest backup timestamp, row counts,
who ran it) in `application/notes/gk-040-restore-drill-YYYY-MM-DD.md`.

### When a real restore is needed

The same `restore.sh` works against the live database. Before doing
this in production:

1. Take a fresh `pg_dump` snapshot of the current (broken) state — you
   may need it for forensics.
2. Stop the `api`, `bot`, and `admin` services so nothing writes during
   the restore.
3. Drop or truncate target tables as needed (the dump uses `CREATE
   TABLE` not `CREATE TABLE IF NOT EXISTS`, so a clean target is best).
4. Run `restore.sh <object>` with `RESTORE_CONFIRM=YES`.
5. Re-run `alembic upgrade head` to ensure schema matches code.
6. Restart services and validate with the post-deploy smoke checklist.

---

## 6. Post-Deploy Smoke Checklist

Run after every deploy (`docker compose -p membership_saas up -d --build`).
Each step has a clear pass/fail.

1. **Container health.**
   ```bash
   docker compose -p membership_saas ps
   ```
   Expect all services `running`/`healthy`. The `api` and `admin`
   containers must show `(healthy)`. The `migrate` service is
   `Exited (0)`.

2. **API liveness.**
   ```bash
   curl -fsS https://community.example.com/health | jq .
   ```
   Expect `status: ok`, `db.ok: true`, `bot.stale: false`.

3. **Admin login page.**
   ```bash
   curl -fsS -I https://community.example.com/ | head -1
   ```
   Expect `HTTP/2 200`.

4. **Bot is online.** Open Telegram, send `/start` to the production
   bot. Expect the standard welcome reply within a few seconds.

5. **Heartbeat is fresh.** Re-run step 2; `bot.heartbeat_age_seconds`
   should be under 120.

6. **Scheduler jobs registered.**
   ```bash
   docker compose -p membership_saas logs --tail=200 bot | grep -i scheduler
   ```
   Expect `kick_expired`, `remind_expiring`, `bot_heartbeat`,
   `scheduler_health` to appear at startup.

7. **Webhook plumbing.** From the Stripe Dashboard → Webhooks, send a
   `invoice.payment_succeeded` test event. Expect a `200` in Stripe's
   delivery log and a corresponding JSON log line in the API with
   `provider=stripe` and a fresh `correlation_id`.

8. **Backup container (if enabled).**
   ```bash
   docker compose -p membership_saas --profile backup logs --tail=80 backup
   ```
   Expect a recent `ok ts=...` line and no error tail. If first deploy:
   expect a one-shot run within ~1 minute of container start.

9. **Sentry test event.** From any container with the DSN set:
   ```bash
   docker compose -p membership_saas exec api python -c \
     "import sentry_sdk; sentry_sdk.capture_message('smoke-test from api')"
   ```
   Expect the message to land in the Sentry project within ~60s.

10. **Telegram ops alert.** Issue a manual test:
    ```bash
    docker compose -p membership_saas exec bot python -c \
      "import asyncio; from app.observability import send_ops_alert; \
       asyncio.run(send_ops_alert('post-deploy smoke test', severity='info'))"
    ```
    Expect a message in `@alert_membership_community`.

If any step fails, the deploy is not done. Roll back with
`docker compose -p membership_saas up -d` against the previous tag/commit and
investigate before retrying.
