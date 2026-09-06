#!/usr/bin/env bash
# GK-040: run backup.sh on an interval, surface failures.
#
# This script is the long-running container command. It sleeps until the next
# scheduled tick, runs backup.sh, and on failure tries to alert via the
# optional BACKUP_ALERT_WEBHOOK_URL. We don't use cron because the official
# Alpine cron doesn't capture exit codes the way we need without extra glue.

set -uo pipefail

interval="${BACKUP_INTERVAL_SECONDS:-86400}"  # 24h default

log() { printf '[entrypoint] %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# GK-427: alert into the same Telegram ops chat the rest of the system uses.
#
# The original failure path was BACKUP_ALERT_WEBHOOK_URL only, and that
# variable has been empty on the live host since the machinery was written —
# so a broken backup told nobody, which is the one failure mode a backup
# system must never have. BOT_TOKEN and ALERT_CHAT_ID already arrive here
# through `env_file: .env`, and delivery to that chat is proven, so reuse it.
# The generic webhook stays supported for anyone wiring healthchecks.io.
alert() {
    message="$1"
    delivered=0

    if [ -n "${BOT_TOKEN:-}" ] && [ -n "${ALERT_CHAT_ID:-}" ]; then
        if curl -fsS --max-time 15 -o /dev/null \
            -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${ALERT_CHAT_ID}" \
            --data-urlencode "disable_web_page_preview=true" \
            --data-urlencode "text=${message}"; then
            delivered=1
        else
            log "telegram alert delivery FAILED"
        fi
    else
        log "telegram alert skipped: BOT_TOKEN/ALERT_CHAT_ID not set"
    fi

    if [ -n "${BACKUP_ALERT_WEBHOOK_URL:-}" ]; then
        body=$(printf '{"text":"%s"}' "$(printf '%s' "$message" | tr '\n' ' ')")
        if curl -fsS --max-time 10 -o /dev/null -X POST -H 'Content-Type: application/json' \
            --data "$body" "$BACKUP_ALERT_WEBHOOK_URL"; then
            delivered=1
        else
            log "webhook alert delivery FAILED"
        fi
    fi

    [ "$delivered" = "1" ] || log "NOBODY WAS ALERTED about: $message"
}

run_once() {
    # `./backup.sh && return 0` rather than `if ./backup.sh; then ... fi`:
    # after a false `if` with no `else`, bash resets $? to 0, so the original
    # "failed (exit $?)" line always logged exit 0. The && short-circuit
    # leaves backup.sh's real status in $?.
    ./backup.sh && return 0
    rc=$?

    # GK-427: exit 3 means the dump was taken, encrypted and uploaded, and only
    # the pruning of old remote copies failed. Alerting that as "BACKUP FAILED"
    # would be a lie, and an alert that cries wolf is on its way to being
    # ignored — which is how the original silent failure survived for weeks.
    if [ "$rc" = "3" ]; then
        log "backup.sh: upload ok, remote retention did not run (exit 3)"
        alert "$(printf '⚠️ membership_saas ops\nBackup uploaded OK — but old copies are NOT being deleted\nwhen: %s\nremote: %s\nToday'"'"'s backup is safe. Left alone, storage fills up and then backups start failing for real. Check the backup container logs.' \
            "$(date -u +%FT%TZ)" "${BACKUP_RCLONE_REMOTE:-?}")"
        return 1
    fi

    log "backup.sh failed (exit $rc)"
    alert "$(printf '🚨 membership_saas ops\nDATABASE BACKUP FAILED\nwhen: %s\ndb: %s\nremote: %s\nexit: %s\nThe newest good dump is whatever preceded this run — check before relying on it.' \
        "$(date -u +%FT%TZ)" "${POSTGRES_DB:-?}" "${BACKUP_RCLONE_REMOTE:-?}" "$rc")"
    return 1
}

# Run immediately on start so the very first deploy produces a backup
# without waiting `interval` seconds for the first tick.
run_once || true

while true; do
    sleep "$interval"
    run_once || true
done
